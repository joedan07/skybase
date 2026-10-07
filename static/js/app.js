/* SkyBase — the small amount of JS the pages actually need.
   No framework: four behaviours, each a dozen lines. */

/* ---- 1 · split-flap boards ---------------------------------------------
   Each [data-flap] renders its text as character cells, then flaps them in
   with a stagger, the way a Solari board settles. */
(function () {
  const CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  document.querySelectorAll('[data-flap]').forEach((el, idx) => {
    const text = (el.dataset.flap || '').toUpperCase();
    el.textContent = '';
    const cells = [...text].map((ch) => {
      const i = document.createElement('i');
      i.textContent = ch === ' ' ? ' ' : ch;
      el.appendChild(i);
      return { node: i, final: ch };
    });
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    cells.forEach((c, k) => {
      if (c.final === ' ') return;
      let spins = 3 + (k % 3);
      const tick = () => {
        if (spins-- <= 0) {
          c.node.textContent = c.final;
          c.node.classList.add('is-flipping');
          setTimeout(() => c.node.classList.remove('is-flipping'), 280);
          return;
        }
        c.node.textContent = CHARS[(Math.random() * CHARS.length) | 0];
        setTimeout(tick, 55);
      };
      setTimeout(tick, idx * 70 + k * 45);
    });
  });
})();

/* ---- 2 · origin/destination cannot be the same -------------------------
   The database has CHECK(origin_id <> dest_id); the form should not let you
   reach it in the first place. */
(function () {
  const o = document.getElementById('origin');
  const d = document.getElementById('dest');
  if (!o || !d) return;
  const sync = (changed, other) => {
    [...other.options].forEach((opt) => {
      opt.disabled = opt.value !== '' && opt.value === changed.value;
    });
    if (other.value && other.value === changed.value) other.value = '';
  };
  o.addEventListener('change', () => sync(o, d));
  d.addEventListener('change', () => sync(d, o));
  if (o.value) sync(o, d);
})();

/* ---- 3 · swap origin and destination ----------------------------------- */
(function () {
  const btn = document.getElementById('swap');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const o = document.getElementById('origin');
    const d = document.getElementById('dest');
    [...o.options].forEach((x) => (x.disabled = false));
    [...d.options].forEach((x) => (x.disabled = false));
    const t = o.value; o.value = d.value; d.value = t;
    o.dispatchEvent(new Event('change'));
  });
})();

/* ---- 4 · seat picker ---------------------------------------------------
   Click to hold up to `pax` seats; the form posts seat_id[] and the server
   re-checks everything under a row lock. The UI is a convenience, never the
   guarantee. */
(function () {
  const cabin = document.getElementById('cabin');
  if (!cabin) return;
  const max = +cabin.dataset.pax || 1;
  const unit = +cabin.dataset.fare || 0;
  const out = document.getElementById('picked');
  const total = document.getElementById('total');
  const go = document.getElementById('go');
  const hidden = document.getElementById('seatinputs');
  const picked = [];

  const paint = () => {
    hidden.innerHTML = picked
      .map((s) => `<input type="hidden" name="seat_id" value="${s.id}">`)
      .join('');
    out.textContent = picked.length ? picked.map((s) => s.no).join(' · ') : 'none yet';
    total.textContent = '₹' + (picked.length * unit).toLocaleString('en-IN');
    go.disabled = picked.length === 0;
    go.textContent = picked.length
      ? `Pay & issue ${picked.length} ticket${picked.length > 1 ? 's' : ''}`
      : 'Pick a seat';
  };

  cabin.addEventListener('click', (e) => {
    const b = e.target.closest('.seat');
    if (!b || b.disabled) return;
    const id = b.dataset.id;
    const at = picked.findIndex((s) => s.id === id);
    if (at >= 0) {
      picked.splice(at, 1);
      b.classList.remove('is-picked');
    } else {
      if (picked.length >= max) {
        const drop = picked.shift();
        cabin.querySelector(`.seat[data-id="${drop.id}"]`)?.classList.remove('is-picked');
      }
      picked.push({ id, no: b.dataset.no });
      b.classList.add('is-picked');
    }
    paint();
  });
  paint();
})();

/* ---- 5 · flash messages retire themselves ------------------------------ */
(function () {
  const stack = document.getElementById('flashes');
  if (!stack) return;
  setTimeout(() => {
    [...stack.children].forEach((el, i) => {
      setTimeout(() => {
        el.style.transition = 'opacity .4s, transform .4s';
        el.style.opacity = '0';
        el.style.transform = 'translateX(16px)';
        setTimeout(() => el.remove(), 420);
      }, i * 110);
    });
  }, 5200);
})();
