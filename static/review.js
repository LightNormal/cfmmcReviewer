/**
 * 期货交易复盘系统 - 前端逻辑 (v3.0)
 * =======================================
 * 功能:
 *   1. 用户登录（验证码）
 *   2. 页面加载时自动获取日期范围并填充
 *   3. 查询指定日期范围的复盘数据（带进度提示）
 *   4. 渲染总览卡片、品种表格、TOP交易、图表
 *   5. 点击品种名称弹出详情 Modal
 */

// ============================================================
// 全局状态
// ============================================================
const API = '';
let _fullDateRange = null;
let _isQuerying = false;
let _isLoggedIn = false;

// ============================================================
// 工具函数
// ============================================================

/** 获取今天的 YYYY-MM-DD 字符串 */
function getTodayStr() {
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
}

function fmtNum(n, decimals = 2) {
    if (n === null || n === undefined || n === 'N/A' || n === '∞') return n ?? '-';
    const num = typeof n === 'string' ? parseFloat(n) : n;
    if (isNaN(num)) return n;
    const fixed = num.toFixed(decimals);
    const [int, dec] = fixed.split('.');
    const formatted = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return dec ? `${formatted}.${dec}` : formatted;
}

function fmtColored(val, decimals = 2) {
    const n = typeof val === 'string' ? parseFloat(val) : val;
    if (isNaN(n)) return { text: val ?? '-', cls: '' };
    const cls = n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral';
    const prefix = n > 0 ? '+' : '';
    return { text: prefix + fmtNum(n, decimals), cls };
}

function shortDate(d) {
    if (!d) return '-';
    return d.substring(5);
}

// ============================================================
// 主题（亮色 / 暗色）
// ============================================================
// 说明：页面配色全部走 CSS 变量，切换 data-theme 即可；
// 但 ECharts 的坐标轴/提示框颜色是写在 option 里的（画在 canvas 上），
// 不随 CSS 变，所以主题切换后必须用新配色重绘图表。
const THEME_KEY = 'cfmmc-theme';

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

/** 从当前主题的 CSS 变量里取图表要用的颜色 */
function chartTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback;
    const light = currentTheme() === 'light';
    return {
        text: v('--text-dim', light ? '#475569' : '#94a3b8'),
        textStrong: v('--text', light ? '#0f172a' : '#e2e8f0'),
        axis: v('--chart-axis', light ? '#cbd5e1' : '#334155'),
        split: v('--chart-split', light ? 'rgba(148,163,184,.35)' : 'rgba(51,65,85,.55)'),
        muted: v('--chart-muted', light ? '#94a3b8' : '#64748b'),
        tooltipBg: v('--chart-tooltip-bg', light ? 'rgba(255,255,255,.98)' : 'rgba(15,23,42,.95)'),
        tooltipBorder: v('--chart-tooltip-border', light ? '#cbd5e1' : '#334155'),
        // 序列主色（两种主题通用，亮色下略微加深以保证对比度）
        blue: light ? '#2563eb' : '#3b82f6',
        green: light ? '#16a34a' : '#22c55e',
        red: light ? '#dc2626' : '#ef4444',
        orange: light ? '#d97706' : '#f59e0b',
        purple: light ? '#7c3aed' : '#a855f7',
    };
}

function updateThemeButton(theme) {
    const icon = document.getElementById('themeIcon');
    const label = document.getElementById('themeLabel');
    const btn = document.getElementById('themeBtn');
    if (icon) icon.textContent = theme === 'light' ? '☀️' : '🌙';
    if (label) label.textContent = theme === 'light' ? '亮色' : '暗色';
    if (btn) btn.title = theme === 'light' ? '当前亮色，点击切换到暗色' : '当前暗色，点击切换到亮色';
}

function applyTheme(theme, persist) {
    const t = theme === 'light' ? 'light' : 'dark';
    if (t === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    if (persist) {
        try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* 隐私模式忽略 */ }
    }
    updateThemeButton(t);
    // 主题变了，图表要按新配色重绘
    redrawChartsForTheme();
}

function toggleTheme() {
    applyTheme(currentTheme() === 'light' ? 'dark' : 'light', true);
}

function redrawChartsForTheme() {
    try { if (typeof redrawReviewCharts === 'function') redrawReviewCharts(); } catch (e) { /* ignore */ }
    try { if (typeof redrawScreenerChart === 'function') redrawScreenerChart(); } catch (e) { /* ignore */ }
}

// ============================================================
// 进度遮罩控制
// ============================================================

function showProgress(title, desc) {
    document.getElementById('progressTitle').textContent = title || '正在处理...';
    document.getElementById('progressDesc').textContent = desc || '准备中';
    document.getElementById('progressBar').style.width = '0%';
    document.getElementById('progressDetail').textContent = '';
    for (let i = 1; i <= 3; i++) {
        const step = document.getElementById(`step${i}`);
        step.classList.remove('active', 'done');
        document.getElementById(`step${i}Status`).textContent = '';
    }
    document.getElementById('progressOverlay').style.display = 'flex';
}

function hideProgress() {
    document.getElementById('progressOverlay').style.display = 'none';
}

function activateStep(stepNum) {
    document.getElementById(`step${stepNum}`).classList.add('active');
}

function completeStep(stepNum, statusText) {
    const step = document.getElementById(`step${stepNum}`);
    step.classList.remove('active');
    step.classList.add('done');
    if (statusText) document.getElementById(`step${stepNum}Status`).textContent = statusText;
}

function updateProgress(percent, detail) {
    document.getElementById('progressBar').style.width = percent + '%';
    if (detail !== undefined) document.getElementById('progressDetail').textContent = detail;
}

// ============================================================
// API 调用
// ============================================================

async function apiGet(url) {
    try {
        const res = await fetch(API + url);
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        return await res.json();
    } catch (err) {
        console.error('API Error:', url, err);
        throw err;
    }
}

async function apiPost(url, body) {
    try {
        const res = await fetch(API + url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        // 对于登录相关接口，400 可能是业务错误（如验证码错误），仍然尝试解析 JSON
        const data = await res.json();
        if (!res.ok && !data.error) {
            throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        }
        return data;
    } catch (err) {
        console.error('API POST Error:', url, err);
        throw err;
    }
}

// ============================================================
// 登录系统
// ============================================================

/** 检查登录状态 */
async function checkLoginStatus() {
    try {
        const data = await apiGet('/api/login/status');
        _isLoggedIn = data.logged_in;
        updateLoginUI(data);
        return data;
    } catch (e) {
        console.error('检查登录状态失败:', e);
        return { logged_in: false };
    }
}

/** 显示账号信息（未登录但有配置时也能显示） */
function showAccountInfo(accountInfo) {
    const loginInfo = document.getElementById('loginInfo');
    const name = accountInfo.fund_name || accountInfo.account_no || '';
    if (name) {
        loginInfo.textContent = `📋 ${name}${accountInfo.broker ? ' (' + accountInfo.broker + ')' : ''}`;
        loginInfo.style.color = 'var(--text-dimmer)';
    }
}

/** 更新登录按钮和信息显示 */
function updateLoginUI(status) {
    const loginBtn = document.getElementById('loginBtn');
    const logoutBtn = document.getElementById('logoutBtn');
    const loginInfo = document.getElementById('loginInfo');

    if (status && status.logged_in) {
        loginBtn.style.display = 'none';
        logoutBtn.style.display = 'inline-flex';
        loginInfo.textContent = `✓ ${status.account_info?.fund_name || status.account_info?.account_no || '已登录'}`;
        loginInfo.style.color = 'var(--green)';
        _isLoggedIn = true;
    } else {
        loginBtn.style.display = 'inline-flex';
        logoutBtn.style.display = 'none';
        // 未登录时不清空账号信息（保留 config.json / 环境变量中读取的账号显示）
        _isLoggedIn = false;
    }
}

/** 点击登录按钮 → 打开登录模态框 */
async function handleLoginClick() {
    const modal = document.getElementById('loginModal');
    const errorEl = document.getElementById('loginError');

    modal.style.display = 'flex';
    errorEl.textContent = '';

    // 重置验证码区域到初始占位状态
    document.getElementById('vericodeInput').value = '';
    document.getElementById('vericodeInput').disabled = true;
    document.getElementById('vericodeImg').src = '';
    document.getElementById('vericodeImg').style.display = 'none';
    document.getElementById('refreshVeriBtn').style.display = 'none';
    document.getElementById('loginSubmitBtn').style.display = 'none';
    const placeholder = document.getElementById('vericodePlaceholder');
    placeholder.style.display = '';
    placeholder.innerHTML = '<span class="veri-placeholder-icon">📷</span><span class="veri-placeholder-text">点击获取验证码</span>';

    // 聚焦到账号输入框
    setTimeout(() => document.getElementById('loginAccountNo').focus(), 300);
}

/**
 * 获取验证码（用户点击占位区域后调用）
 * 读取账号密码输入框的值，发送给后端获取验证码图片
 */
async function fetchVericode() {
    const accountNo = document.getElementById('loginAccountNo').value.trim();
    const password = document.getElementById('loginPassword').value.trim();
    const errorEl = document.getElementById('loginError');

    errorEl.textContent = '';

    // 构造请求体（如果用户填了账号密码就传给后端，否则后端用 config.json 的）
    const body = {};
    if (accountNo) body.account_no = accountNo;
    if (password) body.password = password;

    try {
        // 显示加载中状态
        const placeholder = document.getElementById('vericodePlaceholder');
        placeholder.innerHTML = '<span class="veri-placeholder-icon">⏳</span><span class="veri-placeholder-text">正在获取...</span>';

        const data = await apiPost('/api/init-login', body);

        if (data.error) {
            placeholder.innerHTML = '<span class="veri-placeholder-icon">❌</span><span class="veri-placeholder-text">获取失败，点击重试</span>';
            errorEl.textContent = data.error;
            return;
        }

        // 隐藏占位符，显示验证码图片和刷新按钮
        placeholder.style.display = 'none';
        const img = document.getElementById('vericodeImg');
        img.src = data.vericode_image;
        img.style.display = '';
        document.getElementById('refreshVeriBtn').style.display = '';

        // 启用验证码输入框
        document.getElementById('vericodeInput').disabled = false;

        // 显示登录按钮
        document.getElementById('loginSubmitBtn').style.display = 'inline-flex';

        // 自动聚焦到验证码输入框
        setTimeout(() => document.getElementById('vericodeInput').focus(), 300);

    } catch (e) {
        const placeholder = document.getElementById('vericodePlaceholder');
        placeholder.innerHTML = '<span class="veri-placeholder-icon">❌</span><span class="veri-placeholder-text">网络错误，点击重试</span>';
        errorEl.textContent = '初始化登录失败: ' + e.message;
    }
}

/** 刷新验证码 */
async function refreshVericode() {
    const img = document.getElementById('vericodeImg');
    // 旋转动画
    img.style.opacity = '0.5';
    try {
        const data = await apiGet('/api/vericode');
        if (data.image) img.src = data.image;
    } catch (e) {
        console.warn('刷新验证码失败:', e);
    }
    img.style.opacity = '1';
}

/** 关闭登录模态框 */
function closeLoginModal() {
    document.getElementById('loginModal').style.display = 'none';
}

/** 提交登录 */
async function submitLogin() {
    const vericode = document.getElementById('vericodeInput').value.trim();
    const errorEl = document.getElementById('loginError');
    const submitBtn = document.getElementById('loginSubmitBtn');

    if (!vericode) {
        errorEl.textContent = '请输入验证码';
        return;
    }

    errorEl.textContent = '';
    submitBtn.disabled = true;
    submitBtn.textContent = '⏳ 登录中...';

    try {
        const data = await apiPost('/api/login', { vericode });

        if (data.error) {
            errorEl.textContent = data.error;
            // 如果需要刷新验证码
            if (data.need_refresh) {
                refreshVericode();
                document.getElementById('vericodeInput').value = '';
                document.getElementById('vericodeInput').focus();
            }
            submitBtn.disabled = false;
            submitBtn.textContent = '确认登录';
            return;
        }

        // 登录成功
        console.log('登录成功:', data);
        closeLoginModal();

        // 更新 UI
        await checkLoginStatus();

        // 如果当前有日期范围，自动执行一次查询
        const start = document.getElementById('startDate').value;
        const end = document.getElementById('endDate').value;
        if (start && end) {
            doQuery();
        }

    } catch (e) {
        errorEl.textContent = '登录请求异常: ' + e.message;
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = '确认登录';
    }
}

/** 登出 */
async function doLogout() {
    try {
        await apiPost('/api/logout');
        updateLoginUI({ logged_in: false });
        console.log('已登出');
    } catch (e) {
        console.error('登出失败:', e);
    }
}

// 点击遮罩关闭登录框
document.addEventListener('click', e => {
    if (e.target.id === 'loginModal') closeLoginModal();
});

// ============================================================
// 重载数据（下载完新结算单后不用重启服务）
// ============================================================

async function reloadData() {
    const btn = document.getElementById('reloadBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 重载中...'; }
    try {
        const res = await fetch(API + '/api/reload', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        const r = data.date_range || {};
        setStatus('ready', `已重载 ${data.total_days} 个交易日 (${r.start || '-'} ~ ${r.end || '-'})`);
        // 重新填充日期范围并自动查询一次
        await initDates();
    } catch (e) {
        console.error('重载失败:', e);
        setStatus('error', '重载失败');
        alert('重载失败：' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔄 重载数据'; }
    }
}

// ============================================================
// 初始化：检查登录状态 + 获取日期范围
// ============================================================

async function initDates() {
    setStatus('loading', '正在加载数据...');

    // 并行执行：检查登录状态 + 获取日期数据
    const [loginData, datesData] = await Promise.allSettled([
        checkLoginStatus(),
        apiGet('/api/dates'),
    ]);

    // 处理日期数据
    if (datesData.status === 'fulfilled') {
        const data = datesData.value;
        const { date_range, available_dates, logged_in, has_data, output_dir_exists, account_info } = data;

        // 显示账号信息（无论是否已登录）
        if (account_info) {
            showAccountInfo(account_info);
        }

        if (date_range.start && date_range.end) {
            _fullDateRange = { start: date_range.start, end: date_range.end };
            window._fullDateRange = _fullDateRange;

            const today = getTodayStr();
            const sEl = document.getElementById('startDate');
            const eEl = document.getElementById('endDate');
            // 结束日期不超过今天
            sEl.value = date_range.start;
            sEl.setAttribute('data-full', date_range.start);
            eEl.value = date_range.end > today ? today : date_range.end;
            eEl.setAttribute('data-full', date_range.end > today ? today : date_range.end);

            // 限制结束日期 input 的 max 为今天
            eEl.max = today;

            setStatus('ready', `已加载 ${available_dates.length} 个交易日 (${date_range.start} ~ ${eEl.value})`);

            // 有存量数据就自动查询（无需登录）
            doQuery();
        } else {
            // 没有本地数据 — 设置默认日期为今天和一个月前
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
            const monthAgo = new Date(now.getFullYear(), now.getMonth()-1, now.getDate());

            const todayStr = fmt(now);
            document.getElementById('startDate').value = fmt(monthAgo);
            document.getElementById('endDate').value = todayStr;
            document.getElementById('endDate').max = todayStr;

            if (!output_dir_exists) {
                setStatus('error', `输出目录不存在 (${data.output_dir || '-'})，请先登录下载数据`);
            } else if (!has_data) {
                setStatus('ready', '输出目录为空，请登录后下载交易数据');
            } else {
                setStatus('ready', '暂无本地数据，请选择日期后查询（登录后可自动下载）');
            }

            // 无数据时才弹出登录框
            if (!_isLoggedIn && !has_data) {
                setTimeout(() => handleLoginClick(), 500);
            }
        }
    } else {
        setStatus('error', '加载数据失败: ' + datesData.reason?.message);
    }
}

// ============================================================
// 状态指示器
// ============================================================

function setStatus(state, text) {
    document.getElementById('dataStatus').className = 'status-dot ' + state;
    document.getElementById('statusText').textContent = text;
}

// ============================================================
// 主查询（带进度遮罩）
// ============================================================

async function doQuery() {
    if (_isQuerying) {
        console.log('查询进行中，忽略重复请求');
        return;
    }

    const start = document.getElementById('startDate').value;
    const end = document.getElementById('endDate').value;

    if (!start || !end) {
        alert('请选择日期范围');
        return;
    }

    _isQuerying = true;
    const queryBtn = document.getElementById('queryBtn');
    queryBtn.disabled = true;
    queryBtn.textContent = '⏳ 查询中...';

    showProgress(
        '正在复盘查询',
        `正在查询 ${start} ~ ${end} 的交易数据`
    );

    activateStep(1);
    updateProgress(10, '检查数据完整性...');

    try {
        updateProgress(15, '正在请求后端分析...');
        const data = await apiGet(`/api/query?start=${start}&end=${end}`);

        // Step 1 完成
        // 「缺失」可能包含三类：真缺口 / 早于已有数据 / 晚于已有数据。
        // 这里只报最需要关注的真实缺口，明细交给下方的缺口诊断面板。
        const md = data.missing_detail || {};
        const gaps = (md.gaps || []).length;
        const pending = (data.need_download_count || 0);
        if (gaps > 0) {
            completeStep(1, data.did_auto_download
                ? `⬇ 已尝试补下载，仍有真实缺口 ${gaps} 天`
                : `⚠ 真实缺口 ${gaps} 天`);
        } else if (pending > 0) {
            completeStep(1, `✓ 无中间缺口，待补新交易日 ${pending} 天`);
        } else {
            completeStep(1, '✓ 数据完整');
        }

        // Step 2: 分析 & 渲染
        activateStep(2);
        updateProgress(50, '正在分析交易数据...');

        // 保存一份供「展开/收起」时重绘缺口面板
        window._lastQueryData = data;
        renderAll(data);

        updateProgress(70, '正在渲染结果...');

        // Step 3: 图表
        completeStep(2, '✓ 分析完成');
        activateStep(3);
        updateProgress(85, '正在生成图表...');

        await loadCharts(start, end);

        completeStep(3, '✓ 完成');
        updateProgress(100, `共 ${data.overview['交易日数']} 个交易日, ${data.overview['品种数']} 个品种`);

        setTimeout(() => {
            hideProgress();
            const nd = (data.need_download_count || 0);
            const gaps = ((data.missing_detail || {}).gaps || []).length;
            let statusText = `查询完成 · ${data.overview['交易日数']} 个交易日`;
            if (gaps > 0) {
                statusText += `（真实缺口 ${gaps} 天）`;
            } else if (nd > 0) {
                statusText += `（${nd} 天待补）`;
            }
            setStatus('ready', statusText);
            document.getElementById('queryInfo').textContent = data.overview['查询范围'] || `${start} ~ ${end}`;
            // 不再弹原生 confirm：缺口明细已在上方面板里分类展示，
            // 需要补下载时面板里有明确按钮，避免"只报一个数字"的困惑。
        }, 600);

    } catch (e) {
        console.error('doQuery error:', e);
        hideProgress();
        setStatus('error', '查询失败');
        document.getElementById('queryInfo').textContent = '查询失败: ' + e.message;
        document.getElementById('step2Status').textContent = '✗ 失败';
        document.getElementById('step2').classList.add('active');
    } finally {
        _isQuerying = false;
        queryBtn.disabled = false;
        queryBtn.textContent = '🔍 查询复盘';
    }
}

// ============================================================
// 渲染全部数据
// ============================================================

function renderAll(data) {
    renderOverview(data.overview);
    renderWarnings(data);
    renderGapPanel(data);
    renderSymbolTable(data.symbol_summary);
    renderTopTrades(data.trades_sorted);
    renderTopOptTrades(data.opt_top_wins, data.opt_top_losses);
}

// ---- 数据缺口诊断面板 ----
let _gapExpanded = false;

function _dateChips(list, maxShown) {
    if (!list || !list.length) return '<span class="gap-none">无</span>';
    const shown = _gapExpanded ? list : list.slice(0, maxShown);
    let html = shown.map(d => `<span class="gap-chip">${escHtml(d)}</span>`).join('');
    if (!_gapExpanded && list.length > maxShown) {
        html += `<span class="gap-more">… 还有 ${list.length - maxShown} 天</span>`;
    }
    return html;
}

/**
 * 把「区间内没有结算单的交易日」分成三类展示。
 *
 * 只弹一个 "缺 24 天" 无法判断到底是真缺、还是账户开立前的日期、
 * 还是节假日（本来就没有）。这里逐类说明，并单独列出区间内的休市日，
 * 让用户一眼确认节假日确实被正确排除了。
 */
function renderGapPanel(data) {
    const panel = document.getElementById('gapPanel');
    if (!panel) return;

    const d = data.missing_detail;
    if (!d) { panel.style.display = 'none'; return; }

    const gaps = d.gaps || [];
    const before = d.before_range || [];
    const after = d.after_range || [];
    const unexplained = d.unexplained || [];
    const holidays = d.holidays || [];
    const totalMissing = (data.missing_count || 0);
    const candidates = (d.download_candidates || []);

    // 没有任何缺口也没有节假日信息 → 隐藏面板
    if (!totalMissing && !holidays.length) {
        panel.style.display = 'none';
        return;
    }

    const rows = [];

    rows.push(`<div class="gap-summary">
        区间 <b>${escHtml(d.start)} ~ ${escHtml(d.end)}</b>：
        交易日 <b>${d.trading_days}</b> 天，已有数据 <b>${d.with_data}</b> 天，
        缺失 <b class="${totalMissing ? 'gap-warn' : ''}">${totalMissing}</b> 天；
        区间内休市（节假日）<b>${d.holiday_days}</b> 天，周末 ${d.weekend_days} 天
        ${d.calendar_ok
            ? '· 已启用法定节假日日历'
            : '· <span class="gap-warn">未安装 chinesecalendar，节假日未校验</span>'}
    </div>`);

    rows.push(`<div class="gap-row">
        <div class="gap-label gap-label-bad">真实缺口 ${gaps.length} 天
            <small>夹在已有数据之间，最该关注</small></div>
        <div class="gap-chips">${_dateChips(gaps, 20)}</div>
    </div>`);

    rows.push(`<div class="gap-row">
        <div class="gap-label">早于已有数据 ${before.length} 天
            <small>本地最早 ${escHtml(d.first_data || '-')}，之前多为账户开立前，补不到</small></div>
        <div class="gap-chips">${_dateChips(before, 12)}</div>
    </div>`);

    rows.push(`<div class="gap-row">
        <div class="gap-label">晚于已有数据 ${after.length} 天
            <small>本地最新 ${escHtml(d.last_data || '-')}，属于新交易日，可补</small></div>
        <div class="gap-chips">${_dateChips(after, 12)}</div>
    </div>`);

    if (unexplained.length) {
        rows.push(`<div class="gap-row">
            <div class="gap-label">未分类 ${unexplained.length} 天
                <small>本地完全没有任何数据</small></div>
            <div class="gap-chips">${_dateChips(unexplained, 12)}</div>
        </div>`);
    }

    rows.push(`<div class="gap-row">
        <div class="gap-label gap-label-ok">区间内休市 ${holidays.length} 天
            <small>节假日，本来就不该有结算单，已正确排除</small></div>
        <div class="gap-chips">${_dateChips(holidays, 20)}</div>
    </div>`);

    const body = document.getElementById('gapBody');
    if (body) body.innerHTML = rows.join('');

    // 「值得补」= 真缺口 + 新交易日；为 0 就不必打扰用户
    const btn = document.getElementById('gapDownloadBtn');
    if (btn) {
        btn.style.display = candidates.length ? 'inline-flex' : 'none';
        btn.textContent = `登录补下载 ${candidates.length} 天`;
    }

    const toggle = document.getElementById('gapToggleBtn');
    if (toggle) {
        toggle.style.display = (gaps.length > 20 || before.length > 12
            || after.length > 12 || holidays.length > 20) ? 'inline-flex' : 'none';
        toggle.textContent = _gapExpanded ? '收起日期' : '展开全部日期';
    }

    panel.style.display = 'block';
}

function initGapPanelEvents() {
    const toggle = document.getElementById('gapToggleBtn');
    if (toggle) {
        toggle.addEventListener('click', () => {
            _gapExpanded = !_gapExpanded;
            if (window._lastQueryData) renderGapPanel(window._lastQueryData);
        });
    }
    const close = document.getElementById('gapCloseBtn');
    if (close) {
        close.addEventListener('click', () => {
            const p = document.getElementById('gapPanel');
            if (p) p.style.display = 'none';
        });
    }
    const dl = document.getElementById('gapDownloadBtn');
    if (dl) {
        dl.addEventListener('click', () => {
            if (!_isLoggedIn) { handleLoginClick(); return; }
            // 已登录：带着 auto_download 重新查询一次
            doQuery();
        });
    }
}

function escHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * 数据质量提示横幅。
 *
 * 这几类问题必须让用户看见，否则盈亏会被悄悄算错：
 *   1. 期权合约乘数无法确定（新品种未收录且结算单没有权利金总额）
 *      —— 原油这类品种按 1.0 计算会把盈亏缩小 1000 倍
 *   2. 期权到期作废被强制结算 —— 虚值到期不产生成交行，需要让用户核对
 *   3. 期末仍有未平仓期权 —— 已实现盈亏不含其浮动盈亏
 */
function renderWarnings(data) {
    const el = document.getElementById('dataWarnings');
    if (!el) return;

    const items = [];
    (data.multiplier_warnings || []).forEach(w => items.push(escHtml(w)));

    // 到期作废：虚值到期不会产生成交行，系统按到期日以 0 价格强制平仓
    const expiries = data.expiry_settlements || [];
    if (expiries.length) {
        const detail = expiries.slice(0, 5).map(e => {
            const sign = (e.pnl || 0) >= 0 ? '+' : '';
            return `${escHtml(e.contract)}（${escHtml(e.side)} ${e.volume} 手，`
                + `${sign}${fmtNum(e.pnl)} 元${e.date_is_approximate ? '，日期近似' : ''}）`;
        }).join('、');
        items.push(`已按「到期作废」结算 ${expiries.length} 个期权合约：${detail}`
            + `${expiries.length > 5 ? ' 等' : ''}。`
            + '虚值到期不会产生平仓成交行，系统按到期日以 0 价格平仓计入已实现盈亏；'
            + '若其中有实值行权，请核对结算单');
    }

    const openPos = data.opt_open_positions || {};
    // 解析布局提示：表头认不出来会回退固定列，可能算错，必须提示
    (data.layout_notes || []).forEach(w => items.push(escHtml(w)));
    // 到期日规则未收录 → 没做结算，需要提醒人工核对
    (data.expiry_unknown || []).forEach(w => items.push(escHtml(w)));

    // 方向无法识别（行权/履约）的期权行：这些行的盈亏确实没有被计算。
    // 必须聚合展示 —— 实测一次可能上百条，逐条列会把页面刷爆。
    const unparsed = data.unparsed_option_rows || [];
    if (unparsed.length) {
        unparsed.slice(0, 5).forEach(row => {
            items.push(`${escHtml(row.contract)} 在 ${escHtml(row.date)} 有 `
                + `${row.volume} 手方向为「${escHtml(row.direction)}」的记录，`
                + '未计入盈亏');
        });
        if (unparsed.length > 5) {
            items.push(`…另有 ${unparsed.length - 5} 条方向无法识别的期权记录`
                + `（共 ${unparsed.length} 条，这些行的盈亏均未计入）`);
        }
    }
    const openItems = Object.entries(openPos)
        .filter(([, v]) => v && ((v.long || 0) > 0 || (v.short || 0) > 0));
    if (openItems.length) {
        const detail = openItems.slice(0, 5)
            .map(([k, v]) => `${escHtml(k)} 多${v.long}手/空${v.short}手`)
            .join('、');
        items.push(`期末仍有 ${openItems.length} 个期权合约未平仓（${detail}${openItems.length > 5 ? ' 等' : ''}），`
            + '已实现盈亏不包含这些持仓的浮动盈亏');
    }

    if (!items.length) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    // 兜底截断：任何一类提示过多都不该把整页刷爆
    const MAX_ITEMS = 8;
    let shown = items;
    let tail = '';
    if (items.length > MAX_ITEMS) {
        shown = items.slice(0, MAX_ITEMS);
        tail = `<li class="warn-more">…另有 ${items.length - MAX_ITEMS} 条提示未显示</li>`;
    }
    el.style.display = 'block';
    el.innerHTML = `<div class="warn-title">⚠ 数据质量提示（${items.length}）</div>
        <ul>${shown.map(x => `<li>${x}</li>`).join('')}${tail}</ul>`;
}

function renderOverview(ov) {
    const set = (id, val, cls) => {
        const el = document.getElementById(id);
        el.textContent = val;
        el.className = 'card-value ' + (cls || '');
    };

    set('ovDays', ov['交易日数']);
    set('ovFuturesTrades', ov['期货笔数']);
    set('ovOptTrades', ov['期权笔数']);
    set('ovSymbols', ov['品种数']);

    // 总盈亏 = 期货平仓盈亏 + 期权FIFO已实现盈亏
    const totalPnl = (ov['期货盈亏(元)'] || 0) + (ov['期权盈亏(元)'] || 0);
    const tp = fmtColored(totalPnl);
    set('ovTotalPnl', tp.text, tp.cls);

    // 分项显示
    const pnl = fmtColored(ov['期货盈亏(元)']);
    set('ovPnl', pnl.text, pnl.cls);

    const optPnl = fmtColored(ov['期权盈亏(元)']);
    set('ovOptPnl', optPnl.text, optPnl.cls);

    const totalComm = (ov['期货手续费(元)'] || 0) + (ov['期权手续费(元)'] || 0);
    set('ovCommission', fmtNum(totalComm));

    // 净盈亏 = 总盈亏 - 全部手续费
    const net = fmtColored(totalPnl - totalComm);
    set('ovNetPnl', net.text, net.cls);
}

// ---- Symbol Summary Table ----
function renderSymbolTable(rows) {
    const tbody = document.getElementById('symbolTbody');
    if (!rows || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><p>暂无数据</p></td></tr>';
        return;
    }

    tbody.innerHTML = rows.map(r => {
        const fp = r['期货盈亏(元)'];
        const op = r['期权盈亏(元)'];
        const tp = r['总盈亏(元)'];
        const fpCls = fp > 0 ? 'positive' : fp < 0 ? 'negative' : '';
        const opCls = op > 0 ? 'positive' : op < 0 ? 'negative' : '';
        const tpCls = tp > 0 ? 'positive' : tp < 0 ? 'negative' : '';
        // 胜率颜色：>=50% 绿色，<50% 红色
        const wrVal = parseFloat(r['胜率']);
        const wrCls = !isNaN(wrVal) && wrVal >= 50 ? 'positive' : (!isNaN(wrVal) && wrVal > 0 ? 'negative' : '');
        return `<tr class="clickable" onclick="showSymbolDetail('${r['品种']}')">
            <td><a class="symbol-link">${r['品种']}</a></td>
            <td>${r['手数']}</td>
            <td class="${fpCls}">${(fp >= 0 ? '+' : '') + fmtNum(fp)}</td>
            <td>${fmtNum(r['期货手续费(元)'])}</td>
            <td class="${opCls}">${(op >= 0 ? '+' : '') + fmtNum(op)}</td>
            <td>${fmtNum(r['期权手续费(元)'])}</td>
            <td class="${tpCls}">${(tp >= 0 ? '+' : '') + fmtNum(tp)}</td>
            <td>${fmtNum(r['总手续费(元)'])}</td>
            <td class="${wrCls}">${r['胜率'] || '-'}</td>
            <td>${r['盈亏比'] || '-'}</td>
        </tr>`;
    }).join('');
}

// ---- TOP Trades ----
function renderTopTrades(trades) {
    const winsBody = document.getElementById('topWinsBody');
    const lossesBody = document.getElementById('topLossesBody');

    // 空结果必须清空表格，否则会残留上一次查询的 TOP 数据，
    // 与已更新的总览卡片形成"张冠李戴"的错配。
    if (!trades || trades.length === 0) {
        const emptyRow = '<tr><td colspan="8" class="empty-state">暂无成交记录</td></tr>';
        if (winsBody) winsBody.innerHTML = emptyRow;
        if (lossesBody) lossesBody.innerHTML = emptyRow;
        return;
    }

    winsBody.innerHTML = trades.slice(0, 10).map(t => {
        const pnlCls = t.realized_pnl > 0 ? 'positive' : t.realized_pnl < 0 ? 'negative' : '';
        const pnlText = (t.realized_pnl >= 0 ? '+' : '') + fmtNum(t.realized_pnl);
        return `<tr>
            <td>${shortDate(t.date)}</td>
            <td style="font-weight:600">${t.contract}</td>
            <td>${t.direction}</td>
            <td>${t.open_close}</td>
            <td>${fmtNum(t.price)}</td>
            <td>${t.volume}</td>
            <td class="${pnlCls}">${pnlText}</td>
            <td>${fmtNum(t.commission)}</td>
        </tr>`;
    }).join('');

    lossesBody.innerHTML = trades.slice(-10).reverse().map(t => {
        const pnlCls = t.realized_pnl > 0 ? 'positive' : t.realized_pnl < 0 ? 'negative' : '';
        const pnlText = (t.realized_pnl >= 0 ? '+' : '') + fmtNum(t.realized_pnl);
        return `<tr>
            <td>${shortDate(t.date)}</td>
            <td style="font-weight:600">${t.contract}</td>
            <td>${t.direction}</td>
            <td>${t.open_close}</td>
            <td>${fmtNum(t.price)}</td>
            <td>${t.volume}</td>
            <td class="${pnlCls}">${pnlText}</td>
            <td>${fmtNum(t.commission)}</td>
        </tr>`;
    }).join('');
}

// ---- TOP Options Trades（FIFO 匹配后的已实现盈亏）----
function renderTopOptTrades(optWins, optLosses) {
    // 优先取 FIFO 已实现盈亏 realized_pnl；旧接口只有 pnl 时回退
    const optPnlOf = o => (typeof o.realized_pnl === 'number' ? o.realized_pnl : (o.pnl || 0));

    const renderRows = rows => rows.map(o => {
        const pnl = optPnlOf(o);
        const pnlCls = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
        const pnlText = (pnl >= 0 ? '+' : '') + fmtNum(pnl);
        return `<tr>
            <td>${shortDate(o.date)}</td>
            <td style="font-weight:600">${o.opt_contract}</td>
            <td>${o.underlying || '-'}</td>
            <td>${o.opt_type || '-'}</td>
            <td>${o.direction}</td>
            <td>${fmtNum(o.strike)}</td>
            <td>${fmtNum(o.premium_price)}</td>
            <td>${o.volume}</td>
            <td class="${pnlCls}" style="font-weight:600">${pnlText}</td>
        </tr>`;
    }).join('');

    // TOP 10 期权盈利
    const winsBody = document.getElementById('topOptWinsBody');
    winsBody.innerHTML = (optWins && optWins.length)
        ? renderRows(optWins)
        : '<tr><td colspan="9" class="empty-state"><p>暂无期权平仓盈利</p></td></tr>';

    // TOP 10 期权亏损
    const lossesBody = document.getElementById('topOptLossesBody');
    lossesBody.innerHTML = (optLosses && optLosses.length)
        ? renderRows(optLosses)
        : '<tr><td colspan="9" class="empty-state"><p>暂无期权平仓亏损</p></td></tr>';
}

// ============================================================
// 图表加载
// ============================================================

async function loadCharts(start, end) {
    // 使用 ECharts 渲染图表（不再加载后端 PNG）
    try {
        const data = await apiGet(`/api/chart-data?start=${start}&end=${end}`);
        if (data.error) return;

        if (data.daily_trend) renderTrendChart(data.daily_trend);
        renderEquityChart(data.equity, data.equity_stats);

        updateProgress(98, '图表渲染完成');
    } catch (e) {
        console.warn('图表数据加载失败:', e);
    }
}

// ---- 账户权益曲线 ----
let _equityChart = null;
let _equityResizeBound = false;
let _lastEquity = null;      // { rows, stats } —— 供主题切换重绘
let _lastTrend = null;       // 每日趋势数据 —— 供主题切换重绘

/** 主题切换后，用缓存的同一份数据以新配色重绘两张图 */
function redrawReviewCharts() {
    if (_lastTrend) {
        try { renderTrendChart(_lastTrend); } catch (e) { /* ignore */ }
    }
    if (_lastEquity) {
        try { renderEquityChart(_lastEquity.rows, _lastEquity.stats); } catch (e) { /* ignore */ }
    }
}

/**
 * 权益曲线：客户权益 + 当日结存（左轴）、保证金占用（右轴）。
 *
 * 数据来自结算单的「资金状况」区。之前这块解析了却从未展示，
 * 导致复盘只能看"赚了多少"，看不到"账户怎么走过来的"（含回撤）。
 */
function renderEquityChart(rows, stats) {
    const T = chartTheme();
    // 缓存一份，切换主题时用新配色重绘（图表颜色不在 CSS 里）
    _lastEquity = { rows: rows, stats: stats };
    const dom = document.getElementById('chartEquity');
    if (!dom) return;

    if (!rows || !rows.length) {
        dom.innerHTML = '<div class="empty-state"><p>该区间没有资金状况数据</p></div>';
        _setEquityStats(null);
        return;
    }
    if (typeof echarts === 'undefined') {
        dom.innerHTML = '<div class="empty-state"><p>ECharts 未加载（请检查网络能否访问 CDN）</p></div>';
        return;
    }

    dom.innerHTML = '';
    if (_equityChart && !_equityChart.isDisposed()) _equityChart.dispose();

    _equityChart = echarts.init(dom, null, { renderer: 'canvas' });

    const dates = rows.map(r => String(r.date || '').slice(5));   // MM-DD
    const equity = rows.map(r => r.equity);
    const deposit = rows.map(r => r.deposit);
    const margin = rows.map(r => r.margin);
    const available = rows.map(r => r.available);

    // 起点定为区间首日，让"涨了多少"一眼可见
    const base = equity.length ? equity[0] : 0;

    _equityChart.setOption({
        animation: false,
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
            backgroundColor: T.tooltipBg,
            borderColor: T.tooltipBorder,
            textStyle: { color: T.textStrong, fontSize: 12 },
            valueFormatter: (v) => (v === null || v === undefined) ? '-' : fmtNum(v),
        },
        legend: {
            top: 0, textStyle: { color: T.text, fontSize: 11 },
            data: ['客户权益', '当日结存', '保证金占用', '可用资金'],
        },
        grid: { left: 76, right: 76, top: 34, bottom: 66 },
        xAxis: {
            type: 'category', data: dates, boundaryGap: false,
            axisLine: { lineStyle: { color: T.axis } },
            axisLabel: { color: T.text, fontSize: 10 },
        },
        yAxis: [
            {
                type: 'value', scale: true, name: '权益(元)',
                nameTextStyle: { color: T.text, fontSize: 10 },
                splitLine: { lineStyle: { color: T.split } },
                axisLabel: { color: T.text, fontSize: 10, formatter: v => fmtNum(v, 0) },
            },
            {
                type: 'value', scale: true, name: '保证金(元)',
                nameTextStyle: { color: T.text, fontSize: 10 },
                splitLine: { show: false },
                axisLabel: { color: T.text, fontSize: 10, formatter: v => fmtNum(v, 0) },
            },
        ],
        dataZoom: [
            { type: 'inside', start: 0, end: 100 },
            { type: 'slider', bottom: 8, height: 16, start: 0, end: 100,
              textStyle: { color: T.text, fontSize: 10 } },
        ],
        series: [
            {
                name: '客户权益', type: 'line', data: equity,
                symbol: 'none', smooth: true,
                lineStyle: { width: 2, color: '#3b82f6' },
                itemStyle: { color: '#3b82f6' },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(59,130,246,.35)' },
                        { offset: 1, color: 'rgba(59,130,246,.02)' },
                    ]),
                },
                // 首日基准线：直观看出区间内权益是涨是跌
                markLine: base ? {
                    symbol: 'none', silent: true,
                    data: [{
                        yAxis: base,
                        lineStyle: { color: T.muted, type: 'dashed', width: 1 },
                        label: { formatter: '起始 ' + fmtNum(base, 0),
                                 color: T.text, position: 'insideEndTop', fontSize: 10 },
                    }],
                } : undefined,
            },
            {
                name: '当日结存', type: 'line', data: deposit, symbol: 'none',
                lineStyle: { width: 1.2, type: 'dashed', color: '#a855f7' },
                itemStyle: { color: '#a855f7' },
            },
            {
                name: '保证金占用', type: 'line', yAxisIndex: 1, data: margin,
                symbol: 'none', smooth: true,
                lineStyle: { width: 1.2, color: '#f59e0b' },
                itemStyle: { color: '#f59e0b' },
            },
            {
                name: '可用资金', type: 'line', yAxisIndex: 1, data: available,
                symbol: 'none',
                lineStyle: { width: 1, type: 'dotted', color: '#22c55e' },
                itemStyle: { color: '#22c55e' },
            },
        ],
    });

    _setEquityStats(stats);

    if (!_equityResizeBound) {
        _equityResizeBound = true;
        window.addEventListener('resize', () => {
            if (_equityChart && !_equityChart.isDisposed()) _equityChart.resize();
        });
    }
}

function _setEquityStats(stats) {
    const el = document.getElementById('equityStats');
    if (!el) return;
    if (!stats || !stats.last_equity) { el.textContent = ''; return; }
    const chg = stats.change || 0;
    const cls = chg > 0 ? 'negative' : chg < 0 ? 'positive' : '';   // 红涨绿跌
    const sign = chg > 0 ? '+' : '';
    const dd = stats.max_drawdown_pct;
    el.innerHTML = `起始 ${fmtNum(stats.first_equity, 0)} → 期末 `
        + `<b class="${cls}">${fmtNum(stats.last_equity, 0)}</b>`
        + `（<span class="${cls}">${sign}${fmtNum(chg, 0)}`
        + `${stats.change_pct === null || stats.change_pct === undefined ? '' : '，' + sign + fmtNum(stats.change_pct) + '%'}</span>）`
        + (dd === null || dd === undefined ? ''
            : ` · 最大回撤 <span class="positive">${fmtNum(dd)}%</span>`
              + (stats.peak_date ? `（${escHtml(stats.peak_date)} → ${escHtml(stats.trough_date)}）` : ''));
}

// ---- 每日趋势图（横向：X轴日期，Y轴金额）----
// 用于点击反查的最新上下文（图表实例 + 日期数组），供 mouseup 兜底使用
let _trendChart = null;
let _trendDates = [];
let _trendResizeBound = false;

// 全局只绑定一次窗口缩放监听：图表跟随浏览器窗口自适应
function ensureTrendChartResize() {
    if (_trendResizeBound) return;
    _trendResizeBound = true;
    window.addEventListener('resize', () => {
        if (_trendChart && !_trendChart.isDisposed()) _trendChart.resize();
    });
}

function renderTrendChart(trendData) {
    const T = chartTheme();
    // 缓存一份，切换主题时重绘
    _lastTrend = trendData;
    const dom = document.getElementById('chartTrend');
    if (!dom || !trendData || !trendData.length) return;

    // 重复渲染时先销毁旧实例，避免 ECharts 警告与事件重复绑定
    const old = echarts.getInstanceByDom(dom);
    if (old) old.dispose();
    const chart = echarts.init(dom);
    _trendChart = chart;
    ensureTrendChartResize();

    // 完整日期 YYYY-MM-DD（后端已改为全量日期，跨年不会重复）
    const dates = trendData.map(d => String(d.date).slice(0, 10));
    _trendDates = dates;
    const dailyPnl = trendData.map(d => (d.pnl || 0) - (d.commission || 0));
    const cumulative = trendData.map((d, i) =>
        dailyPnl.slice(0, i + 1).reduce((a, b) => a + b, 0)
    );

    // 固定高度（标准图表高度）
    dom.style.height = '420px';
    chart.resize();

    const n = dates.length;
    // 长周期默认聚焦最近 60 个交易日，并提供滑块让用户完整浏览全部区间
    const defaultWindow = 60;
    const startPct = n > defaultWindow ? Math.max(0, (1 - defaultWindow / n) * 100) : 0;
    const zoomComponents = n > 15 ? [
        { type: 'inside', start: startPct, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
        { type: 'slider', start: startPct, end: 100, height: 18, bottom: 10,
          fillerOpacity: 0.25, handleSize: '110%',
          textStyle: { color: T.text, fontSize: 10 } },
    ] : [];

    chart.setOption({
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
            backgroundColor: T.tooltipBg,
            borderColor: 'rgba(255,255,255,0.15)',
            borderWidth: 1,
            borderRadius: 6,
            padding: [8, 12],
            textStyle: { color: T.textStrong, fontSize: 13 },
        },
        legend: {
            data: ['每日净盈亏', '累计净盈亏'],
            top: 0,
            itemWidth: 20,
            itemHeight: 12,
            itemGap: 24,
            textStyle: { color: '#fff', fontSize: 13, fontWeight: '500' },
            selectedMode: true,
            backgroundColor: 'rgba(255,255,255,0.06)',
            borderColor: 'rgba(255,255,255,0.12)',
            borderWidth: 1,
            borderRadius: 6,
            padding: [8, 16],
        },
        grid: { left: 60, right: 60, top: 55, bottom: zoomComponents.length ? 90 : 60 },
        xAxis: {
            type: 'category',
            data: dates,
            axisLabel: {
                fontSize: 11, color: T.text,
                rotate: n > 20 ? 35 : (n > 10 ? 25 : 0),
                // 跨年范围显示 YY-MM-DD，否则 MM-DD
                formatter: v => (n > 0 && new Set(dates.map(d => d.slice(0, 4))).size > 1)
                    ? v.substring(2) : v.substring(5),
            },
            axisLine: { lineStyle: { color: T.axis } },
            axisTick: { alignWithLabel: true },
        },
        yAxis: {
            type: 'value',
            axisLabel: {
                fontSize: 11, color: T.text,
                formatter: v => (Math.abs(v) >= 10000 ? (v / 10000).toFixed(1) + 'w' : v),
            },
            axisLine: { lineStyle: { color: T.axis } },
            splitLine: { lineStyle: { color: T.split } }
        },
        dataZoom: zoomComponents,
        series: [
            {
                name: '每日净盈亏',
                type: 'bar',
                data: dailyPnl.map(v => ({
                    value: Math.round(v * 100) / 100,
                    itemStyle: { color: v >= 0 ? '#e74c3c' : '#27ae60' }
                })),
                barMaxWidth: 30,
            },
            {
                name: '累计净盈亏',
                type: 'line',
                data: cumulative.map(v => Math.round(v * 100) / 100),
                smooth: true,
                yAxisIndex: 0,
                lineStyle: { color: '#3b82f6', width: 2.5 },
                itemStyle: { color: '#3b82f6' },
                areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                    colorStops: [{ offset: 0, color: 'rgba(59,130,246,0.25)' }, { offset: 1, color: 'rgba(59,130,246,0)' }]
                }}
            }
        ]
    });

    // 点击柱状图 → 弹出该日交割单
    // 主路径: ECharts 原生 click（拿到 params.name 即类目值）
    // 兜底路径: canvas mouseup + convertFromPixel 反查 dataIndex，
    //          兼容点击空白热区/负值柱顶部等原生事件未命中的情况；
    //          convertFromPixel 自动考虑 dataZoom 当前窗口，缩放后依然准确。
    let clickHandled = false;

    chart.on('click', function (params) {
        if (params.componentType === 'series' && params.seriesType === 'bar' && params.name) {
            clickHandled = true;
            showDailyDetail(String(params.name).slice(0, 10));
        }
    });

    // 每次重新渲染都要先摘掉上一轮绑定的处理器。
    // 原实现直接 addEventListener，查询 N 次就会累积 N 个闭包（捕获的是已被
    // dispose 的旧 chart），点击趋势图时会重复触发并访问已销毁的实例。
    if (dom._trendHandlers) {
        dom.removeEventListener('mousedown', dom._trendHandlers.down);
        dom.removeEventListener('mouseup', dom._trendHandlers.up);
    }

    const onTrendMouseDown = () => { clickHandled = false; };
    const onTrendMouseUp = function (e) {
        if (clickHandled) return;
        const rect = dom.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        let idx = null;
        try {
            const point = chart.convertFromPixel({ seriesIndex: 0 }, [x, y]);
            if (point && isFinite(point[0])) idx = Math.round(point[0]);
        } catch (_) { /* 图表正在重建等场景忽略 */ }
        if (idx === null || idx < 0 || idx >= _trendDates.length) return;
        // 仅当点击确实落在绘图区时响应
        let grid = {};
        try {
            const opt = chart.getOption();
            grid = (opt.grid && opt.grid[0]) || {};
        } catch (_) { return; }   // 旧 chart 已销毁时不响应
        const left = parseFloat(grid.left) || 60;
        const right = parseFloat(grid.right) || 60;
        const top = parseFloat(grid.top) || 55;
        const bottom = parseFloat(grid.bottom) || 60;
        if (x < left || x > rect.width - right || y < top || y > rect.height - bottom) return;
        showDailyDetail(_trendDates[idx]);
    };

    dom.addEventListener('mousedown', onTrendMouseDown);
    dom.addEventListener('mouseup', onTrendMouseUp);
    dom._trendHandlers = { down: onTrendMouseDown, up: onTrendMouseUp };
}


// ============================================================
// 品种详情 Modal
// ============================================================

let currentSymbol = '';

async function showSymbolDetail(symbol) {
    currentSymbol = symbol;
    const start = document.getElementById('startDate').value;
    const end = document.getElementById('endDate').value;

    document.getElementById('symbolModal').style.display = 'flex';
    document.getElementById('modalTitle').textContent = `品种详情 - ${symbol}`;

    document.getElementById('modalStats').innerHTML = '<div class="skeleton" style="min-height:120px;grid-column:1/-1;"></div>';
    ['detailTradesBody', 'detailOptsBody', 'detailDailyBody', 'detailCommBody'].forEach(id => {
        document.getElementById(id).innerHTML = '<tr><td colspan="10" class="empty-state"><p>加载中...</p></td></tr>';
    });

    try {
        let url = `/api/symbol/${symbol}`;
        if (start && end) url += `?start=${start}&end=${end}`;
        const data = await apiGet(url);

        renderModalStats(data.stats);
        renderDetailTrades(data.trades);
        renderDetailOptions(data.options);
        renderDetailDaily(data.daily_stats);
        renderDetailCommission(data.stats, data.daily_stats);
        switchTab('tabTrades');
    } catch (e) {
        document.getElementById('modalStats').innerHTML =
            `<div class="empty-state" style="grid-column:1/-1;"><p>加载失败: ${e.message}</p></div>`;
    }
}

function closeModal() { document.getElementById('symbolModal').style.display = 'none'; }

document.addEventListener('click', e => { if (e.target.id === 'symbolModal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ---- Modal Stats Cards ----
function renderModalStats(stats) {
    const fields = [
        ['期货笔数', stats['期货笔数']], ['期权笔数', stats['期权笔数']],
        ['平仓笔数', stats['平仓笔数']], ['总成交量(手)', stats['总成交量(手)']],
        ['总盈亏(元)', stats['总盈亏(元)']], ['净盈亏(元)', stats['净盈亏(元)']],
        ['期货盈亏(元)', stats['期货盈亏(元)']], ['期货净盈亏(元)', stats['期货净盈亏(元)']],
        ['期权盈亏(元)', stats['期权盈亏(元)']], ['期权权利金收支(元)', stats['期权权利金收支(元)']],
        ['期货手续费(元)', stats['期货手续费(元)']], ['期权手续费(元)', stats['期权手续费(元)']],
        ['胜率', stats['胜率']], ['盈亏比', stats['盈亏比']],
        ['均赢(元)', stats['均赢(元)']], ['均亏(元)', stats['均亏(元)']],
        ['最大单笔盈利(元)', stats['最大单笔盈利(元)']], ['最大单笔亏损(元)', stats['最大单笔亏损(元)']],
    ];
    const coloredLabels = ['总盈亏(元)', '净盈亏(元)', '期货盈亏(元)', '期货净盈亏(元)',
                           '期权盈亏(元)', '期权权利金收支(元)',
                           '均赢(元)', '均亏(元)',
                           '最大单笔盈利(元)', '最大单笔亏损(元)'];

    document.getElementById('modalStats').innerHTML = fields.map(([label, val]) => {
        const colored = coloredLabels.includes(label);
        const c = fmtColored(val);
        return `<div class="card"><div class="card-label">${label}</div><div class="card-value ${colored ? c.cls : ''}">${c.text}</div></div>`;
    }).join('');
}

// ---- 成交明细（期货）----
function renderDetailTrades(trades) {
    const tbody = document.getElementById('detailTradesBody');
    const tfoot = document.getElementById('detailTradesFoot');

    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><p>暂无期货成交记录</p></td></tr>';
        tfoot.innerHTML = '';
        return;
    }

    // 统计盈亏
    let totalPnl = 0, totalCommission = 0, totalAmount = 0, totalVolume = 0;
    let winCount = 0, lossCount = 0, winPnl = 0, lossPnl = 0;

    tbody.innerHTML = trades.map(t => {
        const pnl = t.realized_pnl || 0;
        totalPnl += pnl;
        totalCommission += (t.commission || 0);
        totalAmount += (t.amount || 0);
        totalVolume += (t.volume || 0);
        if (pnl > 0) { winCount++; winPnl += pnl; }
        else if (pnl < 0) { lossCount++; lossPnl += pnl; }

        const pnlCls = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
        const pnlText = (pnl >= 0 ? '+' : '') + fmtNum(pnl);
        return `<tr>
            <td>${shortDate(t.date)}</td>
            <td style="font-weight:600">${t.contract}</td>
            <td>${t.direction}</td>
            <td>${t.open_close}</td>
            <td>${fmtNum(t.price)}</td>
            <td>${t.volume}</td>
            <td>${fmtNum(t.amount)}</td>
            <td>${fmtNum(t.commission)}</td>
            <td class="${pnlCls}">${pnlText}</td>
        </tr>`;
    }).join('');

    // 汇总行
    const netPnlCls = totalPnl >= 0 ? 'positive' : 'negative';
    const netText = (totalPnl >= 0 ? '+' : '') + fmtNum(totalPnl);
    tfoot.innerHTML = `<tr class="summary-row">
        <td colspan="4" style="text-align:right;font-weight:700;">合计 (${trades.length} 笔)</td>
        <td>-</td>
        <td>${totalVolume}</td>
        <td>${fmtNum(totalAmount)}</td>
        <td>${fmtNum(totalCommission)}</td>
        <td class="${netPnlCls}" style="font-weight:700;">${netText}</td>
    </tr><tr class="sub-summary-row">
        <td colspan="6"></td>
        <td class="positive">盈利: +${fmtNum(winPnl)} (${winCount}笔)</td>
        <td></td>
        <td class="negative">亏损: ${fmtNum(lossPnl)} (${lossCount}笔)</td>
    </tr>`;
}

// ---- 期权明细（FIFO 匹配后平仓盈亏）----
function renderDetailOptions(options) {
    const tbody = document.getElementById('detailOptsBody');
    const tfoot = document.getElementById('detailOptsFoot');

    if (!options || options.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><p>暂无期权成交记录</p></td></tr>';
        tfoot.innerHTML = '';
        return;
    }

    // 统计盈亏（优先 FIFO 已实现盈亏 realized_pnl）
    let totalPnl = 0, totalCommission = 0, totalVolume = 0;
    let winCount = 0, lossCount = 0, winPnl = 0, lossPnl = 0;

    tbody.innerHTML = options.map(o => {
            // 优先 FIFO 已实现盈亏 realized_pnl，旧数据回退权利金现金流
            const pnl = typeof o.realized_pnl === 'number' ? o.realized_pnl : (o.pnl || 0);
        totalPnl += pnl;
        totalCommission += (o.commission || 0);
        totalVolume += (o.volume || 0);
        if (pnl > 0) { winCount++; winPnl += pnl; }
        else if (pnl < 0) { lossCount++; lossPnl += pnl; }

        const pnlCls = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
        const pnlText = (pnl >= 0 ? '+' : '') + fmtNum(pnl);
        return `<tr>
            <td>${shortDate(o.date)}</td>
            <td style="font-weight:600">${o.opt_contract}</td>
            <td>${o.underlying}</td>
            <td>${o.opt_type}</td>
            <td>${fmtNum(o.strike)}</td>
            <td>${o.direction}</td>
            <td>${fmtNum(o.premium_price)}</td>
            <td>${o.volume}</td>
            <td class="${pnlCls}">${pnlText}</td>
            <td>${fmtNum(o.commission)}</td>
        </tr>`;
    }).join('');

    // 汇总行
    const netPnlCls = totalPnl >= 0 ? 'positive' : 'negative';
    const netText = (totalPnl >= 0 ? '+' : '') + fmtNum(totalPnl);
    tfoot.innerHTML = `<tr class="summary-row">
        <td colspan="7" style="text-align:right;font-weight:700;">合计 (${options.length} 笔)</td>
        <td>${totalVolume}</td>
        <td class="${netPnlCls}" style="font-weight:700;">${netText}</td>
        <td>${fmtNum(totalCommission)}</td>
    </tr><tr class="sub-summary-row">
        <td colspan="8"></td>
        <td class="positive">盈利: +${fmtNum(winPnl)} (${winCount}笔)</td>
        <td class="negative">亏损: ${fmtNum(lossPnl)} (${lossCount}笔)</td>
    </tr>`;
}

// ---- 每日汇总 ----
function renderDetailDaily(dailyStats) {
    const tbody = document.getElementById('detailDailyBody');
    if (!dailyStats || dailyStats.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state"><p>暂无每日汇总</p></td></tr>';
        return;
    }
    tbody.innerHTML = dailyStats.map(d => {
        const cls = d['净盈亏(元)'] > 0 ? 'positive' : d['净盈亏(元)'] < 0 ? 'negative' : '';
        const netText = (d['净盈亏(元)'] >= 0 ? '+' : '') + fmtNum(d['净盈亏(元)']);
        return `<tr>
            <td>${d.date}</td>
            <td>${d['笔数']}</td>
            <td class="${d['盈亏(元)'] > 0 ? 'positive' : d['盈亏(元)'] < 0 ? 'negative' : ''}">${fmtNum(d['盈亏(元)'])}</td>
            <td>${fmtNum(d['手续费(元)'])}</td>
            <td class="${cls}">${netText}</td>
        </tr>`;
    }).join('');
}

// ---- 手续费统计 ----
function renderDetailCommission(stats, dailyStats) {
    const summaryEl = document.getElementById('commissionSummary');
    summaryEl.innerHTML = `
        <div class="cstat"><div class="cstat-label">期货手续费</div><div class="cstat-val negative">${fmtNum(stats['期货手续费(元)'])}</div></div>
        <div class="cstat"><div class="cstat-label">期权手续费</div><div class="cstat-val negative">${fmtNum(stats['期权手续费(元)'])}</div></div>
        <div class="cstat"><div class="cstat-label">总手续费</div><div class="cstat-val negative">${fmtNum((stats['期货手续费(元)']||0) + (stats['期权手续费(元)']||0))}</div></div>
        <div class="cstat"><div class="cstat-label">手续费/盈亏比</div><div class="cstat-val neutral">${stats['期货净盈亏(元)'] !== 0 ?
            (Math.abs(stats['期货手续费(元)']) / Math.abs(stats['期货净盈亏(元)']) * 100).toFixed(1) + '%' : '-'}</div></div>
        <div class="cstat"><div class="cstat-label">日均手续费</div><div class="cstat-val negative">${dailyStats.length > 0 ? fmtNum((stats['期货手续费(元)']||0) / dailyStats.length) : '-'}</div></div>
        <div class="cstat"><div class="cstat-label">单笔平均手续费</div><div class="cstat-val negative">${stats['期货笔数'] > 0 ? fmtNum(stats['期货手续费(元)'] / stats['期货笔数']) : '-'}</div></div>
    `;

    const tbody = document.getElementById('detailCommBody');
    if (!dailyStats || dailyStats.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="empty-state"><p>暂无数据</p></td></tr>';
        return;
    }

    const sorted = [...dailyStats].sort((a, b) => b['手续费(元)'] - a['手续费(元)']);
    const totalComm = sorted.reduce((s, d) => s + d['手续费(元)'], 0);

    tbody.innerHTML = sorted.map((d) => {
        const pct = ((d['手续费(元)'] / totalComm) * 100).toFixed(1);
        return `<tr>
            <td>${d.date}</td>
            <td>${d['笔数']}</td>
            <td class="negative">${fmtNum(d['手续费(元)'])}<span style="color:var(--text-dimmer);font-size:11px;margin-left:4px;">(${pct}%)</span></td>
        </tr>`;
    }).join('');
}

// ---- Tab 切换 ----
function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`[data-tab="${tabId}"]`).classList.add('active');
    document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
    document.getElementById(tabId).style.display = 'block';
}

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ============================================================
// 日期交割单（点击趋势图日期时展示）
// ============================================================

/** 打开某日期的交割单 */
async function showDailyDetail(dateStr) {
    const start = document.getElementById('startDate').value;
    document.getElementById('dailyModal').style.display = 'flex';
    document.getElementById('dailyModalTitle').textContent = `📅 交割单 - ${dateStr}`;

    // 显示骨架屏
    document.getElementById('dailyStats').innerHTML = '<div class="skeleton" style="min-height:100px;grid-column:1/-1;"></div>';
    ['dailyFuturesBody', 'dailyOptionsBody', 'dailyFuturesFoot', 'dailyOptionsFoot'].forEach(id => {
        document.getElementById(id).innerHTML = '<tr><td colspan="9" class="empty-state"><p>加载中...</p></td></tr>';
    });

    try {
        let url = `/api/daily/${encodeURIComponent(dateStr)}`;
        if (start) url += `?start=${start}`;
        const data = await apiGet(url);

        renderDailyStats(data.summary);
        renderDailyFutures(data.futures_trades);
        renderDailyOptions(data.options_trades);
        switchTab('dailyTabFutures');
    } catch (e) {
        document.getElementById('dailyStats').innerHTML =
            `<div class="empty-state" style="grid-column:1/-1;"><p>加载失败: ${e.message}</p></div>`;
    }
}

function closeDailyModal() { document.getElementById('dailyModal').style.display = 'none'; }

document.addEventListener('click', e => { if (e.target.id === 'dailyModal') closeDailyModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDailyModal(); });

/** 渲染当日汇总统计卡片 */
function renderDailyStats(summary) {
    const fields = [
        ['期货笔数', summary['期货笔数']], ['期权笔数', summary['期权笔数']],
        ['期货成交量(手)', summary['期货成交量(手)']],
        ['期货盈亏(元)', summary['期货盈亏(元)']], ['期货手续费(元)', summary['期货手续费(元)']],
        ['期货净盈亏(元)', summary['期货净盈亏(元)']],
        ['期权盈亏(元)', summary['期权盈亏(元)']], ['期权手续费(元)', summary['期权手续费(元)']],
        ['当日总盈亏(元)', summary['当日总盈亏(元)']],
        ['当日总手续费(元)', summary['当日总手续费(元)']],
        ['当日净盈亏(元)', summary['当日净盈亏(元)']],
    ];

    document.getElementById('dailyStats').innerHTML = fields.map(([label, val]) => {
        const colored = ['期货盈亏(元)', '期货净盈亏(元)', '期权盈亏(元)',
                        '当日总盈亏(元)', '当日净盈亏(元)'].includes(label);
        const c = fmtColored(val);
        return `<div class="card"><div class="card-label">${label}</div><div class="card-value ${colored ? c.cls : ''}">${c.text}</div></div>`;
    }).join('');
}

/** 渲染当日期货成交明细 */
function renderDailyFutures(trades) {
    const tbody = document.getElementById('dailyFuturesBody');
    const tfoot = document.getElementById('dailyFuturesFoot');

    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><p>该日无期货成交记录</p></td></tr>';
        tfoot.innerHTML = '';
        return;
    }

    let totalPnl = 0, totalComm = 0, totalAmount = 0, totalVolume = 0;

    tbody.innerHTML = trades.map(t => {
        const pnl = t.realized_pnl || 0;
        totalPnl += pnl;
        totalComm += (t.commission || 0);
        totalAmount += (t.amount || 0);
        totalVolume += (t.volume || 0);

        const pnlCls = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
        const pnlText = (pnl >= 0 ? '+' : '') + fmtNum(pnl);
        return `<tr>
            <td style="font-weight:600">${t.contract}</td>
            <td><a class="symbol-link" onclick="showSymbolDetail('${t.symbol}');closeDailyModal();">${t.symbol}</a></td>
            <td>${t.direction}</td>
            <td>${t.open_close}</td>
            <td>${fmtNum(t.price)}</td>
            <td>${t.volume}</td>
            <td>${fmtNum(t.amount)}</td>
            <td>${fmtNum(t.commission)}</td>
            <td class="${pnlCls}" style="font-weight:600">${pnlText}</td>
        </tr>`;
    }).join('');

    const netPnlCls = totalPnl >= 0 ? 'positive' : 'negative';
    tfoot.innerHTML = `<tr class="summary-row">
        <td colspan="5" style="text-align:right;font-weight:700;">合计 (${trades.length} 笔)</td>
        <td>${totalVolume}</td>
        <td>${fmtNum(totalAmount)}</td>
        <td>${fmtNum(totalComm)}</td>
        <td class="${netPnlCls}" style="font-weight:700;">${(totalPnl >= 0 ? '+' : '') + fmtNum(totalPnl)}</td>
    </tr>`;
}

/** 渲染当日期权成交明细 */
function renderDailyOptions(options) {
    const tbody = document.getElementById('dailyOptionsBody');
    const tfoot = document.getElementById('dailyOptionsFoot');

    if (!options || options.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><p>该日无期权成交记录</p></td></tr>';
        tfoot.innerHTML = '';
        return;
    }

    let totalPnl = 0, totalComm = 0;

    tbody.innerHTML = options.map(o => {
        // 优先 FIFO 已实现盈亏 realized_pnl，旧数据回退权利金现金流
        const pnl = typeof o.realized_pnl === 'number' ? o.realized_pnl : (o.pnl || 0);
        totalPnl += pnl;
        totalComm += (o.commission || 0);

        const pnlCls = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : '';
        const pnlText = (pnl >= 0 ? '+' : '') + fmtNum(pnl);
        return `<tr>
            <td style="font-weight:600">${o.opt_contract}</td>
            <td>${o.underlying}</td>
            <td>${o.opt_type}</td>
            <td>${o.direction}</td>
            <td>${fmtNum(o.strike)}</td>
            <td>${fmtNum(o.premium_price)}</td>
            <td>${o.volume}</td>
            <td class="${pnlCls}" style="font-weight:600">${pnlText}</td>
            <td>${fmtNum(o.commission)}</td>
        </tr>`;
    }).join('');

    const netPnlCls = totalPnl >= 0 ? 'positive' : 'negative';
    tfoot.innerHTML = `<tr class="summary-row">
        <td colspan="7" style="text-align:right;font-weight:700;">合计 (${options.length} 笔)</td>
        <td class="${netPnlCls}" style="font-weight:700;">${(totalPnl >= 0 ? '+' : '') + fmtNum(totalPnl)}</td>
        <td>${fmtNum(totalComm)}</td>
    </tr>`;
}

// ============================================================
// 启动
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    updateThemeButton(currentTheme());
    initGapPanelEvents();
    initDates();
});