/* =========================================================
   格式化与转义：纯函数，不依赖任何状态
   拆分自 app.js —— 原来这些散在 2900 行的巨文件里，改个数字格式要翻半天
   ========================================================= */
(function (global) {
  'use strict';

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

  /**
   * HTML 转义：模型名 / 项目名 / 积分包名都来自外部数据，拼进模板前必须过一遍。
   * 本地目录名里出现 < > & 完全合法，不转义轻则破坏结构，重则注入脚本。
   */
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  global.F = F;
  global.esc = esc;
})(window);
