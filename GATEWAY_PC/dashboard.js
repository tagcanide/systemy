"use strict";
/* CANIDÉ — dashboard.js
   1) Canide: identidade (arquivos oficiais do kit) e moldura do aplicativo — sidebar,
      topbar e barra de abas. Todas as páginas carregam este arquivo primeiro.
   2) EcoUI: núcleo visual compartilhado (formatação e gráficos SVG interativos).
      historico.html também carrega este arquivo para reutilizar os gráficos.
   3) Painel Tempo real: só executa quando <body data-page="live">.
   Cada gráfico monta seu esqueleto SVG uma única vez; a cada polling apenas
   atributos (d, cx, cy, textos) são alterados, sem reconstruir o SVG.
   Toda animação é disparada por dados: primeira série recebida, nova amostra. */
(() => {
const SVG_NS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);
const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
const reduced = () => motionQuery.matches;

/* ---------- Utilitários ---------- */
function svgEl(tag, attrs, parent) {
 const node = document.createElementNS(SVG_NS, tag);
 if (attrs) for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
 if (parent) parent.appendChild(node);
 return node;
}
function setAttr(node, name, value) { const v = String(value); if (node.getAttribute(name) !== v) node.setAttribute(name, v); }
function setAttrs(node, attrs) { for (const k in attrs) setAttr(node, k, attrs[k]); }
function setText(node, text) { const v = String(text); if (node.textContent !== v) node.textContent = v; }
function sync(group, count, tag, attrs) {
 while (group.childElementCount < count) svgEl(tag, attrs, group);
 while (group.childElementCount > count) group.lastElementChild.remove();
 return group.children;
}
const format = (v, decimals = 2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('pt-BR', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
const fmtClock = (ms) => Number.isFinite(ms) ? new Date(ms).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—';
function niceStep(span, count) {
 const raw = Math.max(span, 1e-12) / Math.max(1, count), mag = 10 ** Math.floor(Math.log10(raw)), n = raw / mag;
 return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}
function tickValues(lo, hi, count) {
 const step = niceStep(hi - lo, count), values = [];
 for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9 && values.length < 12; v += step) values.push(Number(v.toPrecision(12)));
 return {values, step};
}
function stepDecimals(step) {
 let d = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
 while (d < 7 && Math.abs(Math.round(step * 10 ** d) - step * 10 ** d) > 1e-6) d++;
 return d;
}
function nearestIndex(points, x) {
 let lo = 0, hi = points.length - 1;
 while (lo < hi) { const mid = (lo + hi) >> 1; if (points[mid].x < x) lo = mid + 1; else hi = mid; }
 return lo > 0 && Math.abs(points[lo - 1].x - x) <= Math.abs(points[lo].x - x) ? lo - 1 : lo;
}
const isMasked = () => document.body.classList.contains('mask-coords');
/* Altitude em três faixas com cores exatas da paleta (o manual não admite
   gradientes nem variações de tom): azul (baixa) · turquesa · amarelo (alta). */
const RAMP = ['#0367A6', '#04ADBF', '#F29F05'];
function rampColor(f) { return RAMP[Math.min(2, Math.floor(Math.max(0, Math.min(1, f)) * 3))]; }
function ping(node, scale = 3.4) {
 if (reduced() || !node.animate) return;
 node.animate([{transform: 'scale(1)', opacity: .85}, {transform: `scale(${scale})`, opacity: 0}], {duration: 950, easing: 'cubic-bezier(.22,1,.36,1)'});
}
function store(key, value) {
 try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { /* armazenamento indisponível */ }
 return null;
}

/* ---------- Identidade CANIDÉ: arquivos oficiais do kit (/assets/brand) ----------
   A arara da marca é o rosto em espaço negativo do D: nada é redesenhado aqui.
   Cada uso aponta para o arquivo oficial previsto no manual para aquele fundo
   e tamanho mínimo (horizontal ≥ 160 px, símbolo ≥ 40 px, compacta ≥ 200 px,
   favicon para 16–32 px). */
// O site público (GitHub Pages) roda num subcaminho e define data-brand-base relativo.
const BRAND = document.documentElement.dataset.brandBase || '/assets/brand/';
const SPECTATOR = document.body.dataset.page === 'spectator';
const BRAND_FILE = {
 logo: 'logos/01-principal-horizontal.svg', // assinatura colorida: só sobre branco
 negative: 'logos/03-negativa-branca.svg',  // branca: campo azul uniforme
 symbol: 'logos/05-simbolo.svg',            // o D com o rosto da arara
 compact: 'logos/06-compacta.svg',          // painel de abertura
 favicon: 'icons/08-favicon.svg'            // microaplicação
};
const brandSrc = (kind) => BRAND + BRAND_FILE[kind];
const BRAND_SIZE = {logo: [941, 209], negative: [941, 209], symbol: [164, 164], compact: [480, 480], favicon: [32, 32]};
const brandImg = (kind, cls, alt = '') => `<img class="${cls}" src="${brandSrc(kind)}" alt="${alt}" width="${BRAND_SIZE[kind][0]}" height="${BRAND_SIZE[kind][1]}" decoding="async" draggable="false" translate="no">`;
function mountBrand(root = document) { for (const el of root.querySelectorAll('[data-brand]')) if (!el.firstElementChild) el.innerHTML = brandImg(el.dataset.brand, 'brand-img'); }

/* Abertura: painel compacto oficial sobre o padrão da marca, uma vez por sessão
   do navegador. Some quando a página fica pronta (primeira resposta do gateway
   no Tempo real), nunca antes de 600 ms nem depois de 2,5 s. */
function splash() {
 let seen = null; try { seen = sessionStorage.getItem('canide.splash'); sessionStorage.setItem('canide.splash', '1'); } catch { /* sem armazenamento: mostra */ }
 if (seen) return;
 const el = document.createElement('div');
 el.className = 'splash';
 el.setAttribute('role', 'status');
 el.innerHTML = `${brandImg('compact', 'splash__mark', 'CANIDÉ')}<p class="splash__msg"><span class="splash__ring" aria-hidden="true"></span>${SPECTATOR ? 'Conectando ao acompanhamento…' : 'Conectando ao gateway local…'}</p>`;
 document.body.appendChild(el);
 const t0 = performance.now();
 let done = false;
 const close = () => {
  if (done) return; done = true;
  setTimeout(() => { el.classList.add('is-leaving'); setTimeout(() => el.remove(), reduced() ? 0 : 420); }, Math.max(0, 600 - (performance.now() - t0)));
 };
 if (!document.body.classList.contains('is-loading')) close();
 else new MutationObserver((_, mo) => { if (!document.body.classList.contains('is-loading')) { mo.disconnect(); close(); } }).observe(document.body, {attributes: true, attributeFilter: ['class']});
 setTimeout(close, 2500);
}

/* ---------- Moldura do aplicativo: sidebar, topbar e barra de abas ----------
   Uma única definição de navegação para as três páginas. "sections" são as
   âncoras de "Nesta página", destacadas conforme a rolagem. */
const ICON = {
 live: '<path d="M1.5 8.5h3l2-5 2.5 9 2-4h3.5"/>',
 history: '<path d="M2.8 8a5.2 5.2 0 1 0 1.6-3.7M2.6 2.4v2.8h2.8M8 5.2V8l2.1 1.4"/>',
 quality: '<path d="M8 1.8 13 3.6v4c0 3-2.2 5.3-5 6.6C5.2 12.9 3 10.6 3 7.6v-4ZM5.8 8l1.6 1.6 3-3.2"/>',
 collapse: '<path d="M10 3.5 5.5 8l4.5 4.5"/>',
 host: '<path d="M2.5 4.5h11v7h-11zM5.5 14h5M8 11.5V14"/>'
};
const icon = (name) => `<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICON[name]}</svg>`;
const NAV = SPECTATOR ? [
 {page: 'spectator', icon: 'live', href: './', label: 'Acompanhar', sections: [['h-state', 'Estado'], ['h-track', 'Trajetória'], ['h-readings', 'Leituras']]}
] : [
 {page: 'live', href: '/', label: 'Tempo real', sections: [['h-traj', 'Trajetória online'], ['h-track', 'Plano de voo'], ['bme-title', 'Atmosfera'], ['gps-title', 'GPS'], ['ops-title', 'Gateway e banco']]},
 {page: 'history', href: '/historico', label: 'Histórico', sections: [['h-filters', 'Explorar registros'], ['h-compare', 'Comparar sessões'], ['h-table', 'Dados consultados']]},
 {page: 'quality', href: '/qualidade', label: 'Qualidade', sections: [['h-filters', 'Auditoria'], ['h-score', 'Integridade'], ['h-sessions', 'Sessões'], ['h-events', 'Eventos']]}
];
function navLinks(page, withTitle) {
 return NAV.map(n => `<li><a href="${n.href}"${n.page === page ? ' aria-current="page"' : ''}${withTitle ? ` title="${n.label}"` : ''}>${icon(n.icon || n.page)}<span>${n.label}</span></a></li>`).join('');
}
function renderShell() {
 const page = document.body.dataset.page, current = NAV.find(n => n.page === page) || NAV[0];
 const side = document.querySelector('[data-shell="sidebar"]'), top = document.querySelector('[data-shell="topbar"]'), tabs = document.querySelector('[data-shell="tabbar"]');
 if (side) side.innerHTML = `<a class="side-brand" href="${NAV[0].href}" aria-label="CANIDÉ, telemetria e rastreamento. Ir para ${NAV[0].label}">${brandImg('negative', 'side-brand__logo')}<span class="side-brand__mark">${brandImg('symbol', 'brand-img')}</span><span class="side-brand__tag">Telemetria &amp; rastreamento</span></a>`
  + `<nav class="side-nav" aria-label="Navegação principal"><p class="side-label">Operação</p><ul>${navLinks(page, true)}</ul></nav>`
  + `<nav class="side-sections" aria-label="Nesta página"><p class="side-label">Nesta página</p><ul>${current.sections.filter(([id]) => $(id)).map(([id, label]) => `<li><a href="#${id}" data-section="${id}">${label}</a></li>`).join('')}</ul></nav>`
  + '<span class="side-gesture" aria-hidden="true"></span>'
  + `<div class="side-foot"><p class="side-host" title="${SPECTATOR ? 'Acompanhamento online' : 'Gateway local'}">${icon('host')}<span>${SPECTATOR ? 'Acompanhamento online' : 'Gateway local · ' + (location.host || '127.0.0.1')}</span></p>`
  + `<button type="button" class="side-collapse" aria-pressed="false">${icon('collapse')}<span>Recolher menu</span></button></div>`;
 if (top) top.innerHTML = `<a class="top-brand" href="${NAV[0].href}" aria-label="CANIDÉ, ir para ${NAV[0].label}">${brandImg('logo', 'top-brand__logo')}</a>`
  + `<p class="top-title"><span class="top-crumb">CANIDÉ</span><span class="top-sep" aria-hidden="true">/</span><strong>${current.label}</strong></p>`
  + `<div class="link-state" title="${SPECTATOR ? 'Estado da conexão deste dispositivo' : 'Estado da ligação com o gateway local'}"><span id="dot" class="dot" aria-hidden="true"></span><span id="mode-label">Conectando…</span></div>`;
 if (tabs) tabs.innerHTML = `<ul>${navLinks(page, false)}</ul>`;
 mountBrand();
 splash();

 // Recolher a sidebar: preferência deste navegador.
 const collapse = side?.querySelector('.side-collapse');
 const applyRail = (on) => { document.body.classList.toggle('side-rail', on); if (collapse) { collapse.setAttribute('aria-pressed', String(on)); collapse.setAttribute('aria-label', on ? 'Expandir menu lateral' : 'Recolher menu lateral'); } };
 applyRail(store('canide.sidebar') === 'rail');
 collapse?.addEventListener('click', () => { const on = !document.body.classList.contains('side-rail'); applyRail(on); store('canide.sidebar', on ? 'rail' : 'full'); });
 requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add('shell-ready')));

 // "Nesta página": destaca a seção visível.
 const links = side ? [...side.querySelectorAll('[data-section]')] : [];
 if (links.length && 'IntersectionObserver' in window) {
  // Observa o bloco que contém cada título; o link continua apontando para o título.
  const blocks = new Map(links.map(a => { const h = $(a.dataset.section); return [h.closest('section, aside') || h, a]; }));
  const visible = new Set();
  const io = new IntersectionObserver(entries => {
   for (const e of entries) e.isIntersecting ? visible.add(blocks.get(e.target)) : visible.delete(blocks.get(e.target));
   const first = links.find(a => visible.has(a));
   if (first) links.forEach(a => a.classList.toggle('is-current', a === first));
  }, {rootMargin: '-15% 0px -55% 0px'});
  for (const block of blocks.keys()) io.observe(block);
 }
 store('canide.visited.' + page, '1');
}

/* Data local do navegador, no subtítulo da página (sem saudação). */
function greeting(now = new Date()) {
 const day = now.toLocaleDateString('pt-BR', {weekday: 'long', day: 'numeric', month: 'long'});
 return day.charAt(0).toUpperCase() + day.slice(1);
}
renderShell();
for (const el of document.querySelectorAll('[data-greeting]')) setText(el, greeting());

/* ---------- Base interativa: tooltip, teclado, redimensionamento ---------- */
let chartSeq = 0;
class BaseChart {
 constructor(svg, options) {
  this.svg = svg;
  this.o = options;
  this.uid = 'eco' + (++chartSeq);
  this.frame = svg.parentElement;
  this.data = null; this.pointer = null; this.cursor = null; this.W = 0; this.H = 0; this.drawn = false;
  this.tip = options.tooltip || this.frame.appendChild(document.createElement('div'));
  this.tip.classList.add('chart-tooltip', 'hidden');
  this.tip.setAttribute('aria-hidden', 'true');
  this.live = this.frame.appendChild(document.createElement('p'));
  this.live.className = 'visually-hidden';
  this.live.setAttribute('aria-live', 'polite');
  svg.setAttribute('tabindex', '0');
  svg.setAttribute('aria-roledescription', 'gráfico interativo');
  svg.setAttribute('aria-keyshortcuts', 'ArrowLeft ArrowRight Home End Escape');
  svg.replaceChildren();
  this.build();
  this.bind();
  this.measure();
  if ('ResizeObserver' in window) new ResizeObserver(() => this.measure()).observe(svg);
  else window.addEventListener('resize', () => this.measure());
 }
 bind() {
  const s = this.svg;
  const move = (e) => {
   const r = s.getBoundingClientRect(); if (!r.width) return;
   this.pointer = {x: (e.clientX - r.left) * (this.W / r.width), y: (e.clientY - r.top) * (this.H / r.height)};
   this.cursor = null; this.inspect(false);
  };
  s.addEventListener('pointermove', move);
  s.addEventListener('pointerdown', move);
  s.addEventListener('pointerleave', () => { this.pointer = null; if (this.cursor === null) this.hideInspect(); });
  s.addEventListener('focus', () => { if (this.pointer === null) { this.cursor = 0; this.inspect(true); } });
  s.addEventListener('blur', () => { this.cursor = null; if (this.pointer === null) this.hideInspect(); });
  s.addEventListener('keydown', (e) => {
   // cursor = distância, em pontos válidos, a partir da leitura mais recente.
   const steps = {ArrowLeft: 1, ArrowRight: -1, PageUp: 10, PageDown: -10};
   if (e.key in steps) this.cursor = Math.max(0, (this.cursor ?? 0) + steps[e.key]);
   else if (e.key === 'Home') this.cursor = Number.MAX_SAFE_INTEGER;
   else if (e.key === 'End') this.cursor = 0;
   else if (e.key === 'Escape') { this.cursor = null; this.hideInspect(); return; }
   else return;
   e.preventDefault(); this.pointer = null; this.inspect(true);
  });
 }
 measure() {
  const r = this.svg.getBoundingClientRect(), W = Math.round(r.width), H = Math.round(r.height);
  if (W < 40 || H < 40 || (W === this.W && H === this.H)) return;
  this.W = W; this.H = H;
  this.svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  this.render();
 }
 update(data) { this.data = data; this.render(); }
 fillTip(head, rows, announce) {
  const nodes = [], spoken = [head];
  const h = document.createElement('div'); h.className = 'tt-head'; h.textContent = head; nodes.push(h);
  for (const row of rows) {
   const line = document.createElement('div'); line.className = 'tt-row tone-' + row.tone;
   const sw = document.createElement('span'); sw.className = 'tt-swatch';
   const name = document.createElement('span'); name.className = 'tt-name'; name.textContent = row.name;
   const value = document.createElement('span'); value.className = 'tt-value'; value.textContent = row.value;
   line.append(sw, name, value);
   if (row.detail) { const det = document.createElement('span'); det.className = 'tt-detail'; det.textContent = row.detail; line.append(det); }
   nodes.push(line); spoken.push(`${row.name}: ${row.value}${row.detail ? ' (' + row.detail + ')' : ''}`);
  }
  this.tip.replaceChildren(...nodes);
  this.tip.classList.remove('hidden');
  if (announce) setText(this.live, spoken.join(' · '));
 }
 placeTip(ax, ay, centerY) {
  const r = this.svg.getBoundingClientRect(), fr = this.frame.getBoundingClientRect(), k = r.width / this.W;
  const px = r.left - fr.left + ax * k, py = r.top - fr.top + ay * k;
  const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
  let x = px + 16; if (x + tw > fr.width) x = px - 16 - tw;
  let y = centerY ? py - th / 2 : py;
  x = Math.max(0, Math.min(x, fr.width - tw)); y = Math.max(0, Math.min(y, fr.height - th));
  this.tip.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
 }
}

/* ---------- Série temporal (uma ou mais séries) + instrumento de fita ---------- */
class LineChart extends BaseChart {
 constructor(svg, options) {
  super(svg, Object.assign({pad: {l: 56, r: 14, t: 24, b: 28}, unit: '', decimals: 2, step: false, area: true,
   yTicks: null, sensitive: false, series: [{name: '', tone: 'blue'}], empty: 'Sem dados', head: null, detail: null,
   valueText: null, tape: null, arrival: false, dots: false}, options));
 }
 build() {
  const s = this.svg;
  this.gGrid = svgEl('g', {class: 'grid'}, s);
  this.gY = svgEl('g', {class: 'tick tick-y'}, s);
  this.gX = svgEl('g', {class: 'tick tick-x'}, s);
  this.unitText = svgEl('text', {class: 'axis-unit', 'text-anchor': 'end'}, s);
  this.cross = svgEl('line', {class: 'crosshair is-off'}, s);
  this.lines = this.o.series.map((spec, i) => {
   const g = svgEl('g', {class: 'series tone-' + spec.tone}, s), gid = `${this.uid}-fill${i}`;
   const grad = svgEl('linearGradient', {id: gid, x1: 0, y1: 0, x2: 0, y2: 1}, svgEl('defs', null, g));
   svgEl('stop', {offset: 0, class: 'area-stop-top'}, grad);
   svgEl('stop', {offset: 1, class: 'area-stop-bottom'}, grad);
   return {spec, g, lastKey: null,
    area: svgEl('path', {class: 'series-area', fill: `url(#${gid})`}, g),
    line: svgEl('path', {class: 'series-line'}, g),
    dots: svgEl('g', null, g),
    pulse: svgEl('circle', {class: 'last-pulse', r: 4}, g),
    last: svgEl('circle', {class: 'last-point', r: 4}, g),
    focus: svgEl('circle', {class: 'focus-point is-off', r: 5}, g)};
  });
  if (this.o.tape) {
   const t = svgEl('g', {class: 'tape tone-' + this.o.series[0].tone}, s);
   this.tape = {g: t,
    track: svgEl('rect', {class: 'tape-track', rx: 3}, t),
    fill: svgEl('rect', {class: 'tape-fill', rx: 3}, t),
    mark: svgEl('path', {class: 'tape-mark', d: 'M0 0l-7-4.5v9z'}, t),
    range: svgEl('text', {class: 'tape-text', 'text-anchor': 'middle'}, t)};
  }
  this.emptyText = svgEl('text', {class: 'empty-msg', 'text-anchor': 'middle'}, s);
  this.hit = svgEl('rect', {class: 'hit'}, s);
 }
 drawGrid(ys, L, R, B) {
  const nodes = sync(this.gGrid, ys.length + 1, 'line');
  [...ys, B].forEach((y, i) => {
   const n = nodes[i], v = y.toFixed(1);
   setAttrs(n, {x1: L, x2: R, y1: v, y2: v, class: i === ys.length ? 'base' : ''});
  });
 }
 render() {
  const W = this.W, H = this.H; if (!W) return;
  const p = this.o.pad, L = p.l, R = W - p.r - (this.o.tape ? 40 : 0), T = p.t, B = H - p.b, d = this.data;
  this.box = {L, R, T, B};
  setAttrs(this.hit, {x: L, y: T, width: Math.max(0, R - L), height: Math.max(0, B - T)});
  const series = d?.series || [];
  const values = series.flat().filter(q => q && q.y !== null).map(q => q.y);
  const empty = !values.length;
  this.svg.classList.toggle('is-empty', empty);
  setAttrs(this.emptyText, {x: ((L + R) / 2).toFixed(1), y: ((T + B) / 2 + 4).toFixed(1)});
  setText(this.emptyText, empty ? (d?.empty || this.o.empty) : '');
  if (this.tape) this.tape.g.classList.toggle('is-off', empty);
  if (empty) { this.drawGrid([0, 1, 2].map(i => T + i * (B - T) / 3), L, R, B); this.scale = null; this.hideInspect(); return; }

  let lo, hi;
  if (d.yDomain) [lo, hi] = d.yDomain;
  else { lo = Math.min(...values); hi = Math.max(...values); const span = Math.max(hi - lo, Math.abs(hi) * 1e-6, 1e-6); lo -= span * .15; hi += span * .15; }
  const x0 = d.x0, x1 = d.x1 > d.x0 ? d.x1 : d.x0 + 1;
  const sx = v => L + (v - x0) / (x1 - x0) * (R - L), sy = v => T + (hi - v) / (hi - lo) * (B - T);
  this.scale = {sx, sy, x0, x1};

  let yt = this.o.yTicks;
  if (!yt) { const {values: tv, step} = tickValues(lo, hi, (B - T) > 120 ? 4 : 3); const dec = stepDecimals(step); yt = tv.map(v => ({v, label: format(v, dec)})); }
  this.drawGrid(yt.map(t => sy(t.v)), L, R, B);
  const yNodes = sync(this.gY, yt.length, 'text', {'text-anchor': 'end'});
  yt.forEach((t, i) => { setAttrs(yNodes[i], {x: L - 8, y: (sy(t.v) + 3.5).toFixed(1)}); setText(yNodes[i], t.label); });
  const xt = d.xTicks || [], xNodes = sync(this.gX, xt.length, 'text');
  xt.forEach((t, i) => {
   const X = sx(t.x);
   setAttrs(xNodes[i], {x: X.toFixed(1), y: B + 18, 'text-anchor': X - L < 30 ? 'start' : R - X < 30 ? 'end' : 'middle'});
   setText(xNodes[i], t.label);
  });
  setAttrs(this.unitText, {x: L - 8, y: T - 10}); setText(this.unitText, d.unit ?? this.o.unit);

  const firstDraw = !this.drawn; this.drawn = true;
  this.lines.forEach((ln, i) => {
   const segs = []; let cur = [];
   for (const q of series[i] || []) { if (q.y === null) { if (cur.length) segs.push(cur); cur = []; } else cur.push(q); }
   if (cur.length) segs.push(cur);
   let dl = '', da = '';
   for (const seg of segs) {
    let path = '';
    seg.forEach((q, k) => { const X = sx(q.x).toFixed(1), Y = sy(q.y).toFixed(1); path += !k ? `M${X} ${Y}` : this.o.step ? `H${X}V${Y}` : `L${X} ${Y}`; });
    dl += path;
    if (this.o.area) da += `${path}V${B}H${sx(seg[0].x).toFixed(1)}Z`;
   }
   setAttr(ln.line, 'd', dl); setAttr(ln.area, 'd', da);
   if (this.o.dots) {
    const valid = segs.flat(), dots = sync(ln.dots, valid.length, 'circle', {class: 'sample-dot', r: 1.6});
    valid.forEach((q, k) => setAttrs(dots[k], {cx: sx(q.x).toFixed(1), cy: sy(q.y).toFixed(1)}));
   }
   const last = segs.length ? segs.at(-1).at(-1) : null;
   ln.g.classList.toggle('is-off', !last);
   if (!last) return;
   const cx = sx(last.x).toFixed(1), cy = sy(last.y).toFixed(1);
   for (const c of [ln.last, ln.pulse]) setAttrs(c, {cx, cy});
   // Animações por dados: curva desenhada na primeira série; pulso a cada amostra nova.
   const key = last.t ?? last.x;
   if (firstDraw && !reduced() && ln.line.animate) {
    ln.line.setAttribute('pathLength', '1'); ln.line.style.strokeDasharray = '1';
    const a = ln.line.animate([{strokeDashoffset: 1}, {strokeDashoffset: 0}], {duration: 1200, easing: 'cubic-bezier(.22,1,.36,1)'});
    a.onfinish = a.oncancel = () => { ln.line.removeAttribute('pathLength'); ln.line.style.strokeDasharray = ''; };
    ln.area.animate([{opacity: 0}, {opacity: 1}], {duration: 900, delay: 300, fill: 'backwards'});
   } else if (this.o.arrival && ln.lastKey !== null && key !== ln.lastKey) ping(ln.pulse);
   ln.lastKey = key;
  });

  if (this.tape) {
   const tp = this.tape, tx = R + 14, w = 8;
   const [tmin, tmax] = this.o.tape.fixed || [Math.min(...values), Math.max(...values)];
   const lastVal = (series[0] || []).filter(q => q.y !== null).at(-1)?.y ?? values.at(-1);
   const f = tmax > tmin ? Math.max(0, Math.min(1, (lastVal - tmin) / (tmax - tmin))) : .5;
   const yv = T + (1 - f) * (B - T);
   setAttrs(tp.track, {x: tx, y: T, width: w, height: B - T});
   setAttrs(tp.fill, {x: tx, y: yv.toFixed(1), width: w, height: (B - yv).toFixed(1)});
   tp.mark.style.transform = `translate(${tx - 3}px, ${yv.toFixed(1)}px)`;
   // Faixa da fita escrita na vertical, ao longo do instrumento (não disputa espaço com o eixo X).
   const dec = this.o.tape.fixed ? 0 : this.o.decimals, rx = tx + w + 12, ry = ((T + B) / 2).toFixed(1);
   setAttrs(tp.range, {x: rx, y: ry, transform: `rotate(-90 ${rx} ${ry})`});
   setText(tp.range, this.o.tape.fixed ? `escala de ${format(tmin, 0)} a ${format(tmax, 0)}` : `mín ${format(tmin, dec)} · máx ${format(tmax, dec)}`);
  }
  if (this.pointer !== null || this.cursor !== null) this.inspect(false);
 }
 valueLabel(y) {
  if (y === null) return 'sem leitura';
  if (this.o.sensitive && isMasked()) return 'oculto';
  if (this.o.valueText) return this.o.valueText(y);
  const unit = this.data.unit ?? this.o.unit;
  return `${format(y, this.data.decimals ?? this.o.decimals)}${unit ? ' ' + unit : ''}`;
 }
 inspect(announce) {
  const d = this.data, sc = this.scale; if (!d || !sc) { this.hideInspect(); return; }
  let target;
  if (this.cursor !== null) {
   const ref = d.series.find(s => s.some(q => q.y !== null)); if (!ref) { this.hideInspect(); return; }
   const valid = ref.filter(q => q.y !== null);
   this.cursor = Math.min(this.cursor, valid.length - 1);
   target = valid[valid.length - 1 - this.cursor].x;
  } else if (this.pointer !== null) {
   const {L, R} = this.box, px = Math.max(L, Math.min(R, this.pointer.x));
   target = sc.x0 + (px - L) / (R - L) * (sc.x1 - sc.x0);
  } else { this.hideInspect(); return; }
  const rows = []; let anchor = null;
  this.lines.forEach((ln, i) => {
   const pts = d.series[i] || [];
   if (!pts.length) { ln.focus.classList.add('is-off'); return; }
   const q = pts[nearestIndex(pts, target)];
   if (q.y !== null) { setAttrs(ln.focus, {cx: sc.sx(q.x).toFixed(1), cy: sc.sy(q.y).toFixed(1)}); ln.focus.classList.remove('is-off'); }
   else ln.focus.classList.add('is-off');
   rows.push({tone: ln.spec.tone, name: ln.spec.name, value: this.valueLabel(q.y), detail: this.o.detail ? this.o.detail(q) : ''});
   if (!anchor) anchor = q;
  });
  if (!anchor) { this.hideInspect(); return; }
  const ax = sc.sx(anchor.x).toFixed(1), {T, B} = this.box;
  setAttrs(this.cross, {x1: ax, x2: ax, y1: T, y2: B});
  this.cross.classList.remove('is-off');
  this.fillTip(this.o.head ? this.o.head(anchor) : fmtClock(anchor.t), rows, announce);
  this.placeTip(Number(ax), T, false);
 }
 hideInspect() {
  this.cross.classList.add('is-off');
  this.lines.forEach(l => l.focus.classList.add('is-off'));
  this.tip.classList.add('hidden');
 }
}

/* ---------- Trajetória XY: metros relativos à primeira posição (T0) ---------- */
class TrackChart extends BaseChart {
 constructor(svg, options = {}) { super(svg, Object.assign({empty: 'Aguardando fix GPS · nenhuma posição válida na janela', emptyShort: 'Aguardando fix GPS'}, options)); }
 build() {
  const s = this.svg;
  this.frameRect = svgEl('rect', {class: 'track-frame', rx: 12}, s);
  this.clipRect = svgEl('rect', {rx: 12}, svgEl('clipPath', {id: this.uid + '-clip'}, svgEl('defs', null, s)));
  const clip = `url(#${this.uid}-clip)`;
  // Estado vazio: varredura indica busca de posição; some com a primeira posição real.
  this.radar = svgEl('g', {class: 'radar', 'clip-path': clip}, s);
  this.radarRings = [1, 2, 3].map(() => svgEl('circle', {class: 'axis-ring'}, this.radar));
  const sweep = svgEl('g', {class: 'radar-sweep'}, this.radar);
  // Símbolo oficial (o D da arara) no centro da varredura: o sistema "procura" a primeira posição válida.
  this.radarBird = svgEl('image', {class: 'radar-bird', href: brandSrc('symbol')}, this.radar);
  this.sweepLine = svgEl('line', null, sweep); this.sweepWedge = svgEl('path', null, sweep); this.sweep = sweep;
  this.gGrid = svgEl('g', {class: 'grid', 'clip-path': clip}, s);
  this.gRings = svgEl('g', {'clip-path': clip}, s);
  this.gRingText = svgEl('g', {class: 'tick', 'clip-path': clip}, s);
  this.compass = svgEl('text', {class: 'compass'}, s); this.compass.textContent = 'N';
  this.scaleText = svgEl('text', null, svgEl('g', {class: 'tick'}, s));
  this.cross = svgEl('g', {class: 'crosshair is-off'}, s);
  this.crossV = svgEl('line', null, this.cross); this.crossH = svgEl('line', null, this.cross);
  this.halo = svgEl('path', {class: 'track-halo'}, s);
  this.gSeg = svgEl('g', null, s);
  this.gDots = svgEl('g', null, s);
  this.start = svgEl('circle', {class: 'track-start', r: 4.5}, s);
  this.startLabel = svgEl('text', {class: 'track-label'}, s); this.startLabel.textContent = 'T0';
  this.reticle = svgEl('g', {class: 'reticle'}, s);
  svgEl('circle', {r: 12}, this.reticle);
  for (const [x1, y1, x2, y2] of [[0, -18, 0, -13], [0, 13, 0, 18], [-18, 0, -13, 0], [13, 0, 18, 0]]) svgEl('line', {x1, y1, x2, y2}, this.reticle);
  const end = svgEl('g', {class: 'series tone-orange'}, s);
  this.ripple = svgEl('circle', {class: 'ripple', r: 8}, end);
  this.last = svgEl('circle', {class: 'last-point', r: 5}, end);
  this.focus = svgEl('circle', {class: 'focus-point is-off', r: 7}, end);
  this.endGroup = end;
  this.emptyText = svgEl('text', {class: 'empty-msg', 'text-anchor': 'middle'}, s);
  this.hit = svgEl('rect', {class: 'hit'}, s);
 }
 render() {
  const W = this.W, H = this.H; if (!W) return;
  const L = 8, R = W - 8, T = 8, B = H - 30; this.box = {L, R, T, B};
  setAttrs(this.frameRect, {x: L, y: T, width: R - L, height: B - T});
  setAttrs(this.clipRect, {x: L, y: T, width: R - L, height: B - T});
  setAttrs(this.hit, {x: L, y: T, width: R - L, height: B - T});
  setAttrs(this.compass, {x: L + 14, y: T + 24});
  setAttrs(this.scaleText, {x: L + 2, y: H - 9});
  const pts = this.data?.points || [];
  const empty = !pts.length;
  this.svg.classList.toggle('is-empty', empty);
  const mx = (L + R) / 2, my = (T + B) / 2;
  setAttrs(this.emptyText, {x: mx.toFixed(1), y: (B - 22).toFixed(1)});
  setText(this.emptyText, empty ? (W < 560 ? this.o.emptyShort : this.o.empty) : '');
  for (const n of [this.halo, this.gSeg, this.gDots, this.start, this.startLabel, this.reticle, this.endGroup, this.gRings, this.gRingText, this.gGrid]) n.classList.toggle('is-off', empty);
  if (empty) {
   const rr = Math.min(R - L, B - T) * .36;
   this.radarRings.forEach((c, i) => setAttrs(c, {cx: mx, cy: my, r: (rr * (i + 1) / 3).toFixed(1)}));
   setAttrs(this.sweepLine, {x1: mx, y1: my, x2: mx, y2: (my - rr).toFixed(1)});
   setAttr(this.sweepWedge, 'd', `M${mx} ${my}L${mx} ${my - rr}A${rr} ${rr} 0 0 0 ${(mx - rr * Math.sin(.6)).toFixed(1)} ${(my - rr * Math.cos(.6)).toFixed(1)}Z`);
   this.sweep.style.transformOrigin = `${mx}px ${my}px`;
   const size = Math.max(40, rr * .5); // símbolo isolado: mínimo 40 px (manual)
   setAttrs(this.radarBird, {x: (mx - size / 2).toFixed(1), y: (my - size / 2).toFixed(1), width: size.toFixed(1), height: size.toFixed(1)});
   setText(this.scaleText, ''); this.proj = []; this.drawn = false; this.lastKey = null; this.hideInspect(); return;
  }

  // Projeção local equiretangular: adequada para deslocamentos de poucos km.
  const lat0 = pts[0].lat, lon0 = pts[0].lon, k = Math.cos(lat0 * Math.PI / 180);
  const m = pts.map(q => ({q, x: (q.lon - lon0) * 111320 * k, y: (q.lat - lat0) * 110574}));
  const xs = m.map(o => o.x), ys = m.map(o => o.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const spanX = Math.max(maxX - minX, 4), spanY = Math.max(maxY - minY, 4);
  const scale = Math.min((R - L - 80) / spanX, (B - T - 80) / spanY);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const px = x => mx + (x - cx) * scale, py = y => my - (y - cy) * scale;
  this.proj = m.map(o => ({...o, px: px(o.x), py: py(o.y)}));
  const first = this.proj[0], last = this.proj.at(-1);

  // Eixos N–S / L–O por T0 e anéis de distância rotulados (escala real em metros).
  const ext = Math.hypot(R - L, B - T) / scale, step = niceStep(Math.max(spanX, spanY) / 1.2, 3);
  const g = sync(this.gGrid, 2, 'line');
  setAttrs(g[0], {x1: first.px.toFixed(1), x2: first.px.toFixed(1), y1: T, y2: B, class: 'base'});
  setAttrs(g[1], {x1: L, x2: R, y1: first.py.toFixed(1), y2: first.py.toFixed(1), class: 'base'});
  const ringCount = Math.max(1, Math.min(8, Math.floor(ext / step)));
  const rings = sync(this.gRings, ringCount, 'circle', {class: 'axis-ring'}), labels = sync(this.gRingText, ringCount, 'text');
  for (let i = 0; i < ringCount; i++) {
   const r = step * (i + 1) * scale;
   setAttrs(rings[i], {cx: first.px.toFixed(1), cy: first.py.toFixed(1), r: r.toFixed(1)});
   setAttrs(labels[i], {x: (first.px + r * .707 + 4).toFixed(1), y: (first.py - r * .707 - 4).toFixed(1)});
   setText(labels[i], `${format(step * (i + 1), stepDecimals(step))} m`);
  }
  let pathLen = 0; for (let i = 1; i < m.length; i++) pathLen += Math.hypot(m[i].x - m[i - 1].x, m[i].y - m[i - 1].y);
  const st = format(step, stepDecimals(step)), ex = `${format(maxX - minX, 1)} × ${format(maxY - minY, 1)} m`, pl = format(pathLen, 1);
  setText(this.scaleText, W < 560 ? `anéis ${st} m · ${ex} · percurso ${pl} m` : `Anéis a cada ${st} m de T0 · extensão ${ex} · percurso ${pl} m`);

  // Trajeto: halo + segmentos coloridos pela altitude real (quando disponível).
  setAttr(this.halo, 'd', this.proj.map((o, i) => `${i ? 'L' : 'M'}${o.px.toFixed(1)} ${o.py.toFixed(1)}`).join(''));
  const alts = pts.map(q => q.alt).filter(Number.isFinite);
  const aMin = alts.length ? Math.min(...alts) : 0, aSpan = alts.length ? Math.max(Math.max(...alts) - aMin, .5) : 1;
  const segs = sync(this.gSeg, Math.max(0, this.proj.length - 1), 'line', {class: 'track-seg'});
  for (let i = 1; i < this.proj.length; i++) {
   const a = this.proj[i - 1], b = this.proj[i], alt = (a.q.alt + b.q.alt) / 2;
   setAttrs(segs[i - 1], {x1: a.px.toFixed(1), y1: a.py.toFixed(1), x2: b.px.toFixed(1), y2: b.py.toFixed(1), stroke: Number.isFinite(alt) ? rampColor((alt - aMin) / aSpan) : RAMP[0]});
  }
  const dots = sync(this.gDots, this.proj.length, 'circle', {class: 'track-dot', r: 1.8});
  this.proj.forEach((o, i) => setAttrs(dots[i], {cx: o.px.toFixed(1), cy: o.py.toFixed(1)}));
  setAttrs(this.start, {cx: first.px.toFixed(1), cy: first.py.toFixed(1)});
  setAttrs(this.startLabel, {x: (first.px + 9).toFixed(1), y: (first.py + 16).toFixed(1)});
  for (const c of [this.last, this.ripple]) setAttrs(c, {cx: last.px.toFixed(1), cy: last.py.toFixed(1)});
  this.reticle.style.transform = `translate(${last.px.toFixed(1)}px, ${last.py.toFixed(1)}px)`;

  // Animação por dados: trajeto revelado em sequência na chegada; ondas a cada nova posição.
  const key = last.q.t;
  if (!this.drawn && !reduced() && this.gSeg.animate) {
   const n = segs.length;
   [...segs].forEach((sg, i) => sg.animate([{opacity: 0}, {opacity: 1}], {duration: 260, delay: i * Math.min(12, 1100 / Math.max(1, n)), fill: 'backwards'}));
   this.halo.animate([{opacity: 0}, {opacity: 1}], {duration: 1200, fill: 'backwards'});
  } else if (this.lastKey !== null && key !== this.lastKey) { ping(this.ripple, 3.2); }
  this.drawn = true; this.lastKey = key;
  if (this.pointer !== null || this.cursor !== null) this.inspect(false);
 }
 inspect(announce) {
  const proj = this.proj || []; if (!proj.length) { this.hideInspect(); return; }
  let idx;
  if (this.cursor !== null) { this.cursor = Math.min(this.cursor, proj.length - 1); idx = proj.length - 1 - this.cursor; }
  else if (this.pointer !== null) {
   let best = Infinity; idx = 0;
   proj.forEach((o, i) => { const dd = (o.px - this.pointer.x) ** 2 + (o.py - this.pointer.y) ** 2; if (dd <= best) { best = dd; idx = i; } });
  } else { this.hideInspect(); return; }
  const o = proj[idx], {L, R, T, B} = this.box, masked = isMasked();
  const X = o.px.toFixed(1), Y = o.py.toFixed(1);
  setAttrs(this.crossV, {x1: X, x2: X, y1: T, y2: B}); setAttrs(this.crossH, {x1: L, x2: R, y1: Y, y2: Y});
  this.cross.classList.remove('is-off');
  setAttrs(this.focus, {cx: X, cy: Y}); this.focus.classList.remove('is-off');
  const rows = [
   {tone: 'teal', name: 'Latitude', value: masked ? 'oculto' : format(o.q.lat, 7) + '°'},
   {tone: 'teal', name: 'Longitude', value: masked ? 'oculto' : format(o.q.lon, 7) + '°'},
   {tone: 'neutral', name: 'Distância de T0', value: format(Math.hypot(o.x, o.y), 1) + ' m'}];
  if (Number.isFinite(o.q.alt)) rows.push({tone: 'orange', name: this.data.altName || 'Altitude', value: format(o.q.alt, 1) + ' m'});
  this.fillTip(`${fmtClock(o.q.t)} · ponto ${idx + 1}/${proj.length}`, rows, announce);
  this.placeTip(o.px, o.py, true);
 }
 hideInspect() { this.cross.classList.add('is-off'); this.focus.classList.add('is-off'); this.tip.classList.add('hidden'); }
}

window.EcoUI = Object.freeze({LineChart, TrackChart, format, fmtClock, niceStep, stepDecimals, setText});
window.Canide = Object.freeze({brandSrc, brandImg, mountBrand, greeting});

/* ======================================================================
   Painel Tempo real
   ====================================================================== */
if (document.body.dataset.page !== 'live') return;

const DEF = [
 {id: 'temperature', key: 'temperature_c', decimal: 2, unit: '°C', name: 'Temperatura', tone: 'orange', tape: {}},
 {id: 'pressure', key: 'pressure_pa', calc: v => v / 100, decimal: 2, unit: 'hPa', name: 'Pressão', tone: 'blue', tape: {}},
 {id: 'humidity', key: 'humidity_pct', decimal: 2, unit: '% UR', name: 'Umidade', tone: 'teal', tape: {fixed: [0, 100]}},
 {id: 'altitude', key: 'altitude_m', decimal: 2, unit: 'm', name: 'Altitude barométrica', tone: 'olive', tape: {}},
 {id: 'latitude', key: 'latitude', decimal: 7, unit: '°', name: 'Latitude', tone: 'teal', fixOnly: true, sensitive: true},
 {id: 'longitude', key: 'longitude', decimal: 7, unit: '°', name: 'Longitude', tone: 'teal', fixOnly: true, sensitive: true},
 {id: 'gps-altitude', key: 'gps_altitude_m', decimal: 2, unit: 'm', name: 'Altitude GPS', tone: 'olive', fixOnly: true},
 {id: 'satellites', key: 'satellites', decimal: 0, unit: 'sat', name: 'Satélites', tone: 'blue', step: true, integer: true},
 {id: 'fix', key: 'gps_fix', decimal: 0, unit: '', name: 'Fix GPS', tone: 'yellow', boolean: true, step: true}
];
let initialized = false, previousBucket = 'SEU_BUCKET', pollBusy = false;
const seen = {};
function isFiniteNum(v) { return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)); }
function sampleValue(s, def) {
 if (!s || (def.fixOnly && s.gps_fix !== true)) return null;
 const v = s[def.key];
 if (def.boolean) return typeof v === 'boolean' ? (v ? 1 : 0) : null;
 if (!isFiniteNum(v)) return null;
 const n = Number(v); return def.calc ? def.calc(n) : n;
}
function domainFor(def, vals) {
 let lo = Math.min(...vals), hi = Math.max(...vals);
 if (def.boolean) return [-.15, 1.15];
 if (def.key === 'humidity_pct') { lo = Math.max(0, lo - 4); hi = Math.min(100, hi + 4); if (hi - lo < 1) { lo -= 1; hi += 1; } return [lo, hi]; }
 if (def.integer) return [Math.max(0, Math.floor(lo) - 1), Math.ceil(hi) + 1];
 const span = Math.max(hi - lo, def.decimal >= 6 ? 0.00001 : 0.1);
 return [lo - span * .2, hi + span * .2];
}
function timeTicks(samples) {
 const n = samples.length; if (!n) return [];
 return [...new Set(n === 1 ? [0] : [0, Math.round((n - 1) / 2), n - 1])].map(i => ({x: i, label: fmtClock(Number(samples[i].timestamp_ms))}));
}
function toneTag(id, text, tone) { const el = $(id); setText(el, text); setAttr(el, 'class', 'tag' + (tone ? ' tag--' + tone : '')); }
function stateClass(el, kind) { el.classList.toggle('state-ok', kind === 'ok'); el.classList.toggle('state-warn', kind === 'warn'); el.classList.toggle('state-err', kind === 'err'); }
/* Lâmpada + texto de um subsistema; estados: ok, warn, err, wait, idle. */
function system(name, state, text) { setAttr($('lamp-' + name), 'data-state', state); if (text !== undefined) { const el = $({gateway: 'gw-state', source: 'src-state', packets: 'packets-state', bme: 'bme-status', gps: 'gps-sys', csv: 'csv-state', db: 'db-state'}[name]); setText(el, text); stateClass(el, state); } }
function boot(name, state) { setAttr($('boot-' + name), 'data-state', state); }
/* Primeiros passos: aparece até ser dispensado; cada etapa reflete o estado real do gateway. */
function guideStep(name, done) { const li = document.querySelector(`#guide [data-step="${name}"]`); if (li) setAttr(li, 'data-state', done ? 'done' : 'todo'); }
if (store('canide.guide') !== 'done') $('guide').classList.remove('hidden');
guideStep('history', store('canide.visited.history') === '1');
$('guide-dismiss').addEventListener('click', () => {
 const g = $('guide'); store('canide.guide', 'done');
 if (reduced() || !g.animate) { g.classList.add('hidden'); return; }
 g.classList.add('is-leaving'); g.addEventListener('animationend', () => g.classList.add('hidden'), {once: true});
});
function announce(key, value, text) { const known = key in seen; if (seen[key] === value) return; seen[key] = value; if (known) setText($('announcer'), text); }
function updateFlux() { $('flux').textContent = `from(bucket: ${JSON.stringify(previousBucket)})\n  |> range(start: -30m)\n  |> filter(fn: (r) => r._measurement == "ecotam_telemetry")\n  |> filter(fn: (r) => contains(value: r._field, set: [\n    "temperature_c", "pressure_pa", "humidity_pct",\n    "altitude_m", "latitude", "longitude",\n    "gps_altitude_m", "satellites", "gps_fix"]))`; }
function message(id, txt, err = false) { const el = $(id); el.textContent = txt; el.classList.toggle('err', err); }
async function post(endpoint, payload) { const r = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}); const out = await r.json(); if (!r.ok) throw Error(out.error || `HTTP ${r.status}`); return out; }
async function busy(button, label, task) { const old = button.textContent; button.disabled = true; button.textContent = label; try { return await task(); } finally { button.disabled = false; button.textContent = old; } }

const charts = {};
for (const def of DEF) {
 charts[def.id] = new LineChart($('chart-' + def.id), {
  unit: def.unit, decimals: def.decimal, step: !!def.step, area: !def.boolean, sensitive: !!def.sensitive,
  pad: {l: def.sensitive ? 92 : def.boolean ? 66 : 58, r: 14, t: def.boolean ? 14 : 24, b: 28},
  series: [{name: def.name, tone: def.tone}], tape: def.tape || null, arrival: true, dots: !!def.tape,
  yTicks: def.boolean ? [{v: 0, label: 'Sem fix'}, {v: 1, label: 'Fix'}] : null,
  valueText: def.boolean ? (v => v ? 'Fix válido' : 'Sem fix') : null,
  empty: 'Sem dados válidos nesta janela'
 });
}
const track = new TrackChart($('chart-track'));

/* Chegada de amostras nos últimos 60 s: um traço por amostra real; faixas marcam intervalos > 2,5 s. */
const activity = $('packet-activity');
activity.setAttribute('preserveAspectRatio', 'none');
svgEl('line', {class: 'baseline', x1: 0, x2: 320, y1: 55, y2: 55, 'vector-effect': 'non-scaling-stroke'}, activity);
const actGaps = svgEl('g', null, activity), actTicks = svgEl('g', null, activity);
function drawActivity(all, now) {
 const recent = all.map(s => Number(s.timestamp_ms)).filter(t => Number.isFinite(t) && now - t <= 60000).sort((a, b) => a - b);
 const x = t => (320 * (1 - (now - t) / 60000)).toFixed(1);
 const ticks = sync(actTicks, recent.length, 'line', {class: 'tick-mark', y1: 10, y2: 52, 'vector-effect': 'non-scaling-stroke'});
 recent.forEach((t, i) => { setAttrs(ticks[i], {x1: x(t), x2: x(t)}); ticks[i].classList.toggle('is-new', i === recent.length - 1 && now - t < 2000); });
 const gaps = [];
 for (let i = 1; i < recent.length; i++) if (recent[i] - recent[i - 1] > 2500) gaps.push([recent[i - 1], recent[i]]);
 if (recent.length && now - recent.at(-1) > 2500) gaps.push([recent.at(-1), now]);
 const bands = sync(actGaps, gaps.length, 'rect', {class: 'gap-band', y: 0, height: 56});
 gaps.forEach(([a, b], i) => setAttrs(bands[i], {x: x(a), width: Math.max(0, x(b) - x(a)).toFixed(1)}));
 return recent.length;
}

function setOnline(online, err) {
 document.body.classList.remove('is-loading');
 document.body.classList.toggle('is-offline', !online);
 $('offline-banner').classList.toggle('hidden', online);
 announce('online', online, online ? 'Gateway reconectado.' : 'Gateway offline.');
 if (!online) {
  setText($('mode-label'), 'Gateway desconectado');
  $('dot').classList.remove('on', 'warn');
  system('gateway', 'err', 'Offline'); boot('link', 'err');
  for (const n of ['source', 'packets', 'bme', 'gps', 'csv', 'db']) setAttr($('lamp-' + n), 'data-state', 'idle');
  $('error-status').textContent = String(err);
 }
}

async function refresh() {
 if (pollBusy) return; pollBusy = true;
 try {
  const resp = await fetch('/api/status', {cache: 'no-store'}); if (!resp.ok) throw Error(`HTTP ${resp.status}`);
  const state = await resp.json();
  setOnline(true);
  if (!initialized) { $('source-mode').value = state.source; $('source-port').value = state.port || 'COM3'; initialized = true; }
  const expectedSource = state.source === 'rx' ? 'rx_serial' : 'sim';
  const fromSource = state.samples.filter(s => s.source === expectedSource);
  const samples = fromSource.slice(-Number($('window-size').value || 120));
  const last = samples.at(-1), age = last ? Math.max(0, (Date.now() - Number(last.timestamp_ms)) / 1000) : null;
  const fresh = age !== null && age < 8;
  $('dot').classList.toggle('on', fresh); $('dot').classList.toggle('warn', !fresh);
  setText($('mode-label'), state.source === 'rx' ? 'RX • ' + state.port : 'SIMULAÇÃO');
  setText($('session-state'), state.session_id || '—');
  system('gateway', 'ok', 'Online · ' + location.host); boot('link', 'ok');
  const srcState = state.rx_error ? 'err' : state.source === 'rx' ? (state.rx_port_open ? 'ok' : 'warn') : 'warn';
  system('source', srcState, state.source === 'rx' ? `ESP32 RX · ${state.port}` : 'Simulador · dados artificiais'); boot('source', srcState);
  setText($('fresh-label'), fresh ? 'Atualizando em tempo real' : age !== null ? `Sem novas amostras há ${Math.round(age)} s` : 'Aguardando primeiras amostras');
  stateClass($('fresh-label'), fresh ? 'ok' : age !== null ? 'warn' : '');
  announce('fresh', fresh, fresh ? 'Recebendo amostras em tempo real.' : 'Sem novas amostras.');
  const perMin = drawActivity(fromSource, Date.now());
  setText($('packet-rate'), `${perMin} amostras/min`);
  system('packets', fresh ? 'ok' : age !== null ? 'warn' : 'wait', fresh ? `${perMin}/min · ${format(age, 1)} s` : age !== null ? `parado há ${Math.round(age)} s` : 'aguardando');
  boot('telemetry', fresh ? 'ok' : age !== null ? 'warn' : 'wait');
  guideStep('source', fresh);
  setText($('last-sample-time'), last ? new Date(last.timestamp_ms).toLocaleTimeString('pt-BR') : '—');
  const version = last?.protocol_version;
  setText($('protocol-label'), version === 3 ? 'V3 / 84 B' : version === 2 ? 'V2 / 64 B' : state.source === 'sim' ? 'Simulador' : 'Aguardando');
  const v3 = version === 3;
  const bme = last && isFiniteNum(last.temperature_c) ? (fresh ? 'Recebendo' : 'Sem dados recentes') : 'Aguardando';
  system('bme', bme === 'Recebendo' ? 'ok' : bme === 'Aguardando' ? 'idle' : 'warn', bme);
  const gps = v3 ? (last?.gps_fix === true ? 'Fix válido' : last?.gps_fix === false ? 'Sem fix' : 'Aguardando') : 'Requer V3';
  toneTag('gps-status', gps, gps === 'Fix válido' ? 'olive' : gps === 'Sem fix' ? 'yellow' : '');
  const gpsState = gps === 'Fix válido' ? 'ok' : gps === 'Sem fix' ? 'warn' : 'idle';
  system('gps', gpsState, gps === 'Fix válido' && isFiniteNum(last.satellites) ? `Fix · ${last.satellites} sat` : gps); boot('gps', gpsState);
  if (v3) announce('fix', gps, `GPS: ${gps}.`);
  setText($('fix-state'), v3 && typeof last?.gps_fix === 'boolean' ? (last.gps_fix ? 'Válido' : 'Sem fix') : '—');
  system('csv', state.csv_error ? 'err' : state.count ? 'ok' : 'idle', state.csv_error ? 'Falha ao gravar' : state.count ? 'Gravando em dados/' : 'Pronto');
  const dbState = state.influx_error ? 'err' : state.influx_configured ? 'ok' : 'idle';
  system('db', dbState, state.influx_configured ? state.influx_status : 'Não configurado');
  boot('storage', state.csv_error ? 'err' : state.influx_error ? 'warn' : 'ok');
  guideStep('influx', !!state.influx_configured && !state.influx_error);

  const n = samples.length, xTicks = timeTicks(samples), masked = isMasked();
  for (const def of DEF) {
   const v = last ? sampleValue(last, def) : null;
   if (def.id !== 'fix') setText($(def.id), format(v, def.decimal));
   const pts = samples.map((s, i) => ({x: i, y: sampleValue(s, def), t: Number(s.timestamp_ms)}));
   const vals = pts.filter(q => q.y !== null).map(q => q.y);
   charts[def.id].update({series: [pts], x0: n === 1 ? -1 : 0, x1: n === 1 ? 1 : n - 1, xTicks, yDomain: vals.length ? domainFor(def, vals) : null});
   const meta = $('meta-' + def.id);
   if (!vals.length) { setText(meta, def.fixOnly ? 'Aguardando pacotes V3 com fix GPS válido.' : 'Aguardando pacotes com este campo.'); continue; }
   const lastVal = def.sensitive && masked ? 'oculto' : def.boolean ? (vals.at(-1) ? 'fix válido' : 'sem fix') : `${format(vals.at(-1), def.decimal)} ${def.unit}`.trim();
   const spread = def.tape ? ` · Δ janela ${format(Math.max(...vals) - Math.min(...vals), def.decimal)} ${def.unit}` : '';
   setText(meta, `${vals.length}/${n} amostras válidas · último: ${lastVal}${spread}`);
   setAttr($('chart-' + def.id), 'aria-label', `${def.name}: ${vals.length} de ${n} amostras válidas, último valor ${lastVal}. Use as setas para inspecionar.`);
  }

  const fixed = samples.filter(s => s.gps_fix === true && isFiniteNum(s.latitude) && isFiniteNum(s.longitude));
  // Gradação de altitude: GPS quando presente na maioria das posições; senão barométrica.
  const gpsAlt = fixed.filter(s => isFiniteNum(s.gps_altitude_m)).length >= fixed.length / 2;
  const altKey = gpsAlt ? 'gps_altitude_m' : 'altitude_m';
  const points = fixed.map(s => ({lat: Number(s.latitude), lon: Number(s.longitude), t: Number(s.timestamp_ms), alt: isFiniteNum(s[altKey]) ? Number(s[altKey]) : NaN}));
  track.update({points, altName: gpsAlt ? 'Altitude GPS' : 'Altitude barométrica'});
  const alts = points.map(p => p.alt).filter(Number.isFinite);
  $('alt-source').parentElement.classList.toggle('is-off', !alts.length);
  setText($('alt-min'), alts.length ? format(Math.min(...alts), 1) + ' m' : '—');
  setText($('alt-max'), alts.length ? format(Math.max(...alts), 1) + ' m' : '—');
  setText($('alt-source'), alts.length ? (gpsAlt ? 'altitude GPS' : 'altitude barométrica') : 'altitude indisponível');
  setText($('meta-track'), points.length ? `${points.length} posições válidas na janela · variações pequenas podem ser ruído do receptor` : 'A trajetória aparece com fix GPS válido em pacotes V3.');

  setText($('samples-total'), state.count.toLocaleString('pt-BR'));
  setText($('rx-frames'), state.rx_frames.toLocaleString('pt-BR'));
  setText($('queue-drops'), state.queue_drops.toLocaleString('pt-BR'));
  setText($('influx-ok'), state.influx_writes.toLocaleString('pt-BR'));
  setText($('influx-fail'), state.influx_failed.toLocaleString('pt-BR'));
  setText($('last-write'), state.last_write || '—');
  setText($('influx-status'), state.influx_status);
  setText($('error-status'), [state.csv_error, state.rx_error, state.influx_error].filter(Boolean).join(' | '));
  setText($('rx-live'), state.source === 'rx' ? `${state.port} · ${state.rx_lines} linhas · ${state.rx_headers} cabeçalhos · ${state.rx_frames} frames · ${state.rx_last_age_s == null ? 'nenhum frame recebido' : `${state.rx_last_age_s} s desde o último frame`}` : 'Simulador selecionado: dados artificiais. Mude para RX/COM3 para observar os sensores.');
  if (state.influx_bucket && state.influx_bucket !== previousBucket) { previousBucket = state.influx_bucket; updateFlux(); }
  renderSync(state.sync || {}, state.firebase_public || {}, state.source);
 } catch (err) { setOnline(false, err); }
 finally { pollBusy = false; }
}

/* Transmissão online: estado da trajetória e da fila de envio ao Firebase. */
const TRAJ = {preparing: ['Preparando', 'blue'], running: ['Em andamento', 'olive'], paused: ['Pausada', 'yellow'], resuming: ['Retomando', 'blue'], finished: ['Finalizada', 'neutral']};
const LINK = {off: ['Desligada · dados só no computador', ''], ok: ['Sincronizado', 'ok'], retrying: ['Sem conexão · reenviando', 'warn'], denied: ['Escrita negada', 'err']};
let syncPrefilled = false;
function renderSync(sync, pub, source) {
 if (!syncPrefilled) {
  syncPrefilled = true;
  if (pub.databaseURL && !$('sync-db').value) $('sync-db').value = pub.databaseURL;
  if (pub.apiKey && !$('sync-key').value) $('sync-key').value = pub.apiKey;
 }
 const t = sync.trajectory, status = t ? t.status : null;
 const [label, tone] = status ? TRAJ[status] : ['Sem trajetória', ''];
 toneTag('traj-tag', label, tone);
 const open = !!t && status !== 'finished';
 $('traj-start').disabled = open;
 $('traj-pause').disabled = !open || status === 'paused';
 $('traj-resume').disabled = status !== 'paused';
 $('traj-finish').disabled = !open;
 $('traj-allow-sim').closest('.check').classList.toggle('is-off', source !== 'sim');
 setText($('traj-id'), t ? t.id : '—');
 setText($('traj-points'), t ? `${Number(sync.sent_points || 0).toLocaleString('pt-BR')} enviados · ${Number(t.points || 0).toLocaleString('pt-BR')} na sessão` : '—');
 setText($('traj-pending'), `${Number(sync.pending || 0).toLocaleString('pt-BR')} operações${sync.dropped ? ` · ${sync.dropped} descartadas` : ''}`);
 const [linkText, linkState] = LINK[sync.link] || LINK.off;
 setText($('traj-link'), sync.configured ? linkText : 'Transmissor não conectado');
 stateClass($('traj-link'), sync.configured ? linkState : '');
 const site = $('traj-site');
 if (pub.publicSiteUrl) { setAttr(site, 'href', pub.publicSiteUrl); setText(site, pub.publicSiteUrl.replace(/^https:\/\//, '')); site.target = '_blank'; site.rel = 'noopener'; }
 setText($('traj-error'), sync.error || '');
 // Transmissor conectado (ou reconectando com a conexão lembrada): o formulário dá lugar ao estado.
 const linked = !!sync.configured && (sync.authorized === true || !!sync.remembered);
 $('sync-form').classList.toggle('hidden', linked);
 $('sync-connected').classList.toggle('hidden', !linked);
 setText($('sync-account'), sync.email || '—');
 setText($('sync-auto'), sync.remembered ? (sync.authorized ? 'Reconecta sozinho ao iniciar o gateway.' : 'Reconectando automaticamente…') : 'Conexão só até fechar o gateway.');
 if (sync.remembered && sync.authorized) setText($('sync-auto'), sync.protected
  ? 'Credencial cifrada com DPAPI e presa a este computador. Reconecta sozinho ao iniciar.'
  : 'Credencial de transporte em transmissor.local.json: leve a pasta ao computador da antena. Lá ela é cifrada no primeiro início.');
}
async function trajectory(action, button) {
 if (action === 'finish' && !window.confirm('Encerrar a trajetória para todos os espectadores? A sessão não pode ser reaberta.')) return;
 message('traj-message', '');
 try {
  const out = await busy(button, button.textContent, () => post('/api/trajectory', {action, allowSim: $('traj-allow-sim').checked}));
  message('traj-message', out.message); await refresh();
 } catch (err) { message('traj-message', String(err.message || err), true); }
}
for (const b of document.querySelectorAll('.traj-actions [data-action]')) b.addEventListener('click', () => trajectory(b.dataset.action, b));
$('sync-form').addEventListener('submit', async e => {
 e.preventDefault(); message('sync-message', 'Autenticando o transmissor…');
 try {
  const out = await busy($('sync-connect'), 'Conectando…', () => post('/api/sync/connect', {databaseURL: $('sync-db').value, apiKey: $('sync-key').value, email: $('sync-email').value, password: $('sync-password').value, remember: $('sync-remember').checked}));
  $('sync-password').value = ''; message('sync-message', out.message);
 } catch (err) { message('sync-message', String(err.message || err), true); }
});
$('sync-disconnect').addEventListener('click', async () => { try { const out = await busy($('sync-disconnect'), 'Desligando…', () => post('/api/sync/disconnect', {})); message('sync-message', out.message); } catch (err) { message('sync-message', String(err.message || err), true); } });

/* Relógio de missão: apenas o relógio do navegador, sem comunicação com o gateway. */
function tickClock() {
 const now = new Date();
 setText($('clock-local'), now.toLocaleTimeString('pt-BR'));
 setText($('clock-utc'), now.toISOString().slice(11, 19) + 'Z');
}

/* Ocultar coordenadas: preferência local deste navegador, para capturas de tela. */
function applyMask(on) {
 document.body.classList.toggle('mask-coords', on);
 $('mask-coords').setAttribute('aria-pressed', String(on));
 $('mask-coords').lastChild.textContent = on ? 'Mostrar coordenadas' : 'Ocultar coordenadas';
}
applyMask(store('ecotam.maskCoords') === '1');
$('mask-coords').addEventListener('click', () => {
 const on = !isMasked(); applyMask(on);
 store('ecotam.maskCoords', on ? '1' : '0');
 refresh();
});

$('influx-form').addEventListener('submit', async e => {
 e.preventDefault(); message('connect-message', 'Conectando ao InfluxDB…');
 try {
  const out = await busy($('connect-button'), 'Conectando…', () => post('/api/connect', {url: $('influx-url').value, org: $('influx-org').value, bucket: $('influx-bucket').value, token: $('influx-token').value}));
  $('influx-token').value = ''; previousBucket = $('influx-bucket').value; updateFlux(); message('connect-message', out.message);
 } catch (err) { message('connect-message', String(err), true); }
});
$('disconnect-button').addEventListener('click', async () => { try { const out = await busy($('disconnect-button'), 'Desconectando…', () => post('/api/disconnect', {})); message('connect-message', out.message); } catch (err) { message('connect-message', String(err), true); } });
$('source-apply').addEventListener('click', async () => {
 message('source-message', 'Aplicando fonte…');
 try { const out = await busy($('source-apply'), 'Aplicando…', () => post('/api/source', {mode: $('source-mode').value, port: $('source-port').value})); message('source-message', out.message); await refresh(); }
 catch (err) { message('source-message', String(err), true); }
});
$('copy-flux').addEventListener('click', () => { navigator.clipboard.writeText($('flux').textContent).then(() => message('connect-message', 'Consulta Flux copiada.')).catch(() => message('connect-message', 'Selecione e copie a consulta.', true)); });
$('window-size').addEventListener('change', refresh);
updateFlux(); tickClock(); setInterval(tickClock, 1000);
refresh(); setInterval(refresh, 1000);
})();
