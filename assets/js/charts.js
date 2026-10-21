/* =========================================================
   图表引擎（零依赖 · 纯 SVG / DOM 手绘）
   统一提供：坐标轴、数值标注、占比标注、悬停提示、入场动效
   ========================================================= */
(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  let uid = 0;
  let tipEl = null;

  /* ---------------- 基础工具 ---------------- */

  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  };

  const txt = (parent, x, y, str, cls, anchor) => {
    const t = el('text', { x, y, class: cls || 'ax-label', 'text-anchor': anchor || 'middle' }, parent);
    t.textContent = str;
    return t;
  };

  const h = (tag, cls, html, parent) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    if (parent) parent.appendChild(n);
    return n;
  };

  function token(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function palette() {
    return ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6'].map(v => token(v, '#2563eb'));
  }
  function toneColor(tone) {
    const p = palette();
    const m = /^c([1-6])$/.exec(tone || 'c1');
    return p[(m ? +m[1] : 1) - 1];
  }

  function hexToRgba(hex, a) {
    let c = (hex || '#2563eb').replace('#', '').trim();
    if (c.length === 3) c = c.split('').map(x => x + x).join('');
    const num = parseInt(c, 16);
    if (isNaN(num)) return `rgba(37,99,235,${a})`;
    return `rgba(${(num >> 16) & 255},${(num >> 8) & 255},${num & 255},${a})`;
  }

  function fmt(n, d) {
    const v = Number(n) || 0;
    if (d !== undefined) return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
    // 积分是小数：非整数保留两位，整数不带小数点
    const dec = Math.abs(v) >= 1000 ? 0 : (Number.isInteger(v) ? 0 : 2);
    return v.toLocaleString('zh-CN', { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function shortNum(v) {
    const n = Math.abs(Number(v) || 0);
    if (n >= 1e4) return (v / 1e4).toFixed(n >= 1e5 ? 0 : 1) + '万';
    if (n >= 100) return fmt(Math.round(v));           // 轴刻度取整
    if (n >= 10) return (Math.round(v * 10) / 10).toString();
    if (n > 0) return (Math.round(v * 10) / 10).toString();
    return '0';
  }
  const pct = v => (v * 100).toFixed(1) + '%';

  function motionLevel() {
    return document.documentElement.getAttribute('data-motion') || 'full';
  }
  const easeOut = t => 1 - Math.pow(1 - t, 3);
  const easeInOut = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

  /* ---------------- 动效调度 ---------------- */

  function stopAll(container) {
    (container.__cancels || []).forEach(fn => fn());
    container.__cancels = [];
  }

  function tween(container, duration, onUpdate, delay) {
    const level = motionLevel();
    if (level === 'off') { onUpdate(1); return () => {}; }
    const d = duration * (level === 'reduced' ? 0.45 : 1);
    let raf = null, tid = null, start = null, cancelled = false, done = false;
    const scheduled = performance.now() + (delay || 0);
    // 看门狗：若 rAF 被节流，保证在 1 秒内落到终态
    const guard = setTimeout(() => {
      if (!done && !cancelled) { cancelled = true; onUpdate(1); }
    }, (delay || 0) + d + 400);
    const step = ts => {
      if (cancelled) return;
      if (start === null) {
        start = ts;
        if (ts - scheduled > d + 700) { onUpdate(1); return; }  // 首帧严重迟到：直接终态
      }
      const p = d <= 0 ? 1 : Math.min(1, (ts - start) / d);
      onUpdate(easeOut(p));
      if (p < 1) raf = requestAnimationFrame(step);
      else { done = true; clearTimeout(guard); }
    };
    tid = setTimeout(() => { raf = requestAnimationFrame(step); }, delay || 0);
    return () => { cancelled = true; clearTimeout(tid); clearTimeout(guard); if (raf) cancelAnimationFrame(raf); };
  }

  function addCancel(container, fn) {
    (container.__cancels = container.__cancels || []).push(fn);
  }

  /**
   * 先绘制终态再做动画：
   * WebView2 在窗口未聚焦/被节流时 rAF 可能不推进，若只依赖动画会停在首帧。
   */
  function animate(container, duration, onUpdate, delay) {
    onUpdate(1);
    // 小窗/失焦窗口的 rAF 会被 Chromium 节流，动画会卡在首帧：这类场景只保留终态
    if (document.hidden || !document.hasFocus() || motionLevel() === 'off') return;
    // 桌面壳：WebView2 在窗口被遮挡时会挂起渲染线程，入场动画可能卡在中途，
    // 因此直接呈现终态；浏览器预览仍保留完整动效。
    if (document.body.classList.contains('is-desktop')) return;
    addCancel(container, tween(container, duration, onUpdate, delay));
  }

  /* ---------------- 尺寸 / 重绘 ---------------- */

  function mount(container, draw) {
    // 不使用 ResizeObserver：窗口尺寸变化由 App 统一节流后重绘，
    // 避免"绘制→尺寸变化→再绘制"的反馈循环（此前的卡顿与内存增长来源之一）。
    stopAll(container);
    container.innerHTML = '';
    draw(container);
  }

  function niceMax(maxVal, tickCount) {
    if (!(maxVal > 0)) return { max: 10, step: 2.5 };
    const rough = maxVal / (tickCount || 4);
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    return { max: Math.ceil(maxVal / step) * step, step };
  }

  function topRoundRect(x, y, w, hh, r) {
    const rr = Math.max(0, Math.min(r, w / 2, hh));
    if (hh <= 0.4) return `M${x},${y + hh} h${w}`;
    return `M${x},${y + hh} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + hh} Z`;
  }

  /* ---------------- 提示层 ---------------- */

  function tipNode() {
    if (!tipEl) tipEl = document.getElementById('tooltip');
    return tipEl;
  }

  function showTip(html, x, y) {
    const t = tipNode();
    if (!t) return;
    t.innerHTML = html;
    t.classList.add('is-on');
    const w = t.offsetWidth, hh = t.offsetHeight;
    let left = x + 16;
    let top = y - hh - 14;
    if (left + w > window.innerWidth - 10) left = x - w - 16;
    if (left < 8) left = 8;
    if (top < 8) top = y + 20;
    if (top + hh > window.innerHeight - 8) top = window.innerHeight - hh - 8;
    t.style.setProperty('--tx', left + 'px');
    t.style.setProperty('--ty', top + 'px');
  }

  function hideTip() {
    const t = tipNode();
    if (t) t.classList.remove('is-on');
  }

  function tipRows(rows) {
    return rows.map(r => {
      if (r.hr) return '<div style="height:1px;background:rgba(255,255,255,.12);margin:5px 0"></div>';
      return `<div class="tt-row">${r.color ? `<i class="tt-dot" style="background:${r.color}"></i>` : ''}<span class="tt-name">${r.name}</span><span class="tt-val">${r.value}</span>${r.pct ? `<span class="tt-pct">${r.pct}</span>` : ''}</div>`;
    }).join('');
  }

  /* =========================================================
     柱状图（单系列 / 堆叠）
     ========================================================= */

  function bar(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      if (!W || !H) return;
      const data = cfg.data || [];
      const series = cfg.series || null;
      const minimal = !!cfg.minimal;
      const pad = Object.assign(
        { l: minimal ? 2 : 44, r: minimal ? 2 : 12, t: minimal ? 10 : 26, b: minimal ? 15 : 26 },
        cfg.pad || {});
      const iw = Math.max(10, W - pad.l - pad.r);
      const ih = Math.max(10, H - pad.t - pad.b);
      const svg = el('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, container);

      const totals = data.map((d, i) => series ? series.reduce((s, se) => s + (se.values[i] || 0), 0) : d.value);
      const maxVal = Math.max(1, ...totals);
      const { max, step } = niceMax(maxVal, cfg.yTicks || 4);
      const yOf = v => pad.t + ih - (v / max) * ih;
      const n = data.length;
      const bandW = iw / n;
      const barW = Math.min(cfg.maxBarW || 26, bandW * (cfg.barRatio || 0.56));
      const gradId = 'gb' + (++uid);
      const grad = el('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--brand-light', '#60a5fa') }, grad);
      el('stop', { offset: '100%', 'stop-color': token('--brand', '#2563eb') }, grad);
      const nowGradId = 'gn' + (++uid);
      const nowGrad = el('linearGradient', { id: nowGradId, x1: 0, y1: 0, x2: 0, y2: 1 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--c2', '#22d3ee') }, nowGrad);
      el('stop', { offset: '100%', 'stop-color': token('--brand', '#2563eb') }, nowGrad);

      // 网格 + 左轴
      const ticks = [];
      for (let v = 0; v <= max + 1e-6; v += step) ticks.push(v);
      if (!minimal) ticks.forEach(v => {
        el('line', {
          x1: pad.l, x2: W - pad.r, y1: yOf(v), y2: yOf(v),
          stroke: 'var(--grid)', 'stroke-width': 1, 'stroke-dasharray': v === 0 ? '0' : '3 4',
          'shape-rendering': 'crispEdges'
        }, svg);
        txt(svg, pad.l - 8, yOf(v) + 3.5, shortNum(v), 'ax-label', 'end');
      });

      // 柱体
      const bars = [];
      const labels = [];
      const showAll = !minimal && bandW >= 26 && cfg.showValues !== false;
      const topIdx = totals.map((v, i) => ({ v, i })).sort((a, b) => b.v - a.v).slice(0, 3).map(o => o.i);
      const showTop = !minimal && cfg.showValues !== false && cfg.showValues !== 'none';

      data.forEach((d, i) => {
        const x = pad.l + bandW * i + (bandW - barW) / 2;
        const g = el('g', null, svg);
        const segs = [];
        if (series) {
          let acc = 0;
          series.forEach(se => {
            const v = se.values[i] || 0;
            const seg = el('path', { fill: toneColor(se.tone), 'fill-opacity': se.opacity || 1 }, g);
            segs.push({ node: seg, x, w: barW, base: acc, hh: (v / max) * ih, v });
            acc += v;
          });
        } else {
          const v = d.value;
          const isNow = d.isNow;
          const seg = el('path', { fill: isNow ? `url(#${nowGradId})` : `url(#${gradId})` }, g);
          segs.push({ node: seg, x, w: barW, base: 0, hh: (v / max) * ih, v });
        }
        bars.push(segs);

        // 悬停命中区
        const hit = el('rect', {
          x: pad.l + bandW * i, y: pad.t, width: bandW, height: ih,
          fill: 'transparent', style: 'cursor:default'
        }, svg);
        const total = totals[i];
        const rows = series
          ? series.map(se => ({ color: toneColor(se.tone), name: se.name, value: fmt(se.values[i]) + ' 分', pct: total ? pct((se.values[i] || 0) / total) : '' }))
          : [
            { color: toneColor(cfg.tone || 'c1'), name: cfg.valueName || '积分', value: fmt(total) + ' 分' },
            { name: '占比', value: pct(total / (cfg.sum || totals.reduce((a, b) => a + b, 0) || 1)) }
          ];
        hit.addEventListener('mouseenter', ev => {
          showTip(`<div class="tt-title">${d.tipTitle || d.label}</div>${tipRows(rows)}`, ev.clientX, ev.clientY);
          g.style.opacity = '.82';
        });
        hit.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${d.tipTitle || d.label}</div>${tipRows(rows)}`, ev.clientX, ev.clientY));
        hit.addEventListener('mouseleave', () => { hideTip(); g.style.opacity = '1'; });

        // 数值标注
        if (showAll || (showTop && topIdx.includes(i))) {
          const isTop = topIdx[0] === i;
          const t = txt(svg, pad.l + bandW * i + bandW / 2, 0, total ? shortNum(total) : '', isTop ? 'val-label' : 'val-label is-dim');
          labels.push({ node: t, x: pad.l + bandW * i + bandW / 2, y: yOf(total) - 7, i });
        }
      });

      // X 轴标签
      const every = cfg.xEvery || Math.ceil(n / 8);
      data.forEach((d, i) => {
        if (minimal) {
          if (i % 6 !== 0) return;
        } else if (i % every !== 0 && i !== n - 1) return;
        txt(svg, pad.l + bandW * i + bandW / 2, H - pad.b + 16, d.short || d.label, 'ax-label');
      });

      // 当前时段标记
      if (!minimal && cfg.nowIndex !== undefined && cfg.nowIndex >= 0 && cfg.showNowTag !== false) {
        const nx = pad.l + bandW * cfg.nowIndex + bandW / 2;
        el('line', {
          x1: nx, x2: nx, y1: pad.t - 12, y2: pad.t + ih,
          stroke: 'var(--c2)', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: .7
        }, svg);
        const tag = el('g', null, svg);
        el('rect', { x: nx - 15, y: pad.t - 24, width: 30, height: 15, rx: 4, fill: 'var(--c2)' }, tag);
        const tagText = txt(tag, nx, pad.t - 13, '现在', 'ax-label', 'middle');
        tagText.style.fill = '#fff';
      }

      // 入场动效
      animate(container, 760, p => {
        bars.forEach((segs, i) => {
          const local = Math.max(0, Math.min(1, (p - Math.min(.6, i * 0.018)) / .8));
          const e = easeOut(local);
          segs.forEach(s => {
            const hh = s.hh * e;
            const y = pad.t + ih - (s.base / max) * ih * e - hh;
            s.node.setAttribute('d', topRoundRect(s.x, y, s.w, hh, 4));
          });
        });
        labels.forEach(l => {
          l.node.setAttribute('y', l.y);
          l.node.setAttribute('opacity', Math.max(0, (p - .55) / .45).toFixed(3));
        });
      }, 40);
    });
  }

  /* =========================================================
     组合图：柱（消耗）+ 折线（余额）/ 双轴
     ========================================================= */

  function combo(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      if (!W || !H) return;
      const labels = cfg.labels, bars = cfg.bars, line = cfg.line;
      const pad = { l: 46, r: 48, t: 30, b: 28 };
      const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
      const svg = el('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, container);

      const { max: barMax, step: barStep } = niceMax(Math.max(...bars, 1), 4);
      const lMin = Math.min(...line), lMax = Math.max(...line);
      const span = Math.max(lMax - lMin, 1);
      const lPad = span * 0.18;
      const low = Math.max(0, lMin - lPad), high = lMax + lPad;
      const yBar = v => pad.t + ih - (v / barMax) * ih;
      const yLine = v => pad.t + ih - ((v - low) / (high - low)) * ih;

      const gradId = 'gc' + (++uid);
      const grad = el('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--brand-light', '#60a5fa'), 'stop-opacity': .95 }, grad);
      el('stop', { offset: '100%', 'stop-color': token('--brand', '#2563eb'), 'stop-opacity': .55 }, grad);

      for (let v = 0; v <= barMax + 1e-6; v += barStep) {
        el('line', {
          x1: pad.l, x2: W - pad.r, y1: yBar(v), y2: yBar(v),
          stroke: 'var(--grid)', 'stroke-width': 1, 'stroke-dasharray': v === 0 ? '0' : '3 4'
        }, svg);
        txt(svg, pad.l - 8, yBar(v) + 3.5, shortNum(v), 'ax-label', 'end');
      }
      [low, (low + high) / 2, high].forEach(v => {
        txt(svg, W - pad.r + 8, yLine(v) + 3.5, shortNum(v), 'ax-label', 'start');
      });

      const n = labels.length;
      const bandW = iw / n;
      const barW = Math.min(cfg.maxBarW || 18, bandW * 0.5);
      const paths = [];

      labels.forEach((lb, i) => {
        const x = pad.l + bandW * i + (bandW - barW) / 2;
        const p = el('path', { fill: `url(#${gradId})`, 'fill-opacity': cfg.dimLast && i === n - 1 ? .45 : 1 }, svg);
        paths.push({ node: p, x, hh: (bars[i] / barMax) * ih });

        const hit = el('rect', { x: pad.l + bandW * i, y: pad.t - 10, width: bandW, height: ih + 10, fill: 'transparent' }, svg);
        const day = cfg.days[i];
        const rows = [
          { color: token('--c1'), name: '消耗', value: fmt(bars[i]) + ' 分' },
          { color: token('--c3'), name: cfg.lineName || '累计', value: fmt(line[i]) + ' 分' }
        ];
        hit.addEventListener('mouseenter', ev => {
          showTip(`<div class="tt-title">${lb} ${day.weekday ? '周' + day.weekday : ''}</div>${tipRows(rows)}`, ev.clientX, ev.clientY);
          guide.setAttribute('opacity', '1');
          guide.setAttribute('x1', pad.l + bandW * i + bandW / 2);
          guide.setAttribute('x2', pad.l + bandW * i + bandW / 2);
          dot.setAttribute('cx', pad.l + bandW * i + bandW / 2);
          dot.setAttribute('cy', yLine(line[i]));
          dot.setAttribute('opacity', '1');
        });
        hit.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${lb} ${day.weekday ? '周' + day.weekday : ''}</div>${tipRows(rows)}`, ev.clientX, ev.clientY));
        hit.addEventListener('mouseleave', () => { hideTip(); guide.setAttribute('opacity', '0'); dot.setAttribute('opacity', '0'); });

        if (cfg.xLabels && (i % cfg.xLabels === 0 || i === n - 1)) {
          txt(svg, pad.l + bandW * i + bandW / 2, H - pad.b + 17, lb, 'ax-label');
        }
      });

      const guide = el('line', { x1: 0, x2: 0, y1: pad.t, y2: pad.t + ih, stroke: 'var(--axis)', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0 }, svg);

      // 折线 + 面积
      const pts = line.map((v, i) => [pad.l + bandW * i + bandW / 2, yLine(v)]);
      const dLine = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
      const dArea = `${dLine} L${pts[pts.length - 1][0]},${pad.t + ih} L${pts[0][0]},${pad.t + ih} Z`;
      const areaId = 'ga' + (++uid);
      const aGrad = el('linearGradient', { id: areaId, x1: 0, y1: 0, x2: 0, y2: 1 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--c3', '#7c3aed'), 'stop-opacity': .16 }, aGrad);
      el('stop', { offset: '100%', 'stop-color': token('--c3', '#7c3aed'), 'stop-opacity': 0 }, aGrad);
      const area = el('path', { d: dArea, fill: `url(#${areaId})`, opacity: 0 }, svg);
      const linePath = el('path', { d: dLine, fill: 'none', stroke: token('--c3', '#7c3aed'), 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, svg);
      const dot = el('circle', { r: 4, cx: 0, cy: 0, fill: token('--surface', '#fff'), stroke: token('--c3', '#7c3aed'), 'stroke-width': 2, opacity: 0 }, svg);

      const len = linePath.getTotalLength ? linePath.getTotalLength() : 1200;
      linePath.setAttribute('stroke-dasharray', len);
      linePath.setAttribute('stroke-dashoffset', len);

      animate(container, 900, p => {
        paths.forEach((b, i) => {
          const local = Math.max(0, Math.min(1, (p - Math.min(.5, i * 0.012)) / .82));
          const hh = b.hh * easeOut(local);
          b.node.setAttribute('d', topRoundRect(b.x, pad.t + ih - hh, barW, hh, 3));
        });
        linePath.setAttribute('stroke-dashoffset', len * (1 - easeInOut(p)));
        area.setAttribute('opacity', Math.max(0, (p - .35) / .65).toFixed(3));
      }, 30);
    });
  }

  /* =========================================================
     环形图 + 图例
     ========================================================= */

  function donut(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      if (!W || !H) return;
      const items = (cfg.items || []).filter(d => d.value > 0);
      const total = items.reduce((s, d) => s + d.value, 0) || 1;
      const layout = cfg.layout || 'bottom';
      const wrap = h('div', 'donut-wrap', '', container);
      wrap.style.cssText = 'display:flex;height:100%;width:100%;align-items:center;justify-content:center;';
      if (layout === 'side') { wrap.style.flexDirection = 'row'; wrap.style.gap = '8px'; }
      else { wrap.style.flexDirection = 'column'; wrap.style.gap = '2px'; }

      let size;
      if (layout === 'side') size = Math.min(Math.min(cfg.size || 118, W * 0.4), H);
      else size = Math.min(cfg.size || 172, W - 8, Math.max(80, H * (cfg.hRatio || 0.62)));
      size = Math.max(60, size);

      const box = h('div', '', '', wrap);
      box.style.cssText = `width:${size}px;height:${size}px;flex:none;`;
      const svg = el('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}` }, box);
      const cx = size / 2, cy = size / 2;
      const thick = Math.max(12, size * (layout === 'side' ? .155 : .17));
      const r = cx - thick / 2 - 4;
      const C = 2 * Math.PI * r;

      el('circle', { cx, cy, r, fill: 'none', stroke: 'var(--surface-3)', 'stroke-width': thick }, svg);

      const segs = [];
      let acc = 0;
      items.forEach((d, i) => {
        const share = d.value / total;
        const seg = el('circle', {
          cx, cy, r, fill: 'none',
          stroke: toneColor(d.tone || ('c' + ((i % 6) + 1))),
          'stroke-width': thick,
          'stroke-linecap': 'butt',
          transform: `rotate(-90 ${cx} ${cy})`,
          'stroke-dasharray': `0 ${C}`,
          'stroke-dashoffset': -acc * C,
          style: 'transition:stroke-width 160ms cubic-bezier(.16,1,.3,1), opacity 160ms linear'
        }, svg);
        segs.push({ node: seg, share, start: acc, d });
        acc += share;

        // 百分比：大扇区标在环内，小扇区用引线标到环外，保证每一项都有占比
        if (size >= 130 && share > 0 && items.length > 1) {   // 单一项不标 100%，与图例重复
          const mid = (acc - share / 2) * Math.PI * 2 - Math.PI / 2;
          const cos = Math.cos(mid), sin = Math.sin(mid);
          const pctText = Math.round(share * 1000) / 10;
          const label = (pctText % 1 === 0 ? pctText.toFixed(0) : pctText.toFixed(1)) + '%';
          if (share >= 0.07) {
            const t = txt(svg, cx + cos * r, cy + sin * r + 3.2, label, '', 'middle');
            t.style.fill = '#fff';
            t.style.fontSize = Math.max(8.5, size * .052) + 'px';
            t.style.fontWeight = '600';
            t.setAttribute('opacity', '0');
            t.style.pointerEvents = 'none';
            segs[segs.length - 1].pctNode = t;
          } else {
            const r1 = r + thick / 2 + 1;
            const r2 = r + thick / 2 + 7;
            const ox = cx + cos * r2, oy = cy + sin * r2;
            const line = el('line', {
              x1: cx + cos * r1, y1: cy + sin * r1, x2: ox, y2: oy,
              stroke: 'var(--axis)', 'stroke-width': 0.8, opacity: 0
            }, svg);
            const t = txt(svg, ox + (cos >= 0 ? 3 : -3), oy + 2.8, label,
              cos >= 0 ? 'start' : 'end');
            t.style.fontSize = Math.max(8, size * .045) + 'px';
            t.style.fill = 'var(--text-3)';
            t.setAttribute('opacity', '0');
            t.style.pointerEvents = 'none';
            segs[segs.length - 1].pctNode = t;
            segs[segs.length - 1].pctLine = line;
          }
        }

        seg.addEventListener('mouseenter', ev => {
          showTip(`<div class="tt-title">${d.name}</div>${tipRows([
            { color: toneColor(d.tone || ('c' + ((i % 6) + 1))), name: '消耗', value: fmt(d.value) + ' 分' },
            { name: '占比', value: pct(share) }
          ])}`, ev.clientX, ev.clientY);
          segs.forEach(s => { s.node.style.opacity = s === segs[i] ? '1' : '.42'; });
          seg.setAttribute('stroke-width', thick + 4);
        });
        seg.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${d.name}</div>${tipRows([
          { color: toneColor(d.tone || ('c' + ((i % 6) + 1))), name: '消耗', value: fmt(d.value) + ' 分' },
          { name: '占比', value: pct(share) }
        ])}`, ev.clientX, ev.clientY));
        seg.addEventListener('mouseleave', () => {
          hideTip();
          segs.forEach(s => { s.node.style.opacity = '1'; });
          seg.setAttribute('stroke-width', thick);
        });
      });

      // 圆心文本：按字符数自适应字号，避免"1,234.56"这类长数字压到圆环上。
      // valueOutside=true 时数字整体移到环下方，环内只留一行固定短标签，彻底避免挤压。
      const outside = !!cfg.valueOutside;
      const inner = Math.max(28, (r - thick / 2) * 1.84);
      const fitText = (node, max) => {
        const len = (node.textContent || '').length;
        if (!len) return;
        const sizeByLen = inner / Math.max(1, len * 0.56);
        const s = Math.max(8, Math.min(max, sizeByLen));
        node.style.fontSize = s.toFixed(1) + 'px';
      };
      const hasLabel = cfg.centerLabel !== '';
      const cLabel = hasLabel
        ? txt(svg, cx, outside ? cy + 3.5 : cy - (size > 140 ? 12 : 10), cfg.centerLabel || '今日合计', 'center-sub')
        : null;
      const cVal = outside ? null : txt(svg, cx, cy + (hasLabel ? (size > 140 ? 12 : 8) : 5), '', 'center-main');
      const cUnit = outside ? null : txt(svg, cx, cy + (hasLabel ? (size > 140 ? 28 : 22) : (size > 140 ? 24 : 18)), cfg.centerUnit || '分', 'center-unit');
      if (size < 120) {
        if (cLabel) cLabel.setAttribute('font-size', '9');
        if (cUnit) cUnit.setAttribute('font-size', '9');
      }

      const valueEl = outside ? h('div', 'donut-value', '', wrap) : null;

      // 图例
      const showLegend = cfg.legend !== false;
      let legend = null;
      if (showLegend) {
        legend = h('div', 'legend' + (layout === 'side' ? ' legend--compact' : ''), '', wrap);
        if (layout === 'side') { legend.style.flex = '1'; legend.style.minWidth = '0'; }
        else { legend.style.width = '100%'; legend.style.marginTop = '4px'; }
      }
      if (!showLegend) { wrap.style.flexDirection = 'column'; }
      items.forEach((d, i) => {
        const color = toneColor(d.tone || ('c' + ((i % 6) + 1)));
        if (!legend) return;
        const row = h('div', 'legend-row', '', legend);
        h('i', 'legend-dot', '', row).style.background = color;
        h('span', 'legend-name', d.name, row);
        h('span', 'legend-val', fmt(d.value), row);
        h('span', 'legend-pct', pct(d.value / total), row);
        row.addEventListener('mouseenter', () => {
          segs.forEach((s, si) => { s.node.style.opacity = si === i ? '1' : '.42'; });
          segs[i].node.setAttribute('stroke-width', thick + 4);
        });
        row.addEventListener('mouseleave', () => {
          segs.forEach(s => { s.node.style.opacity = '1'; s.node.setAttribute('stroke-width', thick); });
        });
      });

      // 动效：整体从 12 点方向顺时针绘制
      animate(container, 820, p => {
        const e = easeOut(p);
        segs.forEach(s => {
          const len = s.share * C * e + 0.6;   // 轻微重叠，消除段间缝隙
          s.node.setAttribute('stroke-dasharray', `${len} ${C}`);
          s.node.setAttribute('stroke-dashoffset', -s.start * C * e);
          const op = Math.max(0, (p - .6) / .4).toFixed(3);
          if (s.pctNode) s.pctNode.setAttribute('opacity', op);
          if (s.pctLine) s.pctLine.setAttribute('opacity', (op * 0.75).toFixed(3));
        });
        const val = cfg.centerFormat ? cfg.centerFormat(total * e) : fmt(Math.round(total * e));
        if (cVal) {
          cVal.textContent = val;
          fitText(cVal, size * 0.30);
          if (cfg.centerCenter !== false) {
            cVal.setAttribute('x', cx);
            cVal.setAttribute('text-anchor', 'middle');
          }
        }
        if (valueEl) valueEl.innerHTML = `${val}<em>${cfg.centerUnit || '分'}</em>`;
      }, 40);
    });
  }

  /* =========================================================
     横向排行条
     ========================================================= */

  function hbar(container, cfg) {
    mount(container, () => {
      const items = cfg.items || [];
      const max = Math.max(...items.map(d => d.value), 1);
      const total = items.reduce((s, d) => s + d.value, 0) || 1;
      const list = h('div', 'rank-list', '', container);
      items.forEach((d, i) => {
        const row = h('div', 'rank-row', '', list);
        row.style.animationDelay = (i * 40) + 'ms';
        h('span', 'rank-idx', String(i + 1).padStart(2, '0'), row);
        h('span', 'rank-name', d.name, row);
        const track = h('div', 'rank-track', '', row);
        const fill = h('i', 'rank-fill', '', track);
        fill.style.background = d.tone ? `linear-gradient(90deg, ${hexToRgba(toneColor(d.tone), .55)}, ${toneColor(d.tone)})` : '';
        const val = h('span', 'rank-val', '', row);
        val.innerHTML = `${fmt(d.value)}<small>${pct(d.value / total)}</small>`;

        const target = Math.max(3, (d.value / max) * 100);
        fill.style.width = '0%';
        requestAnimationFrame(() => {
          setTimeout(() => { fill.style.width = target.toFixed(2) + '%'; }, 40 + i * 30);
        });

        row.addEventListener('mouseenter', ev => {
          showTip(`<div class="tt-title">${d.name}</div>${tipRows([
            { color: d.tone ? toneColor(d.tone) : token('--brand'), name: '消耗', value: fmt(d.value) + ' 分' },
            { name: '占今日', value: pct(d.value / total) }
          ])}`, ev.clientX, ev.clientY);
        });
        row.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${d.name}</div>${tipRows([
          { color: d.tone ? toneColor(d.tone) : token('--brand'), name: '消耗', value: fmt(d.value) + ' 分' },
          { name: '占今日', value: pct(d.value / total) }
        ])}`, ev.clientX, ev.clientY));
        row.addEventListener('mouseleave', hideTip);
      });
    });
  }

  /* =========================================================
     热力图（模型 × 时段）
     ========================================================= */

  function heatmap(container, cfg) {
    mount(container, () => {
      const rows = cfg.rows || [];
      const max = cfg.max || 1;
      const base = token('--brand', '#2563eb');
      const wrap = h('div', 'heat', '', container);

      rows.forEach((row, ri) => {
        const r = h('div', 'heat-row', '', wrap);
        h('span', 'heat-label', row.name, r);
        row.values.forEach((v, i) => {
          const a = v <= 0 ? 0.03 : 0.045 + Math.pow(v / max, .5) * 0.92;
          const cell = h('i', 'heat-cell', '', r);
          cell.style.background = v <= 0 ? 'var(--surface-3)' : hexToRgba(base, a.toFixed(3));
          cell.style.animationDelay = (ri * 46 + i * 6) + 'ms';
          cell.addEventListener('mouseenter', ev => {
            showTip(`<div class="tt-title">${row.name} · ${String(i).padStart(2, '0')}:00</div>${tipRows([
              { color: base, name: '消耗', value: fmt(v) + ' 分' },
              { name: '占今日', value: pct(v / (cfg.total || 1)) }
            ])}`, ev.clientX, ev.clientY);
          });
          cell.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${row.name} · ${String(i).padStart(2, '0')}:00</div>${tipRows([
            { color: base, name: '消耗', value: fmt(v) + ' 分' },
            { name: '占今日', value: pct(v / (cfg.total || 1)) }
          ])}`, ev.clientX, ev.clientY));
          cell.addEventListener('mouseleave', hideTip);
        });
      });

      const axis = h('div', 'heat-axis', '', wrap);
      h('span', '', '', axis);
      for (let i = 0; i < 24; i++) h('span', '', i % 3 === 0 ? String(i).padStart(2, '0') : '', axis);

      const lg = h('div', 'heat-legend', '', wrap);
      h('span', '', '低', lg);
      const scale = h('div', 'heat-scale', '', lg);
      [.08, .24, .42, .62, .82].forEach(a => {
        const i = h('i', '', '', scale);
        i.style.background = hexToRgba(base, a);
      });
      h('span', '', '高', lg);
    });
  }

  /* =========================================================
     仪表盘（额度使用率）
     ========================================================= */

  function gauge(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      if (!W || !H) return;
      const svg = el('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, container);
      const cx = W / 2;
      const start = Math.PI * 0.82, sweep = Math.PI * 1.36;
      // 弧线纵向居中：上方极限 cy-r，下方极限 cy+0.53r
      const r = Math.max(40, Math.min(W * 0.38, (H - 16) / 1.58));
      const cy = H / 2 + r * 0.225;

      const arc = (from, to) => {
        const x1 = cx + Math.cos(from) * r, y1 = cy + Math.sin(from) * r;
        const x2 = cx + Math.cos(to) * r, y2 = cy + Math.sin(to) * r;
        return `M${x1},${y1} A${r},${r} 0 ${to - from > Math.PI ? 1 : 0} 1 ${x2},${y2}`;
      };

      el('path', { d: arc(start, start + sweep), fill: 'none', stroke: 'var(--surface-3)', 'stroke-width': 11, 'stroke-linecap': 'round' }, svg);

      const gid = 'gg' + (++uid);
      const grad = el('linearGradient', { id: gid, x1: 0, y1: 1, x2: 1, y2: 0 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--brand', '#2563eb') }, grad);
      el('stop', { offset: '60%', 'stop-color': token('--c2', '#22d3ee') }, grad);
      el('stop', { offset: '100%', 'stop-color': token('--c4', '#f59e0b') }, grad);

      const valPath = el('path', { d: arc(start, start + 0.001), fill: 'none', stroke: `url(#${gid})`, 'stroke-width': 11, 'stroke-linecap': 'round' }, svg);

      const pctVal = cfg.value || 0;
      txt(svg, cx, cy - r * 0.34, cfg.label || '本周期已用', 'center-sub');
      const big = txt(svg, cx, cy + r * 0.14, '0%', 'center-main');
      big.style.fontSize = Math.max(22, Math.min(34, r * 0.42)) + 'px';
      txt(svg, cx, cy + r * 0.14 + Math.max(14, r * 0.16), cfg.sub !== undefined ? cfg.sub : `余 ${cfg.remain !== undefined ? cfg.remain : '—'} 天`, 'center-unit');

      // 端点刻度
      txt(svg, cx + Math.cos(start) * (r + 2), cy + Math.sin(start) * (r + 2) + 15, '0%', 'ax-label');
      txt(svg, cx + Math.cos(start + sweep) * (r + 2), cy + Math.sin(start + sweep) * (r + 2) + 15, '100%', 'ax-label');

      animate(container, 900, p => {
        const e = easeOut(p);
        const v = pctVal * e;
        valPath.setAttribute('d', v <= 0.001 ? arc(start, start + 0.001) : arc(start, start + sweep * v));
        big.textContent = (v * 100).toFixed(1) + '%';
      }, 60);
    });
  }

  /* =========================================================
     迷你趋势线（KPI 卡）
     ========================================================= */

  function sparkline(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      const vals = cfg.values || [];
      if (!W || !H || vals.length < 2) return;
      const svg = el('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, container);
      const min = Math.min(...vals), max = Math.max(...vals);
      const span = max - min || 1;
      const padY = 6;
      const pts = vals.map((v, i) => [
        (i / (vals.length - 1)) * W,
        H - padY - ((v - min) / span) * (H - padY * 2)
      ]);
      const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
      const gid = 'gs' + (++uid);
      const g = el('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, el('defs', null, svg));
      el('stop', { offset: '0%', 'stop-color': token('--brand', '#2563eb'), 'stop-opacity': .22 }, g);
      el('stop', { offset: '100%', 'stop-color': token('--brand', '#2563eb'), 'stop-opacity': 0 }, g);
      el('path', { d: `${d} L${W},${H} L0,${H} Z`, fill: `url(#${gid})`, opacity: 0 }, svg).setAttribute('class', 'spark-area');
      const line = el('path', { d, fill: 'none', stroke: token('--brand', '#2563eb'), 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-opacity': .85 }, svg);
      const dot = el('circle', { cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1], r: 2.4, fill: token('--brand', '#2563eb') }, svg);

      const len = line.getTotalLength ? line.getTotalLength() : 200;
      line.setAttribute('stroke-dasharray', len);
      line.setAttribute('stroke-dashoffset', len);
      animate(container, 780, p => {
        line.setAttribute('stroke-dashoffset', len * (1 - easeOut(p)));
        const area = svg.querySelector('.spark-area');
        if (area) area.setAttribute('opacity', Math.max(0, (p - .3) / .7).toFixed(2));
        dot.setAttribute('opacity', p > .85 ? '1' : '0');
      }, 120);
    });
  }


  /* =========================================================
     日历热力格（按整月对齐 · 自适应铺满卡片宽度）
     cfg: { days: [{date:'2026-10-21', points, calls, tokens}], tone }
     ========================================================= */
  function calendar(container, cfg) {
    mount(container, () => {
      const W = container.clientWidth, H = container.clientHeight;
      if (!W || !H) return;
      const gap = 3;
      const padL = 20, padR = 2, padT = 20, padB = 4;
      const rows = 7;
      const availW = W - padL - padR;
      const availH = H - padT - padB;

      // 行高：7 行正好铺满容器高度
      let cellH = Math.floor((availH - gap * (rows - 1)) / rows);
      cellH = Math.max(10, Math.min(34, cellH));

      const day0 = d => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
      const mondayOf = d => {
        const x = day0(d);
        x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
        return x;
      };
      const today = day0(new Date());
      const thisMonday = mondayOf(today);

      // 目标列数：按"方格"尺寸算，尽量多排几周把宽度用满（格子接近正方形）
      const target = Math.max(6, Math.round((availW + gap) / (cellH + gap)));

      // 起点必须是「某月 1 号所在周的周一」：取列数最接近目标的那个月
      let best = null;
      for (let back = 0; back <= 11; back++) {   // 最多回看 12 个月
        const first = new Date(today.getFullYear(), today.getMonth() - back, 1);
        const start = mondayOf(first);
        const weeks = Math.round((thisMonday - start) / (7 * 86400000)) + 1;
        const score = Math.abs(weeks - target);
        if (!best || score < best.score || (score === best.score && weeks > best.weeks)) {
          best = { start, weeks, score, month: first };
        }
        if (weeks >= target) break;
      }
      const start = best.start;
      const weeks = Math.max(4, best.weeks);
      const monthFirst = new Date(start.getFullYear(), start.getMonth(), 1);

      // 列宽：优先正方（最多宽 6%），剩余宽度整体居中，避免把方格拉成长条
      const fillW = (availW - gap * (weeks - 1)) / weeks;
      const cellW = Math.max(8, Math.min(cellH * 1.06, fillW));
      const gridW = weeks * (cellW + gap) - gap;
      const left = padL + Math.max(0, (availW - gridW) / 2);
      const gridH = rows * (cellH + gap) - gap;

      const svg = el('svg', {
        width: W, height: H, viewBox: `0 0 ${W} ${H}`,
        style: 'overflow:visible;display:block'
      }, container);

      const map = {};
      (cfg.days || []).forEach(d => { map[d.date] = d; });

      const vals = (cfg.days || []).map(d => d.points).filter(v => v > 0).sort((a, b) => a - b);
      const q = p => vals.length ? vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] : 0;
      const steps = [q(.2), q(.45), q(.7), q(.9), vals.length ? vals[vals.length - 1] : 1];
      const level = v => {
        if (!v) return 0;
        for (let i = 0; i < steps.length; i++) if (v <= steps[i]) return i + 1;
        return 5;
      };
      const base = toneColor(cfg.tone || 'c1');
      const alpha = [.075, .18, .38, .58, .78, 1];   // 0~5 级的底色浓度
      const colors = alpha.map(a => (a >= 1 ? base : hexToRgba(base, a)));   // 无消耗：极淡底色，避免"一片灰墙"
      // 悬停联动：本体更亮一档，四周邻格轻微提亮（"被照亮"）
      const hotColors = alpha.map((a, i) => hexToRgba(base, Math.min(1, i === 0 ? .30 : a * 1.35 + .16)));
      const nearColors = alpha.map((a, i) => hexToRgba(base, Math.min(1, a + (i === 0 ? .07 : .13))));

      const fmtTokens = v => (v == null ? '—' : shortNum(v) + ' Token');
      const fmtCalls = v => (v == null ? '—' : fmt(v) + ' 次');
      const glowOn = cfg.glow !== false && motionLevel() !== 'off';

      // 入场动效：从左上角起，一列推一列（每列延迟 14ms）。
      // container.dataset.played 保证同一次进入页面只播一次，resize/切主题不重播。
      const play = cfg.animate !== false && motionLevel() !== 'off'
        && container.dataset.played !== '1' && container.clientWidth > 0;
      if (play) container.dataset.played = '1';
      const cells = [];

      /* ---- 悬停：光圈 + 邻格联动提亮 ---- */
      let halo = null, hot = null, touched = [];
      const glowR = Math.max(28, cellH * 2.8);

      function unfocus() {
        if (hot) hot.node.classList.remove('is-hot');
        for (let i = 0; i < touched.length; i++) touched[i].node.style.fill = '';
        touched = [];
        hot = null;
        if (halo) halo.setAttribute('opacity', '0');
      }

      function focus(c) {
        if (!c || hot === c) return;
        unfocus();
        hot = c;
        const cx0 = c.x + c.w / 2, cy0 = c.y + c.h / 2;
        // 邻格按曼哈顿/对角距离逐级提亮，形成"被照亮一圈"的观感
        for (let i = 0; i < cells.length; i++) {
          const o = cells[i];
          if (o === c) continue;
          const dx = Math.abs(o.col - c.col), dy = Math.abs(o.row - c.row);
          if (dx > 1 || dy > 1) continue;
          if (Math.hypot(dx, dy) > 1.45) continue;
          o.node.style.fill = nearColors[o.lv];
          touched.push(o);
        }
        c.node.style.fill = hotColors[c.lv];
        touched.push(c);
        c.node.classList.add('is-hot');

        halo.setAttribute('cx', cx0);
        halo.setAttribute('cy', cy0);
        halo.setAttribute('r', glowR);
        halo.setAttribute('opacity', '1');
      }

      let prevMonth = -1;
      for (let col = 0; col < weeks; col++) {
        const colStart = new Date(start);
        colStart.setDate(colStart.getDate() + col * 7);
        if (colStart.getMonth() !== prevMonth) {
          prevMonth = colStart.getMonth();
          const x = left + col * (cellW + gap);
          const sameYear = colStart.getFullYear() === today.getFullYear();
          const label = (sameYear ? '' : String(colStart.getFullYear()).slice(2) + '年') + (colStart.getMonth() + 1) + '月';
          const t = txt(svg, x, padT - 7, label, 'ax-label');
          t.setAttribute('text-anchor', 'start');
          t.style.fontSize = '9.5px';
        }
      }

      for (let i = 0; i < weeks * 7; i++) {
        const d = new Date(start);
        d.setDate(d.getDate() + i);
        const col = Math.floor(i / 7), row = i % 7;
        // 起点列里 1 号之前的日子留白，视觉上明确「从当月 1 号开始」
        if (d < monthFirst) continue;
        const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        const rec = map[key];
        const v = rec ? rec.points : 0;
        const lv = level(v);
        const x = left + col * (cellW + gap);
        const y = padT + row * (cellH + gap);
        const future = d > today;
        const rect = el('rect', {
          x, y, width: cellW, height: cellH, rx: Math.min(3.4, cellH * .16),
          fill: colors[lv],
          opacity: future ? '.32' : '1',
          class: 'cal-cell'
        }, svg);
        cells.push({ node: rect, col, row, x, y, w: cellW, h: cellH, lv, op: future ? 0.32 : 1 });

        const wd = '日一二三四五六'[d.getDay()];
        const rowsTip = [
          { color: colors[lv], name: '消耗积分', value: fmt(v) + ' 分' },
          { name: '消耗 Token', value: fmtTokens(rec ? rec.tokens : null) },
          { name: '调用次数', value: fmtCalls(rec ? rec.calls : null) }
        ];
        const title = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} 周${wd}${future ? ' · 未到' : ''}${rec && rec.src === 'local' ? ' · 本机记录' : ''}`;
        rect.addEventListener('mouseenter', ev => {
          showTip(`<div class="tt-title">${title}</div>${tipRows(rowsTip)}`, ev.clientX, ev.clientY);
          if (glowOn) focus(rect.__cell);
        });
        rect.addEventListener('mousemove', ev => showTip(`<div class="tt-title">${title}</div>${tipRows(rowsTip)}`, ev.clientX, ev.clientY));
        rect.addEventListener('mouseleave', () => { hideTip(); if (glowOn) unfocus(); });
        rect.__cell = cells[cells.length - 1];
      }

      // 悬停层放在最上层：光圈要盖在格子上才能"照亮"周围
      const dark = (document.documentElement.getAttribute('data-theme') || 'light') === 'dark';
      const defs = el('defs', null, svg);
      const haloId = 'gh' + (++uid);
      const haloGrad = el('radialGradient', { id: haloId }, defs);
      el('stop', { offset: '0%', 'stop-color': base, 'stop-opacity': dark ? .46 : .28 }, haloGrad);
      el('stop', { offset: '46%', 'stop-color': base, 'stop-opacity': dark ? .20 : .12 }, haloGrad);
      el('stop', { offset: '100%', 'stop-color': base, 'stop-opacity': 0 }, haloGrad);
      const layer = el('g', { 'pointer-events': 'none' }, svg);
      halo = el('circle', {
        cx: -999, cy: -999, r: 0, fill: `url(#${haloId})`, opacity: 0, class: 'cal-halo'
      }, layer);
      svg.addEventListener('mouseleave', () => { hideTip(); unfocus(); });

      if (play && cells.length) {
        requestAnimationFrame(() => {
          cells.forEach(c => {
            // 对角波：从左上角（最早一天）逐格推向右下角（今天）。
            // 延迟用 0.88 次幂压平，靠前的格子间距更匀，波前不会被"拉成台阶"。
            const dist = Math.pow(c.col * 0.94 + c.row * 0.8, 0.88);
            const delay = Math.min(680, dist * 13);
            const frames = [
              { transform: 'scale(.55)', opacity: 0, easing: 'cubic-bezier(.2,.9,.3,1)' },
              { transform: 'scale(1.035)', opacity: c.op, offset: .74, easing: 'cubic-bezier(.33,0,.2,1)' },
              { transform: 'scale(1)', opacity: c.op }
            ];
            const timing = { duration: 640, delay, easing: 'linear', fill: 'backwards' };
            if (c.node.animate) { c.node.animate(frames, timing); return; }
            // 兜底（极老内核）：退回 CSS 过渡
            c.node.style.transformBox = 'fill-box';
            c.node.style.transformOrigin = 'center';
            c.node.style.opacity = '0';
            c.node.style.transform = 'scale(.55)';
            setTimeout(() => {
              c.node.style.transition = 'opacity 340ms ease-out, transform 520ms cubic-bezier(.16,1,.3,1)';
              c.node.style.opacity = String(c.op);
              c.node.style.transform = 'scale(1)';
            }, delay);
          });
        });
        // 看门狗：窗口被遮挡时动画可能整段不推进，到点强制落到终态，绝不停在 scale(.55)
        setTimeout(() => {
          cells.forEach(c => {
            if (c.node.getAnimations) c.node.getAnimations().forEach(a => a.cancel());
          });
        }, 900 + 700);
      }

      // 星期标签（一、三、五）
      ['一', '', '三', '', '五', '', ''].forEach((label, i) => {
        if (!label) return;
        const t = txt(svg, padL - 6, padT + i * (cellH + gap) + cellH - 3, label, 'ax-label');
        t.setAttribute('text-anchor', 'end');
        t.style.fontSize = '9px';
      });

      // 底部说明由卡片副标题承担，这里只画一条基线
      el('line', {
        x1: left, x2: W - padR, y1: padT + gridH + 4, y2: padT + gridH + 4,
        stroke: 'var(--grid)', 'stroke-width': 1
      }, svg);
    });
  }

  window.Charts = {
    bar, combo, donut, hbar, heatmap, gauge, sparkline, calendar,
    showTip, hideTip, palette, toneColor, fmt, shortNum, hexToRgba
  };
})();
