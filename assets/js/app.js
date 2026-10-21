/* =========================================================
   应用逻辑：数据接入 / 路由 / 主题 / 渲染 / 双窗口协同
   数据来源：本机 WorkBuddy 会话记录（经 Python 侧 usage_source 聚合）
   ========================================================= */
(function () {
  'use strict';

  const C = window.Charts;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const qs = new URLSearchParams(location.search);

  const HASH = location.hash.replace('#', '').split('&').filter(Boolean);
  const HASH_MODE = HASH.find(x => x === 'popup' || x === 'main') || null;
  const HASH_VIEW = HASH.find(x => ['overview', 'analytics', 'ledger', 'settings'].includes(x)) || null;

  const TITLES = {
    overview: ['总览', 'WorkBuddy 账号的积分余额与今日使用情况'],
    analytics: ['用量分析', '消耗日历、模型用量与项目排行的真实拆解'],
    ledger: ['积分流水', 'WorkBuddy 逐次调用的真实积分扣减记录'],
    settings: ['设置', '账号、主题、动效、刷新节奏与数据源']
  };

  const state = {
    mode: HASH_MODE || (qs.get('mode') === 'popup' ? 'popup' : 'main'),
    view: HASH_VIEW || 'overview',
    range: 7,
    ledgerType: 'all',
    ledgerQuery: '',
    ledgerLimit: 24,
    popQuery: ''
  };

  /* ---------------- 数字格式 ---------------- */

  const F = {
    pad: n => String(n).padStart(2, '0'),
    fmt(n, d) {
      const v = Number(n) || 0;
      return v.toLocaleString('zh-CN', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
    },
    /** 积分：最多两位小数，整数不带小数点 */
    credit(v) {
      const n = Number(v) || 0;
      const dec = Math.abs(n) >= 1000 ? 0 : (Number.isInteger(n) ? 0 : 2);
      return n.toLocaleString('zh-CN', { minimumFractionDigits: dec, maximumFractionDigits: dec });
    },
    fmtWan(n) {
      const v = Number(n) || 0;
      if (v >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
      if (v >= 1e4) return (v / 1e4).toFixed(1) + ' 万';
      return F.fmt(v);
    },
    dateKey(d) {
      if (!d) return '—';
      if (typeof d === 'string') return d.length >= 10 ? d.slice(5, 10) : d;
      return `${F.pad(d.getMonth() + 1)}-${F.pad(d.getDate())}`;
    },
    clock(d) {
      if (!(d instanceof Date)) return '—';
      return `${F.pad(d.getHours())}:${F.pad(d.getMinutes())}`;
    }
  };

  /* ---------------- 偏好 ---------------- */

  const PREF_KEY = 'pi.prefs.v1';
  const prefs = Object.assign(
    { theme: 'light', motion: 'full', countUp: true, labels: true, tray: true,
      holiday: true, signin: true, localRefresh: 60 },
    readPrefs()
  );

  function readPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); } catch (e) { return {}; }
  }
  function savePrefs() {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) { /* file:// 忽略 */ }
  }

  const systemDark = () => window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const resolved = () => (prefs.theme === 'system' ? (systemDark() ? 'dark' : 'light') : prefs.theme);
  const motionOff = () => prefs.motion === 'off';

  /* ---------------- 桌面桥接 ---------------- */

  const bridge = {
    api() { return (window.pywebview && window.pywebview.api) || null; },
    has() { const a = this.api(); return !!(a && typeof a.get_usage === 'function'); },
    call(name, ...args) {
      const api = this.api();
      if (!api || typeof api[name] !== 'function') return null;
      try { return api[name](...args); } catch (e) { return null; }
    },
    expand() {
      if (this.call('expand')) return;
      setMode('main');
    },
    collapse() {
      if (this.call('collapse')) return;
      setMode('popup');
    },
    hide() {
      if (this.call('hide')) return;
      toast('浏览器预览模式无法收起到托盘', 'i-info');
    },
    syncTheme(theme) { this.call('sync_theme', theme); }
  };

  /* ---------------- 数据 ---------------- */

  let D = normalize(window.APP_DATA || {});
  let source = 'demo';
  let ready = false;
  let ACCOUNT = { status: 'disconnected', cloud: null, interval: 60 };

  function normalize(raw) {
    const d = raw || {};
    d.meta = d.meta || {};
    d.meta.now = d.meta.now ? new Date(d.meta.now) : new Date();
    d.meta.records = d.meta.records || 0;
    d.meta.days = d.meta.days || 0;
    d.account = d.account || { total: 0, used: 0, available: 0, usedPct: 0, configured: false };
    d.account.cycleEnd = d.account.cycleEnd || '';
    d.today = d.today || { consumed: 0, earned: 0, requests: 0, tokens: 0, avgPerRequest: 0, deltaPct: 0 };
    d.ledger = (d.ledger || []).map(e => Object.assign({}, e, {
      time: e.time instanceof Date ? e.time : new Date(e.time),
      tone: e.tone || 'c1'
    }));
    d.hourly = d.hourly || [];
    d.models = d.models || [];
    d.tasks = d.tasks || [];
    d.projects = d.projects || [];
    d.projects7 = d.projects7 || d.projects || [];
    d.daily = d.daily || [];
    d.week = d.week || [];
    d.heat = d.heat || [];
    d.taskDefs = d.taskDefs || [];
    d.insights = d.insights || [];
    return d;
  }

  function applyUsage(raw, silent) {
    D = normalize(raw);
    window.APP_DATA = D;
    source = (D.meta && D.meta.source === 'local') ? 'real' : 'demo';
    if (!silent) renderActive(true);
  }

  async function loadUsage() {
    if (!bridge.has()) return false;
    try {
      const raw = await bridge.call('get_usage');
      if (raw && raw.meta) { applyUsage(raw, true); return true; }
    } catch (e) { console.warn('读取用量失败', e); }
    return false;
  }

  const hasQuota = () => !!(effAccount() && effAccount().configured);

  /** 换数据目录 / 手动同步后统一刷一次（本地 + 云端 + 重绘） */
  async function reloadAll() {
    await loadAccount();
    await loadUsage();
    renderActive(true);
  }

  async function loadAccount() {
    if (!bridge.has()) return false;
    try {
      const a = await bridge.call('get_account');
      if (a) { ACCOUNT = a; return true; }
    } catch (e) { /* ignore */ }
    return false;
  }

  /* ---------------- 可用额度折算（工作日 / 签到） ---------------- */

  function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
  function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
  function diffDays(a, b) { return Math.round((startOfDay(b) - startOfDay(a)) / 86400000); }
  function parseDate(v) {
    if (!v) return null;
    const m = String(v).match(/(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3]);
  }
  function isWorkday(d, useHoliday) {
    const w = d.getDay();
    if (w === 0 || w === 6) return false;
    if (useHoliday && window.HOLIDAYS && HOLIDAYS.has(d)) return false;
    return true;
  }
  function countWorkdays(from, to, useHoliday) {
    let n = 0;
    for (let d = startOfDay(from); d <= startOfDay(to); d = addDays(d, 1)) {
      if (isWorkday(d, useHoliday)) n++;
    }
    return n;
  }

  /** 按"剩余工作日"折算每天/每周可用积分 */
  function allowance(acc) {
    const total = acc.total || 0;
    const available = acc.available || 0;
    const end = parseDate(acc.expireAt) || addDays(new Date(), 30);
    const today = startOfDay(new Date());
    const leftDays = Math.max(1, diffDays(today, end) + 1);
    const useHoliday = prefs.holiday !== false;
    const leftWorkdays = Math.max(1, countWorkdays(today, end, useHoliday));
    const signinOn = prefs.signin !== false;
    const signin = signinOn ? Math.max(0, 100 * leftDays) : 0;
    const pool = available + signin;
    const perDay = pool / leftWorkdays;
    const perWeek = perDay * Math.min(5, leftWorkdays);
    return { perDay, perWeek, leftWorkdays, leftDays, pool, signin, end, total, available };
  }

  /** 云端账户优先，本地记录兜底 */
  function effAccount() {
    const base = (D && D.account) || {};
    const c = (ACCOUNT && ACCOUNT.status === 'connected' && ACCOUNT.cloud) ? ACCOUNT.cloud : null;
    if (!c || (c.total == null && c.available == null && c.used == null)) return base;
    const total = (c.total != null) ? c.total : base.total;
    const used = (c.used != null) ? c.used
      : (total != null && c.available != null ? Math.max(0, total - c.available) : base.used);
    const available = (c.available != null) ? c.available
      : Math.max(0, (total || 0) - (used || 0));
    const configured = !!(total > 0);
    return Object.assign({}, base, {
      total, used, available, configured,
      usedPct: configured ? used / total : 0,
      cycleStart: c.cycleStartText || base.cycleStart || '',
      cycleEnd: c.cycleEndText || base.cycleEnd || '',
      expireAt: c.expireAtText || '',
      source: 'cloud'
    });
  }

  /* ---------------- 主题 ---------------- */

  let lastPainted = null;      // 上次真正落地的主题（此前漏声明，strict 模式下 paintTheme 首行即抛错）
  let themeTimer = null;

  /**
   * 主题切换：整页 260ms 交叉淡入淡出（View Transitions），
   * 不做任何从某点扩散的全屏遮罩，避免"被盖住"的观感。
   *
   * 注意：View Transitions 的更新回调要等"下一帧"才执行，而 WebView2 在关闭 GPU 合成
   * 或窗口未聚焦时可能长时间不产帧。因此这里加 60ms 看门狗：无论过渡是否真的开始，
   * 主题都会立即落地，绝不会出现"点了没反应"。
   */
  function applyTheme(pref, animate) {
    const next = pref === 'system' ? (systemDark() ? 'dark' : 'light') : pref;
    const commit = () => paintTheme(next, true);
    if (!animate || motionOff() || typeof document.startViewTransition !== 'function') {
      paintTheme(next);
      return;
    }
    let applied = false;
    const once = () => { if (!applied) { applied = true; commit(); } };
    try {
      const vt = document.startViewTransition(once);
      setTimeout(once, 60);                      // 兜底：回调不执行也照样切换
      if (vt && vt.finished) {
        vt.finished.catch(() => {}).finally(() => {
          once();
          clearTimeout(themeTimer);
          themeTimer = setTimeout(() => renderActive(true), 40);
        });
      }
      clearTimeout(themeTimer);
      themeTimer = setTimeout(() => renderActive(true), 320);
    } catch (e) {
      once();
      setTimeout(() => renderActive(true), 30);
    }
  }

  function paintTheme(theme, skipRender, fromSync) {
    if (theme === lastPainted && fromSync) return;   // 被动同步且无变化：直接忽略
    lastPainted = theme;
    document.documentElement.setAttribute('data-theme', theme);
    const targetIcon = theme === 'dark' ? '#i-sun' : '#i-moon';
    $$('#themeBtn use, #popTheme use').forEach(u => u.setAttribute('href', targetIcon));
    const label = $('#themeSwitchLabel');
    if (label) label.textContent = theme === 'dark' ? '浅色模式' : '深色模式';
    const sw = $('#themeSwitch');
    if (sw) sw.setAttribute('aria-checked', theme === 'dark' ? 'true' : 'false');
    $$('#themeCards .theme-card').forEach(card => {
      card.classList.toggle('is-active', card.dataset.themeChoice === prefs.theme);
    });
    // 图表颜色全部来自 CSS 变量：主题一变必须重绘，否则会出现"深色底 + 浅色旧图"
    clearTimeout(themeTimer);
    themeTimer = setTimeout(() => renderActive(true), skipRender ? 90 : 60);
    if (!fromSync) bridge.syncTheme(theme);          // 只有主动切换才通知另一端
  }

  function setTheme(pref, originEl) {
    prefs.theme = pref;
    savePrefs();
    applyTheme(pref, !motionOff());
  }

  /* ---------------- 动效 / 计数 ---------------- */

  function setMotion(level) {
    prefs.motion = level;
    savePrefs();
    document.documentElement.setAttribute('data-motion', level);
    $$('#motionSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.motionChoice === level));
    renderActive();
    toast(`动效已设为「${level === 'full' ? '完整' : level === 'reduced' ? '精简' : '关闭'}」`);
  }

  /**
   * 数值落地。
   * 说明：WebView2 在窗口未获得焦点时可能整体挂起渲染线程，rAF/定时器都不推进，
   * 数字滚动会永远停在 0。数值正确性优先，因此这里直接写入终值；
   * 入场动效交给 CSS（卡片位移、图表扫描等合成层动画）。
   */
  function countUp(node, target, opts) {
    if (!node) return;
    const o = opts || {};
    const format = o.format || (v => F.fmt(v, o.decimals || 0));
    node.textContent = format(Number(target) || 0);
  }

  /* ---------------- 通用 ---------------- */

  function setText(sel, v) { const n = $(sel); if (n) n.textContent = v; }
  function setHTML(sel, v) { const n = $(sel); if (n) n.innerHTML = v; }

  function renderSidebar() {
    const acc = effAccount();
    const configured = hasQuota();
    const plan = (ACCOUNT && ACCOUNT.user && ACCOUNT.user.plan) || '本地记录';

    setText('#sidePlan', plan);
    setText('#sideLeft', configured ? F.credit(acc.available) : F.credit(acc.used));
    const unit = document.querySelector('.side-quota-num span');
    if (unit) unit.textContent = configured ? `/ ${F.credit(acc.total)} 分` : '分 · 本周期消耗';
    const pct = configured ? Math.min(1, acc.usedPct) : 0;
    const bar = $('#sideProgress');
    if (bar) requestAnimationFrame(() => { bar.style.width = (pct * 100).toFixed(2) + '%'; });
    setText('#sideUsedPct', configured ? (pct * 100).toFixed(1) + '%' : '—');

    if (configured) {
      const al = allowance(acc);
      setText('#sideCycle', `剩 ${al.leftWorkdays} 工作日 · 日约 ${F.credit(al.perDay)}`);
    } else {
      setText('#sideCycle', '未连接云端');
    }
  }

  let lastRender = -1e9;
  function renderActive(force) {
    const now = performance.now();
    if (!force && now - lastRender < 250) return;
    lastRender = now;
    if (state.mode === 'popup') { renderPopup(); return; }
    renderSidebar();
    if (state.view === 'overview') renderOverview();
    else if (state.view === 'analytics') renderAnalytics();
    else if (state.view === 'ledger') renderLedger(true);
    else if (state.view === 'settings') renderSettings();
  }

  let localTimer = null;
  /* ---------------- 总览提示条 ---------------- */

  /** 顶部那条明确提示：说清"哪个数字是实时的、哪个有延迟、当前读的是哪份数据"。 */
  function renderNotice() {
    const el = $('#noticeText');
    if (!el) return;
    const connected = ACCOUNT && ACCOUNT.status === 'connected';
    const sync = ACCOUNT && ACCOUNT.lastSync ? String(ACCOUNT.lastSync).slice(5, 16) : '';
    const iv = ACCOUNT && ACCOUNT.interval;
    const every = iv === 0 ? '手动' : (iv ? iv + ' 分钟' : '60 分钟');
    let txt;
    if (connected) {
      txt = `云端<b>每 ${every}同步一次</b>（上次 ${sync || '—'}，有延迟）；`
          + `今日消耗 / 模型 / 项目取<b>本机记录</b>，实时。`;
    } else {
      txt = `当前只有<b>本机记录</b>：消耗与 Token 实时，但<b>不含账号余额</b>；`
          + `到「设置 → 账号与云端」扫码后可显示可用积分与周期。`;
    }
    if (D.meta && D.meta.sourceKind === 'db') {
      txt += ' 未找到会话明细，已按 <b>workbuddy.db</b> 估算。';
    }
    el.innerHTML = txt;
  }

  /* ---------------- 版本号（每次提交自动递增） ---------------- */

  let appVersion = 'v1.0.0';

  async function loadVersion() {
    try {
      if (!bridge.has()) return;
      const r = await bridge.call('get_version');
      if (r && r.version) {
        appVersion = 'v' + String(r.version).replace(/^v/, '');
        setText('#appVersion', appVersion);
        setText('#sideVersion', appVersion);
        setText('#aboutVersion', appVersion);
        document.title = `WorkBuddy 积分观测台 ${appVersion}`;
      }
    } catch (e) { /* ignore */ }
  }

  /* ---------------- 窗口状态（标题栏/缩放共用） ---------------- */

  let winMaxed = false;

  function applyWinState(st) {
    if (!st) return;
    winMaxed = !!st.maximized;
    const btn = $('#winMax');
    if (btn) {
      const use = btn.querySelector('use');
      if (use) use.setAttribute('href', winMaxed ? '#i-restore' : '#i-max');
      btn.title = winMaxed ? '还原窗口' : '最大化';
      btn.setAttribute('aria-label', winMaxed ? '还原' : '最大化');
    }
    document.body.classList.toggle('is-maximized', winMaxed);
  }

  async function readWinState() {
    if (!bridge.has()) return;
    try { applyWinState(await bridge.call('win_state')); } catch (e) { /* ignore */ }
  }

  /* ---------------- 自绘窗口缩放（窗口已无系统边框） ---------------- */


  function setupResize() {
    const box = $('#rz');
    if (!box || state.mode !== 'main') return;   // bridge 在 bind 时可能还没就绪，这里不依赖它
    let st = null, pending = null, timer = 0, last = 0;

    const flush = () => {
      timer = 0;
      last = performance.now();
      const p = pending;
      pending = null;
      if (p) bridge.call('win_resize', p[0], p[1], p[2], p[3]);
    };
    const queue = (x, y, w, h) => {
      pending = [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
      if (timer) return;
      timer = setTimeout(flush, Math.max(0, 12 - (performance.now() - last)));
    };

    box.querySelectorAll('[data-rz]').forEach(handle => {
      handle.addEventListener('mousedown', async e => {
        if (e.button !== 0) return;
        e.preventDefault();
        const pos = await bridge.call('win_pos');
        if (!pos) return;
        st = { x: pos.x, y: pos.y, w: pos.w, h: pos.h, sx: e.screenX, sy: e.screenY, dir: handle.dataset.rz };
        document.body.classList.add('is-resizing');
      });
    });

    window.addEventListener('mousemove', e => {
      if (!st) return;
      const dx = e.screenX - st.sx;
      const dy = e.screenY - st.sy;
      let { x, y, w, h } = st;
      if (st.dir.includes('e')) w += dx;
      if (st.dir.includes('s')) h += dy;
      if (st.dir.includes('w')) { x += dx; w -= dx; }
      if (st.dir.includes('n')) { y += dy; h -= dy; }
      if (w < 1024) { if (st.dir.includes('w')) x = st.x + st.w - 1024; w = 1024; }
      if (h < 680) { if (st.dir.includes('n')) y = st.y + st.h - 680; h = 680; }
      queue(x, y, w, h);
    });

    window.addEventListener('mouseup', () => {
      if (!st) return;
      st = null;
      document.body.classList.remove('is-resizing');
      setTimeout(readWinState, 120);
    });
  }

  function setupLocalRefresh() {
    if (localTimer) { clearInterval(localTimer); localTimer = null; }
    const sec = +prefs.localRefresh;
    if (!sec || sec <= 0) return;
    localTimer = setInterval(async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        if (bridge.has()) {
          const raw = await bridge.call('refresh_usage');
          if (raw && raw.meta) {
            applyUsage(raw, true);
            if (state.mode === 'main') renderActive(true);
            else renderPopup();
          }
        }
      } catch (e) { /* ignore */ }
    }, sec * 1000);
  }

  function renderPopup() {
    const today = D.today, acc = effAccount();
    const configured = hasQuota();
    // 大字 = 今日使用积分；可用积分降为辅助小字（原顺序相反，主次不清）
    setText('#popAvailLabel', '今日使用积分');
    countUp($('#popTodayUsed'), today.consumed, { duration: 760, format: F.credit });
    setText('#popAvailable', configured ? F.credit(acc.available) : '—');
    setText('#popUsedPct', configured ? (acc.usedPct * 100).toFixed(1) + '%' : '未设置');

    // 延迟说明：云端多久之前同步的
    const syncAt = ACCOUNT && ACCOUNT.lastSync ? new Date(String(ACCOUNT.lastSync).replace(' ', 'T')) : null;
    const mins = syncAt && !isNaN(syncAt.getTime()) ? Math.max(0, Math.round((Date.now() - syncAt.getTime()) / 60000)) : null;
    setText('#popSyncAt', mins == null ? '尚未同步'
      : mins < 1 ? '刚刚' : mins < 60 ? `${mins} 分钟前` : `${Math.floor(mins / 60)} 小时前`);

    const al = allowance(acc);
    setText('#popCycle', configured
      ? `剩 ${al.leftWorkdays} 个工作日 · 每日约 ${F.credit(al.perDay)}`
      : '连接账号后自动计算');

    setText('#popTotal', `共 ${F.credit(today.consumed)} 分`);
    setText('#popUpdated', `${source === 'real' ? '本机' : '演示'} ${D.meta.updated || '—'}`);
    setText('#popRecentCount', `${Math.min(4, (D.ledger || []).length)} 条`);
    const popCloud = $('#popCloud');
    if (popCloud) {
      const st = (ACCOUNT && ACCOUNT.lastSync) ? String(ACCOUNT.lastSync).slice(11, 16) : '';
      popCloud.textContent = st ? `云端 ${st}` : '同步云端';
      popCloud.title = st ? `云端上次同步 ${ACCOUNT.lastSync}，点击立即同步` : '点击同步云端账号（手动一次，不改变自动间隔）';
    }

    const delta = $('#popDelta');
    if (delta) {
      const up = today.deltaPct >= 0;
      delta.className = 'delta' + (up ? '' : ' is-good');
      delta.innerHTML = `<svg class="i"><use href="#${up ? 'i-up' : 'i-down'}"/></svg>${Math.abs(today.deltaPct || 0).toFixed(1)}%`;
    }
    const prog = $('#popProgress');
    if (prog) requestAnimationFrame(() => { prog.style.width = ((configured ? acc.usedPct : 0) * 100).toFixed(2) + '%'; });

    const bar = $('#popCompBar'), list = $('#popCompList'), donut = $('#popDonut');
    const compRows = (D.models || []).filter(m => m.points > 0);
    const compSum = compRows.reduce((a, b) => a + b.points, 0) || today.consumed || 1;
    if (donut) {
      if (compRows.length) {
        C.donut(donut, {
          items: compRows.map(m => ({ name: m.name, value: m.points, tone: m.tone })),
          legend: false,
          size: 84,
          hRatio: 0.80,
          valueOutside: true,
          centerLabel: '今日',
          centerUnit: '分',
          // 四位数以上不再带小数，避免环内文字溢出压到圆环
          centerFormat: v => (v >= 1000 ? F.fmt(Math.round(v)) : F.credit(v))
        });
      } else {
        donut.innerHTML = '<div class="comp-empty" style="font-size:10px;text-align:center">今日暂无消耗</div>';
      }
    }
    if (bar) {
      bar.innerHTML = compRows.map(m =>
        `<i style="width:${((m.points / compSum) * 100).toFixed(2)}%;background:${C.toneColor(m.tone)}"></i>`).join('');
    }

    // 今日时段（小窗内保持一份极简柱图，填满垂直空间）
    const popBars = $('#popBars');
    if (popBars) {
      C.bar(popBars, {
        data: (D.hourly || []).map(hr => ({ label: hr.label, short: F.pad(hr.hour), value: hr.points })),
        tone: 'c1', minimal: true, showValues: false,
        sum: today.consumed, maxBarW: 14
      });
    }
    setText('#popPeak', D.peakPoints ? `峰值 ${F.pad(D.peakHour)}:00 · ${F.credit(D.peakPoints)} 分` : '—');
    if (list) {
      list.innerHTML = compRows.slice(0, 3).map(m => `
        <div class="comp-row comp-row--sm">
          <i class="dot" style="background:${C.toneColor(m.tone)}"></i>
          <span class="nm" title="${m.name}">${m.name}</span>
          <span class="val">${F.credit(m.points)}</span>
          <span class="pct">${((m.points / compSum) * 100).toFixed(0)}%</span>
        </div>`).join('') || '<div class="comp-empty">今日暂无消耗</div>';
    }

    renderPopupList();
  }

  function renderPopupList() {
    const box = $('#popList');
    if (!box) return;
    const q = (state.popQuery || '').trim().toLowerCase();
    const rows = (D.ledger || [])
      .filter(e => !q || [e.model, e.project, e.task].join(' ').toLowerCase().includes(q))
      .slice(0, 4);
    box.innerHTML = rows.map(e => {
      const t = e.time instanceof Date ? e.time : new Date(e.time);
      return `<div class="pop-row">
        <i class="swatch" style="background:${C.toneColor(e.tone)}"></i>
        <span class="nm"><b>${e.model}</b><span class="tm">${F.clock(t)}</span></span>
        <span class="pt">-${F.credit(e.points)}</span>
      </div>`;
    }).join('') || '<div class="pop-empty">暂无记录</div>';
  }

  /** 消耗构成：环形图 + 横向堆叠条 + 明细列表（三者同一数据源，合计可对） */
  function renderComposition(items, totalAll) {
    const bar = $('#compBar'), list = $('#compList'), donutBox = $('#compDonut');
    const rows = (items || []).filter(m => m.points > 0);
    const sum = rows.reduce((a, b) => a + b.points, 0) || totalAll || 1;

    if (donutBox) {
      if (rows.length) {
        C.donut(donutBox, {
          items: rows.map(m => ({ name: m.name, value: m.points, tone: m.tone })),
          layout: 'bottom',
          legend: false,
          size: 232,
          hRatio: 0.84,
          valueOutside: true,               // 数字放环外，避免长数字压到圆环
          centerLabel: rows.length > 1 ? '今日合计' : '单一模型',
          centerUnit: '分',
          centerFormat: v => F.credit(v)
        });
      } else {
        donutBox.innerHTML = '<div class="comp-empty">今日暂无消耗</div>';
      }
    }

    if (bar) {
      bar.innerHTML = rows.map(m => {
        const pct = (m.points / sum) * 100;
        return `<i style="width:${pct.toFixed(2)}%;background:${C.toneColor(m.tone)}" title="${m.name} ${F.credit(m.points)} 分"></i>`;
      }).join('');
    }

    if (list) {
      list.innerHTML = rows.map(m => {
        const pct = (m.points / sum) * 100;
        return `<div class="comp-row">
          <i class="dot" style="background:${C.toneColor(m.tone)}"></i>
          <span class="nm" title="${m.name}">${m.name}</span>
          <span class="val">${F.credit(m.points)}</span>
          <span class="pct">${pct.toFixed(1)}%</span>
        </div>`;
      }).join('') || '<div class="comp-empty">今日暂无消耗</div>';
    }
  }

  function renderOverview() {
    const acc = effAccount(), today = D.today;
    const configured = hasQuota();

    renderNotice();
    setText('#kpi1Label', configured ? '可用积分' : '本周期消耗');
    countUp($('[data-count="available"]'), configured ? acc.available : acc.used, { format: F.credit });
    setText('#kpiAvailSub', configured ? `已用 ${(acc.usedPct * 100).toFixed(1)}%` : `累计 ${D.meta.days || 0} 天`);
    const syncedAt = (ACCOUNT && ACCOUNT.lastSync) ? String(ACCOUNT.lastSync).slice(11, 16) : '';
    setText('#kpiAvailNote', configured
      ? (syncedAt ? `云端 ${syncedAt} 同步` : `总额度 ${F.credit(acc.total)}`)
      : '未设置额度上限');
    const availChip = $('#kpiAvailSub');
    if (availChip) availChip.title = syncedAt
      ? `可用积分来自云端（${syncedAt} 同步），可能有延迟；今日消耗/调用/Token 为本地实时`
      : '可用积分来自本地记录';

    countUp($('[data-count="todayUsed"]'), today.consumed, { format: F.credit });
    const delta = $('#kpiTodayDelta');
    if (delta) {
      const up = (today.deltaPct || 0) >= 0;
      delta.className = 'delta' + (up ? '' : ' is-good');
      delta.innerHTML = `<svg class="i"><use href="#${up ? 'i-up' : 'i-down'}"/></svg>${Math.abs(today.deltaPct || 0).toFixed(1)}%`;
    }

    countUp($('[data-count="todayReq"]'), today.requests);
    setText('#kpiNet', (today.avgPerRequest || 0).toFixed(2));

    const tk = today.tokens || 0;
    const useYi = tk >= 1e8;
    const tkNode = $('[data-count="todayToken"]');
    countUp(tkNode, useYi ? tk / 1e8 : tk / 1e4, useYi ? { decimals: 2 } : {});
    if (tkNode && tkNode.nextElementSibling) tkNode.nextElementSibling.textContent = useYi ? '亿' : '万';
    setText('#kpiAvg', `${F.fmtWan(tk)} Token`);

    const sparks = {
      available: D.daily.slice(-14).map(d => d.balance),
      todayUsed: D.daily.slice(-14).map(d => d.consumed),
      todayReq: D.daily.slice(-14).map(d => d.consumed),
      todayToken: D.daily.slice(-14).map(d => d.consumed)
    };
    $$('[data-spark]').forEach(node => {
      C.sparkline(node, { values: sparks[node.dataset.spark] || [] });
    });

    const strip = $('#insightStrip');
    if (strip) {
      const topTask = D.tasks[0];
      strip.innerHTML = `
        <span class="badge"><svg class="i"><use href="#i-spark"/></svg></span>
        <span class="txt">今日已消耗 <b>${F.credit(today.consumed)} 分</b> · ${F.fmt(today.requests)} 次调用 · 峰值 <b>${F.pad(D.peakHour || 0)}:00</b>${topTask ? ` · 主要来自 <b>${topTask.name}</b>` : ''}${source === 'demo' ? ' · <b>演示数据</b>' : ''}</span>
        <button class="link" id="goAnalytics">查看分析 →</button>`;
      const link = $('#goAnalytics');
      if (link) link.addEventListener('click', () => go('analytics'));
    }

    setText('#hourlyPeak', D.peakPoints ? `峰值 ${F.pad(D.peakHour)}:00 · ${F.credit(D.peakPoints)} 分` : '今日暂无消耗');
    setHTML('#footReq', F.fmt(today.requests));
    setHTML('#footAvg', (today.avgPerRequest || 0).toFixed(2));
    setHTML('#footToken', F.fmtWan(today.tokens || 0));
    setHTML('#footNow', D.meta.updated || '—');

    C.bar($('[data-chart="hourly"]'), {
      data: D.hourly.map(hr => ({
        label: hr.label, short: F.pad(hr.hour), value: hr.points, isNow: hr.isNow,
        tipTitle: `${F.pad(hr.hour)}:00–${F.pad((hr.hour + 1) % 24)}:00`
      })),
      tone: 'c1', valueName: '消耗', sum: today.consumed,
      nowIndex: D.meta.nowHour, xEvery: 3, showValues: prefs.labels, maxBarW: 22
    });

    renderComposition(D.models, today.consumed);
    setText('#compCount', D.models.length ? `${D.models.length} 个模型` : '—');

    // 单一模型时补一行真实统计，避免 100% 环图信息量过低
    const note = $('#compNote');
    if (note) {
      note.innerHTML = D.models.length <= 1
        ? `<span><b>${F.fmt(today.requests)}</b> 次调用</span><span>平均 <b>${(today.avgPerRequest || 0).toFixed(2)}</b> 分/次</span><span>累计上下文 <b>${F.fmtWan(today.tokens || 0)}</b> Token</span>`
        : `<span>共 <b>${D.models.length}</b> 个模型</span><span>平均 <b>${(today.avgPerRequest || 0).toFixed(2)}</b> 分/次</span><span>累计上下文 <b>${F.fmtWan(today.tokens || 0)}</b> Token</span>`;
    }

    renderTrend();

    C.gauge($('[data-chart="gauge"]'), {
      value: configured ? acc.usedPct : 0,
      label: configured ? '本周期已用' : '未设置额度',
      sub: configured ? `周期剩余 ${cycleDaysLeft()} 天` : '在设置中填写额度'
    });
    const al = allowance(acc);
    setText('#quotaWeek', configured ? F.credit(al.perWeek) : '—');
    setText('#quotaLeft', configured ? F.credit(al.available) : '—');
    setText('#quotaDay', configured ? F.credit(al.perDay) : '—');
    const qh = $('#quotaHint');
    if (qh) {
      qh.textContent = configured
        ? `剩余 ${al.leftWorkdays} 个工作日 / ${al.leftDays} 天 · ${prefs.signin !== false ? '含每日签到 +' + F.credit(al.signin) + ' 分' : '不含签到'}`
        : '连接账号后可计算每日可用额度';
    }
  }

  function cycleDaysLeft() {
    const end = D.account.cycleEnd;
    if (!end) return '—';
    const e = end instanceof Date ? end : new Date(end);
    const diff = Math.ceil((e - new Date()) / 86400000);
    return isNaN(diff) ? '—' : Math.max(0, diff);
  }

  function renderTrend() {
    const days = D.daily.slice(-state.range);
    let run = 0;
    const cumulative = days.map(d => (run += d.consumed));
    const card = $('[data-chart="trend"]') && $('[data-chart="trend"]').closest('.chart-card');
    if (card) {
      const h2 = card.querySelector('h2');
      if (h2) h2.textContent = `近 ${state.range} 天消耗趋势`;
    }
    C.combo($('[data-chart="trend"]'), {
      labels: days.map(d => d.date),
      days,
      bars: days.map(d => d.consumed),
      line: cumulative,
      lineName: '累计消耗',
      xLabels: state.range <= 7 ? 1 : state.range <= 14 ? 2 : 5,
      dimLast: true
    });
  }

  /* =========================================================
     用量分析
     ========================================================= */

  /** 统一的排行行（模型 / 项目共用同一套栅格，保证左右严格对齐） */
  function renderRows(el, items, opts) {
    if (!el) return;
    const o = opts || {};
    const list = (items || []).filter(i => i.points > 0).slice(0, o.limit || 6);
    if (!list.length) {
      el.innerHTML = `<div class="rows-empty">${o.empty || '暂无数据'}</div>`;
      return;
    }
    const sum = list.reduce((a, b) => a + b.points, 0) || 1;
    const max = Math.max(...list.map(i => i.points), 0.01);
    el.innerHTML = list.map((m, i) => {
      const pct = (m.points / sum) * 100;
      const tone = C.toneColor('c' + ((i % 6) + 1));
      const w = Math.max(3, (m.points / max) * 100);
      const calls = (m.calls == null) ? '' : ` · ${F.fmt(m.calls)} 次`;
      return `<div class="rows-row" data-name="${String(m.name).replace(/"/g, '&quot;')}"
          data-points="${F.credit(m.points)}" data-pct="${pct.toFixed(1)}" data-calls="${m.calls == null ? '' : m.calls}">
        <span class="rows-idx">${String(i + 1).padStart(2, '0')}</span>
        <span class="rows-name" title="${m.name}${calls}">${m.name}</span>
        <span class="rows-track"><i data-w="${w.toFixed(1)}" style="width:0%;background:linear-gradient(90deg, ${C.hexToRgba(tone, .45)}, ${tone})"></i></span>
        <span class="rows-val">${F.credit(m.points)}</span>
        <span class="rows-pct">${pct.toFixed(1)}%</span>
      </div>`;
    }).join('');
    // 让条形有一次"从 0 到目标"的过渡（合成层属性，窗口被遮挡也不会停在 0）
    requestAnimationFrame(() => {
      Array.from(el.querySelectorAll('.rows-track i')).forEach(n => {
        n.style.width = (n.dataset.w || '0') + '%';
      });
    });
  }

  function calendarDays() {
    const connected = !!(ACCOUNT && ACCOUNT.status === 'connected');
    const cloudDaily = (connected && Array.isArray(ACCOUNT.daily)) ? ACCOUNT.daily : [];
    const year = new Date().getFullYear();
    const localDaily = (D.daily || []).map(d => ({
      date: d.date && d.date.length === 5 ? `${year}-${d.date}` : d.date,
      points: d.consumed || 0,
      calls: d.calls == null ? null : d.calls,
      tokens: d.tokens == null ? null : d.tokens
    }));
    const localMap = {};
    localDaily.forEach(d => { if (d.date) localMap[d.date] = d; });

    const map = {};
    localDaily.forEach(d => {
      if (!d.date) return;
      map[d.date] = { date: d.date, points: d.points, calls: d.calls, tokens: d.tokens, src: 'local' };
    });
    let cloudCount = 0;
    cloudDaily.forEach(d => {
      if (!d || !d.date) return;
      const loc = localMap[d.date];
      map[d.date] = {
        date: d.date,
        points: d.points || 0,
        calls: d.calls == null ? null : d.calls,
        tokens: loc ? loc.tokens : null,     // 云端逐笔不含 Token：有本机同日记录才给值，否则 —
        src: 'cloud'
      };
      cloudCount += 1;
    });
    return {
      days: Object.values(map),
      cloudCount,
      localCount: localDaily.filter(d => d.points > 0).length
    };
  }

  function renderAnalytics() {
    /* ---------- 消耗日历：云端逐笔优先，本机记录补盲区 ---------- */
    const cal = calendarDays();
    const calDays = cal.days;
    const calTotal = calDays.reduce((a, b) => a + b.points, 0);
    const activeDays = calDays.filter(d => d.points > 0).length;
    setText('#weekSum', `区间合计 ${F.credit(calTotal)} 分`);
    setText('#calSub', `每格一天，从当月起按整月对齐 · 颜色越深消耗越多 · 悬停查看积分 / Token / 调用次数`);

    const srcChip = $('#calSource');
    if (srcChip) {
      const useCloud = cal.cloudCount > 0;
      const both = useCloud && cal.localCount > 0;
      srcChip.textContent = useCloud ? (both ? '云端 + 本机' : '云端逐笔') : (cal.localCount ? '本地记录' : '暂无数据');
      srcChip.className = 'chip ' + (useCloud ? 'chip-good' : (cal.localCount ? 'is-warn' : ''));
      srcChip.title = useCloud
        ? `积分与调用次数取自云端 billing 逐笔（${cal.cloudCount} 天）；云端不返回 Token，Token 取本机同日记录，缺失显示「—」`
        : '云端未连接：按本机会话记录统计，可能不完整';
      srcChip.dataset.state = useCloud ? 'cloud' : 'local';
    }
    requestAnimationFrame(() => requestAnimationFrame(() => {
      C.calendar($('[data-chart="calendar"]'), { days: calDays, tone: 'c1' });
    }));

    /* ---------- 模型使用量：云端 30 天逐笔优先 ---------- */
    const connected = !!(ACCOUNT && ACCOUNT.status === 'connected');
    let modelItems = [];
    if (connected && Array.isArray(ACCOUNT.cloudModels) && ACCOUNT.cloudModels.length) {
      modelItems = ACCOUNT.cloudModels.map(m => ({ name: m.name, points: m.points, calls: m.calls }));
    } else if (Array.isArray(D.models30) && D.models30.length) {
      modelItems = D.models30.map(m => ({ name: m.name, points: m.points, calls: m.calls, tokens: m.tokens }));
    } else {
      modelItems = (D.models || []).map(m => ({ name: m.name, points: m.points, calls: m.calls }));
    }
    modelItems = modelItems.filter(m => m.points > 0).sort((a, b) => b.points - a.points);

    const mTotal = modelItems.reduce((a, b) => a + b.points, 0);
    setText('#modelTotal', modelItems.length ? `合计 ${F.credit(mTotal)} 分` : '暂无数据');

    const mlist = $('#modelList'), single = $('#modelSingle');
    if (mlist && single) {
      if (modelItems.length <= 1) {
        // 只有一个模型：占比环 + 明细 + 说明，避免一整块空白
        mlist.hidden = true;
        single.hidden = false;
        const m = modelItems[0];
        const tokens = m && m.tokens != null ? F.fmtWan(m.tokens) : null;
        single.innerHTML = `
          <div class="ms-donut" id="msDonut"></div>
          <div class="ms-stats">
            <div><span>模型</span><b>${m ? m.name : '—'}</b></div>
            <div><span>调用次数</span><b>${m && m.calls != null ? F.fmt(m.calls) + ' 次' : '—'}</b></div>
            <div><span>平均每次</span><b>${m && m.calls ? (m.points / m.calls).toFixed(2) + ' 分' : '—'}</b></div>
            <div><span>累计 Token</span><b>${tokens || '—'}</b></div>
          </div>
          <div class="ms-note">30 天内只用到 <b>1 个模型</b>，因此占比恒为 <b>100%</b>；切换模型或使用子任务后，这里会自动按模型拆分。</div>`;
        const box = $('#msDonut');
        if (box) {
          C.donut(box, {
            items: [{ name: m ? m.name : '—', value: m ? m.points : 0, tone: 'c1' }],
            legend: false,
            size: 156,
            hRatio: 0.96,
            centerLabel: '模型占比',
            centerFormat: () => '100%',
            centerUnit: '单一模型'
          });
        }
      } else {
        mlist.hidden = false;
        single.hidden = true;
        renderRows(mlist, modelItems, { limit: 6, empty: '暂无模型消耗' });
      }
    }

    /* ---------- 项目消耗排行：本机记录（云端不返回项目） ---------- */
    const projItems = (D.projects7 || []).map(p => ({ name: p.name, points: p.points }));
    const pTotal = projItems.reduce((a, b) => a + b.points, 0);
    setText('#projTotal', projItems.length ? `近 7 天 ${F.credit(pTotal)} 分` : '暂无数据');
    renderRows($('#projectRank'), projItems, { limit: 6, empty: '近 7 天暂无项目消耗' });

    const list = $('#insightList');
    if (list) {
      list.innerHTML = D.insights.map((it, i) => `
        <div class="insight-item" style="animation-delay:${i * 60}ms">
          <span class="badge"><svg class="i"><use href="#${it.icon || 'i-info'}"/></svg></span>
          <div><h4>${it.title}</h4><p>${it.text}</p></div>
        </div>`).join('');
    }
  }

  /* =========================================================
     流水
     ========================================================= */

  function ledgerRows() {
    const q = state.ledgerQuery.trim().toLowerCase();
    const now = new Date();
    const todayKey = `${now.getFullYear()}-${F.pad(now.getMonth() + 1)}-${F.pad(now.getDate())}`;
    return D.ledger.filter(e => {
      const t = e.time instanceof Date ? e.time : new Date(e.time);
      const key = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())}`;
      if (state.ledgerType === 'today' && key !== todayKey) return false;
      if (state.ledgerType === 'week' && (now - t) > 7 * 86400000) return false;
      if (!q) return true;
      return [e.model, e.task, e.project].join(' ').toLowerCase().includes(q);
    });
  }

  function renderLedger(reset) {
    if (reset) state.ledgerLimit = 24;
    const all = ledgerRows();
    const rows = all.slice(0, state.ledgerLimit);
    const body = $('#ledgerBody');
    if (!body) return;

    const today = D.today;
    const maxPts = Math.max(...all.map(e => e.points), 0.01);

    setHTML('#ledgerSub', `本机记录 · 共 ${F.fmt(D.meta.records || 0)} 次调用，覆盖 ${D.meta.days || 0} 天`);
    const sum = $('#ledgerSummary');
    if (sum) {
      sum.innerHTML = `
        <div><span>今日消耗</span><b>${F.credit(today.consumed)} <small style="font-size:10px;color:var(--text-3)">分</small></b></div>
        <div><span>今日调用</span><b>${F.fmt(today.requests)} <small style="font-size:10px;color:var(--text-3)">次</small></b></div>
        <div><span>今日 Token</span><b>${F.fmtWan(today.tokens || 0)}</b></div>
        <div><span>筛选结果</span><b>${all.length} <small style="font-size:10px;color:var(--text-3)">条</small></b></div>`;
    }

    if (!rows.length) {
      body.innerHTML = '<div class="pop-empty" style="padding:26px 0">当前筛选条件下没有记录</div>';
    } else {
      // 同一分钟的记录合并展示：首条显示时间与条数，其余用左侧导轨承接
      const groups = {};
      rows.forEach(e => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const key = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())} ${F.clock(t)}`;
        const g = (groups[key] = groups[key] || { list: [], sum: 0 });
        g.list.push(e);
        g.sum += e.points;
      });
      const seen = {};
      body.innerHTML = rows.map((e, i) => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const wd = '日一二三四五六'[t.getDay()];
        const share = e.points / (today.consumed || 1);
        const key = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())} ${F.clock(t)}`;
        const group = groups[key] || { list: [], sum: 0 };
        const isFirst = !seen[key];
        seen[key] = true;
        const grouped = group.list.length > 1;
        const timeCell = isFirst
          ? `<span class="t">${F.dateKey(t)} ${F.clock(t)}${grouped ? `<i class="minute-badge">×${group.list.length}</i>` : ''}<small>周${wd}</small></span>`
          : `<span class="t t--cont"><i class="rail"></i></span>`;
        // 同一分钟的积分合计放到「积分」列；时间徽标只留 ×N
        const ptCell = (isFirst && grouped)
          ? `<span class="pt pt-group">-${F.credit(group.sum)}<small>×${group.list.length} 合计</small></span>`
          : `<span class="pt${isFirst ? '' : ' is-sub'}">-${F.credit(e.points)}</span>`;
        return `<div class="table-row${isFirst ? '' : ' is-cont'}" style="animation-delay:${Math.min(i, 12) * 18}ms">
          ${timeCell}
          <span><i class="badge-type badge-consume">消耗</i></span>
          <span class="nm"><i class="legend-dot" style="display:inline-block;vertical-align:middle;margin-right:5px;background:${C.toneColor(e.tone)}"></i>${e.model}<small>${F.fmtWan(e.tokens || 0)} Token</small></span>
          <span class="dim">${e.task}</span>
          <span class="dim">${e.project}</span>
          ${ptCell}
          <span class="share-cell"><i class="share-bar"><i style="width:${Math.max(4, (e.points / maxPts) * 100).toFixed(1)}%"></i></i><span>${(share * 100) < 1 ? (share * 100).toFixed(2) : (share * 100).toFixed(1)}%</span></span>
        </div>`;
      }).join('');
    }

    setHTML('#ledgerCount', `显示 ${rows.length} / ${all.length} 条`);
    const more = $('#ledgerMore');
    if (more) {
      more.disabled = rows.length >= all.length;
      more.style.opacity = rows.length >= all.length ? .45 : 1;
    }
  }

  /* =========================================================
     设置
     ========================================================= */

  function avatarMarkup(size) {
    const u = (ACCOUNT && ACCOUNT.user) || {};
    const initial = (u.nickname || 'WB').slice(0, 2);
    return { img: u.avatar || '', initial };
  }

  function renderAvatar() {
    const img = $('#avatarImg'), txt = $('#avatarText');
    if (!img || !txt) return;
    const { img: url, initial } = avatarMarkup();
    if (url) {
      img.src = url; img.hidden = false; txt.hidden = true;
    } else {
      img.hidden = true; txt.hidden = false; txt.textContent = initial;
    }
    // 设置页的大头像
    const big = $('#accAvatar');
    if (big) {
      if (url) {
        big.textContent = '';
        big.style.backgroundImage = `url("${url}")`;
        big.style.backgroundSize = 'cover';
        big.style.backgroundPosition = 'center';
      } else {
        big.style.backgroundImage = '';
        big.textContent = initial;
      }
    }

    const u = (ACCOUNT && ACCOUNT.user) || {};
    const tip = [u.nickname, u.plan, u.phone].filter(Boolean).join(' · ') || '未连接账号';
    const btn = $('#avatar');
    if (btn) btn.title = tip;
  }

  function renderAccountCard() {
    const chip = $('#cloudChip');
    if (!chip) return;
    const st = ACCOUNT.status || 'disconnected';
    const MAP = {
      connected: ['已连接', 'is-ok'],
      logining: ['等待登录', 'is-warn'],
      expired: ['登录已过期', 'is-warn'],
      error: ['连接异常', 'is-warn'],
      disconnected: ['未连接', '']
    };
    const [text, cls] = MAP[st] || MAP.disconnected;
    chip.textContent = text;
    chip.className = 'chip ' + cls;

    const c = ACCOUNT.cloud || {};
    const u = (ACCOUNT.user || {});
    const credit = v => (v == null ? '—' : F.credit(v) + ' 分');
    setText('#cloudAvail', credit(c.available));
    setText('#cloudUsed', credit(c.used));
    const plan = u.plan || '';
    setText('#cloudCycle', c.cycleStartText ? `${c.cycleStartText} 起${plan ? ' · ' + plan : ''}` : (plan || '—'));
    setText('#cloudExpire', c.expireAtText || '—');

    // 账号身份
    setText('#accName', u.nickname || (st === 'connected' ? 'WorkBuddy 账号' : '未连接账号'));
    setText('#accPlan', u.plan ? `${u.plan}${u.phone ? ' · ' + u.phone : ''}` : '扫码后可读取余额与有效期');
    setText('#accId', u.uin ? 'UIN ' + u.uin : (u.uid ? 'UID ' + String(u.uid).slice(0, 8) : '—'));
    renderAvatar();

    // 积分包明细
    const list = $('#pkgList');
    if (list) {
      const pkgs = (ACCOUNT.packages || []).filter(p => p.total > 0);
      list.innerHTML = pkgs.length
        ? pkgs.map(p => {
            const pct = p.total ? Math.max(2, (p.remain / p.total) * 100) : 0;
            return `<div class="pkg-row"><span class="nm">${p.name}</span>
              <span class="bar"><i style="width:${pct.toFixed(1)}%"></i></span>
              <b>${F.credit(p.remain)}</b><span>/ ${F.credit(p.total)}</span></div>`;
          }).join('')
        : '';
    }

    // 头像状态点：连接成功=绿，过期/异常=黄，未连接=灰
    const dot = $('#avatarDot');
    if (dot) {
      dot.className = 'avatar-dot ' + (st === 'connected' ? 'is-on'
        : (st === 'expired' || st === 'error') ? 'is-warn' : 'is-off');
      dot.title = st === 'connected' ? '云端已连接' : st === 'expired' ? '登录已过期' : '未连接云端';
    }

    // 上次同步 / 间隔（渲染只回填值，事件在 bind 里只绑一次）
    setText('#accLastSync', ACCOUNT.lastSync
      ? ACCOUNT.lastSync + (ACCOUNT.lastError ? ' · 异常：' + ACCOUNT.lastError : '')
      : '尚未同步');
    const sel = $('#cloudInterval');
    if (sel && document.activeElement !== sel) sel.value = String(ACCOUNT.interval == null ? 60 : ACCOUNT.interval);
    setText('#cloudHint', ACCOUNT.lastError
      ? `最近一次同步失败：${ACCOUNT.lastError}（登录态可能已过期，点「连接账号」重新扫码）`
      : '登录态经 DPAPI 加密保存在本机，不上传；「刷新」只读本机记录，云端按上方间隔同步');
  }

  function renderSettings() {
    const acc = effAccount();
    renderAccountCard();

    setText('#srcCloud', ACCOUNT.status === 'connected'
      ? `已连接 · ${(ACCOUNT.user && ACCOUNT.user.plan) || '标准版'}`
      : '未连接');
    setText('#srcRecords', F.fmt(D.meta.records || 0) + ' 条');
    setText('#srcDays', `${D.meta.days || 0} 天`);
    setText('#srcInterval', (+ACCOUNT.interval === 0 ? '仅手动' : (ACCOUNT.interval || 60) + ' 分钟'));
    setText('#aboutVersion', appVersion);
    setText('#appVersion', appVersion);
    setText('#sideVersion', appVersion);

    // 数据目录（#7）：异步取一次，避免阻塞渲染
    const kindText = { jsonl: '本机会话 JSONL（逐笔，实时）', db: 'workbuddy.db（仅日级估算）',
                       none: '未找到可用数据目录' };
    if (bridge.has()) {
      bridge.call('get_datasource').then(r => {
        const st = (r && r.state) || null;
        if (!st) return;
        setText('#srcPath', st.home || '—');
        setText('#srcKind', `${kindText[st.sourceKind || (st.valid ? 'jsonl' : 'none')] || '—'}${st.source === 'manual' ? ' · 手动指定' : st.source === 'auto' ? ' · 自动探测' : ''}`);
        const note = $('#dbNote');
        if (note) {
          note.textContent = st.valid
            ? [
                st.hasProjects ? '含 projects/ 会话明细' : '无 projects/ 明细',
                st.hasDb ? '含 workbuddy.db（锁定时会先快照再读）' : '无 workbuddy.db',
                st.note || ''
              ].filter(Boolean).join(' · ')
            : (st.note || '未找到数据目录，请点「选择目录」指定。');
          note.classList.toggle('warn', !st.valid);
        }
      }).catch(() => {});
    }


    $('#optLabels').checked = !!prefs.labels;
    $('#optTray').checked = !!prefs.tray;
    if ($('#optHoliday')) $('#optHoliday').checked = prefs.holiday !== false;
    if ($('#optSignin')) $('#optSignin').checked = prefs.signin !== false;
    const lrSel = $('#optLocalRefresh');
    if (lrSel && document.activeElement !== lrSel) lrSel.value = String(prefs.localRefresh ?? 60);

    $$('#themeCards .theme-card').forEach(card => {
      card.classList.toggle('is-active', card.dataset.themeChoice === prefs.theme);
    });
    $$('#motionSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.motionChoice === prefs.motion));
  }

  /* =========================================================
     路由
     ========================================================= */

  function go(view) {
    if (state.view === view) return;
    state.view = view;
    $$('.nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === view));
    moveIndicator();
    const [t, s] = TITLES[view] || ['', ''];
    setText('#pageTitle', t);
    setText('#pageSub', s);
    $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === view));
    $('#views').scrollTop = 0;
    const dr = $('.stack-legend');
    if (dr && view !== 'analytics') dr.remove();
    renderActive(true);
  }

  function moveIndicator() {
    const active = $('.nav-item.is-active');
    const ind = $('#navIndicator');
    if (active && ind) ind.style.setProperty('--nav-y', active.offsetTop + 'px');
  }

  function setMode(mode) {
    state.mode = mode;
    document.body.dataset.mode = mode;
    document.documentElement.dataset.mode = mode;
    renderActive(true);
    requestAnimationFrame(() => { if (mode === 'main') moveIndicator(); });
  }

  /* ---------------- 提示 / 刷新 ---------------- */

  function toast(msg, icon) {
    const host = $('#toastHost');
    if (!host) return;
    const n = document.createElement('div');
    n.className = 'toast';
    n.innerHTML = `<svg class="i"><use href="#${icon || 'i-check'}"/></svg><span>${msg}</span>`;
    host.appendChild(n);
    setTimeout(() => { n.classList.add('is-out'); setTimeout(() => n.remove(), 240); }, 2200);
  }

  async function refresh() {
    const btn = $('#refreshBtn');
    if (btn) { btn.classList.add('is-spinning'); setTimeout(() => btn.classList.remove('is-spinning'), 720); }
    const cards = $$('.card');
    cards.forEach(c => {
      if (!c.querySelector('.refresh-bar')) c.appendChild(Object.assign(document.createElement('div'), { className: 'refresh-bar' }));
      c.classList.add('is-refreshing');
    });

    if (bridge.has()) {
      try {
        const raw = await bridge.call('refresh_usage');
        if (raw && raw.meta) applyUsage(raw, true);
        // 刷新只读本地记录：云端同步走 60 分钟定时或「立即同步」，避免频繁触发风控
      } catch (e) { /* ignore */ }
    }
    setTimeout(() => {
      cards.forEach(c => c.classList.remove('is-refreshing'));
      renderActive(true);
      const t = new Date();
      setText('#syncTime', `${F.pad(t.getHours())}:${F.pad(t.getMinutes())}`);
      toast(bridge.has() ? '已重新读取本机用量' : '浏览器预览无法读取本机数据', bridge.has() ? 'i-check' : 'i-info');
    }, motionOff() ? 60 : 560);
  }

  /* =========================================================
     事件
     ========================================================= */

  function bind() {
    $$('.nav-item').forEach(b => b.addEventListener('click', () => go(b.dataset.view)));

    const toggle = el => () => setTheme(resolved() === 'dark' ? 'light' : 'dark', el);
    const tb = $('#themeBtn'); if (tb) tb.addEventListener('click', toggle(tb));
    const pt = $('#popTheme'); if (pt) pt.addEventListener('click', toggle(pt));
    const sw = $('#themeSwitch');
    if (sw) sw.addEventListener('click', () => setTheme(resolved() === 'dark' ? 'light' : 'dark', sw));
    $$('#themeCards .theme-card').forEach(card => {
      card.addEventListener('click', () => setTheme(card.dataset.themeChoice, card));
    });

    $$('#motionSeg button').forEach(b => b.addEventListener('click', () => setMotion(b.dataset.motionChoice)));

    const bindToggle = (sel, key, after) => {
      const n = $(sel);
      if (!n) return;
      n.addEventListener('change', () => {
        prefs[key] = n.checked;
        savePrefs();
        if (after) after();
        renderActive();
      });
    };
    bindToggle('#optLabels', 'labels');
    bindToggle('#optHoliday', 'holiday', () => renderActive(true));
    bindToggle('#optSignin', 'signin', () => renderActive(true));
    bindToggle('#optTray', 'tray', () => bridge.call('set_close_to_tray', prefs.tray));

    const lr = $('#optLocalRefresh');
    if (lr) lr.addEventListener('change', () => {
      const v = +lr.value;
      if (v === +prefs.localRefresh) return;      // 忽略表单状态恢复导致的伪变更
      prefs.localRefresh = v;
      savePrefs();
      setupLocalRefresh();
      toast(v === 0 ? '本地数据改为手动刷新' : `本地记录每 ${v} 秒刷新一次`);
    });

    $$('#rangeSeg button').forEach(b => {
      b.addEventListener('click', () => {
        state.range = +b.dataset.range;
        $$('#rangeSeg button').forEach(x => x.classList.toggle('is-active', x === b));
        renderTrend();
      });
    });

    $$('#ledgerType button').forEach(b => {
      b.addEventListener('click', () => {
        state.ledgerType = b.dataset.type;
        $$('#ledgerType button').forEach(x => x.classList.toggle('is-active', x === b));
        renderLedger(true);
      });
    });
    const search = $('#ledgerSearch');
    if (search) {
      let t;
      search.addEventListener('input', () => {
        clearTimeout(t);
        t = setTimeout(() => { state.ledgerQuery = search.value; renderLedger(true); }, 140);
      });
    }
    const more = $('#ledgerMore');
    if (more) more.addEventListener('click', () => { state.ledgerLimit += 24; renderLedger(false); });

    const ps = $('#popSearch');
    if (ps) {
      ps.addEventListener('input', () => {
        state.popQuery = ps.value;
        const clear = $('#popSearchClear');
        if (clear) clear.hidden = !ps.value;
        renderPopupList();
      });
    }
    const pclear = $('#popSearchClear');
    if (pclear) pclear.addEventListener('click', () => {
      const ps2 = $('#popSearch');
      if (ps2) { ps2.value = ''; ps2.focus(); }
      state.popQuery = '';
      pclear.hidden = true;
      renderPopupList();
    });

    const cl = $('#cloudLogin');
    if (cl) cl.addEventListener('click', async () => {
      const r = await bridge.call('account_login');
      if (r && r.ok) {
        ACCOUNT.status = 'logining';
        renderAccountCard();
        toast('已打开登录页，请扫码完成登录');
        // 轮询账号状态，登录成功后自动刷新界面
        let tries = 0;
        const timer = setInterval(async () => {
          tries += 1;
          await loadAccount();
          renderAccountCard();
          if (ACCOUNT.status === 'connected') {
            clearInterval(timer);
            renderActive(true);
            toast('账号已连接，已读取账号积分');
          } else if (tries > 40) {
            clearInterval(timer);
          }
        }, 3000);
      } else {
        toast('无法打开登录页：' + ((r && r.error) || '浏览器模式不支持'));
      }
    });
    const cs = $('#cloudSync');
    if (cs) cs.addEventListener('click', async () => {
      toast('正在同步账号数据…');
      const r = await bridge.call('account_sync');
      await loadAccount();
      renderAccountCard();
      renderActive(true);
      toast(r && r.ok ? '账号数据已同步' : '同步失败，登录态可能已过期');
    });
    const ci = $('#cloudInterval');
    if (ci) ci.addEventListener('change', async () => {
      if (+ci.value === +(ACCOUNT.interval == null ? 60 : ACCOUNT.interval)) return;
      await bridge.call('account_set_interval', ci.value);
      await loadAccount();
      renderAccountCard();
      toast(+ci.value === 0 ? '已改为仅手动同步' : `自动同步间隔 ${ci.value} 分钟`);
    });
    const clo = $('#cloudLogout');
    if (clo) clo.addEventListener('click', async () => {
      await bridge.call('account_logout');
      await loadAccount();
      renderAccountCard();
      renderActive(true);
      toast('已断开账号，界面回到本机记录');
    });

    const sy = $('#syncState');
    if (sy) sy.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法同步账号', 'i-info'); return; }
      toast('正在同步云端账号…');
      await bridge.call('account_sync');
      await loadAccount();
      renderAccountCard(); renderActive(true);
      toast(ACCOUNT.status === 'connected' ? '云端数据已同步' : '同步失败，请检查登录状态');
    });
    const rb = $('#refreshBtn'); if (rb) rb.addEventListener('click', refresh);
    const pr = $('#popRefresh'); if (pr) pr.addEventListener('click', refresh);

    ['#popExpand', '#popExpandTop'].forEach(s => { const n = $(s); if (n) n.addEventListener('click', () => bridge.expand()); });
    /* ---------------- 账号悬浮栏 ---------------- */

    const acctPop = $('#acctPop');
    const avatarBtn = $('#avatar');

    const positionAccountPop = () => {
      if (!acctPop || !avatarBtn) return;
      const r = avatarBtn.getBoundingClientRect();
      acctPop.style.top = Math.round(r.bottom + 8) + 'px';
      acctPop.style.right = Math.max(10, Math.round(window.innerWidth - r.right)) + 'px';
    };

    const setAccountPop = open => {
      if (!acctPop) return;
      acctPop.hidden = !open;
      if (avatarBtn) avatarBtn.setAttribute('aria-expanded', String(open));
      if (open) {
        renderAccountCard();          // 展开时用最新账号数据重绘
        positionAccountPop();
      }
    };

    if (avatarBtn) avatarBtn.addEventListener('click', e => {
      e.stopPropagation();
      setAccountPop(acctPop ? acctPop.hidden : false);
    });

    if (acctPop) acctPop.addEventListener('click', e => e.stopPropagation());

    document.addEventListener('click', () => { if (acctPop && !acctPop.hidden) setAccountPop(false); });

    window.addEventListener('resize', () => { if (acctPop && !acctPop.hidden) positionAccountPop(); });
    const ph = $('#popHide'); if (ph) ph.addEventListener('click', () => bridge.hide());
    const pv = $('#popPreviewBtn'); if (pv) pv.addEventListener('click', () => {
      if (bridge.has()) bridge.collapse();
      else toast('浏览器预览中，桌面版可直接收起为右下角小窗', 'i-info');
    });

    /* ---------------- 自绘标题栏（最小化 / 最大化 / 关闭） ---------------- */

    const wmin = $('#winMin');
    if (wmin) wmin.addEventListener('click', () => {
      if (bridge.has()) bridge.call('win_min');
      else toast('浏览器预览无法最小化', 'i-info');
    });
    const wmax = $('#winMax');
    if (wmax) wmax.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法最大化', 'i-info'); return; }
      applyWinState(await bridge.call('win_max'));
      setTimeout(readWinState, 240);
    });
    const wclose = $('#winClose');
    if (wclose) wclose.addEventListener('click', async () => {
      if (bridge.has()) await bridge.call('win_close');
      else toast('浏览器预览无法收起到托盘', 'i-info');
    });
    const topbar = $('#topbar');
    if (topbar) topbar.addEventListener('dblclick', e => {
      if (e.target.closest('button') || !bridge.has()) return;
      bridge.call('win_max').then(st => { applyWinState(st); setTimeout(readWinState, 240); });
    });

    /* 自绘标题栏拖动：用绝对坐标，避免 pywebview 自带 drag 的增量叠加（越拖越快） */
    let drag = null, dragPending = null, dragTimer = 0, dragLast = 0;
    const queueMove = (x, y) => {
      dragPending = [x, y];
      if (dragTimer) return;
      const wait = Math.max(0, 12 - (performance.now() - dragLast));
      dragTimer = setTimeout(() => {
        dragTimer = 0;
        dragLast = performance.now();
        const p = dragPending;
        dragPending = null;
        if (p) bridge.call('win_move', p[0], p[1]);
      }, wait);
    };
    const startDrag = async e => {
      if (!topbar || e.button !== 0 || !bridge.has()) return;
      if (e.target.closest('button, input, select, textarea, a, .win-controls, .dock')) return;
      if (winMaxed) {                       // 最大化时拖动 = 先还原，再跟随光标
        applyWinState(await bridge.call('win_max'));
        await new Promise(r => setTimeout(r, 70));
      }
      const pos = await bridge.call('win_pos');
      if (!pos || pos.x == null) return;
      drag = { sx: e.screenX, sy: e.screenY, wx: pos.x, wy: pos.y, dpr: window.devicePixelRatio || 1 };
      document.body.classList.add('is-dragging');
    };
    if (topbar) topbar.addEventListener('mousedown', startDrag);
    window.addEventListener('mousemove', e => {
      if (!drag) return;
      queueMove(
        Math.round(drag.wx + (e.screenX - drag.sx) * drag.dpr),
        Math.round(drag.wy + (e.screenY - drag.sy) * drag.dpr)
      );
    });
    window.addEventListener('mouseup', () => {
      if (!drag) return;
      drag = null;
      document.body.classList.remove('is-dragging');
      setTimeout(readWinState, 120);
    });
    readWinState();
    setInterval(() => {
      if (state.mode === 'main' && document.visibilityState === 'visible') readWinState();
    }, 1500);

    /* ---------------- 排行行悬停提示（模型 / 项目共用） ---------------- */

    document.addEventListener('mouseover', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (!row || !row.dataset.name) return;
      const rows = [
        `<div class="tt-row"><span class="tt-name">消耗</span><span class="tt-val">${row.dataset.points} 分</span></div>`,
        `<div class="tt-row"><span class="tt-name">占比</span><span class="tt-val">${row.dataset.pct}%</span></div>`,
        `<div class="tt-row"><span class="tt-name">调用</span><span class="tt-val">${row.dataset.calls || '—'}</span></div>`
      ].join('');
      C.showTip(`<div class="tt-title">${row.dataset.name}</div>${rows}`, e.clientX, e.clientY);
    });
    document.addEventListener('mousemove', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (!row) return;
      C.showTip(`<div class="tt-title">${row.dataset.name}</div>
        <div class="tt-row"><span class="tt-name">消耗</span><span class="tt-val">${row.dataset.points} 分</span></div>
        <div class="tt-row"><span class="tt-name">占比</span><span class="tt-val">${row.dataset.pct}%</span></div>
        <div class="tt-row"><span class="tt-name">调用</span><span class="tt-val">${row.dataset.calls || '—'}</span></div>`, e.clientX, e.clientY);
    });
    document.addEventListener('mouseout', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (row) C.hideTip();
    });

    const noticeLink = $('#noticeSource');
    if (noticeLink) noticeLink.addEventListener('click', () => go('settings'));

    const pickBtn = $('#dbPick');
    if (pickBtn) pickBtn.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法选择目录', 'i-info'); return; }
      toast('请选择 WorkBuddy 数据目录…');
      const r = await bridge.call('choose_data_dir');
      if (!r || r.cancelled) return;
      await reloadAll();
      toast(r.ok ? '数据目录已切换' : '该目录下没有 projects/ 或 workbuddy.db', r.ok ? 'i-check' : 'i-info');
    });

    const resetBtn = $('#dbReset');
    if (resetBtn) resetBtn.addEventListener('click', async () => {
      if (!bridge.has()) return;
      await bridge.call('reset_data_dir');
      await reloadAll();
      toast('已恢复自动检测');
    });

    setupResize();

    const pcl = $('#popCloud');
    if (pcl) pcl.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法同步云端', 'i-info'); return; }
      toast('正在同步云端账号…');
      await bridge.call('account_sync');
      await loadAccount();
      renderPopup();
      renderAccountCard();
      toast(ACCOUNT.status === 'connected' ? '云端数据已同步' : '同步失败，请检查登录状态');
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        if (acctPop && !acctPop.hidden) setAccountPop(false);
        if (state.mode === 'popup') bridge.hide();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'r') { e.preventDefault(); refresh(); }
      if (e.altKey && /^[1-4]$/.test(e.key) && state.mode === 'main') {
        go(['overview', 'analytics', 'ledger', 'settings'][+e.key - 1]);
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) renderActive();
    });

    let rt;
    window.addEventListener('resize', () => {
      clearTimeout(rt);
      rt = setTimeout(() => { moveIndicator(); renderActive(); }, 180);
    });

    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const handler = () => { if (prefs.theme === 'system') paintTheme(mq.matches ? 'dark' : 'light'); };
      if (mq.addEventListener) mq.addEventListener('change', handler);
    }

    window.addEventListener('pywebviewready', () => {
      document.body.classList.add('is-desktop');
      bridge.call('set_close_to_tray', prefs.tray);
      // 桥接晚于 DOM 就绪：无论是否已渲染，都补一次真实数据
      Promise.all([loadUsage(), loadAccount()]).then(([ok]) => {
        if (ok) { ready ? renderActive(true) : bootRender(); }
      });
    });
  }

  /* =========================================================
     启动
     ========================================================= */

  function bootRender() {
    if (ready) { renderActive(); return; }
    ready = true;
    if (state.mode === 'popup') {
      renderPopup();
    } else {
      if (state.view !== 'overview') {
        $$('.nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === state.view));
        $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === state.view));
        const [t, s] = TITLES[state.view] || ['', ''];
        setText('#pageTitle', t);
        setText('#pageSub', s);
      }
      renderActive();
      requestAnimationFrame(moveIndicator);
    }
    paintTheme(resolved(), true);
    setTimeout(() => document.body.classList.remove('is-booting'), 40);
    setTimeout(() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }, 120);
  }

  function whenBridgeReady(timeout) {
    return new Promise(resolve => {
      if (bridge.has()) return resolve();
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      window.addEventListener('pywebviewready', finish, { once: true });
      setTimeout(finish, timeout || 1500);
    });
  }

  async function boot() {
    const forced = qs.get('theme');
    if (forced === 'dark' || forced === 'light') prefs.theme = forced;
    document.documentElement.setAttribute('data-theme', resolved());
    document.documentElement.setAttribute('data-motion', prefs.motion);
    document.documentElement.dataset.mode = state.mode;
    document.body.dataset.mode = state.mode;
    document.body.classList.add('is-booting');
    if (HASH_MODE || qs.get('shell') === 'desktop' || window.pywebview) document.body.classList.add('is-desktop');

    bind();
    $$('#rangeSeg button').forEach(b => b.classList.toggle('is-active', +b.dataset.range === state.range));
    setText('#syncTime', D.meta.updated || '—');

    if (HASH_MODE || bridge.has()) {
      document.body.classList.add('is-loading');
      await whenBridgeReady();
      await loadUsage();
      await loadAccount();
      await loadVersion();
      document.body.classList.remove('is-loading');
    }

    bootRender();

    setTimeout(() => { if (state.mode === 'popup') renderPopup(); }, 260);
    setupLocalRefresh();

    // 后台同步完成后自动刷新账号卡片（60 秒轮询一次，本地调用，无网络开销）
    setInterval(async () => {
      if (!bridge.has()) return;
      const before = ACCOUNT && ACCOUNT.lastSync;
      await loadAccount();
      if (ACCOUNT && ACCOUNT.lastSync !== before) {
        renderAccountCard();
        renderActive(true);
      }
    }, 60000);
  }

  window.App = {
    go, setMode, renderActive, refresh,
    refreshLayout: () => renderActive(),
    reloadAccount: async () => {
      const ok = await loadAccount();
      if (ok) { renderAccountCard(); renderActive(true); }
      return ok;
    },
    reloadUsage: async () => {
      const ok = await loadUsage();
      if (ok) renderActive();
      return ok;
    },
    __applyTheme: t => {
      const next = t === 'dark' ? 'dark' : 'light';
      if (document.documentElement.getAttribute('data-theme') === next) return;
      prefs.theme = next;
      paintTheme(next, false, true);
    },
    __setMode: m => setMode(m)
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
