"use strict";
/* CANIDÉ — acompanhar.js
   Espectador do site público. Só lê: conecta ao Realtime Database por REST
   streaming (EventSource), recebe o estado compartilhado em "live" e o percurso
   da sessão em "sessions/{id}/track". Nenhum controle do transmissor existe aqui.

   Recuperação: o stream do percurso é reaberto a partir da última chave recebida
   (orderBy $key + startAt), então quem perde a conexão ou entra no meio da sessão
   recebe só o que falta. Pontos repetidos são descartados pela chave.
   Frescor: medido com o relógio monotônico do navegador (performance.now), sem
   depender do relógio de parede de cada dispositivo. */
(() => {
const $ = (id) => document.getElementById(id);
const core = window.CanideSync, ui = window.EcoUI;
const cfg = window.CANIDE_CONFIG || {};
const mono = () => performance.now();
const {format, fmtClock, setText} = ui;

const BASE = String(cfg.databaseURL || '').trim().replace(/\/+$/, '');
const CONFIGURED = /^https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)*\.(firebaseio\.com|firebasedatabase\.app)$/i.test(BASE)
 || (cfg.allowLocal === true && /^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(BASE));
function dbUrl(path, params = {}) {
 const q = new URLSearchParams();
 if (cfg.namespace) q.set('ns', cfg.namespace);
 for (const [k, v] of Object.entries(params)) q.set(k, v);
 const qs = q.toString();
 return `${BASE}/${path}.json${qs ? '?' + qs : ''}`;
}

const S = {
 live: null, liveSig: null, loaded: false, stream: 'connecting', online: navigator.onLine !== false,
 loadedAt: mono(), hbChangedAt: null, lastEventAt: mono(), liveES: null, liveRetry: 0,
 sessionId: null, track: core.createTrack(), trackES: null, trackState: 'idle', trackRetry: 0, trackRetryAt: 0,
 lastPointAt: null, probing: false, lastProbe: 0, fullReloadAt: 0, follow: true, lastState: null
};
const ACTIVE = new Set(['preparing', 'running', 'paused', 'resuming']);

/* ---------- Estado compartilhado: live ---------- */
function parse(e) { try { return JSON.parse(e.data); } catch { return null; } }
function openLive() {
 if (S.liveES) S.liveES.close();
 if (!S.loaded) S.stream = 'connecting';
 const es = new EventSource(dbUrl('live'));
 S.liveES = es;
 const handle = (kind) => (e) => {
  if (es !== S.liveES) return;
  const msg = parse(e); if (!msg) return;
  S.lastEventAt = mono(); S.stream = 'open'; S.liveRetry = 0;
  S.live = core.applyEvent(S.live, kind, msg.path, msg.data);
  onLive();
 };
 es.addEventListener('put', handle('put'));
 es.addEventListener('patch', handle('patch'));
 es.addEventListener('keep-alive', () => { if (es === S.liveES) { S.lastEventAt = mono(); S.stream = 'open'; } });
 const refused = () => { es.close(); S.liveES = null; S.stream = 'error'; showBanner('O banco recusou a leitura do estado. Nova tentativa automática.'); retryLive(); };
 es.addEventListener('cancel', refused);
 es.addEventListener('auth_revoked', refused);
 es.onopen = () => { if (es === S.liveES) { S.stream = 'open'; render(); } };
 es.onerror = () => {
  if (es !== S.liveES) return;
  S.stream = 'error';
  // CLOSED: o navegador desistiu (resposta HTTP de erro). CONNECTING: ele mesmo tenta de novo.
  if (es.readyState === EventSource.CLOSED) { S.liveES = null; retryLive(); }
  render();
 };
}
function retryLive() {
 const delay = Math.min(30000, 1000 * 2 ** S.liveRetry++);
 setTimeout(() => { if (!S.liveES) openLive(); }, delay);
}
function onLive() {
 const sig = JSON.stringify(S.live);
 // Qualquer alteração depois da primeira leitura prova que o transmissor está escrevendo.
 if (S.liveSig !== null && sig !== S.liveSig) S.hbChangedAt = mono();
 if (!S.loaded) { S.loaded = true; S.loadedAt = mono(); }
 S.liveSig = sig;
 const sid = S.live && S.live.sessionId ? S.live.sessionId : null;
 if (sid !== S.sessionId) switchSession(sid);
 else checkMissing();
 render();
}

/* ---------- Percurso da sessão ---------- */
function switchSession(sid) {
 closeTrack();
 S.sessionId = sid; S.track = core.createTrack(); S.lastPointAt = null; S.trackRetry = 0;
 S.trackState = sid ? 'loading' : 'idle';
 drawData(true);
 if (sid) openTrack();
}
function closeTrack() { if (S.trackES) { S.trackES.close(); S.trackES = null; } }
function openTrack(full = false) {
 closeTrack();
 const sid = S.sessionId; if (!sid) return;
 if (full) S.track = core.createTrack();
 const params = S.track.lastKey ? {orderBy: '"$key"', startAt: `"${S.track.lastKey}"`} : {};
 if (!S.track.keys.length) S.trackState = 'loading';
 const es = new EventSource(dbUrl(`sessions/${sid}/track`, params));
 S.trackES = es;
 const handle = (kind) => (e) => {
  if (es !== S.trackES) return;
  const msg = parse(e); if (!msg) return;
  const added = core.mergeTrackEvent(S.track, kind, msg.path, msg.data);
  const first = S.trackState !== 'open';
  S.trackState = 'open'; S.trackRetry = 0; hideBanner();
  if (added && !first) S.lastPointAt = mono();
  if (added || first) scheduleDraw();
  render();
 };
 es.addEventListener('put', handle('put'));
 es.addEventListener('patch', handle('patch'));
 const fail = () => {
  if (es !== S.trackES) return;
  // Fecha para reabrir a partir da última chave recebida (só o que falta).
  es.close(); S.trackES = null;
  S.trackState = S.track.keys.length ? 'retrying' : 'error';
  const delay = Math.min(30000, 1000 * 2 ** S.trackRetry++);
  S.trackRetryAt = mono() + delay;
  if (S.trackState === 'error') showBanner(`Não foi possível recuperar o percurso desta sessão. Nova tentativa em ${Math.round(delay / 1000)} s.`);
  setTimeout(() => { if (S.sessionId === sid && !S.trackES) openTrack(); }, delay);
  render();
 };
 es.addEventListener('cancel', fail);
 es.onerror = fail;
}
/* live.points informa quantos pontos o transmissor já publicou: falta algum, recarrega. */
let missingSince = null;
function checkMissing() {
 const expected = Number(S.live?.points);
 const lack = S.trackState === 'open' ? core.missing(S.track, expected) : 0;
 if (!lack) { missingSince = null; return; }
 missingSince ??= mono();
 if (mono() - missingSince > 8000 && mono() - S.fullReloadAt > 30000) {
  S.fullReloadAt = mono(); missingSince = null;
  openTrack(true);
 }
}

/* ---------- Vigia: distingue transmissor calado de conexão travada ---------- */
async function watchdog() {
 const active = S.live && ACTIVE.has(S.live.status);
 if (!CONFIGURED || !active || S.stream !== 'open' || S.probing) return;
 if (mono() - S.lastEventAt < 12000 || mono() - S.lastProbe < 10000) return;
 S.probing = true; S.lastProbe = mono();
 const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 6000);
 try {
  const r = await fetch(dbUrl('live/heartbeatAt'), {cache: 'no-store', signal: ctl.signal});
  if (!r.ok) throw Error('HTTP ' + r.status);
  const hb = await r.json();
  S.lastEventAt = mono();
  if (hb !== S.live?.heartbeatAt) openLive();   // o banco tem novidade que o stream não entregou
 } catch {
  S.stream = 'error'; openLive();
 } finally { clearTimeout(timer); S.probing = false; render(); }
}

/* ---------- Interface ---------- */
const LAMP = {running: 'ok', preparing: 'wait', resuming: 'wait', syncing: 'wait', paused: 'warn', noSignal: 'warn',
 transmitterDown: 'warn', interrupted: 'err', finished: 'idle', waiting: 'idle', unconfigured: 'idle'};
function showBanner(text) { const b = $('sync-banner'); setText(b, text); b.classList.remove('hidden'); }
function hideBanner() { $('sync-banner').classList.add('hidden'); }
function currentState() {
 return core.deriveState({configured: CONFIGURED, online: S.online, stream: S.stream, loaded: S.loaded, live: S.live,
  now: mono(), hbChangedAt: S.hbChangedAt, loadedAt: S.loadedAt, trackLoading: S.trackState === 'loading' || S.trackState === 'error'});
}
const secs = (ms) => Math.max(0, Math.round(ms / 1000));
function render() {
 if (document.body.classList.contains('is-loading') && (S.loaded || !CONFIGURED || S.stream === 'error')) document.body.classList.remove('is-loading');
 const state = currentState(), info = core.STATES[state], live = S.live || {};
 const panel = $('state-panel');
 panel.dataset.state = state; panel.dataset.tone = info.tone;
 setText($('state-label'), info.label);
 setText($('state-text'), info.text);
 $('state-lamp').setAttribute('data-state', LAMP[state]);
 if (state !== S.lastState) { if (S.lastState !== null) setText($('announcer'), `Estado da trajetória: ${info.label}.`); S.lastState = state; }
 document.body.classList.toggle('is-offline', state === 'interrupted');

 const active = ACTIVE.has(live.status);
 setText($('session-id'), live.sessionId || '—');
 const alive = core.transmitterAlive({now: mono(), hbChangedAt: S.hbChangedAt});
 setText($('tx-state'), !active ? (live.status === 'finished' ? 'Encerrou a sessão' : '—')
  : alive === true ? 'Conectado' : alive === null && mono() - S.loadedAt < core.CONFIRM_WINDOW_MS ? 'Confirmando…'
  : `Sem resposta há ${secs(mono() - (S.hbChangedAt ?? S.loadedAt))} s`);
 const ant = live.antenna || {};
 const antText = {ok: 'Recebendo dados', stale: `Sem dados há ${format(ant.ageS, 0)} s`, none: 'Nenhum pacote recebido',
  disconnected: 'Porta serial desconectada', simulated: 'Simulação de teste'}[ant.state];
 setText($('antenna-state'), active && antText ? antText : '—');
 const device = !S.online ? 'Sem internet' : S.stream === 'open' ? 'Conectado' : S.stream === 'error' ? 'Reconectando…' : 'Conectando…';
 setText($('device-state'), device);
 // Indicador do topo: conexão deste dispositivo (não confundir com a antena).
 if ($('mode-label')) {
  setText($('mode-label'), CONFIGURED ? device : 'Não configurado');
  $('dot').classList.toggle('on', S.online && S.stream === 'open');
  $('dot').classList.toggle('warn', CONFIGURED && !(S.online && S.stream === 'open'));
 }
 const count = S.track.keys.length, expected = Number(live.points);
 setText($('points-count'), S.sessionId ? (Number.isFinite(expected) && expected > count ? `${count} de ${expected}` : String(count)) : '—');
 setText($('started-at'), Number.isFinite(live.startedAt) ? fmtClock(live.startedAt) : '—');

 const last = S.track.keys.length ? S.track.byKey.get(S.track.lastKey) : null;
 const ago = S.lastPointAt !== null ? ' · há ' + secs(mono() - S.lastPointAt) + ' s' : '';
 setText($('last-update'), last ? fmtClock(last.t) + ago : '—');
 const current = core.positionIsCurrent(state);
 $('chart-track').classList.toggle('is-stale', !!last && !current);
 setText($('readings-age'), !last ? 'Nenhum ponto recebido nesta sessão.' : current ? 'Valores do ponto mais recente recebido da antena.'
  : `Última leitura conhecida, de ${fmtClock(last.t)}. Não representa a situação atual.`);
 if (S.trackState === 'retrying') showBanner(`Conexão com o percurso interrompida. Recuperando os pontos que faltam em ${secs(Math.max(0, S.trackRetryAt - mono()))} s.`);
}

/* ---------- Dados: trajetória, leituras e séries ---------- */
const track = new ui.TrackChart($('chart-track'));
const altChart = new ui.LineChart($('chart-altitude'), {unit: 'm', decimals: 1, arrival: true, empty: 'Sem pontos nesta sessão',
 series: [{name: 'Altitude GPS', tone: 'olive'}, {name: 'Altitude barométrica', tone: 'blue'}]});
const tempChart = new ui.LineChart($('chart-temperature'), {unit: '°C', decimals: 2, arrival: true, empty: 'Sem pontos nesta sessão',
 series: [{name: 'Temperatura', tone: 'orange'}]});
const num = (v) => typeof v === 'number' && Number.isFinite(v);
let drawQueued = false, lastDraw = 0;
function scheduleDraw() {
 if (drawQueued) return; drawQueued = true;
 const wait = Math.max(0, 400 - (mono() - lastDraw));
 setTimeout(() => requestAnimationFrame(() => { drawQueued = false; lastDraw = mono(); drawData(false); }), wait);
}
function drawData(reset) {
 const pts = core.points(S.track), fx = core.fixes(pts), last = pts.at(-1), lastFix = fx.at(-1);
 if (S.follow || reset) {
  const gpsAlt = fx.filter(p => num(p.ga)).length >= fx.length / 2;
  const shown = core.decimate(fx, 2500).map(p => ({lat: p.lat, lon: p.lon, t: p.t, alt: num(gpsAlt ? p.ga : p.ba) ? (gpsAlt ? p.ga : p.ba) : NaN}));
  track.update({points: shown, altName: gpsAlt ? 'Altitude GPS' : 'Altitude barométrica'});
  const alts = shown.map(p => p.alt).filter(Number.isFinite);
  $('alt-source').parentElement.classList.toggle('is-off', !alts.length);
  setText($('alt-min'), alts.length ? format(Math.min(...alts), 1) + ' m' : '—');
  setText($('alt-max'), alts.length ? format(Math.max(...alts), 1) + ' m' : '—');
  setText($('alt-source'), alts.length ? (gpsAlt ? 'altitude GPS' : 'altitude barométrica') : 'altitude indisponível');
  const recent = pts.slice(-300), n = recent.length;
  const xTicks = [...new Set(n <= 1 ? [0] : [0, Math.round((n - 1) / 2), n - 1])].filter(i => recent[i]).map(i => ({x: i, label: fmtClock(recent[i].t)}));
  const series = (key, needFix) => recent.map((p, i) => ({x: i, t: p.t, y: num(p[key]) && (!needFix || p.fix === true) ? p[key] : null}));
  const frame = {x0: n === 1 ? -1 : 0, x1: n === 1 ? 1 : n - 1, xTicks, yDomain: null};
  altChart.update({...frame, series: [series('ga', true), series('ba', false)]});
  tempChart.update({...frame, series: [series('tc', false)]});
 }
 setText($('meta-track'), fx.length ? `${fx.length} posições válidas de ${pts.length} pontos${fx.length > 2500 ? ' · desenho simplificado, dados completos' : ''}${S.follow ? '' : ' · vista fixa'}`
  : pts.length ? 'Pontos recebidos sem fix GPS válido; a trajetória aparece com a primeira posição.' : 'O percurso aparece quando a antena enviar posições.');
 setText($('latitude'), lastFix ? format(lastFix.lat, 7) : '—');
 setText($('longitude'), lastFix ? format(lastFix.lon, 7) : '—');
 setText($('gps-altitude'), lastFix && num(lastFix.ga) ? format(lastFix.ga, 1) : '—');
 setText($('distance'), fx.length ? format(core.distance(fx), 1) : '—');
 const hd = core.heading(fx);
 setText($('heading'), hd == null ? '—' : `${format(hd, 0)}`);
 setText($('heading-unit'), hd == null ? '°' : `° ${core.compass(hd)}`);
 const gps = !last ? ['Aguardando', ''] : last.fix === true ? ['Fix válido', 'olive'] : last.fix === false ? ['Sem fix', 'yellow'] : ['Sem dados GPS', ''];
 const tag = $('gps-status'); setText(tag, gps[0]); tag.className = 'tag' + (gps[1] ? ' tag--' + gps[1] : '');
 setText($('temperature'), last && num(last.tc) ? format(last.tc, 2) : '—');
 setText($('pressure'), last && num(last.pa) ? format(last.pa / 100, 2) : '—');
 setText($('humidity'), last && num(last.hu) ? format(last.hu, 1) : '—');
 setText($('altitude'), last && num(last.ba) ? format(last.ba, 1) : '—');
 setText($('satellites'), last && last.fix === true && num(last.sat) ? String(last.sat) : '—');
 setText($('fix-state'), !last || typeof last.fix !== 'boolean' ? '—' : last.fix ? 'Válido' : 'Sem fix');
 // Lâmpada acesa só quando o campo veio no último ponto; ausente fica neutra.
 for (const li of document.querySelectorAll('.readings-list .sys')) { const v = li.querySelector('.sys__value'); li.querySelector('.lamp').setAttribute('data-state', last && !/^—|Não transmitida/.test(v.textContent.trim()) ? 'ok' : 'idle'); }
}

/* ---------- Preferências locais deste navegador ---------- */
function pref(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { /* sem armazenamento */ } return null; }
function applyMask(on) {
 document.body.classList.toggle('mask-coords', on);
 $('mask-coords').setAttribute('aria-pressed', String(on));
 $('mask-coords').lastChild.textContent = on ? 'Mostrar coordenadas' : 'Ocultar coordenadas';
}
applyMask(pref('ecotam.maskCoords') === '1');
$('mask-coords').addEventListener('click', () => { const on = !document.body.classList.contains('mask-coords'); applyMask(on); pref('ecotam.maskCoords', on ? '1' : '0'); drawData(true); });
/* Vista automática: a trajetória se reenquadra a cada ponto. Desligada, a vista fica parada para explorar. */
$('follow').addEventListener('click', () => {
 S.follow = !S.follow; $('follow').setAttribute('aria-pressed', String(S.follow));
 drawData(true);
});

/* ---------- Conexão do dispositivo ---------- */
window.addEventListener('offline', () => { S.online = false; render(); });
window.addEventListener('online', () => { S.online = true; openLive(); if (S.sessionId && !S.trackES) openTrack(); render(); });
document.addEventListener('visibilitychange', () => {
 if (document.visibilityState !== 'visible' || !CONFIGURED) return;
 if (S.stream !== 'open') openLive();
 if (S.sessionId && !S.trackES) openTrack();
});

drawData(true);
if (CONFIGURED) openLive();
render();
setInterval(() => { render(); checkMissing(); watchdog(); }, 1000);
})();
