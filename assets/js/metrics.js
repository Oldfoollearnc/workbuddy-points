/* =========================================================
   数据口径层（纯计算，不碰 DOM）
   ---------------------------------------------------------
   为什么单独抽出来：
   1) allowance / todayStat / quotaWindow / dailySeries 这些是"数字怎么算"的全部逻辑，
      放在 app.js 里既没法单测，也容易被渲染代码顺手改坏；
   2) 它们只依赖三样东西：本机数据 D、云端快照 ACCOUNT、偏好 prefs。
      用 configure() 注入取值函数，浏览器和 node 里都能跑（tests/metrics.test.js）。
   ========================================================= */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Metrics = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  let ENV = {
    data: () => ({}),
    account: () => ({}),
    prefs: () => ({})
  };

  function configure(env) {
    ENV = Object.assign({}, ENV, env || {});
    return api;
  }

  function ctx() {
    return {
      D: ENV.data() || {},
      ACCOUNT: ENV.account() || {},
      prefs: ENV.prefs() || {}
    };
  }

  const pad = n => String(n).padStart(2, '0');
  const round2 = v => Math.round((Number(v) || 0) * 100) / 100;

  /* ---------------- 日期 ---------------- */

  function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
  function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
  function diffDays(a, b) { return Math.round((startOfDay(b) - startOfDay(a)) / 86400000); }
  const dayKeyOf = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  /**
   * 把 "MM-DD"（本机口径的 daily.date）还原成日期对象。
   * 必须处理跨年：12 月的记录在 1 月查看，直接拼当前年就会错到 11 个月后。
   * 规则：不晚于今天，晚了就归到上一年。
   */
  function monthDayToDate(md, now) {
    const m = /^(\d{1,2})-(\d{1,2})$/.exec(String(md || ''));
    if (!m) return null;
    const base = now || new Date();
    const d = new Date(base.getFullYear(), +m[1] - 1, +m[2]);
    if (startOfDay(d) > startOfDay(base)) d.setFullYear(d.getFullYear() - 1);
    return d;
  }

  function parseDate(v) {
    if (!v) return null;
    const s = String(v).trim();
    let m = s.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
    // 云端返回的有效期是 "11-19" 这种不带年份的格式（_date() 只取 MM-DD）。
    // 只认 yyyy-mm-dd 的话会一直解析失败、回退成"今天 +30 天"，折算天数偏大。
    m = s.match(/^(\d{1,2})-(\d{1,2})$/);
    if (m) {
      const now = new Date();
      let d = new Date(now.getFullYear(), +m[1] - 1, +m[2]);
      if (d < startOfDay(now)) d = new Date(now.getFullYear() + 1, +m[1] - 1, +m[2]);
      return d;
    }
    return null;
  }

  /* ---------------- 折算 ---------------- */

  /**
   * 「工作日折算」开 = 只算工作日（剔除周末与法定节假日）；
   * 关 = 按自然日算（每天都可花），这样开关才有可见差别。
   */
  function isWorkday(d, useHoliday) {
    if (!useHoliday) return true;
    const w = d.getDay();
    if (w === 0 || w === 6) return false;
    if (typeof HOLIDAYS !== 'undefined' && HOLIDAYS && HOLIDAYS.has && HOLIDAYS.has(d)) return false;
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
    const { prefs } = ctx();
    acc = acc || {};
    const total = acc.total || 0;
    const available = acc.available || 0;
    const end = parseDate(acc.expireAt) || addDays(new Date(), 30);
    const today = startOfDay(new Date());
    const leftDays = Math.max(1, diffDays(today, end) + 1);
    const holidayOn = prefs.holiday !== false;
    const spendDays = Math.max(1, countWorkdays(today, end, holidayOn));
    const signinOn = prefs.signin !== false;
    // 签到每天都有（含周末），所以按自然日累加
    const signin = signinOn ? Math.max(0, 100 * leftDays) : 0;
    const pool = available + signin;
    const perDay = pool / spendDays;
    const weekSpan = holidayOn ? 5 : 7;
    const perWeek = perDay * Math.min(weekSpan, spendDays);
    return {
      perDay, perWeek, leftDays, spendDays, holidayOn,
      leftWorkdays: spendDays,          // 兼容旧字段名
      pool, signin, end, total, available
    };
  }

  const leftDaysText = al => (al.holidayOn ? `${al.spendDays} 个工作日` : `${al.spendDays} 天`);

  /* ---------------- 云端逐笔 / 本机逐笔 ---------------- */

  /** 可用的云端逐笔（积分流水）。没有它就退回本机记录。 */
  function cloudRows() {
    const { ACCOUNT } = ctx();
    const a = ACCOUNT || {};
    return (a.status === 'connected' && Array.isArray(a.usageRows) && a.usageRows.length) ? a.usageRows : null;
  }

  const rowTime = r => new Date(String((r && r.t) || '').replace(' ', 'T'));

  /* ---------------- 来源（客户端） ----------------
     同一个账号既跑 WorkBuddy 也跑 CodeBuddy，共用一份积分。
     云端逐笔带 client 字段时才能拆；字段缺失时下面这些函数一律返回 null，
     界面据此隐藏来源切换，绝不编造拆分数字。 */

  /** 已知标识 → 展示名。实测值（2026-10-30）：WorkBuddy / CodeBuddyIDE / app_cloud。 */
  const CLIENT_LABEL = {
    workbuddy: 'WorkBuddy',
    codebuddy: 'CodeBuddy',
    codebuddyide: 'CodeBuddy',
    // 服务端第三个来源：只在 10-19、10-21 出现过，模型是 hy4-preview / step-5-preview，
    // 看不出属于哪个产品，就按"App / 云端"单列一类，不并入 WB 也不并入 CB。
    app_cloud: 'App / 云端'
  };

  function clientName(id) {
    const k = String(id || '');
    if (!k) return '历史（未标注）';
    const hit = CLIENT_LABEL[k.toLowerCase()];
    if (hit) return hit;
    // 服务端换后缀（CodeBuddyIDE / CodeBuddyCN / CodeBuddy-Web）也归到同一个名字，
    // 否则同一客户端在界面上会分裂成两三行。
    if (/codebuddy/i.test(k)) return 'CodeBuddy';
    if (/workbuddy/i.test(k)) return 'WorkBuddy';
    return k;
  }

  /** 本机 JSONL 记录一定是 WorkBuddy 产生的 —— 判断某来源要不要并入 localTail。 */
  const isWorkBuddyClient = id => !id || /workbuddy/i.test(String(id));

  function clientMap() {
    const { ACCOUNT } = ctx();
    const a = ACCOUNT || {};
    return (a.status === 'connected' && a.cloudClients && typeof a.cloudClients === 'object')
      ? a.cloudClients : null;
  }

  /**
   * 可切换的来源列表（首项恒为「全部」）。
   * 只有真的能区分时才给数组：至少 2 个有消耗的桶，且至少一个带非空 client。
   * 字段整个缺失时云端只会有一个空 id 的桶 —— 那不算能拆。
   */
  function clientOptions() {
    const cc = clientMap();
    if (!cc) return null;
    // 空 id = 服务端还没带 client 的历史行。它不能作为可选项：
    // 「全部」的 id 也是空串，两者会撞成两个同时高亮的按钮。
    // 单独把它的金额挂到返回数组上，供界面在「全部」的提示里说明。
    const unlabeled = Number((cc[''] || {}).points) || 0;
    const items = Object.keys(cc)
      .filter(id => id)
      .map(id => ({
        id,
        name: clientName(id),
        points: Number((cc[id] || {}).points) || 0,
        calls: Number((cc[id] || {}).calls) || 0
      }))
      .filter(x => x.points > 0)
      .sort((a, b) => b.points - a.points);
    if (items.length < 2) return null;
    const out = [{ id: '', name: '全部',
                   points: round2(items.reduce((s, x) => s + x.points, 0) + unlabeled),
                   calls: items.reduce((s, x) => s + x.calls, 0) }].concat(items);
    out.unlabeled = round2(unlabeled);
    return out;
  }

  /** 某来源的按天合计（日历用）。id 为空表示全部，走原有合并口径。 */
  function clientDays(id) {
    const cc = clientMap();
    const rec = id && cc ? cc[id] : null;
    if (!rec || !Array.isArray(rec.days)) return null;
    return rec.days.map(d => ({
      date: d.date,
      points: Number(d.points) || 0,
      calls: d.calls == null ? null : d.calls
    }));
  }

  function clientModels(id) {
    const cc = clientMap();
    const rec = id && cc ? cc[id] : null;
    return (rec && Array.isArray(rec.models)) ? rec.models : null;
  }

  /** 今日各来源消耗：总览小字与构成明细用。不可区分时返回 null。零消耗的来源不列，免得占满一行。 */
  function todayByClient() {
    const opts = clientOptions();
    if (!opts) return null;
    return opts.filter(o => o.id).map(o => {
      const st = todayStat(o.id);
      return { id: o.id, name: o.name, points: st.consumed, calls: st.requests };
    }).filter(x => x.points > 0);
  }

  /** 上次云端同步的时刻（毫秒）；没同步过返回 0 */
  function lastSyncMs() {
    const { ACCOUNT } = ctx();
    const s = ACCOUNT && ACCOUNT.lastSync;
    if (!s) return 0;
    const t = new Date(String(s).replace(' ', 'T')).getTime();
    return isNaN(t) ? 0 : t;
  }

  /** 上次同步之后、本机新增的逐笔（云端还没有的那一截） */
  function localTail() {
    const { D } = ctx();
    const since = lastSyncMs();
    const src = D.ledger || [];
    if (!since) return src.slice();
    return src.filter(e => {
      const t = (e.time instanceof Date ? e.time : new Date(e.time)).getTime();
      return !isNaN(t) && t > since;
    });
  }

  /* ---------------- 今日口径 ---------------- */

  /**
   * 今日消耗/调用/时段/模型构成：一个数据源。
   * 连上云端 = 云端逐笔 + 同步后本机逐笔；未连接才退回本机记录。
   */
  function todayStat(clientId) {
    const { D } = ctx();
    const want = String(clientId || '');
    const rows = cloudRows();
    if (rows) {
      const now = new Date();
      const todayKey = dayKeyOf(now);
      const yestKey = dayKeyOf(addDays(now, -1));
      // 同比要卡到"此刻"：拿今天到现在的消耗去比昨天全天，早上必然显示 -90%，
      // 那个数字没有信息量，还会让人以为今天特别省。
      const nowMin = now.getHours() * 60 + now.getMinutes();
      const hours = Array.from({ length: 24 }, (_, h) => ({ hour: h, points: 0, requests: 0 }));
      const map = new Map();
      let sum = 0, calls = 0, yday = 0;
      const fold = (d, credit, model) => {
        const hb = hours[d.getHours()];
        hb.points += credit; hb.requests += 1;
        const name = model || '未知模型';
        const m = map.get(name) || { name, points: 0, calls: 0 };
        m.points += credit; m.calls += 1;
        map.set(name, m);
        sum += credit; calls += 1;
      };
      rows.forEach(r => {
        if (want && String(r.client || '') !== want) return;
        const d = rowTime(r);
        if (isNaN(d.getTime())) return;
        const credit = Number(r.credit) || 0;
        const key = dayKeyOf(d);
        if (key === yestKey) {
          if (d.getHours() * 60 + d.getMinutes() <= nowMin) yday += credit;
          return;
        }
        if (key !== todayKey) return;
        fold(d, credit, r.model);
      });
      // 本机 tail 只有 WorkBuddy 会产生：筛到别的来源时不能并进来
      if (isWorkBuddyClient(want)) localTail().forEach(e => {
        const d = e.time instanceof Date ? e.time : new Date(e.time);
        if (isNaN(d.getTime())) return;
        const key = dayKeyOf(d);
        const credit = Number(e.points) || 0;
        if (key === yestKey) {
          if (d.getHours() * 60 + d.getMinutes() <= nowMin) yday += credit;
          return;
        }
        if (key !== todayKey) return;
        fold(d, credit, e.model);
      });
      const models = Array.from(map.values()).sort((a, b) => b.points - a.points);
      models.forEach((m, i) => { m.points = round2(m.points); m.tone = 'c' + ((i % 6) + 1); });
      hours.forEach(h => { h.points = round2(h.points); });
      const peak = hours.reduce((a, b) => (b.points > a.points ? b : a), hours[0]);
      return {
        cloud: true,
        client: want,
        consumed: round2(sum),
        requests: calls,
        tokens: (D.today && D.today.tokens) || 0,   // 云端逐笔不含 Token
        avgPerRequest: calls ? sum / calls : 0,
        deltaPct: yday ? (sum / yday - 1) * 100 : 0,
        hours, models,
        peakHour: peak ? peak.hour : 0,
        peakPoints: peak ? peak.points : 0
      };
    }

    const t = D.today || {};
    const hours = Array.from({ length: 24 }, (_, h) => {
      const src = (D.hourly || []).find(x => x.hour === h) || {};
      return { hour: h, points: src.points || 0, requests: src.requests || 0 };
    });
    return {
      cloud: false,
      client: '',
      consumed: t.consumed || 0,
      requests: t.requests || 0,
      tokens: t.tokens || 0,
      avgPerRequest: t.avgPerRequest || 0,
      deltaPct: t.deltaPct || 0,
      hours,
      models: (D.models || []).map(m => ({ name: m.name, points: m.points, calls: m.calls, tone: m.tone })),
      peakHour: D.peakHour || 0,
      peakPoints: D.peakPoints || 0
    };
  }

  /** 近 N 天每日消耗：今天用实时合并值，其余优先云端逐日、缺失退本机 */
  function dailySeries(n) {
    const { D, ACCOUNT } = ctx();
    const stat = todayStat();
    const cmap = {};
    if (ACCOUNT && ACCOUNT.status === 'connected' && Array.isArray(ACCOUNT.daily)) {
      ACCOUNT.daily.forEach(d => { if (d && d.date) cmap[d.date] = d.points || 0; });
    }
    const lmap = {};
    (D.daily || []).forEach(d => {
      const t = monthDayToDate(d.date);
      if (t) lmap[dayKeyOf(t)] = d.consumed || 0;
    });
    const out = [];
    for (let i = n - 1; i >= 0; i--) {
      const d = addDays(new Date(), -i);
      const key = dayKeyOf(d);
      let v;
      if (i === 0) v = stat.consumed;
      else if (cmap[key] != null) v = cmap[key];
      else v = lmap[key] || 0;
      out.push({ date: key.slice(5), consumed: round2(v), weekday: '日一二三四五六'[d.getDay()] });
    }
    return out;
  }

  /**
   * 上次同步后本机又花掉的分（云端还没入账）。
   * 注意：这只是**下限** —— 本机 JSONL 可能只保留了部分会话，
   * 实际未入账金额可能更高。界面文案要按"至少"来表述。
   */
  const PENDING_MAX_H = 72;   // 服务端只带回最近 72 小时的本地逐笔

  function pendingSinceSync() {
    const { D, ACCOUNT } = ctx();
    if (!ACCOUNT || ACCOUNT.status !== 'connected' || !ACCOUNT.lastSync) return 0;
    const since = new Date(String(ACCOUNT.lastSync).replace(' ', 'T')).getTime();
    if (!since || isNaN(since)) return 0;
    if (Date.now() - since > PENDING_MAX_H * 3600000) return 0;
    const src = (D.recent && D.recent.length)
      ? D.recent
      : (D.ledger || []).map(e => [new Date(e.time).getTime(), Number(e.points) || 0]);
    let sum = 0;
    src.forEach(r => { if (r[0] > since) sum += Number(r[1]) || 0; });
    return round2(sum);
  }

  /** 云端账户优先、本机兜底，并把"同步后未入账"的扣减算进去 */
  function effAccount() {
    const { D, ACCOUNT } = ctx();
    const base = (D && D.account) || {};
    const c = (ACCOUNT && ACCOUNT.status === 'connected' && ACCOUNT.cloud) ? ACCOUNT.cloud : null;
    if (!c || (c.total == null && c.available == null && c.used == null)) return base;
    const total = (c.total != null) ? c.total : base.total;
    const used = (c.used != null) ? c.used
      : (total != null && c.available != null ? Math.max(0, total - c.available) : base.used);
    const available = (c.available != null) ? c.available
      : Math.max(0, (total || 0) - (used || 0));
    const configured = !!(total > 0);
    const pending = pendingSinceSync();
    const liveAvailable = Math.max(0, available - pending);
    const liveUsed = Math.max(0, used + pending);
    return Object.assign({}, base, {
      total, used: liveUsed, available: liveAvailable, configured,
      pending,
      cloudAvailable: available,
      cloudUsed: used,
      usedPct: configured ? Math.min(1, liveUsed / total) : 0,
      cycleStart: c.cycleStartText || base.cycleStart || '',
      cycleEnd: c.cycleEndText || base.cycleEnd || '',
      expireAt: c.expireAtText || '',
      source: 'cloud'
    });
  }

  /** 区间内消耗：云端逐笔（含同步后本机）优先，未连接回落 D.daily */
  function quotaWindow(kind) {
    const { D } = ctx();
    const now = new Date();
    let from;
    if (kind === 'day') {
      from = startOfDay(now);
    } else if (kind === 'month') {
      from = new Date(now.getFullYear(), now.getMonth(), 1);
    } else {
      const wd = (now.getDay() + 6) % 7;          // 周一为 0
      from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - wd);
    }

    const rows = cloudRows();
    if (rows) {
      let sum = 0;
      rows.forEach(r => {
        const t = rowTime(r);
        if (!isNaN(t.getTime()) && t >= from && t <= now) sum += Number(r.credit) || 0;
      });
      localTail().forEach(e => {
        const t = e.time instanceof Date ? e.time : new Date(e.time);
        if (!isNaN(t.getTime()) && t >= from && t <= now) sum += Number(e.points) || 0;
      });
      return { sum: round2(sum), from };
    }

    const mk = s => monthDayToDate(s, now);
    let sum = 0;
    (D.daily || []).forEach(d => {
      const t = mk(d.date);
      if (t && t >= from && t <= now) sum += d.consumed;
    });
    if (kind === 'day' && !sum) sum = (D.today && D.today.consumed) || 0;
    return { sum, from };
  }

  const api = {
    configure, ctx,
    startOfDay, addDays, diffDays, parseDate, dayKeyOf, round2, monthDayToDate,
    isWorkday, countWorkdays, allowance, leftDaysText,
    cloudRows, rowTime, lastSyncMs, localTail,
    clientName, isWorkBuddyClient, clientOptions, clientDays, clientModels, todayByClient,
    todayStat, dailySeries, pendingSinceSync, effAccount, quotaWindow
  };
  return api;
});
