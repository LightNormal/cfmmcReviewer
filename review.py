"""
期货交易复盘系统
================
功能：
  1. 解析监控中心下载的逐笔/逐日 xls 结算单
  2. 按日期范围查询盈亏，按盈利金额排序
  3. 品种汇总（Ag2604 + Ag2608 + Ag2604C15000 → Ag）
  4. 统计：胜率、盈亏比、手续费、品种排名
  5. 可视化：盈亏柱状图、品种饼图、趋势图
  6. 自动补数：缺失日期自动从监控中心下载

用法：
  python review.py                    # 交互式 CLI
  python review.py --start 20260401 --end 20260510   # 命令行模式
"""

import os
import re
import json
import glob
import argparse
import threading
import functools
import time
import uuid
from datetime import datetime, timedelta

import xlrd
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # 非GUI后端，兼容 Web 服务和 CLI
import matplotlib.pyplot as plt
matplotlib.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'PingFang SC']
matplotlib.rcParams['axes.unicode_minus'] = False


# ============================================================
# 配置
# ============================================================
DEFAULT_OUTPUT_DIR = './output'
CONFIG_FILE = 'config.json'


# ============================================================
# 中国证券市场交易日判断
# ============================================================
# 优先使用 chinesecalendar 库（专业维护中国法定节假日及调休安排）
# pip install chinesecalendar   → 每年更新，含调休上班日
# 若未安装或超出数据年份范围，回退到仅排除周末
try:
    import chinese_calendar  # type: ignore
    _HAS_CHINESE_CALENDAR = True
except ImportError:
    _HAS_CHINESE_CALENDAR = False


def is_cn_trading_day(date_str: str) -> bool:
    """
    判断某日是否为中国证券市场的交易日（非周末且非法定节假日）。

    优先使用 chinesecalendar 库（自动处理历年节假日及调休上班日）；
    若未安装或超出其数据年份范围，回退到仅排除周末。
    """
    if not date_str:
        return False
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return False

    # 排除周末
    if dt.weekday() >= 5:
        return False

    # 优先使用 chinesecalendar（专业中国假期库，含调休上班日）
    if _HAS_CHINESE_CALENDAR:
        try:
            return chinese_calendar.is_workday(dt)
        except NotImplementedError:
            # 超出 chinesecalendar 数据年份范围，回退
            pass

    # 回退：无额外库或超出范围时仅排除周末
    # 建议安装: pip install chinesecalendar
    return True


def trading_calendar_note() -> str:
    """未安装 chinesecalendar 时的提示文案（已安装返回空串）。"""
    if _HAS_CHINESE_CALENDAR:
        return ''
    return ('未安装 chinesecalendar：法定节假日会被当作交易日，'
            '导致"缺失 N 天"里混入假期。建议 pip install chinesecalendar')


# ============================================================
# 工具函数
# ============================================================
def _to_float(val) -> float:
    """把单元格转成 float，尽量宽容地处理结算单里各种写法。

    真实结算单里同一个数值字段可能写成：
      '3,900.00'（千分位）、'+0.00'（带正号）、'(1,234.5)'（括号负数）、
      '--' / '-' / '—'（无数据）、全角逗号、前后空格。
    直接 float() 会全部失败并静默变成 0 —— 这会让价格/手数/盈亏悄悄错掉。
    """
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return 0.0
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    s = str(val).strip()
    if not s:
        return 0.0
    # 全角数字/符号 → 半角
    s = s.translate(str.maketrans('０１２３４５６７８９．，－＋（）',
                                  '0123456789.,-+()'))
    s = s.replace(',', '').replace(' ', '').replace('\u00a0', '')
    if s in ('--', '-', '—', '——', '---', '/', 'N/A', 'nan', 'None'):
        return 0.0

    negative = False
    if s.startswith('(') and s.endswith(')'):
        negative = True
        s = s[1:-1]

    try:
        f = float(s)
    except ValueError:
        # 兜底：抽出第一个数字（例如 '15.01元' 这种带单位的写法）
        m = re.search(r'-?\d+(?:\.\d+)?', s)
        if not m:
            return 0.0
        try:
            f = float(m.group(0))
        except ValueError:
            return 0.0
    return -f if negative else f


def _parse_pnl(val) -> float:
    s = str(val).strip()
    if s in ('--', '', '-'):
        return 0.0
    return _to_float(s)


def _normalize_account(raw) -> str:
    """把结算单里的资金账号规范化成 12 位数字字符串。

    xlrd 对数值型单元格返回 float（如 72503313361.0），
    直接 str().zfill(12) 会得到 '72503313361.0' 这种脏值。
    """
    s = str(raw).strip()
    if not s:
        return ''
    # 去掉数值型尾缀 '.0'
    if re.fullmatch(r'\d+\.0+', s):
        s = s.split('.')[0]
    elif re.fullmatch(r'\d+\.\d+', s):
        # 其它浮点形式（理论上不该出现）也退化为整数部分
        s = s.split('.')[0]
    return s.zfill(12) if s.isdigit() else s


def compute_pnl_stats(pnl_values) -> dict:
    """根据已实现盈亏序列计算胜率/盈亏比等统计量。

    口径（品种汇总与品种详情共用，避免两处各写一份导致互相矛盾）：
      - 只统计「已实现盈亏 ≠ 0」的记录；纯开仓（结算单开仓列为 '--' → 0）不参与
      - 胜率 = 盈利笔数 / (盈利笔数 + 亏损笔数)
      - 盈亏比 = 总盈利 / |总亏损|
          * 无亏损但有盈利 → '∞'
          * 无已平仓记录   → '-'
          * 全部亏损       → 0.0（而不是 '∞' —— 这正是旧实现的问题：
            纯期权亏损品种因期货侧无记录被算成"盈亏比 ∞"，看着完美实则全亏）

    返回 dict，含数值型与展示用字符串两套字段。
    """
    vals = [_to_float(v) for v in (pnl_values or [])]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]

    n_win, n_loss = len(wins), len(losses)
    n_closed = n_win + n_loss
    gross_win = float(sum(wins))
    gross_loss = float(sum(losses))          # 负数或 0

    if n_closed == 0:
        win_rate, pf = None, None
        win_rate_text = '-'
        pf_text = '-'
    else:
        win_rate = n_win / n_closed * 100
        win_rate_text = f'{win_rate:.1f}%'
        if gross_loss == 0:
            pf = None                        # 无亏损单，比值无定义
            pf_text = '∞' if gross_win > 0 else '-'
        else:
            pf = gross_win / abs(gross_loss)
            pf_text = f'{pf:.2f}'

    return {
        'closed': n_closed,
        'wins': n_win,
        'losses': n_loss,
        'win_rate': win_rate,                # float | None
        'win_rate_text': win_rate_text,
        'profit_factor': pf,                 # float | None
        'profit_factor_text': pf_text,
        'gross_win': round(gross_win, 2),
        'gross_loss': round(gross_loss, 2),
        'avg_win': round(gross_win / n_win, 2) if n_win else 0.0,
        'avg_loss': round(abs(gross_loss) / n_loss, 2) if n_loss else 0.0,
        'max_win': round(max(wins), 2) if wins else 0.0,
        'max_loss': round(min(losses), 2) if losses else 0.0,
    }


def extract_symbol(contract: str) -> str:
    """从合约名提取品种代码。AG2608→AG, Ag2604C15000→Ag, SC2605P600→SC"""
    s = contract.strip().upper()
    s = s.split('-')[0]
    m = re.match(r'^([A-Z]+)', s)
    return m.group(1) if m else s


# ============================================================
# 期权合约乘数（每点代表多少元）
# ============================================================
# 期权权利金报价单位为「点」，真实盈亏必须乘以合约乘数：
#   盈亏(元) = 权利金价差(点) × 手数 × 合约乘数(元/点)
#
# 口径说明：国内商品期货期权的「1 个点」= 1 个报价单位（元/吨、元/克、元/桶…），
# 而一手期权对应的数量 = 标的期货的「交易单位」。因此
#   合约乘数(元/点) ≡ 标的期货交易单位
# 例：沪铜 5 吨/手 → 5；沪金 1000 克/手 → 1000；原油 1000 桶/手 → 1000；
#     多晶硅 3 吨/手 → 3；工业硅 5 吨/手 → 5；碳酸锂 1 吨/手 → 1。
#
# 本表仅作**兜底**：结算单的权利金字段本身已含乘数（=单价×手数×乘数），
# 解析时优先用反推值（见 _infer_multiplier），能自动覆盖任意新品种。
#
# 维护提示：新增期权品种时务必同步本表，否则在反推失败（权利金列缺失/为 0）
# 时会退回 1.0，导致盈亏被放缩数倍乃至上千倍。tests/test_review_engine.py
# 里有覆盖率断言，会检查常见期权品种都在表内。
OPTION_MULTIPLIER = {
    # ---- CFFEX 中金所 ----
    'IO': 100, 'MO': 100, 'HO': 100,     # 沪深300/中证1000/上证50 股指期权（100 元/点）
    'IF': 300, 'IC': 200, 'IM': 200, 'IH': 300,   # 股指期货（无对应期权，仅备查）
    'T': 10000, 'TF': 10000, 'TL': 10000,          # 国债期货：面值 100 万 → 1 点 = 1 万元
    'TS': 20000,                                    # 2 年期国债面值 200 万 → 1 点 = 2 万元

    # ---- SHFE 上期所 ----
    'CU': 5, 'AL': 5, 'ZN': 5, 'PB': 5,
    'NI': 1, 'SN': 1,
    'AU': 1000, 'AG': 15,
    'RB': 10, 'WR': 10, 'HC': 10, 'SS': 5,
    'RU': 10, 'BU': 10, 'FU': 10, 'SP': 10,
    'AO': 20, 'BR': 5,

    # ---- INE 上期能源（原先整组缺失）----
    'SC': 1000,      # 原油：1000 桶/手
    'LU': 10,        # 低硫燃料油：10 吨/手
    'NR': 10,        # 20 号胶：10 吨/手
    'BC': 5,         # 国际铜：5 吨/手
    'EC': 50,        # 集运指数(欧线)：合约乘数 50 元/点

    # ---- DCE 大商所 ----
    'M': 10, 'Y': 10, 'P': 10, 'C': 10, 'CS': 10, 'A': 10, 'B': 10,
    'L': 5, 'V': 5, 'PP': 5, 'EG': 10, 'EB': 5, 'PG': 20, 'RR': 10,
    'J': 100, 'JM': 60, 'I': 100,
    'LH': 16,        # 生猪：16 吨/手
    'JD': 10,        # 鸡蛋：5 吨/手、报价 元/500千克 → 1 点 = 10 元（原表误填 5）
    'LG': 90,        # 原木：90 立方米/手（原表误填 9）
    'FB': 500, 'BB': 500,   # 纤维板/胶合板：500 张/手

    # ---- CZCE 郑商所 ----
    'SR': 10, 'CF': 5, 'TA': 5, 'MA': 10, 'FG': 20, 'RM': 10, 'OI': 10,
    'AP': 10, 'CJ': 5, 'PK': 5, 'SA': 20, 'SF': 5, 'SM': 5,
    'UR': 20, 'PX': 5, 'SH': 30, 'PF': 5, 'CY': 5, 'RS': 10,

    # ---- GFEX 广期所（原先整组缺失）----
    'SI': 5,         # 工业硅：5 吨/手
    'LC': 1,         # 碳酸锂：1 吨/手
    'PS': 3,         # 多晶硅：3 吨/手
    'PT': 1000, 'PD': 1000,   # 铂/钯：1000 克/手
}

# 反推值与内置乘数的容差：2% 内视为一致（消除浮点噪声，取整数表值）
_MULTIPLIER_MATCH_TOL = 0.02

# 完全无法确定乘数时的兜底值。用它算出的人民币盈亏一定是错的，
# 但至少不会抛异常；调用方必须把该品种记入 multiplier_warnings 告知用户。
UNKNOWN_MULTIPLIER = 1.0


def is_known_multiplier(contract: str) -> bool:
    """该合约的品种乘数是否已收录在内置表中。"""
    return extract_symbol(contract) in OPTION_MULTIPLIER


def get_option_multiplier(contract: str) -> float:
    """按合约名取期权乘数（元/点）。

    ⚠️ 未收录品种返回 UNKNOWN_MULTIPLIER(1.0) —— 这是个**错误的数值**，
    只用于避免崩溃。需要区分「是否真的知道」时请用 is_known_multiplier()
    或 resolve_multiplier()。
    """
    sym = extract_symbol(contract)
    return float(OPTION_MULTIPLIER.get(sym, UNKNOWN_MULTIPLIER))


def _infer_multiplier(premium_total: float, price: float, volume: float):
    """从结算单反推合约乘数：乘数 = |权利金总额(元)| / (单价(点) × 手数)。

    结果不可靠（分母为 0、量级异常）时返回 None，交由上层兜底。
    """
    try:
        denom = float(price) * float(volume)
        total = float(premium_total)
    except (TypeError, ValueError):
        return None
    if abs(denom) < 1e-9 or abs(total) < 1e-9:
        return None
    m = abs(total) / abs(denom)
    # 乘数应为正且量级合理（国内期权乘数区间 1 ~ 10000）
    if m <= 0 or m > 1e6:
        return None
    return m


def resolve_multiplier(contract: str, premium_total=None, price=None, volume=None):
    """确定某笔期权成交的合约乘数。

    优先级：结算单反推 > 内置表 > 未知兜底。

    返回 (multiplier, source, warning)：
      source  'inferred' 反推得到（与内置表一致时取整数表值）
              'table'    反推失败，用内置表
              'unknown'  都没有，只能用 1.0 —— warning 非空
      warning 给用户看的提示文案，无需提示时为 None
    """
    sym = extract_symbol(contract)
    table = OPTION_MULTIPLIER.get(sym)
    inferred = _infer_multiplier(premium_total, price, volume)

    if inferred is not None:
        if table is not None:
            if abs(inferred - table) <= table * _MULTIPLIER_MATCH_TOL:
                # 反推与内置一致：用整数表值，避免 999.9998 这类浮点噪声
                return float(table), 'inferred', None
            return round(inferred, 4), 'inferred', (
                f'{sym} 反推乘数 {inferred:.4g} 与内置乘数 {table} 不一致，'
                f'已优先采用结算单反推值，请核对')
        return round(inferred, 4), 'inferred', None

    if table is not None:
        return float(table), 'table', None

    return UNKNOWN_MULTIPLIER, 'unknown', (
        f'品种 {sym} 的期权合约乘数未知（结算单未提供权利金总额），'
        f'暂按 1.0 计算，该品种盈亏可能严重失真')


# ============================================================
# 数据解析：从 xls 中提取成交记录
# ============================================================
def _date_from_path(filepath: str):
    """从结算单文件名里取交易日期（YYYY-MM-DD）；取不到返回 None。

    文件名形如 {account}_{YYYY-MM-DD}.xls 或 {account}_{YYYYMMDD}.xls。
    """
    base = os.path.basename(str(filepath))
    m = re.search(r'(20\d{2})[-_]?(\d{2})[-_]?(\d{2})', base)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    ds = f'{y}-{mo}-{d}'
    try:
        datetime.strptime(ds, '%Y-%m-%d')
    except ValueError:
        return None
    return ds


def _report_kind(filepath: str) -> str:
    """判断一条结算单是「逐笔」还是「逐日」。"""
    parts = os.path.normpath(str(filepath)).split(os.sep)
    return '逐日' if '逐日' in parts else '逐笔'


def _looks_like_excel(filepath: str) -> bool:
    """检查文件魔数：真正的 xls(BIFF) / xlsx(zip) 才继续解析。
    监控中心会话过期时会返回 HTML 登录页并同样存成 .xls，
    直接喂给 xlrd 会抛异常，这里提前识别并跳过。"""
    try:
        with open(filepath, 'rb') as f:
            head = f.read(512)
    except OSError:
        return False
    if not head:
        return False
    # BIFF: \xd0\xcf\x11\xe0 (ole2) 或 \x09\x00\x04\x00 / \x09\x04 (老式 BIFF)
    if head[:4] in (b'\xd0\xcf\x11\xe0', b'\x09\x00\x04\x00') or head[:2] == b'\x09\x04':
        return True
    # xlsx: zip
    if head[:2] == b'PK':
        return True
    # 其余（HTML 文本 '<html'/'<!DOC' 等）视为无效结算单
    return False


# ============================================================
# 成交汇总表：按表头关键字定位列
# ============================================================
# 为什么不能写死列号：不同券商/不同版本的结算单列顺序并不一致。
# 实测用户导出的「期权成交汇总」是
#   0合约 1标的 2类型 3买卖 4行权价 5权利金价格 6手数 7平仓盈亏 8手续费
# 而早期版本假设的是
#   0合约 1标的 2类型 3行权价 4买卖 5权利金价格 6手数 7权利金 9手续费
# 写死列号会把「3,900.00」当成买卖方向，整行被判为无法识别 → 盈亏恒为 0。
#
# 所以改为：读表头行（标题行的下一行）→ 按关键字建映射；
# 识别失败才回退旧版固定列，并在结果里留下 layout_notes 提示。

_OPTION_HEADER_FIELDS = [
    # (字段, 关键字)；顺序即优先级，靠前的先占列
    ('contract',      ('合约',)),
    ('underlying',    ('标的',)),
    ('opt_type',      ('类型',)),
    ('direction',     ('买卖', '方向')),
    ('strike',        ('行权价', '行权价格', '执行价')),
    ('premium_price', ('权利金价格', '权利金单价', '单价', '价格')),
    ('volume',        ('手数', '数量', '成交量')),
    ('premium_total', ('权利金',)),
    ('commission',    ('手续费',)),
    ('realized_pnl',  ('平仓盈亏', '盈亏')),
]

_FUTURES_HEADER_FIELDS = [
    ('contract',     ('合约',)),
    ('direction',    ('买卖', '方向')),
    ('hedge_type',   ('投保', '套保', '投机')),
    ('price',        ('价格', '成交价')),
    ('volume',       ('手数', '数量', '成交量')),
    ('amount',       ('成交额', '金额')),
    ('open_close',   ('开平',)),
    ('commission',   ('手续费',)),
    ('realized_pnl', ('平仓盈亏', '盈亏')),
]

# 期权行权明细（实值期权到期行权时才会出现这一块）
# 表头实测：交易所 | 品种合约 | 标的合约 | 期权类型 | 执行价 | 行权日期 |
#           买/卖 | 成交量 | 行权盈亏 | 手续费 | 行权锁定保证金 | 冻结资金
_EXERCISE_HEADER_FIELDS = [
    ('contract',     ('品种合约', '合约')),
    ('underlying',   ('标的合约', '标的')),
    ('opt_type',     ('期权类型', '类型')),
    ('strike',       ('执行价', '行权价')),
    ('date',         ('行权日期',)),
    ('direction',    ('买卖', '方向')),
    ('volume',       ('成交量', '手数', '数量')),
    ('exercise_pnl', ('行权盈亏', '盈亏')),
    ('commission',   ('手续费',)),
]

# 旧版固定列（表头无法识别时兜底）
_LEGACY_OPTION_COLUMNS = {
    'contract': 0, 'underlying': 1, 'opt_type': 2, 'strike': 3,
    'direction': 4, 'premium_price': 5, 'volume': 6, 'premium_total': 7,
    'commission': 9,
}
_LEGACY_FUTURES_COLUMNS = {
    'contract': 0, 'direction': 1, 'hedge_type': 2, 'price': 3,
    'volume': 4, 'amount': 5, 'open_close': 7, 'commission': 8,
    'realized_pnl': 9,
}


def _cell_raw(ws, row_idx: int, col):
    """安全取原始单元格：行/列越界或 col 为 None 时返回 None。"""
    if col is None or col < 0:
        return None
    if row_idx < 0 or row_idx >= ws.nrows:
        return None
    try:
        if col >= ws.ncols:
            return None
        return ws.cell_value(row_idx, col)
    except (IndexError, ValueError):
        return None


def _cell_text(ws, row_idx: int, col) -> str:
    v = _cell_raw(ws, row_idx, col)
    return '' if v is None else str(v).strip()


def _cell_float(ws, row_idx: int, col) -> float:
    return _to_float(_cell_raw(ws, row_idx, col))


def _find_section_row(ws, include, exclude=()):
    """在首列中查找区块标题行（包含任一关键字即可）。

    不能精确匹配 '期权成交汇总'：不同券商/版本可能写成「期权成交明细」
    「期权成交汇总表」等。精确匹配一旦失手，整段数据会被静默跳过 —— 症状和
    列顺序错位一模一样（盈亏恒为 0），而且更难发现。
    """
    for r in range(ws.nrows):
        text = str(ws.cell_value(r, 0)).strip().replace(' ', '')
        if not text:
            continue
        if any(k in text for k in include) and not any(e in text for e in exclude):
            return r
    return None


def _is_section_total(text: str) -> bool:
    """判断某行是否为合计行（数据段结束标志）。"""
    t = str(text or '').strip()
    return bool(t) and (t.startswith('合计') or t.startswith('小计')
                        or t.startswith('总计') or t.startswith('合  计'))


def _header_map_at(ws, header_row: int, fields) -> dict:
    """在某一行上按关键字建立 字段→列号 映射（不判断够不够用）。

    表头文本要先「规整」再匹配：真实结算单里方向列写的是 **`买/卖`**、
    开平列写的是 **`开/平`**（带斜杠），直接拿 `买卖` / `开平` 去匹配会失败，
    结果这些字段映射到空列 —— 所有成交行都被判为"方向无法识别"、盈亏恒为 0。
    所以统一去掉 / 、／ 和空格。
    """
    if header_row is None or header_row < 0 or header_row >= ws.nrows:
        return {}

    def _norm(s: str) -> str:
        return (s.replace('/', '').replace('／', '').replace('、', '')
                 .replace(' ', '').replace('\u3000', ''))

    header = [_norm(_cell_text(ws, header_row, c)) for c in range(ws.ncols)]
    mapping = {}
    used = set()
    for field, keywords in fields:
        for idx, text in enumerate(header):
            if idx in used or not text:
                continue
            if any(k in text for k in keywords):
                mapping[field] = idx
                used.add(idx)
                break
    return mapping


def _best_header(ws, title_row: int, fields, max_scan: int = 4):
    """在标题行之后的若干行里找"最像表头"的那一行。

    不能死按「标题行 + 1」：有些报表标题和表头之间还有空行或合并行，
    读错一行就会让所有字段映射到空列（实测踩过）。
    返回 (header_row, mapping)；都不像表头时返回 (None, {})。
    """
    best_row, best_map = None, {}
    for offset in range(1, max_scan + 1):
        r = title_row + offset
        if r >= ws.nrows:
            break
        m = _header_map_at(ws, r, fields)
        if len(m) > len(best_map):
            best_row, best_map = r, m
        # 已经足够好（含合约 + 至少 5 个字段）就不再往下找
        if 'contract' in m and len(m) >= 5:
            break
    if 'contract' in best_map and len(best_map) >= 4:
        return best_row, best_map
    return None, {}


# 买卖方向的合法取值。用于「表头映射不准时按内容兜底识别」。
def _looks_like_direction(text: str) -> bool:
    t = str(text or '').strip()
    if not t or len(t) > 8:
        return False
    if '买卖' in t:      # 表头文字，排除
        return False
    return t.startswith('买') or t.startswith('卖')


def _resolve_direction(cells, mapped_idx):
    """确定一行里的「买卖方向」。

    优先用表头映射到的列；若该列不是买卖值（空、或映射被表头合并单元格带偏），
    就在整行里按内容找形似「买/卖」的单元格。

    为什么必须这样兜底：实测某券商结算单的表头映射会把「买卖」指到空列，
    于是**所有**期权行都被判为"方向无法识别"、盈亏恒为 0 —— 而合约和手数
    却解析正常，从结果上看非常迷惑。

    返回 (direction, column_index, used_fallback)。
    """
    if mapped_idx is not None and 0 <= mapped_idx < len(cells):
        v = str(cells[mapped_idx] or '').strip()
        if _looks_like_direction(v):
            return v, mapped_idx, False
    for i, c in enumerate(cells):
        if i == mapped_idx:
            continue
        if _looks_like_direction(c):
            return str(c).strip(), i, True
    return '', mapped_idx, False


def _row_cells(ws, row_idx: int) -> list:
    return [_cell_text(ws, row_idx, c) for c in range(ws.ncols)]


def parse_trade_xls(filepath: str) -> dict:
    """解析一天的结算单 XLS，返回当天的成交与资金摘要。"""
    if not _looks_like_excel(filepath):
        raise ValueError('文件不是有效的结算单（可能是登录过期的网页，请删除后重新下载）')
    wb = xlrd.open_workbook(filepath)
    if wb.nsheets == 0:
        raise ValueError('结算单中没有工作表')
    ws = wb.sheet_by_index(0)
    if ws.ncols == 0 or ws.nrows == 0:
        raise ValueError('结算单为空表（当日无数据）')

    result = {
        'date': '', 'account': '',
        'futures_trades': [], 'options_trades': [], 'summary': {},
        'multiplier_warnings': [],
        # 实值期权到期行权的记录（虚值到期不会有这一块）
        'option_exercise': [],
    }

    # ---- 逐日汇总解析（资金状况区域）----
    # 实际 xls 布局为两列并排：
    #   左列: c0=标签, c2=值   |   右列: c5=标签, c7=值
    #   特殊: "交易日期"在c5, 值在c7; "可用资金"等只有右半边
    # 兼容两种报表格式:
    #   逐日盯市: "当日盈亏"
    #   逐笔对冲: "平仓盈亏", "浮动盈亏"
    _sum_labels_left = ['上日结存', '当日存取合计', '当日盈亏', '平仓盈亏',
                        '浮动盈亏', '当日总权利金',
                        '当日手续费', '当日结存']
    _sum_labels_right = ['客户权益', '实有货币资金', '非货币充抵金额',
                         '货币充抵金额', '冻结资金', '保证金占用',
                         '可用资金', '风险度', '追加保证金']
    for r in range(min(20, ws.nrows)):
        row_vals = [str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
        # 账号
        if '客户期货期权内部资金账户' in row_vals[0]:
            # 账号单元格可能是文本，也可能是数值型（xlrd 给出 float），
            # 直接 str().zfill(12) 会得到 '72503313361.0' 这种脏值
            result['account'] = _normalize_account(
                row_vals[2] if len(row_vals) > 2 else '')
        # 交易日期（在右半边 c5）
        # 原写法 `if '交易日期' in row_vals[5] if len(row_vals) > 5 else False`
        # 依赖条件表达式优先级，可读性极差；改为显式判断
        if len(row_vals) > 5 and '交易日期' in row_vals[5]:
            raw = str(row_vals[7] if len(row_vals) > 7 else '')
            try:
                result['date'] = datetime.strptime(raw[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
            except Exception:
                pass
        # 左半边: 标签在 c0, 值在 c2
        key0 = str(ws.cell_value(r, 0)).strip()
        if key0 in _sum_labels_left and ws.ncols > 2:
            val = ws.cell_value(r, 2)
            if str(val).strip() != '':
                result['summary'][key0] = val
        # 右半边: 标签在 c5, 值在 c7
        key5 = str(ws.cell_value(r, 5)).strip() if ws.ncols > 5 else ''
        if key5 in _sum_labels_right and ws.ncols > 7:
            val = ws.cell_value(r, 7)
            if str(val).strip() != '':
                result['summary'][key5] = val

    # 如果没从表格中提取到日期，尝试从文件名提取
    if not result['date']:
        import re as _re
        # 文件名格式: {account}_{YYYY-MM-DD}.xls 或 {account}_{YYYYMMDD}.xls
        basename = os.path.basename(filepath)
        # 先找标准日期格式 YYYY-MM-DD 或 YYYYMMDD（在 _ 之后的部分）
        m = _re.search(r'_(\d{4}[-]?\d{2}[-]?\d{2})\.', basename)
        if m:
            d = m.group(1)
            if len(d) == 8:
                result['date'] = f'{d[:4]}-{d[4:6]}-{d[6:]}'
            else:
                result['date'] = d.replace('-', '')[:8]
                result['date'] = f'{result["date"][:4]}-{result["date"][4:6]}-{result["date"][6:]}'

    # ---- 期货成交汇总 ----
    # 不再假设固定列序：按表头关键字建立映射（不同券商/版本的列顺序并不一致）。
    # 表头也不死按「标题行+1」，而是在其后几行里挑最像表头的一行。
    futures_start = None
    _futures_title = _find_section_row(
        ws, ('期货成交汇总', '期货成交明细', '期货成交'),
        exclude=('期权',))
    if _futures_title is not None:
        f_header_row, fmap = _best_header(ws, _futures_title, _FUTURES_HEADER_FIELDS)
        if f_header_row is not None:
            futures_start = f_header_row + 1
        else:
            futures_start = _futures_title + 2
            fmap = dict(_LEGACY_FUTURES_COLUMNS)
    if futures_start is not None:
        fmap = fmap or {}
        _dir_fallback = 0
        for r in range(futures_start, ws.nrows):
            contract = _cell_text(ws, r, fmap.get('contract'))
            if not contract or _is_section_total(contract):
                break
            cells = _row_cells(ws, r)
            direction, _dcol, used_fb = _resolve_direction(cells, fmap.get('direction'))
            if used_fb:
                _dir_fallback += 1
            result['futures_trades'].append({
                'contract': contract,
                'symbol': extract_symbol(contract),
                'direction': direction,
                'hedge_type': _cell_text(ws, r, fmap.get('hedge_type')),
                'price': _cell_float(ws, r, fmap.get('price')),
                'volume': _cell_float(ws, r, fmap.get('volume')),
                'amount': _cell_float(ws, r, fmap.get('amount')),
                'open_close': _cell_text(ws, r, fmap.get('open_close')),
                'commission': _cell_float(ws, r, fmap.get('commission')),
                'realized_pnl': _parse_pnl(_cell_raw(ws, r, fmap.get('realized_pnl'))),
                'date': result['date'],
                'account': result['account'],
            })
        if _dir_fallback:
            result.setdefault('layout_notes', []).append(
                f'期货成交汇总的「买卖」列未按表头定位到，'
                f'已改为按内容识别（{_dir_fallback} 行），结果应正确')

    # ---- 期权成交汇总 ----
    options_start = None
    _options_title = _find_section_row(
        ws, ('期权成交汇总', '期权成交明细', '期权成交'))
    fmap = None
    if _options_title is not None:
        o_header_row, omap = _best_header(ws, _options_title, _OPTION_HEADER_FIELDS)
        if o_header_row is not None:
            options_start = o_header_row + 1
        else:
            options_start = _options_title + 2
            omap = dict(_LEGACY_OPTION_COLUMNS)
            result.setdefault('layout_notes', []).append(
                '期权成交汇总的表头无法识别，已回退到旧版固定列位置，'
                '若盈亏不对请用 --dump-sheet 导出原文核对')
    if options_start:
        omap = omap or {}
        _opt_dir_fallback = 0

        for r in range(options_start, ws.nrows):
            opt_contract = _cell_text(ws, r, omap.get('contract'))
            if not opt_contract or _is_section_total(opt_contract):
                break
            cells = _row_cells(ws, r)
            direction, _dcol, used_fb = _resolve_direction(cells, omap.get('direction'))
            if used_fb:
                _opt_dir_fallback += 1
            price_pts = _cell_float(ws, r, omap.get('premium_price'))   # 权利金单价（点）
            vol = _cell_float(ws, r, omap.get('volume'))                # 手数
            premium_abs = abs(_cell_float(ws, r, omap.get('premium_total')))
            cash_sign = (-1.0 if direction.startswith('买')
                         else (1.0 if direction.startswith('卖') else 0.0))

            # 合约乘数（元/点）：优先用 总额 ÷ (单价×手数) 反推（自动适配任意品种），
            # 反推失败（字段缺失/为0）时按内置乘数表兜底；两者都没有则标记告警。
            # 注意：未能确定乘数时绝不能静默按 1.0 计算 —— 原油（1000）这类品种
            # 会把盈亏直接缩小一千倍，且用户完全看不出来。
            multiplier, mult_source, mult_warn = resolve_multiplier(
                opt_contract, premium_abs, price_pts, vol)
            if mult_warn and mult_warn not in result['multiplier_warnings']:
                result['multiplier_warnings'].append(mult_warn)

            # 有些结算单的期权成交汇总没有「权利金总额」列（只有单价和手数）。
            # 此时自己算：权利金 = 单价(点) × 手数 × 乘数(元/点)。
            # 不算的话「权利金收支」会恒为 0，看起来像没成交。
            if premium_abs <= 0 and price_pts > 0 and vol > 0:
                premium_abs = price_pts * vol * multiplier

            result['options_trades'].append({
                'opt_contract': opt_contract,
                'symbol': extract_symbol(opt_contract),
                'underlying': _cell_text(ws, r, omap.get('underlying')),
                'opt_type': _cell_text(ws, r, omap.get('opt_type')),
                'strike': _cell_float(ws, r, omap.get('strike')),
                'direction': direction,
                'premium_price': price_pts,
                'volume': vol,
                'multiplier': multiplier,
                'multiplier_source': mult_source,
                'premium_total': cash_sign * premium_abs,
                'commission': _cell_float(ws, r, omap.get('commission')),
                # pnl = 权利金净收支（现金流口径，买方为负、卖方为正）
                'pnl': cash_sign * premium_abs,
                'date': result['date'],
                'account': result['account'],
            })

        if _opt_dir_fallback:
            result.setdefault('layout_notes', []).append(
                f'期权成交汇总的「买卖」列未按表头定位到，'
                f'已改为按内容识别（{_opt_dir_fallback} 行），结果应正确；'
                f'如仍异常请用 --dump-sheet 导出原文核对')

    # ---- 期权行权明细（实值行权）----
    # 实值期权到期会被行权，这一笔**不会**出现在「期权成交汇总」里，
    # 而是单独记在「期权行权明细」区块。只读成交汇总的话，这笔仓位的
    # 已实现盈亏会算成"完全没平仓"，最后被按作废处理 —— 全损，方向全错。
    ex_title = _find_section_row(ws, ('期权行权明细', '行权明细', '期权行权'))
    if ex_title is not None:
        ex_header_row, exmap = _best_header(ws, ex_title, _EXERCISE_HEADER_FIELDS)
        ex_start = (ex_header_row + 1) if ex_header_row is not None else ex_title + 2
        exmap = exmap or {}
        for r in range(ex_start, ws.nrows):
            contract = _cell_text(ws, r, exmap.get('contract', 1))
            if not contract or _is_section_total(contract):
                break
            cells = _row_cells(ws, r)
            direction, _c, _fb = _resolve_direction(cells, exmap.get('direction'))
            vol = _cell_float(ws, r, exmap.get('volume'))
            ex_pnl = _cell_float(ws, r, exmap.get('exercise_pnl'))
            ex_date = (_cell_text(ws, r, exmap.get('date')) or result['date'])[:10]
            if vol <= 0:
                continue
            result['option_exercise'].append({
                'opt_contract': contract,
                'symbol': extract_symbol(contract),
                'underlying': _cell_text(ws, r, exmap.get('underlying')),
                'opt_type': _cell_text(ws, r, exmap.get('opt_type')),
                'strike': _cell_float(ws, r, exmap.get('strike')),
                'direction': direction,
                'volume': vol,
                'exercise_pnl': ex_pnl,
                'commission': _cell_float(ws, r, exmap.get('commission')),
                'date': ex_date or result['date'],
                'account': result['account'],
            })

    return result


# ============================================================
# 期权到期日 / 到期作废结算
# ============================================================
# 为什么需要这个：
#   期权到期作废（虚值，未被行权）时，结算单里**不会出现任何平仓成交行**，
#   持仓只是从持仓汇总里消失了。FIFO 匹配只吃成交行，于是买方付出的权利金
#   会一直挂在"未平仓"上，已实现盈亏永远是 0 —— 一笔全额亏损在复盘里凭空消失。
#   所以必须按合约的到期日，把期末仍未平仓的头寸强制按「0」结算。
#
# 到期日规则（各交易所合约规则）：
#   中金所 股指期权 IO/MO/HO   → 合约月份第三个星期五（遇节假日顺延）
#   中金所 国债期权 T/TF/TS/TL → 合约月份第二个星期五（遇节假日顺延）
#   商品期权（SHFE/INE/DCE/CZCE/GFEX）→ 标的期货交割月**前一个月**的第 5 个交易日
#
# 作废时的盈亏（权利金全额）：
#   买方（付出权利金）→ 亏损 = -(开仓价 × 手数 × 乘数)
#   卖方（收取权利金）→ 盈利 = +(开仓价 × 手数 × 乘数)
#
# 局限：本实现只按「到期作废」处理，不区分「到期行权」。若某笔实为行权（实值），
# 结算单通常会另有行权成交行，FIFO 会把仓位平掉、不会走到这段逻辑；
# 但为稳妥起见，所有被强制结算的合约都会记入 expiry_settlements 并在界面提示。

_OPT_CONTRACT_RE = re.compile(r'^([A-Z]{1,3})(\d{4})([CP])(\d+(?:\.\d+)?)$')

# 中金所：合约月即到期月，到期日为「第 n 个星期五」
_CFFEX_FRIDAY_RULES = {
    'IO': 3, 'MO': 3, 'HO': 3,          # 股指期权：第三个星期五
    'T': 2, 'TF': 2, 'TS': 2, 'TL': 2,  # 国债期权：第二个星期五
}

# 商品期权到期日规则（各交易所并不统一，务必按下表，不能一刀切）：
#   SHFE 上期所  标的期货交割月前一月的**倒数第 5 个交易日**
#   INE  上期能源 前一月的**倒数第 13 个交易日**
#   DCE  大商所  前一月的**第 12 个交易日**（2501 合约起由「第 5 个」延后）
#   CZCE 郑商所  前一月第 15 个日历日（含）之前的**倒数第 3 个交易日**
#   GFEX 广期所  前一月的**第 5 个交易日**
# 参考：上期所铜期权合约、上期能源原油期权到期日公告、大商所期权规则修订公告、
#       郑商所白糖期权合约、广期所工业硅期权合约。
#
# 重要：规则算得「偏晚」只是结算不到（保守），算得「偏早」会把仍在交易的合约
# 提前按 0 平掉，随后真实平仓又会开出一笔反向仓 —— 造成重复计账。因此未收录的
# 品种一律返回 None（不结算）并提示人工核对，绝不用猜的规则。
_OPTION_EXPIRY_GROUPS = {
    'nth_last_trading:5': {          # 上期所：倒数第 5 个交易日
        'CU', 'AL', 'ZN', 'PB', 'NI', 'SN', 'AU', 'AG', 'RB', 'RU',
        'BU', 'SP', 'AO', 'BR', 'SS', 'WR', 'FU',
    },
    'nth_last_trading:13': {         # 上期能源：倒数第 13 个交易日
        'SC', 'LU', 'NR', 'BC',
    },
    'nth_trading:12': {              # 大商所：第 12 个交易日
        'M', 'Y', 'P', 'C', 'CS', 'A', 'B', 'L', 'V', 'PP', 'EG',
        'EB', 'PG', 'I', 'J', 'JM', 'LH', 'JD', 'LG', 'RR',
    },
    'czce_15th_third_last': {        # 郑商所：第 15 日之前的倒数第 3 个交易日
        'SR', 'CF', 'TA', 'MA', 'FG', 'RM', 'OI', 'AP', 'CJ', 'PK',
        'SA', 'SF', 'SM', 'UR', 'PX', 'SH', 'PF', 'CY', 'RS',
    },
    'nth_trading:5': {               # 广期所：第 5 个交易日
        'SI', 'LC', 'PS', 'PT', 'PD',
    },
}

# 反查表：symbol -> 规则名
_SYMBOL_EXPIRY_RULE = {
    sym: rule
    for rule, symbols in _OPTION_EXPIRY_GROUPS.items()
    for sym in symbols
}


def parse_option_contract(contract):
    """解析期权合约代码。

    兼容两种写法：
      'IO2611-C-4000'  （中金所，带连字符）
      'cu2611C78000'   （商品，无连字符）
    返回 dict(symbol, year, month, cp, strike) 或 None。
    """
    s = re.sub(r'[\s\-_/]', '', str(contract or '').strip().upper())
    m = _OPT_CONTRACT_RE.match(s)
    if not m:
        return None
    sym, yymm, cp, strike = m.groups()
    yy, mm = int(yymm[:2]), int(yymm[2:])
    if not 1 <= mm <= 12:
        return None
    return {
        'symbol': sym, 'year': 2000 + yy, 'month': mm,
        'cp': cp, 'strike': float(strike), 'yymm': yymm,
    }


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    """该月第 n 个星期 weekday（Monday=0），返回 date。"""
    first = datetime(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return (first + timedelta(days=offset + 7 * (n - 1))).date()


def _roll_to_trading_day(d):
    """若 d 不是交易日则顺延到下一个交易日（最多找 10 天）。"""
    for _ in range(10):
        if is_cn_trading_day(d.isoformat()):
            return d
        d = d + timedelta(days=1)
    return d


def _trading_days_in_month(year: int, month: int) -> list:
    """某月全部交易日（升序）。"""
    out = []
    d = datetime(year, month, 1)
    while d.month == month:
        if is_cn_trading_day(d.date().isoformat()):
            out.append(d.date())
        d = d + timedelta(days=1)
    return out


def _prev_month(year: int, month: int):
    return (year, month - 1) if month > 1 else (year - 1, 12)


def option_expiry_date(contract):
    """推算期权最后交易日/到期日；无法判断时返回 None。

    ⚠️ 各交易所规则不同，见 _OPTION_EXPIRY_GROUPS 的注释。
    若对某品种的规则没有把握，宁可不结算（返回 None），也不要猜 ——
    算早了会把仍在交易的合约提前按 0 平掉，造成重复计账。
    """
    info = parse_option_contract(contract)
    if not info:
        return None
    sym, year, month = info['symbol'], info['year'], info['month']

    # ---- 中金所：第 n 个星期五 ----
    if sym in _CFFEX_FRIDAY_RULES:
        d = _nth_weekday(year, month, 4, _CFFEX_FRIDAY_RULES[sym])  # 4 = 星期五
        return _roll_to_trading_day(d)

    # ---- 商品期权：按交易所各自规则 ----
    rule = _SYMBOL_EXPIRY_RULE.get(sym)
    if rule is None:
        return None          # 未收录品种：不猜，交由调用方提示人工核对

    py, pm = _prev_month(year, month)
    days = _trading_days_in_month(py, pm)
    if not days:
        return None

    if rule == 'czce_15th_third_last':
        cutoff = datetime(py, pm, 15).date()
        eligible = [d for d in days if d <= cutoff]
        return eligible[-3] if len(eligible) >= 3 else None

    if rule.startswith('nth_trading:'):
        n = int(rule.split(':')[1])
        return days[n - 1] if len(days) >= n else None

    if rule.startswith('nth_last_trading:'):
        n = int(rule.split(':')[1])
        return days[-n] if len(days) >= n else None

    return None


def option_expiry_known(contract) -> bool:
    """该合约的到期日规则是否已收录（未收录时不做到期结算）。"""
    info = parse_option_contract(contract)
    if not info:
        return False
    return info['symbol'] in _CFFEX_FRIDAY_RULES or info['symbol'] in _SYMBOL_EXPIRY_RULE


def is_option_contract(contract) -> bool:
    """判断是否是期权合约代码（而非期货）。"""
    return parse_option_contract(contract) is not None


def _has_report_files(directory: str) -> bool:
    """该目录下是否存在任何「逐笔/逐日」结算单文件。"""
    directory = os.path.abspath(directory)
    for kind in ('逐笔', '逐日'):
        if glob.glob(os.path.join(directory, '**', kind, '*.xls'), recursive=True):
            return True
    return False


def resolve_output_dir(configured: str) -> str:
    """返回真正可用的数据目录。

    常见情况：配置里写的是相对路径 `./output`，而实际数据放在**上一级目录**的
    output 里（比如从不同工作目录运行过下载脚本）。这时配置的目录是空的，
    界面上就会显示"没有任何数据"，很容易误判成数据丢了。

    这里只在配置目录**没有任何结算单**时，才向上找最多 3 级同名目录，
    并打印一条提示 —— 不会覆盖用户显式指定的、确实有数据的目录。

    注意：所有路径先转绝对路径。带 `..` 的相对路径会让 glob 的 `**` 匹配失效，
    导致"明明有文件却判定为空"。
    """
    configured = os.path.abspath(configured or DEFAULT_OUTPUT_DIR)
    if os.path.isdir(configured) and _has_report_files(configured):
        return configured

    base = configured
    name = os.path.basename(base.rstrip(os.sep)) or 'output'
    cur = os.path.dirname(base)
    for _ in range(3):
        cand = os.path.join(cur, name)
        if os.path.abspath(cand) != base and os.path.isdir(cand) and _has_report_files(cand):
            print(f'  提示: 配置的数据目录 {base} 里没有结算单，'
                  f'已自动改用 {cand}')
            return cand
        nxt = os.path.dirname(cur)
        if nxt == cur:
            break
        cur = nxt
    return configured


# ============================================================
# 数据加载：扫描目录下所有 xls 文件
# ============================================================
class DataLoader:
    """加载和管理本地已下载的结算单数据"""

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR):
        # 绝对路径：带 `..` 的相对路径会让 glob 的 `**` 匹配失效，
        # 出现"目录里明明有文件却判定为空"的假象。
        self.output_dir = os.path.abspath(output_dir or DEFAULT_OUTPUT_DIR)
        self.cache = {}  # date_str -> parsed_data
        self._lock = threading.RLock()

    def scan_files(self) -> list[str]:
        """扫描结算单文件：**逐笔优先，逐日兜底**，同一交易日只取一份。

        为什么两种都要扫：监控中心同时提供「逐笔对冲」和「逐日盯市」两种日报，
        用户往往只下载了其中一种。实测某账户有 298 天「逐日」但只有 23 天「逐笔」，
        若只扫逐笔，275 天的数据会被白白丢掉（而且界面上表现为"大量缺失"）。

        两种报表的成交汇总都带「平仓盈亏」列，口径一致，可以混用；
        同一交易日两者都有时优先用逐笔对冲（与逐笔核算口径最接近）。

        同时排除：
          - 「月报」：月度汇总单据，当逐笔日报解析会覆盖同日数据
          - 同名的 .bad：已隔离的坏文件
        """
        per_date = {}      # date -> (kind_priority, path)
        no_date = []
        skipped_monthly = 0
        for kind, prio in (('逐笔', 0), ('逐日', 1)):
            pattern = os.path.join(self.output_dir, '**', kind, '*.xls')
            for fp in glob.glob(pattern, recursive=True):
                parts = os.path.normpath(fp).split(os.sep)
                if '月报' in parts:
                    skipped_monthly += 1
                    continue
                d = _date_from_path(fp)
                if not d:
                    no_date.append(fp)
                    continue
                prev = per_date.get(d)
                if prev is None or prio < prev[0]:
                    per_date[d] = (prio, fp)

        if skipped_monthly:
            print(f'  已跳过 {skipped_monthly} 个月报文件（月报是月度汇总，不能当逐笔日报解析）')

        files = [p for _prio, p in per_date.values()] + no_date
        return sorted(files)

    def load_all(self, quarantine: bool = True) -> dict:
        """加载所有可用的结算单。

        鲁棒性处理：
          - 无成交日或登录过期时，监控中心可能返回 HTML 提示页并被存成 .xls，
            这类文件重命名为 .xls.bad 隔离（保留原内容供人工检查），
            下次查询即可正常重新下载，不会反复解析出错。
          - 单个文件解析失败只警告，不中断整体加载。
          - 先用新 dict 构建、最后整体替换 self.cache，避免并发请求在
            `for ... in self.cache.items()` 迭代时遇到 "dictionary changed size
            during iteration"。
        """
        files = self.scan_files()
        bad_files = []      # 无效（多半是登录过期存下的网页）
        parse_errors = []
        new_cache = {}
        duplicates = []
        for fp in files:
            try:
                data = parse_trade_xls(fp)
                if data['date']:
                    prev = new_cache.get(data['date'])
                    if prev is not None and prev.get('account') != data.get('account'):
                        # 多账户同日：目前缓存以日期为键，只能保留后一个。
                        # 这里显式告警，而不是静默丢数据。
                        duplicates.append((data['date'], prev.get('account'), data.get('account')))
                    data['source'] = _report_kind(fp)
                    data['file'] = fp
                    new_cache[data['date']] = data
            except Exception as e:
                if not _looks_like_excel(fp):
                    bad_files.append(fp)
                else:
                    parse_errors.append((fp, e))

        # ---- 无效文件：默认不批量改名 ----
        # 这类文件是「登录过期时把网页存成了 .xls」，数量可能很大（实测 233 个）。
        # 逐个 os.rename 会在用户磁盘上产生几百次重命名，风险高、收益低，
        # 改成：少量时自愈隔离，大量时只报告并提示用 --clean-bad 清理。
        quarantined = 0
        if bad_files and quarantine:
            if len(bad_files) <= 20:
                for fp in bad_files:
                    try:
                        with self._lock:
                            os.rename(fp, fp + '.bad')
                        quarantined += 1
                    except OSError:
                        pass
            else:
                print(f'  ⚠ 有 {len(bad_files)} 个无效结算单文件（登录过期时存下的网页，不是真 Excel），'
                      f'已跳过。数量较多，未自动改名；可运行 '
                      f'`python review.py --clean-bad` 统一清理或重新下载')
        if quarantined:
            print(f'  已隔离 {quarantined} 个无效文件，重新查询时会自动补下载')
        for fp, e in parse_errors[:3]:
            print(f'  警告: 解析失败 {os.path.basename(fp)}: {e}')
        if len(parse_errors) > 3:
            print(f'  警告: 另有 {len(parse_errors) - 3} 个文件解析失败（已忽略）')
        if duplicates:
            print(f'  ⚠ 发现 {len(duplicates)} 个「多个账户同一天」的结算单，'
                  f'当前只保留最后一个账户的数据：')
            for d, a, b in duplicates[:5]:
                print(f'      {d}: 账户 {a} 被 {b} 覆盖')
            print('      → 如需统计多账户合计，请为每个账户使用独立的 output_dir')
        with self._lock:
            self.cache = new_cache
        # 记录当前数据文件指纹，供 Web 层判断是否需要自动重载
        try:
            self._snapshot = self.snapshot()
        except Exception:
            self._snapshot = None
        print(f'已加载 {len(self.cache)} 个交易日的数据')
        return self.cache

    def snapshot(self):
        """数据文件的轻量指纹（文件数 + 最新修改时间）。

        用于判断磁盘上的结算单有没有变化（比如刚下载了新数据），
        变化时自动重载，不必重启 Web 服务。
        """
        n = 0
        newest = 0.0
        for kind in ('逐笔', '逐日'):
            for fp in glob.glob(os.path.join(self.output_dir, '**', kind, '*.xls'),
                                recursive=True):
                n += 1
                try:
                    m = os.path.getmtime(fp)
                except OSError:
                    continue
                if m > newest:
                    newest = m
        return (n, round(newest, 3))

    def clean_bad_files(self, dry_run: bool = False) -> dict:
        """把无效结算单（登录过期时存下的网页）改名成 .bad，方便重新下载。

        这类文件的特征是「扩展名 .xls 但内容不是 Excel」。数量可能很大，
        所以做成显式命令，而不是在加载时批量乱改名。
        """
        bad = []
        if os.path.isdir(self.output_dir):
            for kind in ('逐笔', '逐日'):
                for fp in glob.glob(os.path.join(self.output_dir, '**', kind, '*.xls'),
                                    recursive=True):
                    if not _looks_like_excel(fp):
                        bad.append(fp)
        renamed, failed = 0, 0
        for fp in bad:
            if dry_run:
                continue
            try:
                os.rename(fp, fp + '.bad')
                renamed += 1
            except OSError:
                failed += 1
        return {'found': len(bad), 'renamed': renamed, 'failed': failed,
                'dry_run': dry_run,
                'sample': [os.path.relpath(f, self.output_dir) for f in bad[:5]]}

    def get_date_range(self) -> tuple:
        """获取已有数据的日期范围"""
        dates = sorted(self.cache.keys())
        if not dates:
            return None, None
        return dates[0], dates[-1]

    # ---- 结算单原文诊断 ----
    def describe_data(self) -> dict:
        """描述当前数据目录的实际内容，用于排查"为什么找不到某天的结算单"。

        只报"找不到"没有意义 —— 可能是目录不对、格式不对、目录层级不对，
        或者那天确实没下载。这里把能看到的都列出来。
        """
        out_dir = os.path.abspath(self.output_dir)
        matched = self.scan_files()
        dates = sorted(self.cache.keys())

        # 分别统计「逐笔 / 逐日」有效文件数
        kinds = {'逐笔': 0, '逐日': 0}
        invalid = 0
        for fp in matched:
            kinds[_report_kind(fp)] = kinds.get(_report_kind(fp), 0) + 1

        # 更宽泛地扫一遍，用来说明"文件其实存在，只是不在预期位置/格式"
        all_xls, all_xlsx, all_other = [], 0, []
        if os.path.isdir(out_dir):
            for root, _dirs, names in os.walk(out_dir):
                for n in names:
                    low = n.lower()
                    full = os.path.join(root, n)
                    rel = os.path.relpath(full, out_dir)
                    parts = os.path.normpath(rel).split(os.sep)
                    if low.endswith('.xlsx'):
                        all_xlsx += 1
                    elif low.endswith('.xls'):
                        if not (_looks_like_excel(full)):
                            invalid += 1
                        elif '逐笔' not in parts and '逐日' not in parts:
                            all_xls.append(rel)
                    elif low.endswith('.bad'):
                        all_other.append(rel)

        return {
            'output_dir': out_dir,
            'output_dir_exists': os.path.isdir(out_dir),
            'pattern': os.path.join(out_dir, '**', '逐笔|逐日', '*.xls'),
            'file_count': len(matched),
            'zhubi_files': kinds.get('逐笔', 0),
            'zhuri_files': kinds.get('逐日', 0),
            'invalid_files': invalid,
            'loaded_days': len(self.cache),
            'first_day': dates[0] if dates else None,
            'last_day': dates[-1] if dates else None,
            'sample_files': [os.path.relpath(f, out_dir) for f in matched[:5]],
            'xls_outside_zhubi': all_xls[:5],
            'xlsx_count': all_xlsx,
            'quarantined': all_other[:5],
        }

    def find_file_for_date(self, date_str: str):
        """按日期找到对应的逐笔结算单文件路径。"""
        target = str(date_str)[:10].replace('-', '')
        for fp in self.scan_files():
            name = os.path.basename(fp).replace('-', '')
            if target in name:
                return fp
        return None

    def dump_sheet(self, date_str: str, contract: str = None,
                   max_rows: int = 400) -> dict:
        """把某天的结算单原文导出来，用于排查"某个合约为什么没被识别"。

        返回：
          file       文件路径
          sections   疑似区块标题（首列含 汇总/明细/持仓/行权/交割 的行）
          matches    包含目标合约的行（不传 contract 时为空）
          rows       全部行（最多 max_rows 行，每行为字符串列表）

        这是只读诊断工具，不修改任何数据。定位逻辑（行权/履约是否单独成块、
        列位置如何）在没有真实样本时无法可靠推断，所以这里直接把原文摊开。
        """
        fp = self.find_file_for_date(date_str)
        if not fp:
            info = self.describe_data()
            return {
                'error': (f'找不到 {date_str} 的结算单文件'
                          f'（数据目录 {info["output_dir"]}，'
                          f'已发现 {info["file_count"]} 个结算单，'
                          f'覆盖 {info["first_day"] or "-"} ~ {info["last_day"] or "-"}）'),
                'file': None,
                'data_info': info,
                'sections': [], 'matches': [], 'rows': [],
            }
        if not _looks_like_excel(fp):
            return {'error': f'{os.path.basename(fp)} 不是有效的 Excel（可能是登录过期的网页）',
                    'file': fp, 'sections': [], 'matches': [], 'rows': []}

        wb = xlrd.open_workbook(fp)
        ws = wb.sheet_by_index(0)
        sections, matches, rows = [], [], []
        needle = (contract or '').strip().upper()
        section_kw = ('汇总', '明细', '持仓', '行权', '履约', '交割', '资金状况')

        for r in range(min(ws.nrows, max_rows)):
            cells = []
            for c in range(ws.ncols):
                v = ws.cell_value(r, c)
                cells.append('' if v is None else str(v).strip())
            rows.append(cells)

            first = cells[0] if cells else ''
            if any(k in first for k in section_kw):
                sections.append({'row': r, 'title': first})

            if needle:
                joined = ' '.join(cells).upper()
                if needle in joined:
                    matches.append({'row': r, 'cells': cells})

        return {
            'file': fp,
            'date': date_str,
            'contract': contract,
            'nrows': ws.nrows,
            'ncols': ws.ncols,
            'truncated': ws.nrows > max_rows,
            'sections': sections,
            'matches': matches,
            'rows': rows if not needle else [],   # 指定合约时只回匹配行，避免刷屏
        }


    def get_missing_dates(self, start: str, end: str) -> list[str]:
        """指定范围内缺失的交易日（排除周末和中国法定节假日）。"""
        return self.analyze_missing_dates(start, end)['missing']

    def analyze_missing_dates(self, start: str, end: str) -> dict:
        """把「区间内没有本地结算单的交易日」分类。

        只报一个数字（"缺 24 天"）是没用的 —— 这 24 天可能是三种完全不同的情况：

          gaps          夹在已有数据之间的缺口 → **真正的缺失**，最该关注
          before_range  早于本地最早数据       → 多为账户开立前的日期，补也补不到
          after_range   晚于本地最晚数据       → 新交易日，值得补

        同时把区间内的**节假日**单独列出，用来证明"不是漏下载，而是本来就没有"。
        """
        result = {
            'start': start, 'end': end,
            'calendar_ok': _HAS_CHINESE_CALENDAR,
            'trading_days': 0,
            'weekend_days': 0,
            'holiday_days': 0,
            'holidays': [],
            'with_data': 0,
            'missing': [],
            'gaps': [],
            'before_range': [],
            'after_range': [],
            'unexplained': [],
            'first_data': None,
            'last_data': None,
            'download_candidates': [],
        }
        try:
            cur = datetime.strptime(str(start)[:10], '%Y-%m-%d')
            end_dt = datetime.strptime(str(end)[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return result
        today = datetime.now()
        if end_dt > today:
            end_dt = today          # 未来日期不可能有结算单
        if cur > end_dt:
            return result

        dates = sorted(self.cache.keys())
        first_data = dates[0] if dates else None
        last_data = dates[-1] if dates else None
        result['first_data'] = first_data
        result['last_data'] = last_data

        while cur <= end_dt:
            ds = cur.strftime('%Y-%m-%d')
            if cur.weekday() >= 5:
                result['weekend_days'] += 1
            elif not is_cn_trading_day(ds):
                # 工作日但休市（元旦/春节/…）→ 本来就不该有结算单
                result['holiday_days'] += 1
                result['holidays'].append(ds)
            else:
                result['trading_days'] += 1
                if ds in self.cache:
                    result['with_data'] += 1
                else:
                    result['missing'].append(ds)
            cur += timedelta(days=1)

        for ds in result['missing']:
            if first_data and ds < first_data:
                result['before_range'].append(ds)
            elif last_data and ds > last_data:
                result['after_range'].append(ds)
            elif first_data and last_data:
                result['gaps'].append(ds)      # 夹在已有数据之间 → 真缺口
            else:
                result['unexplained'].append(ds)

        # 值得补下载的：真缺口 + 晚于已有数据的新交易日。
        # 「早于最早数据」的一律不补 —— 那多半是账户开立前的日期，
        # 反复请求只会白白触发监控中心的失败重试。
        # 例外：本地完全没有任何数据时，区间内的交易日全都值得尝试。
        if not first_data:
            result['download_candidates'] = list(result['missing'])
        else:
            result['download_candidates'] = result['gaps'] + result['after_range']
        return result


# ============================================================
# 核心分析引擎
# ============================================================
class ReviewEngine:
    """复盘分析引擎"""

    def __init__(self, data_loader: DataLoader):
        self.loader = data_loader
        self.trades_df = None
        self.options_df = None
        self.daily_summary_df = None
        self._matched_cache = None
        self.option_open_positions = {}
        self.multiplier_warnings = []   # 期权乘数无法确定/存疑的提示
        self.expiry_settlements = []    # 被按「到期作废」强制结算的合约
        self.expiry_unknown = []        # 到期日规则未收录、因而未结算的合约
        self.unparsed_option_rows = []  # 方向无法识别（行权/履约等）的期权行
        self._window = (None, None)     # 当前查询窗口 (start, end)

    def build_dfs(self, start: str = None, end: str = None):
        """构建 DataFrame（可选日期过滤）"""
        self._matched_cache = None  # 数据范围变化，匹配结果需重算
        self._window = (start, end)  # 到期结算行需要按查询窗口过滤
        all_futures = []
        all_options = []
        daily_rows = []

        for date_str, data in self.loader.cache.items():
            if start and date_str < start:
                continue
            if end and date_str > end:
                continue

            for t in data['futures_trades']:
                all_futures.append(t)
            for t in data['options_trades']:
                all_options.append(t)

            summary = data.get('summary', {})
            # 兼容两种报表格式：
            #   逐日盯市: "当日盈亏" / 无浮动盈亏
            #   逐笔对冲: "平仓盈亏" / "浮动盈亏"
            # 注意：不能用 `a or b` —— 当日盈亏恰为 0.0（横盘日）时会被误判为缺失，
            # 从而错误地回退到「平仓盈亏」。必须显式判断 None。
            daily_pnl = summary.get('当日盈亏')
            if daily_pnl is None:
                daily_pnl = summary.get('平仓盈亏', 0)
            daily_rows.append({
                'date': date_str,
                'equity': _to_float(summary.get('客户权益', 0)),
                'pnl': _to_float(daily_pnl),
                'float_pnl': _to_float(summary.get('浮动盈亏', summary.get('持仓盈亏', 0))),
                'commission': _to_float(summary.get('当日手续费', 0)),
                'deposit': _to_float(summary.get('当日结存', 0)),
                'margin': _to_float(summary.get('保证金占用', 0)),
            })

        self.trades_df = pd.DataFrame(all_futures) if all_futures else pd.DataFrame()
        self.options_df = pd.DataFrame(all_options) if all_options else pd.DataFrame()
        self.daily_summary_df = pd.DataFrame(daily_rows) if daily_rows else pd.DataFrame()

        if not self.daily_summary_df.empty:
            self.daily_summary_df['date'] = pd.to_datetime(self.daily_summary_df['date'])
            self.daily_summary_df = self.daily_summary_df.sort_values('date').reset_index(drop=True)

        if not self.trades_df.empty:
            self.trades_df['date'] = pd.to_datetime(self.trades_df['date'])

        if not self.options_df.empty:
            self.options_df['date'] = pd.to_datetime(self.options_df['date'])

        return self

    # ---- 期权开平仓匹配（FIFO）----
    def _exercise_to_trade(self, ex: dict):
        """把「期权行权明细」里的一条记录转成一笔等效的平仓成交。

        FIFO 只认成交行。实值行权在结算单里单独记在「期权行权明细」，
        不在「期权成交汇总」里 —— 所以必须在这里把它翻译成一笔成交，
        否则这笔仓位永远平不掉，最后会被当成「到期作废」按 0 处理（全损）。

        换算：
          行权盈亏 = |标的价格 - 执行价| × 手数 × 乘数
          ⇒ 内在价值(点) = |行权盈亏| / (手数 × 乘数)
        这笔成交的价格就取这个内在价值（思路上等价于"以内在价值平仓"）。

        方向要反过来：
          行权明细写「买」（客户持多头被行权）→ FIFO 里需要一笔「卖」来平掉多头
          行权明细写「卖」（客户持空头被指派）→ FIFO 里需要一笔「买」来平掉空头
        """
        contract = str(ex.get('opt_contract') or '').strip()
        vol = _to_float(ex.get('volume'))
        ex_pnl = _to_float(ex.get('exercise_pnl'))
        if not contract or vol <= 0:
            return None

        mult, _src, _warn = resolve_multiplier(contract)
        if not (mult > 0):
            return None

        intrinsic_pts = abs(ex_pnl) / (vol * mult)
        side = str(ex.get('direction') or '').strip()
        # 行权方是买方（多头）→ 平掉多头需要「卖」；反之需要「买」
        close_dir = '卖' if side.startswith('买') else '买'

        return {
            'opt_contract': contract,
            'symbol': extract_symbol(contract),
            'underlying': ex.get('underlying', ''),
            'opt_type': ex.get('opt_type', ''),
            'strike': _to_float(ex.get('strike')),
            'direction': close_dir,
            'premium_price': round(intrinsic_pts, 4),
            'volume': vol,
            'multiplier': mult,
            'multiplier_source': 'exercise',
            'premium_total': 0.0,      # 行权本身不产生权利金现金流
            'commission': _to_float(ex.get('commission')),
            'pnl': 0.0,
            'date': ex.get('date', ''),
            'account': ex.get('account', ''),
            'is_exercise': True,
        }

    def _build_full_options_df(self) -> pd.DataFrame:
        """构建不限日期范围的期权成交表。

        FIFO 配对必须基于完整历史：若只在查询窗口内配对，窗口开始前就已
        持有的仓位在本窗口内平仓时找不到对应开仓，已实现盈亏会被算成 0，
        前端就只能看到权利金收支。

        这里还把「期权行权明细」的记录翻译成等效平仓成交一并纳入，
        让实值行权能走同一条 FIFO 路径（详见 _exercise_to_trade）。
        """
        rows = []
        for data in self.loader.cache.values():
            for t in data.get('options_trades', []):
                rows.append(t)
            for ex in data.get('option_exercise', []) or []:
                converted = self._exercise_to_trade(ex)
                if converted is not None:
                    rows.append(converted)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        return df.sort_values(['date', 'opt_contract'], kind='stable').reset_index(drop=True)

    def _match_options_pnl(self, base_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        使用 FIFO 算法对期权成交记录进行开平仓匹配，计算每笔的已实现盈亏。

        在「同一合约 + 同一方向」内做先进先出配对（结算单已按净方向给出买卖）：
          - 买方先开仓（付权利金），后续卖方卖出 → 盈亏 = (卖出价 - 买入价) × 手数 × 乘数
          - 卖方先开仓（收权利金），后续买方买回 → 盈亏 = (卖出价 - 买回价) × 手数 × 乘数
        期权单价以「点」报价，必须乘以合约乘数换算成元
        （如 MO/IO 每点100元、CU 每点5元、AG 每点15元）。
        已实现盈亏记账在**平仓那一笔**上，与逐笔对冲报表口径一致。

        返回带 realized_pnl / open_volume 列的副本。
        """
        full = self._build_full_options_df()
        if full.empty:
            self.option_open_positions = {}
            self.multiplier_warnings = []
            self.expiry_settlements = []
            self.expiry_unknown = []
            self.unparsed_option_rows = []
            return pd.DataFrame()

        df = full.copy()
        df['realized_pnl'] = 0.0
        # 每笔成交中用于平掉旧仓的手数 / 新开仓的手数
        df['close_volume'] = 0.0
        df['open_volume'] = 0.0
        # 旧数据缓存可能缺 multiplier 列：按内置表补齐（并用 resolve_multiplier
        # 保证拿到一个可用数值，同时把「未知品种」记进告警）
        if 'multiplier' not in df.columns:
            resolved = df['opt_contract'].map(
                lambda c: resolve_multiplier(c)[0])
            df['multiplier'] = resolved
        self.multiplier_warnings = []
        _warn_seen = set()

        def _mult_for(idx):
            """取该笔成交的乘数；缺失时按内置表兜底，仍未知则告警。"""
            mult = _to_float(df.at[idx, 'multiplier'])
            contract = str(df.at[idx, 'opt_contract'])
            if mult > 0:
                return mult
            mult, source, warn = resolve_multiplier(contract)
            if warn and warn not in _warn_seen:
                _warn_seen.add(warn)
                self.multiplier_warnings.append(warn)
            return mult

        open_positions = {}
        self.expiry_settlements = []
        self.expiry_unknown = []
        self.unparsed_option_rows = []
        _unparsed_seen = set()
        expiry_rows = []

        # 数据实际覆盖到的最后一个交易日：用于判断合约是否已经走到到期日。
        # 必须取「全部已加载交易日」，而不是期权成交行的最大日期 ——
        # 某笔仓位可能在很久以前开仓、之后一直没有期权成交，但数据其实已经
        # 覆盖到它的到期日，这时就该结算。
        try:
            _all_days = [str(d)[:10] for d in self.loader.cache.keys() if d]
            last_data_date = (datetime.strptime(max(_all_days), '%Y-%m-%d').date()
                              if _all_days else None)
        except (ValueError, TypeError):
            last_data_date = None

        for _, g in df.groupby('opt_contract', sort=False):
            long_q = []   # 买方持仓队列（成本价，点）
            short_q = []  # 卖方持仓队列（卖出价，点）

            for idx in g.index:
                direction = str(df.at[idx, 'direction'] or '').strip()
                volume = _to_float(df.at[idx, 'volume'])
                price = _to_float(df.at[idx, 'premium_price'])
                mult = _mult_for(idx)
                if volume <= 0:
                    continue

                is_buy = direction.startswith('买')
                is_sell = direction.startswith('卖')
                if not (is_buy or is_sell):
                    # 结算单里可能出现「行权」「履约」「到期」等方向。
                    # 原实现直接按开仓处理并 continue —— 于是这些行对盈亏毫无贡献，
                    # 用户只看到"这个合约盈亏没算"，却不知道为什么。
                    # 这里显式记下来并上抛，至少能让用户看到原因。
                    df.at[idx, 'open_volume'] = volume
                    key = (str(df.at[idx, 'opt_contract']), direction)
                    if key not in _unparsed_seen:
                        _unparsed_seen.add(key)
                        self.unparsed_option_rows.append({
                            'contract': key[0],
                            'direction': direction or '(空)',
                            'volume': _to_float(volume),
                            'date': str(df.at[idx, 'date'])[:10],
                        })
                    continue

                if is_buy:
                    # 先冲抵已有卖方持仓（买回平仓），盈亏 = (卖出价 - 买回价) × 手数 × 乘数
                    queue, remain, pnl = short_q, volume, 0.0
                    while remain > 0 and queue:
                        pos = queue[0]
                        take = min(remain, pos['volume'])
                        pnl += (pos['price'] - price) * take * mult
                        pos['volume'] -= take
                        remain -= take
                        if pos['volume'] <= 1e-9:
                            queue.pop(0)
                    df.at[idx, 'realized_pnl'] = round(pnl, 2)
                    df.at[idx, 'close_volume'] = round(volume - remain, 2)
                    if remain > 0:
                        df.at[idx, 'open_volume'] = round(remain, 2)
                        long_q.append({'volume': remain, 'price': price, 'mult': mult})
                else:
                    # 先冲抵已有买方持仓（卖出平仓），盈亏 = (卖出价 - 买入价) × 手数 × 乘数
                    queue, remain, pnl = long_q, volume, 0.0
                    while remain > 0 and queue:
                        pos = queue[0]
                        take = min(remain, pos['volume'])
                        pnl += (price - pos['price']) * take * mult
                        pos['volume'] -= take
                        remain -= take
                        if pos['volume'] <= 1e-9:
                            queue.pop(0)
                    df.at[idx, 'realized_pnl'] = round(pnl, 2)
                    df.at[idx, 'close_volume'] = round(volume - remain, 2)
                    if remain > 0:
                        df.at[idx, 'open_volume'] = round(remain, 2)
                        short_q.append({'volume': remain, 'price': price, 'mult': mult})

            # 期末未平仓手数（用于提示已实现盈亏不含浮动部分）
            contract_name = str(g['opt_contract'].iloc[0])
            long_vol = round(sum(p['volume'] for p in long_q), 2)
            short_vol = round(sum(p['volume'] for p in short_q), 2)
            open_positions[contract_name] = {'long': long_vol, 'short': short_vol}

            # ---- 到期作废结算 ----
            # 到期作废的期权不会产生任何平仓成交行，FIFO 只能看到"未平仓"。
            # 这里按到期日把它强制按 0 结算，否则买方付出的权利金（全额亏损）
            # 会永远停在浮动状态，投资回顾里看不到这笔亏损。
            if long_vol <= 0 and short_vol <= 0:
                continue
            if last_data_date is None:
                continue

            expiry = option_expiry_date(contract_name)
            if expiry is None:
                # 该品种的到期日规则未收录（或合约月份无法识别）→ 不结算。
                # 宁可让这笔仓位保持"未平仓"，也不能用猜的规则提前平掉 ——
                # 提前平仓会与后续的真实平仓重复计账。
                if not option_expiry_known(contract_name):
                    note = (f'{contract_name}: 该品种的期权到期日规则未收录，'
                            f'未做到期结算（若已到期作废，这笔亏损会显示为未平仓）')
                    if note not in self.expiry_unknown:
                        self.expiry_unknown.append(note)
                continue

            # 安全网：若该合约在推算的到期日之后仍有成交，说明到期日推算有误，
            # 绝不能把仍在交易的合约强行按 0 结算。
            try:
                contract_last = pd.to_datetime(g['date'], errors='coerce').max().date()
            except Exception:
                contract_last = None
            if contract_last is not None and contract_last > expiry:
                continue

            # 数据还没走到到期日 → 只是正常持有中
            if last_data_date < expiry:
                continue

            pnl = 0.0
            for pos in long_q:
                pnl -= pos['price'] * pos['volume'] * (pos.get('mult') or 1.0)
            for pos in short_q:
                pnl += pos['price'] * pos['volume'] * (pos.get('mult') or 1.0)

            settled_long = long_vol > 0
            total_vol = long_vol if settled_long else short_vol
            entry = (long_q[0] if settled_long else short_q[0])

            row = {col: None for col in df.columns}
            first = g.iloc[0]
            row.update({
                'date': pd.Timestamp(expiry),
                'opt_contract': contract_name,
                'symbol': extract_symbol(contract_name),
                'underlying': first.get('underlying'),
                'opt_type': first.get('opt_type'),
                'strike': first.get('strike'),
                'direction': '到期作废',
                'premium_price': entry['price'],
                'volume': total_vol,
                'multiplier': entry.get('mult') or 1.0,
                'multiplier_source': 'expiry',
                'premium_total': 0.0,
                'pnl': 0.0,
                'commission': 0.0,
                'account': first.get('account'),
                'realized_pnl': round(pnl, 2),
                'close_volume': total_vol,
                'open_volume': 0.0,
                'is_expiry': True,
            })
            expiry_rows.append(row)

            # 结算后该合约已无持仓
            open_positions[contract_name] = {'long': 0.0, 'short': 0.0}
            _sym = extract_symbol(contract_name)
            # 商品期权的到期日是「前一个月第 5 个交易日」，需要法定节假日日历才能算准；
            # 未安装 chinesecalendar 时只会排除周末，日期可能偏早几天（金额不受影响）。
            _approx = (not _HAS_CHINESE_CALENDAR) and (_sym not in _CFFEX_FRIDAY_RULES)
            self.expiry_settlements.append({
                'contract': contract_name,
                'date': expiry.isoformat(),
                'date_is_approximate': _approx,
                'side': '买方' if settled_long else '卖方',
                'volume': total_vol,
                'entry_price': entry['price'],
                'multiplier': entry.get('mult') or 1.0,
                'pnl': round(pnl, 2),
            })
        self.option_open_positions = open_positions

        expiry_df = pd.DataFrame(expiry_rows) if expiry_rows else pd.DataFrame()

        if base_df is None:
            out = df
        elif base_df.empty:
            # 查询窗口内一笔期权成交都没有。
            # 原实现这里直接返回了**全历史** df，导致查询一个与数据不相交的窗口
            # （比如数据只有 3 月、却查 2027 年）仍然会显示出全历史的期权盈亏 ——
            # 凭空多出来的钱。空窗口必须返回空结果。
            out = base_df.copy()
            for col, default in (('realized_pnl', 0.0), ('close_volume', 0.0),
                                 ('open_volume', 0.0), ('multiplier', 1.0)):
                if col not in out.columns:
                    out[col] = default
        else:
            # 用完整历史算出的 realized_pnl 回填到查询窗口内的记录
            key_cols = ['date', 'opt_contract', 'direction', 'premium_price', 'volume']
            merged = base_df.copy()
            helper = df[key_cols + ['realized_pnl', 'close_volume', 'open_volume']].copy()
            helper['__seq'] = helper.groupby(key_cols).cumcount()
            merged['__seq'] = merged.groupby(key_cols).cumcount()
            out = merged.merge(
                helper.drop_duplicates(subset=key_cols + ['__seq']),
                on=key_cols + ['__seq'], how='left')
            for col in ('realized_pnl', 'close_volume', 'open_volume'):
                out[col] = out[col].fillna(0.0)
            out = out.drop(columns='__seq')

        # 到期结算行不属于任何一笔成交，无法通过 merge 回填，
        # 这里按**查询窗口**（而不是窗口内成交的日期范围）过滤后单独追加。
        # 注意必须用查询窗口：某笔期权可能在窗口之前开仓、在窗口内到期作废，
        # 此时窗口内没有任何成交行，但亏损仍然属于这个窗口。
        if not expiry_df.empty:
            win_start, win_end = getattr(self, '_window', (None, None))
            if win_start or win_end:
                def _in_window(row):
                    d = row['date'].date().isoformat()
                    if win_start and d < win_start:
                        return False
                    if win_end and d > win_end:
                        return False
                    return True
                kept = [r for r in expiry_rows if _in_window(r)]
                expiry_df = pd.DataFrame(kept) if kept else pd.DataFrame()
            if not expiry_df.empty:
                out = pd.concat([out, expiry_df], ignore_index=True)

        # 行权结算行也不在 base_df 里（它是从「期权行权明细」翻译成成交的），
        # 必须同样按窗口单独追加 —— 否则实值行权那笔盈亏不会计入查询结果。
        if 'is_exercise' in df.columns:
            ex_df = df[df['is_exercise'] == True].copy()   # noqa: E712  (NaN 不等于 True)
            if not ex_df.empty:
                win_start, win_end = getattr(self, '_window', (None, None))
                if win_start or win_end:
                    keep = []
                    for _, row in ex_df.iterrows():
                        d = str(row['date'])[:10]
                        if win_start and d < win_start:
                            continue
                        if win_end and d > win_end:
                            continue
                        keep.append(row)
                    ex_df = pd.DataFrame(keep) if keep else pd.DataFrame()
                if not ex_df.empty:
                    ex_df['direction'] = '行权'
                    out = pd.concat([out, ex_df], ignore_index=True)
        return out

    def get_matched_options_df(self) -> pd.DataFrame:
        """获取已完成开平仓匹配的期权 DataFrame（含 realized_pnl），带缓存"""
        if getattr(self, '_matched_cache', None) is not None:
            return self._matched_cache
        self._matched_cache = self._match_options_pnl(self.options_df)
        return self._matched_cache

    # ---- 期权乘数告警 ----
    def get_layout_notes(self) -> list:
        """汇总解析时的布局提示（如表头无法识别已回退固定列）。

        这类情况不会报错，但可能算错，必须让用户看到。
        """
        notes = []
        for data in self.loader.cache.values():
            for n in data.get('layout_notes') or []:
                if n not in notes:
                    notes.append(n)
        return notes


    def get_multiplier_warnings(self) -> list:
        """汇总所有「期权合约乘数无法确定或存疑」的提示。

        来源有两处：
          1. 解析结算单时记录到每天数据里的 multiplier_warnings
          2. FIFO 匹配过程中对缺失乘数的补算
        去重后返回，供前端提示"该品种盈亏可能不准"。
        """
        warnings = []
        for data in self.loader.cache.values():
            for w in data.get('multiplier_warnings') or []:
                if w not in warnings:
                    warnings.append(w)
        for w in getattr(self, 'multiplier_warnings', None) or []:
            if w not in warnings:
                warnings.append(w)
        return warnings

    # ---- 按盈利排序的成交记录 ----
    def get_trades_sorted_by_pnl(self, top_n: int = None, ascending: bool = False) -> pd.DataFrame:
        """获取按平仓盈亏排序的成交记录（默认从高到低）"""
        if self.trades_df.empty:
            return pd.DataFrame()
        df = self.trades_df.sort_values('realized_pnl', ascending=ascending).reset_index(drop=True)
        if top_n:
            df = df.head(top_n)
        return df

    # ---- 品种汇总 ----
    def get_symbol_summary(self) -> pd.DataFrame:
        """
        按品种汇总统计。

        期权盈亏使用 **FIFO 开平仓匹配后的已实现盈亏**（平仓那一笔记账），
        同时给出权利金净收支持作为参考列。
        同时覆盖：只做期权的品种（无期货成交记录）。

        Returns DataFrame with columns:
          品种, 手数, 期货盈亏(元), 期货手续费(元), 期权盈亏(元), 期权手续费(元),
          总盈亏(元), 总手续费(元), 胜率, 盈亏比
        """
        # 收集所有品种（期货 ∪ 期权）
        all_symbols = set()
        if not self.trades_df.empty:
            all_symbols.update(self.trades_df['symbol'].unique())

        opt_matched = self.get_matched_options_df()
        if not opt_matched.empty:
            all_symbols.update(opt_matched['symbol'].unique())

        if not all_symbols:
            return pd.DataFrame()

        df = self.trades_df.copy()

        results = []
        for symbol in sorted(all_symbols):
            # ---- 期货部分 ----
            futures_of_symbol = df[df['symbol'] == symbol] if not df.empty else pd.DataFrame()

            total_pnl = futures_of_symbol['realized_pnl'].sum() if not futures_of_symbol.empty else 0
            total_commission = futures_of_symbol['commission'].sum() if not futures_of_symbol.empty else 0

            # ---- 期权部分（FIFO 已实现盈亏）----
            opt_group = opt_matched[opt_matched['symbol'] == symbol] if not opt_matched.empty else pd.DataFrame()
            opt_commission = opt_group['commission'].sum() if not opt_group.empty else 0
            opt_pnl = opt_group['realized_pnl'].sum() if not opt_group.empty else 0

            # ---- 胜率 / 盈亏比：期货 + 期权合并统计 ----
            # 口径必须与 /api/symbol 的品种详情一致，否则主表显示"盈亏比 ∞"
            # （纯期权亏损品种）、而点进去的详情却显示 0.0%，自相矛盾。
            pnl_values = []
            if not futures_of_symbol.empty:
                pnl_values += futures_of_symbol['realized_pnl'].tolist()
            if not opt_group.empty:
                pnl_values += opt_group['realized_pnl'].tolist()
            stats = compute_pnl_stats(pnl_values)

            total_volume = futures_of_symbol['volume'].sum() if not futures_of_symbol.empty else 0
            total_pnl_all = total_pnl + opt_pnl
            total_comm_all = total_commission + opt_commission

            results.append({
                '品种': symbol,
                '手数': int(total_volume),
                '期货盈亏(元)': round(total_pnl, 2),
                '期货手续费(元)': round(total_commission, 2),
                '期权盈亏(元)': round(opt_pnl, 2),
                '期权手续费(元)': round(opt_commission, 2),
                '总盈亏(元)': round(total_pnl_all, 2),
                '总手续费(元)': round(total_comm_all, 2),
                # 胜率与盈亏比（期货 + 期权的已实现盈亏合并统计）
                '胜率': stats['win_rate_text'],
                '盈亏比': stats['profit_factor_text'],
            })

        result_df = pd.DataFrame(results)
        if not result_df.empty:
            result_df = result_df.sort_values('总盈亏(元)', ascending=False).reset_index(drop=True)
        return result_df

    # ---- 每日盈亏趋势（基于逐笔平仓盈亏求和）----
    def get_daily_trend(self) -> pd.DataFrame:
        """每日累计盈亏趋势，期货平仓盈亏 + 期权 FIFO 已实现盈亏按日期求和"""
        if self.trades_df.empty and self.options_df.empty:
            return pd.DataFrame()

        # 从期货逐笔数据按日期汇总
        daily = {}
        if not self.trades_df.empty:
            for _, row in self.trades_df.iterrows():
                d = str(row['date'])[:10]
                if d not in daily:
                    daily[d] = {'futures_pnl': 0.0, 'futures_comm': 0.0, 'opt_pnl': 0.0, 'opt_comm': 0.0}
                daily[d]['futures_pnl'] += row['realized_pnl']
                daily[d]['futures_comm'] += row['commission']

        # 从期权逐笔数据按日期汇总（FIFO 匹配后的已实现盈亏，记在平仓日）
        opt_matched = self.get_matched_options_df()
        if not opt_matched.empty:
            for _, row in opt_matched.iterrows():
                d = str(row['date'])[:10]
                if d not in daily:
                    daily[d] = {'futures_pnl': 0.0, 'futures_comm': 0.0, 'opt_pnl': 0.0, 'opt_comm': 0.0}
                daily[d]['opt_pnl'] += row.get('realized_pnl', 0)
                daily[d]['opt_comm'] += row.get('commission', 0)
        
        rows = []
        for d in sorted(daily.keys()):
            item = daily[d]
            total_pnl = item['futures_pnl'] + item['opt_pnl']
            total_comm = item['futures_comm'] + item['opt_comm']
            rows.append({
                'date': d,
                'pnl': total_pnl,
                'futures_pnl': item['futures_pnl'],
                'opt_pnl': item['opt_pnl'],
                'commission': total_comm,
            })
        
        df = pd.DataFrame(rows)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df['累计净盈亏'] = (df['pnl'] - df['commission']).cumsum()
        return df

    # ---- 账户权益曲线 ----
    def get_equity_curve(self) -> pd.DataFrame:
        """账户权益曲线（来自结算单「资金状况」区，按日）。

        列：date, equity(客户权益), deposit(当日结存), margin(保证金占用),
            available(可用资金≈权益-保证金), pnl(当日盈亏), commission(手续费)

        权益全为 0 时返回空表 —— 说明结算单里没有资金状况区，
        画出来会是一条压在 0 上的直线，没有意义。
        """
        df = self.daily_summary_df
        if df is None or df.empty:
            return pd.DataFrame()

        out = df.copy()
        for col in ('equity', 'deposit', 'margin', 'pnl', 'commission', 'float_pnl'):
            if col not in out.columns:
                out[col] = 0.0
            out[col] = pd.to_numeric(out[col], errors='coerce').fillna(0.0)

        if not (out['equity'] != 0).any():
            return pd.DataFrame()

        out['available'] = (out['equity'] - out['margin']).round(2)
        cols = ['date', 'equity', 'deposit', 'margin', 'available',
                'pnl', 'commission', 'float_pnl']
        return out[cols].reset_index(drop=True)

    # ---- 按日期查询 ----
    def query_by_date_range(self, start: str, end: str) -> dict:
        """综合查询指定日期范围"""
        self.build_dfs(start, end)

        symbol_df = self.get_symbol_summary()
        trades_df = self.get_trades_sorted_by_pnl()
        trend_df = self.get_daily_trend()

        # 只计算期货逐笔平仓盈亏
        futures_pnl = self.trades_df['realized_pnl'].sum() if not self.trades_df.empty else 0
        futures_comm = self.trades_df['commission'].sum() if not self.trades_df.empty else 0
        # 期权：FIFO 匹配后的已实现盈亏（记在平仓日），权利金收支单列为参考项
        opt_matched = self.get_matched_options_df()
        opt_realized = opt_matched['realized_pnl'].sum() if not opt_matched.empty else 0
        opt_cashflow = opt_matched['pnl'].sum() if not opt_matched.empty else 0
        opt_comm = opt_matched['commission'].sum() if not opt_matched.empty else 0

        overview = {
            '查询范围': f'{start} ~ {end}',
            '交易日数': len(self.daily_summary_df),
            '期货笔数': len(self.trades_df),
            '期权笔数': len(self.options_df),
            # 期货（核心指标）
            '期货盈亏(元)': round(futures_pnl, 2),
            '期货手续费(元)': round(futures_comm, 2),
            '期货净盈亏(元)': round(futures_pnl - futures_comm, 2),
            # 期权（FIFO 已实现盈亏 + 权利金现金流参考）
            '期权盈亏(元)': round(opt_realized, 2),
            '期权权利金收支(元)': round(opt_cashflow, 2),
            '期权手续费(元)': round(opt_comm, 2),
            '期权净盈亏(元)': round(opt_realized - opt_comm, 2),
            '品种数': len(symbol_df) if not symbol_df.empty else 0,
        }

        return {
            'overview': overview,
            'symbol_summary': symbol_df,
            'trades_sorted': trades_df,
            'daily_trend': trend_df,
            # 期权乘数无法确定/存疑的提示（前端需要显著展示，否则盈亏会被悄悄放大或缩小）
            'multiplier_warnings': self.get_multiplier_warnings(),
            'opt_open_positions': dict(getattr(self, 'option_open_positions', {}) or {}),
            # 被按「到期作废」强制结算的合约（虚值到期不会产生成交行）
            'expiry_settlements': list(getattr(self, 'expiry_settlements', []) or []),
            # 到期日规则未收录、因而未做结算的合约（提示人工核对）
            'expiry_unknown': list(getattr(self, 'expiry_unknown', []) or []),
            # 方向无法识别（行权 / 履约 / 到期）的期权行：这些行不参与盈亏计算
            'unparsed_option_rows': list(getattr(self, 'unparsed_option_rows', []) or []),
            # 解析布局提示（表头无法识别时回退了固定列）
            'layout_notes': self.get_layout_notes(),
        }


def compute_equity_stats(equity_df: pd.DataFrame) -> dict:
    """根据每日资金数据计算区间权益变动与最大回撤。

    数据来自结算单的「资金状况」区（客户权益 / 保证金占用 / 当日结存）。
    早期版本把这块解析出来却从未展示，这些指标就是让它产生价值的地方。

    返回：首末权益、变动额与比例、最大回撤（金额与百分比）、峰谷日期。
    """
    if equity_df is None or equity_df.empty:
        return {}
    if 'equity' not in equity_df.columns:
        return {}

    eq = pd.to_numeric(equity_df['equity'], errors='coerce')
    if eq.notna().sum() == 0:
        return {}

    dates = (pd.to_datetime(equity_df['date'], errors='coerce')
             .dt.strftime('%Y-%m-%d').tolist())

    # 跳过「入金前的 0 权益」。
    # 账户第一天往往权益为 0（还没入金），若把它当作起点，
    # 首末权益会变成 0 → 3 万，回撤也会失真。以第一笔正的权益作为起点。
    positive = eq[eq > 0]
    if positive.empty:
        return {}
    start_pos = int(eq.index.get_loc(positive.index[0]))
    eq = eq.iloc[start_pos:].reset_index(drop=True)
    dates = dates[start_pos:]

    first_equity = float(eq.iloc[0])
    last_equity = float(eq.iloc[-1])
    change = last_equity - first_equity
    change_pct = (change / first_equity * 100) if first_equity else None

    # 最大回撤：权益相对历史峰值的最大跌幅
    peak = eq.cummax()
    drawdown = eq - peak
    trough_pos = int(drawdown.idxmin()) if len(drawdown) else 0
    max_dd = float(drawdown.iloc[trough_pos]) if len(drawdown) else 0.0
    peak_value = float(peak.iloc[trough_pos]) if len(drawdown) else 0.0
    max_dd_pct = (max_dd / peak_value * 100) if peak_value else None

    # 回撤峰值出现在谷底之前最近的最高点
    peak_pos = trough_pos
    for i in range(trough_pos, -1, -1):
        if abs(float(eq.iloc[i]) - peak_value) < 1e-9:
            peak_pos = i
            break

    def _date(pos):
        return dates[pos] if 0 <= pos < len(dates) and dates[pos] else None

    return {
        'first_equity': round(first_equity, 2),
        'last_equity': round(last_equity, 2),
        'change': round(change, 2),
        'change_pct': round(change_pct, 2) if change_pct is not None else None,
        'max_drawdown': round(max_dd, 2),
        'max_drawdown_pct': round(max_dd_pct, 2) if max_dd_pct is not None else None,
        'peak_date': _date(peak_pos),
        'trough_date': _date(trough_pos),
        'days': int(len(eq)),
    }


# ============================================================
# 可视化
# ============================================================
# matplotlib 的 pyplot 状态（figure 注册表、rcParams）是全局的，多线程同时
# 画图会互相干扰，甚至把 A 请求的图写进 B 请求的文件。这里统一串行化。
_PLT_LOCK = threading.Lock()


def _serialized_plot(func):
    """给画图方法加全局锁，避免并发请求互相污染 figure。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _PLT_LOCK:
            return func(*args, **kwargs)
    return wrapper


class ReviewVisualizer:
    """复盘结果可视化"""

    def __init__(self, output_dir: str = './review_charts'):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def _save(self, fig, filename: str, save_path: str = None) -> str:
        """保存并关闭 figure。

        save_path 为 None 时用固定文件名（CLI 场景）；
        Web 场景会传入带唯一后缀的临时路径，避免并发请求互相覆盖同一个文件。
        """
        path = save_path or os.path.join(self.output_dir, filename)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fig.savefig(path, dpi=120, bbox_inches='tight')
        plt.close(fig)
        return path

    @_serialized_plot
    def plot_symbol_pnl_bar(self, symbol_df: pd.DataFrame, title: str = '品种净盈亏排名',
                            save_path: str = None) -> str:
        """品种净盈亏柱状图"""
        if symbol_df.empty:
            return ''
        fig, ax = plt.subplots(figsize=(max(10, len(symbol_df) * 0.7), 6))

        symbols = symbol_df['品种'].tolist()
        # symbol_summary 只有 '总盈亏(元)'（未扣手续费），没有 '净盈亏(元)' 列，
        # 原来的 `'净盈亏(元)' if ... else` 分支永远走 else，属死代码；
        # 坐标轴标签却写死成"净盈亏"，容易让人以为已经扣过手续费。
        pnl_col = '净盈亏(元)' if '净盈亏(元)' in symbol_df.columns else '总盈亏(元)'
        axis_label = '净盈亏 (元)' if pnl_col == '净盈亏(元)' else '总盈亏 (元，未扣手续费)'
        pnls = symbol_df[pnl_col].tolist()
        colors = ['#e74c3c' if v >= 0 else '#27ae60' for v in pnls]

        bars = ax.barh(symbols[::-1], [v for v in reversed(pnls)], color=colors[::-1], edgecolor='white', height=0.6)
        ax.axvline(x=0, color='gray', linewidth=0.8, linestyle='-')
        ax.set_xlabel(axis_label, fontsize=11)
        ax.set_title(title, fontsize=14, fontweight='bold', pad=15)

        for bar, val in zip(bars, pnls[::-1]):
            width = bar.get_width()
            offset = 5 if width >= 0 else -5
            ha = 'left' if width >= 0 else 'right'
            ax.text(width + offset, bar.get_y() + bar.get_height()/2,
                    f'{val:+,.0f}', va='center', ha=ha, fontsize=9)

        plt.tight_layout()
        return self._save(fig, 'symbol_pnl_ranking.png', save_path)

    @_serialized_plot
    def plot_daily_trend(self, trend_df: pd.DataFrame, title: str = '每日累计净盈亏趋势',
                         save_path: str = None) -> str:
        """每日盈亏趋势折线图"""
        if trend_df.empty:
            return ''
        fig, ax1 = plt.subplots(figsize=(14, 6))

        dates = trend_df['date'].dt.strftime('%m-%d').tolist()
        x = range(len(dates))

        # 每日净盈亏柱状图
        daily_net = (trend_df['pnl'] - trend_df['commission']).tolist()
        colors = ['#e74c3c' if v >= 0 else '#27ae60' for v in daily_net]
        ax1.bar(x, daily_net, color=colors, alpha=0.6, label='每日净盈亏', width=0.7)

        # 累计线
        ax2 = ax1.twinx()
        cumulative = trend_df['累计净盈亏'].tolist()
        ax2.plot(x, cumulative, 'b-o', linewidth=2, markersize=5, label='累计净盈亏')
        ax2.axhline(y=0, color='blue', linewidth=0.8, linestyle='--', alpha=0.5)

        ax1.set_xticks(x)
        ax1.set_xticklabels(dates, rotation=45, ha='right', fontsize=9)
        ax1.set_ylabel('每日净盈亏 (元)', fontsize=11, color='gray')
        ax2.set_ylabel('累计净盈亏 (元)', fontsize=11, color='blue')
        ax1.set_title(title, fontsize=14, fontweight='bold', pad=15)

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left')

        plt.tight_layout()
        return self._save(fig, 'daily_trend.png', save_path)

    @_serialized_plot
    def plot_symbol_pie(self, symbol_df: pd.DataFrame, title: str = '品种盈亏分布',
                        save_path: str = None) -> str:
        """品种盈亏饼图（只显示有盈亏的品种）"""
        if symbol_df.empty:
            return ''
        df = symbol_df[symbol_df['总盈亏(元)'] != 0].copy()
        if df.empty:
            return ''

        fig, ax = plt.subplots(figsize=(10, 8))
        labels = df['品种'].tolist()
        sizes = [abs(v) for v in df['总盈亏(元)'].tolist()]
        colors = ['#e74c3c' if v > 0 else '#27ae60' for v in df['总盈亏(元)'].tolist()]

        explode = [0.05] * len(labels)
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, explode=explode,
            autopct=lambda pct: f'{pct:.1f}%\n({pct/100*sum(sizes):+.0f}元)' if pct > 3 else '',
            textprops={'fontsize': 9}, startangle=90, pctdistance=0.75
        )
        ax.set_title(title, fontsize=14, fontweight='bold')

        plt.tight_layout()
        return self._save(fig, 'symbol_pie.png', save_path)

    @_serialized_plot
    def plot_win_rate(self, symbol_df: pd.DataFrame, title: str = '品种胜率与盈亏比',
                      save_path: str = None) -> str:
        """胜率和盈亏比对比图。

        注意：get_symbol_summary() 在没有已平仓记录时，胜率列给出的是 '-'，
        盈亏比列可能是 '∞'。这里必须先做数值化清洗再过滤，否则
        float('-') 会抛 ValueError，把整张图（乃至 CLI 报告）带崩。
        """
        if symbol_df.empty:
            return ''

        df = symbol_df.copy()
        df['__wr'] = pd.to_numeric(
            df['胜率'].astype(str).str.replace('%', '', regex=False), errors='coerce')
        df['__pf'] = pd.to_numeric(
            df['盈亏比'].astype(str).map({'∞': '10', '-': None, '': None, 'nan': None}),
            errors='coerce')
        df = df[df['__wr'].notna()]
        if df.empty:
            return ''

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        symbols = df['品种'].tolist()
        x = range(len(symbols))

        # 胜率
        win_rates = df['__wr'].tolist()
        colors_wr = ['#27ae60' if wr >= 50 else '#e74c3c' for wr in win_rates]
        ax1.bar(x, win_rates, color=colors_wr, width=0.6)
        ax1.axhline(y=50, color='gray', linewidth=1, linestyle='--', label='50%基准线')
        ax1.set_xticks(x)
        ax1.set_xticklabels(symbols, rotation=45, ha='right', fontsize=9)
        ax1.set_ylabel('胜率 (%)', fontsize=11)
        ax1.set_title('各品种胜率', fontsize=12, fontweight='bold')
        ax1.legend()
        ax1.set_ylim(0, 110)

        # 盈亏比
        pf_values = [10.0 if pd.isna(v) else float(v) for v in df['__pf'].tolist()]
        colors_pf = ['#27ae60' if pf >= 1 else '#e74c3c' for pf in pf_values]
        ax2.bar(x, pf_values, color=colors_pf, width=0.6)
        ax2.axhline(y=1, color='gray', linewidth=1, linestyle='--', label='盈亏平衡线')
        ax2.set_xticks(x)
        ax2.set_xticklabels(symbols, rotation=45, ha='right', fontsize=9)
        ax2.set_ylabel('盈亏比', fontsize=11)
        ax2.set_title('各品种盈亏比', fontsize=12, fontweight='bold')
        ax2.legend()

        fig.suptitle(title, fontsize=14, fontweight='bold', y=1.02)
        plt.tight_layout()
        return self._save(fig, 'win_rate_profit_factor.png', save_path)


# ============================================================
# 自动补数
# ============================================================
def auto_download_missing(missing_dates: list[str], config: dict):
    """调用 cfmmc_crawler 补充缺失的数据。

    限速与熔断：
      - 每次请求之间 sleep（原实现连续打，缺失一个月就是 ~40 次连发，有被风控的风险）
      - 连续失败达到阈值就停手，避免在登录态失效时继续无效请求
      - 单次最多补 MAX_DAYS 个交易日，其余留到下次（防止误查长区间时打爆）
    """
    try:
        from cfmmc_crawler import CFMMCCrawler
    except ImportError:
        print('错误: 无法导入 cfmmc_crawler，请确认文件存在')
        return

    MAX_DAYS = int(os.environ.get('CFMMC_MAX_AUTO_DOWNLOAD_DAYS', '60'))
    MAX_CONSECUTIVE_FAIL = 5
    REQUEST_INTERVAL = float(os.environ.get('CFMMC_DOWNLOAD_INTERVAL', '0.5'))

    dates = list(missing_dates or [])
    if len(dates) > MAX_DAYS:
        print(f'⚠ 缺失 {len(dates)} 个交易日，本次只补最近 {MAX_DAYS} 个'
              f'（可用环境变量 CFMMC_MAX_AUTO_DOWNLOAD_DAYS 调整）')
        dates = dates[-MAX_DAYS:]
    if not dates:
        return

    for account in config.get('accounts', []):
        # 注意：CFMMCCrawler 的签名是 (broker, account_no, password, output_dir,
        # auto_recognize) —— 没有 fund_name 形参。原先按位置传了 5 个参数再叠一个
        # auto_recognize 关键字，会直接抛 TypeError，导致「自动补数」整条链路失效。
        # 这里统一用关键字传参，避免再出现顺序错位。
        try:
            crawler = CFMMCCrawler(
                broker=account['broker'],
                account_no=account['account_no'],
                password=account['password'],
                output_dir=config['output_dir'],
                auto_recognize=True,
            )
        except KeyError as e:
            print(f'账户配置缺少字段 {e}，跳过')
            continue
        except TypeError as e:
            print(f'创建爬虫失败（请检查 CFMMCCrawler 签名）: {e}')
            continue
        print(f'\n正在登录 {account.get("fund_name", "")} - {account["broker"]} ...')
        try:
            crawler.login()
        except Exception as e:
            print(f'登录失败: {e}')
            continue

        if not crawler.token:
            continue

        consecutive_fail = 0
        for d in dates:
            try:
                dt_obj = datetime.strptime(d, '%Y-%m-%d').date()
            except ValueError:
                continue
            for qt in ['逐日', '逐笔']:
                try:
                    crawler.get_daily_data(dt_obj, qt)
                    consecutive_fail = 0
                except Exception as e:
                    consecutive_fail += 1
                    print(f'  下载失败 {d} {qt}: {e}')
                time.sleep(REQUEST_INTERVAL)
            if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                print(f'  ⚠ 连续失败 {consecutive_fail} 次，停止本次补数')
                break
        crawler.logout()


# ============================================================
# 报告输出
# ============================================================
def print_report(result: dict, visualizer: ReviewVisualizer = None):
    """打印复盘报告"""
    ov = result['overview']
    sym = result['symbol_summary']
    trades = result['trades_sorted']
    trend = result['daily_trend']

    print('\n' + '=' * 70)
    print(f'  📊 期货交易复盘报告  |  {ov["查询范围"]}')
    print('=' * 70)

    # 总览（键与 query_by_date_range 返回的 overview 保持一致）
    total_trades = ov.get('期货笔数', 0) + ov.get('期权笔数', 0)
    total_pnl = ov.get('期货盈亏(元)', 0) + ov.get('期权盈亏(元)', 0)
    total_comm = ov.get('期货手续费(元)', 0) + ov.get('期权手续费(元)', 0)
    print(f'\n┌─── 总览 ───────────────────────────────────────┐')
    print(f'│  交易日数:     {ov["交易日数"]:>6}                          │')
    print(f'│  总成交笔数:   {total_trades:>6}                          │')
    print(f'│  品种数:       {ov["品种数"]:>6}                          │')
    print(f'│  ─────────────────────────────────────────── │')
    print(f'│  总盈亏:       {total_pnl:>+12,.2f} 元              │')
    print(f'│  总手续费:     {total_comm:>12,.2f} 元              │')
    print(f'│  净盈亏:       {total_pnl - total_comm:>+12,.2f} 元              │')
    print(f'│  其中期权(FIFO已实现): {ov.get("期权盈亏(元)", 0):>+12,.2f} 元       │')
    print(f'└──────────────────────────────────────────────┘')

    # 品种汇总
    if not sym.empty:
        print(f'\n┌─── 品种汇总（按净盈亏排序）────────────────────┐')
        print(sym.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

    # TOP 盈利/亏损交易
    # 只统计真正产生平仓盈亏的记录：开仓行（结算单里开仓列为 '--' → 0）不应
    # 混进"TOP 盈利/亏损"，否则盈利不足 10 笔时会掺进一堆 +0.00 的开仓记录。
    closed_trades = (trades[trades['realized_pnl'] != 0]
                     if not trades.empty else trades)
    if not closed_trades.empty:
        print(f'\n┌─── TOP 10 盈利交易 ───────────────────────────┐')
        top_wins = closed_trades.head(10)[['date', 'contract', 'direction', 'open_close',
                                           'price', 'volume', 'realized_pnl', 'commission']].copy()
        top_wins.columns = ['日期', '合约', '方向', '开平', '价格', '手数', '平仓盈亏', '手续费']
        top_wins['日期'] = top_wins['日期'].astype(str).str[:10]
        print(top_wins.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

        print(f'\n┌─── TOP 10 亏损交易 ───────────────────────────┐')
        top_losses = closed_trades.tail(10)[['date', 'contract', 'direction', 'open_close',
                                             'price', 'volume', 'realized_pnl', 'commission']].iloc[::-1].copy()
        top_losses.columns = ['日期', '合约', '方向', '开平', '价格', '手数', '平仓盈亏', '手续费']
        top_losses['日期'] = top_losses['日期'].astype(str).str[:10]
        print(top_losses.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

    # 可视化
    if visualizer:
        print('\n📈 正在生成图表...')
        paths = []
        p1 = visualizer.plot_symbol_pnl_bar(sym)
        if p1:
            paths.append(p1)
        p2 = visualizer.plot_daily_trend(trend)
        if p2:
            paths.append(p2)
        p3 = visualizer.plot_symbol_pie(sym)
        if p3:
            paths.append(p3)
        p4 = visualizer.plot_win_rate(sym)
        if p4:
            paths.append(p4)
        for p in paths:
            print(f'  ✅ {p}')

    print()


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='期货交易复盘系统')
    parser.add_argument('--start', type=str, help='开始日期 YYYYMMDD 或 YYYY-MM-DD')
    parser.add_argument('--end', type=str, help='结束日期 YYYYMMDD 或 YYYY-MM-DD')
    parser.add_argument('--output-dir', type=str, default=None, help='数据目录')
    parser.add_argument('--no-chart', action='store_true', help='不生成图表')
    parser.add_argument('--auto-download', action='store_true', help='自动下载缺失数据')
    parser.add_argument('--dump-sheet', type=str, default=None, metavar='DATE',
                        help='诊断模式：导出指定日期结算单原文（YYYY-MM-DD）')
    parser.add_argument('--contract', type=str, default=None,
                        help='配合 --dump-sheet，只看包含该合约的行')
    parser.add_argument('--list-data', action='store_true',
                        help='诊断模式：列出数据目录里实际有哪些结算单文件')
    parser.add_argument('--clean-bad', action='store_true',
                        help='把无效结算单（登录过期存下的网页）改名成 .bad')
    parser.add_argument('--dry-run', action='store_true',
                        help='配合 --clean-bad，只统计不实际改名')
    args = parser.parse_args()

    # 加载配置
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)

    output_dir = args.output_dir or config.get('output_dir', DEFAULT_OUTPUT_DIR)
    # 配置的目录里没有结算单时，向上找同名目录（常见于相对路径 ./output 指错）
    output_dir = resolve_output_dir(output_dir)

    # 格式化日期
    def fmt_date(d):
        d = d.strip()
        if len(d) == 8:
            return f'{d[:4]}-{d[4:6]}-{d[6:]}'
        return d

    # ---- 诊断模式：列出数据目录里到底有什么 ----
    if args.list_data:
        dumper = DataLoader(output_dir)
        dumper.load_all()          # 先真正加载一次，loaded_days 才有意义
        info = dumper.describe_data()
        print(f'数据目录: {info["output_dir"]}')
        print(f'  目录存在        : {info["output_dir_exists"]}')
        print(f'  匹配到的结算单  : {info["file_count"]} 个'
              f'（逐笔 {info["zhubi_files"]} / 逐日 {info["zhuri_files"]}）')
        print(f'  无效文件(网页)  : {info["invalid_files"]} 个'
              f'{"（可用 --clean-bad 清理）" if info["invalid_files"] else ""}')
        print(f'  已加载交易日    : {info["loaded_days"]} 天'
              f'  ({info["first_day"] or "-"} ~ {info["last_day"] or "-"})')
        print(f'  扫描模式        : {info["pattern"]}')
        if info['sample_files']:
            print('  样例文件:')
            for f in info['sample_files']:
                print(f'    {f}')
        if info['xls_outside_zhubi']:
            print('  ⚠ 有 .xls 但不在「逐笔/逐日」目录下（不会被加载）:')
            for f in info['xls_outside_zhubi']:
                print(f'    {f}')
        if info['xlsx_count']:
            print(f'  ⚠ 有 {info["xlsx_count"]} 个 .xlsx 文件 —— '
                  f'本系统只读 .xls，请另存为 .xls')
        if info['quarantined']:
            print('  已隔离的坏文件 (.bad):')
            for f in info['quarantined']:
                print(f'    {f}')
        if not info['output_dir_exists']:
            print('\n  → 目录不存在。请确认是在项目根目录运行，'
                  '或用 --output-dir 指定实际路径')
        elif info['file_count'] == 0:
            print('\n  → 目录里没有「逐笔/逐日/*.xls」。请先登录下载数据，'
                  '或确认 CSV/XLSX 已另存为 .xls 并放进 日报/逐笔 或 日报/逐日 目录')
        return

    # ---- 清理无效结算单 ----
    if args.clean_bad:
        dumper = DataLoader(output_dir)
        res = dumper.clean_bad_files(dry_run=args.dry_run)
        mode = '（仅统计）' if res['dry_run'] else ''
        print(f'{mode}发现无效结算单 {res["found"]} 个')
        if res['sample']:
            print('  样例:')
            for f in res['sample']:
                print(f'    {f}')
        if not res['dry_run']:
            print(f'  已改名为 .bad: {res["renamed"]} 个，失败 {res["failed"]} 个')
            print('  重新查询时会自动补下载')
        return

    # ---- 诊断模式：导出结算单原文 ----
    if args.dump_sheet:
        dump_target = fmt_date(args.dump_sheet)
        dumper = DataLoader(output_dir)
        dump = dumper.dump_sheet(dump_target, args.contract)
        if dump.get('error'):
            print(f'✗ {dump["error"]}')
            info = dump.get('data_info') or {}
            if info.get('output_dir'):
                print(f'\n  数据目录      : {info["output_dir"]}')
                print(f'  目录存在      : {info["output_dir_exists"]}')
                print(f'  扫描模式      : {info["pattern"]}')
                print(f'  匹配到的文件  : {info["file_count"]} 个')
                print(f'  已加载交易日  : {info["loaded_days"]} 天'
                      f'  ({info["first_day"] or "-"} ~ {info["last_day"] or "-"})')
                if info.get('sample_files'):
                    print('  样例文件:')
                    for f in info['sample_files']:
                        print(f'    {f}')
                if info.get('xls_outside_zhubi'):
                    print('  ⚠ 有 .xls 但不在「逐笔」目录下:')
                    for f in info['xls_outside_zhubi']:
                        print(f'    {f}')
                if info.get('xlsx_count'):
                    print(f'  ⚠ 有 {info["xlsx_count"]} 个 .xlsx（只支持 .xls）')
                print('\n  提示: 用 --list-data 可查看完整清单')
            return
        print(f'文件: {dump["file"]}')
        print(f'尺寸: {dump["nrows"]} 行 × {dump["ncols"]} 列'
              f'{"（已截断）" if dump.get("truncated") else ""}')
        print('\n识别到的区块标题:')
        for sec in dump['sections']:
            print(f'  第 {sec["row"]:>3} 行: {sec["title"]}')
        if args.contract:
            print(f'\n包含 "{args.contract}" 的行:')
            if not dump['matches']:
                print('  （没有找到 —— 说明该合约不在这一天的结算单里）')
            for m in dump['matches']:
                print(f'  第 {m["row"]:>3} 行: {m["cells"]}')
        else:
            print('\n全部内容:')
            for i, cells in enumerate(dump['rows']):
                if any(c for c in cells):
                    print(f'  {i:>3}| ' + ' | '.join(cells))
        return

    start = fmt_date(args.start) if args.start else None
    end = fmt_date(args.end) if args.end else None

    # 交互式输入
    if not start:
        start = input('开始日期 (YYYYMMDD): ').strip()
        start = fmt_date(start)
    if not end:
        end = input('结束日期 (YYYYMMDD): ').strip()
        end = fmt_date(end)

    # 加载数据
    loader = DataLoader(output_dir)
    loader.load_all()

    _cal_note = trading_calendar_note()
    if _cal_note:
        print(f'⚠ {_cal_note}')

    # 检查缺失（分类展示：真缺口 / 早于已有数据 / 晚于已有数据）
    detail = loader.analyze_missing_dates(start, end)
    if detail['missing'] and args.auto_download:
        todo = detail['download_candidates']
        if todo:
            print(f'\n发现 {len(todo)} 个待补交易日，自动下载中...')
            auto_download_missing(todo, config)
            loader.load_all()  # 重新加载
            detail = loader.analyze_missing_dates(start, end)
        else:
            print('\n没有需要补下载的交易日（缺失日期均早于本地最早数据）')

    if detail['missing']:
        print(f'\n⚠️  区间内 {detail["trading_days"]} 个交易日，'
              f'已有 {detail["with_data"]} 天，缺失 {len(detail["missing"])} 天')
        if detail['gaps']:
            print(f'    真实缺口 {len(detail["gaps"])} 天（最该关注）: '
                  f'{detail["gaps"][:5]}{"..." if len(detail["gaps"]) > 5 else ""}')
        if detail['before_range']:
            print(f'    早于本地最早数据 {len(detail["before_range"])} 天'
                  f'（{detail["first_data"]} 之前，多为账户开立前）')
        if detail['after_range']:
            print(f'    晚于本地最新数据 {len(detail["after_range"])} 天'
                  f'（{detail["last_data"]} 之后，可补下载）')
    if detail['holiday_days']:
        print(f'    区间内休市 {detail["holiday_days"]} 天（节假日，本来就没有结算单）')

    # 分析
    engine = ReviewEngine(loader)
    result = engine.query_by_date_range(start, end)

    # 可视化 & 输出
    viz = None if args.no_chart else ReviewVisualizer()
    print_report(result, viz)


if __name__ == '__main__':
    main()
