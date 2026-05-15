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
import io
import base64
import json
import time
from datetime import date

import matplotlib
matplotlib.use('Agg')  # 非GUI后端，避免 macOS 线程崩溃
import pandas as pd
from bs4 import BeautifulSoup

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from review import DataLoader, ReviewEngine, ReviewVisualizer, auto_download_missing

# 延迟导入 cfMMC crawler（避免未安装依赖时启动失败）
_cfmmc_crawler_module = None
def _get_cfmmc():
    global _cfmmc_crawler_module
    if _cfmmc_crawler_module is None:
        _cfmmc_crawler_module = __import__('cfmmc_crawler')
    return _cfmmc_crawler_module.CFMMCCrawler

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

# ============================================================
# 全局状态
# ============================================================

_loader = None
_engine = None
_visualizer = ReviewVisualizer()

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
    """懒加载/获取引擎"""
    global _loader, _engine
    if _loader is None:
        config = _load_config()
        output_dir = config.get('output_dir', './output')
        _loader = DataLoader(output_dir)
        _loader.load_all()
        _engine = ReviewEngine(_loader)
    return _engine


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

    # 接收前端传入的账号密码（优先），否则从 config.json 读取
    body = request.get_json(silent=True) or {}
    input_account_no = (body.get('account_no') or '').strip()
    input_password = (body.get('password') or '').strip()

    config = _load_config()
    output_dir = config.get('output_dir', './output')

    # 确定账号密码
    if input_account_no and input_password:
        # 使用用户手动输入的账号密码
        account_no = input_account_no
        password = input_password
        fund_name = '手动输入'
        broker = ''
    else:
        # 从 config.json 读取
        accounts = config.get('accounts', [])
        if not accounts:
            return jsonify({'error': '请输入账号和密码，或在 config.json 中配置账户信息'}), 400
        account = accounts[0]
        fund_name = account.get('fund_name', '')
        broker = account.get('broker', '')
        account_no = account['account_no']
        password = account['password']

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
    start, end = _loader.get_date_range()
    dates = sorted(_loader.cache.keys())

    # 检查输出目录状态
    config = _load_config()
    output_dir = config.get('output_dir', './output')
    output_dir_exists = os.path.isdir(output_dir)
    has_data = len(dates) > 0

    return jsonify({
        'date_range': {'start': start, 'end': end},
        'available_dates': dates,
        'total_days': len(dates),
        'logged_in': _login_status['logged_in'],
        'output_dir_exists': output_dir_exists,
        'has_data': has_data,
        'output_dir': output_dir,
    })


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
    missing = _loader.get_missing_dates(start, end)

    # 如果有缺失且开启了自动下载，且已登录
    if missing and auto_dl:
        print(f'[API] 发现 {len(missing)} 个缺失交易日: {missing[:5]}...')
        if _login_status['logged_in'] and _crawler and _crawler.token:
            print('[API] 已登录，尝试自动下载缺失数据...')
            try:
                _download_with_crawler(missing)
                # 重新加载数据
                _loader.load_all()
                engine = ReviewEngine(_loader)
                _engine = engine
                missing = _loader.get_missing_dates(start, end)
                if missing:
                    print(f'[API] 下载后仍有 {len(missing)} 个缺失: {missing}')
            except Exception as e:
                print(f'[API] 自动下载失败: {e}')
        else:
            # 尝试通过 auto_download_missing（会新建 crawler 登录）
            config = _load_config()
            if config.get('accounts'):
                try:
                    auto_download_missing(missing, config)
                    _loader.load_all()
                    engine = ReviewEngine(_loader)
                    _engine = engine
                    missing = _loader.get_missing_dates(start, end)
                except Exception as e:
                    print(f'[API] auto_download_missing 失败: {e}')
            else:
                print('[API] 未配置账户信息，跳过自动下载')

    # 判断是否需要提示用户登录（有缺失数据但未能自动下载）
    need_login = False
    if missing and auto_dl:
        is_logged_in = _login_status['logged_in'] and _crawler and _crawler.token
        has_config = bool(_load_config().get('accounts'))
        if not is_logged_in and not has_config:
            need_login = True

    result = engine.query_by_date_range(start, end)

    # DataFrame → dict list（JSON 序列化）
    def df_to_records(df):
        if df.empty:
            return []
        return df.to_dict(orient='records')

    response = {
        'overview': result['overview'],
        'symbol_summary': df_to_records(result['symbol_summary']),
        'trades_sorted': df_to_records(result['trades_sorted']),
        'daily_trend': df_to_records(result['daily_trend']),
        'missing_dates': missing,
        'missing_count': len(missing),
        'need_login': need_login,
    }

    # 处理 datetime 序列化
    for row in response['daily_trend']:
        if 'date' in row and hasattr(row['date'], 'strftime'):
            row['date'] = row['date'].strftime('%Y-%m-%d')
    for row in response['trades_sorted']:
        if 'date' in row and hasattr(row['date'], 'strftime'):
            row['date'] = row['date'].strftime('%Y-%m-%d')

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
    options_of_symbol = pd.DataFrame() if engine.options_df.empty else \
        engine.options_df[engine.options_df['symbol'] == symbol].copy()

    # ---- 2. 基础统计 ----
    total_futures_commission = futures_of_symbol['commission'].sum() if not futures_of_symbol.empty else 0
    total_options_commission = options_of_symbol['commission'].sum() if not options_of_symbol.empty else 0
    total_futures_pnl = futures_of_symbol['realized_pnl'].sum() if not futures_of_symbol.empty else 0
    total_options_pnl = options_of_symbol['pnl'].sum() if not options_of_symbol.empty else 0

    closed_trades = futures_of_symbol[futures_of_symbol['realized_pnl'] != 0] if not futures_of_symbol.empty else pd.DataFrame()
    wins = closed_trades[closed_trades['realized_pnl'] > 0] if not closed_trades.empty else pd.DataFrame()
    losses = closed_trades[closed_trades['realized_pnl'] < 0] if not closed_trades.empty else pd.DataFrame()

    win_count = len(wins)
    loss_count = len(losses)
    total_closed = win_count + loss_count
    avg_win = wins['realized_pnl'].mean() if len(wins) > 0 else 0
    avg_loss = abs(losses['realized_pnl'].mean()) if len(losses) > 0 else 0
    pf = (wins['realized_pnl'].sum() / abs(losses['realized_pnl'].sum())) if losses['realized_pnl'].sum() != 0 else float('inf')
    max_win = futures_of_symbol['realized_pnl'].max() if not futures_of_symbol.empty else 0
    max_loss = futures_of_symbol['realized_pnl'].min() if not futures_of_symbol.empty else 0
    total_volume = futures_of_symbol['volume'].sum() if not futures_of_symbol.empty else 0
    total_amount = futures_of_symbol['amount'].sum() if not futures_of_symbol.empty else 0

    stats = {
        '品种': symbol,
        '期货笔数': int(len(futures_of_symbol)),
        '期权笔数': int(len(options_of_symbol)),
        '平仓笔数': int(total_closed),
        '总成交量(手)': round(total_volume, 1),
        '总成交额(元)': round(total_amount, 2),
        # 只算期货逐笔平仓盈亏
        '期货盈亏(元)': round(total_futures_pnl, 2),
        '期货净盈亏(元)': round(total_futures_pnl - total_futures_commission, 2),
        # 期权单独列示
        '期权盈亏(元)': round(total_options_pnl, 2),
        '期货手续费(元)': round(total_futures_commission, 2),
        '期权手续费(元)': round(total_options_commission, 2),
        '胜率': f'{win_count/total_closed*100:.1f}%' if total_closed > 0 else 'N/A',
        '盈利次数': win_count,
        '亏损次数': loss_count,
        '均赢(元)': round(avg_win, 2),
        '均亏(元)': round(avg_loss, 2),
        '盈亏比': round(pf, 2) if pf != float('inf') else '∞',
        '最大单笔盈利(元)': round(max_win, 2),
        '最大单笔亏损(元)': round(max_loss, 2),
    }

    # ---- 3. 期货成交明细（按日期倒序）----
    trades_list = []
    if not futures_of_symbol.empty:
        for _, row in futures_of_symbol.sort_values('date', ascending=False).iterrows():
            d = row.to_dict()
            if hasattr(d.get('date'), 'strftime'):
                d['date'] = d['date'].strftime('%Y-%m-%d')
            for k, v in d.items():
                if isinstance(v, (float,)):
                    d[k] = round(v, 2)
            trades_list.append(d)

    # ---- 4. 期权成交明细 ----
    opts_list = []
    if not options_of_symbol.empty:
        for _, row in options_of_symbol.sort_values('date', ascending=False).iterrows():
            d = row.to_dict()
            if hasattr(d.get('date'), 'strftime'):
                d['date'] = d['date'].strftime('%Y-%m-%d')
            for k, v in d.items():
                if isinstance(v, (float,)):
                    d[k] = round(v, 2)
            opts_list.append(d)

    # ---- 5. 每日盈亏 & 手续费汇总 ----
    daily_stats = []
    if not futures_of_symbol.empty:
        for date_str, group in futures_of_symbol.groupby(futures_of_symbol['date'].dt.date if hasattr(futures_of_symbol['date'].iloc[0], 'date') else 'date'):
            ds = str(date_str)
            daily_stats.append({
                'date': ds,
                '笔数': len(group),
                '盈亏(元)': round(group['realized_pnl'].sum(), 2),
                '手续费(元)': round(group['commission'].sum(), 2),
                '净盈亏(元)': round(group['realized_pnl'].sum() - group['commission'].sum(), 2),
            })
        daily_stats.sort(key=lambda x: x['date'], reverse=True)

    return jsonify({
        'stats': stats,
        'trades': trades_list,
        'options': opts_list,
        'daily_stats': daily_stats,
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

    path_map = {
        'pnl_bar': lambda: _visualizer.plot_symbol_pnl_bar(sym_df),
        'daily_trend': lambda: _visualizer.plot_daily_trend(trend_df),
        'pie': lambda: _visualizer.plot_symbol_pie(sym_df),
        'win_rate': lambda: _visualizer.plot_win_rate(sym_df),
    }

    gen_func = path_map.get(chart_type)
    if not gen_func:
        return jsonify({'error': f'未知图表类型: {chart_type}'}), 400

    try:
        chart_path = gen_func()
        if not chart_path or not os.path.exists(chart_path):
            return jsonify({'error': '无法生成图表（可能无数据）'}), 404

        with open(chart_path, 'rb') as f:
            img_base64 = base64.b64encode(f.read()).decode('utf-8')
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

    # 每日趋势数据
    if not trend_df.empty:
        trend_records = trend_df.to_dict(orient='records')
        for r in trend_records:
            if 'date' in r and hasattr(r['date'], 'strftime'):
                r['date'] = r['date'].strftime('%m-%d')
            for k, v in r.items():
                if isinstance(v, (float,)):
                    r[k] = round(v, 2)
        result['daily_trend'] = trend_records

    # 饼图数据：品种盈亏分布（排除0值）
    if not symbol_df.empty:
        pie_df = symbol_df[symbol_df['总盈亏(元)'] != 0].copy()
        if not pie_df.empty:
            result['pie'] = [{'name': row['品种'], 'value': round(row['总盈亏(元)'], 2)}
                             for _, row in pie_df.iterrows()]

        # 排名数据：品种盈亏排名
        result['ranking'] = [{'name': row['品种'], 'value': round(row['总盈亏(元)'], 2)}
                            for _, row in symbol_df.iterrows()]

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
    print("=" * 55)
    print("  📊 期货交易复盘系统 Web 版 (v3.0)")
    print("=" * 55)
    print(f"  服务地址: http://localhost:8869")
    print(f"  API 接口:")
    print(f"    POST /api/init-login       初始化登录(获取验证码)")
    print(f"    POST /api/login            提交验证码登录")
    print(f"    POST /api/logout           登出")
    print(f"    GET  /api/login/status     查询登录状态")
    print(f"    GET  /api/dates             数据日期范围")
    print(f"    GET  /api/query?start=&end= 复盘查询")
    print(f"    GET  /api/symbol/<品种>     品种详情")
    print(f"    GET  /api/chart/<类型>       图表")
    print("=" * 55)
    app.run(host='0.0.0.0', port=8869, debug=True)
