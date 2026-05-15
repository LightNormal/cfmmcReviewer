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
        loginInfo.textContent = '';
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
        const { date_range, available_dates, logged_in, has_data, output_dir_exists } = data;

        if (date_range.start && date_range.end) {
            _fullDateRange = { start: date_range.start, end: date_range.end };
            window._fullDateRange = _fullDateRange;

            const sEl = document.getElementById('startDate');
            const eEl = document.getElementById('endDate');
            sEl.value = date_range.start;
            sEl.setAttribute('data-full', date_range.start);
            eEl.value = date_range.end;
            eEl.setAttribute('data-full', date_range.end);

            setStatus('ready', `已加载 ${available_dates.length} 个交易日 (${date_range.start} ~ ${date_range.end})`);

            // 有数据就自动查询
            doQuery();
        } else {
            // 没有本地数据 — 设置默认日期为今天和一个月前
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
            const monthAgo = new Date(now.getFullYear(), now.getMonth()-1, now.getDate());

            document.getElementById('startDate').value = fmt(monthAgo);
            document.getElementById('endDate').value = fmt(now);

            if (!output_dir_exists) {
                setStatus('error', `输出目录不存在 (${data.output_dir || '-'})，请先登录下载数据`);
            } else if (!has_data) {
                setStatus('ready', '输出目录为空，请登录后下载交易数据');
            } else {
                setStatus('ready', '暂无本地数据，请选择日期后查询（登录后可自动下载）');
            }

            // 无数据时强制弹出登录框（无论是否已登录，都需要登录来下载数据）
            if (!_isLoggedIn) {
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
        if (data.missing_count > 0) {
            completeStep(1, `⚠ ${data.missing_count}天缺失`);

            // 如果后端提示需要登录才能下载数据
            if (data.need_login) {
                hideProgress();
                _isQuerying = false;
                queryBtn.disabled = false;
                queryBtn.textContent = '🔍 查询复盘';

                // 弹出确认框，用户确认后打开登录模态框
                const confirmLogin = confirm(
                    `发现 ${data.missing_count} 个交易日的数据缺失，\n` +
                    `需要登录监控中心才能自动下载。\n\n` +
                    `是否立即登录？`
                );
                if (confirmLogin) {
                    handleLoginClick();
                }
                return; // 中止后续渲染，等用户登录后再查
            }
        } else {
            completeStep(1, '✓ 数据完整');
        }

        // Step 2: 分析 & 渲染
        activateStep(2);
        updateProgress(50, '正在分析交易数据...');

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
            setStatus('ready', `查询完成 · ${data.overview['交易日数']} 个交易日`);
            document.getElementById('queryInfo').textContent = data.overview['查询范围'] || `${start} ~ ${end}`;
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
    renderSymbolTable(data.symbol_summary);
    renderTopTrades(data.trades_sorted);
    renderPnlOverview(data.symbol_summary);  // 盈亏纵览表格
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

    // 总盈亏 = 期货盈亏 + 期权盈亏
    const totalPnl = (ov['期货盈亏(元)'] || 0) + (ov['期权盈亏(元)'] || 0);
    const tp = fmtColored(totalPnl);
    set('ovTotalPnl', tp.text, tp.cls);

    // 分项显示
    const pnl = fmtColored(ov['期货盈亏(元)']);
    set('ovPnl', pnl.text, pnl.cls);

    const optPnl = fmtColored(ov['期权盈亏(元)']);
    set('ovOptPnl', optPnl.text, optPnl.cls);

    set('ovCommission', fmtNum(ov['期货手续费(元)']));

    // 净盈亏 = 总盈亏 - 手续费
    const net = fmtColored(totalPnl - (ov['期货手续费(元)'] || 0));
    set('ovNetPnl', net.text, net.cls);
}

// ---- Symbol Summary Table ----
function renderSymbolTable(rows) {
    const tbody = document.getElementById('symbolTbody');
    if (!rows || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><p>暂无数据</p></td></tr>';
        return;
    }

    tbody.innerHTML = rows.map(r => {
        const fp = r['期货盈亏(元)'];
        const op = r['期权盈亏(元)'];
        const tp = r['总盈亏(元)'];
        const fpCls = fp > 0 ? 'positive' : fp < 0 ? 'negative' : '';
        const opCls = op > 0 ? 'positive' : op < 0 ? 'negative' : '';
        const tpCls = tp > 0 ? 'positive' : tp < 0 ? 'negative' : '';
        return `<tr class="clickable" onclick="showSymbolDetail('${r['品种']}')">
            <td><a class="symbol-link">${r['品种']}</a></td>
            <td>${r['手数']}</td>
            <td class="${fpCls}">${(fp >= 0 ? '+' : '') + fmtNum(fp)}</td>
            <td>${fmtNum(r['期货手续费(元)'])}</td>
            <td class="${opCls}">${(op >= 0 ? '+' : '') + fmtNum(op)}</td>
            <td>${fmtNum(r['期权手续费(元)'])}</td>
            <td class="${tpCls}">${(tp >= 0 ? '+' : '') + fmtNum(tp)}</td>
            <td>${fmtNum(r['总手续费(元)'])}</td>
        </tr>`;
    }).join('');
}

// ---- TOP Trades ----
function renderTopTrades(trades) {
    if (!trades || trades.length === 0) return;

    const winsBody = document.getElementById('topWinsBody');
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

    const lossesBody = document.getElementById('topLossesBody');
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

// ---- 盈亏纵览表格（替代原来的柱状图）----
function renderPnlOverview(rows) {
    const tbody = document.getElementById('pnlOverviewBody');
    if (!rows || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><p>暂无数据，请先查询</p></td></tr>';
        return;
    }

    // 按总盈亏排序
    const sorted = [...rows].sort((a, b) => (b['总盈亏(元)'] || 0) - (a['总盈亏(元)'] || 0));

    // 计算汇总
    let totalFuturesPnl = 0, totalFuturesComm = 0, totalOptPnl = 0, totalOptComm = 0, totalAllPnl = 0, totalAllComm = 0;
    sorted.forEach(r => {
        totalFuturesPnl += (r['期货盈亏(元)'] || 0);
        totalFuturesComm += (r['期货手续费(元)'] || 0);
        totalOptPnl += (r['期权盈亏(元)'] || 0);
        totalOptComm += (r['期权手续费(元)'] || 0);
        totalAllPnl += (r['总盈亏(元)'] || 0);
        totalAllComm += (r['总手续费(元)'] || 0);
    });

    tbody.innerHTML = sorted.map((r, idx) => {
        const fp = r['期货盈亏(元)'];
        const op = r['期权盈亏(元)'];
        const tp = r['总盈亏(元)'];
        const fpCls = fp > 0 ? 'positive' : fp < 0 ? 'negative' : '';
        const opCls = op > 0 ? 'positive' : op < 0 ? 'negative' : '';
        const tpCls = tp > 0 ? 'positive' : tp < 0 ? 'negative' : '';

        let rankBadge = `<span class="rank-num">${idx + 1}</span>`;
        if (idx === 0 && tp > 0) rankBadge = '<span class="rank-num rank-gold">🥇</span>';
        else if (idx === 1 && tp > 0) rankBadge = '<span class="rank-num rank-silver">🥈</span>';
        else if (idx === 2 && tp > 0) rankBadge = '<span class="rank-num rank-bronze">🥉</span>';

        return `<tr class="clickable" onclick="showSymbolDetail('${r['品种']}')">
            <td style="text-align:center">${rankBadge}</td>
            <td style="font-weight:700">${r['品种']}</td>
            <td>${r['手数']}</td>
            <td class="${fpCls}">${(fp >= 0 ? '+' : '') + fmtNum(fp)}</td>
            <td>${fmtNum(r['期货手续费(元)'])}</td>
            <td class="${opCls}">${(op >= 0 ? '+' : '') + fmtNum(op)}</td>
            <td>${fmtNum(r['期权手续费(元)'])}</td>
            <td class="${tpCls}" style="font-weight:600">${(tp >= 0 ? '+' : '') + fmtNum(tp)}</td>
            <td>${fmtNum(r['总手续费(元)'])}</td>
        </tr>`;
    }).join('');

    // 汇总行
    const totalCls = totalAllPnl >= 0 ? 'positive' : 'negative';
    const summaryRow = document.createElement('tr');
    summaryRow.className = 'summary-row';
    summaryRow.innerHTML = `
        <td colspan="3" style="text-align:right;font-weight:700;">合计 (${sorted.length} 个品种)</td>
        <td class="${totalFuturesPnl >= 0 ? 'positive' : 'negative'}">${(totalFuturesPnl >= 0 ? '+' : '') + fmtNum(totalFuturesPnl)}</td>
        <td>${fmtNum(totalFuturesComm)}</td>
        <td class="${totalOptPnl >= 0 ? 'positive' : 'negative'}">${(totalOptPnl >= 0 ? '+' : '') + fmtNum(totalOptPnl)}</td>
        <td>${fmtNum(totalOptComm)}</td>
        <td class="${totalCls}" style="font-weight:700">${(totalAllPnl >= 0 ? '+' : '') + fmtNum(totalAllPnl)}</td>
        <td>${fmtNum(totalAllComm)}</td>`;
    tbody.appendChild(summaryRow);
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

        updateProgress(98, '图表渲染完成');
    } catch (e) {
        console.warn('图表数据加载失败:', e);
    }
}

// ---- 每日趋势图（纵向：柱子横排，日期在Y轴）----
function renderTrendChart(trendData) {
    const dom = document.getElementById('chartTrend');
    if (!dom || !trendData || !trendData.length) return;
    const chart = echarts.init(dom);

    const dates = trendData.map(d => d.date);
    const dailyPnl = trendData.map(d => d.pnl - d.commission);
    const cumulative = trendData.map((d, i) =>
        dailyPnl.slice(0, i + 1).reduce((a, b) => a + b, 0)
    );

    chart.setOption({
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
            textStyle: { color: '#fff' }
        },
        legend: {
            data: ['每日净盈亏', '累计净盈亏'],
            top: 0,
            textStyle: { color: '#fff' }
        },
        grid: { left: 70, right: 70, top: 40, bottom: 20 },
        xAxis: {
            type: 'value',
            axisLabel: { formatter: v => (v / 10000).toFixed(1) + 'w', color: '#fff' },
            axisLine: { lineStyle: { color: '#555' } },
            splitLine: { lineStyle: { color: '#333' } }
        },
        yAxis: {
            type: 'category',
            data: dates.slice().reverse(),
            axisLabel: { fontSize: 11, color: '#fff' },
            axisLine: { lineStyle: { color: '#555' } },
        },
        series: [
            {
                name: '每日净盈亏',
                type: 'bar',
                data: dailyPnl.slice().reverse().map(v => ({
                    value: v,
                    itemStyle: { color: v >= 0 ? '#e74c3c' : '#27ae60' }
                })),
                barWidth: '60%'
            },
            {
                name: '累计净盈亏',
                type: 'line',
                data: cumulative.slice().reverse(),
                smooth: true,
                lineStyle: { color: '#3b82f6', width: 2.5 },
                itemStyle: { color: '#3b82f6' },
                areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
                    colorStops: [{ offset: 0, color: 'rgba(59,130,246,0)' }, { offset: 1, color: 'rgba(59,130,246,0.15)' }]
                }}
            }
        ]
    });
    window.addEventListener('resize', () => chart.resize());
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
        ['期货盈亏(元)', stats['期货盈亏(元)']], ['期货净盈亏(元)', stats['期货净盈亏(元)']],
        ['期权盈亏(元)', stats['期权盈亏(元)']],
        ['期货手续费(元)', stats['期货手续费(元)']], ['期权手续费(元)', stats['期权手续费(元)']],
        ['胜率', stats['胜率']], ['盈亏比', stats['盈亏比']],
        ['均赢(元)', stats['均赢(元)']], ['均亏(元)', stats['均亏(元)']],
        ['最大单笔盈利(元)', stats['最大单笔盈利(元)']], ['最大单笔亏损(元)', stats['最大单笔亏损(元)']],
    ];

    document.getElementById('modalStats').innerHTML = fields.map(([label, val]) => {
        const colored = ['总盈亏(元)', '净盈亏(元)', '均赢(元)', '均亏(元)',
                        '最大单笔盈利(元)', '最大单笔亏损(元)'].includes(label);
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

// ---- 期权明细 ----
function renderDetailOptions(options) {
    const tbody = document.getElementById('detailOptsBody');
    const tfoot = document.getElementById('detailOptsFoot');

    if (!options || options.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><p>暂无期权成交记录</p></td></tr>';
        tfoot.innerHTML = '';
        return;
    }

    // 统计盈亏
    let totalPnl = 0, totalCommission = 0, totalPremium = 0;
    let winCount = 0, lossCount = 0, winPnl = 0, lossPnl = 0;

    tbody.innerHTML = options.map(o => {
        const pnl = o.pnl || 0;
        totalPnl += pnl;
        totalCommission += (o.commission || 0);
        totalPremium += (o.premium_total || 0);
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
        <td>${fmtNum(totalPremium)}</td>
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
// 启动
// ============================================================
document.addEventListener('DOMContentLoaded', initDates);