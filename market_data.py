"""
行情数据层（品种筛选功能的数据底座）
====================================
数据源：新浪财经期货「主力连续合约」日线
    GET https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{CODE}0=/
        InnerFuturesNewService.getDailyKLine?symbol={CODE}0

为什么选新浪：
  - 免费、无需 token、无需注册（对比 tushare 需要积分、akshare 依赖重）
  - 覆盖国内五大交易所全部活跃品种，返回从上市至今的完整日线
  - 单品种 1 次请求即可拿到全部历史，便于计算「长期」高低点

设计要点：
  1. 全部品种并发拉取（ThreadPoolExecutor），一次全量刷新约 1~3 秒
  2. 两级缓存：进程内 LRU + 磁盘 JSON（默认 TTL 600 秒），避免频繁打第三方
  3. 任何网络失败都退回磁盘缓存（标记 stale=True），不让筛选整体失败
  4. 数据源可通过 `KlineStore(provider=...)` 注入替换，便于换源/测试

已知局限（务必知悉）：
  - 「主力连续合约」在主力换月时会产生价格跳空（新旧合约价差），
    因此超长期高低点可能包含换月缺口造成的失真。本模块对此不做复权处理，
    界面上会明确标注数据口径。
  - 日线最后一根在盘中是「未收盘」的实时快照，尾盘前指标会随价格抖动。
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

import requests

# ============================================================
# 配置
# ============================================================

CACHE_DIR = os.environ.get('SCREENER_CACHE_DIR', './cache/market')
CACHE_TTL = int(os.environ.get('SCREENER_CACHE_TTL', '600'))  # 秒
HTTP_TIMEOUT = int(os.environ.get('SCREENER_HTTP_TIMEOUT', '20'))
MAX_WORKERS = int(os.environ.get('SCREENER_WORKERS', '10'))

# 换月复权序列的起点：留足 1000+ 个交易日，保证 lookback=250 与各指标预热都够用
ADJUST_START = os.environ.get('SCREENER_ADJUST_START', '2022-06-01')

_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_HEADERS = {
    'User-Agent': _USER_AGENT,
    'Referer': 'https://finance.sina.com.cn',
}

_KLINE_URL = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
              'var%20_{code}=/InnerFuturesNewService.getDailyKLine?symbol={code}')

# 实时快照（盘中最新价）。日线接口只在收盘后更新，因此筛选必须叠加实时价，
# 否则「价格即将突破前高」在盘中根本抓不到。
_REALTIME_URL = 'https://hq.sinajs.cn/list={codes}'
_REALTIME_BATCH = 40

# 匹配 JSONP 外壳: var _CU0=([{...},{...}]);
_JSONP_RE = re.compile(r'=\((\[.*?\])\)\s*;?\s*$', re.S)

# 新浪内盘期货实时字段位置（nf_ 前缀）
_RT_IDX = {
    'open': 2, 'high': 3, 'low': 4, 'last': 8,
    'prev_settle': 10, 'open_interest': 13, 'volume': 14, 'date': 17,
}


# ============================================================
# 品种元数据
# ============================================================
# sina 代码 = 品种代码 + '0'（主力连续）。已验证可用性；
# 郑商所 WH/JR/LR/PM/RI 等冷门品种新浪不提供主力连续，故不列入。

@dataclass(frozen=True)
class SymbolMeta:
    code: str          # 品种代码（大写），如 'CU'
    name: str          # 中文名，如 '沪铜'
    exchange: str      # 交易所，如 'SHFE'

    @property
    def sina(self) -> str:
        return f'{self.code}0'

    @property
    def display(self) -> str:
        return f'{self.name}({self.code})'


EXCHANGE_NAMES = {
    'SHFE': '上期所',
    'INE': '能源中心',
    'DCE': '大商所',
    'CZCE': '郑商所',
    'CFFEX': '中金所',
    'GFEX': '广期所',
}

_RAW_SYMBOLS = [
    # ---- 上期所 SHFE ----
    ('CU', '沪铜', 'SHFE'), ('AL', '沪铝', 'SHFE'), ('ZN', '沪锌', 'SHFE'),
    ('PB', '沪铅', 'SHFE'), ('NI', '沪镍', 'SHFE'), ('SN', '沪锡', 'SHFE'),
    ('AU', '沪金', 'SHFE'), ('AG', '沪银', 'SHFE'), ('RB', '螺纹钢', 'SHFE'),
    ('WR', '线材', 'SHFE'), ('HC', '热卷', 'SHFE'), ('SS', '不锈钢', 'SHFE'),
    ('BU', '沥青', 'SHFE'), ('RU', '天然橡胶', 'SHFE'), ('FU', '燃料油', 'SHFE'),
    ('SP', '纸浆', 'SHFE'), ('AO', '氧化铝', 'SHFE'), ('BR', '丁二烯橡胶', 'SHFE'),
    # ---- 能源中心 INE ----
    ('SC', '原油', 'INE'), ('LU', '低硫燃料油', 'INE'), ('NR', '20号胶', 'INE'),
    ('BC', '国际铜', 'INE'), ('EC', '集运欧线', 'INE'),
    # ---- 大商所 DCE ----
    ('A', '豆一', 'DCE'), ('B', '豆二', 'DCE'), ('C', '玉米', 'DCE'),
    ('CS', '玉米淀粉', 'DCE'), ('M', '豆粕', 'DCE'), ('Y', '豆油', 'DCE'),
    ('P', '棕榈油', 'DCE'), ('FB', '纤维板', 'DCE'), ('BB', '胶合板', 'DCE'),
    ('JD', '鸡蛋', 'DCE'), ('L', '塑料', 'DCE'), ('V', 'PVC', 'DCE'),
    ('PP', '聚丙烯', 'DCE'), ('EG', '乙二醇', 'DCE'), ('EB', '苯乙烯', 'DCE'),
    ('PG', '液化石油气', 'DCE'), ('RR', '粳米', 'DCE'), ('I', '铁矿石', 'DCE'),
    ('J', '焦炭', 'DCE'), ('JM', '焦煤', 'DCE'), ('LH', '生猪', 'DCE'),
    ('LG', '原木', 'DCE'),
    # ---- 郑商所 CZCE ----
    ('SR', '白糖', 'CZCE'), ('CF', '棉花', 'CZCE'), ('TA', 'PTA', 'CZCE'),
    ('MA', '甲醇', 'CZCE'), ('FG', '玻璃', 'CZCE'), ('RM', '菜粕', 'CZCE'),
    ('OI', '菜油', 'CZCE'), ('AP', '苹果', 'CZCE'), ('CJ', '红枣', 'CZCE'),
    ('PK', '花生', 'CZCE'), ('SA', '纯碱', 'CZCE'), ('SF', '硅铁', 'CZCE'),
    ('SM', '锰硅', 'CZCE'), ('UR', '尿素', 'CZCE'), ('PX', '对二甲苯', 'CZCE'),
    ('SH', '烧碱', 'CZCE'), ('PF', '短纤', 'CZCE'), ('RS', '菜籽', 'CZCE'),
    ('CY', '棉纱', 'CZCE'),
    # ---- 中金所 CFFEX ----
    ('IF', '沪深300', 'CFFEX'), ('IC', '中证500', 'CFFEX'),
    ('IH', '上证50', 'CFFEX'), ('IM', '中证1000', 'CFFEX'),
    ('T', '十年国债', 'CFFEX'), ('TF', '五年国债', 'CFFEX'),
    ('TS', '二年国债', 'CFFEX'), ('TL', '三十年国债', 'CFFEX'),
    # ---- 广期所 GFEX ----
    ('SI', '工业硅', 'GFEX'), ('LC', '碳酸锂', 'GFEX'), ('PS', '多晶硅', 'GFEX'),
]

SYMBOLS = {code: SymbolMeta(code, name, exch) for code, name, exch in _RAW_SYMBOLS}


def all_symbols() -> list[SymbolMeta]:
    """返回全部已支持品种（按交易所、代码排序）。"""
    order = {k: i for i, k in enumerate(EXCHANGE_NAMES)}
    return sorted(SYMBOLS.values(), key=lambda s: (order.get(s.exchange, 99), s.code))


def get_symbol(code: str) -> Optional[SymbolMeta]:
    """按品种代码查元数据，大小写不敏感。"""
    if not code:
        return None
    return SYMBOLS.get(code.strip().upper())


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Kline:
    """单品种日线序列（各列等长，按日期升序）。"""
    code: str
    dates: list[str]        # 'YYYY-MM-DD'
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]
    fetched_at: float = 0.0     # unix 时间戳
    stale: bool = False         # True 表示数据来自过期/兜底缓存
    source: str = 'sina'
    live: bool = False          # True 表示最后一根已叠加实时快照（未收盘）

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def last_date(self) -> str:
        return self.dates[-1] if self.dates else ''

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'dates': self.dates,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'fetched_at': self.fetched_at,
            'stale': self.stale,
            'source': self.source,
        }
    @classmethod
    def from_dict(cls, d: dict) -> 'Kline':
        return cls(
            code=d['code'],
            dates=d['dates'], open=d['open'], high=d['high'],
            low=d['low'], close=d['close'], volume=d['volume'],
            fetched_at=float(d.get('fetched_at') or 0),
            stale=bool(d.get('stale')),
            source=d.get('source', 'sina'),
        )


# ============================================================
# 数据提供者
# ============================================================

class SinaDailyProvider:
    """新浪财经主力连续合约日线提供者。"""

    name = 'sina'

    def __init__(self, timeout: int = HTTP_TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def fetch(self, meta: SymbolMeta) -> Kline:
        """拉取单品种全部历史日线。失败时抛 RuntimeError。"""
        url = _KLINE_URL.format(code=meta.sina)
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f'{meta.code} 行情请求失败: {exc}') from exc

        if resp.status_code != 200:
            raise RuntimeError(f'{meta.code} 行情返回 HTTP {resp.status_code}')

        # 新浪返回 GBK 系编码，历史数据里偶有中文注释，统一按 utf-8 容错解码
        resp.encoding = resp.encoding or 'utf-8'
        text = resp.text
        match = _JSONP_RE.search(text.strip())
        if not match:
            raise RuntimeError(f'{meta.code} 行情返回格式异常（非 JSONP）')

        try:
            rows = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'{meta.code} 行情 JSON 解析失败: {exc}') from exc

        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f'{meta.code} 行情返回空数据')

        dates, opens, highs, lows, closes, vols = [], [], [], [], [], []
        for row in rows:
            try:
                dates.append(str(row['d'])[:10])
                opens.append(float(row['o']))
                highs.append(float(row['h']))
                lows.append(float(row['l']))
                closes.append(float(row['c']))
                vols.append(float(row.get('v') or 0))
            except (KeyError, TypeError, ValueError):
                continue  # 单行异常不影响整条序列

        if not dates:
            raise RuntimeError(f'{meta.code} 行情无有效记录')

        return Kline(
            code=meta.code, dates=dates, open=opens, high=highs,
            low=lows, close=closes, volume=vols,
            fetched_at=time.time(), source=self.name,
        )


class SinaRealtimeProvider:
    """新浪实时快照提供者（盘中最新价 / 最高 / 最低 / 成交量）。

    日线接口只在收盘后落库，盘中拿到的是上一交易日；
    而「价格即将突破前高」这类信号必须在盘中就能看到，
    因此用实时快照覆盖/追加当日这根 K 线。
    """

    name = 'sina_realtime'

    def __init__(self, timeout: int = min(HTTP_TIMEOUT, 10)):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def fetch_many(self, codes: Iterable[str]) -> dict[str, dict]:
        """批量取快照，返回 {code: {date, open, high, low, last, volume, ...}}。

        单个批次失败不影响其他批次；全部失败返回空 dict，由调用方降级。
        """
        metas = [get_symbol(c) for c in codes]
        metas = [m for m in metas if m]
        out: dict[str, dict] = {}

        for i in range(0, len(metas), _REALTIME_BATCH):
            chunk = metas[i:i + _REALTIME_BATCH]
            url = _REALTIME_URL.format(codes=','.join(f'nf_{m.sina}' for m in chunk))
            try:
                resp = self._session.get(url, timeout=self.timeout)
                resp.encoding = 'gbk'
                text = resp.text
            except requests.RequestException:
                continue

            for line in text.splitlines():
                parsed = self._parse_line(line)
                if parsed:
                    out[parsed[0]] = parsed[1]
        return out

    @staticmethod
    def _parse_line(line: str):
        """解析单行 `var hq_str_nf_CU0="...";`。"""
        if 'hq_str_nf_' not in line or '="' not in line:
            return None
        try:
            head, rest = line.split('="', 1)
            code = head.split('hq_str_nf_')[-1].strip()
            payload = rest.rstrip('"\n; ').strip('"')
        except ValueError:
            return None
        if not payload:
            return None
        parts = payload.split(',')
        need = max(_RT_IDX.values()) + 1
        if len(parts) < need:
            return None

        def num(key):
            try:
                return float(parts[_RT_IDX[key]])
            except (ValueError, IndexError):
                return None

        date_str = parts[_RT_IDX['date']].strip()
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_str or ''):
            return None

        last, opn, hi, lo = num('last'), num('open'), num('high'), num('low')
        # 集合竞价前或停牌时这些字段可能为 0，视为无效快照
        if not last or last <= 0:
            return None

        base = code[:-1] if code.endswith('0') else code
        return base, {
            'date': date_str,
            'open': opn or last,
            'high': hi or last,
            'low': lo or last,
            'last': last,
            'prev_settle': num('prev_settle'),
            'open_interest': num('open_interest'),
            'volume': num('volume') or 0.0,
        }


def apply_realtime(kline: Kline, rt: dict) -> Kline:
    """把实时快照合并进日线序列（返回新对象，不修改入参）。

    规则：
      - 快照日期等于最后一根 → 覆盖该根（盘中未收盘的当日 K 线）
      - 快照日期晚于最后一根 → 追加一根
      - 其余情况（快照过期、停牌）→ 原样返回
    """
    if not kline or not rt:
        return kline
    d = rt.get('date')
    if not d or not kline.dates:
        return kline

    last = kline.dates[-1]
    if d < last:
        return kline

    bar = (d, float(rt['open']), float(rt['high']),
           float(rt['low']), float(rt['last']), float(rt.get('volume') or 0))

    if d == last:
        # 覆盖：用实时值替换最后一行
        kline.open[-1], kline.high[-1], kline.low[-1] = bar[1], bar[2], bar[3]
        kline.close[-1], kline.volume[-1] = bar[4], bar[5]
    else:
        kline.dates.append(bar[0])
        kline.open.append(bar[1])
        kline.high.append(bar[2])
        kline.low.append(bar[3])
        kline.close.append(bar[4])
        kline.volume.append(bar[5])
    kline.live = True
    return kline


# ============================================================
# 缓存存储
# ============================================================
class KlineStore:
    """带磁盘缓存与并发拉取的行情仓库。

    用法::

        store = KlineStore()
        batch = store.load(['CU', 'RB'])     # -> {code: Kline}
        one   = store.load_one('CU')
    """

    def __init__(self, provider=None, cache_dir: str = CACHE_DIR,
                 ttl: int = CACHE_TTL, max_workers: int = MAX_WORKERS,
                 realtime_provider=None):
        self.provider = provider or SinaDailyProvider()
        self.realtime_provider = realtime_provider or SinaRealtimeProvider()
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.max_workers = max_workers
        self._mem: dict[str, Kline] = {}

    # ---- 缓存路径 ----
    def _path(self, code: str) -> str:
        return os.path.join(self.cache_dir, f'{code.upper()}.json')

    # ---- 读写磁盘缓存 ----
    def _read_disk(self, code: str) -> Optional[Kline]:
        path = self._path(code)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return Kline.from_dict(json.load(fh))
        except (OSError, ValueError, KeyError):
            return None

    def _write_disk(self, kline: Kline) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            # 先写临时文件再原子替换，避免并发读到半截文件
            tmp = self._path(kline.code) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(kline.to_dict(), fh, ensure_ascii=False)
            os.replace(tmp, self._path(kline.code))
        except OSError:
            pass  # 缓存写失败不影响主流程

    def _is_fresh(self, kline: Kline) -> bool:
        age = time.time() - (kline.fetched_at or 0)
        return 0 <= age < self.ttl

    # ---- 日线：单个 ----
    def _load_daily(self, code: str, force: bool = False) -> Optional[Kline]:
        """只取日线（带缓存），不含实时快照。"""
        code = code.strip().upper()
        meta = get_symbol(code)
        if meta is None:
            return None

        if not force:
            cached = self._mem.get(code)
            if cached is not None and self._is_fresh(cached):
                return cached

            disk = self._read_disk(code)
            if disk is not None and self._is_fresh(disk):
                self._mem[code] = disk
                return disk

        try:
            kline = self.provider.fetch(meta)
            self._mem[code] = kline
            self._write_disk(kline)
            return kline
        except Exception:
            # 网络失败：退回任何可用缓存并标记 stale
            fallback = self._mem.get(code) or self._read_disk(code)
            if fallback is not None:
                fallback.stale = True
                self._mem[code] = fallback
                return fallback
            return None

    # ---- 单个品种（日线 + 实时）----
    def load_one(self, code: str, force: bool = False,
                 realtime: bool = True) -> Optional[Kline]:
        """加载单品种，优先内存 → 新鲜磁盘缓存 → 网络 → 过期磁盘缓存。"""
        kline = self._load_daily(code, force)
        if kline is None:
            return None
        if realtime:
            self._overlay_realtime([kline])
        return kline

    # ---- 实时覆盖 ----
    def _overlay_realtime(self, klines: list[Kline]) -> None:
        """批量叠加实时快照（就地修改并回写内存缓存）。"""
        if not klines:
            return
        try:
            snaps = self.realtime_provider.fetch_many([k.code for k in klines])
        except Exception:
            return
        if not snaps:
            return
        for kline in klines:
            snap = snaps.get(kline.code)
            if snap:
                apply_realtime(kline, snap)

    # ---- 批量 ----
    def _load_adjusted(self, code: str, force: bool = False) -> Optional[Kline]:
        """加载**换月复权**后的日线（带磁盘缓存）。

        为什么默认要用复权数据：主力连续在换月处有台阶，实测跳空平均是正常日
        波动的 2 倍（玻璃/焦煤单次可达 15%）。这会让「前高/前低」和所有动量类
        指标都失真 —— 一条 15% 的假跳空足以凭空造出一个"前高"。

        后复权的好处：最新价格保持真实，所以实时快照可以直接叠加，不用换算。
        """
        path = os.path.join(self.cache_dir, 'adjusted', code + '.json')
        if not force and os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as f:
                    d = json.load(f)
                if (time.time() - float(d.get('fetched_at') or 0)) < self.ttl:
                    return Kline.from_dict(d['kline'])
            except Exception:
                pass

        try:
            from contracts import build_adjusted_kline
            kline = build_adjusted_kline(code, ADJUST_START)
        except Exception as exc:
            print(f'  ⚠ {code}: 换月复权构建失败（{type(exc).__name__}: {exc}），该品种本轮跳过')
            kline = None
        if kline is None or len(kline) < 60:
            # 拿不到单合约历史就没法复权。明确提示，避免品种"静默消失"，
            # 让人误以为筛选器漏了品种。
            if kline is not None:
                print(f'  ⚠ {code}: 复权序列只有 {len(kline)} 根，不足 60 根，本轮跳过')
            return None

        kline.fetched_at = time.time()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'kline': kline.to_dict(),
                           'fetched_at': kline.fetched_at}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass
        return kline

    def load(self, codes: Iterable[str], force: bool = False,
             realtime: bool = True, adjusted: bool = False) -> dict[str, Kline]:
        """并发加载多个品种。返回 {code: Kline}，缺数据的不出现在结果里。

        adjusted=True 时返回换月复权序列（推荐），False 返回新浪原始主力连续。
        """
        wanted = []
        seen = set()
        for raw in codes:
            code = raw.strip().upper()
            if code and code not in seen and get_symbol(code):
                seen.add(code)
                wanted.append(code)
        if not wanted:
            return {}

        if adjusted:
            result: dict[str, Kline] = {}
            pending = []
            for code in wanted:
                hit = self._load_adjusted(code, force)
                if hit is not None:
                    result[code] = hit
                else:
                    pending.append(code)
            if pending:
                with ThreadPoolExecutor(max_workers=min(self.max_workers,
                                                        len(pending))) as pool:
                    for code, kline in zip(pending,
                                           pool.map(lambda c: self._load_adjusted(c, force),
                                                    pending)):
                        if kline is not None:
                            result[code] = kline
            if realtime:
                self._overlay_realtime(list(result.values()))
            return result

        # 先收集已经命中缓存的，减少网络请求
        result: dict[str, Kline] = {}
        pending: list[str] = []
        for code in wanted:
            if not force:
                cached = self._mem.get(code)
                if cached is not None and self._is_fresh(cached):
                    result[code] = cached
                    continue
                disk = self._read_disk(code)
                if disk is not None and self._is_fresh(disk):
                    self._mem[code] = disk
                    result[code] = disk
                    continue
            pending.append(code)

        if pending:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(pending))) as pool:
                futures = {pool.submit(self._load_daily, code, force): code
                           for code in pending}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        kline = future.result()
                    except Exception:
                        kline = None
                    if kline is not None:
                        result[code] = kline

        if realtime:
            self._overlay_realtime(list(result.values()))
        return result

    def clear_memory(self) -> None:
        self._mem.clear()



# ============================================================
# 模块级默认实例
# ============================================================

_default_store: Optional[KlineStore] = None


def get_store() -> KlineStore:
    """获取进程级默认 KlineStore（懒加载）。"""
    global _default_store
    if _default_store is None:
        _default_store = KlineStore()
    return _default_store
