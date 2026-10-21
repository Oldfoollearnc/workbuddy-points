/* =========================================================
   数据层（演示数据）
   - 所有维度由同一份总量派生，保证「柱状 / 环形 / 流水 / 排行」互相对得上
   - 接入真实接口时，只需替换 build() 的返回结构
   ========================================================= */
(function () {
  'use strict';

  const NOW = new Date();
  const NOW_HOUR = NOW.getHours();
  const NOW_MIN = NOW.getMinutes();
  const FULL_DAY_TOTAL = 8642;      // 满日参考用量
  const YESTERDAY_FULL = 7690;      // 昨日全天用量
  const TOTAL_QUOTA = 200000;
  const USED_QUOTA = 137480;

  /* ---------- 基础工具 ---------- */

  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  const rnd = mulberry32(20261021);

  /** 最大余数法：把 total 按 shares 拆成整数，且严格求和等于 total */
  function allocate(total, shares) {
    const sum = shares.reduce((a, b) => a + b, 0) || 1;
    const raw = shares.map(s => (s / sum) * total);
    const out = raw.map(v => Math.floor(v));
    let rest = total - out.reduce((a, b) => a + b, 0);
    const order = raw
      .map((v, i) => ({ i, frac: v - Math.floor(v) }))
      .sort((a, b) => b.frac - a.frac);
    let k = 0;
    while (rest > 0 && order.length) {
      const idx = order[k % order.length].i;
      if (out[idx] >= 0) { out[idx] += 1; rest -= 1; }
      k += 1;
    }
    return out;
  }

  function fmt(n, d) {
    const v = Number(n) || 0;
    return v.toLocaleString('zh-CN', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function fmtWan(n) {
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return fmt(n);
  }
  const pad = n => String(n).padStart(2, '0');
  const dateKey = d => `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const clock = d => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  /* ---------- 维度定义 ---------- */

  const MODEL_DEFS = [
    { key: 'hunyuan',  name: '混元系列',      share: .30, rshare: .28, tshare: .31, tone: 'c1' },
    { key: 'claude',   name: 'Claude 系列',   share: .24, rshare: .24, tshare: .26, tone: 'c2' },
    { key: 'deepseek', name: 'DeepSeek 系列', share: .20, rshare: .21, tshare: .17, tone: 'c3' },
    { key: 'gpt',      name: 'GPT 系列',      share: .16, rshare: .16, tshare: .15, tone: 'c4' },
    { key: 'gemini',   name: 'Gemini 系列',   share: .10, rshare: .11, tshare: .11, tone: 'c5' }
  ];

  const TASK_DEFS = [
    { name: '代码开发与重构', share: .27, tone: 'c1' },
    { name: '文档与 PPT 生成', share: .21, tone: 'c2' },
    { name: '数据分析与图表', share: .19, tone: 'c3' },
    { name: '网页与应用搭建', share: .16, tone: 'c4' },
    { name: '调研与信息检索', share: .11, tone: 'c5' },
    { name: '其他', share: .06, tone: 'c6' }
  ];

  const PROJECT_DEFS = [
    { name: '积分观测台', share: .24 },
    { name: '季度经营分析', share: .19 },
    { name: '官网改版', share: .15 },
    { name: '论文排版', share: .11 },
    { name: '数据清洗脚本', share: .09 },
    { name: '客户周报', share: .06 },
    { name: '其他项目', share: .16 }
  ];

  // 24 小时用量权重（真实使用曲线：上午与傍晚双峰）
  const HOUR_W = [.35, .22, .16, .11, .09, .14, .34, .62, 1.25, 2.55, 3.65, 3.25,
                  2.15, 2.7, 3.35, 3.85, 4.15, 3.55, 2.6, 2.0, 1.7, 1.3, .9, .6];

  function weightedPick(defs, shareKey) {
    const total = defs.reduce((s, d) => s + (d[shareKey] || 0), 0);
    let r = rnd() * total;
    for (const d of defs) { r -= (d[shareKey] || 0); if (r <= 0) return d; }
    return defs[defs.length - 1];
  }

  /* ---------- 今日分时 ---------- */

  const activeHours = [];
  for (let h = 0; h <= NOW_HOUR; h++) activeHours.push(h);

  const runWeight = activeHours.reduce((s, h) => s + HOUR_W[h], 0);
  const fullWeight = HOUR_W.reduce((a, b) => a + b, 0);
  const todayConsumed = Math.round(FULL_DAY_TOTAL * (runWeight / fullWeight));
  const hourPoints = allocate(todayConsumed, activeHours.map(h => HOUR_W[h]));
  const todayRequests = Math.max(1, Math.round(todayConsumed / 22.4));
  const hourRequests = allocate(todayRequests, activeHours.map(h => HOUR_W[h]));
  const todayTokens = Math.round(todayConsumed * 558);
  const hourTokens = allocate(todayTokens, activeHours.map(h => HOUR_W[h]));

  const hourly = [];
  for (let h = 0; h < 24; h++) {
    const idx = activeHours.indexOf(h);
    hourly.push({
      hour: h,
      label: `${pad(h)}:00`,
      points: idx >= 0 ? hourPoints[idx] : 0,
      requests: idx >= 0 ? hourRequests[idx] : 0,
      tokens: idx >= 0 ? hourTokens[idx] : 0,
      isFuture: h > NOW_HOUR,
      isNow: h === NOW_HOUR
    });
  }

  const peak = hourly.reduce((a, b) => (b.points > a.points ? b : a), hourly[0]);

  /* ---------- 模型 / 任务 / 项目 ---------- */

  const modelPoints = allocate(todayConsumed, MODEL_DEFS.map(m => m.share));
  const modelRequests = allocate(todayRequests, MODEL_DEFS.map(m => m.rshare));
  const modelTokens = allocate(todayTokens, MODEL_DEFS.map(m => m.tshare));

  const models = MODEL_DEFS.map((m, i) => ({
    key: m.key,
    name: m.name,
    tone: m.tone,
    points: modelPoints[i],
    requests: modelRequests[i],
    tokens: modelTokens[i],
    share: modelPoints[i] / (todayConsumed || 1),
    avg: modelRequests[i] ? modelPoints[i] / modelRequests[i] : 0
  })).sort((a, b) => b.points - a.points);

  const taskPoints = allocate(todayConsumed, TASK_DEFS.map(t => t.share));
  const tasks = TASK_DEFS.map((t, i) => ({
    name: t.name, tone: t.tone, points: taskPoints[i],
    share: taskPoints[i] / (todayConsumed || 1)
  })).sort((a, b) => b.points - a.points);

  const projectPoints = allocate(todayConsumed, PROJECT_DEFS.map(p => p.share));
  const projects = PROJECT_DEFS.map((p, i) => ({
    name: p.name, points: projectPoints[i],
    requests: Math.max(1, Math.round(modelRequests[0] * (projectPoints[i] / (todayConsumed || 1)) * 5.2)),
    share: projectPoints[i] / (todayConsumed || 1)
  })).sort((a, b) => b.points - a.points);

  /* ---------- 近 30 天 ---------- */

  const WEEKDAY_FACTOR = [.66, 1.05, 1.0, 1.0, 1.02, 1.0, .72]; // 日 一 二 三 四 五 六
  const daily = [];
  for (let i = 29; i >= 0; i--) {
    const d = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - i);
    const ramp = i <= 9 ? 1 + 0.10 * (9 - i) : 1;
    let consumed;
    if (i === 0) consumed = todayConsumed;
    else if (i === 1) consumed = YESTERDAY_FULL;
    else consumed = Math.round(4250 * ramp * WEEKDAY_FACTOR[d.getDay()] * (0.93 + rnd() * 0.14));
    const earned = i === 0 ? 1200 : Math.round((900 + rnd() * 500) / 50) * 50;
    daily.push({
      date: dateKey(d),
      dateObj: d,
      weekday: '日一二三四五六'[d.getDay()],
      consumed,
      earned,
      balance: 0
    });
  }

  const available = TOTAL_QUOTA - USED_QUOTA;
  let running = available;
  for (let i = daily.length - 1; i >= 0; i--) {
    daily[i].balance = running;
    running += daily[i].consumed - daily[i].earned;
  }
  // 防御：早期余额不应超过总额度
  const overshoot = daily[0].balance - TOTAL_QUOTA;
  if (overshoot > 0) {
    const oldDays = daily.slice(0, 12);
    const cut = allocate(Math.round(overshoot), oldDays.map(d => d.consumed));
    oldDays.forEach((d, i) => { d.consumed = Math.max(600, d.consumed - cut[i]); });
    running = available;
    for (let i = daily.length - 1; i >= 0; i--) { daily[i].balance = running; running += daily[i].consumed - daily[i].earned; }
  }

  const fullDays = daily.slice(22, 29);                       // 不含今日的最近 7 个完整日
  const avg7 = Math.round(fullDays.reduce((s, d) => s + d.consumed, 0) / fullDays.length);
  const daysLeft = available / (avg7 || 1);
  const runOutDate = new Date(NOW.getTime() + daysLeft * 86400000);
  const prevFull = daily[28].earned;

  /* ---------- 近 7 天 × 模型（堆叠） ---------- */

  const taskDefs = tasks.map(t => ({ key: t.name, name: t.name, tone: t.tone }));
  const week = daily.slice(-7).map((d, di) => {
    const jitter = TASK_DEFS.map(m => m.share * (0.82 + rnd() * 0.36));
    const pts = allocate(d.consumed, jitter);
    return {
      date: d.date,
      weekday: d.weekday,
      isToday: di === 6,
      total: d.consumed,
      values: pts
    };
  });

  /* ---------- 热力矩阵 ---------- */

  const heat = tasks.slice(0, 6).map(t => ({
    name: t.name,
    tone: t.tone,
    values: hourly.map(h => Math.round(h.points * t.share * 100) / 100)
  }));
  const heatMax = Math.max(1, ...heat.flatMap(r => r.values));

  /* ---------- 流水 ---------- */

  const ledger = [];
  let seq = 0;

  function makeEntry(when, type, points, extra) {
    seq += 1;
    return Object.assign({
      id: 'e' + seq,
      time: when,
      ts: when.getTime(),
      type,
      points,
      model: '',
      tone: 'c1',
      task: '',
      project: '',
      requests: 0,
      tokens: 0
    }, extra || {});
  }

  // 今日：按小时拆分，保证合计等于今日消耗
  activeHours.forEach((h, i) => {
    let pts = hourPoints[i];
    if (!pts) return;
    const chunks = pts > 700 ? 3 : pts > 260 ? 2 : 1;
    const parts = allocate(pts, Array.from({ length: chunks }, () => 0.6 + rnd() * 0.8));
    parts.forEach(p => {
      if (!p) return;
      const m = weightedPick(MODEL_DEFS, 'share');
      const t = weightedPick(TASK_DEFS, 'share');
      const pj = weightedPick(PROJECT_DEFS, 'share');
      const when = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), h, Math.floor(rnd() * (h === NOW_HOUR ? Math.max(2, NOW_MIN) : 58)));
      ledger.push(makeEntry(when, 'consume', p, {
        model: m.name, tone: m.tone, task: t.name, project: pj.name,
        requests: Math.max(1, Math.round(p / 22)),
        tokens: Math.round(p * (480 + rnd() * 160))
      }));
    });
  });
  // 今日获得
  ledger.push(makeEntry(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), 0, 6), 'earn', 200, {
    model: '积分中心', tone: 'c5', task: '每日签到', project: '账户奖励'
  }));
  ledger.push(makeEntry(new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), 10, 12), 'earn', 1000, {
    model: '积分中心', tone: 'c4', task: '活动奖励', project: '账户奖励'
  }));

  // 前两日：按全天曲线拆分
  for (let back = 1; back <= 2; back++) {
    const d = daily[daily.length - 1 - back];
    const base = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate() - back);
    const chunks = 9;
    const parts = allocate(d.consumed, Array.from({ length: chunks }, () => 0.55 + rnd() * 0.9));
    parts.forEach((p, i) => {
      const h = Math.min(23, 7 + Math.round((i / chunks) * 12 + rnd() * 2));
      const m = weightedPick(MODEL_DEFS, 'share');
      const t = weightedPick(TASK_DEFS, 'share');
      const pj = weightedPick(PROJECT_DEFS, 'share');
      ledger.push(makeEntry(new Date(base.getFullYear(), base.getMonth(), base.getDate(), h, Math.floor(rnd() * 58)), 'consume', p, {
        model: m.name, tone: m.tone, task: t.name, project: pj.name,
        requests: Math.max(1, Math.round(p / 22)),
        tokens: Math.round(p * (480 + rnd() * 160))
      }));
    });
    ledger.push(makeEntry(new Date(base.getFullYear(), base.getMonth(), base.getDate(), 0, 8), 'earn', 200, {
      model: '积分中心', tone: 'c5', task: '每日签到', project: '账户奖励'
    }));
  }

  ledger.sort((a, b) => b.ts - a.ts);

  const ledgerToday = ledger.filter(e => e.type === 'consume' && dateKey(e.time) === dateKey(NOW));
  const todayUsedSum = ledgerToday.reduce((s, e) => s + e.points, 0);
  const todayEarnSum = ledger.filter(e => e.type === 'earn' && dateKey(e.time) === dateKey(NOW)).reduce((s, e) => s + e.points, 0);

  /* ---------- 指标 ---------- */

  const deltaPct = (todayConsumed / (YESTERDAY_FULL * (runWeight / fullWeight)) - 1) * 100;
  const usedPct = USED_QUOTA / TOTAL_QUOTA;
  const cycleEnd = new Date(NOW.getFullYear(), NOW.getMonth() + 1, 1);

  const topModel = models[0];
  const topTask = tasks[0];
  const topProject = projects[0];

  const insights = [
    {
      icon: 'i-bolt',
      title: '时段峰值集中在傍晚',
      text: `<b>${pad(peak.hour)}:00–${pad(peak.hour + 1)}:00</b> 消耗 <b>${fmt(peak.points)} 分</b>，占今日 <b>${(peak.points / (todayConsumed || 1) * 100).toFixed(1)}%</b>，为全天最高时段。`
    },
    {
      icon: 'i-chart',
      title: '模型消耗结构',
      text: `<b>${topModel.name}</b> 占今日 <b>${(topModel.share * 100).toFixed(1)}%</b>（${fmt(topModel.points)} 分），单次平均 <b>${topModel.avg.toFixed(1)} 分</b>。`
    },
    {
      icon: 'i-clock',
      title: '额度可用性',
      text: `按近 7 日均值 <b>${fmt(avg7)} 分/天</b> 估算，可用额度约支撑 <b>${daysLeft.toFixed(1)} 天</b>，预计 <b>${dateKey(runOutDate)}</b> 前后耗尽。`
    }
  ];

  window.APP_DATA = {
    meta: {
      now: NOW,
      updated: `${clock(NOW)}`,
      updatedFull: `${dateKey(NOW)} ${clock(NOW)}`,
      nowHour: NOW_HOUR,
      snapshotAt: `${pad(NOW_HOUR)}:${pad(NOW_MIN)}`,
      source: 'demo',
    },
    account: {
      name: '老大',
      plan: '专业版 · 团队席位',
      id: 'WB-8842-0931',
      total: TOTAL_QUOTA,
      used: USED_QUOTA,
      available,
      usedPct,
      configured: true,
      cycleEnd
    },
    today: {
      consumed: todayConsumed,
      earned: todayEarnSum,
      requests: todayRequests,
      tokens: todayTokens,
      avgPerRequest: todayConsumed / (todayRequests || 1),
      deltaPct,
      yarn: prevFull
    },
    hourly,
    peakHour: peak.hour,
    peakPoints: peak.points,
    modelDefs: MODEL_DEFS.map(m => ({ key: m.key, name: m.name, tone: m.tone, share: m.share })),
    taskDefs,
    projects7: projects,
    models,
    tasks,
    projects,
    daily,
    week,
    heat,
    heatMax,
    ledger,
    ledgerTodayTotal: todayUsedSum,
    avg7,
    daysLeft,
    runOutDate,
    pctOfQuota: todayConsumed / TOTAL_QUOTA,
    insights
  };

  window.APP_FMT = { fmt, fmtWan, pad, dateKey, clock };
})();
