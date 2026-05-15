"""
CFMMC 爬虫 Web 服务
提供 HTTP 接口调用爬虫功能：
  GET /cfmmc              - 单日查询
  GET /cfmmc/batch        - 批量日期范围查询
"""

import json

import tornado.ioloop
import tornado.web

import cfmmc as cf


class MainHandler(tornado.web.RequestHandler):
    """单日查询接口"""

    def get(self):
        account = self.get_argument('account')
        passwd = self.get_argument('passwd')
        print(f'[请求] 单日查询 - 账户: {account}')
        result = cf.do(account, passwd)
        self.set_header('Content-Type', 'application/json; charset=utf-8')
        self.write(json.dumps(result, ensure_ascii=False) if result else json.dumps({'error': '查询失败'}))


class BatchHandler(tornado.web.RequestHandler):
    """批量日期范围查询接口"""

    def get(self):
        account = self.get_argument('account')
        passwd = self.get_argument('passwd')
        start_date = self.get_argument('startDate')   # 开始日期 YYYY-MM-DD
        end_date = self.get_argument('endDate')       # 结束日期 YYYY-MM-DD
        output_file = self.get_argument('outputFile', default=None)  # 可选输出文件名

        print(f'[请求] 批量查询 - 账户: {account}, 范围: {start_date} ~ {end_date}')

        results = cf.do_batch(
            user_id=account,
            passwd=passwd,
            start_date=start_date,
            end_date=end_date,
            output_file=output_file,
        )

        self.set_header('Content-Type', 'application/json; charset=utf-8')
        self.write(json.dumps({
            'total': len(results),
            'success': sum(1 for r in results if r.get('currentEquity') is not None),
            'data': results,
        }, ensure_ascii=False))


def make_app():
    return tornado.web.Application([
        (r"/cfmmc", MainHandler),
        (r"/cfmmc/batch", BatchHandler),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(8868)
    print("CFMMC Web 服务已启动: http://localhost:8868")
    print("  单日查询:   /cfmmc?account=xxx&passwd=xxx")
    print("  批量查询:   /cfmmc/batch?account=xxx&passwd=xxx&startDate=2026-04-01&endDate=2026-05-10")
    tornado.ioloop.IOLoop.current().start()
