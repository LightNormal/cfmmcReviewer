import datetime as dt
import json
import os
import time
from io import BytesIO
from typing import Sequence

import PIL.ImageOps
from PIL import Image
from bs4 import BeautifulSoup
from requests import session


class UserNamePasswordError(ValueError):
    pass


class VerificationCodeError(ValueError):
    pass


class CFMMCCrawler(object):
    # modular constants, mostly web addresses
    base_url = "https://investorservice.cfmmc.com"
    login_url = base_url + '/login.do'
    logout_url = base_url + '/logout.do'
    data_url = base_url + '/customer/setParameter.do'
    excel_daily_download_url = base_url + '/customer/setupViewCustomerDetailFromCompanyWithExcel.do'
    excel_monthly_download_url = base_url + '/customer/setupViewCustomerMonthDetailFromCompanyWithExcel.do'
    header = {
        'Connection': 'keep-alive',
        'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    query_type_dict = {'逐日': 'day', '逐笔': 'trade'}
    MAX_RETRY = 50              # 验证码自动识别最大重试次数
    VERI_CODE_LENGTH = 6       # 验证码预期长度

    def __init__(self, broker: str,
                 account_no: str, password: str,
                 output_dir: str,
                 auto_recognize: bool = True) -> None:
        """
        从期货保证金结算中心下载期货结算单到本地
        本地输出地址为 output_dir/broker_account_no/日报 或 月报/逐日 或 逐笔/account_no_date.xls

        :param broker: 期货公司
        :param account_no: 账号
        :param password: 密码
        :param output_dir: 输出目录
        :param auto_recognize: 是否自动识别验证码（默认True），False则手动输入
        """

        self.broker =  broker
        self.account_no, self.password = account_no, password
        self.auto_recognize = auto_recognize

        self.output_dir = output_dir

        self._ss = None
        self.token = None

    @staticmethod
    def _recognize_veri_code(image_bytes: bytes) -> str | None:
        """
        使用 PIL 图像预处理（二值化+反色）后保存临时文件供 Tesseract 识别。
        返回识别结果，失败返回 None。
        注意: 需要系统安装 tesseract (macOS: brew install tesseract)
        """
        try:
            import subprocess
            import tempfile

            im = Image.open(BytesIO(image_bytes))
            im = im.convert('L')
            # 二值化（阈值210）+ 反色，增强对比度
            binary = im.point([0 if i < 210 else 1 for i in range(256)], '1')
            inverted = PIL.ImageOps.invert(binary.convert('L'))

            # 写入临时文件给 tesseract CLI 调用
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
                inverted.save(tmp_path)

            result = subprocess.run(
                ['tesseract', tmp_path, 'stdout', '-l', 'eng', '--psm', '7'],
                capture_output=True, text=True, timeout=10
            )
            os.unlink(tmp_path)

            code = result.stdout.strip()
            return code if code else None
        except FileNotFoundError:
            # tesseract 未安装
            return None
        except Exception:
            return None

    def _get_veri_code(self, verification_code_url: str) -> str:
        """
        获取验证码：优先自动识别，失败则回退到手动输入。
        """
        img_bytes = self._ss.get(verification_code_url).content

        if self.auto_recognize:
            code = self._recognize_veri_code(img_bytes)
            if code and len(code) == self.VERI_CODE_LENGTH:
                code = ''.join(filter(str.isalnum, code))
                print(f'  自动识别验证码: {code}')
                return code
            else:
                print(f'  自动识别失败(code="{code}"), 切换到手动输入...')

        # 手动输入 fallback
        with Image.open(BytesIO(img_bytes)) as img:
            img.show()
            code = input('请输入验证码: ').strip()
        return code

    def login(self, max_retry: int | None = None) -> None:
        """
        登录（支持自动重试验证码）
        :param max_retry: 验证码最大重试次数，默认使用类常量 MAX_RETRY
        """
        if max_retry is None:
            max_retry = self.MAX_RETRY

        self._ss = session()
        res = self._ss.get(self.login_url, headers=self.header)
        bs = BeautifulSoup(res.text, features="html.parser")
        token = bs.body.form.input['value']
        verification_code_url = self.base_url + bs.body.form.img['src']

        for attempt in range(max_retry):
            print(f'  第 {attempt + 1}/{max_retry} 次尝试登录...')

            try:
                verification_code = self._get_veri_code(verification_code_url)
            except Exception as e:
                print(f'  获取验证码异常: {e}')
                time.sleep(1)
                # 刷新验证码 URL
                verification_code_url = f'{self.base_url}/veriCode.do?t={int(time.time() * 1000)}'
                continue

            if not verification_code:
                raise VerificationCodeError('验证码为空')

            post_data = {
                "org.apache.struts.taglib.html.TOKEN": token,
                "showSaveCookies": '',
                "userID": self.account_no,
                "password": self.password,
                "vericode": verification_code,
            }
            data_page = self._ss.post(self.login_url, data=post_data, headers=self.header, timeout=5)

            if "验证码错误" in data_page.text:
                print(f'  ✗ 验证码错误, 重试...')
                time.sleep(1)
                # 刷新验证码 URL
                verification_code_url = f'{self.base_url}/veriCode.do?t={int(time.time() * 1000)}'
                continue

            if '请勿在公用电脑上记录您的查询密码' in data_page.text:
                raise UserNamePasswordError('用户名密码错误!')

            print('✓ 登录成功!')
            self.token = self._get_token(data_page.text)
            return

        raise VerificationCodeError(f'登录失败: 已重试 {max_retry} 次仍无法通过验证码!')

    def logout(self) -> None:
        """
        登出
        """
        if self.token:
            self._ss.post(self.logout_url)
            self.token = None

    def _check_args(self, query_type: str) -> None:
        if not self.token:
            raise RuntimeError('需要先登录成功才可进行查询!')

        if query_type not in self.query_type_dict.keys():
            raise ValueError('query_type 必须为 逐日 或 逐笔 !')

    def get_daily_data(self, date: dt.date, query_type: str) -> None:
        """
        下载日报数据（如果文件已存在则跳过）

        :param date: 日期
        :param query_type: 逐日 或 逐笔
        :return: None
        """
        self._check_args(query_type)

        trade_date = date.strftime('%Y-%m-%d')
        path = os.path.join(self.output_dir, self.broker + '_' + self.account_no, '日报', query_type)
        file_name = self.account_no + '_' + trade_date + '.xls'
        full_path = os.path.join(path, file_name)

        # 文件已存在则跳过
        if os.path.exists(full_path):
            print(f'  跳过 (已存在): {full_path}')
            return

        os.makedirs(path, exist_ok=True)

        post_data = {
            "org.apache.struts.taglib.html.TOKEN": self.token,
            "tradeDate": trade_date,
            "byType": self.query_type_dict[query_type]
        }
        data_page = self._ss.post(self.data_url, data=post_data, headers=self.header, timeout=5)
        self.token = self._get_token(data_page.text)

        self._download_file(self.excel_daily_download_url, full_path)

    def get_monthly_data(self, month: dt.date, query_type: str) -> None:
        """
        下载月报数据（如果文件已存在则跳过）

        :param month: 日期
        :param query_type: 逐日 或 逐笔
        :return: None
        """
        self._check_args(query_type)

        trade_date = month.strftime('%Y-%m')
        path = os.path.join(self.output_dir, self.broker + '_' + self.account_no, '月报', query_type)
        file_name = self.account_no + '_' + trade_date + '.xls'
        full_path = os.path.join(path, file_name)

        # 文件已存在则跳过
        if os.path.exists(full_path):
            print(f'  跳过 (已存在): {full_path}')
            return

        os.makedirs(path, exist_ok=True)

        post_data = {
            "org.apache.struts.taglib.html.TOKEN": self.token,
            "tradeDate": trade_date,
            "byType": self.query_type_dict[query_type]
        }
        data_page = self._ss.post(self.data_url, data=post_data, headers=self.header, timeout=5)
        self.token = self._get_token(data_page.text)

        self._download_file(self.excel_monthly_download_url, full_path)

    @staticmethod
    def _get_token(page: str) -> str:
        token = BeautifulSoup(page, features="html.parser").form.input['value']
        return token

    def _download_file(self, web_address: str, download_path: str) -> None:
        excel_response = self._ss.get(web_address)
        with(open(download_path, 'wb')) as fh:
            fh.write(excel_response.content)
        print('下载 ', download_path, ' 完成!')

    def get_trading_days(self, start_date: str, end_date: str, exclude_weekend: bool = True) -> Sequence[dt.date]:
        """
        生成日期范围内的所有交易日

        :param start_date: 开始日期，格式 YYYYMMDD
        :param end_date: 结束日期，格式 YYYYMMDD
        :param exclude_weekend: 是否排除周末（默认True）
        :return: 日期列表
        """
        start = dt.date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
        end = dt.date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:8]))
        dates = []
        current = start
        while current <= end:
            if not exclude_weekend or current.weekday() < 5:  # 周一=0 ... 周五=4
                dates.append(current)
            current += dt.timedelta(days=1)
        return dates

    def batch_daily_download(self, start_date: str, end_date: str, exclude_weekend: bool = True) -> None:
        """
        批量日报下载, 包括逐日和逐笔
        :param start_date: 开始日期
        :param end_date: 结束日期
        :param exclude_weekend: 是否排除周末（默认True）
        :return: None
        """
        all_dates = self.get_trading_days(start_date, end_date, exclude_weekend)
        total = len(all_dates)
        print(f'\n批量下载日报: 共 {total} 个交易日 ({start_date} ~ {end_date})')
        print('=' * 50)
        success, fail = 0, 0
        for idx, date in enumerate(all_dates):
            for query_type in self.query_type_dict.keys():
                try:
                    self.get_daily_data(date, query_type)
                    success += 1
                except Exception as e:
                    print(f'  ✗ 跳过 {date.strftime("%Y-%m-%d")} {query_type}: {e}')
                    fail += 1
                    continue
            # 每个日期间隔一下，避免请求过快
            if idx < total - 1:
                time.sleep(1)
        print(f'\n批量日报完成! 成功: {success}, 失败: {fail}, 总计: {success + fail}')

    def batch_monthly_download(self, start_date: str, end_date: str) -> None:
        """
        批量月报下载, 包括昨日和逐笔
        :param start_date: 开始日期
        :param end_date: 结束日期
        :return: None
        """
        query_months = self._generate_months_first_day(start_date, end_date)
        for month in query_months:
            for query_type in self.query_type_dict.keys():
                self.get_monthly_data(month, query_type)

    @staticmethod
    def _generate_months_first_day(start_date: str, end_date: str) -> Sequence[dt.date]:
        start = dt.date(int(start_date[:4]), int(start_date[4:6]), 1)
        end = dt.date(int(end_date[:4]), int(end_date[4:6]), 1)
        storage = []
        while start <= end:
            storage.append(start)
            start = dt.date(start.year + start.month // 12, (start.month + 1) % 13 + start.month // 12, 1)
        return storage


if __name__ == '__main__':
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # integrity check
    needed_keys = ['accounts', 'start_date', 'end_date', 'output_dir']
    for key in needed_keys:
        if key not in config.keys():
            raise ValueError(key + '不在config中')

    # let it begin
    for account in config['accounts']:
        crawler = CFMMCCrawler(account['fund_name'], account['broker'], account['account_no'], account['password'],
                               config['output_dir'])
        print('正在登陆 ', account['fund_name'], ' - ', account['broker'])
        while crawler.token is None:
            try:
                crawler.login()
            except UserNamePasswordError as e:
                print(e)
                break
            except VerificationCodeError as e:
                print(e)

        if crawler.token:
            crawler.batch_daily_download(config['start_date'], config['end_date'])
            crawler.batch_monthly_download(config['start_date'], config['end_date'])
            print('完成操作, 登出!')
            crawler.logout()