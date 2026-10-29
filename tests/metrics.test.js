/* 数据口径单测（无依赖，直接 node tests/metrics.test.js）
   覆盖：日期解析、工作日折算、今日合并、待入账、可用余额、区间统计、每日序列。
   这些函数决定界面上每一个"分"，改坏了肉眼很难发现，所以必须有断言兜着。 */
'use strict';

const assert = require('assert');
const path = require('path');
const M = require(path.join(__dirname, '..', 'assets', 'js', 'metrics.js'));

let passed = 0;
function ok(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ✓', name);
  } catch (e) {
    console.error('  ✗', name, '\n   ', e.message);
    process.exitCode = 1;
  }
}

const pad = n => String(n).padStart(2, '0');
const fmt = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

const NOW = new Date();
const TODAY = M.dayKeyOf(NOW);
const YEST = M.dayKeyOf(M.addDays(NOW, -1));

/* ---------------- 日期 ---------------- */

console.log('日期解析');
ok('yyyy-mm-dd 正常解析', () => {
  const d = M.parseDate('2026-11-19');
  assert.strictEqual(d.getFullYear(), 2026);
  assert.strictEqual(d.getMonth(), 10);
  assert.strictEqual(d.getDate(), 19);
});
ok('MM-DD 自动补年份（跨年归到明年）', () => {
  const d = M.parseDate('11-19');
  assert.ok(d instanceof Date && !isNaN(d));
  assert.strictEqual(d.getMonth(), 10);
  assert.strictEqual(d.getDate(), 19);
  assert.ok(d >= M.startOfDay(NOW), '解析出的日期不应早于今天');
});
ok('空值返回 null', () => {
  assert.strictEqual(M.parseDate(''), null);
  assert.strictEqual(M.parseDate(null), null);
});

/* ---------------- 工作日 ---------------- */

console.log('工作日折算');
ok('2026-10-19(一) ~ 10-25(日) 有 5 个工作日', () => {
  const from = new Date(2026, 9, 19), to = new Date(2026, 9, 25);
  assert.strictEqual(M.countWorkdays(from, to, true), 5);
});
ok('关闭折算后按自然日算（7 天）', () => {
  const from = new Date(2026, 9, 19), to = new Date(2026, 9, 25);
  assert.strictEqual(M.countWorkdays(from, to, false), 7);
});

/* ---------------- 额度折算 ---------------- */

console.log('额度折算');
const expire = M.dayKeyOf(M.addDays(NOW, 10));

ok('关闭签到后池子里只剩可用余额', () => {
  M.configure({ prefs: () => ({ holiday: false, signin: false }) });
  const al = M.allowance({ total: 1000, available: 700, expireAt: expire });
  assert.strictEqual(al.spendDays, 11);          // 今天 ~ 10 天后，含首尾
  assert.ok(Math.abs(al.perDay - 700 / 11) < 1e-9);
  assert.ok(Math.abs(al.perWeek - (700 / 11) * 7) < 1e-9);
});
ok('打开签到 = 每天 +100（含周末）', () => {
  M.configure({ prefs: () => ({ holiday: false, signin: true }) });
  const al = M.allowance({ total: 1000, available: 700, expireAt: expire });
  assert.strictEqual(al.signin, 1100);
  assert.ok(Math.abs(al.perDay - (700 + 1100) / 11) < 1e-9);
});
ok('有效期缺失时回退 30 天窗口', () => {
  M.configure({ prefs: () => ({ holiday: false, signin: false }) });
  const al = M.allowance({ total: 1000, available: 700, expireAt: '' });
  assert.strictEqual(al.leftDays, 31);
});

/* ---------------- 今日合并 / 待入账 / 可用余额 ---------------- */

console.log('今日口径');
const lastSync = new Date(NOW.getTime() - 30 * 60000);          // 30 分钟前同步
const rows = [
  { id: 'a', t: `${TODAY} 10:05:00`, credit: 1.5, model: 'm1', purpose: 'conversation' },
  { id: 'b', t: `${TODAY} 10:06:00`, credit: 2.5, model: 'm2', purpose: 'conversation' },
  { id: 'c', t: `${YEST} 09:00:00`, credit: 9, model: 'm1', purpose: 'conversation' }
];
const ledger = [
  { id: 'l1', time: new Date(NOW.getTime() - 5 * 60000), type: 'consume', points: 3, model: 'm1', tokens: 10 },
  { id: 'l2', time: new Date(NOW.getTime() - 90 * 60000), type: 'consume', points: 8, model: 'm1', tokens: 10 }
];
M.configure({
  prefs: () => ({ holiday: false, signin: false }),
  account: () => ({
    status: 'connected',
    lastSync: fmt(lastSync),
    cloud: { total: 200, available: 100, used: 100, expireAtText: '', cycleStartText: TODAY },
    usageRows: rows,
    daily: [{ date: TODAY, points: 4 }, { date: YEST, points: 9 }]
  }),
  data: () => ({
    ledger,
    recent: [[NOW.getTime() - 5 * 60000, 3], [NOW.getTime() - 90 * 60000, 8]],
    today: { consumed: 999, tokens: 55 },
    daily: [],
    hourly: [],
    models: []
  })
});

ok('今日 = 云端今日 + 同步后本机（昨天的不算）', () => {
  const st = M.todayStat();
  assert.strictEqual(st.cloud, true);
  assert.strictEqual(st.consumed, 7);       // 1.5 + 2.5 + 3
  assert.strictEqual(st.requests, 3);
  assert.strictEqual(st.hours[10].points, 4);
  assert.strictEqual(st.models.length, 2);
  assert.strictEqual(st.tokens, 55);        // Token 仍取本机
});
ok('待入账只算 lastSync 之后的本机逐笔', () => {
  assert.strictEqual(M.pendingSinceSync(), 3);
});
ok('可用余额 = 云端余额 − 待入账', () => {
  const acc = M.effAccount();
  assert.strictEqual(acc.available, 97);
  assert.strictEqual(acc.used, 103);
  assert.ok(Math.abs(acc.usedPct - 103 / 200) < 1e-9);
  assert.strictEqual(acc.pending, 3);
});
ok('今日区间统计与 todayStat 一致', () => {
  assert.strictEqual(M.quotaWindow('day').sum, 7);
});
ok('每日序列：今天用实时值，昨天用云端逐日', () => {
  const s = M.dailySeries(2);
  assert.strictEqual(s.length, 2);
  assert.strictEqual(s[1].consumed, 7);
  assert.strictEqual(s[0].consumed, 9);
});

/* ---------------- 未连接时回落本机 ---------------- */

console.log('未连接回落');
M.configure({
  account: () => ({ status: 'disconnected' }),
  data: () => ({
    today: { consumed: 12, requests: 4, tokens: 8, avgPerRequest: 3, deltaPct: 0 },
    hourly: [{ hour: 9, points: 12, requests: 4 }],
    models: [{ name: 'm1', points: 12, calls: 4, tone: 'c1' }],
    daily: [{ date: TODAY.slice(5), consumed: 12 }],
    ledger: [],
    recent: []
  })
});
ok('未连接时今日取本机记录', () => {
  const st = M.todayStat();
  assert.strictEqual(st.cloud, false);
  assert.strictEqual(st.consumed, 12);
  assert.strictEqual(st.hours[9].points, 12);
});
ok('未连接时可用余额为空、pending 为 0', () => {
  assert.strictEqual(M.pendingSinceSync(), 0);
  assert.deepStrictEqual(M.effAccount(), {});   // D.account 未给，回落空对象
});

/* ---------------- 跨年 ---------------- */

console.log('跨年还原');
ok('12-31 在 1 月查看 → 归到上一年', () => {
  const d = M.monthDayToDate('12-31', new Date(2026, 0, 2));
  assert.strictEqual(d.getFullYear(), 2025);
  assert.strictEqual(d.getMonth(), 11);
  assert.strictEqual(d.getDate(), 31);
});
ok('01-01 在 10 月查看 → 保持当年', () => {
  const d = M.monthDayToDate('01-01', new Date(2026, 9, 22));
  assert.strictEqual(d.getFullYear(), 2026);
  assert.strictEqual(d.getMonth(), 0);
});
ok('今天本身不往回退一年', () => {
  const today = new Date(2026, 9, 22, 18, 30);
  const d = M.monthDayToDate('10-22', today);
  assert.strictEqual(M.dayKeyOf(d), '2026-10-22');
});
ok('非 MM-DD 输入返回 null', () => {
  assert.strictEqual(M.monthDayToDate('2026-01-01'), null);
  assert.strictEqual(M.monthDayToDate(''), null);
  assert.strictEqual(M.monthDayToDate(null), null);
});

/* ---------------- 来源（WorkBuddy / CodeBuddy） ---------------- */

console.log('来源拆分');
const clientRows = [
  { id: 'a', t: `${TODAY} 10:05:00`, credit: 1.5, model: 'm1', purpose: 'conversation', client: 'workbuddy' },
  { id: 'b', t: `${TODAY} 10:06:00`, credit: 2.5, model: 'm2', purpose: 'conversation', client: 'codebuddy' },
  { id: 'c', t: `${YEST} 09:00:00`, credit: 9, model: 'm1', purpose: 'conversation', client: 'codebuddy' }
];
M.configure({
  prefs: () => ({ holiday: false, signin: false }),
  account: () => ({
    status: 'connected',
    lastSync: fmt(lastSync),
    cloud: { total: 200, available: 100, used: 100 },
    usageRows: clientRows,
    daily: [{ date: TODAY, points: 4 }],
    cloudClients: {
      workbuddy: { points: 1.5, calls: 1, days: [{ date: TODAY, points: 1.5, calls: 1 }],
                   models: [{ name: 'm1', points: 1.5, calls: 1 }] },
      codebuddy: { points: 11.5, calls: 2,
                   days: [{ date: TODAY, points: 2.5, calls: 1 }, { date: YEST, points: 9, calls: 1 }],
                   models: [{ name: 'm2', points: 2.5, calls: 1 }] }
    }
  }),
  data: () => ({
    ledger,
    recent: [[NOW.getTime() - 5 * 60000, 3]],
    today: { consumed: 999, tokens: 55 },
    daily: [],
    hourly: [],
    models: []
  })
});

ok('来源名：已知标识翻译，空值标注为历史', () => {
  assert.strictEqual(M.clientName('workbuddy'), 'WorkBuddy');
  assert.strictEqual(M.clientName('codebuddy'), 'CodeBuddy');
  assert.strictEqual(M.clientName('CodeBuddyIDE'), 'CodeBuddy');   // 服务端实际取值
  assert.strictEqual(M.clientName('WorkBuddy'), 'WorkBuddy');      // 服务端实际取值
  assert.strictEqual(M.clientName('CodeBuddyCN'), 'CodeBuddy');    // 换后缀也归一
  assert.strictEqual(M.clientName(''), '历史（未标注）');
  assert.strictEqual(M.clientName('weird'), 'weird');
});
ok('可区分时给出「全部 + 各来源」', () => {
  const opts = M.clientOptions();
  assert.ok(opts);
  assert.deepStrictEqual(opts.map(o => o.id), ['', 'codebuddy', 'workbuddy']);
  assert.strictEqual(opts[0].name, '全部');
  assert.strictEqual(opts[0].points, 13);
});
ok('CodeBuddy 只算自己的云端行，且不并入本机 tail', () => {
  const st = M.todayStat('codebuddy');
  assert.strictEqual(st.client, 'codebuddy');
  assert.strictEqual(st.consumed, 2.5);       // 本机那 3 分属于 WorkBuddy
  assert.strictEqual(st.requests, 1);
});
ok('WorkBuddy 会并入同步后的本机 tail', () => {
  const st = M.todayStat('workbuddy');
  assert.strictEqual(st.consumed, 4.5);       // 1.5 云端 + 3 本机
  assert.strictEqual(st.requests, 2);
});
ok('不带参数仍然算全部', () => {
  assert.strictEqual(M.todayStat().consumed, 7);
});
ok('按来源的按天 / 按模型聚合取得到', () => {
  assert.deepStrictEqual(M.clientDays('codebuddy').map(d => d.points), [2.5, 9]);
  assert.strictEqual(M.clientModels('codebuddy')[0].name, 'm2');
  assert.strictEqual(M.clientDays('workbuddy')[0].date, TODAY);
});
ok('今日各来源并列（总览小字用）', () => {
  const by = M.todayByClient();
  assert.deepStrictEqual(by.map(x => [x.name, x.points]), [['CodeBuddy', 2.5], ['WorkBuddy', 4.5]]);
});
ok('今日零消耗的来源不挤进 KPI 脚注', () => {
  M.configure({
    account: () => ({
      status: 'connected',
      usageRows: clientRows,
      cloudClients: {
        workbuddy: { points: 1.5, calls: 1, days: [{ date: TODAY, points: 1.5, calls: 1 }], models: [] },
        codebuddy: { points: 11.5, calls: 2, days: [{ date: TODAY, points: 2.5, calls: 1 }], models: [] },
        app_cloud: { points: 5.6, calls: 9, days: [{ date: YEST, points: 5.6, calls: 9 }], models: [] }
      }
    })
  });
  const by = M.todayByClient();
  assert.deepStrictEqual(by.map(x => x.id), ['codebuddy', 'workbuddy'], 'app_cloud 今天没消耗，不该出现');
});
ok('历史未标注桶不占切换项，金额挂在「全部」上说明', () => {
  M.configure({
    account: () => ({
      status: 'connected',
      usageRows: clientRows,
      cloudClients: {
        workbuddy: { points: 1.5, calls: 1, days: [], models: [] },
        codebuddy: { points: 11.5, calls: 2, days: [], models: [] },
        '': { points: 30, calls: 5, days: [], models: [] }
      }
    })
  });
  const opts = M.clientOptions();
  assert.deepStrictEqual(opts.map(o => o.id), ['', 'codebuddy', 'workbuddy']);  // 只有一个空 id
  assert.strictEqual(opts.unlabeled, 30);
  assert.strictEqual(opts[0].points, 43);                                       // 全部含未标注
});

ok('字段缺失（只有一个未标注桶）时不提供拆分', () => {
  M.configure({
    account: () => ({
      status: 'connected',
      usageRows: clientRows.map(r => Object.assign({}, r, { client: '' })),
      cloudClients: { '': { points: 13, calls: 3, days: [], models: [] } }
    })
  });
  assert.strictEqual(M.clientOptions(), null);
  assert.strictEqual(M.todayByClient(), null);
});

console.log(`\n${passed} 项通过`);
