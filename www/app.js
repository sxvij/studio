// Studio page: talks only to /api on the same origin (behind Cloudflare Access). All user and model text goes in with
// textContent; images are same-origin URLs.
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
let CFG = null, current = null, libBefore = 0;

async function api(path, data) {
  const opt = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio': '1' }, body: JSON.stringify(data) };
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error(j.detail || `Request failed (${r.status})`);
  return j;
}
function toast(msg, kind = 'info') {
  const t = $('#toast'); t.textContent = msg; t.className = `toast ${kind}`; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 3800);
}
const ago = s => { const d = Date.now() / 1000 - s; return d < 60 ? 'just now' : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : new Date(s * 1000).toLocaleDateString(); };
const KIND = { generate: 'Generated', edit: 'Edited', outpaint: 'Extended', upscale: 'Upscaled', cutout: 'Cut-out', resize: 'Resized', template: 'Template', upload: 'Uploaded' };

/* ---------- tabs ---------- */
function tab(name) {
  for (const b of document.querySelectorAll('nav button')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const s of document.querySelectorAll('main > section')) s.hidden = s.id !== `tab-${name}`;
  if (name === 'library') loadLibrary(true);
  if (name === 'jobs') loadJobs();
  if (name === 'create') loadRecent();
}
for (const b of document.querySelectorAll('nav button')) b.addEventListener('click', () => tab(b.dataset.tab));

/* ---------- thumbnails ---------- */
function card(it, onClick) {
  const c = el('button', `card${it.alpha ? ' alpha' : ''}`); c.type = 'button';
  const img = el('img'); img.loading = 'lazy'; img.alt = it.prompt || it.title || KIND[it.kind] || ''; img.src = `/api/items/${it.id}/thumb`;
  c.append(img, el('span', 'tag', `${KIND[it.kind] || it.kind} · ${it.w}×${it.h}${it.starred ? ' · ★' : ''}`));
  c.addEventListener('click', () => (onClick || openItem)(it));
  return c;
}
async function loadRecent() {
  const { items } = await api('/api/items?limit=24');
  $('#recent').replaceChildren(...(items.length ? items.map(it => card(it)) : [el('p', 'hint', 'Nothing yet: describe a picture above.')]));
}
async function loadLibrary(reset) {
  if (reset) { libBefore = 0; $('#library').replaceChildren(); }
  const p = new URLSearchParams({ q: $('#q').value, kind: $('#kind').value, starred: $('#starredOnly').checked, limit: 120 });
  if (libBefore) p.set('before', libBefore);
  const { items } = await api(`/api/items?${p}`);
  $('#library').append(...items.map(it => card(it)));
  if (reset && !items.length) $('#library').replaceChildren(el('p', 'hint', 'No pictures match.'));
  libBefore = items.length ? items[items.length - 1].created : libBefore;
  $('#more').hidden = items.length < 120;
}
let qTimer; $('#q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => loadLibrary(true), 300); });
$('#kind').addEventListener('change', () => loadLibrary(true));
$('#starredOnly').addEventListener('change', () => loadLibrary(true));
$('#more').addEventListener('click', () => loadLibrary(false));
$('#uploadBtn').addEventListener('click', () => $('#uploadFile').click());
$('#uploadFile').addEventListener('change', async e => {
  for (const f of e.target.files) {
    const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(f); });
    try { await api('/api/upload', { data, name: f.name }); toast(`Uploaded ${f.name}`, 'ok'); } catch (err) { toast(err.message, 'err'); }
  }
  e.target.value = ''; loadLibrary(true);
});

/* ---------- create ---------- */
$('#genForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements;
  const seed = f.seed.value.trim();
  try {
    const j = await api('/api/generate', { prompt: f.prompt.value, negative: f.negative.value, style: f.style.value, size: f.size.value,
      quality: f.quality.value, n: Number(f.n.value), seed: seed ? Number(seed) : null, override: f.override.checked });
    toast(j.status === 'held' || (j.images > CFG.hold_over && CFG.market_hours && !f.override.checked) ? 'Queued: big batches wait until 4:00 PM ET on market days.' : 'Generating…', 'ok');
    pollSoon();
  } catch (err) { toast(err.message, 'err'); }
});

/* ---------- viewer ---------- */
async function openItem(it) {
  it = await api(`/api/items/${it.id}`);
  current = it;
  const v = $('#viewer');
  $('#vImg').src = `/api/items/${it.id}/image`;
  $('#vTitle').textContent = it.title || KIND[it.kind] || 'Picture';
  const p = it.params || {};
  $('#vMeta').textContent = [`${it.w}×${it.h}`, KIND[it.kind], p.quality, p.style && p.style !== 'none' ? p.style : '', p.seed != null ? `seed ${p.seed}` : '', ago(it.created)].filter(Boolean).join(' · ');
  $('#vPrompt').textContent = it.prompt || '';
  $('#vDownload').href = `/api/items/${it.id}/image?download=true`;
  $('#vStar').textContent = it.starred ? '★ Starred' : '☆ Star';
  $('#vParent').hidden = !it.parent;
  for (const t of document.querySelectorAll('.tool')) t.hidden = true;
  $('#vActions').hidden = false;
  $('#maskCanvas').hidden = true;
  if (!v.open) v.showModal();
}
$('#vClose').addEventListener('click', () => $('#viewer').close());
for (const c of document.querySelectorAll('.tool .cancel')) c.addEventListener('click', () => openItem(current));
function showTool(id) { $('#vActions').hidden = true; for (const t of document.querySelectorAll('.tool')) t.hidden = t.id !== id; }
const queued = msg => { toast(msg, 'ok'); pollSoon(); };

$('#vActions').addEventListener('click', async e => {
  const act = e.target.closest('button')?.dataset.act; if (!act) return;
  const it = current;
  try {
    if (act === 'vary') { await api('/api/generate', { prompt: it.prompt, size: it.params.size || '1024x1024', style: it.params.style || 'none', quality: it.params.quality || 'draft', n: 2 }); queued('Making 2 variations…'); }
    else if (act === 'reuse') { const f = $('#genForm').elements; f.prompt.value = it.prompt; if (it.params.size) f.size.value = it.params.size; if (it.params.style) f.style.value = it.params.style; $('#viewer').close(); tab('create'); f.prompt.focus(); }
    else if (act === 'edit') { showTool('editTool'); startMask(); }
    else if (act === 'extend') showTool('extendTool');
    else if (act === 'resize') showTool('resizeTool');
    else if (act === 'up2' || act === 'up4') { await api('/api/upscale', { item: it.id, scale: act === 'up2' ? 2 : 4 }); queued('Upscaling…'); }
    else if (act === 'cutout') { await api('/api/cutout', { item: it.id }); queued('Removing the background…'); }
    else if (act === 'template') { picks.background = it.alpha ? picks.background : it; if (it.alpha) picks.foreground = it; showPicks(); $('#viewer').close(); tab('templates'); }
    else if (act === 'star') { current = await api(`/api/items/${it.id}`, { starred: !it.starred }); openItem(current); }
    else if (act === 'parent') openItem({ id: it.parent });
    else if (act === 'delete') { if (!confirm('Delete this picture? It cannot be brought back.')) return; await api(`/api/items/${it.id}`, { deleted: true }); $('#viewer').close(); toast('Deleted', 'ok'); refreshVisible(); }
  } catch (err) { toast(err.message, 'err'); }
});

/* mask painting: drawn at the image's real size, shown scaled over it */
let painting = false;
function startMask() {
  const img = $('#vImg'), c = $('#maskCanvas');
  c.width = current.w; c.height = current.h; c.hidden = false;
  c.getContext('2d').clearRect(0, 0, c.width, c.height);
}
function paint(e) {
  if (!painting) return;
  const c = $('#maskCanvas'), r = c.getBoundingClientRect(), ctx = c.getContext('2d');
  const x = (e.clientX - r.left) * c.width / r.width, y = (e.clientY - r.top) * c.height / r.height;
  const size = Number($('#brush').value) * c.width / r.width;
  ctx.globalCompositeOperation = $('#erase').checked ? 'destination-out' : 'source-over';
  ctx.fillStyle = 'rgba(255,60,60,0.55)';
  ctx.beginPath(); ctx.arc(x, y, size / 2, 0, Math.PI * 2); ctx.fill();
}
$('#maskCanvas').addEventListener('pointerdown', e => { painting = true; e.target.setPointerCapture(e.pointerId); paint(e); });
$('#maskCanvas').addEventListener('pointermove', paint);
$('#maskCanvas').addEventListener('pointerup', () => { painting = false; });
$('#maskClear').addEventListener('click', startMask);
function maskPNG() {
  const src = $('#maskCanvas'), out = document.createElement('canvas');
  out.width = src.width; out.height = src.height;
  const o = out.getContext('2d'); o.fillStyle = '#000'; o.fillRect(0, 0, out.width, out.height);
  const d = src.getContext('2d').getImageData(0, 0, src.width, src.height).data, img = o.getImageData(0, 0, out.width, out.height);
  let any = false;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 20) { img.data[i - 3] = img.data[i - 2] = img.data[i - 1] = 255; any = true; }
  o.putImageData(img, 0, 0);
  return any ? out.toDataURL('image/png') : null;
}
$('#editTool').addEventListener('submit', async e => {
  e.preventDefault();
  const mask = maskPNG(); if (!mask) { toast('Paint over the part to change first.', 'err'); return; }
  const f = e.target.elements;
  try { await api('/api/edit', { item: current.id, mask, prompt: f.prompt.value, strength: Number(f.strength.value), n: Number(f.n.value) }); queued('Repainting…'); openItem(current); }
  catch (err) { toast(err.message, 'err'); }
});
$('#extendTool').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements, b = { item: current.id, prompt: f.prompt.value };
  for (const k of ['left', 'right', 'top', 'bottom']) b[k] = Number(f[k].value) || 0;
  try { await api('/api/outpaint', b); queued('Extending…'); openItem(current); } catch (err) { toast(err.message, 'err'); }
});
$('#resizeTool').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements, [fx, fy] = f.focus.value.split(',').map(Number);
  try { const it = await api('/api/resize', { item: current.id, preset: f.preset.value, mode: f.mode.value, bg: f.bg.value, fx, fy }); toast('Done: added to the Library.', 'ok'); openItem(it); refreshVisible(); }
  catch (err) { toast(err.message, 'err'); }
});

/* ---------- templates ---------- */
const picks = { background: null, foreground: null, logo: null };
function showPicks() {
  for (const p of document.querySelectorAll('.pick')) {
    const it = picks[p.dataset.pick];
    p.querySelector('span').textContent = it ? `${KIND[it.kind] || ''} ${it.w}×${it.h}${it.prompt ? ' · ' + it.prompt.slice(0, 40) : ''}` : 'none';
  }
}
for (const p of document.querySelectorAll('.pick')) {
  p.querySelector('button:not(.clear)').addEventListener('click', () => choose(it => { picks[p.dataset.pick] = it; showPicks(); }));
  p.querySelector('.clear').addEventListener('click', () => { picks[p.dataset.pick] = null; showPicks(); });
}
async function choose(cb) {
  const { items } = await api('/api/items?limit=120');
  $('#pickGrid').replaceChildren(...items.map(it => card(it, x => { $('#picker').close(); cb(x); })));
  $('#picker').showModal();
}
$('#pickClose').addEventListener('click', () => $('#picker').close());
$('#tplForm').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target.elements, fields = {};
  for (const k of ['headline', 'subline', 'button', 'accent', 'ink', 'bg', 'font', 'align', 'position']) fields[k] = f[k].value;
  for (const k of ['background', 'foreground', 'logo']) if (picks[k]) fields[k] = picks[k].id;
  try { await api('/api/render', { layout: f.layout.value, preset: f.preset.value, fields }); queued('Rendering…'); } catch (err) { toast(err.message, 'err'); }
});

/* ---------- jobs ---------- */
const STATUS = { queued: 'Waiting', held: 'Held', running: 'Working', done: 'Done', error: 'Failed', cancelled: 'Cancelled' };
const JOBKIND = { generate: 'Generate', edit: 'Edit', outpaint: 'Extend', upscale: 'Upscale', cutout: 'Remove background', render: 'Template' };
async function loadJobs() {
  const { jobs, market_hours } = await api('/api/jobs');
  $('#jobsNote').textContent = market_hours ? `Market hours: batches of more than ${CFG.hold_over} pictures wait until 4:00 PM ET (Tickdeck uses the same GPU). Use "Run now" to override.` : '';
  $('#jobs').replaceChildren(...(jobs.length ? jobs.map(j => {
    const row = el('div', `job ${j.status}`);
    const what = el('div', 'what');
    what.append(el('b', '', `${JOBKIND[j.kind] || j.kind}${j.images ? ` · ${j.done}/${j.images}` : ''}`), el('span', 'hint', ` ${STATUS[j.status] || j.status} · ${ago(j.created)}`));
    if (j.params?.prompt) what.append(el('p', 'prompt', j.params.prompt));
    if (j.error) what.append(el('p', 'err', j.error));
    if (j.note) what.append(el('p', 'hint', j.note));
    row.append(what);
    const btns = el('div', 'row');
    if (j.status === 'held') { const b = el('button', '', 'Run now'); b.onclick = () => api(`/api/jobs/${j.id}`, { action: 'run-now' }).then(loadJobs); btns.append(b); }
    if (j.status === 'queued' || j.status === 'held') { const b = el('button', '', 'Cancel'); b.onclick = () => api(`/api/jobs/${j.id}`, { action: 'cancel' }).then(loadJobs); btns.append(b); }
    row.append(btns);
    if (j.results?.length) { const g = el('div', 'grid small'); for (const id of j.results) g.append(card({ id, kind: '', w: '', h: '' })); row.append(g); }
    return row;
  }) : [el('p', 'hint', 'No jobs yet.')]));
}

/* ---------- polling: while anything is active ---------- */
let pollTimer = null, wasActive = 0;
async function poll() {
  try {
    const { jobs } = await api('/api/jobs?active=true');
    const n = jobs.length;
    $('#jobCount').textContent = n ? String(n) : '';
    $('#status').textContent = n ? (jobs.some(j => j.status === 'running') ? 'Working…' : jobs.every(j => j.status === 'held') ? 'Held until the close' : 'Waiting…') : '';
    if (n !== wasActive || n) refreshVisible();
    wasActive = n;
    pollTimer = setTimeout(poll, n ? 2500 : 15000);
  } catch { pollTimer = setTimeout(poll, 10000); }
}
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 600); }
function refreshVisible() {
  if (!$('#tab-create').hidden) loadRecent();
  if (!$('#tab-jobs').hidden) loadJobs();
  if (!$('#tab-library').hidden && !libBefore) loadLibrary(true);
}

/* ---------- start ---------- */
(async () => {
  CFG = await api('/api/config');
  const opts = (sel, entries) => $(sel).replaceChildren(...entries.map(([v, t]) => { const o = el('option', '', t); o.value = v; return o; }));
  opts('#genForm [name=style]', CFG.styles.map(s => [s, s === 'none' ? 'None' : s === '3d' ? '3D render' : s[0].toUpperCase() + s.slice(1)]));
  $('#genForm [name=style]').value = 'cinematic';
  opts('#genForm [name=size]', Object.entries(CFG.sizes).map(([k, v]) => [k, `${v} (${k.replace('x', '×')})`]));
  const presets = Object.entries(CFG.presets).map(([k, p]) => [k, `${p.label} (${p.w}×${p.h})`]);
  opts('#tplForm [name=preset]', presets); opts('#resizeTool [name=preset]', presets);
  $('#tplForm [name=preset]').value = 'web-hero';
  opts('#tplForm [name=layout]', Object.entries(CFG.layouts));
  $('#genHint').textContent = CFG.market_hours ? `Market hours: batches over ${CFG.hold_over} wait until 4 PM ET.` : '';
  loadRecent(); poll();
})().catch(err => toast(err.message, 'err'));
