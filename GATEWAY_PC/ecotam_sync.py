"""Sincronização online da trajetória (Firebase Realtime Database) para o gateway CANIDÉ.

O InfluxDB e o CSV continuam sendo o registro oficial da telemetria. Este módulo
apenas distribui o estado da trajetória e os pontos da sessão ativa para os
espectadores do site público. Nada aqui bloqueia a recepção da antena:

* ``on_sample`` só enfileira operações em memória (chamado dentro de save_sample);
* uma thread própria envia lotes por REST (PATCH multi-caminho), com reenvio;
* sem internet ou sem Firebase, a fila cresce (limitada) e é enviada depois,
  na mesma ordem. Chaves determinísticas tornam o reenvio idempotente.

Autorização: o gateway entra com uma conta do Firebase Authentication
(e-mail e senha, só em memória). As regras do banco só aceitam escrita de UIDs
listados em ``access/transmitters``. Nenhuma credencial administrativa é usada.
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
# Credencial do transmissor: token de renovação do Firebase, nunca a senha.
# Fica na pasta do gateway para acompanhar a pasta até o computador da antena
# (OneDrive, pendrive ou zip). O .gitignore impede que vá para o repositório público.
CREDENTIALS_PATH = Path(__file__).resolve().parent / "transmissor.local.json"
# Local usado pela versão anterior; ainda é lido se existir.
LEGACY_CREDENTIALS_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CANIDE" / "transmissor-firebase.json"

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
                 credentials_path: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.credentials_path = Path(credentials_path) if credentials_path else CREDENTIALS_PATH
        if credentials_path is None and not self.credentials_path.exists() and LEGACY_CREDENTIALS_PATH.exists():
            self.credentials_path = LEGACY_CREDENTIALS_PATH
        self.remembered = self.credentials_path.exists()   # a thread de envio reconecta sozinha
        self.wake = threading.Event()
        self.data_dir = Path(data_dir)
        self.gateway_session = gateway_session
        self.antenna_probe = antenna_probe or (lambda: {"state": "none", "ageS": None, "source": "rx_serial"})
        self.cfg: dict[str, Any] | None = None
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

    def _sign_in(self, api_key: str, email: str, password: str) -> None:
        url = f"{AUTH_BASE}/accounts:signInWithPassword?" + urllib.parse.urlencode({"key": api_key})
        try:
            out = self._post_json(url, json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode(),
                                  "application/json")
        except SyncError as exc:
            msg = str(exc)
            if any(code in msg for code in ("INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_EMAIL")):
                raise ValueError("E-mail ou senha do transmissor incorretos") from exc
            if "API key" in msg or "API_KEY" in msg:
                raise ValueError("Chave de API do Firebase inválida") from exc
            if "OPERATION_NOT_ALLOWED" in msg or "PASSWORD_LOGIN_DISABLED" in msg:
                raise ValueError("Ative o login por e-mail e senha no Firebase Authentication") from exc
            raise ValueError(f"Falha ao autenticar no Firebase: {msg}") from exc
        self.id_token = out["idToken"]
        self.refresh_token = out["refreshToken"]
        self.token_exp = time.monotonic() + int(out.get("expiresIn", "3600")) - 300
        self.uid = out["localId"]

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

    # ------------------------------------------------------------ conexão lembrada
    def _save_credentials(self) -> None:
        cfg = self.cfg
        if cfg is None or not self.refresh_token:
            return
        doc = {"apiKey": cfg["apiKey"], "databaseURL": cfg["databaseURL"], "email": cfg["email"],
               "uid": self.uid, "refreshToken": self.refresh_token}
        try:
            self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.credentials_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(self.credentials_path)
            self.remembered = True
        except OSError as exc:
            self.last_error = f"Conectado, mas não foi possível lembrar a conexão neste computador: {exc}"

    def _forget_credentials(self) -> None:
        try:
            self.credentials_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.remembered = False

    def auto_connect(self) -> bool:
        """Reconecta com a conexão lembrada. True quando conectou e está autorizado."""
        try:
            doc = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            base, query = normalize_database_url(doc["databaseURL"])
            api_key, refresh, email = str(doc["apiKey"]), str(doc["refreshToken"]), str(doc["email"])
        except FileNotFoundError:
            return False
        except (OSError, ValueError, KeyError, TypeError):
            with self.lock:
                self.last_error = "A conexão lembrada está inválida. Conecte o transmissor novamente."
            self._forget_credentials()
            return False
        with self.lock:
            self.cfg = {"apiKey": api_key, "base": base, "query": query, "databaseURL": doc["databaseURL"], "email": email}
            self.refresh_token = refresh
            self.uid = str(doc.get("uid", ""))
            self.remembered = True
            self.link = "retrying"
        try:
            self._refresh()
            allowed = self._request("GET", f"access/transmitters/{self.uid}")
        except SyncError as exc:
            msg = str(exc)
            if any(c in msg for c in ("INVALID_REFRESH_TOKEN", "TOKEN_EXPIRED", "USER_DISABLED", "USER_NOT_FOUND")):
                with self.lock:
                    self.cfg = None
                    self.link = "off"
                    self.last_error = "A conexão lembrada expirou ou a conta foi alterada. Conecte o transmissor novamente."
                self._forget_credentials()
                return False
            with self.lock:
                self.last_error = f"Aguardando internet para reconectar o transmissor: {msg[:120]}"
            return False
        except urllib.error.HTTPError as exc:
            allowed = None if exc.code in (401, 403) else False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            with self.lock:
                self.last_error = f"Aguardando internet para reconectar o transmissor. Os dados continuam no InfluxDB e no CSV. ({str(exc)[:100]})"
            return False
        with self.lock:
            self.authorized = allowed is True
            self.link = "ok" if self.authorized else "denied"
            self.last_error = "" if self.authorized else (
                f"Conta sem autorização de transmissor. Adicione access/transmitters/{self.uid} = true no Realtime Database.")
            self._save_credentials()   # o token de renovação pode ter mudado
        self.wake.set()
        return bool(self.authorized)

    def configure(self, data: dict[str, Any]) -> dict[str, str]:
        api_key = str(data.get("apiKey", "")).strip()
        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))
        base, query = normalize_database_url(str(data.get("databaseURL", "")))
        if not api_key or not email or not password:
            raise ValueError("Preencha chave de API, e-mail e senha do transmissor")
        if len(api_key) > 128 or len(email) > 254 or len(password) > 256:
            raise ValueError("Dados de acesso inválidos")
        self._sign_in(api_key, email, password)
        with self.lock:
            self.cfg = {"apiKey": api_key, "base": base, "query": query, "databaseURL": str(data.get("databaseURL", "")).strip(),
                        "email": email}
        try:
            allowed = self._request("GET", f"access/transmitters/{self.uid}")
        except urllib.error.HTTPError as exc:
            allowed = None if exc.code in (401, 403) else False
        except (urllib.error.URLError, OSError) as exc:
            with self.lock:
                self.cfg = None
            raise ValueError(f"Realtime Database não respondeu: {exc}") from exc
        with self.lock:
            self.authorized = allowed is True
            self.link = "ok" if self.authorized else "denied"
            self.last_error = "" if self.authorized else (
                f"Conta autenticada, mas sem autorização de transmissor. Adicione access/transmitters/{self.uid} = true no Realtime Database.")
            self.next_try = 0.0
            self.backoff = 0.0
        self.wake.set()
        if not self.authorized:
            return {"message": self.last_error}
        if data.get("remember", True) is not False:
            with self.lock:
                self._save_credentials()
            return {"message": "Transmissor conectado e lembrado neste computador. Nas próximas vezes o gateway se conecta sozinho."}
        self._forget_credentials()
        return {"message": "Transmissor autenticado e autorizado. A trajetória será publicada no site."}

    def disconnect(self) -> dict[str, str]:
        self._forget_credentials()
        with self.lock:
            self.cfg = None
            self.id_token = self.refresh_token = ""
            self.uid = ""
            self.authorized = None
            self.link = "off"
            self.last_error = ""
        return {"message": "Transmissor desconectado e esquecido neste computador. Recepção, CSV e InfluxDB continuam."}

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
            self._journal("pause")
        self.wake.set()
        return {"message": "Trajetória pausada. A gravação local continua; o site deixa de receber pontos."}

    def resume(self) -> dict[str, str]:
        with self.lock:
            self._require(("paused",), "retomada")
            self._set_status("resuming", "resumedAt")
            self._event("resume")
            self._journal("resume")
        self.wake.set()
        return {"message": "Trajetória retomada. O próximo ponto recebido volta a ser publicado."}

    def finish(self) -> dict[str, str]:
        with self.lock:
            t = self._require(("preparing", "running", "paused", "resuming"), "encerrada")
            self._set_status("finished", "finishedAt")
            self._event("finish")
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
                if exc.code == 403 or "Permission denied" in detail:
                    self.link = "denied"
                    self.last_error = "Firebase recusou a escrita (permissão ou validação). Confira as regras e access/transmitters."
                    self.backoff = 30.0
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
        retry_at, retry_wait = 0.0, 5.0
        while True:
            self.wake.wait(timeout=1.0)
            self.wake.clear()
            with self.lock:
                pending_login = self.remembered and not self.authorized and self.link != "denied"
            if pending_login and time.monotonic() >= retry_at:
                if self.auto_connect():
                    retry_wait = 5.0
                else:
                    retry_at, retry_wait = time.monotonic() + retry_wait, min(120.0, retry_wait * 2)
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
                "remembered": self.remembered,
                "link": self.link,
                "uid": self.uid,
                "database_url": self.cfg["databaseURL"] if self.cfg else "",
                "email": self.cfg["email"] if self.cfg else "",
                "pending": len(self.ops),
                "sent_points": self.sent_points,
                "failures": self.failures,
                "dropped": self.dropped,
                "last_ok_ms": self.last_ok_ms,
                "error": self.last_error,
                "trajectory": dict(t) if t else None,
            }
