# -*- coding: utf-8 -*-
"""Web API 冒烟测试：注入模拟数据，走 Flask test_client 验证各接口 JSON 可用。

检查点：
  - /api/query 期权盈亏为 FIFO 已实现值而非权利金
  - /api/symbol 返回的期权明细含 realized_pnl
  - /api/daily 正常返回
  - /api/chart-data 每日趋势为完整日期且数值 JSON 安全
  - 全部响应可被严格 JSON 解析（无 NaN/Infinity 字面量）
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_engine import build_day, patched_parse  # noqa: E402

import review  # noqa: E402
import review_web  # noqa: E402


def fake_load_all(self):
    self.cache = {}
    days = {
        '2026-03-02': ([], [
            ['IO2606-C-4000', 'IO', '看涨', 4000, '买方', 200, 2, -40000, None, 10],
            ['IO2606-P-3800', 'IO', '看跌', 3800, '卖方', 100, 1, 10000, None, 5]],
            {'平仓盈亏': 0, '当日手续费': 15, '客户权益': 1000000}),
        '2026-03-03': ([
            ['ag2606', '买', '投机', 8000, 1, 120000, None, '开', 5, '--'],
            ['ag2606', '卖', '投机', 8100, 1, 121500, None, '平', 5, 1500]], [
            ['IO2606-C-4000', 'IO', '看涨', 4000, '卖方', 260, 2, 52000, None, 10],
            ['IO2606-P-3800', 'IO', '看跌', 3800, '买方', 60, 1, -6000, None, 5]],
            {'平仓盈亏': 1500, '当日手续费': 20, '客户权益': 1002000}),
        '2026-03-04': ([], [
            ['MO2606-C-6000', 'MO', '看涨', 6000, '卖方', 50, 3, 15000, None, 6],
            ['MO2606-C-6000', 'MO', '看涨', 6000, '买方', 80, 3, -24000, None, 6]],
            {'平仓盈亏': 0, '当日手续费': 12, '客户权益': 1001000}),
        '2026-03-05': ([], [], {'平仓盈亏': 0, '当日手续费': 0, '客户权益': 1001000}),
    }
    for d, (f, o, s) in days.items():
        parsed = patched_parse(build_day(d, f, o, s))
        parsed['date'] = d
        self.cache[d] = parsed
    return self.cache


review.DataLoader.load_all = fake_load_all
review_web.app.config['TESTING'] = True
client = review_web.app.test_client()

STRICT = re.compile(r'\b(NaN|Infinity|-Infinity)\b')


def get_json(path):
    resp = client.get(path)
    assert resp.status_code == 200, f'{path} -> {resp.status_code}: {resp.data[:300]}'
    text = resp.get_data(as_text=True)
    assert not STRICT.search(text), f'{path} 含非法 JSON 字面量 NaN/Infinity'
    return json.loads(text)


# ---- /api/query ----
q = get_json('/api/query?start=2026-03-02&end=2026-03-05&auto_download=false')
ov = q['overview']
assert abs(ov['期权盈亏(元)'] - 7000.0) < 1e-6, ov
assert abs(ov['期权权利金收支(元)'] - 7000.0) < 1e-6, ov
# 期权 TOP 应使用已实现盈亏（含乘数100）：最大盈利 +12000（Day2 平C）
assert q['opt_top_wins'] and abs(q['opt_top_wins'][0]['realized_pnl'] - 12000.0) < 1e-6, q['opt_top_wins'][:2]
assert q['opt_top_losses'] and abs(q['opt_top_losses'][0]['realized_pnl'] - (-9000.0)) < 1e-6, q['opt_top_losses'][:2]
assert 'opt_open_positions' in q
print('✓ /api/query 期权FIFO盈亏(含乘数)+TOP榜正确')

# ---- 跨窗口（起点在开仓日之后）----
q2 = get_json('/api/query?start=2026-03-03&end=2026-03-05&auto_download=false')
assert abs(q2['overview']['期权盈亏(元)'] - 7000.0) < 1e-6, q2['overview']
print('✓ /api/query 跨窗口平仓盈亏正确')

# ---- /api/symbol/IO ----
s = get_json('/api/symbol/IO?start=2026-03-02&end=2026-03-05')
assert abs(s['stats']['期权盈亏(元)'] - 16000.0) < 1e-6, s['stats']
assert 'realized_pnl' in s['options'][0], list(s['options'][0].keys())
assert 'multiplier' in s['options'][0], list(s['options'][0].keys())
print('✓ /api/symbol 期权已实现盈亏(含乘数)与明细列正确')

# ---- /api/daily ----
d = get_json('/api/daily/2026-03-03?start=2026-03-02')
assert abs(d['summary']['期权盈亏(元)'] - 16000.0) < 1e-6, d['summary']
# 当日手续费: 期货 5+5=10, 期权 10+5=15 → 净盈亏 = 1500+16000-25
assert abs(d['summary']['当日净盈亏(元)'] - (1500 + 16000 - 25)) < 1e-6, d['summary']
print('✓ /api/daily 当日期权已实现盈亏正确')

# ---- /api/chart-data ----
c = get_json('/api/chart-data?start=2026-03-02&end=2026-03-05')
dates = [r['date'] for r in c['daily_trend']]
assert all(re.match(r'^\d{4}-\d{2}-\d{2}$', x) for x in dates), dates
rank_names = {r['name'] for r in c['ranking']}
assert {'IO', 'AG', 'MO'} <= rank_names, rank_names
print('✓ /api/chart-data 完整日期与排名数据正确')

# ---- 空范围 ----
e = get_json('/api/query?start=2027-01-01&end=2027-01-31&auto_download=false')
assert e['overview']['交易日数'] == 0
print('✓ 空范围查询正常返回')

print('\nAPI 冒烟测试全部通过 ✔')
