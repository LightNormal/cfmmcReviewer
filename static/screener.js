/**
 * 品种筛选 · 前端逻辑
 * ====================
 * 依赖：review.js（提供 fmtNum / fmtColored / setStatus 等通用函数，先加载）
 *       ECharts（review.html 头部已引入）
 *
 * 功能：
 *   1. 视图切换（复盘 / 品种筛选）
 *   2. 筛选参数 → 调用 /api/screener → 渲染结果表
 *   3. 点击行 → 详情弹窗（指标卡片 + 评分构成 + K线/MA/BOLL/MACD 图）
 *   4. 加入 / 移出自选（/api/watchlist）
 */

(function () {
  'use strict';

  // ============================================================
  // 状态
  // ============================================================
  const state = {
    meta: null,
    results: [],
    watchlist: [],
    mySymbols: null,
    initialized: false,
    running: false,
    lastConfig: null,
    chart: null,
    modalCode: null,
    lastAnalysis: null,
  };

  // ============================================================
  // 小工具
  // ============================================================
  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** 数字格式化：缺失返回 '-' */
  function num(v, digits) {
    if (v === null || v === undefined || v === '' || Number.isNaN(v)) return '-';
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    const d = digits === undefined ? 2 : digits;
    return n.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  /** 带符号的百分比 */
  function pct(v, digits) {
    if (v === null || v === undefined) return '-';
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    const d = digits === undefined ? 2 : digits;
    return (n > 0 ? '+' : '') + n.toFixed(d) + '%';
  }

  /** 国内期货习惯：红涨绿跌。故此处不用现有的 positive/negative（那是绿涨红跌）。 */
  function chgCls(v) {
    if (v === null || v === undefined) return 'scr-flat';
    const n = Number(v);
    if (!Number.isFinite(n) || n === 0) return 'scr-flat';
    return n > 0 ? 'scr-up' : 'scr-down';
  }

  async function getJSON(url) {
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  async function postJSON(url, body, method) {
    const res = await fetch(url, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ============================================================
  // 视图切换
  // ============================================================
  function switchView(view) {
    const isScreener = view === 'screener';

    const reviewView = $('viewReview');
    const screenerView = $('viewScreener');
    const toolbar = $('dateToolbar');

    if (reviewView) reviewView.style.display = isScreener ? 'none' : '';
    if (screenerView) screenerView.style.display = isScreener ? '' : 'none';
    if (toolbar) toolbar.style.display = isScreener ? 'none' : '';

    document.querySelectorAll('.view-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });

    if (isScreener) {
      initScreener().then(() => {
        // 首次进入自动跑一次，省掉用户多点一下
        if (!state.results.length && !state.running) runScreen();
      });
    } else if (state.chart) {
      // 切回复盘时销毁图表，避免 resize 报错
      state.chart.resize();
    }
  }

  // ============================================================
  // 初始化
  // ============================================================
  async function initScreener() {
    if (state.initialized) return;
    state.initialized = true;
    bindEvents();
    markLookbackPreset();

    try {
      state.meta = await getJSON('/api/screener/meta');
    } catch (e) {
      renderEmpty('筛选器初始化失败：' + e.message);
      return;
    }

    // 交易所多选框
    const box = $('scrExchanges');
    if (box) {
      box.innerHTML = (state.meta.exchanges || []).map((ex) => `
        <label class="scr-check">
          <input type="checkbox" class="scr-exchange" value="${esc(ex.code)}" checked />
          ${esc(ex.name)}
        </label>`).join('');
    }

    // 数据口径提示
    if ($('scrNote')) $('scrNote').textContent = state.meta.data_note || '';

    loadWatchlist();
  }

  function bindEvents() {
    const prox = $('scrProximity');
    if (prox) {
      prox.addEventListener('input', () => {
        $('scrProximityVal').textContent = Number(prox.value).toFixed(1) + '%';
      });
    }

    if ($('scrRunBtn')) $('scrRunBtn').addEventListener('click', () => runScreen());
    if ($('scrRefreshBtn')) $('scrRefreshBtn').addEventListener('click', refreshData);
    if ($('scrClearRef')) {
      $('scrClearRef').addEventListener('click', () => {
        $('scrRefStart').value = '';
        $('scrRefEnd').value = '';
      });
    }

    // 回看交易日数：可自由输入，预设按钮只是快捷方式
    const lookbackInput = $('scrLookback');
    if (lookbackInput) {
      lookbackInput.addEventListener('input', markLookbackPreset);
    }
    const presets = $('scrLookbackPresets');
    if (presets) {
      presets.addEventListener('click', (e) => {
        const btn = e.target.closest('.scr-preset');
        if (!btn) return;
        if (lookbackInput) lookbackInput.value = btn.dataset.bars;
        markLookbackPreset();
      });
    }

    const tbody = $('scrTbody');
    if (tbody) {
      tbody.addEventListener('click', (e) => {
        const addBtn = e.target.closest('.scr-add');
        if (addBtn) {
          e.stopPropagation();
          addToWatch(addBtn.dataset.code);
          return;
        }
        const row = e.target.closest('tr[data-code]');
        if (row) openDetail(row.dataset.code);
      });
    }

    const wl = $('scrWatchlist');
    if (wl) {
      wl.addEventListener('click', (e) => {
        const rm = e.target.closest('.scr-watch-remove');
        if (rm) { removeFromWatch(rm.dataset.code); return; }
        const card = e.target.closest('.scr-watch-item');
        if (card) openDetail(card.dataset.code);
      });
    }

    if ($('scrModalWatch')) {
      $('scrModalWatch').addEventListener('click', () => {
        if (state.modalCode) addToWatch(state.modalCode);
      });
    }
  }

  /**
   * 回看交易日数现在可以自由输入，预设按钮只是快捷方式。
   * 这里让"当前值正好等于某个预设"时，把对应按钮高亮。
   */
  function markLookbackPreset() {
    const input = $('scrLookback');
    if (!input) return;
    const cur = String(parseInt(input.value, 10));
    document.querySelectorAll('.scr-preset').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.bars === cur);
    });
  }

  // ============================================================
  // 执行筛选
  // ============================================================
  function collectConfig() {
    const exchanges = Array.from(document.querySelectorAll('.scr-exchange'))
      .filter((c) => c.checked).map((c) => c.value);

    const params = new URLSearchParams();
    params.set('proximity', $('scrProximity').value);

    // 回看交易日数：允许任意非负整数，非法/空值回退 250
    let bars = parseInt($('scrLookback').value, 10);
    if (!Number.isFinite(bars) || bars < 0) bars = 250;
    params.set('lookback', String(bars));

    params.set('direction', $('scrDirection').value);
    params.set('min_score', $('scrMinScore').value || '0');
    params.set('require_level', $('scrRequireLevel').checked ? '1' : '0');

    // 全部交易所都勾选时不传，减少 URL 长度
    const all = (state.meta.exchanges || []).map((x) => x.code);
    if (exchanges.length && exchanges.length < all.length) {
      params.set('exchanges', exchanges.join(','));
    }

    const refStart = $('scrRefStart').value;
    const refEnd = $('scrRefEnd').value;
    if (refStart) params.set('ref_start', refStart);
    if (refEnd) params.set('ref_end', refEnd);

    if ($('scrMyOnly').checked && state.mySymbols && state.mySymbols.length) {
      params.set('symbols', state.mySymbols.join(','));
    }
    return params;
  }

  async function runScreen() {
    if (state.running || !state.meta) return;
    state.running = true;

    const btn = $('scrRunBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 筛选中...'; }
    renderEmpty('正在拉取行情并计算指标，首次加载约 3 秒…');

    try {
      // 「只看我交易过的」需要先拿到品种列表
      if ($('scrMyOnly').checked && state.mySymbols === null) {
        try {
          const mine = await getJSON('/api/screener/my-symbols');
          state.mySymbols = mine.symbols || [];
          if (!state.mySymbols.length) {
            alert('本地结算单里还没有品种数据，先查询一次复盘即可。');
            $('scrMyOnly').checked = false;
          }
        } catch (e) {
          state.mySymbols = [];
          $('scrMyOnly').checked = false;
        }
      }

      const params = collectConfig();
      const data = await getJSON('/api/screener?' + params.toString());
      state.results = data.results || [];
      state.lastConfig = data.meta || null;
      renderStats(data);
      renderTable(state.results);
      if ($('scrCount')) {
        $('scrCount').textContent = `（${state.results.length} 个）`;
      }
    } catch (e) {
      console.error('筛选失败', e);
      renderEmpty('筛选失败：' + e.message);
    } finally {
      state.running = false;
      if (btn) { btn.disabled = false; btn.textContent = '🔍 开始筛选'; }
    }
  }

  async function refreshData() {
    const btn = $('scrRefreshBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 刷新中...'; }
    try {
      await postJSON('/api/screener/refresh');
      await runScreen();
    } catch (e) {
      alert('刷新失败：' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新行情'; }
    }
  }

  // ============================================================
  // 渲染
  // ============================================================
  function renderEmpty(msg) {
    const tbody = $('scrTbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="15" class="scr-empty">${esc(msg)}</td></tr>`;
  }

  function renderStats(data) {
    const box = $('scrStats');
    if (!box) return;
    const s = data.stats || {};
    const m = data.meta || {};

    const card = (label, value, cls) =>
      `<div class="scr-stat"><div class="scr-stat-label">${label}</div>
       <div class="scr-stat-value ${cls || ''}">${value}</div></div>`;

    const modeText = m.level_mode === 'range'
      ? `参考区间 ${m.ref_start || '最早'} ~ ${m.ref_end || '最新'}`
      : (m.lookback ? `前 ${m.lookback} 个交易日` : '全部历史');

    box.innerHTML = [
      card('命中品种', s.matched ?? 0),
      card('逼近前高（多头）', s.long ?? 0, 'scr-up'),
      card('逼近前低（空头）', s.short ?? 0, 'scr-down'),
      card('已突破', s.broken ?? 0, 'scr-warn'),
      card('逼近临界', s.approaching ?? 0, 'scr-warn'),
      card('关键点位口径', modeText),
      card('阈值', `±${m.proximity}%`),
      card('耗时', data.cached ? '缓存命中' : `${data.elapsed_ms || 0} ms`),
    ].join('');
  }

  function bollText(item) {
    const rank = item.boll_width_pct_rank;
    if (rank === null || rank === undefined) return '-';
    if (rank <= 20) return '收口';
    if (rank >= 80) return '张口';
    return '常态';
  }

  function macdText(item) {
    if (item.dif === null || item.dea === null) return '-';
    return item.dif > item.dea ? '多头' : '空头';
  }

  function renderTable(rows) {
    const tbody = $('scrTbody');
    if (!tbody) return;

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="15" class="scr-empty">'
        + '没有品种满足当前条件，试试放宽阈值或切换到「全部方向」</td></tr>';
      return;
    }

    const inWatch = new Set((state.watchlist || []).map((w) => w.code));

    tbody.innerHTML = rows.map((r) => {
      const isLong = r.direction === 'long';
      const dist = isLong ? r.dist_to_high_pct : r.dist_to_low_pct;
      const pillCls = r.setup === 'broken' ? 'scr-pill-broken'
        : (isLong ? 'scr-pill-long' : 'scr-pill-short');
      const added = inWatch.has(r.code);
      // 已突破：向上破 → 红，向下破 → 绿
      const distCls = (dist !== null && dist !== undefined && dist < 0)
        ? (isLong ? 'scr-up' : 'scr-down') : 'scr-flat';

      return `<tr data-code="${esc(r.code)}" title="${esc((r.reasons || []).join(' · '))}">
        <td class="scr-sym">
          <span class="scr-name">${esc(r.name)}</span>
          <span class="scr-code">${esc(r.code)}</span>
        </td>
        <td class="scr-ex">${esc(r.exchange_name)}</td>
        <td class="num">${num(r.close)}</td>
        <td class="num ${chgCls(r.change_pct)}">${pct(r.change_pct)}</td>
        <td><span class="scr-pill ${pillCls}">${esc(r.signal)}</span></td>
        <td class="num">
          ${num(r.level_price)}
          <small class="scr-sub">${esc(r.level_date || '')}</small>
        </td>
        <td class="num ${distCls}">${pct(dist)}</td>
        <td class="num">${r.range_pos === null || r.range_pos === undefined ? '-' : num(r.range_pos, 0) + '%'}</td>
        <td class="scr-trend" title="${esc(r.ma_alignment || '')}">${esc(r.ma_alignment || '-')}</td>
        <td class="${macdText(r) === '多头' ? 'scr-up' : 'scr-down'}">${macdText(r)}</td>
        <td>${bollText(r)}</td>
        <td class="num">${num(r.vol_ratio)}</td>
        <td class="num">${num(r.rsi, 1)}</td>
        <td>
          <div class="scr-score" title="关键点位 ${r.scores.level} · 动量 ${r.scores.momentum || 0} · 均线 ${r.scores.trend} · MACD ${r.scores.macd} · 布林 ${r.scores.boll} · 量能 ${r.scores.volume}">
            <div class="scr-score-bar"><span style="width:${Math.min(100, r.score)}%"></span></div>
            <b>${num(r.score, 1)}</b>
          </div>
        </td>
        <td>
          <button class="btn btn-outline btn-sm scr-add" data-code="${esc(r.code)}" ${added ? 'disabled' : ''}>
            ${added ? '✓ 已自选' : '+ 自选'}
          </button>
        </td>
      </tr>`;
    }).join('');
  }

  // ============================================================
  // 自选
  // ============================================================
  async function loadWatchlist() {
    try {
      const data = await getJSON('/api/watchlist');
      state.watchlist = data.items || [];
    } catch (e) {
      state.watchlist = [];
    }
    renderWatchlist();
  }

  function renderWatchlist() {
    const box = $('scrWatchlist');
    if (!box) return;
    const items = state.watchlist || [];
    if ($('scrWatchCount')) $('scrWatchCount').textContent = `（${items.length} 个）`;

    if (!items.length) {
      box.innerHTML = '<div class="scr-empty">暂无自选品种，在筛选结果里点「+ 自选」添加</div>';
      return;
    }

    box.innerHTML = items.map((w) => `
      <div class="scr-watch-item" data-code="${esc(w.code)}">
        <div class="scr-watch-main">
          <span class="scr-name">${esc(w.name || w.code)}</span>
          <span class="scr-code">${esc(w.code)}</span>
        </div>
        <div class="scr-watch-meta">${esc(w.added_at || '')}</div>
        <div class="scr-watch-actions">
          <button class="btn btn-outline btn-sm scr-open" data-code="${esc(w.code)}">详情</button>
          <button class="btn btn-outline btn-sm scr-watch-remove" data-code="${esc(w.code)}">移除</button>
        </div>
      </div>`).join('');
  }

  async function addToWatch(code) {
    try {
      const data = await postJSON('/api/watchlist', { code });
      state.watchlist = data.items || [];
      renderWatchlist();
      renderTable(state.results);
      if ($('scrModalWatch')) {
        $('scrModalWatch').textContent = '✓ 已在自选';
        $('scrModalWatch').disabled = true;
      }
    } catch (e) {
      alert('加入自选失败：' + e.message);
    }
  }

  async function removeFromWatch(code) {
    try {
      const data = await postJSON('/api/watchlist/' + encodeURIComponent(code), undefined, 'DELETE');
      state.watchlist = data.items || [];
      renderWatchlist();
      renderTable(state.results);
    } catch (e) {
      alert('移出自选失败：' + e.message);
    }
  }

  // ============================================================
  // 详情弹窗
  // ============================================================
  async function openDetail(code) {
    const modal = $('scrModal');
    if (!modal) return;
    state.modalCode = code;

    if ($('scrModalTitle')) $('scrModalTitle').textContent = code + ' 加载中…';
    if ($('scrModalStats')) $('scrModalStats').innerHTML = '';
    if ($('scrScoreBars')) $('scrScoreBars').innerHTML = '';
    if ($('scrReasons')) $('scrReasons').innerHTML = '';
    if ($('scrChart')) $('scrChart').innerHTML = '<div class="scr-empty">正在加载行情…</div>';
    modal.style.display = 'flex';

    const inWatch = (state.watchlist || []).some((w) => w.code === code);
    if ($('scrModalWatch')) {
      $('scrModalWatch').textContent = inWatch ? '✓ 已在自选' : '⭐ 加自选';
      $('scrModalWatch').disabled = inWatch;
    }

    try {
      const q = new URLSearchParams();
      if ($('scrLookback')) q.set('lookback', $('scrLookback').value);
      if ($('scrRefStart') && $('scrRefStart').value) q.set('ref_start', $('scrRefStart').value);
      if ($('scrRefEnd') && $('scrRefEnd').value) q.set('ref_end', $('scrRefEnd').value);

      const data = await getJSON(
        '/api/screener/detail/' + encodeURIComponent(code) + '?' + q.toString());

      renderDetail(data);
    } catch (e) {
      if ($('scrChart')) {
        $('scrChart').innerHTML = `<div class="scr-empty">加载失败：${esc(e.message)}</div>`;
      }
    }
  }

  function renderDetail(data) {
    const a = data.analysis || {};
    const l = a.latest || {};
    const snap = data.screen_snapshot || {};

    if ($('scrModalTitle')) {
      $('scrModalTitle').textContent =
        `${data.name}（${data.code}）· ${data.exchange_name} · ${l.date || ''}`;
    }

    const statCard = (label, value, cls) =>
      `<div class="card"><div class="card-label">${label}</div>
       <div class="card-value ${cls || ''}">${value}</div></div>`;

    const cards = [
      statCard('最新价', num(l.close)),
      statCard('涨跌', pct(l.change_pct), chgCls(l.change_pct)),
      statCard('距前高', pct(l.dist_to_high_pct),
        (l.dist_to_high_pct < 0 ? 'scr-up' : 'scr-flat')),
      statCard('距前低', pct(l.dist_to_low_pct),
        (l.dist_to_low_pct < 0 ? 'scr-down' : 'scr-flat')),
      statCard('前高 / 日期', `${num(l.level_high)}<br><small>${esc(l.level_high_date || '')}</small>`),
      statCard('前低 / 日期', `${num(l.level_low)}<br><small>${esc(l.level_low_date || '')}</small>`),
      statCard('区间位置', l.range_pos === null ? '-' : num(l.range_pos, 0) + '%'),
      statCard('均线排列', esc(l.ma_alignment || '-')),
      statCard('RSI(14)', `${num(l.rsi, 1)}<br><small>${esc(l.rsi_state || '')}</small>`),
      statCard('MACD 柱', num(l.macd, 2), chgCls(l.macd)),
      statCard('布林 %B', num(l.boll_pctb, 2)),
      statCard('量比', num(l.vol_ratio)),
      statCard('ATR%', l.atr_pct === null ? '-' : num(l.atr_pct, 2) + '%'),
      statCard('数据样本', `${a.bars || 0} 根<br><small>${a.live ? '含实时' : '收盘价'}</small>`),
    ].join('');
    if ($('scrModalStats')) $('scrModalStats').innerHTML = cards;

    // 评分构成（权重与 screener.DEFAULT_WEIGHTS 保持一致，经回测校准的 v2）
    const scores = snap.scores;
    if ($('scrScoreBars') && scores) {
      const weights = { level: 28, momentum: 30, trend: 14, macd: 10.5, boll: 10.5, volume: 7 };
      const names = { level: '关键点位', momentum: '20日动量', trend: '均线结构',
                      macd: 'MACD', boll: '布林带', volume: '量能' };
      $('scrScoreBars').innerHTML = Object.keys(weights).map((k) => {
        const got = scores[k] || 0;
        const max = weights[k];
        return `<div class="scr-sb-row">
          <span class="scr-sb-name">${names[k]}</span>
          <div class="scr-sb-track"><span style="width:${Math.min(100, got / max * 100)}%"></span></div>
          <span class="scr-sb-val">${num(got, 1)} / ${max}</span>
        </div>`;
      }).join('') + `<div class="scr-sb-total">综合评分 <b>${num(snap.score, 1)}</b> · ${esc(snap.signal || '')}</div>`;
    } else if ($('scrScoreBars')) {
      $('scrScoreBars').innerHTML = '<div class="scr-empty">无评分数据</div>';
    }

    // 理由
    if ($('scrReasons')) {
      const reasons = (snap.reasons || []);
      $('scrReasons').innerHTML = reasons.length
        ? reasons.map((x) => `<span class="scr-reason">${esc(x)}</span>`).join('')
        : '<div class="scr-empty">无</div>';
    }

    renderChart(a);
  }

  // ============================================================
  // 图表
  // ============================================================
  function renderChart(analysis) {
    const T = chartTheme();
    // 缓存最近一次数据，主题切换时用新配色重绘
    state.lastAnalysis = analysis;
    const el = $('scrChart');
    if (!el) return;
    const s = analysis.series || {};
    if (!s.dates || !s.dates.length) {
      el.innerHTML = '<div class="scr-empty">无行情序列</div>';
      return;
    }
    if (typeof echarts === 'undefined') {
      el.innerHTML = '<div class="scr-empty">ECharts 未加载（请检查网络能否访问 CDN）</div>';
      return;
    }

    el.innerHTML = '';
    if (state.chart) {
      try { state.chart.dispose(); } catch (e) { /* ignore */ }
      state.chart = null;
    }
    state.chart = echarts.init(el, null, { renderer: 'canvas' });

    const dates = s.dates;
    const ohlc = dates.map((_, i) => [s.open[i], s.close[i], s.low[i], s.high[i]]);
    const maNames = Object.keys(s.ma || {});
    const maColors = { '5': '#f59e0b', '10': '#38bdf8', '20': '#a855f7', '60': T.text };

    const series = [{
      name: 'K线', type: 'candlestick', data: ohlc,
      itemStyle: {
        color: '#ef4444', color0: '#22c55e',
        borderColor: '#ef4444', borderColor0: '#22c55e',
      },
    }];

    maNames.forEach((p) => {
      series.push({
        name: 'MA' + p, type: 'line', data: s.ma[p], smooth: true, symbol: 'none',
        lineStyle: { width: 1.2, color: maColors[p] || T.muted },
        itemStyle: { color: maColors[p] || T.muted },
      });
    });

    series.push({
      name: 'BOLL上轨', type: 'line', data: s.boll_upper, symbol: 'none',
      lineStyle: { width: 1, type: 'dashed', color: '#f472b6' }, itemStyle: { color: '#f472b6' },
    }, {
      name: 'BOLL下轨', type: 'line', data: s.boll_lower, symbol: 'none',
      lineStyle: { width: 1, type: 'dashed', color: '#f472b6' }, itemStyle: { color: '#f472b6' },
    }, {
      name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: s.volume,
      itemStyle: {
        color: (p) => {
          const i = p.dataIndex;
          return (s.close[i] >= s.open[i]) ? 'rgba(239,68,68,.65)' : 'rgba(34,197,94,.65)';
        },
      },
    }, {
      name: 'MACD', type: 'bar', xAxisIndex: 2, yAxisIndex: 2, data: s.macd,
      itemStyle: {
        color: (p) => (p.value >= 0 ? 'rgba(239,68,68,.75)' : 'rgba(34,197,94,.75)'),
      },
    }, {
      name: 'DIF', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: s.dif,
      symbol: 'none', lineStyle: { width: 1.2, color: '#f59e0b' }, itemStyle: { color: '#f59e0b' },
    }, {
      name: 'DEA', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: s.dea,
      symbol: 'none', lineStyle: { width: 1.2, color: '#38bdf8' }, itemStyle: { color: '#38bdf8' },
    });

    // 关键点位水平线
    const markLines = [];
    if (s.level_high) {
      markLines.push({
        yAxis: s.level_high,
        lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.4 },
        label: { formatter: '前高 ' + s.level_high, color: '#f59e0b', position: 'insideEndTop', fontSize: 10 },
      });
    }
    if (s.level_low) {
      markLines.push({
        yAxis: s.level_low,
        lineStyle: { color: '#38bdf8', type: 'dashed', width: 1.4 },
        label: { formatter: '前低 ' + s.level_low, color: '#38bdf8', position: 'insideEndBottom', fontSize: 10 },
      });
    }
    if (markLines.length) {
      series[0].markLine = { symbol: 'none', silent: true, data: markLines };
    }

    const axisLabel = { color: T.text, fontSize: 10 };
    const splitLine = { lineStyle: { color: T.split } };

    const option = {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }] },
        backgroundColor: T.tooltipBg,
        borderColor: T.tooltipBorder,
        textStyle: { color: T.textStrong, fontSize: 12 },
      },
      legend: {
        top: 0, textStyle: { color: T.text, fontSize: 11 },
        data: ['MA5', 'MA10', 'MA20', 'MA60', 'BOLL上轨', 'BOLL下轨'],
      },
      grid: [
        { left: 64, right: 64, top: 28, height: '46%' },
        { left: 64, right: 64, top: '58%', height: '13%' },
        { left: 64, right: 64, top: '76%', height: '15%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, boundaryGap: true,
          axisLine: { lineStyle: { color: T.axis } }, axisLabel: { show: false } },
        { type: 'category', data: dates, gridIndex: 1, boundaryGap: true,
          axisLine: { lineStyle: { color: T.axis } }, axisLabel: { show: false } },
        { type: 'category', data: dates, gridIndex: 2, boundaryGap: true,
          axisLine: { lineStyle: { color: T.axis } }, axisLabel: axisLabel },
      ],
      yAxis: [
        { scale: true, gridIndex: 0, splitLine: splitLine, axisLabel: axisLabel,
          axisLine: { lineStyle: { color: T.axis } } },
        { scale: true, gridIndex: 1, splitNumber: 2, splitLine: { show: false },
          axisLabel: axisLabel },
        { scale: true, gridIndex: 2, splitNumber: 2, splitLine: { show: false },
          axisLabel: axisLabel },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1, 2], start: 62, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 2, height: 16,
          start: 62, end: 100, textStyle: { color: T.text, fontSize: 10 } },
      ],
      series: series,
    };

    state.chart.setOption(option);
    window.addEventListener('resize', resizeChart);
    setTimeout(() => state.chart && state.chart.resize(), 60);
  }

  function resizeChart() {
    if (state.chart) state.chart.resize();
  }

  function closeScreenerModal() {
    const modal = $('scrModal');
    if (modal) modal.style.display = 'none';
    if (state.chart) {
      try { state.chart.dispose(); } catch (e) { /* ignore */ }
      state.chart = null;
    }
    window.removeEventListener('resize', resizeChart);
    state.modalCode = null;
  }

  /** 供 review.js 的主题切换调用：弹窗开着时按新配色重绘 K 线图 */
  function redrawScreenerChart() {
    if (state.chart && state.lastAnalysis) {
      try { renderChart(state.lastAnalysis); } catch (e) { /* ignore */ }
    }
  }
  window.redrawScreenerChart = redrawScreenerChart;

  // ============================================================
  // 全局暴露（HTML 内联 onclick 使用）
  // ============================================================
  window.closeScreenerModal = closeScreenerModal;
  window.switchView = switchView;
  window.runScreen = runScreen;

  document.addEventListener('click', (e) => {
    if (e.target.id === 'scrModal') closeScreenerModal();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('scrModal') && $('scrModal').style.display === 'flex') {
      closeScreenerModal();
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.view-btn').forEach((btn) => {
      btn.addEventListener('click', () => switchView(btn.dataset.view));
    });
  });
})();
