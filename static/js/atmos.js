/* ==========================================================================
   Night Apron — the live backdrop.

   One canvas, four layers, drawn in this order:

     1  apron grid   a slow drifting lattice, the painted ramp
     2  route web    drifting waypoints that link to their neighbours, and
                     reach for the cursor when it comes near
     3  radar        a green sweep in the corner, the way an airfield is
                     always being scanned
     4  traffic      aircraft crossing with dashed contrails behind them

   Interactive on purpose: the web leans toward the pointer, and a click
   launches an aircraft from wherever you clicked.

   Admin pages are data-dense, so they get a calmer version — fewer
   waypoints, no radar, lower contrast — because a departure board should
   not have to compete with the wallpaper.
   ========================================================================== */

(function () {
  const cv = document.getElementById('apron');
  if (!cv) return;

  const ctx = cv.getContext('2d', { alpha: true });
  const calm = document.body.classList.contains('is-dense');
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Night Apron palette, as canvas needs numbers rather than custom properties.
  const SODIUM = '255, 179, 71';
  const TAXI = '140, 175, 215';
  const THRESHOLD = '47, 217, 138';

  const CFG = calm
    ? { nodes: 1.9e4, link: 112, reach: 120, web: 0.095, grid: 0.040, radar: false,
        planes: 1, spawn: 0.0018 }
    : { nodes: 1.5e4, link: 132, reach: 180, web: 0.13, grid: 0.045, radar: true,
        planes: 3, spawn: 0.004 };

  let W = 0, H = 0, nodes = [], planes = [], t = 0;
  let mx = -9e3, my = -9e3, running = true, raf = 0;

  function size() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    W = innerWidth;
    H = innerHeight;
    cv.width = W * dpr;
    cv.height = H * dpr;
    cv.style.width = W + 'px';
    cv.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const n = Math.round(Math.min(78, Math.max(22, (W * H) / CFG.nodes)));
    nodes = Array.from({ length: n }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.17,
      vy: (Math.random() - 0.5) * 0.17,
      r: Math.random() * 1.4 + 0.5,
      a: Math.random() * 0.6 + 0.25,
    }));
  }

  function launch(x, y, angle) {
    planes.push({
      x, y,
      a: angle === undefined ? Math.random() * Math.PI * 2 : angle,
      s: Math.random() * 0.8 + 0.8,
      trail: [],
      age: 0,
    });
  }

  /* ---------------------------------------------------------------- draw */

  // start() is the only way a loop begins, and frame() clears the handle before
  // requesting the next one. A page that loads in a hidden tab fires no frames
  // at all, so without this guard the visibilitychange wake-up could start a
  // second loop alongside the first and run everything at double speed.
  function start() {
    if (!raf && running) raf = requestAnimationFrame(frame);
  }

  function frame() {
    raf = 0;
    if (!running) return;
    render();
    raf = requestAnimationFrame(frame);
  }

  // Drawing is separate from scheduling so the backdrop can be painted once,
  // synchronously, at startup: requestAnimationFrame does not fire at all in a
  // hidden tab, and without this the canvas would stay blank until the tab was
  // first looked at.
  function render() {
    t += 0.006;
    ctx.clearRect(0, 0, W, H);

    /* 1 · the apron grid, drifting toward the viewer */
    const gs = 74;
    const off = (t * 10) % gs;
    ctx.strokeStyle = `rgba(${TAXI}, ${CFG.grid})`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let y = off; y < H; y += gs) { ctx.moveTo(0, y); ctx.lineTo(W, y); }
    for (let x = -off; x < W; x += gs) { ctx.moveTo(x, 0); ctx.lineTo(x, H); }
    ctx.stroke();

    /* 3 · radar sweep — drawn under the web so the web stays legible */
    if (CFG.radar && ctx.createConicGradient) {
      const rx = W * 0.85, ry = H * 0.78, rr = Math.min(W, H) * 0.36;
      ctx.save();
      ctx.translate(rx, ry);
      ctx.strokeStyle = `rgba(${THRESHOLD}, .05)`;
      ctx.lineWidth = 1;
      for (let k = 1; k <= 3; k++) {
        ctx.beginPath();
        ctx.arc(0, 0, (rr * k) / 3, 0, Math.PI * 2);
        ctx.stroke();
      }
      const g = ctx.createConicGradient(t * 0.5, 0, 0);
      g.addColorStop(0, `rgba(${THRESHOLD}, .085)`);
      g.addColorStop(0.07, `rgba(${THRESHOLD}, 0)`);
      g.addColorStop(1, `rgba(${THRESHOLD}, 0)`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(0, 0, rr, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    /* 2 · the route web */
    for (const n of nodes) {
      n.x += n.vx;
      n.y += n.vy;
      if (n.x < -40) n.x = W + 40; else if (n.x > W + 40) n.x = -40;
      if (n.y < -40) n.y = H + 40; else if (n.y > H + 40) n.y = -40;
      if (CFG.reach) {
        const dx = mx - n.x, dy = my - n.y;
        if (dx * dx + dy * dy < 42000) { n.x += dx * 0.0015; n.y += dy * 0.0015; }
      }
    }

    const L = CFG.link;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < L * L) {
          const o = (1 - Math.sqrt(d2) / L) * CFG.web;
          ctx.strokeStyle = `rgba(${TAXI}, ${o.toFixed(3)})`;
          ctx.lineWidth = 0.7;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
      if (CFG.reach) {
        const dm = Math.hypot(a.x - mx, a.y - my);
        if (dm < CFG.reach) {
          ctx.strokeStyle =
            `rgba(${SODIUM}, ${((1 - dm / CFG.reach) * 0.24).toFixed(3)})`;
          ctx.lineWidth = 0.8;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(mx, my);
          ctx.stroke();
        }
      }
      ctx.fillStyle = `rgba(190, 212, 238, ${(a.a * 0.5).toFixed(2)})`;
      ctx.beginPath();
      ctx.arc(a.x, a.y, a.r, 0, Math.PI * 2);
      ctx.fill();
    }

    /* 4 · traffic */
    for (let i = planes.length - 1; i >= 0; i--) {
      const p = planes[i];
      p.x += Math.cos(p.a) * p.s;
      p.y += Math.sin(p.a) * p.s;
      p.a += Math.sin(t * 0.7 + i) * 0.0011;   // a long, lazy turn
      p.age++;
      p.trail.push(p.x, p.y);
      if (p.trail.length > 220) p.trail.splice(0, 2);

      ctx.beginPath();
      ctx.moveTo(p.trail[0], p.trail[1]);
      for (let k = 2; k < p.trail.length; k += 2) ctx.lineTo(p.trail[k], p.trail[k + 1]);
      ctx.strokeStyle = `rgba(${SODIUM}, .11)`;
      ctx.lineWidth = 1.1;
      ctx.setLineDash([5, 6]);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.a);
      ctx.fillStyle = `rgba(255, 213, 150, .8)`;
      ctx.beginPath();
      ctx.moveTo(7, 0);
      ctx.lineTo(-5, -4);
      ctx.lineTo(-2.5, 0);
      ctx.lineTo(-5, 4);
      ctx.closePath();
      ctx.fill();
      ctx.restore();

      if (p.age > 2800 || p.x < -160 || p.x > W + 160 || p.y < -160 || p.y > H + 160) {
        planes.splice(i, 1);
      }
    }

    // keep a little traffic in the air, entering from a random edge
    if (planes.length < CFG.planes && Math.random() < CFG.spawn) {
      const e = (Math.random() * 4) | 0;
      const j = Math.random() * 0.8 - 0.4;
      if (e === 0) launch(-50, Math.random() * H, j);
      else if (e === 1) launch(W + 50, Math.random() * H, Math.PI + j);
      else if (e === 2) launch(Math.random() * W, -50, Math.PI / 2 + j);
      else launch(Math.random() * W, H + 50, -Math.PI / 2 + j);
    }
  }

  /* --------------------------------------------------------------- wiring */

  addEventListener('resize', () => { size(); render(); }, { passive: true });
  addEventListener('mousemove', (e) => { mx = e.clientX; my = e.clientY; }, { passive: true });
  addEventListener('mouseout', () => { mx = my = -9e3; }, { passive: true });

  // Click to launch — but never steal a click meant for the page.
  addEventListener('click', (e) => {
    if (e.target.closest('a, button, input, select, textarea, label, summary, .seat')) return;
    launch(e.clientX, e.clientY, Math.random() * Math.PI * 2);
  });

  // A backdrop has no business burning battery behind a hidden tab.
  document.addEventListener('visibilitychange', () => {
    running = !document.hidden;
    if (running) {
      start();
    } else {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  });

  size();
  launch(Math.random() * W, Math.random() * H);
  render();            // paint now, so there is never an empty canvas
  if (!still) start(); // and animate, unless the reader asked us not to
})();
