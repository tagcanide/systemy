"""Sincronização online da trajetória (Firebase Realtime Database) para o gateway CANIDÉ.

O InfluxDB e o CSV continuam sendo o registro oficial da telemetria. Este módulo
apenas distribui o estado da trajetória e os pontos da sessão ativa para os
espectadores do site público. Nada aqui bloqueia a recepção da antena:

* ``on_sample`` só enfileira operações em memória (chamado dentro de save_sample);
* uma thread própria envia lotes por REST (PATCH multi-caminho), com reenvio;
* sem internet ou sem Firebase, a fila cresce (limitada) e é enviada depois,
  na mesma ordem. Chaves determinísticas tornam o reenvio idempotente.

Autorização: cada computador tem identidade própria no Firebase Authentication,
criada por ele mesmo na primeira execução e guardada cifrada (DPAPI) só nele.
O administrador autoriza o computador online (site/admin.html); as regras do banco,
avaliadas no servidor, só aceitam escrita de UIDs em ``access/transmitters``.
Revogar remove o UID e bloqueia a próxima escrita. Nenhuma credencial é transportada.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable

import ecotam_secret

AUTH_BASE = "https://identitytoolkit.googleapis.com/v1"
TOKEN_BASE = "https://securetoken.googleapis.com/v1"
# Emulador local do Firebase (testes). Em produção só HTTPS do Firebase é aceito.
ALLOW_LOCAL = False

STATUSES = ("preparing", "running", "paused", "resuming", "finished")
HEARTBEAT_S = 5.0
ANTENNA_STALE_S = 5.0
MAX_OPS = 120_000          # ~16 h de pontos a 1 Hz com eventos; acima disso descarta os mais antigos
CHUNK_OPS = 400            # operações por PATCH
HTTP_TIMEOUT = 8
SID_FORMAT = "%Y%m%dT%H%M%SZ"
# Identidade deste computador: fica só nele, fora do OneDrive e do repositório.
IDENTITY_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CANIDE" / "identidade-dispositivo.json"
AUTH_RECHECK_S = 60.0       # autorizado: reconfere a autorização (revogação) a cada minuto
PENDING_POLL_S = 10.0       # aguardando aprovação: consulta a cada 10 s

# Campos compactos publicados por ponto. Só entram quando existem na amostra.
POINT_FIELDS = (
    ("sequence", "seq", int), ("latitude", "lat", float), ("longitude", "lon", float),
    ("gps_altitude_m", "ga", float), ("altitude_m", "ba", float), ("temperature_c", "tc", float),
    ("pressure_pa", "pa", float), ("humidity_pct", "hu", float), ("satellites", "sat", int),
    ("protocol_version", "pv", int),
)


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_database_url(raw: str) -> tuple[str, dict[str, str]]:
    """Retorna (base, parâmetros fixos). Aceita só o RTDB do Firebase (ou o emulador em testes)."""
    parsed = urllib.parse.urlsplit(str(raw).strip())
    host = (parsed.hostname or "").lower()
    query = dict(urllib.parse.parse_qsl(parsed.query))
    if parsed.username or parsed.password or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Informe só o endereço do Realtime Database, sem caminho")
    if parsed.scheme == "https" and (host.endswith(".firebaseio.com") or host.endswith(".firebasedatabase.app")):
        if query:
            raise ValueError("O endereço do Realtime Database não leva parâmetros")
        return f"https://{host}", {}
    if ALLOW_LOCAL and parsed.scheme == "http" and host in ("127.0.0.1", "localhost") and parsed.port:
        return f"http://{host}:{parsed.port}", {k: v for k, v in query.items() if k == "ns"}
    raise ValueError("Use o endereço do Realtime Database, por exemplo https://SEU-PROJETO-default-rtdb.firebaseio.com")


def compact_point(record: dict[str, Any], n: int) -> dict[str, Any]:
    point: dict[str, Any] = {"n": n, "t": int(record["timestamp_ms"])}
    has_fix = record.get("gps_fix") is True
    for name, short, cast in POINT_FIELDS:
        if name in ("latitude", "longitude", "gps_altitude_m", "satellites") and not has_fix:
            continue
        value = record.get(name)
        if value is None or value == "":
            continue
        try:
            point[short] = cast(value)
        except (TypeError, ValueError):
            continue
    if isinstance(record.get("gps_fix"), bool):
        point["fix"] = record["gps_fix"]
    return point


class SyncError(Exception):
    pass


class FirebaseSync:
    def __init__(self, data_dir: Path, gateway_session: str,
                 antenna_probe: Callable[[], dict[str, Any]] | None = None,
                 identity_path: Path | None = None, public_config: dict[str, str] | None = None) -> None:
        self.lock = threading.RLock()
        self.identity_path = Path(identity_path) if identity_path else IDENTITY_PATH
        self.computer = ecotam_secret.this_computer() or "COMPUTADOR"
        self.device_state = "starting"   # starting|registering|pending|authorized|revoked|offline|unconfigured
        self.device_name = ""
        self.pair_code = ""
        self.identity_created = 0
        self.new_identity = False
        self.next_auth = 0.0
        self.wake = threading.Event()
        self.data_dir = Path(data_dir)
        self.gateway_session = gateway_session
        self.antenna_probe = antenna_probe or (lambda: {"state": "none", "ageS": None, "source": "rx_serial"})
        self.cfg: dict[str, Any] | None = None
        if public_config:
            self.set_public_config(public_config)
        self.id_token = ""
        self.refresh_token = ""
        self.token_exp = 0.0
        self.uid = ""
        self.authorized: bool | None = None
        self.ops: deque[tuple[str, Any]] = deque()
        self.traj: dict[str, Any] | None = None
        self.key_counter = 0
        self.sent_ops = 0
        self.sent_points = 0
        self.failures = 0
        self.dropped = 0
        self.last_ok_ms = 0
        self.last_error = ""
        self.link = "off"          # off | ok | retrying | denied
        self.backoff = 0.0
        self.next_try = 0.0
        self.last_heartbeat = 0.0
        self._restore()

    # ------------------------------------------------------------ persistência local
    @property
    def journal_path(self) -> Path:
        return self.data_dir / "trajetorias.jsonl"

    @property
    def active_path(self) -> Path:
        return self.data_dir / "trajetoria_ativa.json"

    def _journal(self, event: str) -> None:
        t = self.traj
        if t is None:
            return
        entry = {"event": event, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                 "trajectory_id": t["id"], "gateway_session": t["gatewaySession"], "source": t["source"],
                 "points": t["points"]}
        try:
            self.data_dir.mkdir(exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.last_error = f"Não foi possível registrar a trajetória em dados/: {exc}"
        self._save_active()

    def _save_active(self) -> None:
        try:
            self.data_dir.mkdir(exist_ok=True)
            if self.traj is None or self.traj["status"] == "finished":
                self.active_path.unlink(missing_ok=True)
            else:
                tmp = self.active_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.traj, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.active_path)
        except OSError as exc:
            self.last_error = f"Não foi possível salvar o estado da trajetória: {exc}"

    def _restore(self) -> None:
        """Gateway reiniciado no meio de uma trajetória: continua a mesma sessão."""
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("status") in STATUSES[:-1] and isinstance(data.get("id"), str):
            self.traj = data
            self.traj["gatewaySession"] = self.gateway_session

    # ------------------------------------------------------------ configuração pública
    def set_public_config(self, public: dict[str, str]) -> None:
        """Chave de API web e endereço do banco: valores públicos do repositório, iguais em todo computador."""
        try:
            base, query = normalize_database_url(public.get("databaseURL", ""))
        except ValueError as exc:
            self.last_error = str(exc)
            return
        api_key = str(public.get("apiKey", "")).strip()
        if api_key:
            self.cfg = {"apiKey": api_key, "base": base, "query": query, "databaseURL": public["databaseURL"]}

    # ------------------------------------------------------------ autenticação
    def _post_json(self, url: str, payload: bytes, ctype: str) -> dict[str, Any]:
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": ctype, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read(65536).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(4096).decode("utf-8")).get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise SyncError(f"HTTP {exc.code} {detail}".strip()) from exc

    def _refresh(self) -> None:
        cfg = self.cfg
        if cfg is None:
            raise SyncError("Sem configuração")
        url = f"{TOKEN_BASE}/token?" + urllib.parse.urlencode({"key": cfg["apiKey"]})
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token}).encode()
        out = self._post_json(url, body, "application/x-www-form-urlencoded")
        with self.lock:
            self.id_token = out["id_token"]
            self.refresh_token = out["refresh_token"]
            self.token_exp = time.monotonic() + int(out.get("expires_in", "3600")) - 300

    def _db_url(self, path: str, extra: dict[str, str] | None = None) -> str:
        cfg = self.cfg or {}
        query = dict(cfg.get("query", {}))
        query["auth"] = self.id_token
        if extra:
            query.update(extra)
        return f"{cfg['base']}/{path}.json?" + urllib.parse.urlencode(query)

    def _request(self, method: str, path: str, body: Any = None, extra: dict[str, str] | None = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self._db_url(path, extra), data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read(65536)
            return json.loads(raw.decode("utf-8")) if raw else None

    # ------------------------------------------------------------ identidade do computador
    # Cada computador cria sua própria conta no Firebase Authentication (anônima, sem senha)
    # na primeira execução. O token de renovação dessa conta fica só neste computador,
    # cifrado com DPAPI em %LOCALAPPDATA%\CANIDE (fora do OneDrive e do repositório).
    # Ninguém copia credenciais: o administrador autoriza o UID pela página admin.html,
    # e as regras do banco (avaliadas no servidor) só aceitam escrita de UIDs autorizados.
    def _load_identity(self) -> dict[str, Any] | None:
        try:
            stored = json.loads(self.identity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        try:
            if stored.get("format") == ecotam_secret.FORMAT:
                return json.loads(ecotam_secret.unprotect(stored))
            if not ecotam_secret.available():       # fora do Windows (testes/CI): sem DPAPI
                return stored
        except (ValueError, KeyError, TypeError):
            pass
        return None                                 # de outra conta Windows ou adulterado: nova identidade

    def _save_identity(self) -> None:
        doc = {"uid": self.uid, "refreshToken": self.refresh_token, "pairCode": self.pair_code,
               "computer": self.computer, "createdAt": self.identity_created}
        payload: dict[str, Any] = (ecotam_secret.protect(json.dumps(doc).encode("utf-8"))
                                   if ecotam_secret.available() else doc)
        self.identity_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.identity_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.identity_path)

    @staticmethod
    def _new_pair_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # sem 0/O e 1/I para leitura em voz alta
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        return raw[:4] + "-" + raw[4:]

    def _register(self) -> None:
        """Cria a identidade deste computador (conta anônima própria)."""
        cfg = self.cfg
        assert cfg is not None
        url = f"{AUTH_BASE}/accounts:signUp?" + urllib.parse.urlencode({"key": cfg["apiKey"]})
        out = self._post_json(url, json.dumps({"returnSecureToken": True}).encode(), "application/json")
        with self.lock:
            self.uid = out["localId"]
            self.id_token = out["idToken"]
            self.refresh_token = out["refreshToken"]
            self.token_exp = time.monotonic() + int(out.get("expiresIn", "3600")) - 300
            self.pair_code = self._new_pair_code()
            self.identity_created = now_ms()
            self._save_identity()
            self.new_identity = True

    def _audit(self, kind: str, detail: str = "") -> None:
        """Registro de auditoria imediato (fora da fila de telemetria)."""
        self.key_counter += 1
        key = f"{now_ms():013d}_{self.key_counter % 1_000_000:06d}"
        entry = {"type": kind, "uid": self.uid, "at": {".sv": "timestamp"}, "computer": self.computer}
        if detail:
            entry["detail"] = detail[:200]
        try:
            self._request("PUT", f"audit/{key}", entry)
        except (urllib.error.URLError, OSError):
            pass

    def authenticate(self) -> str:
        """Identidade + autorização. Retorna o estado do dispositivo. Nunca bloqueia a recepção."""
        with self.lock:
            if self.cfg is None:
                self.device_state = "unconfigured"
                return self.device_state
        try:
            if not self.uid:
                ident = self._load_identity()
                if ident:
                    with self.lock:
                        self.uid, self.refresh_token = ident["uid"], ident["refreshToken"]
                        self.pair_code = ident.get("pairCode") or self._new_pair_code()
                        self.identity_created = ident.get("createdAt", 0)
                        self.token_exp = 0.0
                else:
                    with self.lock:
                        self.device_state = "registering"
                    self._register()
            if time.monotonic() >= self.token_exp:
                self._refresh()
            access = self._request("GET", f"access/transmitters/{self.uid}")
        except SyncError as exc:
            msg = str(exc)
            if any(c in msg for c in ("INVALID_REFRESH_TOKEN", "TOKEN_EXPIRED", "USER_DISABLED", "USER_NOT_FOUND")):
                # Conta do computador apagada ou desativada pelo administrador: identidade nova,
                # que precisa de nova autorização. Nada volta a transmitir sem aprovação.
                with self.lock:
                    self.uid = ""
                    self.authorized = False
                    self.device_state = "revoked"
                    self.last_error = "A identidade deste computador foi revogada. Uma nova identidade aguarda autorização."
                    self.identity_path.unlink(missing_ok=True)
                return self.device_state
            with self.lock:
                self.device_state = "offline"
                self.last_error = f"Sem acesso ao serviço de autenticação: {msg[:120]}. A gravação local continua."
            return self.device_state
        except urllib.error.HTTPError as exc:
            with self.lock:
                self.device_state = "offline"
                self.last_error = f"Sem acesso ao Realtime Database (HTTP {exc.code}). A gravação local continua."
            return self.device_state
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            with self.lock:
                self.device_state = "offline"
                self.last_error = f"Sem internet para autenticar este computador. A gravação local continua. ({str(exc)[:100]})"
            return self.device_state

        if access:
            was = self.authorized
            with self.lock:
                self.authorized = True
                self.device_state = "authorized"
                self.device_name = access.get("name", "") if isinstance(access, dict) else ""
                self.link = "ok" if self.link in ("off", "denied") else self.link
                self.last_error = ""
            try:
                self._request("PUT", f"devices/{self.uid}", {"computer": self.computer, "lastSeen": {".sv": "timestamp"},
                                                             "antenna": self.antenna_probe().get("state", "none")})
            except (urllib.error.URLError, OSError):
                pass
            if not was:
                self._audit("auth_ok", f"gateway {self.gateway_session}")
            self.wake.set()
            return self.device_state

        was_authorized = bool(self.authorized) or self.device_state == "revoked"
        with self.lock:
            self.authorized = False
            self.device_state = "revoked" if was_authorized else "pending"
            self.last_error = ("Este computador foi revogado pelo administrador. A transmissão está bloqueada."
                               if was_authorized else "")
        # Pedido de autorização: o administrador confere o código mostrado aqui e aprova.
        try:
            self._request("PUT", f"pairing/{self.uid}", {"code": self.pair_code, "computer": self.computer,
                                                         "requestedAt": {".sv": "timestamp"}})
            if self.new_identity or was_authorized:
                self._audit("pairing_request" if not was_authorized else "revoked_detected")
                self.new_identity = False
        except (urllib.error.URLError, OSError) as exc:
            with self.lock:
                self.last_error = f"Não foi possível registrar o pedido de autorização: {str(exc)[:120]}"
        return self.device_state

    def reset_identity(self) -> dict[str, str]:
        """Recuperação: descarta a identidade deste computador e cria outra (exige nova autorização)."""
        with self.lock:
            self.identity_path.unlink(missing_ok=True)
            self.uid = self.refresh_token = self.id_token = ""
            self.authorized = False
            self.device_state = "starting"
            self.next_auth = 0.0
        self.wake.set()
        return {"message": "Identidade descartada. Uma nova identidade será criada e precisa ser autorizada na página de administração."}

    # ------------------------------------------------------------ trajetória
    def _queue(self, path: str, value: Any) -> None:
        if len(self.ops) >= MAX_OPS:
            self.ops.popleft()
            self.dropped += 1
        self.ops.append((path, value))

    def _event(self, kind: str) -> None:
        t = self.traj
        assert t is not None
        self.key_counter += 1
        key = f"{now_ms():013d}_{self.key_counter % 1_000_000:06d}"
        self._queue(f"sessions/{t['id']}/events/{key}", {"type": kind, "at": {".sv": "timestamp"}})

    def _audit_op(self, kind: str) -> None:
        t = self.traj
        self.key_counter += 1
        key = f"{now_ms():013d}_{self.key_counter % 1_000_000:06d}"
        entry = {"type": kind, "uid": "", "at": {".sv": "timestamp"}, "computer": self.computer}   # uid no envio
        if t:
            entry["detail"] = t["id"]
        self._queue(f"audit/{key}", entry)

    def _set_status(self, status: str, stamp_field: str | None = None) -> None:
        t = self.traj
        assert t is not None
        t["status"] = status
        server_ts = {".sv": "timestamp"}
        self._queue("live/status", status)
        self._queue("live/statusAt", server_ts)
        self._queue(f"sessions/{t['id']}/info/status", status)
        if stamp_field:
            t[stamp_field] = now_ms()
            self._queue(f"sessions/{t['id']}/info/{stamp_field}", server_ts)

    def start(self, allow_sim: bool = False) -> dict[str, str]:
        antenna = self.antenna_probe()   # fora do lock: o probe usa o lock do gateway
        source = antenna.get("source", "rx_serial")
        if source == "sim":
            if not allow_sim:
                raise ValueError("A fonte atual é a simulação. Selecione ESP32 RX ou marque a opção de teste com dados simulados.")
        elif antenna.get("state") != "ok":
            raise ValueError("Antena sem dados válidos recentes. Confira a COM3 e aguarde pacotes do RX antes de iniciar.")
        if not antenna.get("hasSample"):
            raise ValueError("Ainda não há amostras válidas desta fonte. Aguarde a primeira leitura.")
        with self.lock:
            if self.traj is not None and self.traj["status"] != "finished":
                raise ValueError("Já existe uma trajetória aberta. Encerre a atual antes de iniciar outra.")
            sid = dt.datetime.now(dt.timezone.utc).strftime(SID_FORMAT) + "_" + secrets.token_hex(3)
            started = now_ms()
            self.traj = {"id": sid, "status": "preparing", "source": source, "startedAt": started,
                         "points": 0, "lastKey": "", "gatewaySession": self.gateway_session}
            server_ts = {".sv": "timestamp"}
            self._queue(f"sessions/{sid}/info", {"status": "preparing", "startedAt": server_ts, "source": source,
                                                  "gatewaySession": self.gateway_session})
            self._queue("live", {"sessionId": sid, "status": "preparing", "statusAt": server_ts,
                                 "startedAt": server_ts, "source": source, "points": 0, "lastKey": "",
                                 "heartbeatAt": server_ts, "antenna": self._antenna_doc(antenna)})
            self._event("start")
            self._audit_op("trajectory_start")
            self._journal("start")
        self.wake.set()
        return {"message": f"Trajetória {sid} iniciada. Os espectadores passam a acompanhar automaticamente."}

    def _require(self, allowed: tuple[str, ...], verb: str) -> dict[str, Any]:
        t = self.traj
        if t is None or t["status"] not in allowed:
            raise ValueError(f"Não há trajetória que possa ser {verb} agora.")
        return t

    def pause(self) -> dict[str, str]:
        with self.lock:
            self._require(("preparing", "running", "resuming"), "pausada")
            self._set_status("paused", "pausedAt")
            self._event("pause")
            self._audit_op("trajectory_pause")
            self._journal("pause")
        self.wake.set()
        return {"message": "Trajetória pausada. A gravação local continua; o site deixa de receber pontos."}

    def resume(self) -> dict[str, str]:
        with self.lock:
            self._require(("paused",), "retomada")
            self._set_status("resuming", "resumedAt")
            self._event("resume")
            self._audit_op("trajectory_resume")
            self._journal("resume")
        self.wake.set()
        return {"message": "Trajetória retomada. O próximo ponto recebido volta a ser publicado."}

    def finish(self) -> dict[str, str]:
        with self.lock:
            t = self._require(("preparing", "running", "paused", "resuming"), "encerrada")
            self._set_status("finished", "finishedAt")
            self._event("finish")
            self._audit_op("trajectory_finish")
            self._journal("finish")
            sid = t["id"]
        self.wake.set()
        return {"message": f"Trajetória {sid} encerrada. Os registros oficiais permanecem no InfluxDB e no CSV."}

    def on_sample(self, record: dict[str, Any]) -> None:
        """Chamado por save_sample. Só memória; nunca faz rede."""
        with self.lock:
            t = self.traj
            if t is None or t["status"] in ("paused", "finished"):
                return
            if record.get("source") != t["source"] or "timestamp_ms" not in record:
                return
            t["points"] += 1
            self.key_counter += 1
            key = f"{int(record['timestamp_ms']):013d}_{self.key_counter % 1_000_000:06d}"
            t["lastKey"] = key
            self._queue(f"sessions/{t['id']}/track/{key}", compact_point(record, t["points"]))
            if t["status"] in ("preparing", "resuming"):
                self._set_status("running")
            if t["points"] % 30 == 0:
                self._save_active()
        self.wake.set()

    # ------------------------------------------------------------ envio
    @staticmethod
    def _antenna_doc(antenna: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = {"state": antenna.get("state", "none")}
        if isinstance(antenna.get("ageS"), (int, float)):
            doc["ageS"] = round(float(antenna["ageS"]), 1)
        return doc

    def _take_chunk(self) -> tuple[dict[str, Any], int, int, str]:
        """Primeiras operações da fila sem caminhos sobrepostos (exigência do PATCH multi-caminho).

        Retorna (payload, operações consumidas, n do último ponto, chave do último ponto).
        """
        payload: dict[str, Any] = {}
        ancestors: set[str] = set()
        count, last_n, last_key = 0, 0, ""
        for path, value in list(self.ops)[:CHUNK_OPS]:
            parts = path.split("/")
            prefixes = {"/".join(parts[:i]) for i in range(1, len(parts))}
            if path not in payload and (path in ancestors or any(p in payload for p in prefixes)):
                break
            payload[path] = value
            ancestors |= prefixes
            count += 1
            if "/track/" in path:
                last_n, last_key = value["n"], parts[-1]
        return payload, count, last_n, last_key

    def flush_once(self) -> bool:
        """Envia um lote. Retorna True se algo foi enviado com sucesso."""
        antenna = self.antenna_probe()
        with self.lock:
            if self.cfg is None or not self.authorized:
                return False
            if time.monotonic() < self.next_try:
                return False
            payload, count, n, last_key = self._take_chunk()
            for path in [k for k in payload if k.startswith("audit/")]:
                payload[path] = dict(payload[path], uid=self.uid)
            t = self.traj
            beat_due = (t is not None and t["status"] != "finished"
                        and time.monotonic() - self.last_heartbeat >= HEARTBEAT_S)
            if count == 0 and not beat_due:
                return False
            # Batimento, antena e contadores vão junto do lote, salvo quando o lote recria "live".
            if "live" not in payload:
                if t is not None and t["status"] != "finished":
                    payload["live/heartbeatAt"] = {".sv": "timestamp"}
                    payload["live/antenna"] = self._antenna_doc(antenna)
                if n:
                    payload["live/points"] = n
                    payload["live/lastKey"] = last_key
            if not payload:
                return False
            need_refresh = time.monotonic() >= self.token_exp
        try:
            if need_refresh:
                self._refresh()
            try:
                self._request("PATCH", "", payload, {"print": "silent"})
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._refresh()
                    self._request("PATCH", "", payload, {"print": "silent"})
                else:
                    raise
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace")
            with self.lock:
                self.failures += 1
                if exc.code in (401, 403) or "Permission denied" in detail:
                    # Revogado no servidor (ou dado inválido): para de enviar e reconfere a autorização já.
                    self.link = "denied"
                    self.authorized = False
                    self.device_state = "revoked"
                    self.last_error = "O servidor recusou a escrita: este computador não está autorizado no momento."
                    self.backoff = 30.0
                    self.next_auth = 0.0
                else:
                    self.link = "retrying"
                    self.last_error = f"Firebase HTTP {exc.code}: {detail[:160]}"
                    self.backoff = min(30.0, max(1.0, self.backoff * 2))
                self.next_try = time.monotonic() + self.backoff
            return False
        except (urllib.error.URLError, OSError, SyncError, ValueError, KeyError) as exc:
            with self.lock:
                self.failures += 1
                self.link = "retrying"
                self.last_error = f"Sem comunicação com o Firebase: {str(exc)[:160]}. Os dados continuam no InfluxDB e no CSV."
                self.backoff = min(30.0, max(1.0, self.backoff * 2))
                self.next_try = time.monotonic() + self.backoff
            return False
        with self.lock:
            for _ in range(count):
                path, _value = self.ops.popleft()
                if "/track/" in path:
                    self.sent_points += 1
            self.sent_ops += count
            self.last_heartbeat = time.monotonic()
            self.last_ok_ms = now_ms()
            self.link = "ok"
            self.last_error = ""
            self.backoff = 0.0
            self.next_try = 0.0
        return True

    def run_forever(self) -> None:
        offline_wait = 5.0
        while True:
            self.wake.wait(timeout=1.0)
            self.wake.clear()
            if time.monotonic() >= self.next_auth:
                state = self.authenticate()
                if state == "offline":
                    wait, offline_wait = offline_wait, min(120.0, offline_wait * 2)
                else:
                    offline_wait = 5.0
                    wait = AUTH_RECHECK_S if state == "authorized" else PENDING_POLL_S
                with self.lock:
                    self.next_auth = time.monotonic() + wait
            try:
                # Esvazia a fila acumulada em lotes seguidos; depois volta ao ritmo normal.
                for _ in range(50):
                    if not self.flush_once():
                        break
                    with self.lock:
                        if not self.ops:
                            break
            except Exception as exc:  # nunca derruba a thread de sincronização
                with self.lock:
                    self.last_error = f"Falha interna na sincronização: {exc}"
                time.sleep(1)

    def status(self) -> dict[str, Any]:
        with self.lock:
            t = self.traj
            return {
                "configured": self.cfg is not None,
                "authorized": self.authorized,
                "link": self.link,
                "device": {"state": self.device_state, "uid": self.uid, "code": self.pair_code,
                           "computer": self.computer, "name": self.device_name,
                           "protected": ecotam_secret.available()},
                "database_url": self.cfg["databaseURL"] if self.cfg else "",
                "pending": len(self.ops),
                "sent_points": self.sent_points,
                "failures": self.failures,
                "dropped": self.dropped,
                "last_ok_ms": self.last_ok_ms,
                "error": self.last_error,
                "trajectory": dict(t) if t else None,
            }
