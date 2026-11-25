"use strict";
/* CANIDÉ — admin.js
   Autorização online dos computadores transmissores. O administrador entra com sua conta
   do Firebase Authentication; as regras do banco (no servidor) só aceitam as alterações de
   contas listadas em admins/. Nenhuma credencial fica no código ou no navegador: o token
   vive só na memória desta aba. Dados vindos dos pedidos (nome do computador, código) são
   sempre inseridos como texto, nunca como HTML. */
(() => {
const $ = (id) => document.getElementById(id);
const cfg = window.CANIDE_CONFIG || {};
const DB = String(cfg.databaseURL || '').replace(/\/+$/, '');
const AUTH = cfg.authBase || 'https://identitytoolkit.googleapis.com/v1';
const TOKEN = cfg.tokenBase || 'https://securetoken.googleapis.com/v1';
const S = {uid: '', email: '', idToken: '', refreshToken: '', expiresAt: 0, timer: null, counter: 0, sig: {}};
/* Refaz uma lista só quando os dados mudam: o que o administrador digita não é apagado. */
function changed(name, data) { const sig = JSON.stringify(data); if (S.sig[name] === sig) return false; S.sig[name] = sig; return true; }
function topState(text, on) { if (!$('mode-label')) return; $('mode-label').textContent = text; $('dot').classList.toggle('on', on); }

function msg(id, text, err = false) { const el = $(id); el.textContent = text; el.classList.toggle('err', err); }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
const when = (ms) => Number.isFinite(ms) ? new Date(ms).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'medium'}) : '—';

async function authPost(url, body, form = false) {
 const r = await fetch(url, {method: 'POST', headers: {'Content-Type': form ? 'application/x-www-form-urlencoded' : 'application/json'},
  body: form ? new URLSearchParams(body) : JSON.stringify(body)});
 const out = await r.json().catch(() => ({}));
 if (!r.ok) throw Error(out.error?.message || `HTTP ${r.status}`);
 return out;
}
async function token() {
 if (Date.now() < S.expiresAt) return S.idToken;
 const out = await authPost(`${TOKEN}/token?key=${encodeURIComponent(cfg.apiKey)}`, {grant_type: 'refresh_token', refresh_token: S.refreshToken}, true);
 S.idToken = out.id_token; S.refreshToken = out.refresh_token; S.expiresAt = Date.now() + (Number(out.expires_in) - 300) * 1000;
 return S.idToken;
}
async function db(method, path, body) {
 const q = new URLSearchParams({auth: await token()});
 if (cfg.namespace) q.set('ns', cfg.namespace);
 const r = await fetch(`${DB}/${path}.json?${q}`, {method, cache: 'no-store', headers: {'Content-Type': 'application/json'},
  body: body === undefined ? undefined : JSON.stringify(body)});
 if (r.status === 401 || r.status === 403) throw Error('Permissão negada pelo servidor.');
 if (!r.ok) throw Error(`Banco respondeu HTTP ${r.status}`);
 const text = await r.text();
 return text ? JSON.parse(text) : null;
}
function auditKey() { S.counter += 1; return `${String(Date.now()).padStart(13, '0')}_${String(S.counter % 1e6).padStart(6, '0')}`; }
function audit(type, detail) {
 return db('PUT', `audit/${auditKey()}`, {type, uid: S.uid, at: {'.sv': 'timestamp'}, computer: 'administração', detail: String(detail || '').slice(0, 200)}).catch(() => {});
}

/* ---------- Acesso ---------- */
$('login-form').addEventListener('submit', async (e) => {
 e.preventDefault();
 const btn = $('login-button'); btn.disabled = true; msg('login-message', 'Verificando…');
 try {
  const out = await authPost(`${AUTH}/accounts:signInWithPassword?key=${encodeURIComponent(cfg.apiKey)}`,
   {email: $('login-email').value.trim(), password: $('login-password').value, returnSecureToken: true});
  $('login-password').value = '';
  Object.assign(S, {uid: out.localId, email: out.email || $('login-email').value.trim(), idToken: out.idToken,
   refreshToken: out.refreshToken, expiresAt: Date.now() + (Number(out.expiresIn) - 300) * 1000});
  const isAdmin = await db('GET', `admins/${S.uid}`).catch(() => null);
  if (isAdmin !== true) { logout(); msg('login-message', 'Esta conta não tem papel de administrador.', true); return; }
  audit('admin_login', S.email);
  $('admin-email').textContent = S.email;
  $('login-panel').classList.add('hidden'); $('admin-area').classList.remove('hidden');
  msg('login-message', '');
  await load(); S.timer = setInterval(load, 5000);
 } catch (err) {
  msg('login-message', /INVALID|PASSWORD|EMAIL/.test(String(err.message)) ? 'E-mail ou senha incorretos.' : `Não foi possível entrar: ${err.message}`, true);
 } finally { btn.disabled = false; }
});
function logout() {
 clearInterval(S.timer);
 Object.assign(S, {uid: '', email: '', idToken: '', refreshToken: '', expiresAt: 0, timer: null, sig: {}});
 topState('Sem sessão', false);
 $('admin-area').classList.add('hidden'); $('login-panel').classList.remove('hidden');
}
$('logout').addEventListener('click', logout);

/* ---------- Listas ---------- */
async function load() {
 try {
  const [pairing, access, devices, auditLog] = await Promise.all([
   db('GET', 'pairing'), db('GET', 'access/transmitters'), db('GET', 'devices'),
   db('GET', 'audit', undefined).catch(() => null)]);
  if (changed('pending', [pairing, Object.keys(access || {})])) renderPending(pairing || {}, access || {});
  if (changed('devices', [access, devices])) renderDevices(access || {}, devices || {});
  if (changed('audit', auditLog)) renderAudit(auditLog || {});
  topState('Administrador conectado', true);
 } catch (err) { msg('admin-message', String(err.message), true); }
}
function renderPending(pairing, access) {
 const list = $('pending-list'), items = Object.entries(pairing).filter(([uid]) => !access[uid])
  .sort((a, b) => (b[1].requestedAt || 0) - (a[1].requestedAt || 0));
 $('pending-count').textContent = String(items.length);
 if (!items.length) { list.replaceChildren(el('p', 'note', 'Nenhum computador aguardando autorização.')); return; }
 list.replaceChildren(...items.map(([uid, req]) => {
  const row = el('div', 'admin-item');
  const info = el('div', 'admin-item__info');
  info.append(el('strong', '', String(req.computer || 'Computador')), el('span', 'fine', `Pedido em ${when(req.requestedAt)} · identificador ${uid.slice(0, 10)}…`));
  const form = el('form', 'admin-item__form');
  const id = `n-${uid}`, cid = `c-${uid}`;
  const name = el('input', 'input'); name.id = id; name.required = true; name.maxLength = 64; name.value = String(req.computer || '').slice(0, 64);
  const code = el('input', 'input mono'); code.id = cid; code.required = true; code.placeholder = 'XXXX-XXXX'; code.autocomplete = 'off'; code.spellcheck = false;
  const ln = el('label', '', 'Nome do transmissor'); ln.htmlFor = id;
  const lc = el('label', '', 'Código mostrado no computador'); lc.htmlFor = cid;
  const f1 = el('div', 'field'); f1.append(ln, name);
  const f2 = el('div', 'field'); f2.append(lc, code);
  const ok = el('button', 'btn', 'Autorizar'); ok.type = 'submit';
  const no = el('button', 'btn btn--ghost', 'Recusar'); no.type = 'button';
  const actions = el('div', 'btn-row'); actions.append(ok, no);
  form.append(f1, f2, actions);
  form.addEventListener('submit', async (e) => {
   e.preventDefault();
   if (code.value.trim().toUpperCase() !== String(req.code)) { msg('admin-message', 'O código não confere com o mostrado pelo computador. Nada foi autorizado.', true); return; }
   try {
    await db('PUT', `access/transmitters/${uid}`, {name: name.value.trim(), approvedAt: {'.sv': 'timestamp'}, approvedBy: S.uid, computer: String(req.computer || '').slice(0, 64)});
    await db('DELETE', `pairing/${uid}`);
    await audit('device_approved', `${name.value.trim()} (${uid})`);
    msg('admin-message', `${name.value.trim()} autorizado. O computador começa a transmitir em até 10 segundos.`);
    load();
   } catch (err) { msg('admin-message', String(err.message), true); }
  });
  no.addEventListener('click', async () => {
   try { await db('DELETE', `pairing/${uid}`); msg('admin-message', 'Pedido recusado e removido.'); load(); }
   catch (err) { msg('admin-message', String(err.message), true); }
  });
  row.append(info, form);
  return row;
 }));
}
function renderDevices(access, devices) {
 const list = $('devices-list'), items = Object.entries(access);
 $('devices-count').textContent = String(items.length);
 if (!items.length) { list.replaceChildren(el('p', 'note', 'Nenhum transmissor autorizado.')); return; }
 list.replaceChildren(...items.map(([uid, a]) => {
  const d = devices[uid] || {}, row = el('div', 'admin-item');
  const info = el('div', 'admin-item__info');
  const seen = d.lastSeen ? `visto em ${when(d.lastSeen)}` : 'ainda não se conectou';
  info.append(el('strong', '', String(a.name || 'Transmissor')),
   el('span', 'fine', `${String(d.computer || a.computer || '')} · ${seen} · antena ${String(d.antenna || 'desconhecida')} · autorizado em ${when(a.approvedAt)}`));
  const revoke = el('button', 'btn btn--ghost', 'Revogar'); revoke.type = 'button';
  revoke.addEventListener('click', async () => {
   if (!window.confirm(`Revogar ${a.name || 'este transmissor'}? Ele para de transmitir na próxima escrita.`)) return;
   try { await db('DELETE', `access/transmitters/${uid}`); await audit('device_revoked', `${a.name || ''} (${uid})`); msg('admin-message', 'Transmissor revogado.'); load(); }
   catch (err) { msg('admin-message', String(err.message), true); }
  });
  row.append(info, revoke);
  return row;
 }));
}
const AUDIT = {pairing_request: 'Pedido de autorização', auth_ok: 'Computador autenticado', revoked_detected: 'Revogação detectada pelo computador',
 device_approved: 'Computador autorizado', device_revoked: 'Computador revogado', admin_login: 'Entrada do administrador',
 trajectory_start: 'Trajetória iniciada', trajectory_pause: 'Trajetória pausada', trajectory_resume: 'Trajetória retomada', trajectory_finish: 'Trajetória encerrada'};
function renderAudit(log) {
 const rows = Object.entries(log).sort((a, b) => (a[0] < b[0] ? 1 : -1)).slice(0, 40);
 const body = $('audit-rows');
 if (!rows.length) { const tr = el('tr'); const td = el('td', 'empty-row', 'Sem registros.'); td.colSpan = 4; tr.append(td); body.replaceChildren(tr); return; }
 body.replaceChildren(...rows.map(([, a]) => {
  const tr = el('tr');
  tr.append(el('td', '', when(a.at)), el('td', '', AUDIT[a.type] || String(a.type)), el('td', '', String(a.computer || '')), el('td', '', String(a.detail || '')));
  return tr;
 }));
}

document.body.classList.remove('is-loading');
topState('Sem sessão', false);
if (!DB || !cfg.apiKey) msg('login-message', 'Configuração pública do Firebase ausente neste site.', true);
})();
