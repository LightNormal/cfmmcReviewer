"""
期货交易复盘系统 - Web 服务（Flask 前后端分离）
==============================================
后端: Flask RESTful API
前端: static/ 目录下的 HTML/CSS/JS

API 接口:
  GET  /api/login/status        检查登录状态
  GET  /api/vericode            获取验证码图片 (base64)
  POST /api/login               执行登录
  POST /api/logout              登出
  GET  /api/dates               获取已有数据的日期范围
  GET  /api/query?start=&end=   查询指定日期范围的复盘数据
  GET  /api/symbol/<symbol>     查询某品种的所有成交明细 + 统计
  GET  /api/chart/<chart_type>   获取图表（base64 PNG）

用法:
  python3.11 review_web.py
  打开浏览器: http://localhost:8869
"""

import os
import base64
import json
import time
import uuid
import threading
from datetime import date

import matplotlib
matplotlib.use('Agg')  # 非GUI后端，避免 macOS 线程崩溃
import pandas as pd
from bs4 import BeautifulSoup

from flask import Flask, jsonify, request
from flask_cors import CORS

from review import (DataLoader, ReviewEngine, ReviewVisualizer,
                    auto_download_missing, compute_equity_stats, compute_pnl_stats,
                    resolve_output_dir)

import math


def _has_chinese_calendar() -> bool:
    """是否安装了 chinesecalendar（决定节假日能否被正确识别）。"""
    try:
        import chinese_calendar  # noqa: F401
        return True
    except ImportError:
        return False


def _num(v, default=0.0):
    """转成 JSON 安全的数值（NaN/Inf → default）"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return round(f, 2)


def df_to_records(df):
    """DataFrame → dict list，并把 NaN/Inf 清洗为 0/None。

    pandas 的 NaN 会被 json 序列化成非法字面量 `NaN`，
    前端 JSON.parse 直接抛异常，导致整页无法渲染（长周期复盘尤其常见，
    因为缺口日更多、更容易出现空值列）。
    """
    if df is None or df.empty:
        return []
    records = json.loads(df.to_json(orient='records', date_format='iso'))
    for r in records:
        for k, v in list(r.items()):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                r[k] = 0.0
            # datetime 统一为 YYYY-MM-DD。
            # pandas 的 iso 输出形如 '2026-03-03T00:00:00.000'：
            #   下标 10 是 'T'（或空格），11:19 才是 'HH:MM:SS'。
            # 原实现写成了 v[13:19]（得到 ':00:00'），条件永远不成立，
            # 导致前端日期列显示 '03-03T00:00:00.000'。
            if (isinstance(v, str) and len(v) >= 19 and v[4] == '-'
                    and v[10] in ('T', ' ') and v[11:19] == '00:00:00'):
                r[k] = v[:10]
    return records


# 延迟导入 cfMMC crawler（避免未安装依赖时启动失败）
_cfmmc_crawler_module = None
def _get_cfmmc():
    global _cfmmc_crawler_module
    if _cfmmc_crawler_module is None:
        _cfmmc_crawler_module = __import__('cfmmc_crawler')
    return _cfmmc_crawler_module.CFMMCCrawler

app = Flask(__name__, static_folder='static', static_url_path='/static')

# CORS：前后端本来就同源（页面由本服务提供），因此并不需要放开任意源。
# 原实现 CORS(app) 会让**任意网站**都能在用户浏览器里跨域读取 /api/* 的
# 账户号与全部盈亏数据。这里默认只允许本机来源，需要时用环境变量放开：
#   set REVIEW_WEB_CORS_ORIGINS=http://192.168.1.10:8869
_cors_origins = os.environ.get('REVIEW_WEB_CORS_ORIGINS', '').strip()
if _cors_origins:
    CORS(app, resources={r'/api/*': {'origins': [o.strip() for o in _cors_origins.split(',') if o.strip()]}})
else:
    CORS(app, resources={r'/api/*': {'origins': ['http://127.0.0.1:*', 'http://localhost:*']}})

# 品种筛选功能（独立 Blueprint，不侵入复盘逻辑）
from screener_api import screener_bp  # noqa: E402  (需在 app 创建后导入以避免循环依赖)

app.register_blueprint(screener_bp)

# ============================================================
# 全局状态
# ============================================================

_loader = None
_visualizer = ReviewVisualizer()

# 保护 DataLoader 的加载/重新加载。engine 不再做成全局单例（见 _get_engine）。
_loader_lock = threading.RLock()

_CONFIG_FILE = 'config.json'
_config = {}

# 登录相关全局状态
_crawler = None          # CFMMCCrawler 实例（登录后保持 session）
_login_status = {
    'logged_in': False,
    'account_info': None,
    'login_time': None,
}


def _load_config():
    """加载配置文件"""
    global _config
    if not _config and os.path.exists(_CONFIG_FILE):
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            _config = json.load(f)
    return _config


def _get_engine():
    """获取复盘引擎：每次请求新建，只共享只读的数据加载器。

    为什么不再用全局单例 engine：
      Flask 默认 threaded=True，多个请求会共用同一个 ReviewEngine，
      而 /api/query、/api/symbol、/api/daily、/api/chart-data 都会调用
      engine.build_dfs(...) 改写 trades_df / options_df / _matched_cache。
      并发时 A 请求完全可能读到 B 请求的日期范围与统计结果 —— 金额算错且
      完全无法察觉。ReviewEngine 本身很轻（只持有 loader 引用），每次新建的
      代价可以忽略，换来的是请求之间彻底隔离。
    """
    global _loader
    if _loader is None:
        with _loader_lock:
            if _loader is None:
                config = _load_config()
                output_dir = resolve_output_dir(config.get('output_dir', './output'))
                loader = DataLoader(output_dir)
                loader.load_all()
                _loader = loader
    return ReviewEngine(_loader)


def _reload_data():
    """重新扫描并加载结算单（线程安全）。"""
    global _loader
    with _loader_lock:
        if _loader is not None:
            _loader.load_all()
    return _loader


def _maybe_reload(force: bool = False) -> bool:
    """数据文件有变化时自动重载。

    为什么需要：Web 服务把结算单解析进内存后就不再读盘，所以**新下载的数据
    不会自动出现在页面上** —— 用户以为没生效，其实只是进程缓存还是旧的。
    每次进入页面（/api/dates）时比对一次文件指纹，变了就重载，省掉重启服务。
    """
    if _loader is None:
        return False
    if not force:
        try:
            snap = _loader.snapshot()
        except Exception:
            return False
        if snap == getattr(_loader, '_snapshot', None):
            return False

    with _loader_lock:
        before = getattr(_loader, '_snapshot', None)
        _loader.load_all()
        after = getattr(_loader, '_snapshot', None)
    print(f'[API] 数据文件有变化 {before} -> {after}，'
          f'已重新加载 {len(_loader.cache)} 个交易日')
    return True


# ============================================================
# API: 登录相关
# ============================================================

@app.route('/api/login/status', methods=['GET'])
def api_login_status():
    """检查当前登录状态"""
    return jsonify({
        'logged_in': _login_status['logged_in'],
        'account_info': _login_status['account_info'],
        'login_time': _login_status['login_time'],
    })


@app.route('/api/vericode', methods=['GET'])
def api_vericode():
    """
    获取验证码图片（base64 编码）。
    每次调用都会刷新验证码。
    需要先调用过 /api/login 初始化 crawler session。
    """
    global _crawler
    if _crawler is None or _crawler._ss is None:
        return jsonify({'error': '请先初始化登录（POST /api/init-login）'}), 400

    try:
        # 刷新验证码 URL（加时间戳避免缓存）
        CFMMCCrawler = _get_cfmmc()
        veri_url = f'{CFMMCCrawler.base_url}/veriCode.do?t={int(time.time() * 1000)}'
        img_bytes = _crawler._ss.get(veri_url).content
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        return jsonify({
            'image': f'data:image/png;base64,{img_b64}',
        })
    except Exception as e:
        return jsonify({'error': f'获取验证码失败: {e}'}), 500


@app.route('/api/init-login', methods=['POST'])
def api_init_login():
    """
    初始化登录流程：获取登录页面 token + 验证码图片。

    Body JSON (可选，不传则使用 config.json 中的账户):
      account_no: 账号
      password:  密码

    前端流程:
      1. 用户填写账号+密码 → POST /api/init-login {account_no, password}
      2. 后端返回验证码图片
      3. 用户输入验证码 → POST /api/login {vericode}
    """
    global _crawler, _login_status

    # 接收前端传入的账号密码（优先），否则从 config.json / 环境变量读取
    body = request.get_json(silent=True) or {}
    input_account_no = (body.get('account_no') or '').strip()
    input_password = (body.get('password') or '').strip()

    config = _load_config()
    output_dir = config.get('output_dir', './output')

    # 只填一半时必须报错，不能静默回退到配置文件里的账号。
    # 原实现：只要不是"账号和密码都填了"，就走 else 分支使用 config.json 的
    # 账户 —— 用户以为在登录自己的账号，实际登录的是配置文件里那个。
    if bool(input_account_no) != bool(input_password):
        return jsonify({
            'error': '账号和密码必须同时填写；留空则使用配置文件中的账户'
        }), 400

    # 确定账号密码
    if input_account_no and input_password:
        # 使用用户手动输入的账号密码
        account_no = input_account_no
        password = input_password
        fund_name = '手动输入'
        broker = ''
    else:
        # 从环境变量（优先，避免把口令写进 config.json）或 config.json 读取
        env_account_no = os.environ.get('CFMMC_ACCOUNT_NO', '').strip()
        env_password = os.environ.get('CFMMC_PASSWORD', '').strip()
        accounts = config.get('accounts', [])
        if env_account_no and env_password:
            account_no, password = env_account_no, env_password
            fund_name = os.environ.get('CFMMC_FUND_NAME', '环境变量账户')
            broker = os.environ.get('CFMMC_BROKER', '')
        elif accounts:
            account = accounts[0]
            fund_name = account.get('fund_name', '')
            broker = account.get('broker', '')
            account_no = account['account_no']
            password = account['password']
        else:
            return jsonify({
                'error': '请输入账号和密码，或设置 CFMMC_ACCOUNT_NO / CFMMC_PASSWORD 环境变量，'
                         '或在 config.json 中配置账户信息'
            }), 400

    try:
        from cfmmc_crawler import CFMMCCrawler
        from requests import session as create_session

        _crawler = CFMMCCrawler(
            broker, account_no, password,
            output_dir,
            auto_recognize=False,
        )

        # 访问登录页获取 token 和验证码 URL
        _crawler._ss = create_session()
        res = _crawler._ss.get(CFMMCCrawler.login_url, headers=CFMMCCrawler.header)
        bs = BeautifulSoup(res.text, features="html.parser")
        token = bs.body.form.input['value']
        verification_code_url = CFMMCCrawler.base_url + bs.body.form.img['src']

        # 保存 token 到 crawler（login 时需要）
        _crawler._login_token = token

        # 获取验证码图片
        img_bytes = _crawler._ss.get(verification_code_url).content
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')

        return jsonify({
            'success': True,
            'account': {
                'fund_name': fund_name,
                'broker': broker,
                'account_no': account_no,
            },
            'vericode_image': f'data:image/png;base64,{img_b64}',
        })
    except Exception as e:
        print(f'[Init-Login] Error: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'初始化登录失败: {e}'}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    """
    使用用户输入的验证码完成登录。

    Body JSON:
      vericode: 验证码（用户从图片中识别输入）
    """
    global _crawler, _login_status

    if _crawler is None or _crawler._ss is None:
        return jsonify({'error': '请先调用 /api/init-login 初始化'}), 400

    data = request.get_json(silent=True) or {}
    vericode = data.get('vericode', '').strip()

    if not vericode:
        return jsonify({'error': '验证码不能为空'}), 400

    try:
        CFMMCCrawler = _get_cfmmc()
        post_data = {
            "org.apache.struts.taglib.html.TOKEN": getattr(_crawler, '_login_token', ''),
            "showSaveCookies": '',
            "userID": _crawler.account_no,
            "password": _crawler.password,
            "vericode": vericode,
        }
        data_page = _crawler._ss.post(
            CFMMCCrawler.login_url,
            data=post_data,
            headers=CFMMCCrawler.header,
            timeout=10
        )

        # 检查错误
        if "验证码错误" in data_page.text:
            return jsonify({'error': '验证码错误，请重新输入', 'need_refresh': True}), 400

        if '请勿在公用电脑上记录您的查询密码' in data_page.text:
            return jsonify({'error': '用户名或密码错误！请检查 config.json 配置'}), 401

        # 登录成功 → 提取 token
        token = BeautifulSoup(data_page.text, features="html.parser").form.input['value']
        _crawler.token = token

        # 更新登录状态
        config = _load_config()
        account = config.get('accounts', [{}])[0]
        _login_status = {
            'logged_in': True,
            'account_info': {
                'fund_name': account.get('fund_name', ''),
                'account_no': _crawler.account_no,
            },
            'login_time': date.today().isoformat(),
        }

        print(f'[Login] 登录成功! 账号: {_crawler.account_no}')

        return jsonify({
            'success': True,
            'message': '登录成功',
            'status': _login_status,
        })

    except Exception as e:
        return jsonify({'error': f'登录异常: {e}'}), 500


@app.route('/api/logout', methods=['POST'])
def api_logout():
    """登出"""
    global _crawler, _login_status

    if _crawler and _crawler.token:
        try:
            _crawler.logout()
        except Exception:
            pass

    _crawler = None
    _login_status = {
        'logged_in': False,
        'account_info': None,
        'login_time': None,
    }

    return jsonify({'success': True, 'message': '已登出'})


# ============================================================
# API: 数据信息
# ============================================================

@app.route('/api/dates', methods=['GET'])
def api_dates():
    """获取已有数据的日期范围和可用日期列表"""
    engine = _get_engine()
    # 磁盘上的结算单有变化（比如刚下载了新数据）时自动重载，无需重启服务
    _maybe_reload()
    start, end = _loader.get_date_range()
    dates = sorted(_loader.cache.keys())

    # 检查输出目录状态
    config = _load_config()
    output_dir = config.get('output_dir', './output')
    output_dir_exists = os.path.isdir(output_dir)
    has_data = len(dates) > 0

    # 读取配置中的账号信息（用于显示）
    account_info = None
    accounts = config.get('accounts', [])
    if accounts:
        acc = accounts[0]
        account_info = {
            'fund_name': acc.get('fund_name', ''),
            'account_no': acc.get('account_no', ''),
            'broker': acc.get('broker', ''),
        }
    # 如果已登录，优先使用登录后的信息
    if _login_status['logged_in'] and _login_status['account_info']:
        account_info = _login_status['account_info']

    return jsonify({
        'date_range': {'start': start, 'end': end},
        'available_dates': dates,
        'total_days': len(dates),
        'logged_in': _login_status['logged_in'],
        'output_dir_exists': output_dir_exists,
        'has_data': has_data,
        'output_dir': output_dir,
        'account_info': account_info,
        # 未安装 chinesecalendar 时法定节假日不会被识别，会被误判为"缺失交易日"
        'has_chinese_calendar': _has_chinese_calendar(),
        'calendar_note': ('' if _has_chinese_calendar() else
                          '未安装 chinesecalendar：法定节假日会被当作交易日，'
                          '导致反复提示"缺失 N 天"。建议 pip install chinesecalendar'),
    })


# ============================================================
# API: 我交易过的品种（供筛选器「只看自选/持仓品种」使用）
# ============================================================

@app.route('/api/screener/my-symbols', methods=['GET'])
def api_my_symbols():
    """返回结算单里出现过的品种代码，供筛选器限定范围为「我交易过的」。"""
    try:
        engine = _get_engine()
    except Exception as e:
        return jsonify({'symbols': [], 'error': str(e)})

    symbols = set()
    engine.build_dfs()
    if engine.trades_df is not None and not engine.trades_df.empty \
            and 'symbol' in engine.trades_df.columns:
        symbols.update(str(s).upper() for s in engine.trades_df['symbol'].unique())
    opt = engine.get_matched_options_df()
    if opt is not None and not opt.empty and 'symbol' in opt.columns:
        symbols.update(str(s).upper() for s in opt['symbol'].unique())

    return jsonify({'symbols': sorted(symbols), 'count': len(symbols)})


# ============================================================
# API: 结算单原文诊断（排查某合约为何没被识别）
# ============================================================

@app.route('/api/reload', methods=['POST'])
def api_reload():
    """强制重新扫描并加载数据目录（下载完新数据后手动触发）。"""
    _get_engine()          # 确保 loader 已初始化
    changed = _maybe_reload(force=True)
    dates = sorted(_loader.cache.keys())
    return jsonify({
        'success': True,
        'reloaded': changed,
        'total_days': len(dates),
        'date_range': {'start': dates[0] if dates else None,
                       'end': dates[-1] if dates else None},
        'message': f'已重新加载 {len(dates)} 个交易日',
    })


@app.route('/api/debug/data', methods=['GET'])
def api_debug_data():
    """列出数据目录里实际有哪些结算单文件（排查"找不到某天"）。

    返回：数据目录绝对路径、匹配到的文件数、已加载的日期范围、样例文件，
    以及「文件其实存在但位置/格式不对」的线索（.xlsx、不在逐笔目录下）。
    """
    _get_engine()   # 确保 loader 已初始化
    try:
        info = _loader.describe_data()
    except Exception as e:
        return jsonify({'error': f'读取数据目录失败: {e}'}), 500
    return jsonify(_json_safe_json(info))


@app.route('/api/debug/sheet', methods=['GET'])
def api_debug_sheet():
    """导出某天结算单原文，用于排查"某个合约为什么没算盈亏"。

    Query: date=YYYY-MM-DD, contract=可选（只看包含该合约的行）
    """
    date_str = request.args.get('date', '').strip()
    contract = request.args.get('contract', '').strip() or None
    if not date_str:
        return jsonify({'error': '缺少 date 参数'}), 400

    if len(date_str) == 8:
        date_str = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}'

    _get_engine()   # 确保 loader 已初始化
    try:
        dump = _loader.dump_sheet(date_str, contract)
    except Exception as e:
        return jsonify({'error': f'读取结算单失败: {e}'}), 500

    if dump.get('error'):
        return jsonify(dump), 404
    return jsonify(dump)


# ============================================================
# API: 缺失日期诊断（回答"真缺还是本来就没有"）
# ============================================================

@app.route('/api/missing-dates', methods=['GET'])
def api_missing_dates():
    """诊断指定区间内的数据缺口。

    把「没有本地结算单的交易日」分成三类，并列出区间内真正的节假日：
      gaps          夹在已有数据之间的缺口 → 真缺失
      before_range  早于本地最早数据       → 多为账户开立前，补不到
      after_range   晚于本地最晚数据       → 新交易日，值得补
      holidays      区间内休市（元旦/春节/…）→ 本来就不该有
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not start or not end:
        return jsonify({'error': '缺少 start 或 end 参数'}), 400

    def fmt(d):
        d = d.strip()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d

    _get_engine()   # 确保数据已加载
    detail = _loader.analyze_missing_dates(fmt(start), fmt(end))
    return jsonify(_json_safe_json(detail))


def _json_safe_json(obj):
    """极简 JSON 安全化（本模块不需要 pandas 依赖）。"""
    if isinstance(obj, dict):
        return {k: _json_safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe_json(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


# ============================================================
# API: 复盘查询
# ============================================================

@app.route('/api/query', methods=['GET'])
def api_query():
    """
    查询指定日期范围的完整复盘数据。

    Query params:
      start: 开始日期 (YYYY-MM-DD 或 YYYYMMDD)
      end:   结束日期 (YYYY-MM-DD 或 YYYYMMDD)
      auto_download: 是否自动下载缺失数据 (true/false, 默认 true)

    Returns:
      overview, symbol_summary[], trades_sorted[], daily_trend[], missing_dates[]
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    auto_dl = request.args.get('auto_download', 'true').lower() == 'true'

    if not start or not end:
        return jsonify({'error': '缺少 start 或 end 参数'}), 400

    # 格式化日期
    def fmt(d):
        d = d.strip()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d

    start = fmt(start)
    end = fmt(end)

    engine = _get_engine()

    # ---- 检查缺失日期 ----
    # 只报一个总数没用：必须区分「真缺口」「早于最早数据」「晚于最新数据」，
    # 并把区间内的节假日单独列出来，才能回答"到底是真的缺，还是本来就没有"。
    detail = _loader.analyze_missing_dates(start, end)
    to_download = list(detail.get('download_candidates') or [])

    # 只补「值得补」的日期（真缺口 + 更新交易日）。
    # 早于本地最早数据的日期多半是账户开立前，补也补不到，不浪费请求。
    if to_download and auto_dl:
        print(f'[API] 需补 {len(to_download)} 个交易日'
              f'（真缺口 {len(detail.get("gaps") or [])} + '
              f'区间后 {len(detail.get("after_range") or [])}）'
              f': {to_download[:5]}...')
        if _login_status['logged_in'] and _crawler and _crawler.token:
            print('[API] 已登录，尝试自动下载缺失数据...')
            try:
                _download_with_crawler(to_download)
                _reload_data()
                engine = _get_engine()   # 换用重新加载后的数据
                detail = _loader.analyze_missing_dates(start, end)
                if detail.get('missing'):
                    print(f'[API] 下载后仍有 {len(detail["missing"])} 个缺失')
            except Exception as e:
                print(f'[API] 自动下载失败: {e}')
        else:
            # 尝试通过 auto_download_missing（会新建 crawler 登录）
            config = _load_config()
            if config.get('accounts'):
                try:
                    auto_download_missing(to_download, config)
                    _reload_data()
                    engine = _get_engine()
                    detail = _loader.analyze_missing_dates(start, end)
                except Exception as e:
                    print(f'[API] auto_download_missing 失败: {e}')
            else:
                print('[API] 未配置账户信息，跳过自动下载')

    missing = list(detail.get('missing') or [])
    need_download = list(detail.get('download_candidates') or [])

    # 判断是否需要提示用户登录：只有在「确实有值得补的日期」且当前补不了时才提示。
    # 已登录的用户不再弹登录提示，直接用已有数据返回结果
    need_login = False
    did_auto_download = False
    if need_download and auto_dl:
        is_logged_in = _login_status['logged_in'] and _crawler and _crawler.token
        has_config = bool(_load_config().get('accounts'))
        if is_logged_in:
            did_auto_download = True  # 已登录，已尝试自动下载
        # 只有未登录且无配置账户信息时才提示登录
        if not is_logged_in and not has_config:
            need_login = True

    result = engine.query_by_date_range(start, end)

    response = {
        'overview': result['overview'],
        'symbol_summary': df_to_records(result['symbol_summary']),
        'trades_sorted': df_to_records(result['trades_sorted']),
        'daily_trend': df_to_records(result['daily_trend']),
        'missing_dates': missing,
        'missing_count': len(missing),
        # 「值得补」的日期数：前端应以此判断是否需要提示登录/补下载
        'need_download_count': len(need_download),
        'missing_detail': detail,
        'need_login': need_login,
        'did_auto_download': did_auto_download,
        # 期权乘数存疑/未知的提示：前端必须显著展示，否则盈亏会被悄悄放缩
        'multiplier_warnings': result.get('multiplier_warnings', []),
        # 到期作废（虚值到期不产生成交行）被强制结算的合约
        'expiry_settlements': result.get('expiry_settlements', []),
        # 到期日规则未收录、因而未结算的合约
        'expiry_unknown': result.get('expiry_unknown', []),
        # 方向无法识别（行权/履约）的期权行 —— 这些行的盈亏没有被计算
        'unparsed_option_rows': result.get('unparsed_option_rows', []),
        # 解析布局提示（表头无法识别时回退固定列）
        'layout_notes': result.get('layout_notes', []),
    }

    # 期权 TOP 数据：使用 FIFO 匹配后的已实现盈亏（附带权利金现金流参考列 pnl）
    opt_matched = engine.get_matched_options_df()
    if not opt_matched.empty:
        matched_sorted = opt_matched.sort_values('realized_pnl', ascending=False).reset_index(drop=True)
        # 平仓盈亏为 0 的记录不参与 TOP（纯开仓/纯持仓）
        closed_only = matched_sorted[matched_sorted['realized_pnl'] != 0]
        response['opt_top_wins'] = df_to_records(closed_only.head(10))
        tail = closed_only.tail(10).iloc[::-1]
        response['opt_top_losses'] = df_to_records(tail)
        # 期末未平仓合约（已实现盈亏不含浮动部分，供前端提示）
        response['opt_open_positions'] = getattr(engine, 'option_open_positions', {})
    else:
        response['opt_top_wins'] = []
        response['opt_top_losses'] = []
        response['opt_open_positions'] = {}

    return jsonify(response)


def _download_with_crawler(missing_dates):
    """使用已登录的 crawler 下载缺失数据"""
    if not _crawler or not _crawler.token:
        raise RuntimeError('未登录')

    for d in missing_dates:
        dt_obj = __import__('datetime', fromlist=['date']).datetime.strptime(d, '%Y-%m-%d').date()
        for qt in ['逐日', '逐笔']:
            try:
                _crawler.get_daily_data(dt_obj, qt)
            except Exception as e:
                print(f'  下载失败 {d} {qt}: {e}')
        time.sleep(0.5)  # 避免请求过快


# ============================================================
# API: 品种详情（所有成交汇总 + 手续费统计）
# ============================================================

@app.route('/api/symbol/<symbol>', methods=['GET'])
def api_symbol_detail(symbol):
    """
    查询某个品种的所有成交记录 + 详细统计。
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')

    def fmt(d):
        d = d.strip()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d
    if start:
        start = fmt(start)
    if end:
        end = fmt(end)

    engine = _get_engine()
    engine.build_dfs(start, end)

    symbol = symbol.upper()

    # ---- 1. 期货成交明细 ----
    futures_of_symbol = pd.DataFrame() if engine.trades_df.empty else \
        engine.trades_df[engine.trades_df['symbol'] == symbol].copy()

    # 期权：使用 FIFO 匹配后的已实现盈亏（基于全历史配对，记在平仓笔上）
    opt_matched = engine.get_matched_options_df()
    options_of_symbol = pd.DataFrame() if opt_matched.empty else \
        opt_matched[opt_matched['symbol'] == symbol].copy()

    # ---- 2. 基础统计 ----
    total_futures_commission = futures_of_symbol['commission'].sum() if not futures_of_symbol.empty else 0
    total_options_commission = options_of_symbol['commission'].sum() if not options_of_symbol.empty else 0
    total_futures_pnl = futures_of_symbol['realized_pnl'].sum() if not futures_of_symbol.empty else 0
    # 期权盈亏 = FIFO 已实现盈亏；权利金收支单列为参考
    total_options_pnl = options_of_symbol['realized_pnl'].sum() if not options_of_symbol.empty else 0
    total_options_cashflow = options_of_symbol['pnl'].sum() if not options_of_symbol.empty else 0

    # 期权平仓统计（按笔的已实现盈亏）
    # 与品种汇总共用 review.compute_pnl_stats，保证两处口径完全一致
    pnl_values = []
    if not futures_of_symbol.empty:
        pnl_values += futures_of_symbol['realized_pnl'].tolist()
    if not options_of_symbol.empty:
        pnl_values += options_of_symbol['realized_pnl'].tolist()
    pstats = compute_pnl_stats(pnl_values)
    win_count = pstats['wins']
    loss_count = pstats['losses']
    total_closed = pstats['closed']
    avg_win = pstats['avg_win']
    avg_loss = pstats['avg_loss']
    max_win = pstats['max_win']
    max_loss = pstats['max_loss']
    total_volume = futures_of_symbol['volume'].sum() if not futures_of_symbol.empty else 0
    total_amount = futures_of_symbol['amount'].sum() if not futures_of_symbol.empty else 0

    stats = {
        '品种': symbol,
        '期货笔数': int(len(futures_of_symbol)),
        '期权笔数': int(len(options_of_symbol)),
        '平仓笔数': int(total_closed),
        '总成交量(手)': round(total_volume, 1),
        '总成交额(元)': round(total_amount, 2),
        # 期货逐笔平仓盈亏 + 期权FIFO已实现盈亏
        '期货盈亏(元)': round(total_futures_pnl, 2),
        '期货净盈亏(元)': round(total_futures_pnl - total_futures_commission, 2),
        '期权盈亏(元)': round(total_options_pnl, 2),
        '期权权利金收支(元)': round(total_options_cashflow, 2),
        '总盈亏(元)': round(total_futures_pnl + total_options_pnl, 2),
        '净盈亏(元)': round(total_futures_pnl + total_options_pnl - total_futures_commission - total_options_commission, 2),
        '期货手续费(元)': round(total_futures_commission, 2),
        '期权手续费(元)': round(total_options_commission, 2),
        '胜率': pstats['win_rate_text'],
        '盈利次数': win_count,
        '亏损次数': loss_count,
        '均赢(元)': avg_win,
        '均亏(元)': avg_loss,
        '盈亏比': pstats['profit_factor_text'],
        '最大单笔盈利(元)': max_win,
        '最大单笔亏损(元)': max_loss,
    }

    # ---- 3. 期货成交明细（按日期倒序）----
    trades_list = df_to_records(
        futures_of_symbol.sort_values('date', ascending=False)) if not futures_of_symbol.empty else []

    # ---- 4. 期权成交明细（含 FIFO 已实现盈亏列 realized_pnl）----
    opts_list = df_to_records(
        options_of_symbol.sort_values('date', ascending=False)) if not options_of_symbol.empty else []

    # ---- 5. 每日盈亏 & 手续费汇总（期货 + 期权合并）----
    daily_stats = []
    if not futures_of_symbol.empty or not options_of_symbol.empty:
        merged_rows = {}
        if not futures_of_symbol.empty:
            for _, row in futures_of_symbol.iterrows():
                ds = str(row['date'])[:10]
                item = merged_rows.setdefault(ds, {'count': 0, 'pnl': 0.0, 'comm': 0.0})
                item['count'] += 1
                item['pnl'] += float(row.get('realized_pnl') or 0)
                item['comm'] += float(row.get('commission') or 0)
        if not options_of_symbol.empty:
            for _, row in options_of_symbol.iterrows():
                ds = str(row['date'])[:10]
                item = merged_rows.setdefault(ds, {'count': 0, 'pnl': 0.0, 'comm': 0.0})
                item['count'] += 1
                item['pnl'] += float(row.get('realized_pnl') or 0)
                item['comm'] += float(row.get('commission') or 0)
        for ds, item in merged_rows.items():
            daily_stats.append({
                'date': ds,
                '笔数': item['count'],
                '盈亏(元)': round(item['pnl'], 2),
                '手续费(元)': round(item['comm'], 2),
                '净盈亏(元)': round(item['pnl'] - item['comm'], 2),
            })
        daily_stats.sort(key=lambda x: x['date'], reverse=True)

    return jsonify({
        'stats': stats,
        'trades': trades_list,
        'options': opts_list,
        'daily_stats': daily_stats,
    })


# ============================================================
# API: 日期交割单（点击趋势图日期时展示）
# ============================================================

@app.route('/api/daily/<date_str>', methods=['GET'])
def api_daily_detail(date_str):
    """
    查询某一天的全部成交记录（期货 + 期权），用于点击趋势图日期时弹出交割单。
    
    支持格式: YYYY-MM-DD 或 MM-DD（趋势图传的是 MM-DD）
    """
    # 趋势图传的 date 是 MM-DD 格式，需要补全年份
    if len(date_str) == 5 and '-' in date_str:  # MM-DD
        # 使用当前查询范围的年份（取 start 的年份）
        start = request.args.get('start', '')
        end = request.args.get('end', '')
        if start and len(start) >= 4:
            year = start[:4]
            date_str = f'{year}-{date_str}'
        else:
            # 回退到今年
            from datetime import date as _date
            year = str(_date.today().year)
            date_str = f'{year}-{date_str}'

    engine = _get_engine()
    engine.build_dfs()

    # 如果 MM-DD 格式补全后找不到数据，尝试用下一年（跨年场景）
    # 例如查询范围 2025-12 ~ 2026-05，趋势图点击 05-12 实际是 2026-05-12
    has_data = False
    if not engine.trades_df.empty:
        mask = engine.trades_df['date'].astype(str).str[:10] == date_str
        has_data = mask.any()
    if not has_data and not engine.options_df.empty:
        mask = engine.options_df['date'].astype(str).str[:10] == date_str
        has_data = mask.any()

    if not has_data and len(date_str) == 10:
        # 尝试 +1 年
        parts = date_str.split('-')
        try:
            alt_date = f'{int(parts[0]) + 1}-{parts[1]}-{parts[2]}'
            # 检查替代日期是否有数据
            if not engine.trades_df.empty:
                mask = engine.trades_df['date'].astype(str).str[:10] == alt_date
                if mask.any():
                    date_str = alt_date
            if not engine.options_df.empty:
                mask = engine.options_df['date'].astype(str).str[:10] == alt_date
                if mask.any():
                    date_str = alt_date
        except (ValueError, IndexError):
            pass

    # 筛选该日期的数据（期权用全历史 FIFO 匹配后的结果，保证跨窗口平仓也能显示盈亏）
    futures_of_day = pd.DataFrame()
    options_of_day = pd.DataFrame()

    if not engine.trades_df.empty:
        mask = engine.trades_df['date'].astype(str).str[:10] == date_str
        futures_of_day = engine.trades_df[mask].copy()

    opt_matched = engine.get_matched_options_df()
    if not opt_matched.empty:
        mask = opt_matched['date'].astype(str).str[:10] == date_str
        options_of_day = opt_matched[mask].copy()

    # ---- 当日汇总统计 ----
    total_futures_pnl = futures_of_day['realized_pnl'].sum() if not futures_of_day.empty else 0
    total_futures_comm = futures_of_day['commission'].sum() if not futures_of_day.empty else 0
    total_opt_pnl = options_of_day['realized_pnl'].sum() if not options_of_day.empty else 0
    total_opt_comm = options_of_day['commission'].sum() if not options_of_day.empty else 0
    total_futures_volume = futures_of_day['volume'].sum() if not futures_of_day.empty else 0
    total_futures_amount = futures_of_day['amount'].sum() if not futures_of_day.empty else 0
    total_opt_volume = options_of_day['volume'].sum() if not options_of_day.empty else 0

    day_summary = {
        'date': date_str,
        '期货笔数': int(len(futures_of_day)),
        '期权笔数': int(len(options_of_day)),
        '期货成交量(手)': round(total_futures_volume, 1),
        '期货成交额(元)': round(total_futures_amount, 2),
        '期权成交量(手)': round(total_opt_volume, 1),
        '期货盈亏(元)': round(total_futures_pnl, 2),
        '期货手续费(元)': round(total_futures_comm, 2),
        '期货净盈亏(元)': round(total_futures_pnl - total_futures_comm, 2),
        '期权盈亏(元)': round(total_opt_pnl, 2),
        '期权手续费(元)': round(total_opt_comm, 2),
        '当日总盈亏(元)': round(total_futures_pnl + total_opt_pnl, 2),
        '当日总手续费(元)': round(total_futures_comm + total_opt_comm, 2),
        '当日净盈亏(元)': round(total_futures_pnl + total_opt_pnl - total_futures_comm - total_opt_comm, 2),
    }

    return jsonify({
        'summary': day_summary,
        'futures_trades': df_to_records(futures_of_day),
        'options_trades': df_to_records(options_of_day),
    })


# ============================================================
# API: 图表
# ============================================================

@app.route('/api/chart/<chart_type>', methods=['GET'])
def api_chart(chart_type):
    """获取图表图片（base64 编码 PNG）。"""
    start = request.args.get('start', '')
    end = request.args.get('end', '')

    def fmt(d):
        d = d.strip()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d
    if start:
        start = fmt(start)
    if end:
        end = fmt(end)

    engine = _get_engine()
    if start and end:
        engine.build_dfs(start, end)
    else:
        engine.build_dfs()

    sym_df = engine.get_symbol_summary()
    trend_df = engine.get_daily_trend()

    # 每次请求用唯一文件名：固定文件名（symbol_pnl_ranking.png 等）在多请求并发时
    # 会被互相覆盖，导致返回的图与本次的 start/end 参数不符 —— 金额错了还看不出来。
    unique_name = f'api_{chart_type}_{uuid.uuid4().hex}.png'
    path_map = {
        'pnl_bar': lambda p: _visualizer.plot_symbol_pnl_bar(sym_df, save_path=p),
        'daily_trend': lambda p: _visualizer.plot_daily_trend(trend_df, save_path=p),
        'pie': lambda p: _visualizer.plot_symbol_pie(sym_df, save_path=p),
        'win_rate': lambda p: _visualizer.plot_win_rate(sym_df, save_path=p),
    }

    gen_func = path_map.get(chart_type)
    if not gen_func:
        return jsonify({'error': f'未知图表类型: {chart_type}'}), 400

    try:
        chart_path = gen_func(unique_name)
        if not chart_path or not os.path.exists(chart_path):
            return jsonify({'error': '无法生成图表（可能无数据）'}), 404

        try:
            with open(chart_path, 'rb') as f:
                img_base64 = base64.b64encode(f.read()).decode('utf-8')
        finally:
            # 临时图用完即删，避免 review_charts 目录无限堆积
            try:
                os.unlink(chart_path)
            except OSError:
                pass
        return jsonify({'image': f'data:image/png;base64,{img_base64}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# API: 图表数据（JSON，供前端 ECharts 渲染）
# ============================================================

@app.route('/api/chart-data', methods=['GET'])
def api_chart_data():
    """
    返回图表所需的 JSON 数据（不再返回 PNG base64）。
    前端用 ECharts 渲染，支持交互、缩放、tooltip 等。
    
    Query: start, end
    Returns:
      daily_trend: [{date, pnl, commission, futures_pnl, opt_pnl}, ...]
      pie: [{name, value}, ...]  品种盈亏分布
      ranking: [{name, value}, ...]  品种盈亏排名
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')

    def fmt(d):
        d = d.strip()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 else d

    start = fmt(start) if start else ''
    end = fmt(end) if end else ''

    engine = _get_engine()
    engine.build_dfs(start, end)

    trend_df = engine.get_daily_trend()
    symbol_df = engine.get_symbol_summary()

    result = {}

    # 每日趋势数据（完整日期，跨年长周期也不会重复）
    if not trend_df.empty:
        trend_records = df_to_records(trend_df)
        for r in trend_records:
            if 'date' in r and isinstance(r['date'], str) and len(r['date']) >= 10:
                r['date'] = r['date'][:10]
        result['daily_trend'] = trend_records

    # 饼图数据：品种盈亏分布（排除0值）
    if not symbol_df.empty:
        pie_df = symbol_df[symbol_df['总盈亏(元)'] != 0].copy()
        if not pie_df.empty:
            result['pie'] = [{'name': row['品种'], 'value': _num(row['总盈亏(元)'])}
                             for _, row in pie_df.iterrows()]

        # 排名数据：品种盈亏排名
        result['ranking'] = [{'name': row['品种'], 'value': _num(row['总盈亏(元)'])}
                             for _, row in symbol_df.iterrows()]

    # 账户权益曲线（来自结算单资金状况区）
    equity_df = engine.get_equity_curve()
    if not equity_df.empty:
        records = df_to_records(equity_df)
        for r in records:
            if 'date' in r and isinstance(r['date'], str) and len(r['date']) >= 10:
                r['date'] = r['date'][:10]
        result['equity'] = records
        stats = compute_equity_stats(equity_df)
        if stats:
            result['equity_stats'] = stats

    return jsonify(result)


# ============================================================
# 首页路由
# ============================================================

@app.route('/')
def index():
    return app.send_static_file('review.html')


# ============================================================
# 启动
# ============================================================

if __name__ == '__main__':
    # ---- 启动参数：默认只监听本机回环，且关闭 debug ----
    # 原实现是 host='0.0.0.0' + debug=True，风险很大：
    #   1) 局域网内任何设备都能匿名读取账户号、每日盈亏、每笔成交；
    #   2) debug=True 会开启 Werkzeug 交互式调试器，异常页可被用来执行代码。
    # 需要手机/另一台机器访问时，显式指定：
    #   set REVIEW_WEB_HOST=0.0.0.0
    # 需要本机调试时（仅限开发）：
    #   set REVIEW_WEB_DEBUG=1
    host = os.environ.get('REVIEW_WEB_HOST', '127.0.0.1').strip() or '127.0.0.1'
    try:
        port = int(os.environ.get('REVIEW_WEB_PORT', '8869'))
    except ValueError:
        port = 8869
    debug = os.environ.get('REVIEW_WEB_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')

    display_host = 'localhost' if host in ('127.0.0.1', 'localhost') else host
    print("=" * 55)
    print("  📊 期货交易复盘系统 Web 版 (v3.1)")
    print("=" * 55)
    print(f"  服务地址: http://{display_host}:{port}")
    print(f"  监听地址: {host}  |  debug: {'开' if debug else '关'}")
    if host == '0.0.0.0' and not debug:
        print("  ⚠ 已对局域网开放，本服务无登录鉴权，请仅在可信网络使用")
    print(f"  API 接口:")
    print(f"    POST /api/init-login       初始化登录(获取验证码)")
    print(f"    POST /api/login            提交验证码登录")
    print(f"    POST /api/logout           登出")
    print(f"    GET  /api/login/status     查询登录状态")
    print(f"    GET  /api/dates             数据日期范围")
    print(f"    GET  /api/query?start=&end= 复盘查询")
    print(f"    GET  /api/symbol/<品种>     品种详情")
    print(f"    GET  /api/chart-data        图表数据(JSON)")
    print(f"    GET  /api/screener          品种筛选")
    print(f"    GET  /api/watchlist         自选列表")
    print("=" * 55)
    app.run(host=host, port=port, debug=debug)
