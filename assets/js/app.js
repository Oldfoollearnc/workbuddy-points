/* =========================================================
   应用逻辑：数据接入 / 路由 / 主题 / 渲染 / 双窗口协同
   数据来源：本机 WorkBuddy 会话记录（经 Python 侧 usage_source 聚合）

   2900 行按「段」排列，先看这张目录再搜函数名（行号会变）：
     1. 偏好与运行态     prefs / ui / state、readPrefs / savePrefs
     2. 桌面桥           bridge、safeRender（单块渲染失败不拖垮整页）
     3. 路由与主题       go / setMode / applyTheme / moveIndicator
     4. 总览             renderOverview / renderComposition / renderTrend
     5. 额度             allowance / renderQuotaCard / quotaWindow
     6. 签到与猫猫       renderCheckin / paintActCards / startEtaTicker
     7. 用量分析         calendarDays / renderAnalytics / renderRows
     8. 流水             ledgerAll / ledgerRows / renderLedger / buildSummary
     9. 设置             renderSettings / bindToggle / 维护（清缓存清日志）
    10. 初始化           boot / init、定时器、Python 推送入口
   ========================================================= */
(function () {
  'use strict';

  const C = window.Charts;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const qs = new URLSearchParams(location.search);

  const HASH = location.hash.replace('#', '').split('&').filter(Boolean);
  const HASH_MODE = HASH.find(x => x === 'popup' || x === 'main') || null;
  const HASH_VIEW = HASH.find(x => ['overview', 'analytics', 'ledger', 'account', 'settings'].includes(x)) || null;

  // #tooltip 在标记里被包进了 .app-shell，而小窗模式下 .app-shell 是 display:none，
  // 结果小窗里所有图表都没有悬浮提示。统一把它挂到 body 上，两个窗口共用。
  (function reparentTip() {
    const tip = document.getElementById('tooltip');
    if (tip && tip.parentElement !== document.body) document.body.appendChild(tip);
  })();

  const TITLES = {
    overview: ['总览', 'WorkBuddy 账号的积分余额与今日使用情况'],
    analytics: ['用量分析', '消耗日历、模型用量与项目排行的真实拆解'],
    ledger: ['积分流水', 'WorkBuddy 逐次调用的真实积分扣减记录'],
    account: ['账户', '账号、额度与数据源：这台机器读的是哪份数据'],
    settings: ['设置', '主题、动效、刷新节奏与每日积分']
  };

  const state = {
    mode: HASH_MODE || (qs.get('mode') === 'popup' ? 'popup' : 'main'),
    view: HASH_VIEW || 'overview',
    range: 7,
    quotaRange: 'week',
    localRange: '30',      // 本机消耗卡：7 / 30 / month / all
    projRange: '7',        // 项目排行：7 / 30 / month
    ledgerType: 'all',
    ledgerQuery: '',
    ledgerFilter: '',      // 'model:xxx' / 'project:xxx'
    ledgerLimit: 24,
    popQuery: ''
  };


  /* ---------------- 偏好 ---------------- */
  const PREF_KEY = 'pi.prefs.v1';
  const prefs = Object.assign(
    { theme: 'light', motion: 'full', countUp: true, labels: true, tray: true, onTop: false, digest: true,
      holiday: true, signin: true, localRefresh: 60,
      calGlow: true, calIntro: true, groupMinute: true, notice: true,
      checkinAuto: true, checkinTravel: true, checkinDismissed: '',
      sideSwipeSec: 3, popSwipeSec: 5, warnDismissed: '', warnBelow: 500,
      trendRange: 30 },
    readPrefs()
  );
  // 趋势图区间也记住：上次看的是"本月"，重开程序还应该是"本月"
  state.range = prefs.trendRange || 30;

  function readPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); } catch (e) { return {}; }
  }
  function savePrefs() {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) { /* file:// 忽略 */ }
  }

  /* 界面运行态集中在这里。
     以前是 6 个散落的 let（主题、日历签名、流水签名、入场屏蔽…），
     查一个问题要在两千行里来回翻，还容易漏改。 */
  const ui = {
    lastPainted: null,     // 上次真正落地的主题
    themeTimer: null,
    lastCalSig: '',        // 日历重绘签名
    lastLedgerSig: '',     // 流水行内容签名
    lastWeekSig: '',       // （旧）总览活动卡点阵签名
    lastSideWeekSig: '',   // 左下角轮播卡点阵签名
    lastPopWeekSig: '',    // 小窗轮播卡点阵签名
    enterUntil: 0,         // 小窗入场期间屏蔽重绘的截止时刻
    enterTimer: 0
  };

  const systemDark = () => window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const resolved = () => (prefs.theme === 'system' ? (systemDark() ? 'dark' : 'light') : prefs.theme);
  const motionOff = () => prefs.motion === 'off';

  /* ---------------- 桌面桥接 ---------------- */

  const bridge = {
    api() { return (window.pywebview && window.pywebview.api) || null; },
    has() { const a = this.api(); return !!(a && typeof a.get_usage === 'function'); },
    can(name) { const a = this.api(); return !!(a && typeof a[name] === 'function'); },
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

  /* 口径计算全部在 metrics.js（纯函数、可单测）；这里注入实时数据源。
     下面按原名绑定，调用点一行都不用改。 */
  const Metrics = window.Metrics;
  Metrics.configure({
    data: () => D,
    account: () => ACCOUNT,
    prefs: () => prefs
  });
  const {
    startOfDay, addDays, diffDays, parseDate, dayKeyOf, round2, monthDayToDate,
    isWorkday, countWorkdays, allowance, leftDaysText,
    cloudRows, rowTime, lastSyncMs, localTail,
    todayStat, dailySeries, pendingSinceSync, effAccount, quotaWindow
  } = Metrics;

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
    d.recent = d.recent || [];
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
      if (a) {
        ACCOUNT = a;
        renderAvatar();        // 头像/昵称随账号一起更新，避免顶栏一直停在 "WB"
        renderAccountCard();   // 悬浮栏内容也同步刷新（隐藏时也写，展开即最新）
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  /* ---------------- 可用额度折算（工作日 / 签到） ---------------- */

  /* ---------------- 主题 ---------------- */


  /**
   * 主题切换：240ms 纯交叉溶解（View Transitions），不缩放、不扩散。
   * 切换时同步重绘一次图表——SVG 里的颜色是绘制那一刻取的值，
   * 不重绘的话，过渡结束会看到图表"跳色"。
   *
   * 注意：View Transitions 的更新回调要等"下一帧"才执行，而 WebView2 在关闭 GPU 合成
   * 或窗口未聚焦时可能长时间不产帧。因此这里加 60ms 看门狗：无论过渡是否真的开始，
   * 主题都会立即落地，绝不会出现"点了没反应"。
   */
  function applyTheme(pref, animate) {
    const next = pref === 'system' ? (systemDark() ? 'dark' : 'light') : pref;
    const commit = () => {
      paintTheme(next, true);
      clearTimeout(ui.themeTimer);
      renderActive(true);
    };
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
          clearTimeout(ui.themeTimer);
          ui.themeTimer = setTimeout(() => renderActive(true), 40);
        });
      }
      clearTimeout(ui.themeTimer);
      ui.themeTimer = setTimeout(() => renderActive(true), 320);
    } catch (e) {
      once();
      setTimeout(() => renderActive(true), 30);
    }
  }

  function paintTheme(theme, skipRender, fromSync) {
    if (theme === ui.lastPainted && fromSync) return;   // 被动同步且无变化：直接忽略
    ui.lastPainted = theme;
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
    clearTimeout(ui.themeTimer);
    ui.themeTimer = setTimeout(() => renderActive(true), skipRender ? 90 : 60);
    if (!fromSync) bridge.syncTheme(theme);          // 只有主动切换才通知另一端
  }

  function setTheme(pref, originEl) {
    prefs.theme = pref;
    savePrefs();
    applyTheme(pref, !motionOff());
  }

  /* ---------------- 偏好：跨窗口同步 ---------------- */

  /** 所有依赖"额度折算"的地方一次性重算。
   *  工作日折算 / 计入签到 一改，小窗、侧边栏、额度卡三处数字必须同时变，
   *  不能等用户切页或等下一次定时刷新。 */
  function renderQuotaSurfaces() {
    if (state.mode === 'popup') { renderPopup(); return; }
    renderSidebar();
    if (state.view === 'overview') renderOverview();
    else renderQuotaCard(effAccount(), hasQuota());
  }

  /** 把自己这份 prefs 推给另一个窗口（小窗与主界面是两个独立 WebView） */
  function pushPrefs() {
    if (!bridge.has()) return;
    try { bridge.call('sync_prefs', JSON.stringify(prefs)); } catch (e) { /* ignore */ }
  }

  /** 收到另一端的 prefs：合并 → 落盘 → 重算；绝不回推，避免来回弹球 */
  function applyPrefs(raw) {
    let p = raw;
    if (typeof raw === 'string') {
      try { p = JSON.parse(raw); } catch (e) { return false; }
    }
    if (!p || typeof p !== 'object') return false;
    const themeChanged = !!p.theme && p.theme !== prefs.theme;
    Object.keys(p).forEach(k => {
      if (Object.prototype.hasOwnProperty.call(prefs, k)) prefs[k] = p[k];
    });
    savePrefs();
    document.documentElement.setAttribute('data-motion', prefs.motion);
    if (themeChanged) { ui.lastPainted = null; paintTheme(resolved(), true, true); }
    if (state.mode === 'main') renderSettings();
    renderQuotaSurfaces();
    return true;
  }

  /** 兜底：窗口唤起时从 localStorage 对齐一次偏好。
   *  跨窗口推送偶发丢失（隐藏窗口被 WebView2 挂起）时，本地存储仍是最新的。 */
  function prefsFromStore() {
    const stored = readPrefs();
    let changed = false;
    Object.keys(stored).forEach(k => {
      if (Object.prototype.hasOwnProperty.call(prefs, k) && prefs[k] !== stored[k]) {
        prefs[k] = stored[k];
        changed = true;
      }
    });
    return changed;
  }

  /** 窗口显示前统一调用：对齐偏好 + 全量重算 */
  function beforeShow() {
    const changed = prefsFromStore();
    document.documentElement.setAttribute('data-motion', prefs.motion);
    if (changed) {
      ui.lastPainted = null;
      paintTheme(resolved(), true, true);
    }
    // 上次"收成小窗"的退场动画会留下 opacity:0，必须在重新显示前撤销
    if (exitAnim) {
      try { exitAnim.cancel(); } catch (e) { /* ignore */ }
      exitAnim = null;
    }
    renderActive(true);
    // 小窗每次重新显示都回到第一张「今日使用积分」：不记忆上次滑到哪，
    // 否则下次点开看到的是一张和"今天用了多少"无关的卡，得先滑回来。
    if (state.mode === 'popup') {
      if (popSwipe) popSwipe.reset();
    }
    return changed;
  }

  /* ---------------- 动效 / 计数 ---------------- */

  function setMotion(level) {
    prefs.motion = level;
    savePrefs();
    document.documentElement.setAttribute('data-motion', level);
    $$('#motionSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.motionChoice === level));
    renderActive();
    pushPrefs();
    toast(`动效已设为「${level === 'full' ? '完整' : level === 'reduced' ? '精简' : '关闭'}」`);
  }

  /**
   * 数值落地。
   * 现在会从"上一次的值"滚到新值（420ms），既有数字感又不会因为
   * WebView2 节流卡在中间——末尾有看门狗强制落到终值。
   */
  function countUp(node, target, opts) {
    if (!node) return;
    const o = opts || {};
    const format = o.format || (v => F.fmt(v, o.decimals || 0));
    const to = Number(target) || 0;
    const prev = node.dataset.cv;
    const from = prev != null ? Number(prev) : 0;
    const first = (prev == null);
    node.dataset.cv = String(to);
    // 首屏直接给终值：从 0 滚上去会有一瞬间的"0 分"闪烁，而且窗口若被节流还会停在 0
    if (first || motionOff() || document.hidden || Math.abs(to - from) < 0.005) {
      node.textContent = format(to);
      return;
    }
    const dur = 420;
    const t0 = performance.now();
    const step = t => {
      // rAF 回调的时间戳是"这一帧开始"的时刻，可能早于调用 countUp 的 t0，
      // 不夹住会让 p 变成负数，数字会先甩出一个负值（实测出现 -6,253）。
      const p = Math.min(1, Math.max(0, (t - t0) / dur));
      const e = 1 - Math.pow(1 - p, 3);
      node.textContent = format(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    setTimeout(() => { node.textContent = format(to); }, dur + 120);   // 看门狗
  }

  /* ---------------- 窗口出现时的入场动效 ---------------- */

  /**
   * 窗口"出现"发生在 Win32 层，页面收不到通知，因此由 Python 在 ShowWindow 之后调用。
   * 用 Web Animations API 而不是切 class：class 变化会牵动既有 CSS 动画重播，
   * 而 WAAPI 每次都是独立的一次播放，反复开关也不会残留状态。
   */
  let exitAnim = null;          // 收起动画：下次显示前必须 cancel，否则元素停在 opacity:0

  function playEnter(mode) {
    if (prefs.motion === 'off') return;
    const m = mode || state.mode;
    if (m === 'popup') { enterPopup(); return; }
    const groups = [['.topbar', 0], ['.sidebar', 20], ['.view.is-active .notice', 30],
                    ['.view.is-active .card', 60], ['.view.is-active .insight-strip', 90]];
    const dur = prefs.motion === 'reduced' ? 190 : 380;
    const jobs = [];
    groups.forEach(([sel, delay]) => {
      $$(sel).forEach((el, i) => {
        // 卡片按顺序错开，形成"从上往下铺开"的节奏
        const extra = sel === '.view.is-active .card' ? Math.min(i, 8) * 30 : 0;
        jobs.push([el, delay + extra, 12]);
      });
    });
    jobs.forEach(([el, delay, dy]) => {
      try {
        el.animate(
          [{ opacity: 0, transform: `translateY(${dy}px)` }, { opacity: 1, transform: 'none' }],
          { duration: dur, delay, easing: 'cubic-bezier(.16,1,.3,1)', fill: 'backwards' }
        );
      } catch (e) { /* 老内核不支持 WAAPI：直接显示，不影响功能 */ }
    });
  }

  /**
   * 小窗入场：内容分四拍浮起 + 一道斜向高光扫过。
   * 不做整体缩放 —— 窗口底色不透明，卡片一缩就会露出白边，看起来像"闪"。
   *
   * 两个细节决定"丝不丝滑"：
   * 1) 用双 rAF 把动画推到 beforeShow 的重绘**之后**再起，否则起手就掉一帧；
   * 2) 动画期间（ui.enterUntil）屏蔽外部触发的重绘——可见性变化、定时刷新都会来抢主线程，
   *    中途重排一次，动画就"卡一下"。
   */

  function enterPopup() {
    const reduced = prefs.motion === 'reduced';
    const start = () => {
      const jobs = [
        ['.pop-head', 0, -7],
        ['.pop-swipe', 45, 12],
        ['.pop-latency', 80, 8],
        ['.pop-block--hours', 110, 10],
        ['.pop-foot', 145, 8]
      ];
      const dur = reduced ? 200 : 440;
      jobs.forEach(([sel, delay, dy]) => {
        const el = $(sel);
        if (!el) return;
        try {
          el.animate(
            [{ opacity: 0, transform: `translateY(${dy}px)` }, { opacity: 1, transform: 'none' }],
            { duration: dur, delay, easing: 'cubic-bezier(.22,.9,.28,1)', fill: 'backwards' }
          );
        } catch (e) { /* ignore */ }
      });
      const card = $('.popup-card');
      if (card && !reduced) {
        const g = document.createElement('i');
        g.className = 'enter-glow';
        card.appendChild(g);
        setTimeout(() => g.remove(), 900);
      }
      // 时段柱图跟着一起"长起来"
      const popChart = $('[data-chart="popHourly"]');
      if (popChart) replayChart(popChart);
      const total = reduced ? 240 : 620;
      ui.enterUntil = performance.now() + total;
      clearTimeout(ui.enterTimer);
      ui.enterTimer = setTimeout(() => {
        ui.enterUntil = 0;
        renderActive(true);      // 动画结束后补一次渲染，把这段时间被挡下的更新落上去
      }, total + 20);
    };
    requestAnimationFrame(() => requestAnimationFrame(start));
  }

  /** 主窗收成小窗时的退场：内容略微内收 + 淡出，配合 Python 端的窗口几何收拢 */
  function playExit() {
    if (prefs.motion === 'off') return;
    const el = $('.app-shell');
    if (!el) return;
    try {
      exitAnim = el.animate(
        [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.985)' }],
        { duration: 170, easing: 'cubic-bezier(.4,0,.7,.2)', fill: 'forwards' }
      );
    } catch (e) { /* ignore */ }
  }

  /** 连击反馈：短时间内反复开关托盘小窗时，内容轻轻抖一下（窗口本身不动） */
  function burstShake() {
    if (prefs.motion === 'off') return;
    const el = $('.pop-body');
    if (!el) return;
    try {
      el.animate(
        [{ transform: 'translateX(0)' }, { transform: 'translateX(-2.5px)' },
         { transform: 'translateX(2.5px)' }, { transform: 'translateX(-1.5px)' },
         { transform: 'translateX(0)' }],
        { duration: 210, easing: 'ease-in-out' }
      );
    } catch (e) { /* ignore */ }
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
      setText('#sideCycle', `剩 ${leftDaysText(al)} · 日约 ${F.credit(al.perDay)}`);
    } else {
      setText('#sideCycle', '未连接云端');
    }
  }

  let lastRender = -1e9;

  /* ---------------- 图表入场：只在"看得见"的时候长起来 ---------------- */

  /** 元素是否大部分落在可视区（#views 是滚动容器） */
  function mostlyVisible(el) {
    const box = $('#views');
    const r = el.getBoundingClientRect();
    if (!r.height) return false;
    if (!box) return true;
    const v = box.getBoundingClientRect();
    const top = Math.max(r.top, v.top);
    const bot = Math.min(r.bottom, v.bottom);
    return (bot - top) / r.height >= 0.55;
  }

  /** 重绘一张图并播放它的生长动画 */
  function replayChart(el) {
    if (!el || typeof el.__draw !== 'function') return;
    el.dataset.played = '1';
    // 超宽屏上不给生长动画：图表要写几千个 SVG 属性，动画期间每秒几次全量重绘，
    // 屏幕越大越明显。直接落终态，观感损失很小，帧率立刻回来。
    el.dataset.anim = el.clientWidth > 2200 ? '0' : '1';
    el.__draw();
  }

  /** 扫描当前页：进视野的重绘一次（带动画），没进视野的继续保持"只出终态" */
  function revealCharts(replayAll) {
    let i = 0;
    $$('[data-chart]').forEach(el => {
      if (typeof el.__draw !== 'function') return;
      if (!mostlyVisible(el)) return;
      if (!replayAll && el.dataset.played === '1') return;
      // 错峰播放：大屏上一屏能同时看见四五个图表，全一起长会让主线程忙于写 SVG 属性，
      // 表现就是"屏幕越大越卡"。每隔 90ms 起一个，总时长多几百毫秒，但帧率稳得多。
      const delay = i++ * 90;
      if (!delay) { replayChart(el); return; }
      setTimeout(() => { if (el.isConnected) replayChart(el); }, delay);
    });
  }

  /** 只重画图表，不重建 DOM：窗口缩放走这条，比整页重渲染便宜一个量级 */
  function redrawCharts() {
    $$('[data-chart]').forEach(el => {
      if (typeof el.__draw === 'function' && el.isConnected) el.__draw();
    });
  }

  /** 切页时清掉"已播放"，让新页面的图表重新长一次 */
  function resetChartPlayed() {
    $$('[data-chart]').forEach(el => { delete el.dataset.played; });
    const cal = $('[data-chart="calendar"]');
    if (cal) delete cal.dataset.calPlayed;
    ui.lastCalSig = '';
  }

  /**
   * 错误边界：单个区块渲染失败只废掉那一块，其余照常。
   * 以前任何一个 render* 抛异常都会让整页停在半截（白屏），
   * 用户只会觉得"程序坏了"，而实际可能只是某个字段格式不对。
   */
  function safeRender(name, fn) {
    try {
      fn();
      return true;
    } catch (e) {
      console.error('[render:' + name + ']', e);
      // 同一块每会话只提示一次，避免定时刷新把 toast 刷屏
      if (!ui.renderFailed) ui.renderFailed = new Set();
      if (!ui.renderFailed.has(name)) {
        ui.renderFailed.add(name);
        try { toast(name + ' 渲染失败，其他区域不受影响', 'i-alert'); } catch (err) { /* ignore */ }
      }
      return false;
    }
  }

  function renderActive(force) {
    // 小窗入场期间不接受外部触发的重绘：可见性变化、定时刷新都会抢主线程，
    // 中途重排一次动画就"卡一下"。动画结束后由 enterPopup 统一补渲染。
    if (state.mode === 'popup' && performance.now() < ui.enterUntil) return;
    const now = performance.now();
    if (!force && now - lastRender < 250) return;
    lastRender = now;
    if (state.mode === 'popup') { safeRender('小窗', renderPopup); return; }
    // 先统一标记"只出终态"，渲染完再把视野内的重绘成带生长动画的版本
    try { $$('[data-chart]').forEach(el => { el.dataset.anim = '0'; }); } catch (e) { /* ignore */ }
    safeRender('侧栏', renderSidebar);
    safeRender('左下卡片', renderSideSwipe);   // 常驻，切页也要跟着数字走
    if (state.view === 'overview') {
      safeRender('总览', renderOverview);
      safeRender('签到条', renderCheckin);
    } else if (state.view === 'analytics') {
      safeRender('用量分析', renderAnalytics);
    } else if (state.view === 'ledger') {
      safeRender('流水', renderLedger);
    } else if (state.view === 'account' || state.view === 'settings') {
      // 数据源 / 版本 / 维护都在账户页，渲染函数仍是同一个（元素没变，只是换了位置）
      safeRender('设置', renderSettings);
      safeRender('今日状态', renderTodayStatus);
      safeRender('项目别名', renderAliases);
    }
    try { revealCharts(); } catch (e) { /* ignore */ }
  }

  let localTimer = null;
  /* ---------------- 总览提示条 ---------------- */

  /** 两段都隐藏时整条状态栏也收起来，别留一条空壳 */
  function syncStatusbar() {
    const bar = $('#statusbar');
    if (!bar) return;
    const data = $('#sbData');
    const ck = $('#sbCheckin');
    const dot = $('#sbDot');
    const showData = !!(data && !data.hidden);
    const showCk = !!(ck && !ck.hidden);
    if (dot) dot.hidden = !(showData && showCk);
    bar.hidden = !showData && !showCk;
  }

  /** 顶部那条明确提示：说清"哪个数字是实时的、哪个有延迟、当前读的是哪份数据"。 */
  function renderNotice(stat) {
    const el = $('#noticeText');
    if (!el) return;
    const seg = $('#sbData');
    if (seg) seg.hidden = prefs.notice === false;   // 设置里可关掉，把首屏留给数字
    // 数据段自己的两个按钮跟着一起收，否则条上只剩按钮、点 × 也不会消失
    ['#noticeSource', '#noticeClose'].forEach(s => {
      const n = $(s);
      if (n) n.hidden = prefs.notice === false;
    });
    const connected = ACCOUNT && ACCOUNT.status === 'connected';
    const sync = ACCOUNT && ACCOUNT.lastSync ? F.relTime(ACCOUNT.lastSync) : '';
    const iv = ACCOUNT && ACCOUNT.interval;
    const every = iv === 0 ? '手动' : (iv ? iv + ' 分钟' : '60 分钟');
    let txt;
    if (ACCOUNT && ACCOUNT.suspect) {
      // 连续多次非登录类失败：多半是服务端改了字段，而不是网络。
      // 这时候让人"再点一次同步"是浪费，直接指向检查更新。
      txt = `云端接口连续 <b>${ACCOUNT.failStreak || 3}</b> 次没取到数据，可能是服务端调整了字段。`
          + '先看看有没有新版本（设置 → 关于），再重新同步一次。'
          + (ACCOUNT.lastError ? ` <b class="warn-text">${esc(ACCOUNT.lastError)}</b>` : '');
    } else if (connected) {
      const bucketed = !!(ACCOUNT.cloud && ACCOUNT.cloud.countKind === 'bucket');
      txt = `云端每 ${every}同步（${sync || '—'}）：消耗 / 笔数取<b>${bucketed ? '计费记录' : '逐笔'}</b>`
          + (bucketed ? `（5 分钟 × 模型聚合，一笔 ≠ 一次调用）` : '')
          + `，Token 取本机。`
          + (ACCOUNT.softFail
              ? ` <b class="warn-text">⚠ 上次同步没成功（${esc(ACCOUNT.softFail)}），先显示上一次的数据</b>` : '')
          + (ACCOUNT.cloud && ACCOUNT.cloud.stale
              ? ` <b class="warn-text">⚠ ${esc(ACCOUNT.cloud.stale)}</b>` : '');
    } else {
      txt = `当前只有<b>本机记录</b>：消耗与 Token 实时，但<b>不含账号余额</b>；`
          + `点右上角头像 → 连接账号（扫码一次），之后自动显示可用积分与周期。`;
    }
    if (D.meta && D.meta.sourceKind === 'db') {
      txt += ' 未找到会话明细，已按 <b>workbuddy.db</b> 估算。';
    }
    // 逐笔合计与账号自报"已用"必然有差（周期边界 + 入账时序）。正常只差 1-2 个批次，
    // 所以只在 ≥3%（异常）时才占用提示条，避免天天挂一句让人麻木的免责声明。
    const c = ACCOUNT && ACCOUNT.cloud;
    if (connected && c && c.used > 0 && Math.abs(c.driftPct || 0) >= 0.03) {
      txt += ` · 逐笔与已用差 <b>${(c.driftPct * 100).toFixed(1)}%</b>（入账时序，以云端为准）`;
    }
    el.innerHTML = txt;

    // 未连接时整屏没有主按钮，引导只写在正文里（"点右上角头像"）。
    // 把这条提示的按钮直接变成入口，省得新人找不到下一步。
    const srcBtn = $('#noticeSource');
    if (srcBtn) {
      const needLogin = !connected;
      srcBtn.textContent = needLogin ? '扫码连接账号' : '数据源';
      srcBtn.dataset.act = needLogin ? 'login' : 'source';
      srcBtn.title = needLogin ? '打开扫码窗口，连上后自动显示余额与周期' : '去「账户」查看数据目录';
    }
    syncStatusbar();
  }

  /* ---------------- 每日签到结果条 ---------------- */

  let CHECKIN = null;

  /** 今天的日期串，用来做"当天只提示一次"的判定。 */
  function todayKey() {
    const d = new Date();
    return `${d.getFullYear()}-${F.pad(d.getMonth() + 1)}-${F.pad(d.getDate())}`;
  }

  function setCheckinIcon(id) {
    const use = $('#checkinIcon use');
    if (use) use.setAttribute('href', '#' + id);
  }

  /**
   * 三种结论，三种表述，别混：
   *   签到成功（绿）—— 这一次真的领到了 100 分
   *   今天已签过（中性）—— 正常状态，不是错误，别涂红
   *   失败（红）—— 才需要用户处理，并给"重试"
   */
  function buildCheckinText(r) {
    if (!r) return '正在签到并看猫猫…';
    if (r.ok === false) return `自动签到没成功：<b>${esc(r.error || '未知原因')}</b>`;
    const c = r.checkin || {};
    const t = r.travel || {};
    const bits = [];
    if (c.new) bits.push(`今日签到成功 <b>+${c.gain || 100}</b> 分`);
    else if (c.done) bits.push('今天已经签过，没有重复打卡');
    if (r.streak) bits.push(`连签 <b>${r.streak}</b> 天`);
    if (t.claimed) bits.push(`猫猫带回来 <b>${t.claimed}</b> 分`);
    if (t.departed) {
      const until = Math.round(Date.now() / 1000) + (t.remainingSec || 0);
      bits.push(`猫猫已出发去${esc(t.location || '逛街')}`
        + (t.remainingSec ? `，<span class="ck-eta" data-until="${until}">${esc(t.remainingText)}回来</span>` : ''));
    } else if (t.state === 'traveling') {
      // 到家用倒计时而不是死数字：签到条可能挂在屏幕上大半天，
      // 一直写"15 分钟后回来"就成了假消息。
      const until = Math.round(Date.now() / 1000) + (t.remainingSec || 0);
      bits.push(`猫猫在${esc(t.location || '旅行中')}，`
        + `<span class="ck-eta" data-until="${until}">${esc(t.remainingText || '过会儿')}回来</span>`);
      if (t.limitReached) bits.push('今日已派满');
    } else if (t.state === 'arrived') {
      bits.push(`猫猫已到家，<b>${t.reward || 0}</b> 分待领取`);
    } else if (t.state === 'idle' && t.limitReached) {
      // 每天只能派 1 次，领完也不重置，所以这里必须说清楚"不是漏了"
      // claimedToday 是按天累计、写在本地缓存里的，重启后这条也还在
      const gotToday = t.claimedToday || 0;
      bits.push(gotToday
        ? `猫猫今天带回来 <b>${gotToday}</b> 分`
        : (t.unknown ? '今日名额已用，没查到行程细节' : '今日猫猫已派过'));
    }
    if (t.error) bits.push(`<span class="dim">${esc(t.error)}</span>`);
    if (!bits.length) bits.push('签到检查完成');
    return bits.join(' · ');
  }

  function renderCheckin() {
    const seg = $('#sbCheckin');
    if (!seg) return;
    const bar = $('#statusbar');
    const retryEarly = $('#checkinRetry');
    const okEarly = $('#checkinOk');
    // 开关关了、或今天点过「我知道了」—— 签到那段整体收掉，
    // 连同「重试/我知道了」和整条的绿红边框一起复位，别留下一个孤零零的按钮。
    if (!prefs.checkinAuto || prefs.checkinDismissed === todayKey()) {
      seg.hidden = true;
      if (retryEarly) retryEarly.hidden = true;
      if (okEarly) okEarly.hidden = true;
      if (bar) bar.classList.remove('is-ok', 'is-bad');
      syncStatusbar();
      return;
    }
    if (okEarly) okEarly.hidden = false;

    const r = CHECKIN && CHECKIN.result;
    const today = todayKey();
    const isToday = !!(r && (!r.date || r.date === today));
    const running = !!(CHECKIN && CHECKIN.status === 'running');

    const txt = $('#checkinText');
    const retry = $('#checkinRetry');

    // 今天的结论还没出来（启动阶段要等账号同步把页面导好）：先站个位，
    // 直接把昨天那条结果摆上去会让人误以为是今天的。
    if (!isToday || running) {
      seg.hidden = false;
      if (bar) bar.classList.remove('is-ok', 'is-bad');
      setCheckinIcon('i-clock');
      if (txt) txt.innerHTML = running ? '正在签到并看猫猫…' : '正在准备签到…';
      if (retry) retry.hidden = true;
      syncStatusbar();
      return;
    }

    const failed = r.ok === false;
    const gotDaily = !!(r.ok && (r.checkin || {}).new);
    seg.hidden = false;
    if (bar) {
      bar.classList.toggle('is-ok', gotDaily);
      bar.classList.toggle('is-bad', failed);
    }
    setCheckinIcon(failed ? 'i-info' : 'i-check');
    if (txt) txt.innerHTML = buildCheckinText(r);
    if (retry) {
      // 没连账号时点「重试」必然再失败一次：这个按钮应该是「连接账号」
      const needLogin = failed && (r.reason === 'no_account' || r.reason === 'logged_out');
      retry.hidden = !failed;
      retry.textContent = needLogin ? '连接账号' : '重试';
      retry.dataset.act = needLogin ? 'login' : 'run';
    }
    syncStatusbar();
  }

  async function loadCheckin() {
    if (!bridge.has() || !bridge.can('get_checkin')) return false;
    try {
      const r = await bridge.call('get_checkin');
      if (r) { CHECKIN = r; renderCheckin(); renderTodayStatus(); return true; }
    } catch (e) { /* ignore */ }
    return false;
  }

  /* ---------------- 账户页：今日状态三栏 ---------------- */

  /** 三栏共用的写入器：chip 文案 + 主行 + 说明 + 警示边框。 */
  function putStatus(id, chip, chipCls, main, sub, warn) {
    const cEl = $('#' + id + 'Chip');
    if (cEl) { cEl.textContent = chip; cEl.className = 'chip' + (chipCls ? ' ' + chipCls : ''); }
    setText('#' + id + 'Main', main);
    setText('#' + id + 'Sub', sub);
    const card = cEl && cEl.closest ? cEl.closest('.status-card') : null;
    if (card) card.classList.toggle('is-warn', !!warn);
  }

  /** 动作按钮：检查出「能做」才出现，点一下按需执行那一件。 */
  function setAct(id, show, text, kind) {
    const b = $('#' + id);
    if (!b) return;
    b.hidden = !show;
    if (show && text) b.textContent = text;
    if (show && kind) b.dataset.act = kind;
  }

  /**
   * 签到 / 猫猫 / 领积分 三件事分开显示。
   * 以前三件事挤在一行结论里，任何一环判断错了都会把另外两环一起盖掉
   * （典型：猫猫没派、分没领，却显示"今日已完成"）。
   */
  function renderTodayStatus() {
    if (!$('#statusGrid')) return;
    const r = (CHECKIN && CHECKIN.result) || null;
    const isToday = !!(r && (!r.date || r.date === todayKey()));
    const c = (isToday && r.checkin) || {};
    const t = (isToday && r.travel) || {};

    if (!isToday) {
      ['stCk', 'stTv', 'stCl'].forEach(k => putStatus(k, '待检查', '', '还没有今天的结论',
        '点「立即检查」向云端要一次真实状态'));
      ['stCkAct', 'stTvAct', 'stClAct'].forEach(id => setAct(id, false));
      return;
    }

    /* 签到 */
    if (c.done) {
      putStatus('stCk', '已签到', 'chip-good',
        r.streak ? `连签 ${r.streak} 天` : '今日签到已完成',
        c.new ? `本次 +${c.gain || 100} 分` : '今天已经签过，没有重复打卡');
    } else {
      putStatus('stCk', '待签到', '', '今天还没签到',
        prefs.checkinAuto ? '自动签到开着，到点会自己签' : '自动签到已关闭，需要手动触发', true);
    }

    /* 猫猫行程 */
    const st = t.state;
    if (st === 'traveling') {
      putStatus('stTv', '旅行中', '', `在${t.location || '路上'}`,
        `已派出，预计 ${t.remainingText || '过一会儿'}回来`);
    } else if (st === 'arrived') {
      putStatus('stTv', '已到家', '', `带了 ${t.reward || 0} 分回来`,
        '猫猫已到家，积分还躺在那里没领', true);
    } else if (st === 'idle' && t.limitReached) {
      const got = t.claimedToday || 0;
      putStatus('stTv', '今日已派', '', got ? `今天已领 ${got} 分` : '今日派送名额已用完',
        '每天只能派 1 次，明天再来');
    } else if (st === 'idle') {
      putStatus('stTv', '可派送', 'chip-good', '今天还没派',
        prefs.checkinTravel ? '派猫猫开着，下次启动会自动出发' : '派猫猫已关闭，需要手动触发');
    } else {
      putStatus('stTv', '未知', '', '还没读到猫猫状态', '点「立即检查」或连接账号后再看');
    }

    /* 积分领取 */
    const pending = st === 'arrived' ? (t.reward || 0) : 0;
    const got = t.claimedToday || (t.claimed ? t.claimed : 0);
    if (pending > 0) {
      putStatus('stCl', '待领取', '', `${pending} 分还没领`,
        '派猫猫开着时下一轮会自动领；急的话先看看是不是开关关了', true);
    } else if (got > 0) {
      putStatus('stCl', '已领取', 'chip-good', `+${got} 分`, '今天猫猫带回来的积分已经入账');
    } else {
      putStatus('stCl', '—', '', '没有待领积分', '猫猫回来后会在这里提示');
    }

    /* 能做的事才给按钮：签到、领积分、派猫猫各管各的 */
    setAct('stCkAct', !c.done, '立即签到', 'checkin');
    const tvAct = st === 'arrived' ? ['立即领取', 'claim']
      : (st === 'idle' && !t.limitReached ? ['立即派送', 'travel'] : null);
    setAct('stTvAct', !!tvAct, tvAct && tvAct[0], tvAct && tvAct[1]);
    setAct('stClAct', pending > 0, '立即领取', 'claim');
  }

  /* ---------------- 定位高亮 ---------------- */

  let spotTimer = 0;

  /** 清掉所有定位标记：切页时不能把上一页的高亮带过去。 */
  function clearSpots() {
    $$('.is-spot').forEach(el => el.classList.remove('is-spot'));
  }

  /** 滚到目标并标一下：从 KPI 跳过来时，得让人知道"看哪儿"。 */
  function spotlight(sel) {
    const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
    if (!el) return;
    const view = el.closest('.view');
    if (view && !view.classList.contains('is-active')) return;   // 目标不在当前页，别乱标
    try {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } catch (e) {
      try { el.scrollIntoView(); } catch (e2) { /* ignore */ }
    }
    el.classList.remove('is-spot');
    void el.offsetWidth;                    // 强制回流，重新起一次过渡
    el.classList.add('is-spot');
    clearTimeout(spotTimer);
    spotTimer = setTimeout(() => el.classList.remove('is-spot'), 1900);
  }

  /* ---------------- 项目别名 ---------------- */

  let ALIASES = null;

  async function loadAliases() {
    if (!bridge.has() || !bridge.can('get_aliases')) return;
    try {
      const r = await bridge.call('get_aliases');
      ALIASES = (r && r.aliases) || {};
    } catch (e) {
      ALIASES = ALIASES || {};
    }
  }

  /** 当前数据里出现过的项目名（后端给全量，排行只给 Top5 不能当全量用）。 */
  function aliasCandidates() {
    if (Array.isArray(D.projectNames) && D.projectNames.length) {
      return D.projectNames.filter(n => n && n !== '其他项目' && n !== '未归类');
    }
    const seen = new Set();
    ['projects', 'projects7', 'projects30', 'projectsMonth'].forEach(k => {
      (D[k] || []).forEach(p => { if (p && p.name) seen.add(p.name); });
    });
    return Array.from(seen)
      .filter(n => n && n !== '其他项目' && n !== '未归类')
      .sort((a, b) => a.localeCompare(b, 'zh'));
  }

  function renderAliases() {
    const box = $('#aliasList');
    if (!box || box.closest('[hidden]')) return;      // 入口已隐藏，不再白建 28 行 DOM
    const all = aliasCandidates();
    const kwEl = $('#aliasSearch');
    const kw = ((kwEl && kwEl.value) || '').trim().toLowerCase();
    const list = kw ? all.filter(n => n.toLowerCase().includes(kw)) : all;
    setText('#aliasCount', `${list.length} / ${all.length}`);
    if (!list.length) {
      box.innerHTML = `<p class="set-note">${all.length ? '没有匹配的项目名' : '还没有可改名的项目（先产生一些本机调用记录）'}</p>`;
      return;
    }
    const cur = ALIASES || {};
    box.innerHTML = list.map(n => `
      <div class="alias-row">
        <code title="${esc(n)}">${esc(n)}</code>
        <input class="mini-input" data-alias="${esc(n)}" placeholder="留空 = 用原名"
               value="${esc(cur[n] || '')}" maxlength="24">
      </div>`).join('');
  }

  /** 按需执行单个动作（用户点了才算数）。 */
  async function runAction(kind, btn) {
    if (!kind || !bridge.has() || !bridge.can('checkin_action')) {
      toast('浏览器预览无法执行', 'i-info');
      return;
    }
    // 忙碌态用 class：chip 是 span，textContent 一改会把里面的猫猫倒计时冲掉
    if (btn) btn.classList.add('is-busy');
    try {
      const r = await bridge.call('checkin_action', kind);
      if (r && r.ok) {
        toast(r.message || '已完成');
        await loadCheckin();
        renderTodayStatus();
        setText('#stNote', `${r.at || ''} 执行：${r.message || '已完成'}`);
      } else {
        const msg = (r && r.error) || '执行失败，稍后再试';
        toast(msg, 'i-alert');
        setText('#stNote', msg);
      }
    } catch (e) {
      toast('执行失败：' + (e && e.message ? e.message : '未知错误'), 'i-alert');
    } finally {
      if (btn) btn.classList.remove('is-busy');
      // 按钮文案由状态渲染，不在这里硬恢复；否则会把「已签到」改回「立即签到」
      renderTodayStatus();
      renderPopActions();
    }
  }

  /** 「立即检查」：只读，Python 侧会把真实状态写回今日结论。 */
  async function probeStatus(btn) {
    if (!bridge.has() || !bridge.can('checkin_probe')) {
      toast('浏览器预览无法检查云端状态', 'i-info');
      return;
    }
    const old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '检查中…'; }
    try {
      const r = await bridge.call('checkin_probe');
      if (r && r.ok) {
        await loadCheckin();          // 校准后的结论在 get_checkin 里
        renderTodayStatus();
        setText('#stNote', `上次检查 ${r.at} · 只读，没有替你签到/领取`);
        toast('已读取云端真实状态');
      } else {
        const msg = (r && r.error) || '检查失败，稍后再试';
        setText('#stNote', msg);
        toast(msg, 'i-alert');
      }
    } catch (e) {
      setText('#stNote', '检查失败：' + (e && e.message ? e.message : '未知错误'));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  /** 把界面上的两个开关同步给 Python：它启动时要靠这个决定跑不跑签到。 */
  /** 开机自启存在注册表里，不走 prefs —— 每次进来都从系统读真实状态。 */
  async function loadAutostart() {
    if (!bridge.has() || !bridge.can('get_autostart')) return;
    try {
      const r = await bridge.call('get_autostart');
      const el = $('#optAutostart');
      if (el && r) el.checked = !!r.on;
    } catch (e) { /* ignore */ }
  }

  function pushCheckinOptions() {
    if (!bridge.has() || !bridge.can('checkin_options')) return;
    try { bridge.call('checkin_options', prefs.checkinAuto, prefs.checkinTravel); } catch (e) { /* ignore */ }
  }

  /* ---------------- 左下角轮播卡（额度 / 签到 / 猫猫 / 任务） ---------------- */

  let TASKS = null;

  async function loadTasks() {
    if (!bridge.has() || !bridge.can('get_checkin_tasks')) return false;
    try {
      const r = await bridge.call('get_checkin_tasks');
      if (r) { TASKS = r; return true; }
    } catch (e) { /* ignore */ }
    return false;
  }

  /** 环或 id 都能传：同一条渲染逻辑要给左下角卡片和小窗共用。 */
  function pick(x) { return (typeof x === 'string') ? $('#' + x) : x; }

  function paintRing(box, pct) {
    box = pick(box);
    if (!box) return;
    const size = 42, r = 17, c = 2 * Math.PI * r;
    if (!box.firstChild) {
      box.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">
        <circle class="ring-bg" cx="21" cy="21" r="${r}" fill="none" stroke-width="4"/>
        <circle class="ring-fg" cx="21" cy="21" r="${r}" fill="none" stroke-width="4"
                stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${c.toFixed(1)}"/>
      </svg>`;
    }
    const fg = box.querySelector('.ring-fg');
    if (!fg) return;
    const target = c * (1 - Math.max(0, Math.min(1, pct)));
    if (fg.dataset.v === String(target)) return;   // 重绘频繁，值没变就别再排一次 rAF
    fg.dataset.v = String(target);
    requestAnimationFrame(() => { fg.style.strokeDashoffset = target.toFixed(1); });
  }

  function paintBar(bar, pct, tone) {
    bar = pick(bar);
    if (!bar) return;
    const w = (Math.max(0, Math.min(1, pct)) * 100).toFixed(1);
    bar.className = (bar.classList.contains('side-fill') ? 'side-fill' : 'act-fill')
      + (tone && tone !== 'good' ? ' is-' + tone : '');
    if (bar.dataset.w !== w) {
      bar.dataset.w = w;
      bar.style.width = w + '%';
    }
  }

  /** 本周七天点阵：已签实心、今天带环、没到空心。 */
  function paintWeek(box, week, sigKey) {
    box = pick(box);
    if (!box) return;
    const todayIdx = (new Date().getDay() + 6) % 7;   // 周一 = 0
    const sig = JSON.stringify(week) + '|' + todayIdx;
    if (ui[sigKey] === sig) return;                   // 没变就别重建，省得每次重绘都弹一次
    ui[sigKey] = sig;
    const cls = box.classList.contains('side-week') ? 'side-week' : 'act-week';
    let html = '';
    for (let i = 0; i < 7; i++) {
      const on = !!(week && week[i]);
      html += `<i class="${on ? 'on' : ''}${i === todayIdx ? ' today' : ''}" style="--k:${i}"></i>`;
    }
    box.innerHTML = html;
  }

  /* ---------------- 滑动卡片（左下角 / 小窗共用） ---------------- */

  let sideSwipe = null;
  let popSwipe = null;

  /** 左下角四张卡：额度（静态）+ 签到 / 猫猫 / 任务。 */
  /** 把一份签到/旅行/任务状态渲染到任意一组元素上（左下角和小窗共用）。 */
  function paintActCards(p, weekSig) {
    const r = (CHECKIN && CHECKIN.result) || {};
    const t = r.travel || {};
    const fresh = r.date === todayKey();
    const st = t.state;

    paintWeek(p + 'Week', fresh ? r.week : null, weekSig);

    const ckChip = $('#' + p + 'CkChip');
    if (ckChip) {
      const done = !!r.checkedIn;
      // 小窗的 chip 没有标题栏，得自己带前缀；侧栏卡片上方已有「今日签到」
      ckChip.textContent = (p === 'pop' ? '签到 · ' : '') + (done ? '已签到' : (fresh ? '待签到' : '—'));
      ckChip.className = 'chip' + (done ? ' chip-good' : '');
    }
    const ckNote = $('#' + p + 'CkNote');
    if (ckNote) {
      const wk = (fresh && Array.isArray(r.week)) ? r.week.filter(Boolean).length : null;
      ckNote.textContent = wk === null ? '—' : `本周已签 ${wk} / 7 天`;
    }

    const tvChip = $('#' + p + 'TvChip');
    if (tvChip) {
      tvChip.textContent = st === 'traveling' ? '旅行中'
        : (st === 'arrived' ? '可领取' : (st === 'idle' ? (t.limitReached ? '今日已派' : '空闲') : '—'));
      // 今天的猫猫已经派过/收回，也算"完成"，和「已签到」一样给绿色
      tvChip.className = 'chip' + ((st === 'arrived' || (st === 'idle' && t.limitReached)) ? ' chip-good' : '');
    }
    // 小窗的猫猫不是独立 chip（和大数字挤在一起），单独同步一次颜色
    if (p === 'pop') {
      const etaChip = $('.pop-chip-eta');
      if (etaChip) etaChip.classList.toggle('chip-good', st === 'idle' && !!t.limitReached);
    }
    const tvMain = $('#' + p + 'TvMain');
    const tvUnit = $('#' + p + 'TvUnit');
    if (tvMain && tvUnit) {
      delete tvMain.dataset.until;      // 每次重绘先清掉，只有「旅行中」才挂倒计时
      if (st === 'traveling' && t.remainingSec > 0) {
        tvMain.classList.remove('as-text');
        tvMain.textContent = String(Math.max(1, Math.round(t.remainingSec / 60)));
        tvUnit.textContent = '分钟后回来';
        // 卡片也要跟着走秒：否则它停在"出发那一刻的分钟数"，一直到下次数据推送
        // （看猫定时器睡到猫到家，可能 4 小时后）才变 —— 用户看到的就是"数字不动"。
        tvMain.dataset.until = String(Math.round(Date.now() / 1000) + (t.remainingSec || 0));
        tvMain.dataset.p = p;
        paintBar(p + 'TvBar', t.progress || 0, 'good');
      } else if (st === 'arrived') {
        tvMain.classList.remove('as-text');
        tvMain.textContent = String(t.reward || 0);
        tvUnit.textContent = '分待领取';
        paintBar(p + 'TvBar', 1, 'good');
      } else if (st === 'idle') {
        if (t.limitReached) {
          tvMain.textContent = '今天已派满';
          tvMain.classList.add('as-text');
          tvUnit.textContent = '';
        } else {
          tvMain.textContent = '可以派出去';
          tvMain.classList.add('as-text');
          tvUnit.textContent = '';
        }
        paintBar(p + 'TvBar', t.limitReached ? 1 : 0, t.limitReached ? 'muted' : 'good');
      } else {
        tvMain.textContent = '—';
        tvMain.classList.remove('as-text');
        tvUnit.textContent = '';
        paintBar(p + 'TvBar', 0, 'good');
      }
    }
    const tvNote = $('#' + p + 'TvNote');
    if (tvNote) {
      const gotToday = t.claimedToday || 0;
      if (st === 'idle' && t.limitReached) tvNote.textContent = gotToday
        ? `今日已领 ${gotToday} 分 · 明天可再派`
        : '明天 0 点后可再派';
      else if (st === 'idle') tvNote.textContent = '每天可派 1 次';
      // 派满后猫还在路上：两个事实一起说，别让人以为"还没派"
      else if (st === 'traveling' && t.limitReached) tvNote.textContent = `今日已派满 · ${t.location || '在路上'}`;
      else tvNote.textContent = t.location || '—';
    }

    const tk = (TASKS && TASKS.ok) ? TASKS : null;
    const done = $('#' + p + 'TkDone');
    const total = $('#' + p + 'TkTotal');
    if (done) done.textContent = tk ? String(tk.completed) : '—';
    if (total) total.textContent = tk ? '/ ' + tk.total : '';
    const tpct = (tk && tk.total) ? tk.completed / tk.total : 0;
    paintRing(p + 'Ring', tpct);            // 左侧卡片用环
    const tbar = $('#' + p + 'TkBar');      // 小窗用横向条
    if (tbar) tbar.style.width = (tpct * 100).toFixed(1) + '%';
    const tkChip = $('#' + p + 'TkChip');
    if (tkChip) {
      const todo = (tk && tk.total) ? tk.total - tk.completed : null;
      const base = todo === null ? '—' : (todo > 0 ? todo + ' 个可做' : '全做完');
      tkChip.textContent = (p === 'pop' ? '任务 · ' : '') + base;
      tkChip.className = 'chip' + (todo === 0 ? ' chip-good' : '');
    }
    const tkNote = $('#' + p + 'TkNote');
    if (tkNote) {
      tkNote.textContent = tk
        ? `已领 ${F.credit(tk.claimedCredit || 0)} 分`
        : '—';
    }
  }

  function renderSideSwipe() {
    // 胶囊导航下侧栏整条不显示，没必要再刷新不可见的 DOM
    if (document.body.dataset.nav === 'b') return;
    paintActCards('side', 'lastSideWeekSig');
  }

  function renderPopSwipe() {
    paintActCards('pop', 'lastPopWeekSig');
  }
  function startEtaTicker() {
    setInterval(() => {
      const now = Math.round(Date.now() / 1000);
      $$('.ck-eta').forEach(el => {
        const left = (+el.dataset.until || 0) - now;
        el.textContent = left <= 0 ? '应该已经到家，重启程序可领取' : etaText(left);
      });
      // 猫猫卡片上的"X 分钟后回来"：和横幅一样按绝对时间走，不再是个死数字
      $$('[data-until]').forEach(el => {
        if (el.classList.contains('ck-eta')) return;
        const left = (+el.dataset.until || 0) - now;
        const unit = el.dataset.p ? $('#' + el.dataset.p + 'TvUnit') : null;
        if (left <= 0) {
          el.textContent = '到家了';
          el.classList.add('as-text');
          if (unit) unit.textContent = '';
        } else {
          el.textContent = String(Math.max(1, Math.round(left / 60)));
          el.classList.remove('as-text');
          if (unit) unit.textContent = '分钟后回来';
        }
      });
    }, 5000);
  }

  function etaText(sec) {
    const m = Math.round(sec / 60);
    if (m < 60) return `${Math.max(1, m)} 分钟后回来`;
    const h = Math.floor(m / 60), mm = m % 60;
    return `${h} 小时${mm ? ' ' + mm + ' 分钟' : ''}后回来`;
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

  /* ---------------- 小窗：今日消耗时段分布（复刻总览那张柱状图） ---------------- */

  /** 与总览「今日消耗时段分布」同一套 C.bar 组件、同一份数据，只是刻度与柱宽按小窗收窄。 */
  function renderPopHours(stat) {
    const s = stat || todayStat();
    setText('#popPeak', s.peakPoints ? `峰值 ${F.pad(s.peakHour)}:00 · ${F.credit(s.peakPoints)} 分` : '今日暂无消耗');
    setHTML('#popFootReq', F.fmt(s.requests));
    setHTML('#popFootAvg', (s.avgPerRequest || 0).toFixed(2));
    setHTML('#popFootToken', F.fmtWan(s.tokens || 0));
    const box = $('[data-chart="popHourly"]');
    if (!box) return;
    const nowHour = (D.meta && D.meta.nowHour != null) ? D.meta.nowHour : new Date().getHours();

    // 动态窗口：把今天**所有有消耗的时段**都包进来，而不是死板地只看最近 12 小时
    // （以前早上用过、晚上打开就只剩五六根柱子）。至少 8 格、最多 24 格，
    // 末尾留 1 格未来，让"现在"这根不贴边。
    const active = s.hours.filter(h => h.points > 0).map(h => h.hour);
    let start = active.length ? Math.min.apply(null, active) : Math.max(0, nowHour - 7);
    let end = Math.max(nowHour + 1, active.length ? Math.max.apply(null, active) : nowHour);
    if (end - start < 7) { end = Math.min(23, start + 7); start = Math.max(0, end - 7); }
    if (end - start > 23) { start = 0; end = 23; }

    const rows = s.hours.slice(start, end + 1);
    const xEvery = rows.length > 16 ? 4 : rows.length > 10 ? 3 : 2;
    const paint = () => C.bar(box, {
      data: rows.map(hr => ({
        label: `${F.pad(hr.hour)}:00`, short: F.pad(hr.hour), value: hr.points,
        isNow: hr.hour === nowHour,
        tipTitle: `${F.pad(hr.hour)}:00–${F.pad((hr.hour + 1) % 24)}:00`
      })),
      tone: 'c1', valueName: '消耗', sum: s.consumed,
      nowIndex: nowHour - start, xEvery, showValues: false, maxBarW: 18
    });
    box.__draw = paint;
    box.dataset.anim = '0';          // 默认只出终态；小窗弹出时由 replayChart 重绘带生长
    // 小窗首帧容器可能还是 0 高（boot 遮罩 / 布局未定型），量到尺寸再画，最多等 3 秒
    let n = 0;
    const wait = () => {
      if (box.clientWidth && box.clientHeight) { paint(); return; }
      if (n++ > 30) return;
      setTimeout(wait, 100);
    };
    wait();
  }

  /** 小窗快捷按钮：只显示"现在能做的那件"，做不了就置灰。 */
  /** 小窗状态 chip 本身就是操作入口：能做就点，不能做就淡掉并说明原因。
      以前上面一排状态、下面又排三个按钮，同一件事写两遍。 */
  function renderPopActions() {
    if (!$('#popCkChip')) return;
    const r = (CHECKIN && CHECKIN.result) || null;
    const isToday = !!(r && (!r.date || r.date === todayKey()));
    const c = (isToday && r.checkin) || {};
    const t = (isToday && r.travel) || {};
    const pending = t.state === 'arrived' ? (t.reward || 0) : 0;
    const can = bridge.has();

    const ck = $('#popCkChip');
    if (ck) {
      const off = !can || !!c.done;
      ck.classList.toggle('is-off', off);
      ck.dataset.act = off ? '' : 'checkin';
      ck.title = c.done ? '今天已经签过' : (can ? '点击立即签到' : '桌面端才能签到');
    }
    const tv = $('#popTravelChip');
    if (tv) {
      const claim = pending > 0;
      const dep = t.state === 'idle' && !t.limitReached;
      const off = !can || (!claim && !dep);
      tv.classList.toggle('is-off', off);
      tv.dataset.act = claim ? 'claim' : (dep ? 'travel' : '');
      tv.title = claim ? `点击领取 ${pending} 分`
        : dep ? '点击派猫猫出门'
          : t.state === 'traveling' ? '猫猫还在路上' : '今日名额已用完';
    }
  }

  /** 一句话摘要：发群/记笔记用（小窗「复制摘要」）。 */
  function buildDigestText() {
    const stat = todayStat();
    const acc = effAccount();
    const r = (CHECKIN && CHECKIN.result) || {};
    const t = r.travel || {};
    const parts = [`WorkBuddy 今日 ${F.credit(stat.consumed)} 分`];
    if (stat.consumed) parts.push(`${F.fmt(stat.requests)} 次调用`);
    if (hasQuota()) parts.push(`可用 ${F.credit(acc.available)} 分`);
    if (t.state === 'traveling') {
      parts.push(`猫猫在${t.location || '路上'}，${t.remainingText || '稍后'}回来`);
    } else if (t.state === 'arrived') {
      parts.push(`猫猫到家，${t.reward || 0} 分待领`);
    }
    return parts.join(' · ');
  }

  function renderPopup() {
    const stat = todayStat();
    const acc = effAccount();
    const configured = hasQuota();
    // 大字 = 今日使用积分；可用积分降为辅助小字（原顺序相反，主次不清）
    setText('#popAvailLabel', '今日使用积分');
    countUp($('#popTodayUsed'), stat.consumed, { duration: 760, format: F.credit });
    setText('#popAvailable', configured ? F.credit(acc.available) : '—');
    const popAvailEl = $('#popAvailable');
    if (popAvailEl) {
      popAvailEl.title = (acc.source === 'cloud')
        ? `云端余额 ${F.credit(acc.cloudAvailable)} − 同步后本机已花 ${F.credit(acc.pending)}（下限）= ${F.credit(acc.available)}`
        : '可用积分来自本地记录';
    }
    setText('#popUsedPct', configured ? (acc.usedPct * 100).toFixed(1) + '%' : '未设置');

    // 同步时间不再单独占一行（小窗要留给数字和图表），只在"同步云端"按钮上体现
    const al = allowance(acc);
    setText('#popCycle', configured
      ? `剩 ${leftDaysText(al)} · 每日约 ${F.credit(al.perDay)}`
      : '连接账号后自动计算');

    renderPopSwipe();

    setText('#popUpdated', `${source === 'real' ? '本机' : '演示'} ${D.meta.updated || '—'}`);
    const popCloud = $('#popCloud');
    if (popCloud) {
      const st = (ACCOUNT && ACCOUNT.lastSync) ? String(ACCOUNT.lastSync).slice(11, 16) : '';
      popCloud.textContent = ACCOUNT && ACCOUNT.lastSync
        ? `云端 ${F.relTime(ACCOUNT.lastSync)}` : '同步云端';
      popCloud.title = st ? `云端上次同步 ${ACCOUNT.lastSync}，点击立即同步` : '点击同步云端账号（手动一次，不改变自动间隔）';
    }

    const delta = $('#popDelta');
    if (delta) {
      const up = stat.deltaPct >= 0;
      delta.className = 'delta' + (up ? '' : ' is-good');
      delta.innerHTML = `<svg class="i"><use href="#${up ? 'i-up' : 'i-down'}"/></svg>${Math.abs(stat.deltaPct || 0).toFixed(1)}%`;
    }
    const prog = $('#popProgress');
    if (prog) requestAnimationFrame(() => { prog.style.width = ((configured ? acc.usedPct : 0) * 100).toFixed(2) + '%'; });

    renderPopHours(stat);
    renderPopActions();
  }

  /** 今日消耗构成：按「计费笔数 / 调用次数」拆分（云端是计费记录，本机是逐次调用） */
  function renderComposition(items) {
    const list = $('#compList'), donutBox = $('#compDonut');
    const cloudMode = !!cloudRows();
    setText('#compSub', cloudMode
      ? '按模型计费笔数拆分 · 颜色对应下方模型'
      : '按模型调用次数拆分 · 颜色对应下方模型');
    const rows = (items || []).filter(m => (m.calls || 0) > 0)
      .sort((a, b) => b.calls - a.calls);
    const total = rows.reduce((a, b) => a + b.calls, 0) || 1;

    if (donutBox) {
      const draw = () => {
        if (!rows.length) {
          donutBox.innerHTML = '<div class="comp-empty">今日暂无调用</div>';
          return;
        }
        C.donut(donutBox, {
          items: rows.map(m => ({ name: m.name, value: m.calls, tone: m.tone })),
          layout: 'bottom',
          legend: false,
          size: 232,
          hRatio: 0.84,
          hideCenter: true,                  // 环内不留任何文字，靠图例说明
          centerLabel: '',
          centerUnit: cloudMode ? '笔' : '次',
          // 环按笔数/次数拆分，tooltip 就说同一件事；积分只作附加信息，不抢主行
          valueLabel: cloudMode ? '计费笔数' : '调用次数',
          valueUnit: cloudMode ? '笔' : '次',
          extraTip: m => [{ name: '消耗', value: `${F.credit(m.points || 0)} 分` }],
          centerFormat: v => F.fmt(Math.round(v))
        });
      };
      donutBox.__draw = draw;
      draw();
    }

    if (list) {
      list.innerHTML = rows.map((m, i) => {
        const pct = (m.calls / total) * 100;
        return `<div class="comp-row">
          <i class="dot ${C.tonePat(i)}" style="background-color:${C.toneColor(m.tone)}"></i>
          <span class="nm" title="${esc(m.name)}">${esc(m.name)}</span>
          <span class="val">${F.fmt(m.calls)} ${cloudMode ? '笔' : '次'}</span>
          <span class="pct">${pct.toFixed(1)}%</span>
        </div>`;
      }).join('') || '<div class="comp-empty">今日暂无调用</div>';
    }
  }

  /** 额度 / 消耗预警：每天最多弹一次，不打扰。 */
  function maybeWarnQuota() {
    try {
      if (!ACCOUNT || ACCOUNT.status !== 'connected') return;
      const today = todayKey();
      if (prefs.warnDismissed === today) return;
      const al = allowance(effAccount());
      const msgs = [];
      const warnBelow = Number(prefs.warnBelow);
      // 阈值放在设置里：默认 500 对多数人合适，但消耗大的人永远不触发，
      // 填 0 表示不提醒可用积分（消耗异常提醒仍然保留）
      if (warnBelow > 0 && al.available > 0 && al.available < warnBelow) {
        msgs.push(`可用积分只剩 ${F.credit(al.available)} 分`);
      }
      const stat = todayStat();
      const prev = (D.daily || []).slice(-8, -1).map(d => d.points || 0);
      const avg = prev.length ? prev.reduce((a, b) => a + b, 0) / prev.length : 0;
      if (avg > 1 && stat.consumed > avg * 3 && stat.consumed > 10) {
        msgs.push(`今日消耗 ${F.credit(stat.consumed)} 分，是近 7 天均值的 ${(stat.consumed / avg).toFixed(1)} 倍`);
      }
      if (!msgs.length) return;
      prefs.warnDismissed = today;
      savePrefs();
      toast(msgs.join('；'), 'i-alert');
    } catch (e) { /* 预警失败不能影响主渲染 */ }
  }

  function renderOverview() {
    const acc = effAccount();
    const configured = hasQuota();
    const stat = todayStat();          // 今日口径：连上云端后 = 积分流水合计

    // 没连账号、也没有本地额度：四张 KPI 全是 0，不如换成一张能点的引导卡
    const needGuide = !(ACCOUNT && ACCOUNT.status === 'connected') && !configured;
    const guide = $('#kpiGuide');
    const kpiGrid = $('.kpi-grid');
    if (guide) guide.hidden = !needGuide;
    if (kpiGrid) kpiGrid.hidden = needGuide;
    if (needGuide) setHTML('#kgLocal', `本机今日 ${F.credit(stat.consumed)} 分 · ${F.fmt(stat.requests)} 次调用`);

    renderNotice(stat);
    setText('#kpi1Label', configured ? '可用积分' : '本周期消耗');
    countUp($('[data-count="available"]'), configured ? acc.available : acc.used, { format: F.credit });
    setText('#kpiAvailSub', configured ? `已用 ${(acc.usedPct * 100).toFixed(1)}%` : `累计 ${D.meta.days || 0} 天`);
    const syncedAt = (ACCOUNT && ACCOUNT.lastSync) ? String(ACCOUNT.lastSync).slice(11, 16) : '';
    setText('#kpiAvailNote', configured
      ? (acc.pending > 0.005
        ? `云端 ${F.credit(acc.cloudAvailable)} − 同步后已花 ${F.credit(acc.pending)}`
        : (syncedAt ? `云端 ${syncedAt} 同步` : `总额度 ${F.credit(acc.total)}`))
      : '未设置额度上限');
    const availChip = $('#kpiAvailSub');
    if (availChip) availChip.title = (acc.source === 'cloud')
      ? `可用积分 = 云端余额 ${F.credit(acc.cloudAvailable)} − 上次同步（${syncedAt || '—'}）之后本机已消耗 ${F.credit(acc.pending)}。`
        + '云端入账有延迟，先扣掉这段才接近此刻的真实可用；本机记录可能不全，实际未入账只会更多。'
      : '可用积分来自本地记录（未连接云端）';

    countUp($('[data-count="todayUsed"]'), stat.consumed, { format: F.credit });
    const delta = $('#kpiTodayDelta');
    if (delta) {
      const up = (stat.deltaPct || 0) >= 0;
      delta.className = 'delta' + (up ? '' : ' is-good');
      delta.innerHTML = `<svg class="i"><use href="#${up ? 'i-up' : 'i-down'}"/></svg>${Math.abs(stat.deltaPct || 0).toFixed(1)}%`;
    }

    countUp($('[data-count="todayReq"]'), stat.requests);
    // 云端返回的是"计费记录"（近期按 5 分钟 × 模型聚合），不是单次调用：
    // 拿条数当"调用次数"展示会差一个数量级，所以标签、单位、脚注一起切。
    const cloudCount = !!stat.cloud;
    setText('#kpi3Label', cloudCount ? '今日计费笔数' : '今日调用');
    const reqNode = $('[data-count="todayReq"]');
    if (reqNode && reqNode.nextElementSibling) reqNode.nextElementSibling.textContent = cloudCount ? '笔' : '次';
    setText('#kpiEarnedSub', '均价');
    setText('#kpiNetUnit', cloudCount ? ' 分/笔' : ' 分/次');
    setText('#kpiNet', (stat.avgPerRequest || 0).toFixed(2));

    const tk = stat.tokens || 0;
    const useYi = tk >= 1e8;
    const tkNode = $('[data-count="todayToken"]');
    countUp(tkNode, useYi ? tk / 1e8 : tk / 1e4, useYi ? { decimals: 2 } : {});
    if (tkNode && tkNode.nextElementSibling) tkNode.nextElementSibling.textContent = useYi ? '亿' : '万';
    // Token 只有本机口径，所以"平均上下文"必须用本机的调用次数做分母，
    // 不能拿云端的请求数去除（云端按请求计，本机按模型调用计，混用会算出离谱值）
    const reqForToken = (D.today && D.today.requests) || stat.requests;
    setText('#kpiAvg', `${F.fmtWan(tk / Math.max(1, reqForToken))} / 次`);

    // 四张卡各配一条 14 天迷你趋势：可用余额 / 每日消耗 / 每日调用 / 每日 Token
    const last14 = D.daily.slice(-14);
    const sparks = {
      available: last14.map(d => d.balance),
      todayUsed: last14.map(d => d.consumed),
      todayReq: last14.map(d => d.calls || 0),
      todayToken: last14.map(d => d.tokens || 0)
    };
    $$('[data-spark]').forEach(node => {
      C.sparkline(node, { values: sparks[node.dataset.spark] || [] });
    });

    // 洞察横条已删：同样的信息（今日消耗 / 峰值 / 主要模型）在 KPI 与构成卡里都有，
    // 再挂一条只会让首屏连着两条横幅。
    setText('#hourlyPeak', stat.peakPoints ? `峰值 ${F.pad(stat.peakHour)}:00 · ${F.credit(stat.peakPoints)} 分` : '今日暂无消耗');
    setHTML('#footReq', F.fmt(stat.requests));
    setHTML('#footAvg', (stat.avgPerRequest || 0).toFixed(2));
    setHTML('#footToken', F.fmtWan(stat.tokens || 0));
    setHTML('#footNow', D.meta.updated || '—');

    const nowHour = (D.meta && D.meta.nowHour != null) ? D.meta.nowHour : new Date().getHours();
    const hourlyEl = $('[data-chart="hourly"]');
    if (hourlyEl) {
      hourlyEl.__draw = () => C.bar(hourlyEl, {
        data: stat.hours.map(hr => ({
          label: `${F.pad(hr.hour)}:00`, short: F.pad(hr.hour), value: hr.points,
          isNow: hr.hour === nowHour,
          tipTitle: `${F.pad(hr.hour)}:00–${F.pad((hr.hour + 1) % 24)}:00`
        })),
        tone: 'c1', valueName: '消耗', sum: stat.consumed,
        nowIndex: nowHour, xEvery: 3, showValues: prefs.labels, maxBarW: 22
      });
      hourlyEl.__draw();
    }

    // 今日消耗构成：按调用次数拆分（免费模型也计入，只要今天被调用过）
    const compRows = (stat.models || []).filter(m => (m.calls || 0) > 0);
    renderComposition(compRows);
    setText('#compCount', compRows.length ? `${compRows.length} 个模型` : '—');

    renderTrend();

    renderQuotaCard(acc, configured);
    maybeWarnQuota();
  }

  /* ---------------- 额度与消耗：今日 / 本周 / 本月 三个口径 ---------------- */

  /** 上月 1 号到"上月同日"的消耗，用来做本月参照。云端只保留 29 天，覆盖不到就返回 null。 */
  function prevMonthSame() {
    const now = new Date();
    const from = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth(), 0).getDate();
    const to = new Date(now.getFullYear(), now.getMonth() - 1,
      Math.min(now.getDate(), lastDay), 23, 59, 59);
    const rows = cloudRows();
    if (!rows || !rows.length) return null;
    let earliest = Infinity, sum = 0;
    rows.forEach(r => {
      const t = rowTime(r);
      if (isNaN(t.getTime())) return;
      earliest = Math.min(earliest, t.getTime());
      if (t >= from && t <= to) sum += Number(r.credit) || 0;
    });
    // 记录还没覆盖到上月 1 号，算出来的数会偏低，不如不给
    if (!isFinite(earliest) || earliest > from.getTime()) return null;
    return round2(sum);
  }

  const QUOTA_LABEL = { day: '今日', week: '本周', month: '本月', cycle: '本期' };

  /** 区间内已消耗多少分：云端优先（与流水同源），否则回退 D.daily（只带 MM-DD） */
  /** 环图的"额度"跟着区间走：今日=每日可用，本周=本周可用，本月=按剩余工作日折算，
   *  本期=总额度（副标题显示周期剩余）。底部三项统计按数值从小到大排列，不随切换变化。 */
  function renderQuotaCard(acc, configured) {
    const kind = state.quotaRange || 'week';
    const lb = QUOTA_LABEL[kind] || '本周';
    const al = allowance(acc);
    const cycleUsed = acc.used || 0;
    const cycleLeft = acc.available || 0;

    let used = 0, budget = 0;
    if (kind === 'day') { used = quotaWindow('day').sum; budget = al.perDay; }
    else if (kind === 'cycle') { used = cycleUsed; budget = Math.max(1, cycleUsed + cycleLeft); }
    else if (kind === 'month') {
      // 本月没有现成额度：已用 + 剩下这几天能吃多少（perDay 已按剩余工作日折算）
      const now = new Date();
      const monthEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);
      const leftWork = countWorkdays(now, monthEnd, al.holidayOn);
      used = quotaWindow('month').sum;
      budget = used + al.perDay * Math.max(0, leftWork);
    } else { used = quotaWindow('week').sum; budget = al.perWeek; }

    const rawPct = budget > 0 ? used / budget : 0;
    const pct = Math.min(1, rawPct);
    // 超支/接近上限时，仪表盘换色（蓝 → 琥珀 → 红），不再"用超了还是一片安心绿"
    const tone = rawPct >= 1 ? 'over' : rawPct >= 0.85 ? 'warn' : 'ok';
    const over = Math.max(0, used - budget);
    const sub = !configured ? '点头像连接账号'
      : kind === 'cycle' ? `周期剩余 ${F.credit(cycleLeft)} · 已用 ${F.credit(cycleUsed)}`
        : kind === 'month'
          // 本月的分母是"已花 + 剩下还能花"，不是月初定的额度，所以百分比天然偏高。
          ? `已花 ${F.credit(used)} · 剩 ${F.credit(Math.max(0, budget - used))}`
          : budget > 0
            ? `额度 ${F.credit(budget)} · ${over > 0 ? '超 ' + F.credit(over) : '剩 ' + F.credit(budget - used)}`
            : '额度为 0';

    const gaugeEl = $('[data-chart="gauge"]');
    if (gaugeEl) {
      gaugeEl.__draw = () => C.gauge(gaugeEl, {
        value: configured ? pct : 0,
        countTo: configured ? rawPct : 0,
        tone: configured ? tone : 'ok',
        label: configured ? `${lb}已用` : '未连接云端',
        sub
      });
      gaugeEl.__draw();
    }

    // 三项统计：固定口径，按数值从小到大排序（小的在前）
    const stats = [
      { id: 'quotaWeek', label: '本周可用', value: al.perWeek },
      { id: 'quotaLeft', label: '周期剩余', value: al.available },
      { id: 'quotaDay', label: '每日可用', value: al.perDay, warn: true }
    ].sort((a, b) => (a.value || 0) - (b.value || 0));
    const statsBox = $('.quota-stats');
    if (statsBox) {
      statsBox.innerHTML = stats.map(s =>
        `<div><span>${s.label}</span><b id="${s.id}"${s.warn ? ' class="warn"' : ''}>${configured ? F.credit(s.value) : '—'}</b></div>`
      ).join('');
    }

    const qh = $('#quotaHint');
    if (qh) {
      const scope = al.holidayOn
        ? `按 ${al.spendDays} 个工作日折算 / 共 ${al.leftDays} 天`
        : `按 ${al.leftDays} 个自然日折算`;
      // 本月页签补一个参照：上月同期花了多少。没有数据就直说，不拿偏低值凑数。
      let extra = '';
      if (configured && kind === 'month') {
        const prev = prevMonthSame();
        extra = prev == null
          ? ' · 上月同期不在云端覆盖范围'
          : ` · 上月同期 ${F.credit(prev)} 分（截至同日）`;
      }
      qh.textContent = configured
        ? `${scope} · ${prefs.signin !== false ? '含每日签到 +' + F.credit(al.signin) + ' 分' : '不含签到'}${extra}`
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
    // 'month' = 本月 1 号到今天；固定天数则是滚动窗口。
    // 跨月时"30 天"会把两个月混在一起，看月度趋势需要这个口径。
    const isMonth = state.range === 'month';
    const span = isMonth ? new Date().getDate() : (Number(state.range) || 30);
    const days = dailySeries(span);    // 云端优先、缺失退本机，今天用实时合并值
    let run = 0;
    const cumulative = days.map(d => (run += d.consumed));
    const card = $('[data-chart="trend"]') && $('[data-chart="trend"]').closest('.chart-card');
    if (card) {
      const h2 = card.querySelector('h2');
      if (h2) h2.textContent = isMonth
        ? `本月消耗趋势（1–${new Date().getDate()} 日）`
        : `近 ${span} 天消耗趋势`;
    }
    const el = $('[data-chart="trend"]');
    if (!el) return;
    el.__draw = () => C.combo(el, {
      labels: days.map(d => d.date),
      days,
      bars: days.map(d => d.consumed),
      line: cumulative,
      lineName: '累计消耗',
      xLabels: span <= 7 ? 1 : span <= 14 ? 2 : 5,
      dimLast: true
    });
    el.__draw();
  }

  /* =========================================================
     用量分析
     ========================================================= */

  /** 统一的排行行（模型 / 项目共用同一套栅格，保证左右严格对齐） */
  function renderRows(el, items, opts) {
    if (!el) return;
    const o = opts || {};
    // keepZero：模型榜要把"免费用过"的模型也列出来，否则会出现
    // "今日构成 5 个模型、模型使用量只有 1 个"的自相矛盾。
    const list = (items || [])
      .filter(i => (i.points > 0 || (o.keepZero && (i.calls || 0) > 0)))
      .slice(0, o.limit || 6);
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
      const calls = (m.calls == null) ? '' : ` · ${F.fmt(m.calls)} ${cloudRows() ? '笔' : '次'}`;
      const val = m.points > 0 ? F.credit(m.points) : '<em class="rows-free">免费</em>';
      return `<div class="rows-row" data-name="${esc(m.name)}"
          data-points="${F.credit(m.points)}" data-pct="${pct.toFixed(1)}" data-calls="${m.calls == null ? '' : m.calls}"
          data-tokens="${m.tokens == null ? '' : m.tokens}">
        <span class="rows-idx">${String(i + 1).padStart(2, '0')}</span>
        <span class="rows-name" title="${esc(m.name)}${calls}">${esc(m.name)}</span>
        <span class="rows-track"><i data-w="${w.toFixed(1)}" style="width:0%;background:linear-gradient(90deg, ${C.hexToRgba(tone, .45)}, ${tone})"></i></span>
        <span class="rows-val">${val}</span>
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

  /* ---------------- 本机消耗（一个账号多台机器时的"这台"口径） ---------------- */

  const LOCAL_LABEL = { '7': '近 7 天', '30': '近 30 天', month: '本月', all: '全部' };

  /** 本机在指定周期的消耗。数据只来自本机会话记录，不掺云端账号口径。 */
  function localPeriodSum(range) {
    const daily = Array.isArray(D.daily) ? D.daily : [];
    const sum = arr => arr.reduce((s, d) => s + (Number(d.consumed) || 0), 0);
    if (range === 'all') {
      const all = Number(D.totalAll);
      return Number.isFinite(all) && all > 0 ? all : sum(daily);
    }
    if (range === 'month') {
      const now = new Date();
      return sum(daily.filter(d => {
        const dt = monthDayToDate(d.date, now);
        return dt && dt.getFullYear() === now.getFullYear() && dt.getMonth() === now.getMonth();
      }));
    }
    return sum(daily.slice(-Number(range)));
  }

  /** 云端逐笔的积分字段叫 credit（本机 ledger 才叫 points），两个都认。 */
  const rowCredit = r => {
    const v = Number(r && (r.credit != null ? r.credit : r.points));
    return Number.isFinite(v) ? v : 0;
  };

  /** 云端账号级消耗（含其他设备）。没有云端逐笔时返回 null。 */
  function cloudPeriodSum(range) {
    const rows = cloudRows();
    if (!rows || !rows.length) return null;
    let cut;
    if (range === 'all') {
      // 「全部」要对齐本机记录的起点，否则本机 60 天、云端 30 天，
      // 两边时间窗不一样，算出来的"其他设备"是假的。
      const first = (D.meta && D.meta.firstDayAll) || '';
      const from = first ? new Date(first + 'T00:00:00').getTime() : NaN;
      if (!Number.isFinite(from)) return rows.reduce((s, r) => s + rowCredit(r), 0);
      cut = from;
    } else if (range === 'month') {
      const now = new Date();
      cut = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    } else {
      cut = Date.now() - Number(range) * 86400000;
    }
    let total = 0, hit = 0;
    rows.forEach(r => {
      const t = rowTime(r);
      if (t && t.getTime() >= cut) { total += rowCredit(r); hit += 1; }
    });
    return hit ? total : null;
  }

  function renderLocalCard() {
    if (!$('#lcValue')) return;
    const range = state.localRange || '30';
    const local = localPeriodSum(range);
    setText('#lcValue', F.credit(local));
    setText('#lcMachine', (D.meta && D.meta.machine) ? D.meta.machine : '本机');

    const cloud = cloudPeriodSum(range);
    const connected = !!(ACCOUNT && ACCOUNT.status === 'connected');
    if (cloud && cloud > 0) {
      setText('#lcCloud', F.credit(cloud) + ' 分');
      setText('#lcShare', Math.round(Math.min(1, local / cloud) * 100) + '%');
      const other = cloud - local;
      setText('#lcOther', other > 0.5 ? F.credit(other) + ' 分' : '—');
    } else {
      setText('#lcCloud', '—');
      setText('#lcShare', '—');
      setText('#lcOther', '—');
    }

    const dailyArr = Array.isArray(D.daily) ? D.daily : [];
    const days = Number(D.meta && D.meta.daysAll)
      || dailyArr.filter(d => (Number(d.consumed) || 0) > 0).length;
    const first = (D.meta && D.meta.firstDayAll) || '';
    let note = range === 'all'
      ? (days ? `本机记录共 ${days} 天${first ? '，从 ' + first + ' 起' : ''}` : '本机还没有可用记录')
      : '只统计这台机器的 WorkBuddy 会话记录';
    if (!(cloud && cloud > 0)) {
      note += connected ? ' · 云端同期没有逐笔数据' : ' · 连上账号后可对比占比';
    }
    setText('#lcNote', note);
    $$('#lcSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.lc === range));
  }

  function renderAnalytics() {
    renderLocalCard();
    const cloudMode = !!cloudRows();
    setText('#modelSub', cloudMode
      ? '近 30 天按模型统计积分与计费笔数'
      : '近 30 天按模型统计积分与调用次数');
    /* ---------- 消耗日历：云端逐笔优先，本机记录补盲区 ---------- */
    const cal = calendarDays();
    const calDays = cal.days;
    const calTotal = calDays.reduce((a, b) => a + b.points, 0);
    const activeDays = calDays.filter(d => d.points > 0).length;
    setText('#weekSum', `区间合计 ${F.credit(calTotal)} 分`);
    setText('#calSub', `每格一天，从当月起按整月对齐 · 颜色越深消耗越多 · 悬停查看积分 / 次数 / Token`);

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
    const calBox = $('[data-chart="calendar"]');
    // 日历重绘很贵（210+ 个格子），而本地刷新每 30 秒就会走一次 renderActive。
    // 这里用「主题 + 宽度 + 偏好 + 数据」做签名：没变就不重绘，悬停光圈也不会被定时刷新打断。
    const calSig = [
      document.documentElement.getAttribute('data-theme'),
      calBox ? calBox.clientWidth : 0,
      prefs.calGlow !== false ? 1 : 0,
      prefs.calIntro !== false ? 1 : 0,
      calDays.map(d => `${d.date}:${d.points}:${d.calls}:${d.tokens}`).join(',')
    ].join('|');
    if (calBox) {
      // 供"进视野才播"的调度器重绘：日历自己的 calPlayed 决定要不要重播那条多米诺
      calBox.__draw = () => C.calendar(calBox, {
        days: calDays, tone: 'c1',
        glow: prefs.calGlow !== false,        // 悬停光圈 + 邻格联动
        animate: prefs.calIntro !== false     // 入场多米诺
      });
      if (calSig !== ui.lastCalSig) {
        ui.lastCalSig = calSig;
        calBox.__draw();       // 同步画：视图已经 display 了，量得到宽度；异步会和 replay 抢
      }
    }

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
    // 免费模型（0 分但有调用）也要留下：全部滤掉会出现
    // "今日构成 5 个模型、模型使用量只有 1 个"的自相矛盾。
    modelItems = modelItems
      .filter(m => (m.points || 0) > 0 || (m.calls || 0) > 0)
      .sort((a, b) => b.points - a.points);

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
            <div><span>模型</span><b>${m ? esc(m.name) : '—'}</b></div>
            <div><span>${cloudRows() ? '计费笔数' : '调用次数'}</span><b>${m && m.calls != null ? F.fmt(m.calls) + (cloudRows() ? ' 笔' : ' 次') : '—'}</b></div>
            <div><span>平均每次</span><b>${m && m.calls ? (m.points / m.calls).toFixed(2) + ' 分' : '—'}</b></div>
            <div><span>累计 Token</span><b>${tokens || '—'}</b></div>
          </div>
          <div class="ms-note">近 30 天只用过 <b>1 个模型</b>，没有可比的第二项；换成别的模型后这里会自动按模型拆分。</div>`;
        const box = $('#msDonut');
        if (box) {
          C.donut(box, {
            items: [{ name: m ? m.name : '—', value: m ? m.points : 0, tone: 'c1' }],
            legend: false,
            size: 156,
            hRatio: 0.96,
            // 环内一律不写字：156 的环径放 100% 会压到环上，看着像描边糊了
            hideCenter: true,
            centerLabel: '',
            centerFormat: () => '',
            centerUnit: ''
          });
        }
      } else {
        mlist.hidden = false;
        single.hidden = true;
        renderRows(mlist, modelItems, { limit: 6, empty: '暂无模型消耗', keepZero: true });
      }
    }

    /* ---------- 项目消耗排行：本机记录（云端不返回项目），周期可切 ---------- */
    const pr = state.projRange || '7';
    const projSrc = pr === '30' ? (D.projects30 || D.projects7)
      : pr === 'month' ? (D.projectsMonth || D.projects7) : D.projects7;
    const projItems = (projSrc || []).map(p => ({ name: p.name, points: p.points }));
    const pTotal = projItems.reduce((a, b) => a + b.points, 0);
    setText('#projTotal', projItems.length
      ? `${LOCAL_LABEL[pr]} ${cloudMode ? '本机 ' : ''}${F.credit(pTotal)} 分`
      : '暂无数据');
    setText('#projSub', `${LOCAL_LABEL[pr]} Top 6 项目，条形长度按消耗占比（仅本机记录，不含其他设备）`);
    renderRows($('#projectRank'), projItems, {
      limit: 6,
      empty: `${LOCAL_LABEL[pr]}暂无项目消耗（项目只来自本机会话记录）`
    });
    $$('#projSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.proj === pr));

    // 说明：这段在用量分析页，历史上也没有 #insightList 容器，一直未生效；
    // 额度预警已改到总览用 toast 提示（见 maybeWarnQuota）。
    const list = $('#insightList');
    if (list) {
      const connected = !!(ACCOUNT && ACCOUNT.status === 'connected');
      const items = (D.insights || []).filter(it => !(connected && /额度/.test(it.title || '')));
      list.innerHTML = items.map((it, i) => `
        <div class="insight-item" style="animation-delay:${i * 60}ms">
          <span class="badge"><svg class="i"><use href="#${it.icon || 'i-info'}"/></svg></span>
          <div><h4>${esc(it.title)}</h4><p>${it.text}</p></div>
        </div>`).join('');
    }
  }

  /* =========================================================
     流水
     ========================================================= */

  /* 动作口径：云端只给 agentPurpose，这里翻译成中文；未知就原样展示 */
  const PURPOSE_TEXT = {
    conversation: '对话生成', conversation_topic: '会话命名', 'enhance-prompt': '提示词优化',
    agent: 'Agent 任务', 'code-completion': '代码补全', 'inline-completion': '行内补全',
    edit: '编辑', 'apply-patch': '应用补丁', review: '代码评审', summarize: '总结',
    'image-generation': '图像生成', 'video-generation': '视频生成'
  };

  /**
   * 流水数据源：登录云端的账号优先用「云端逐笔」。
   * 原因：云端逐笔是账号级的，换一台电脑登录同一账号看到的是同一份；
   * 本机 JSONL 只覆盖"这台机器"，换机后昨天的明细就断了。
   * 代价：云端不返回项目（cwd）与 Token，这两列在云端行里显示 "—"。
   */
  function cloudLedgerRows() {
    const rows = (ACCOUNT && ACCOUNT.status === 'connected' && Array.isArray(ACCOUNT.usageRows))
      ? ACCOUNT.usageRows : null;
    if (!rows || !rows.length) return null;
    return rows.map((r, i) => ({
      id: 'c' + i,
      time: String(r.t || '').replace(' ', 'T'),
      type: 'consume',
      points: Number(r.credit) || 0,
      model: r.model || '未知模型',
      task: PURPOSE_TEXT[r.purpose] || r.purpose || '—',
      project: '—',
      tokens: null,
      requests: 1,
      tone: 'c1',
      src: 'cloud'
    }));
  }

  let ledgerSource = 'local';
  let ledgerCounts = { cloud: 0, local: 0 };   // 数据源条数（不受筛选影响，用于说明文案）

  function ledgerRows(baseSrc) {
    const q = state.ledgerQuery.trim().toLowerCase();
    const now = new Date();
    const todayKey = `${now.getFullYear()}-${F.pad(now.getMonth() + 1)}-${F.pad(now.getDate())}`;
    const src = baseSrc || ledgerAll();
    const f = state.ledgerFilter || '';
    const cut = f.indexOf(':');
    const fKind = cut > 0 ? f.slice(0, cut) : '';
    const fVal = cut > 0 ? f.slice(cut + 1) : '';
    return src.filter(e => {
      const t = e.time instanceof Date ? e.time : new Date(e.time);
      const key = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())}`;
      if (state.ledgerType === 'today' && key !== todayKey) return false;
      if (state.ledgerType === 'week' && (now - t) > 7 * 86400000) return false;
      // 模型 / 项目是精确匹配，搜索框是模糊匹配：前者用来"只看这个模型"，
      // 后者用来翻某次调用的上下文，两种需求不该挤在一个输入框里。
      if (fKind === 'model' && (e.model || '') !== fVal) return false;
      if (fKind === 'project' && (e.project || '') !== fVal) return false;
      if (!q) return true;
      return [e.model, e.task, e.project].join(' ').toLowerCase().includes(q);
    });
  }

  /** 未筛选的全集：筛选下拉的选项要基于它生成，否则选中一个模型后其它选项就消失了。 */
  function ledgerAll() {
    const cloud = cloudLedgerRows();
    const tail = cloud ? localTail() : [];
    const src = cloud ? cloud.concat(tail) : (D.ledger || []);
    ledgerSource = cloud ? 'cloud+local' : 'local';
    ledgerCounts = { cloud: cloud ? cloud.length : 0, local: cloud ? tail.length : src.length };
    src.sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime());
    return src;
  }

  /** 用全集刷新"按模型 / 按项目"下拉，保留当前选择。 */
  function syncLedgerFilter(all) {
    const sel = $('#ledgerFilter');
    if (!sel) return;
    const uniq = key => Array.from(new Set(all.map(e => e[key]).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'zh'));
    const models = uniq('model');
    const projects = uniq('project');
    const html = ['<option value="">全部模型 / 项目</option>'];
    if (models.length) {
      html.push('<optgroup label="按模型">');
      models.forEach(m => html.push(`<option value="model:${esc(m)}">${esc(m)}</option>`));
      html.push('</optgroup>');
    }
    if (projects.length) {
      html.push('<optgroup label="按项目">');
      projects.forEach(p => html.push(`<option value="project:${esc(p)}">${esc(p)}</option>`));
      html.push('</optgroup>');
    }
    const sig = html.join('');
    if (sel.dataset.sig !== sig) {
      sel.innerHTML = sig;
      sel.dataset.sig = sig;
    }
    sel.value = state.ledgerFilter || '';
    if (sel.value !== (state.ledgerFilter || '')) {
      // 数据集换了（换数据源 / 云端重登），原来选的模型不存在了：回到全部
      state.ledgerFilter = '';
      sel.value = '';
    }
  }

  function renderLedger(reset) {
    if (reset) state.ledgerLimit = 24;
    const full = ledgerAll();
    syncLedgerFilter(full);
    const all = ledgerRows(full);
    const rows = all.slice(0, state.ledgerLimit);
    const body = $('#ledgerBody');
    if (!body) return;

    // 占比与条形只按「消耗」行归一：赠送/退回不参与，否则会出现百分比超过 100%
    const maxPts = Math.max(...all.filter(e => e.type !== 'earn').map(e => e.points), 0.01);

    // 今日合计统一走 todayStat()：与总览 / 小窗 / 额度卡同一个口径，
    // 且不受"类型筛选 / 搜索框"影响（以前用筛选后的 all 求和，一搜索数字就变了）
    const stat = todayStat();
    const dayTotal = stat.consumed || 1;

    const hasCloud = ledgerSource !== 'local';
    setHTML('#ledgerSub', hasCloud
      ? `云端逐笔 ${F.fmt(ledgerCounts.cloud)} 条（仅最近 29 天、最多 1500 条）+ 同步后本机 ${F.fmt(ledgerCounts.local)} 条；项目与 Token 云端不返回`
      : `本机记录 · 共 ${F.fmt(D.meta.records || 0)} 次调用，覆盖 ${D.meta.days || 0} 天`);
    const sum = $('#ledgerSummary');
    if (sum) {
      const cloudSrc = hasCloud;
      // 今日 Token 只可能来自本机：云端 billing 逐笔不返回 token。
      // 之前云端模式下这里直接显示 "—"，与总览的今日 Token 对不上，现统一取本机同日累计并标注来源。
      const tk = stat.tokens || 0;
      const tkText = tk > 0 ? F.fmtWan(tk) : '—';
      const tkTip = '本机会话累计：每次调用的 totalTokens 含整段上下文，逐次相加会重复计入；云端逐笔不返回 Token，所以这一列固定取本机记录';
      sum.innerHTML = `
        <div><span>今日消耗${cloudSrc ? '<i class="src-tag">云端+本机</i>' : ''}</span><b>${F.credit(stat.consumed)} <small style="font-size:10px;color:var(--text-3)">分</small></b></div>
        <div><span>今日${cloudSrc ? '计费笔数' : '调用'}${cloudSrc ? '<i class="src-tag">云端+本机</i>' : ''}</span><b>${F.fmt(stat.requests)} <small style="font-size:10px;color:var(--text-3)">${cloudSrc ? '笔' : '次'}</small></b></div>
        <div title="${tkTip}"><span>今日 Token${cloudSrc && tk > 0 ? '<i class="src-tag">本机</i>' : ''}</span><b>${tkText}</b></div>
        <div><span>筛选结果</span><b>${all.length} <small style="font-size:10px;color:var(--text-3)">条</small></b></div>`;
    }

    // 行内容签名：本地刷新每 30 秒会走一次 renderActive，内容没变就不重建 —— 
    // 否则整张表会被反复替换，入场动画跟着重播，看着一直在闪。
    const ledgerSig = [
      state.ledgerLimit,
      prefs.groupMinute !== false ? 1 : 0,
      ledgerSource,
      // 筛选条件也要进签名：筛成 0 条时 rows 是空的，只靠行内容判断会以为"没变化"，
      // 结果表里还挂着上一次的记录。
      state.ledgerType, state.ledgerQuery, state.ledgerFilter || '',
      all.length,
      rows.map(e => `${e.id}|${new Date(e.time).getTime()}|${e.points}`).join(',')
    ].join('|');
    const rebuild = ledgerSig !== ui.lastLedgerSig;
    ui.lastLedgerSig = ledgerSig;

    if (!rebuild) {
      /* 内容未变：保留现有 DOM */
    } else if (!rows.length) {
      body.innerHTML = '<div class="pop-empty" style="padding:26px 0">当前筛选条件下没有记录</div>';
    } else {
      // 同一分钟的记录合并展示：首条显示时间与条数，其余用左侧导轨承接
      // 设置里可关掉「同分钟合并」，关掉后逐条独立显示
      const merge = prefs.groupMinute !== false;
      const gkeyOf = (t, i) => {
        const k = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())} ${F.clock(t)}`;
        return merge ? k : k + '#' + i;
      };
      const groups = {};
      rows.forEach((e, i) => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const key = gkeyOf(t, i);
        const g = (groups[key] = groups[key] || { list: [], sum: 0 });
        g.list.push(e);
        g.sum += e.points;
      });
      const seen = {};
      // 每天一个小标题：翻到这天才知道"这天一共花了多少"，不用自己心算
      const daySum = {};
      rows.forEach(e => {
        if (e.type === 'earn') return;
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const k = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())}`;
        daySum[k] = (daySum[k] || 0) + e.points;
      });
      const out = [];
      let lastDay = '';
      rows.forEach((e, i) => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const wd = '日一二三四五六'[t.getDay()];
        const dk = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())}`;
        if (dk !== lastDay) {
          lastDay = dk;
          out.push(`<div class="day-sep"><span>${F.dateKey(t)} 周${wd}</span><b>${F.credit(daySum[dk] || 0)} 分</b></div>`);
        }
        const key = gkeyOf(t, i);
        const group = groups[key] || { list: [], sum: 0 };
        const isFirst = !seen[key];
        seen[key] = true;
        const grouped = group.list.length > 1;
        // 首行展示的是该分钟合计，占比必须用合计算，否则会出现"-0.52 / 0.54%"这种对不上的组合
        const shownPts = (isFirst && grouped) ? group.sum : e.points;
        const share = shownPts / dayTotal;
        // 合并后同一张表里既有云端逐笔也有本机补足的记录，标一下来源，避免误读
        const srcText = hasCloud ? (e.src === 'cloud' ? '云端' : '本机') : '';
        const timeCell = isFirst
          ? `<span class="t">${F.dateKey(t)} ${F.clock(t)}${grouped ? `<i class="minute-badge">×${group.list.length}</i>` : ''}<small>周${wd}${srcText ? ' · ' + srcText : ''}</small></span>`
          : `<span class="t t--cont"><i class="rail"></i></span>`;
        // 同一分钟的积分合计放到「积分」列；时间徽标只留 ×N
        // 赠送 / 退回类记录（type=earn）不参与消耗占比，正号显示，避免出现 "-1,000 / 657%" 这种怪组合
        const isEarn = e.type === 'earn';
        const fmtPt = v => (isEarn ? '+' + F.credit(v) : (v > 0 ? '−' + F.credit(v) : '0'));
        // 颜色深浅跟消耗量走（和日历热力一个思路）：扣得越多，蓝得越实；越少越淡。
        const heat = Math.max(0, Math.min(1, shownPts / maxPts));
        const heatStyle = isEarn ? '' : ` style="color:${C.hexToRgba(C.toneColor('c1'), 0.5 + heat * 0.5)}"`;
        const ptCell = (isFirst && grouped)
          ? `<span class="pt pt-group"${heatStyle}>${fmtPt(group.sum)}<small>×${group.list.length} 合计</small></span>`
          : `<span class="pt${isFirst ? '' : ' is-sub'}"${isFirst ? heatStyle : ''}>${fmtPt(e.points)}</span>`;
        const shareCell = isEarn
          ? '<span class="share-cell"><span style="width:auto">—</span></span>'
          : `<span class="share-cell"><i class="share-bar"><i style="width:${Math.max(4, (shownPts / maxPts) * 100).toFixed(1)}%"></i></i><span>${(share * 100) < 1 ? (share * 100).toFixed(2) : (share * 100).toFixed(1)}%</span></span>`;
        const tokenTxt = e.tokens == null ? '' : `<em title="${F.fmt(e.tokens)} Token">${F.fmtWan(e.tokens)} tok</em>`;
        out.push(`<div class="table-row${isFirst ? '' : ' is-cont'}" style="animation-delay:${Math.min(i, 14) * 22}ms">
          ${timeCell}
          <span><i class="badge-type ${isEarn ? 'badge-earn' : 'badge-consume'}">${isEarn ? '赠送' : '消耗'}</i></span>
          <span class="nm"><i class="legend-dot" style="background:${C.toneColor(e.tone)}"></i><b title="${esc(e.model)}">${esc(e.model)}</b>${tokenTxt}</span>
          <span class="dim">${esc(e.task)}</span>
          <span class="dim col-project">${esc(e.project)}</span>
          ${ptCell}
          ${shareCell}
        </div>`);
      });
      body.innerHTML = out.join('');
    }

    const table = $('#ledgerTable');
    if (table) table.classList.toggle('is-cloud', ledgerSource !== 'local');
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

    // 未连接就只显示"连接"引导，不摆一堆空指标；连上之后换成余额视图
    const isLive = st === 'connected' && !!ACCOUNT.cloud;
    const emptyBox = $('#apEmpty'), liveBox = $('#apLive');
    if (emptyBox) emptyBox.hidden = isLive;
    if (liveBox) liveBox.hidden = !isLive;

    const c = ACCOUNT.cloud || {};
    const u = (ACCOUNT.user || {});
    const credit = v => (v == null ? '—' : F.credit(v) + ' 分');
    // 余额与"已用"都按"云端快照 − 本地待入账"后的实时值展示，与总览/小窗同一口径
    const acc = effAccount();
    const hasCloud = st === 'connected' && !!ACCOUNT.cloud;
    setText('#cloudAvail', credit(hasCloud ? acc.available : c.available));
    const availEl = $('#cloudAvail');
    if (availEl) {
      availEl.title = hasCloud
        ? `云端余额 ${F.credit(acc.cloudAvailable)} − 上次同步后本机已花 ${F.credit(acc.pending)} = ${F.credit(acc.available)}`
        : '';
    }
    setText('#cloudUsed', credit(hasCloud ? acc.used : c.used));
    // 余额条：周期已用占比（和侧边栏、KPI 同一套数）
    const availBar = $('#apAvailBar');
    if (availBar) {
      const pct = (hasCloud && acc.total) ? Math.min(1, Math.max(0, acc.usedPct)) : 0;
      requestAnimationFrame(() => { availBar.style.width = (pct * 100).toFixed(1) + '%'; });
    }
    setText('#cloudExpire', c.expireAtText || '—');

    // 账号身份
    setText('#accName', u.nickname || (st === 'connected' ? 'WorkBuddy 账号' : '未连接账号'));
    setText('#accPlan', u.plan ? `${u.plan}${u.phone ? ' · ' + u.phone : ''}` : '扫码后可读取余额与有效期');
    renderAvatar();

    // 头像状态点：连接成功=绿，过期/异常=黄，未连接=灰
    const dot = $('#avatarDot');
    if (dot) {
      dot.className = 'avatar-dot ' + (st === 'connected' ? 'is-on'
        : (st === 'expired' || st === 'error') ? 'is-warn' : 'is-off');
      dot.title = st === 'connected' ? '云端已连接' : st === 'expired' ? '登录已过期' : '未连接云端';
    }

    // 上次同步 / 间隔（渲染只回填值，事件在 bind 里只绑一次）
    // 这里只留"几点几分"，完整时间戳和"同步后已花多少"放到 title 里：
    // 一行挤三件事会把「立即同步」顶到第二行，看着像排版坏了。
    const pendingTxt = (hasCloud && acc.pending > 0.005)
      ? ` · 同步后本机已花 ${F.credit(acc.pending)} 分（可用积分按此扣减，属下限）` : '';
    const ls = ACCOUNT.lastSync || '';
    setText('#accLastSync', ls ? F.relTime(ls) + (ACCOUNT.lastError ? ' · 异常' : '') : '尚未同步');
    const lsEl = $('#accLastSync');
    if (lsEl) lsEl.title = ls ? ls + pendingTxt + (ACCOUNT.lastError ? ' · ' + ACCOUNT.lastError : '') : '';
    const sel = $('#cloudInterval');
    if (sel && document.activeElement !== sel) sel.value = String(ACCOUNT.interval == null ? 60 : ACCOUNT.interval);
    setText('#cloudHint', ACCOUNT.lastError
      ? `最近一次同步失败：${ACCOUNT.lastError}（登录态可能已过期，点「连接账号」重新扫码）`
      : isLive
        ? '数据只读；登录态经 DPAPI 加密保存在本机，不上传'
        : '扫码在官方页面完成，登录态只保存在本机（DPAPI 加密）');
  }

  /** 打开设置页时查一次版本（有缓存就不发请求）。有新版本才改副标题。 */
  let updateCheckedAt = 0;      // 上次真正请求的时间戳：同一小时内只查一次

  async function loadUpdate() {
    const note = $('#updateNote');
    if (!note || !bridge.has() || !bridge.can('check_update')) return;
    // 账户 / 设置页每轮渲染都会走到这里（60 秒刷新、切主题、切页），
    // 而失败不写缓存 —— 断网时不停重发请求，还会 8 秒超时。这里做 1 小时节流。
    if (Date.now() - updateCheckedAt < 60 * 60 * 1000) return;
    updateCheckedAt = Date.now();
    try {
      const r = await bridge.call('check_update');
      if (!r || !r.ok) return;
      if (r.hasUpdate) {
        note.innerHTML = `有新版本 <b>v${r.latest}</b>（当前 v${r.current}）· <a href="#" id="updateLink">去下载</a>`;
        const link = $('#updateLink');
        if (link) link.addEventListener('click', e => {
          e.preventDefault();
          if (bridge.can('open_url')) bridge.call('open_url', r.url);
        });
      } else {
        note.textContent = `已是最新（v${r.current}）`;
      }
    } catch (e) { /* 查不到不影响设置页 */ }
  }

  function renderSettings() {
    const acc = effAccount();
    renderAccountCard();

    setText('#srcCloud', ACCOUNT.status === 'connected'
      ? `已连接 · ${(ACCOUNT.user && ACCOUNT.user.plan) || '标准版'}`
      : '未连接');

    // 账户页顶部四行：账号 / 手机 / 上次同步 / 周期额度
    const u = ACCOUNT.user || {};
    const st = ACCOUNT.status;
    setText('#pgAccName', st === 'connected'
      ? (u.nickname || 'WorkBuddy 账号')
      : (st === 'expired' ? '登录已过期' : '未连接账号'));
    setText('#pgAccPhone', u.phone || '—');
    setText('#pgAccSync', ACCOUNT.lastSync ? F.relTime(ACCOUNT.lastSync) : '—');
    // 可用积分单列一行：从总览 KPI 点进来时，要有个明确的落点可以高亮
    const accEff = effAccount();
    setText('#pgAccAvail', hasQuota() ? F.credit(accEff.available) + ' 分' : '—');
    const cloud = ACCOUNT.cloud || {};
    setText('#pgAccQuota', cloud.total
      ? `${F.credit(cloud.used || 0)} / ${F.credit(cloud.total)}`
      : '—');
    setText('#srcRecords', F.fmt(D.meta.records || 0) + ' 条'
      + (D.meta.coverDays ? ` / 共 ${F.fmt(D.meta.recordsTotal || 0)}` : ''));
    setText('#srcDays', `${D.meta.days || 0} 天有消耗`);
    // 云端同步间隔只在「运行与刷新」里出现一次，数据源组不再重复显示只读值
    setText('#aboutVersion', appVersion);
    loadUpdate();     // 打开设置页时顺手查一次版本（一天缓存）
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
    if ($('#optOnTop')) $('#optOnTop').checked = !!prefs.onTop;
    if ($('#optDigest')) $('#optDigest').checked = prefs.digest !== false;
    if ($('#optCalGlow')) $('#optCalGlow').checked = prefs.calGlow !== false;
    if ($('#optCalIntro')) $('#optCalIntro').checked = prefs.calIntro !== false;
    if ($('#optGroupMinute')) $('#optGroupMinute').checked = prefs.groupMinute !== false;
    if ($('#optNotice')) $('#optNotice').checked = prefs.notice !== false;
    if ($('#optHoliday')) $('#optHoliday').checked = prefs.holiday !== false;
    if ($('#optSignin')) $('#optSignin').checked = prefs.signin !== false;
    if ($('#optCheckinAuto')) $('#optCheckinAuto').checked = prefs.checkinAuto !== false;
    if ($('#optCheckinTravel')) $('#optCheckinTravel').checked = prefs.checkinTravel !== false;
    const psSel = $('#optPopSwipe');
    if (psSel && document.activeElement !== psSel) psSel.value = String(prefs.popSwipeSec ?? 5);
    const lrSel = $('#optLocalRefresh');
    if (lrSel && document.activeElement !== lrSel) lrSel.value = String(prefs.localRefresh ?? 60);
    // 覆盖天数以服务端配置为准（meta 里带回），避免本地偏好与服务端配置打架
    const cdSel = $('#optCoverDays');
    if (cdSel && document.activeElement !== cdSel) {
      const cv = (D.meta && D.meta.coverDays != null) ? D.meta.coverDays : (prefs.coverDays || 0);
      cdSel.value = String(cv);
    }

    $$('#themeCards .theme-card').forEach(card => {
      card.classList.toggle('is-active', card.dataset.themeChoice === prefs.theme);
    });
    $$('#motionSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.motionChoice === prefs.motion));
  }

  /* =========================================================
     路由
     ========================================================= */

  function go(view) {
    clearSpots();
    if (state.view === view) return;
    state.view = view;
    $$('.nav-item, .tb-nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === view));
    requestAnimationFrame(moveTbPill);
    moveIndicator();
    const [t, s] = TITLES[view] || ['', ''];
    setText('#pageTitle', t);
    setText('#pageSub', s);
    const titleEl = $('#pageTitle');
    if (titleEl) {                       // 标题随页面切换轻轻落下，给"换页"一个落点
      titleEl.classList.remove('is-swap');
      void titleEl.offsetWidth;
      titleEl.classList.add('is-swap');
    }
    $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === view));
    $('#views').scrollTop = 0;
    // 换页 = 图表重新长一次（进视野的会在 renderActive 末尾被 replay）
    resetChartPlayed();
    if (view === 'ledger') {          // 流水页每次进来重新展开、重新入场
      state.ledgerLimit = 24;
      ui.lastLedgerSig = '';
    }
    // 整块内容做一次轻微的"落定"，避免旧页瞬间消失、新页直接怼上来
    const viewsBox = $('#views');
    if (viewsBox && !motionOff()) {
      try {
        viewsBox.animate(
          [{ opacity: .35, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
          { duration: 220, easing: 'cubic-bezier(.22,1,.36,1)' });
      } catch (e) { /* ignore */ }
    }
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

  /* ---------------- 加载遮罩 ---------------- */

  let loadingShownAt = 0, loadingTimer = 0;

  /** 慢操作才显示遮罩：<220ms 的操作一闪而过反而更卡顿。 */
  function showLoading(text) {
    const veil = $('#loadingVeil');
    if (!veil) return;
    setText('#loadingText', text || '正在读取数据…');
    clearTimeout(loadingTimer);
    loadingTimer = setTimeout(() => {
      veil.hidden = false;
      loadingShownAt = performance.now();
    }, 220);
  }

  function hideLoading() {
    clearTimeout(loadingTimer);
    const veil = $('#loadingVeil');
    if (!veil || veil.hidden) return;
    const left = 320 - (performance.now() - loadingShownAt);
    setTimeout(() => { veil.hidden = true; }, Math.max(0, left));
  }

  async function refresh() {
    const btn = $('#refreshBtn');
    if (btn) { btn.classList.add('is-spinning'); setTimeout(() => btn.classList.remove('is-spinning'), 720); }
    // 小窗：图标慢起慢收 + 顶部进度线一次充满 + 斜向高光 + 大数字轻跳
    const popBtn = $('#popRefresh');
    if (popBtn) { popBtn.classList.add('is-spinning'); setTimeout(() => popBtn.classList.remove('is-spinning'), 820); }
    const popCard = state.mode === 'popup' ? $('.popup-card') : null;
    if (popCard) {
      if (!popCard.querySelector('.refresh-bar')) {
        popCard.appendChild(Object.assign(document.createElement('div'), { className: 'refresh-bar' }));
      }
      // 连点刷新时 CSS 动画不会自己重播：先摘类、强制重排、再挂回去
      popCard.classList.remove('is-refreshing');
      void popCard.offsetWidth;
      popCard.classList.add('is-refreshing');
    }
    const cards = $$('.card');
    cards.forEach(c => {
      if (!c.querySelector('.refresh-bar')) c.appendChild(Object.assign(document.createElement('div'), { className: 'refresh-bar' }));
      c.classList.add('is-refreshing');
    });

    if (bridge.has()) {
      showLoading('正在刷新本机记录…');
      try {
        const raw = await bridge.call('refresh_usage');
        if (raw && raw.meta) applyUsage(raw, true);
        // 刷新只读本地记录：云端同步走 60 分钟定时或「立即同步」，避免频繁触发风控
      } catch (e) { /* ignore */ }
      hideLoading();
    }
    setTimeout(() => {
      cards.forEach(c => c.classList.remove('is-refreshing'));
      if (popCard) popCard.classList.remove('is-refreshing');
      renderActive(true);
      const t = new Date();
      setText('#syncTime', `${F.pad(t.getHours())}:${F.pad(t.getMinutes())}`);
      toast(bridge.has() ? '已重新读取本机用量' : '浏览器预览无法读取本机数据', bridge.has() ? 'i-check' : 'i-info');
    }, motionOff() ? 60 : (popCard ? 660 : 560));
  }

  /* =========================================================
     事件
     ========================================================= */

  /* ---------------- 导航方式：胶囊滑块 / 侧栏 ---------------- */

  /** 把白色滑块挪到当前选中项下面（带一点回弹，切换有"滑过去"的感觉）。 */
  function moveTbPill() {
    const nav = $('#tbNav'), pill = $('#tbNavPill');
    if (!nav || !pill) return;
    const active = nav.querySelector('.tb-nav-item.is-active');
    if (!active || !active.offsetWidth) { pill.style.opacity = '0'; return; }
    pill.style.opacity = '1';
    pill.style.width = active.offsetWidth + 'px';
    pill.style.transform = `translateX(${active.offsetLeft}px)`;
  }

  function applyNav(mode) {
    const m = ['a', 'b', 'c'].includes(mode) ? mode : 'b';
    document.body.dataset.nav = m;
    $$('#navSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.navChoice === m));
    requestAnimationFrame(moveTbPill);
    setTimeout(moveTbPill, 260);      // 字体就位后再量一次宽度，避免滑块偏一点
  }

  /* ---------------- 命令面板（C 方案） ---------------- */
  function setupCmdk() {
    const box = $('#cmdk'), input = $('#cmdkInput'), list = $('#cmdkList');
    if (!box || !input || !list) return;
    let rows = [], on = 0;

    const cmds = () => [
      { label: '总览', kind: '页面', run: () => go('overview') },
      { label: '用量分析', kind: '页面', run: () => go('analytics') },
      { label: '积分流水', kind: '页面', run: () => go('ledger') },
      { label: '账户', kind: '页面', run: () => go('account') },
      { label: '设置', kind: '页面', run: () => go('settings') },
      { label: '刷新本地数据', kind: '动作', run: () => refresh() },
      {
        label: '同步云端', kind: '动作',
        run: () => { if (bridge.has()) bridge.call('account_sync').then(() => loadAccount()).then(() => renderActive(true)); }
      },
      {
        label: '立即签到 / 看猫猫', kind: '动作',
        run: () => {
          if (!bridge.has()) return;
          bridge.call('checkin_run').then(r => { if (r) { CHECKIN = r; renderCheckin(); } });
        }
      },
      { label: '切换浅色 / 深色', kind: '动作', run: () => { const b = $('#themeBtn'); if (b) b.click(); } }
    ];

    const paint = () => {
      const kw = input.value.trim().toLowerCase();
      rows = cmds().filter(c => !kw || c.label.toLowerCase().includes(kw));
      on = 0;
      list.innerHTML = rows.length
        ? rows.map((c, i) => `<div class="cmdk-row${i === 0 ? ' is-on' : ''}" data-i="${i}">${esc(c.label)}<span class="cmdk-kind">${c.kind}</span></div>`).join('')
        : '<div class="cmdk-empty">没有匹配的命令</div>';
    };
    const open = () => { box.hidden = false; input.value = ''; paint(); input.focus(); };
    const close = () => { box.hidden = true; };
    const runAt = i => { const c = rows[i]; if (!c) return; close(); c.run(); };

    input.addEventListener('input', paint);
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (e.key === 'Enter') { e.preventDefault(); runAt(on); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!rows.length) return;
        on = (on + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        $$('.cmdk-row', list).forEach((r, i) => r.classList.toggle('is-on', i === on));
      }
    });
    list.addEventListener('mousedown', e => {
      const r = e.target.closest('.cmdk-row');
      if (r) runAt(+r.dataset.i);
    });
    box.addEventListener('mousedown', e => { if (e.target === box) close(); });

    const btn = $('#cmdBtn');
    if (btn) btn.addEventListener('click', open);
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); open(); }
    });
  }

  function bind() {
    $$('.nav-item, .tb-nav-item').forEach(b => b.addEventListener('click', () => go(b.dataset.view)));

    /* ---------- 导航方式：固定顶部胶囊（旧版侧栏已下掉，?nav=a 仅作调试） ---------- */
    const navParam = qs.get('nav');
    applyNav(['a', 'b', 'c'].includes(navParam) ? navParam : 'b');
    if (prefs.navCollapsed) document.body.classList.add('nav-collapsed');

    const collapseBtn = $('#navCollapse');
    if (collapseBtn) collapseBtn.addEventListener('click', () => {
      const on = document.body.classList.toggle('nav-collapsed');
      prefs.navCollapsed = on;
      savePrefs();
      setTimeout(() => {
        moveIndicator();
        window.dispatchEvent(new Event('resize'));   // 宽度变了，图表要重排
      }, 220);
    });

    setupCmdk();

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
        renderActive(true);
        pushPrefs();               // 小窗是另一个窗口，必须显式通知
      });
    };
    bindToggle('#optLabels', 'labels');
    bindToggle('#optCalGlow', 'calGlow', () => { if (state.view === 'analytics') renderAnalytics(); });
    bindToggle('#optCalIntro', 'calIntro', () => { if (state.view === 'analytics') renderAnalytics(); });
    bindToggle('#optGroupMinute', 'groupMinute', () => { if (state.view === 'ledger') renderLedger(true); });
    bindToggle('#optNotice', 'notice', () => { if (state.view === 'overview') renderOverview(); });
    // 签到两个开关：Python 启动时按它自己的配置决定跑不跑，本地偏好只做镜像，
    // 所以每次改动都要显式回写一次，不能只存 localStorage。
    bindToggle('#optCheckinAuto', 'checkinAuto', () => {
      pushCheckinOptions();
      renderCheckin();
    });
    bindToggle('#optCheckinTravel', 'checkinTravel', () => pushCheckinOptions());

    /* ---------- 导出：逐笔，或先按模型 / 按月汇总 ---------- */
    /** 汇总当前筛选后的流水（与屏幕上看到的完全一致）。 */
    function buildSummary(kind) {
      const rows = ledgerRows(ledgerAll());
      const use = rows.filter(e => e.type !== 'earn');
      if (!use.length) return null;
      const map = new Map();
      let total = 0, tokens = 0;
      use.forEach(e => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        const key = kind === 'model' ? (e.model || '未知模型')
          : `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}`;
        const m = map.get(key) || { points: 0, calls: 0, tokens: 0 };
        const p = Number(e.points) || 0;
        m.points += p;
        m.calls += 1;
        m.tokens += Number(e.tokens) || 0;
        total += p;
        tokens += Number(e.tokens) || 0;
        map.set(key, m);
      });
      const list = Array.from(map.entries());
      if (kind === 'model') list.sort((a, b) => b[1].points - a[1].points);
      else list.sort((a, b) => a[0].localeCompare(b[0]));
      const head = kind === 'model'
        ? ['模型', '积分', '条数', 'Token', '积分占比']
        : ['月份', '积分', '条数', 'Token', '积分占比'];
      const body = list.map(([k, v]) => [
        k, v.points.toFixed(2), v.calls, v.tokens,
        total > 0 ? (v.points / total * 100).toFixed(1) + '%' : '0%'
      ]);
      body.push(['合计', total.toFixed(2), use.length, tokens, '100%']);
      return { name: kind === 'model' ? '按模型汇总' : '按月汇总', head, rows: body };
    }

    const ledgerExport = $('#ledgerExport');
    const exportKindEl = $('#ledgerExportKind');
    if (ledgerExport) ledgerExport.addEventListener('click', async () => {
      if (!bridge.has()) return;
      const kind = exportKindEl ? exportKindEl.value : 'rows';
      ledgerExport.disabled = true;
      try {
        if (kind !== 'rows') {
          if (!bridge.can('export_rows_csv')) { toast('这个版本不支持汇总导出', 'i-info'); return; }
          const payload = buildSummary(kind);
          if (!payload) { toast('当前筛选下没有可导出的记录', 'i-info'); return; }
          const r = await bridge.call('export_rows_csv', payload.name,
            JSON.stringify(payload.head), JSON.stringify(payload.rows));
          if (r && r.ok) toast(`已导出 ${payload.name} ${r.count} 行：` + r.path);
          else if (!(r && r.cancelled)) toast('导出失败：' + ((r && r.error) || '未知错误'), 'i-alert');
          return;
        }
        if (!bridge.can('export_ledger_csv')) return;
        // 逐笔也按页面当前筛选导出：与屏幕完全一致，并带上本机补足的实时记录。
        // 旧实现直接读云端 usageRows，既无视筛选、也漏掉同步后的本机流水。
        const rows = ledgerRows(ledgerAll());
        if (!rows.length) { toast('当前筛选下没有记录', 'i-info'); return; }
        if (bridge.can('export_rows_csv')) {
          const head = ['时间', '类型', '模型', '动作', '项目', '积分', 'Token'];
          const body = rows.map(e => {
            const t = e.time instanceof Date ? e.time : new Date(e.time);
            const stamp = `${t.getFullYear()}-${F.pad(t.getMonth() + 1)}-${F.pad(t.getDate())} `
              + `${F.pad(t.getHours())}:${F.pad(t.getMinutes())}:${F.pad(t.getSeconds())}`;
            return [stamp, e.type === 'earn' ? '获得' : '消耗', e.model || '', e.task || '',
              e.project || '', e.points || 0, e.tokens == null ? '' : e.tokens];
          });
          const rr = await bridge.call('export_rows_csv', 'WorkBuddy-流水',
            JSON.stringify(head), JSON.stringify(body));
          if (rr && rr.ok) toast('已导出 ' + rr.count + ' 条：' + rr.path);
          else if (!(rr && rr.cancelled)) toast('导出失败：' + ((rr && rr.error) || '未知错误'), 'i-alert');
          return;
        }
        const r = await bridge.call('export_ledger_csv');
        if (r && r.ok) toast('已导出 ' + r.count + ' 条：' + r.path);
        else if (!(r && r.cancelled)) toast('导出失败：' + ((r && r.error) || '未知错误'), 'i-alert');
      } catch (e) {
        toast('导出失败', 'i-alert');
      } finally {
        ledgerExport.disabled = false;
      }
    });

    const weeklyBtn = $('#btnWeekly');
    if (weeklyBtn) weeklyBtn.addEventListener('click', async () => {
      if (!bridge.has() || !bridge.can('export_weekly_report')) {
        toast('浏览器预览无法导出', 'i-info');
        return;
      }
      weeklyBtn.disabled = true;
      try {
        const r = await bridge.call('export_weekly_report');
        if (r && r.ok) toast('周报已保存：' + r.path);
        else if (!(r && r.cancelled)) toast('导出失败：' + ((r && r.error) || '未知错误'), 'i-alert');
      } catch (e) {
        toast('导出失败', 'i-alert');
      } finally {
        weeklyBtn.disabled = false;
      }
    });

    const aliasSave = $('#aliasSave');
    if (aliasSave) aliasSave.addEventListener('click', async () => {
      if (!bridge.has() || !bridge.can('save_aliases')) {
        toast('浏览器预览无法保存', 'i-info');
        return;
      }
      const out = {};
      $$('#aliasList input[data-alias]').forEach(inp => {
        const v = inp.value.trim();
        if (v) out[inp.dataset.alias] = v;
      });
      aliasSave.disabled = true;
      try {
        const r = await bridge.call('save_aliases', out);
        if (r && r.ok) {
          await loadAliases();
          renderAliases();
          await reloadAll();          // 重新取数，排行与流水立刻用新名字
          toast(`已保存 ${r.count} 个别名`);
        } else {
          toast('保存失败：' + ((r && r.error) || '未知错误'), 'i-alert');
        }
      } catch (e) {
        toast('保存失败', 'i-alert');
      } finally {
        aliasSave.disabled = false;
      }
    });
    loadAliases().then(() => { if (state.view === 'settings') renderAliases(); });
    const aliasSearch = $('#aliasSearch');
    if (aliasSearch) aliasSearch.addEventListener('input', renderAliases);

    const diagBtn = $('#btnDiag');
    if (diagBtn) diagBtn.addEventListener('click', async () => {
      if (!bridge.has() || !bridge.can('export_diagnostics')) return;
      diagBtn.disabled = true;
      try {
        const r = await bridge.call('export_diagnostics');
        if (r && r.ok) toast('已导出：' + r.path);
        else if (!(r && r.cancelled)) toast('导出失败：' + ((r && r.error) || '未知错误'), 'i-alert');
      } catch (e) {
        toast('导出失败', 'i-alert');
      } finally {
        diagBtn.disabled = false;
      }
    });

    /* ---------- 维护：清缓存 / 清日志（两次点击才算确认，不依赖原生 confirm） ---------- */
    const bytesText = n => {
      const v = Number(n) || 0;
      if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB';
      if (v >= 1024) return Math.round(v / 1024) + ' KB';
      return v + ' B';
    };
    const loadMaintenance = async () => {
      if (!bridge.has() || !bridge.can('maintenance_info')) return;
      try {
        const r = await bridge.call('maintenance_info');
        if (!r) return;
        setText('#cacheSize', bytesText(r.cacheBytes));
        setText('#logSize', bytesText(r.logBytes));
        const led = r.ledger || {};
        setText('#ledgerSize', led.days
          ? `${led.days} 天 · ${bytesText(led.bytes)}`
          : '还没有记录');
        setText('#ledgerNote', led.days
          ? `从 ${led.first} 到 ${led.last}；WorkBuddy 清理旧会话后，这段历史仍能追溯`
          : '每天一条聚合记录；WorkBuddy 清理旧会话后，历史消耗仍能追溯');
      } catch (e) { /* 只影响设置页的显示 */ }
    };
    loadMaintenance();

    function armDanger(btn, label, fn) {
      let timer = null;
      btn.addEventListener('click', () => {
        if (btn.dataset.armed === '1') {
          clearTimeout(timer);
          delete btn.dataset.armed;
          btn.classList.remove('is-danger');
          btn.textContent = label;
          fn();
          return;
        }
        btn.dataset.armed = '1';
        btn.classList.add('is-danger');
        btn.textContent = '再点一次';
        timer = setTimeout(() => {
          delete btn.dataset.armed;
          btn.classList.remove('is-danger');
          btn.textContent = label;
        }, 4000);
      });
    }

    const btnClearCache = $('#btnClearCache');
    if (btnClearCache) armDanger(btnClearCache, '清空', async () => {
      btnClearCache.disabled = true;
      try {
        const r = await bridge.call('clear_usage_cache');
        if (r && r.ok) {
          toast(r.removed ? '缓存已清空，已重新读取本机记录' : '缓存本来就是空的');
          loadMaintenance();
        } else {
          toast('清空失败：' + ((r && r.error) || '未知错误'), 'i-alert');
        }
      } catch (e) {
        toast('清空失败', 'i-alert');
      } finally {
        btnClearCache.disabled = false;
      }
    });

    const btnClearLedger = $('#btnClearLedger');
    if (btnClearLedger) armDanger(btnClearLedger, '清空', async () => {
      btnClearLedger.disabled = true;
      try {
        const r = await bridge.call('clear_usage_ledger');
        if (r && r.ok) {
          toast('本机台账已清空，下次刷新会从现有记录重新累计');
          loadMaintenance();
        } else {
          toast('清空失败：' + ((r && r.error) || '未知错误'), 'i-alert');
        }
      } catch (e) {
        toast('清空失败', 'i-alert');
      } finally {
        btnClearLedger.disabled = false;
      }
    });

    const btnClearLogs = $('#btnClearLogs');    if (btnClearLogs) armDanger(btnClearLogs, '清空', async () => {
      btnClearLogs.disabled = true;
      try {
        const r = await bridge.call('clear_logs');
        if (r && r.ok) { toast('日志已清空'); loadMaintenance(); }
        else toast('清空失败：' + ((r && r.error) || '未知错误'), 'i-alert');
      } catch (e) {
        toast('清空失败', 'i-alert');
      } finally {
        btnClearLogs.disabled = false;
      }
    });

    const autoEl = $('#optAutostart');
    if (autoEl) autoEl.addEventListener('change', async () => {
      const want = autoEl.checked;
      autoEl.disabled = true;
      try {
        const r = await bridge.call('set_autostart', want);
        if (!r || !r.ok) {
          autoEl.checked = !want;
          toast('设置失败：可能被安全软件拦了注册表写入', 'i-alert');
          return;
        }
        autoEl.checked = !!r.on;
        toast(r.on ? '已开启开机自启（静默到托盘）' : '已关闭开机自启');
      } catch (e) {
        autoEl.checked = !want;
        toast('设置失败', 'i-alert');
      } finally {
        autoEl.disabled = false;
      }
    });

    const psOpt = $('#optPopSwipe');
    if (psOpt) psOpt.addEventListener('change', () => {
      prefs.popSwipeSec = +psOpt.value;
      savePrefs();
      if (popSwipe) popSwipe.setInterval(prefs.popSwipeSec * 1000 || 0);
    });
    // 这两个开关直接决定"每日可用 / 每周可用"的折算结果：所有展示额度的地方必须同步刷新
    bindToggle('#optHoliday', 'holiday', renderQuotaSurfaces);
    bindToggle('#optSignin', 'signin', renderQuotaSurfaces);
    bindToggle('#optTray', 'tray', () => bridge.call('set_close_to_tray', prefs.tray));

    /* 窗口置顶：偏好存本地，启动时立刻应用一次（Python 侧也记着，唤回窗口会续上） */
    const topEl = $('#optOnTop');
    if (topEl) {
      topEl.checked = !!prefs.onTop;
      topEl.addEventListener('change', () => {
        prefs.onTop = topEl.checked;
        savePrefs();
        if (bridge.has()) bridge.call('set_always_on_top', prefs.onTop);
      });
      if (prefs.onTop && bridge.has()) bridge.call('set_always_on_top', true);
    }

    /* 每晚小结：默认开，Python 侧 22:00 后推一条 */
    const dgEl = $('#optDigest');
    if (dgEl) {
      dgEl.checked = prefs.digest !== false;
      dgEl.addEventListener('change', () => {
        prefs.digest = dgEl.checked;
        savePrefs();
        if (bridge.has()) bridge.call('set_digest', prefs.digest);
      });
      if (bridge.has()) bridge.call('set_digest', prefs.digest !== false);
    }

    const warnBelowEl = $('#optWarnBelow');
    if (warnBelowEl) {
      warnBelowEl.value = String(prefs.warnBelow == null ? 500 : prefs.warnBelow);
      warnBelowEl.addEventListener('change', () => {
        const v = Math.max(0, Math.min(1000000, Math.round(Number(warnBelowEl.value) || 0)));
        prefs.warnBelow = v;
        warnBelowEl.value = String(v);
        savePrefs();
        toast(v > 0 ? `可用积分低于 ${F.credit(v)} 分时提醒` : '已关闭可用积分提醒');
      });
    }

    const lr = $('#optLocalRefresh');
    if (lr) lr.addEventListener('change', () => {
      const v = +lr.value;
      if (v === +prefs.localRefresh) return;      // 忽略表单状态恢复导致的伪变更
      prefs.localRefresh = v;
      savePrefs();
      setupLocalRefresh();
      toast(v === 0 ? '本地数据改为手动刷新' : `本地记录每 ${v} 秒刷新一次`);
    });

    // 覆盖天数：写进 points_config.json，后端按窗口重新聚合（额度周期不受影响）
    const cd = $('#optCoverDays');
    if (cd) cd.addEventListener('change', async () => {
      const v = +cd.value;
      prefs.coverDays = v;
      savePrefs();
      if (!bridge.has()) { toast('浏览器预览用的是演示数据，窗口设置不生效', 'i-info'); return; }
      const api = bridge.api();
      if (!api || typeof api.set_cover_days !== 'function') {
        toast('这个设置由主程序写入配置，重启程序后即可生效', 'i-info');
        return;
      }
      const r = await bridge.call('set_cover_days', v);
      if (r && r.data) {
        applyUsage(r.data, true);
        renderActive(true);
        toast(v === 0 ? '已恢复统计全部本机记录' : `只统计最近 ${v} 天的本机记录`);
      } else {
        toast('设置失败：' + ((r && r.error) || '未知错误'), 'i-info');
      }
    });

    $$('#rangeSeg button').forEach(b => {
      b.addEventListener('click', () => {
        state.range = b.dataset.range === 'month' ? 'month' : +b.dataset.range;
        prefs.trendRange = state.range;      // 记住上次选择，别每次重启都回到 30 天
        savePrefs();
        $$('#rangeSeg button').forEach(x => x.classList.toggle('is-active', x === b));
        const el = $('[data-chart="trend"]');
        if (el) el.dataset.anim = '0';
        renderTrend();                 // 先落终态（数据/刻度已经换好）
        replayChart(el);               // 再长一次，切换区间有"重新长出来"的反馈
      });
    });

    // 额度与消耗：今日 / 本周 / 本月，只重绘这张卡的环图与三项统计
    $$('#quotaSeg button').forEach(b => {
      b.addEventListener('click', () => {
        const v = b.dataset.quota;
        if (state.quotaRange === v) return;
        state.quotaRange = v;
        $$('#quotaSeg button').forEach(x => x.classList.toggle('is-active', x === b));
        const g = $('[data-chart="gauge"]');
        if (g) g.dataset.anim = '0';
        renderQuotaCard(effAccount(), hasQuota());
        replayChart(g);                // 仪表盘从零扫到新值
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
    const filterSel = $('#ledgerFilter');
    if (filterSel) {
      filterSel.addEventListener('change', () => {
        state.ledgerFilter = filterSel.value;
        renderLedger(true);
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
      toast(r && r.ok ? '账号数据已同步'
        : (r && r.busy ? '正在同步中，稍等一下'
          : '同步失败：' + ((r && r.error) || '登录态可能已过期')));
    });
    const ci = $('#cloudInterval');
    if (ci) ci.addEventListener('change', async () => {
      if (+ci.value === +(ACCOUNT.interval == null ? 60 : ACCOUNT.interval)) return;
      await bridge.call('account_set_interval', ci.value);
      await loadAccount();
      renderAccountCard();
      renderActive(true);   // 顶部提示条写着"云端每 X 分钟同步"，不重绘会一直停在旧值
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
      showLoading('正在同步云端账号…');
      toast('正在同步云端账号…');
      let rr = null;
      try { rr = await bridge.call('account_sync'); } finally { hideLoading(); }
      await loadAccount();
      renderAccountCard(); renderActive(true);
      // 不能再用 status 判断结果：软失败（断网 / 5xx）会保留 connected，
      // 拿状态判断就会把一次失败说成"已同步"。
      toast(rr && rr.ok ? '云端数据已同步'
        : (rr && rr.busy ? '正在同步中，稍等一下'
          : '同步失败：' + ((rr && rr.error) || '请检查登录状态')));
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

    let acctTimer = 0;
    let acctOpen = false;             // 收起动画期间 hidden 还没生效，用显式状态判断
    const setAccountPop = open => {
      if (!acctPop) return;
      acctOpen = open;
      clearTimeout(acctTimer);
      // 上一次的进出动画可能留了 fill:forwards，先清掉，否则再打开会是透明的
      try { acctPop.getAnimations().forEach(a => a.cancel()); } catch (e) { /* ignore */ }
      if (open) {
        renderAccountCard();          // 展开时用最新账号数据重绘
        positionAccountPop();
        acctPop.hidden = false;
        if (!motionOff()) {
          try {
            acctPop.animate(
              [{ opacity: 0, transform: 'translateY(-8px) scale(.985)' },
               { opacity: 1, transform: 'none' }],
              { duration: 190, easing: 'cubic-bezier(.22,1,.36,1)' });
          } catch (e) { /* ignore */ }
        }
      } else if (!motionOff()) {
        try {
          acctPop.animate(
            [{ opacity: 1, transform: 'none' },
             { opacity: 0, transform: 'translateY(-5px) scale(.99)' }],
            { duration: 130, easing: 'cubic-bezier(.4,0,.7,.3)', fill: 'forwards' });
        } catch (e) { /* ignore */ }
        acctTimer = setTimeout(() => { acctPop.hidden = true; }, 130);
      } else {
        acctPop.hidden = true;
      }
      if (avatarBtn) avatarBtn.setAttribute('aria-expanded', String(open));
    };

    if (avatarBtn) avatarBtn.addEventListener('click', e => {
      e.stopPropagation();
      setAccountPop(!acctOpen);
    });

    if (acctPop) acctPop.addEventListener('click', e => e.stopPropagation());

    document.addEventListener('click', () => { if (acctOpen) setAccountPop(false); });

    window.addEventListener('resize', () => { if (acctPop && !acctPop.hidden) positionAccountPop(); });
    const ph = $('#popHide'); if (ph) ph.addEventListener('click', () => bridge.hide());

    /* 四张 KPI 卡：点进去之后要落在对应的那一格上，不能只是换个页面 */
    const kpiTargets = [
      { view: 'account', spot: '#rowAvail', label: '可用积分' },
      { view: 'ledger', spot: '#ledgerSummary > div:nth-child(1)', label: '今日消耗' },
      { view: 'ledger', spot: '#ledgerSummary > div:nth-child(2)', label: '今日计费笔数' },
      { view: 'ledger', spot: '#ledgerSummary > div:nth-child(3)', label: '今日 Token' }
    ];
    $$('.kpi').forEach((card, i) => {
      const t = kpiTargets[i];
      if (!t) return;
      card.classList.add('is-link');
      card.title = `点击查看${t.label}明细`;
      card.addEventListener('click', () => {
        go(t.view);
        setTimeout(() => spotlight(t.spot), 280);   // 等页面切过去再定位
      });
    });

    /* 图表卡点击见 openDetail()：走浮层详情，不再原地放大 —— 放大后会打乱整个栅格 */

    /* 账户页「今日状态」：三个「立即检查」+ 全部检查，都走同一条只读探测 */
    ['stCkBtn', 'stTvBtn', 'stClBtn', 'stAll'].forEach(id => {
      const b = $('#' + id);
      if (b) b.addEventListener('click', () => probeStatus(b));
    });
    ['stCkAct', 'stTvAct', 'stClAct'].forEach(id => {
      const b = $('#' + id);
      if (b) b.addEventListener('click', () => runAction(b.dataset.act, b));
    });
    /* 小窗快捷操作 */
    /* 小窗：状态 chip 本身就是操作入口（不再额外排一行按钮） */
    const popCkChip = $('#popCkChip');
    if (popCkChip) popCkChip.addEventListener('click', () => {
      if (popCkChip.dataset.act) runAction(popCkChip.dataset.act, popCkChip);
    });
    const popTvChip = $('#popTravelChip');
    if (popTvChip) popTvChip.addEventListener('click', () => {
      if (popTvChip.dataset.act) runAction(popTvChip.dataset.act, popTvChip);
    });

    /* 用量分析：本机消耗周期 + 项目排行周期 */
    const lcSeg = $('#lcSeg');
    if (lcSeg) lcSeg.addEventListener('click', e => {
      const b = e.target.closest('button[data-lc]');
      if (!b) return;
      state.localRange = b.dataset.lc;
      renderLocalCard();
    });
    const projSeg = $('#projSeg');
    if (projSeg) projSeg.addEventListener('click', e => {
      const b = e.target.closest('button[data-proj]');
      if (!b) return;
      state.projRange = b.dataset.proj;
      if (state.view === 'analytics') renderAnalytics();
    });

    /* 点小窗外部收起：由 Python 端监听鼠标左键松开 + 光标是否落在小窗内，
       不依赖焦点（托盘弹出时焦点常常不在小窗上，靠 blur 判断会误收）。 */
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

    /* 自绘标题栏拖动：
       优先交给 Python 的原生循环（SetCapture + GetCursorPos 1:1 跟随）。
       前端算位移有两个致命问题：光标移出窗口后 mousemove 断流 → 窗口停在原地；
       光标再回来时按累计位移补一次 → 窗口瞬移，甚至直接飞出屏幕。
       下面的 queueMove 仅作旧后端的兜底。 */
    let drag = null, dragPending = null, dragTimer = 0, dragLast = 0;
    // 最大化状态下按住标题栏：先只记起点，等指针真的移动了才还原。
    // 原来在 mousedown 里直接调 win_max，于是双击最大化后随便单击一下标题栏，
    // 窗口就缩回去了 —— 这是"最大化后单击变小"的真正原因。
    let restoreAt = null;
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
    const beginDrag = async e => {
      document.body.classList.add('is-dragging');
      if (bridge.can('win_drag_start')) {
        bridge.call('win_drag_start');      // 结束时会回调 App.__dragEnd()
        return;
      }
      const pos = await bridge.call('win_pos');
      if (!pos || pos.x == null) { document.body.classList.remove('is-dragging'); return; }
      drag = { sx: e.screenX, sy: e.screenY, wx: pos.x, wy: pos.y, dpr: window.devicePixelRatio || 1 };
    };
    const startDrag = async e => {
      if (!topbar || e.button !== 0 || !bridge.has()) return;
      if (e.target.closest('button, input, select, textarea, a, .win-controls, .dock')) return;
      e.preventDefault();                   // 别触发文本选中，也别让浏览器自己开始拖拽
      if (winMaxed) {
        restoreAt = { x: e.screenX, y: e.screenY };
        return;
      }
      beginDrag(e);
    };
    if (topbar) topbar.addEventListener('mousedown', startDrag);
    // 系统拖动时后面不会回调 __dragEnd，靠 mouseup 兜一次，别把拖动光标留在地上
    window.addEventListener('mouseup', () => {
      restoreAt = null;
      if (document.body.classList.contains('is-dragging')) {
        setTimeout(() => { 
          document.body.classList.remove('is-dragging');
          readWinState();
        }, 80);
      }
    });
    window.addEventListener('mousemove', e => {
      if (restoreAt) {
        // 4px 死区：双击取消选中、轻微抖动都不该把最大化窗口还原
        if (Math.abs(e.screenX - restoreAt.x) < 4 && Math.abs(e.screenY - restoreAt.y) < 4) return;
        restoreAt = null;
        bridge.call('win_max').then(st => {
          applyWinState(st);
          beginDrag(e);
        });
        return;
      }
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

    /* 点排行里的某一行 → 跳积分流水并筛好这个项目 / 模型，省得再去下拉里找 */
    document.addEventListener('click', e => {
      if (!e.target || !e.target.closest) return;
      const proj = e.target.closest('#projectRank .rows-row');
      const mdl = proj ? null : e.target.closest('#modelList .rows-row');
      const row = proj || mdl;
      if (!row || !row.dataset.name) return;
      state.ledgerFilter = (proj ? 'project:' : 'model:') + row.dataset.name;
      state.ledgerLimit = 24;
      go('ledger');
    });

    /* ---------------- 排行行悬停提示（模型 / 项目共用） ---------------- */

    /** 排行行悬停内容：消耗 / 占比 / 调用，模型再加 Token 与"每分产出"。 */
    const rowTip = row => {
      const rows = [
        `<div class="tt-row"><span class="tt-name">消耗</span><span class="tt-val">${row.dataset.points} 分</span></div>`,
        `<div class="tt-row"><span class="tt-name">占比</span><span class="tt-val">${row.dataset.pct}%</span></div>`,
        `<div class="tt-row"><span class="tt-name">调用</span><span class="tt-val">${row.dataset.calls || '—'}</span></div>`
      ];
      const tk = row.dataset.tokens ? Number(row.dataset.tokens) : 0;
      if (tk > 0) {
        const pts = Number(String(row.dataset.points).replace(/,/g, '')) || 0;
        rows.push(`<div class="tt-row"><span class="tt-name">Token</span><span class="tt-val">${F.fmtWan(tk)}</span></div>`);
        if (pts > 0) {
          rows.push(`<div class="tt-row"><span class="tt-name">每分产出</span><span class="tt-val">${F.fmtWan(Math.round(tk / pts))} Token</span></div>`);
        }
      }
      return `<div class="tt-title">${row.dataset.name}</div>${rows.join('')}`;
    };

    document.addEventListener('mouseover', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (!row || !row.dataset.name) return;
      C.showTip(rowTip(row), e.clientX, e.clientY);
    });
    document.addEventListener('mousemove', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (!row || !row.dataset.name) return;
      C.showTip(rowTip(row), e.clientX, e.clientY);
    });
    document.addEventListener('mouseout', e => {
      const row = e.target && e.target.closest ? e.target.closest('.rows-row') : null;
      if (row) C.hideTip();
    });

    const noticeLink = $('#noticeSource');
    if (noticeLink) noticeLink.addEventListener('click', () => {
      // 同一颗按钮两种用途：没连账号时是"扫码连接"，连上了才是"数据源"
      if (noticeLink.dataset.act === 'login') {
        if (!bridge.has()) { toast('浏览器预览无法连接账号', 'i-info'); return; }
        try { bridge.call('account_login'); } catch (e) { /* 登录窗打不开时不影响页面 */ }
        return;
      }
      go('account');     // 数据目录/重新检测都在账户页，别再跳到设置
    });

    const kgConnect = $('#kgConnect');
    if (kgConnect) kgConnect.addEventListener('click', () => {
      if (!bridge.has()) { toast('浏览器预览无法连接账号', 'i-info'); return; }
      try { bridge.call('account_login'); } catch (e) { toast('打开登录窗口失败', 'i-alert'); }
    });

    // 提示条右上角的 ×：点一次就永久收起（偏好走 localStorage，重启仍生效）。
    // 不是"关掉就再也找不回来"：设置 → 图表与交互 → 顶部数据提示条 还能重新打开。
    const noticeClose = $('#noticeClose');
    if (noticeClose) noticeClose.addEventListener('click', () => {
      prefs.notice = false;
      savePrefs();
      renderActive(true);
      pushPrefs();
      toast('已隐藏顶部提示，可在「设置 → 图表与交互」重新打开', 'i-check');
    });

    // 签到结果条：「我知道了」按天记，当天不再出现，次日自动恢复。
    // 天天重启程序也天天弹一遍同样的"今天已签过"，只会被无视。
    const ckOk = $('#checkinOk');
    if (ckOk) ckOk.addEventListener('click', () => {
      prefs.checkinDismissed = todayKey();
      savePrefs();
      renderCheckin();
    });

    const ckRetry = $('#checkinRetry');
    if (ckRetry) ckRetry.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法签到', 'i-info'); return; }
      if (ckRetry.dataset.act === 'login') {
        try { bridge.call('account_login'); } catch (e) { toast('打开登录窗口失败', 'i-alert'); }
        return;
      }
      ckRetry.disabled = true;
      CHECKIN = Object.assign({}, CHECKIN, { status: 'running' });
      renderCheckin();
      try {
        const r = await bridge.call('checkin_run');
        if (r) CHECKIN = r;
      } finally {
        ckRetry.disabled = false;
        renderCheckin();
      }
    });

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

    /* 系统明暗切换时实时跟随：主题设为「跟随系统」才有反应，
       之前只在启动那一刻取一次，系统切了要重启程序才变 */
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const onSysTheme = () => { if (prefs.theme === 'system') applyTheme('system', false); };
      if (mq.addEventListener) mq.addEventListener('change', onSysTheme);
      else if (mq.addListener) mq.addListener(onSysTheme);
    }

    // 左下角四张卡：只在侧栏模式下存在（胶囊模式下整条侧栏是隐藏的，不用白跑定时器）
    const sideBox = $('#sideSwipe');
    sideSwipe = sideBox && document.body.dataset.nav !== 'b'
      ? createSwipe(sideBox, { auto: true, interval: Math.max(0, prefs.sideSwipeSec) * 1000 || 0 })
      : null;
    // 小窗也自动播，节奏比左侧慢一档；每次打开都回到第 1 张
    popSwipe = createSwipe($('#popSwipe'), {
      auto: true, interval: Math.max(0, prefs.popSwipeSec) * 1000 || 0
    });

    const popCopy = $('#popCopy');
    if (popCopy) popCopy.addEventListener('click', async () => {
      if (!bridge.has() || !bridge.can('copy_text')) {
        toast('浏览器预览无法写剪贴板', 'i-info');
        return;
      }
      try {
        const r = await bridge.call('copy_text', buildDigestText());
        toast(r && r.ok ? '摘要已复制' : '复制失败', r && r.ok ? 'i-check' : 'i-alert');
      } catch (e) {
        toast('复制失败', 'i-alert');
      }
    });

    const pcl = $('#popCloud');
    if (pcl) pcl.addEventListener('click', async () => {
      if (!bridge.has()) { toast('浏览器预览无法同步云端', 'i-info'); return; }
      showLoading('正在同步云端账号…');
      toast('正在同步云端账号…');
      let rr = null;
      try { rr = await bridge.call('account_sync'); } finally { hideLoading(); }
      await loadAccount();
      renderPopup();
      renderAccountCard();
      toast(rr && rr.ok ? '云端数据已同步'
        : (rr && rr.busy ? '正在同步中，稍等一下'
          : '同步失败：' + ((rr && rr.error) || '请检查登录状态')));
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        if (acctOpen) setAccountPop(false);
        if (state.mode === 'popup') bridge.hide();
      }
      // Ctrl+R = 重载整个界面：改了样式/脚本后能立刻看到。
      // 只刷数据的话，CSS 改动永远不生效，看起来就像"没改"。
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'r') {
        e.preventDefault();
        location.reload();
      }
      if (e.altKey && /^[1-5]$/.test(e.key) && state.mode === 'main') {
        go(['overview', 'analytics', 'ledger', 'account', 'settings'][+e.key - 1]);
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) renderActive();
    });

    let rt;
    window.addEventListener('resize', () => {
      clearTimeout(rt);
      rt = setTimeout(() => { moveIndicator(); moveTbPill(); redrawCharts(); }, 180);
    });

    // 滚动到视野里再长：首屏看不见的图表（趋势、构成、日历）滚到跟前才播动画
    const viewsEl = $('#views');
    if (viewsEl) {
      let st = 0;
      viewsEl.addEventListener('scroll', () => {
        clearTimeout(st);
        st = setTimeout(() => revealCharts(false), 140);
      }, { passive: true });
    }

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
    // 首屏必须强制渲染：之前这里走的是节流版 renderActive()，
    // 若 paintTheme 的 60ms 定时渲染刚好在前，这一发就会被 250ms 节流吞掉 ——
    // 用户看到的是"数据还没到"那一版（额度被算成超支），要等下次交互才恢复。
    if (ready) { renderActive(true); return; }
    ready = true;
    if (state.mode === 'popup') {
      renderPopup();
    } else {
      if (state.view !== 'overview') {
        $$('.nav-item, .tb-nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === state.view));
        $$('.view').forEach(v => v.classList.toggle('is-active', v.dataset.view === state.view));
        const [t, s] = TITLES[state.view] || ['', ''];
        setText('#pageTitle', t);
        setText('#pageSub', s);
      }
      renderActive(true);
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

  /* ---------------- 启动遮罩 ---------------- */

  // 加载期间轮播的文案：让等待的那一两秒有点人味，也能说明"在读什么"
  const BOOT_TEXTS = [
    '正在构建今日用量视图…',
    '正在把上千条调用记录排好队…',
    '正在核对云端余额…',
    '正在给柱状图浇水…',
    '正在数你今天花掉的积分…',
    '正在整理消耗日历…',
    '马上就好…'
  ];
  let bootTextTimer = 0;

  function startBootText() {
    const el = $('#bootText');
    if (!el) return;
    let i = Math.floor(Math.random() * BOOT_TEXTS.length);   // 每次启动从不同句子开始
    const tick = () => {
      el.textContent = BOOT_TEXTS[i % BOOT_TEXTS.length];
      el.classList.remove('is-in');
      void el.offsetWidth;
      el.classList.add('is-in');
      i += 1;
    };
    tick();
    clearInterval(bootTextTimer);
    bootTextTimer = setInterval(tick, 1200);
  }

  /** 数据就绪：遮罩淡出（不是硬切），再解除 loading 状态 */
  function finishBoot() {
    clearInterval(bootTextTimer);
    const mask = $('#bootMask');
    if (mask) mask.classList.add('is-out');
    setTimeout(() => document.body.classList.remove('is-loading'), 240);
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

    startBootText();
    bind();
    $$('#rangeSeg button').forEach(b => b.classList.toggle('is-active', String(b.dataset.range) === String(state.range)));
    setText('#syncTime', D.meta.updated || '—');

    if (HASH_MODE || bridge.has()) {
      document.body.classList.add('is-loading');
      // 启动阶段只用启动遮罩，不再叠一层 loading-veil（两个转圈会打架）
      await whenBridgeReady();
      await loadUsage();
      await loadAccount();
      await loadVersion();
      await loadCheckin();
      pushCheckinOptions();   // 让 Python 侧配置与界面开关对齐，避免"界面关了它还在签"
      loadAutostart();
      // 任务进度不挡首屏：它要打两个网络请求，还可能排在账号同步后面等锁，
      // 放在 boot 里 await 会让启动遮罩多停好几秒。
      setTimeout(() => { loadTasks().then(() => renderSideSwipe()); }, 400);
    }

    // 只有浏览器预览（没有桌面桥）才生成演示数据：
    // 桌面态宁可为空，也不先摆一份假数字上去。
    if (!bridge.has() && window.buildDemoData) {
      D = normalize(window.buildDemoData());
      window.APP_DATA = D;
      source = 'demo';
    }

    bootRender();
    finishBoot();

    setTimeout(() => { if (state.mode === 'popup') renderPopup(); }, 260);
    setupLocalRefresh();
    startEtaTicker();

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
    /** 由 Python 在窗口显示后调用：补一次入场过渡 */
    playEnter: mode => playEnter(mode),
    /** 由 Python 在"收成小窗"前调用：内容退场 */
    playExit: () => playExit(),
    /** 连击反馈 */
    burstShake: () => burstShake(),
    /** 由 Python 在窗口显示前调用：对齐偏好并全量重算 */
    beforeShow: () => beforeShow(),
    /** 由 Python 在原生拖动结束时调用 */
    __dragEnd: () => {
      document.body.classList.remove('is-dragging');
      setTimeout(readWinState, 60);
    },
    /** 另一窗口推来的偏好 */
    applyPrefs: raw => applyPrefs(raw),
    /** 当前实际生效的主题（给托盘自绘菜单取色用） */
    getTheme: () => resolved(),
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
    /** 由 Python 在后台签到结束后调用：把结论推到结果条上 */
    /** 供 Python 侧（托盘菜单）切到指定页 */
    go: v => { try { go(v); } catch (e) { /* ignore */ } },
    /** 供 Python 侧（托盘菜单等）弹一条提示 */
    toast: (msg, icon) => { try { toast(msg, icon); } catch (e) { /* ignore */ } },
    reloadCheckin: async () => {
      const ok = await loadCheckin();
      await loadTasks();
      if (state.view === 'overview') renderActive(true);
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
