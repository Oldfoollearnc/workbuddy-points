/* =========================================================
   卡片轮播组件：拖动 / 点指示点 / 可选自动播
   拆分自 app.js —— 纯 DOM 组件，不碰业务状态，两处（左下角、小窗）共用
   ========================================================= */
(function (global) {
  'use strict';

  /** 通用卡片轮播：拖动、点指示点、可选自动播。少于两张卡时直接不启用。 */
  function createSwipe(root, opts) {
    if (!root) return null;
    const o = Object.assign({ auto: false, interval: 5000 }, opts || {});
    const track = root.querySelector('.swipe-track');
    const dotsBox = root.querySelector('.swipe-dots');
    if (!track) return null;
    const cards = Array.prototype.slice.call(track.children);
    if (cards.length < 2) return null;

    let index = 0, timer = null, drag = false, startX = 0, startY = 0, dx = 0, axis = '';

    if (dotsBox) {
      dotsBox.innerHTML = cards.map((_, i) =>
        `<i class="swipe-dot${i === 0 ? ' is-on' : ''}" data-i="${i}"></i>`).join('');
      // 必须用 pointerdown + stopPropagation：root 上有 setPointerCapture，
      // 捕获之后 click 的 target 会变成 root，closest('.swipe-dot') 永远拿不到点，
      // 表现就是"点了一下、卡片抖一下但没切换"。
      dotsBox.addEventListener('pointerdown', e => {
        const d = e.target.closest ? e.target.closest('.swipe-dot') : null;
        if (!d) return;
        e.stopPropagation();
        e.preventDefault();
        jump(+d.dataset.i, 340);
        restartAuto();
      });
    }

    function paint() {
      track.style.transform = 'translateX(' + (-index * 100) + '%)';
      if (dotsBox) {
        Array.prototype.forEach.call(dotsBox.children, (d, i) =>
          d.classList.toggle('is-on', i === index));
      }
      // 切到新卡片：内容轻轻起一下，不然只是位置变了、像一帧硬切
      const card = cards[index];
      if (card) {
        card.classList.remove('is-in');
        void card.offsetWidth;
        card.classList.add('is-in');
      }
      if (o.onIndex) o.onIndex(index);
    }

    /** ms > 0 时用指定时长过渡，用于"回卷"和"点指示点"，避免跨多张的硬切。 */
    function jump(i, ms) {
      if (i === index && !ms) return;
      if (ms) track.style.transitionDuration = ms + 'ms';
      index = i;
      paint();
      if (ms) setTimeout(() => { track.style.transitionDuration = ''; }, ms + 60);
    }

    function autoNext() {
      // 自动播到末尾：用短过渡翻回第一张，而不是一路扫过三张
      if (index >= cards.length - 1) jump(0, 300);
      else jump(index + 1, 0);
    }

    /** 手动翻页不循环：到头就轻轻顶一下，比"从第 4 张硬挤到第 1 张"自然。 */
    function step(delta) {
      const t = index + delta;
      if (t < 0 || t > cards.length - 1) { edge(delta); return; }
      jump(t, 0);
    }

    function edge(delta) {
      const dir = delta > 0 ? 1 : -1;
      track.style.transitionDuration = '110ms';
      track.style.transform = 'translateX(' + (-index * 100 + dir * 1.6) + '%)';
      setTimeout(() => { track.style.transitionDuration = ''; paint(); }, 130);
    }

    function restartAuto() {
      clearInterval(timer);
      if (!o.auto || !o.interval) return;
      timer = setInterval(() => {
        // 界面没露出来就不切：用户看不见的翻页没有意义
        if (document.hidden) return;
        autoNext();
      }, o.interval);
    }

    root.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      drag = true; startX = e.clientX; startY = e.clientY; dx = 0; axis = '';
      root.classList.add('is-drag');
      try { root.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      clearInterval(timer);
    });

    root.addEventListener('pointermove', e => {
      if (!drag) return;
      const mx = e.clientX - startX, my = e.clientY - startY;
      if (!axis) {
        if (Math.abs(mx) < 6 && Math.abs(my) < 6) return;
        axis = Math.abs(mx) > Math.abs(my) ? 'x' : 'y';
        if (axis === 'y') { drag = false; root.classList.remove('is-drag'); restartAuto(); return; }
      }
      dx = mx;
      const off = (dx / (root.clientWidth || 1)) * 100;
      track.style.transform = 'translateX(' + (-index * 100 + off) + '%)';
    });

    function endDrag() {
      if (!drag) return;
      drag = false;
      root.classList.remove('is-drag');
      const th = (root.clientWidth || 1) * 0.16;
      if (dx < -th) step(1);
      else if (dx > th) step(-1);
      else paint();
      dx = 0;
      restartAuto();
    }
    root.addEventListener('pointerup', endDrag);
    root.addEventListener('pointercancel', endDrag);
    root.addEventListener('mouseenter', () => clearInterval(timer));
    root.addEventListener('mouseleave', restartAuto);

    paint();
    restartAuto();

    return {
      go: i => jump(i, 340),
      next: () => step(1),
      prev: () => step(-1),
      index: () => index,
      reset: () => jump(0, 0),
      setInterval: ms => { o.interval = ms; restartAuto(); }
    };
  }

  global.createSwipe = createSwipe;
})(window);
