/* CANIDÉ — sync-core.js
   Lógica pura do acompanhamento online, sem DOM e sem rede: aplicação de eventos
   do Realtime Database (REST streaming), trajetória ordenada sem duplicatas,
   derivação do estado mostrado ao espectador e grandezas calculadas da trajetória.
   Usado pelo site (window.CanideSync) e pelos testes em Node (require). */
(function (root) {
'use strict';

const TRANSMITTER_STALE_MS = 15000;  // três batimentos de 5 s sem novidade
const CONFIRM_WINDOW_MS = 12000;     // tempo para confirmar um transmissor ao entrar

/* Aplica um evento "put" ou "patch" do REST streaming a um objeto JSON. */
function applyEvent(doc, kind, path, data) {
  const parts = String(path || '/').split('/').filter(Boolean);
  if (!parts.length) {
    if (kind === 'patch') return Object.assign(isObj(doc) ? {...doc} : {}, data || {});
    return data === undefined ? null : data;
  }
  const out = isObj(doc) ? {...doc} : {};
  let node = out;
  for (let i = 0; i < parts.length - 1; i++) {
    node[parts[i]] = isObj(node[parts[i]]) ? {...node[parts[i]]} : {};
    node = node[parts[i]];
  }
  const last = parts.at(-1);
  if (kind === 'patch') node[last] = Object.assign(isObj(node[last]) ? {...node[last]} : {}, data || {});
  else if (data === null) delete node[last];
  else node[last] = data;
  return out;
}
function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

const KEY_RX = /^[0-9]{13}_[0-9]{6}$/;

/* Trajetória: chaves "tempo_contador" ordenam cronologicamente. */
function createTrack() {
  return {keys: [], byKey: new Map(), duplicates: 0, outOfOrder: 0, lastKey: ''};
}
function addPoint(track, key, point) {
  if (!KEY_RX.test(key) || !isObj(point) || !Number.isFinite(point.t)) return false;
  if (track.byKey.has(key)) { track.duplicates++; return false; }
  track.byKey.set(key, point);
  if (!track.keys.length || key > track.keys.at(-1)) track.keys.push(key);
  else {
    track.outOfOrder++;
    let lo = 0, hi = track.keys.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (track.keys[mid] < key) lo = mid + 1; else hi = mid; }
    track.keys.splice(lo, 0, key);
  }
  track.lastKey = track.keys.at(-1);
  return true;
}
/* Evento do stream de sessions/{id}/track. Retorna quantos pontos novos entraram. */
function mergeTrackEvent(track, kind, path, data) {
  const parts = String(path || '/').split('/').filter(Boolean);
  let added = 0;
  if (!parts.length) {
    if (isObj(data)) for (const [k, v] of Object.entries(data)) added += addPoint(track, k, v) ? 1 : 0;
  } else if (parts.length === 1 && isObj(data)) {
    added += addPoint(track, parts[0], data) ? 1 : 0;
  }
  return added;
}
function points(track) { return track.keys.map(k => track.byKey.get(k)); }
/* Pontos esperados (live.points) que ainda não chegaram: lacuna a recuperar. */
function missing(track, expected) {
  return Number.isFinite(expected) ? Math.max(0, expected - track.keys.length) : 0;
}
/* Números de ponto (n) ausentes entre os recebidos. */
function gaps(track) {
  let count = 0, prev = 0;
  for (const k of track.keys) { const n = track.byKey.get(k).n; if (Number.isFinite(n)) { if (prev && n > prev + 1) count += n - prev - 1; prev = n; } }
  return count;
}

/* Estado mostrado ao espectador. Entrada:
   {configured, online, stream: 'connecting'|'open'|'error', loaded, live, now,
    hbChangedAt, loadedAt, trackLoading}
   "now", "hbChangedAt" e "loadedAt" vêm do relógio monotônico do navegador
   (performance.now): nada depende do relógio de parede do dispositivo. */
const STATES = {
  unconfigured: {label: 'Sincronização não configurada', tone: 'neutral', text: 'O endereço do banco em tempo real ainda não foi definido neste site.'},
  waiting: {label: 'Aguardando início', tone: 'neutral', text: 'Nenhuma trajetória ativa. O acompanhamento começa sozinho quando o operador iniciar.'},
  preparing: {label: 'Preparando transmissão', tone: 'blue', text: 'O operador iniciou a trajetória. Aguardando o primeiro ponto da antena.'},
  running: {label: 'Em andamento', tone: 'olive', text: 'Recebendo a posição oficial transmitida pelo computador da antena.'},
  paused: {label: 'Pausada', tone: 'yellow', text: 'O operador pausou a trajetória. Novos pontos não são publicados até a retomada.'},
  resuming: {label: 'Retomando', tone: 'blue', text: 'Trajetória retomada. Aguardando o próximo ponto da antena.'},
  finished: {label: 'Finalizada', tone: 'neutral', text: 'A trajetória foi encerrada. O percurso completo continua na tela.'},
  noSignal: {label: 'Sem sinal da antena', tone: 'orange', text: 'O transmissor está conectado, mas a antena parou de enviar dados. A posição mostrada é a última conhecida.'},
  transmitterDown: {label: 'Transmissor desconectado', tone: 'orange', text: 'O computador da antena não confirma presença. A posição mostrada é a última conhecida.'},
  syncing: {label: 'Sincronizando', tone: 'blue', text: 'Recuperando o estado e o percurso da trajetória.'},
  interrupted: {label: 'Conexão interrompida', tone: 'danger', text: 'Este dispositivo perdeu a conexão. Os últimos dados continuam na tela; nova tentativa automática.'}
};
const ACTIVE = new Set(['preparing', 'running', 'paused', 'resuming']);

function transmitterAlive(input) {
  if (input.hbChangedAt == null) return null;  // ainda sem batimento novo observado
  return input.now - input.hbChangedAt < TRANSMITTER_STALE_MS;
}
function deriveState(input) {
  if (!input.configured) return 'unconfigured';
  if (input.online === false || input.stream === 'error') return 'interrupted';
  if (!input.loaded) return 'syncing';
  const live = input.live;
  if (!isObj(live) || !live.sessionId || !live.status) return 'waiting';
  if (input.trackLoading) return 'syncing';      // percurso anterior ainda sendo recuperado
  if (live.status === 'finished') return 'finished';
  if (!ACTIVE.has(live.status)) return 'waiting';
  const alive = transmitterAlive(input);
  if (alive === null) return input.now - input.loadedAt < CONFIRM_WINDOW_MS ? 'syncing' : 'transmitterDown';
  if (!alive) return 'transmitterDown';
  if (live.status === 'paused') return 'paused';
  const antenna = live.antenna?.state;
  const antennaDown = antenna && antenna !== 'ok' && antenna !== 'simulated';
  if (live.status === 'preparing') return antennaDown ? 'noSignal' : 'preparing';
  if (live.status === 'resuming') return antennaDown ? 'noSignal' : 'resuming';
  return antennaDown ? 'noSignal' : 'running';
}
/* A posição só é "atual" quando a trajetória corre e a antena e o transmissor estão vivos. */
function positionIsCurrent(state) { return state === 'running'; }

/* ---- grandezas calculadas a partir das posições reais ---- */
const R_EARTH = 6371008.8;
const rad = d => d * Math.PI / 180;
function haversine(a, b) {
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R_EARTH * Math.asin(Math.min(1, Math.sqrt(h)));
}
function fixes(pts) { return pts.filter(p => p.fix === true && Number.isFinite(p.lat) && Number.isFinite(p.lon)); }
function distance(fixPts) { let d = 0; for (let i = 1; i < fixPts.length; i++) d += haversine(fixPts[i - 1], fixPts[i]); return d; }
/* Rumo entre a última posição e a mais recente afastada pelo menos minMeters (evita ruído parado). */
function heading(fixPts, minMeters = 3) {
  if (fixPts.length < 2) return null;
  const b = fixPts.at(-1);
  for (let i = fixPts.length - 2; i >= 0; i--) {
    const a = fixPts[i];
    if (haversine(a, b) >= minMeters) {
      const y = Math.sin(rad(b.lon - a.lon)) * Math.cos(rad(b.lat));
      const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lon - a.lon));
      return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }
  }
  return null;
}
const COMPASS = ['N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO'];
function compass(deg) { return deg == null ? '' : COMPASS[Math.round(deg / 45) % 8]; }
/* Redução só para desenho: mantém o primeiro, o último e pontos regularmente espaçados. */
function decimate(list, max) {
  if (list.length <= max) return list;
  const out = [], step = (list.length - 1) / (max - 1);
  for (let i = 0; i < max; i++) out.push(list[Math.round(i * step)]);
  return out;
}

const api = {applyEvent, createTrack, addPoint, mergeTrackEvent, points, missing, gaps, deriveState, positionIsCurrent,
  transmitterAlive, STATES, haversine, fixes, distance, heading, compass, decimate, TRANSMITTER_STALE_MS, CONFIRM_WINDOW_MS};
if (typeof module === 'object' && module.exports) module.exports = api;
else root.CanideSync = Object.freeze(api);
})(typeof self !== 'undefined' ? self : this);
