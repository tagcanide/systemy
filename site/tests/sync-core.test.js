'use strict';
// node --test site/tests
const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../sync-core.js');

const key = (t, c) => `${String(t).padStart(13, '0')}_${String(c).padStart(6, '0')}`;
const pt = (n, t, extra = {}) => ({n, t, ...extra});

test('applyEvent: put e patch do REST streaming', () => {
  let doc = core.applyEvent(null, 'put', '/', {sessionId: 'a', status: 'preparing', antenna: {state: 'ok'}});
  doc = core.applyEvent(doc, 'patch', '/', {status: 'running', points: 3});
  assert.equal(doc.status, 'running');
  assert.equal(doc.sessionId, 'a');
  doc = core.applyEvent(doc, 'put', '/antenna', {state: 'stale', ageS: 7});
  assert.deepEqual(doc.antenna, {state: 'stale', ageS: 7});
  doc = core.applyEvent(doc, 'put', '/antenna/ageS', 9);
  assert.equal(doc.antenna.ageS, 9);
  doc = core.applyEvent(doc, 'put', '/points', null);
  assert.equal('points' in doc, false);
  assert.equal(core.applyEvent(doc, 'put', '/', null), null);
});

test('trajetória: ordem cronológica, sem duplicatas, fora de ordem reposicionado', () => {
  const tr = core.createTrack();
  assert.equal(core.mergeTrackEvent(tr, 'put', '/', {[key(1000, 1)]: pt(1, 1000), [key(2000, 2)]: pt(2, 2000)}), 2);
  assert.equal(core.mergeTrackEvent(tr, 'patch', '/', {[key(4000, 4)]: pt(4, 4000)}), 1);
  assert.equal(core.mergeTrackEvent(tr, 'put', '/' + key(3000, 3), pt(3, 3000)), 1);   // atrasado
  assert.equal(core.mergeTrackEvent(tr, 'put', '/' + key(2000, 2), pt(2, 2000)), 0);   // repetido (reconexão)
  assert.deepEqual(core.points(tr).map(p => p.n), [1, 2, 3, 4]);
  assert.equal(tr.duplicates, 1);
  assert.equal(tr.outOfOrder, 1);
  assert.equal(tr.lastKey, key(4000, 4));
  assert.equal(core.gaps(tr), 0);
  assert.equal(core.missing(tr, 6), 2);
});

test('trajetória: rejeita chaves e pontos inválidos', () => {
  const tr = core.createTrack();
  assert.equal(core.mergeTrackEvent(tr, 'put', '/', {'x': pt(1, 1), [key(1, 1)]: {n: 1}}), 0);
  assert.equal(tr.keys.length, 0);
});

test('lacunas de numeração detectadas', () => {
  const tr = core.createTrack();
  core.mergeTrackEvent(tr, 'put', '/', {[key(1, 1)]: pt(1, 1), [key(5, 5)]: pt(5, 5)});
  assert.equal(core.gaps(tr), 3);
});

const base = {configured: true, online: true, stream: 'open', loaded: true, now: 100000, loadedAt: 0, hbChangedAt: 99000};
const live = (status, antenna = 'ok') => ({sessionId: '20261124T120000Z_abcdef', status, antenna: {state: antenna}});

test('estados: cada situação tem um estado distinto', () => {
  assert.equal(core.deriveState({...base, configured: false}), 'unconfigured');
  assert.equal(core.deriveState({...base, loaded: false}), 'syncing');
  assert.equal(core.deriveState({...base, live: null}), 'waiting');
  assert.equal(core.deriveState({...base, live: live('preparing')}), 'preparing');
  assert.equal(core.deriveState({...base, live: live('running')}), 'running');
  assert.equal(core.deriveState({...base, live: live('paused')}), 'paused');
  assert.equal(core.deriveState({...base, live: live('resuming')}), 'resuming');
  assert.equal(core.deriveState({...base, live: live('finished')}), 'finished');
  assert.equal(core.deriveState({...base, live: live('running', 'simulated')}), 'running');
  assert.equal(core.deriveState({...base, trackLoading: true, live: live('running')}), 'syncing');
});

test('estados: perda de internet do espectador não é perda de antena', () => {
  assert.equal(core.deriveState({...base, online: false, live: live('running')}), 'interrupted');
  assert.equal(core.deriveState({...base, stream: 'error', live: live('running')}), 'interrupted');
  assert.equal(core.deriveState({...base, live: live('running', 'stale')}), 'noSignal');
  assert.equal(core.deriveState({...base, live: live('running', 'disconnected')}), 'noSignal');
});

test('estados: transmissor sem batimento vira desconectado, sem usar o relógio de parede', () => {
  assert.equal(core.deriveState({...base, hbChangedAt: base.now - 16000, live: live('running')}), 'transmitterDown');
  // Ao entrar, aguarda a confirmação de um batimento novo antes de concluir.
  assert.equal(core.deriveState({...base, hbChangedAt: null, loadedAt: base.now - 3000, live: live('running')}), 'syncing');
  assert.equal(core.deriveState({...base, hbChangedAt: null, loadedAt: base.now - 13000, live: live('running')}), 'transmitterDown');
  // Pausa com transmissor ativo continua pausa; sessão finalizada não exige batimento.
  assert.equal(core.deriveState({...base, hbChangedAt: base.now - 60000, live: live('finished')}), 'finished');
  assert.equal(core.positionIsCurrent('noSignal'), false);
  assert.equal(core.positionIsCurrent('running'), true);
});

test('distância e rumo calculados das posições reais', () => {
  const a = {lat: -15.8, lon: -48.0, fix: true}, b = {lat: -15.799, lon: -48.0, fix: true};
  const d = core.haversine(a, b);
  assert.ok(Math.abs(d - 111.2) < 0.5, String(d));
  assert.equal(Math.round(core.heading([a, b])), 0);
  assert.equal(core.compass(core.heading([a, b])), 'N');
  const east = {lat: -15.8, lon: -47.999, fix: true};
  assert.equal(core.compass(core.heading([a, east])), 'L');
  assert.equal(core.heading([a, {...a}]), null);          // parado: sem rumo
  assert.equal(core.fixes([a, {lat: 1, lon: 1, fix: false}, {fix: true}]).length, 1);
  assert.ok(Math.abs(core.distance([a, b, a]) - 2 * d) < 1e-6);
});

test('decimação preserva primeiro e último', () => {
  const list = Array.from({length: 10000}, (_, i) => i);
  const out = core.decimate(list, 2500);
  assert.equal(out.length, 2500);
  assert.equal(out[0], 0);
  assert.equal(out.at(-1), 9999);
  assert.equal(core.decimate([1, 2], 10).length, 2);
});
