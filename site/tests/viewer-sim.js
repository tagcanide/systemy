'use strict';
/* Espectador automatizado para o teste ponta a ponta com o emulador do Firebase.
   Usa o mesmo protocolo do site (REST streaming com EventSource) e o mesmo núcleo
   (sync-core.js). Opcionalmente "perde a internet" por um intervalo e volta,
   reabrindo o percurso a partir da última chave recebida.

   node --experimental-eventsource site/tests/viewer-sim.js --base http://127.0.0.1:9000 --ns demo-canide-default-rtdb \
        --name A [--drop-at 9000 --drop-for 4000] [--timeout 120000]
   Escreve um JSON com o que recebeu na saída padrão ao terminar (status "finished"). */
const core = require('../sync-core.js');

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, v, i, all) => (v.startsWith('--') ? acc.concat([[v.slice(2), all[i + 1]]]) : acc), []));
const BASE = args.base, NS = args.ns, NAME = args.name || 'viewer';
const DROP_AT = Number(args['drop-at'] || 0), DROP_FOR = Number(args['drop-for'] || 0), TIMEOUT = Number(args.timeout || 120000);
const t0 = Date.now();
const url = (path, params = {}) => `${BASE}/${path}.json?` + new URLSearchParams({ns: NS, ...params});

let live = null, sessionId = null, track = core.createTrack(), liveES = null, trackES = null;
const statuses = [], sessions = new Set(), reconnects = {live: 0, track: 0};
let dropped = false, firstStatusAt = null;

function openLive() {
  liveES = new EventSource(url('live'));
  const on = kind => e => {
    const msg = JSON.parse(e.data);
    live = core.applyEvent(live, kind, msg.path, msg.data);
    if (live && live.status && statuses.at(-1) !== live.status) { statuses.push(live.status); firstStatusAt ??= Date.now() - t0; }
    const sid = live && live.sessionId || null;
    if (sid && sid !== sessionId) { sessionId = sid; sessions.add(sid); track = core.createTrack(); openTrack(); }
    if (live && live.status === 'finished') setTimeout(finish, 2500);   // tempo para os últimos pontos chegarem
  };
  liveES.addEventListener('put', on('put'));
  liveES.addEventListener('patch', on('patch'));
}
function openTrack() {
  if (trackES) trackES.close();
  const params = track.lastKey ? {orderBy: '"$key"', startAt: `"${track.lastKey}"`} : {};
  trackES = new EventSource(url(`sessions/${sessionId}/track`, params));
  const on = kind => e => { const msg = JSON.parse(e.data); core.mergeTrackEvent(track, kind, msg.path, msg.data); };
  trackES.addEventListener('put', on('put'));
  trackES.addEventListener('patch', on('patch'));
  trackES.onerror = () => { trackES.close(); reconnects.track++; setTimeout(openTrack, 1000); };
}
function drop() {
  dropped = true;
  liveES.close(); if (trackES) trackES.close();
  setTimeout(() => { dropped = false; reconnects.live++; openLive(); if (sessionId) { reconnects.track++; openTrack(); } }, DROP_FOR);
}
let done = false;
function finish() {
  if (done) return; done = true;
  liveES && liveES.close(); trackES && trackES.close();
  const pts = core.points(track);
  process.stdout.write(JSON.stringify({
    name: NAME, statuses, sessions: [...sessions], keys: track.keys, ns: pts.map(p => p.n),
    duplicates: track.duplicates, outOfOrder: track.outOfOrder, gaps: core.gaps(track), reconnects,
    livePoints: live && live.points, firstStatusAt
  }) + '\n');
  process.exit(0);
}
openLive();
if (DROP_AT) setTimeout(drop, DROP_AT);
setTimeout(() => { process.stderr.write(`[${NAME}] tempo esgotado\n`); finish(); }, TIMEOUT);
