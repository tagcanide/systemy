#!/usr/bin/env python3
"""ECOTAM PC Gateway - telemetria, histórico, qualidade (InfluxDB v2) e transmissão online.

Uses only Python's standard library for demo and InfluxDB. Optional pyserial
supports existing ECOTAM RX V2 diagnostic output when the radio is working.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import secrets
import sys

import ecotam_history
import ecotam_quality
import ecotam_sync
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BRAND_DIR = ROOT / "assets" / "brand"
BRAND_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff": "font/woff"}
DADOS = ROOT / "dados"
HOST = "127.0.0.1"
PORT = 8765
MEASUREMENT = "ecotam_telemetry"
SESSION_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)
FIELDS = [
    "time_iso", "source", "device", "sequence", "altitude_m", "pressure_pa",
    "temperature_c", "battery_v", "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z", "gps_simulated",
    "protocol_version", "gps_fix", "latitude", "longitude",
    "gps_altitude_m", "satellites", "humidity_pct",
]


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.source = "sim"
        self.port = "COM3"
        self.samples: deque[dict[str, Any]] = deque(maxlen=240)
        self.count = 0
        self.csv_error = ""
        self.config: dict[str, str] | None = None
        self.influx_ok = 0
        self.influx_failed = 0
        self.last_write = ""
        self.influx_error = ""
        self.influx_status = "Não configurado"
        self.rx_error = ""
        self.rx_serial: Any = None
        self.rx_thread: threading.Thread | None = None
        self.rx_stop = threading.Event()
        self.pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        self.queue_drops = 0
        # Diagnostico da comunicacao RX pela COM3 (sem alterar o protocolo).
        self.rx_lines = 0
        self.rx_headers = 0
        self.rx_frames = 0
        self.rx_last_line = ""
        self.rx_last_frame_time: float | None = None


STATE = AppState()
FIREBASE_CONFIG = ROOT / "firebase.config.json"


def antenna_probe() -> dict[str, Any]:
    """Estado da recepção para a transmissão online. Antena (RX) e internet são coisas distintas."""
    with STATE.lock:
        source = "rx_serial" if STATE.source == "rx" else "sim"
        last = next((s for s in reversed(STATE.samples) if s.get("source") == source), None)
        has_sample = last is not None and time.time() * 1000 - last["timestamp_ms"] < 5000
        if source == "sim":
            return {"source": "sim", "state": "simulated", "ageS": None, "hasSample": has_sample}
        age = (time.monotonic() - STATE.rx_last_frame_time) if STATE.rx_last_frame_time is not None else None
        port_open = STATE.rx_serial is not None and getattr(STATE.rx_serial, "is_open", False)
        if not port_open:
            state = "disconnected"
        elif age is None:
            state = "none"
        else:
            state = "ok" if age < ecotam_sync.ANTENNA_STALE_S else "stale"
        return {"source": source, "state": state, "ageS": age, "hasSample": has_sample}


SYNC = ecotam_sync.FirebaseSync(DADOS, SESSION_ID, antenna_probe)


def public_firebase_config() -> dict[str, str]:
    """Valores públicos (chave de API web, endereço do banco, site). Nunca contém senha."""
    try:
        data = json.loads(FIREBASE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(data[k]) for k in ("apiKey", "databaseURL", "publicSiteUrl") if isinstance(data.get(k), str)}


def build_simulated_sample(sequence: int) -> dict[str, Any]:
    """Non-flight synthetic sine waves for testing the data pipeline only."""
    t = sequence
    return {
        "source": "sim", "device": "pc_simulator", "sequence": sequence,
        "altitude_m": round(15 + 3.5 * math.sin(t / 13), 3),
        "pressure_pa": round(101325 - 42 * math.sin(t / 13), 2),
        "temperature_c": round(24 + 1.2 * math.sin(t / 27), 3),
        "battery_v": round(12 + 0.15 * math.cos(t / 21), 3),
        "accel_x": round(0.3 * math.sin(t / 8), 3),
        "accel_y": round(0.2 * math.cos(t / 9), 3),
        "accel_z": 9.81,
        "gyro_x": round(0.4 * math.sin(t / 11), 3),
        "gyro_y": round(0.3 * math.cos(t / 12), 3),
        "gyro_z": round(0.1 * math.sin(t / 10), 3),
        "gps_simulated": True,
    }


def save_sample(sample: dict[str, Any]) -> None:
    record = dict(sample)
    now = dt.datetime.now(dt.timezone.utc)
    record["timestamp_ms"] = int(now.timestamp() * 1000)
    record["session_id"] = SESSION_ID
    record["time_iso"] = now.isoformat(timespec="seconds")
    with STATE.lock:
        STATE.samples.append(record)
        STATE.count += 1
        try:
            DADOS.mkdir(exist_ok=True)
            suffix = "_v3" if record.get("protocol_version") == 3 else "_s17_v2"
            path = DADOS / ("ecotam_" + now.strftime("%Y%m%d") + suffix + ".csv")
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                writer.writerow(record)
            STATE.csv_error = ""
        except OSError as exc:
            STATE.csv_error = f"Não foi possível salvar CSV: {exc}"
        if STATE.config is not None:
            try:
                STATE.pending.put_nowait(record)
            except queue.Full:
                STATE.queue_drops += 1
        # Só enfileira em memória: o envio ao Firebase acontece em outra thread.
        try:
            SYNC.on_sample(record)
        except Exception as exc:  # a sincronização nunca interrompe a gravação local
            STATE.csv_error = STATE.csv_error or f"Falha na fila de sincronização: {exc}"


def simulation_loop() -> None:
    sequence = 0
    while True:
        time.sleep(1)
        with STATE.lock:
            active = STATE.source == "sim"
        if active:
            sequence += 1
            save_sample(build_simulated_sample(sequence))


class RxLogParser:
    """Parses RX V2 (4 lines) and V3 (5 lines). Raw coordinates remain local."""

    def __init__(self) -> None:
        self.current: dict[str, Any] = {}

    def feed(self, line: str) -> dict[str, Any] | None:
        m = re.search(r"\[RX\] OK seq=(\d+)\s+len=(64|84)\b", line)
        if m:
            version = 3 if int(m.group(2)) == 84 else 2
            self.current = {
                "source": "rx_serial", "device": "esp32_rx",
                "sequence": int(m.group(1)), "protocol_version": version,
                "gps_simulated": version == 2, # legacy V2 has no provenance metadata
            }
            return None
        if not self.current:
            return None
        m = re.search(r"\[RX\] altitude=([-+\d.]+) m \| pressure=([-+\d.]+) Pa \| temp=([-+\d.]+) C \| battery=([-+\d.]+) V", line)
        if m:
            for field, value in zip(("altitude_m", "pressure_pa", "temperature_c", "battery_v"), m.groups()):
                self.current[field] = float(value)
            return None
        m = re.search(r"\[RX\] accel=\(([-+\d.]+) ([-+\d.]+) ([-+\d.]+)\) \| gyro=\(([-+\d.]+) ([-+\d.]+) ([-+\d.]+)\)", line)
        if m:
            for field, value in zip(("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"), m.groups()):
                self.current[field] = float(value)
            if self.current.get("protocol_version") == 2:
                return self._finish()
            return None
        m = re.search(r"\[RX\] extras gps_fix=([01]) lat=([-+\d.]+) lon=([-+\d.]+) gps_alt=([-+\d.]+) satellites=(\d+) humidity=([-+\d.]+) humidity_valid=([01]) altitude_valid=([01])", line)
        if m and self.current.get("protocol_version") == 3:
            fix, lat, lon, alt, sat, humidity, hvalid, avalid = m.groups()
            self.current["gps_fix"] = fix == '1'
            if fix == '1':
                self.current["latitude"] = float(lat)
                self.current["longitude"] = float(lon)
                self.current["satellites"] = int(sat)
                if avalid == '1':
                    self.current["gps_altitude_m"] = float(alt)
            if hvalid == '1':
                self.current["humidity_pct"] = float(humidity)
            return self._finish()
        return None

    def _finish(self) -> dict[str, Any] | None:
        required = ("altitude_m", "pressure_pa", "temperature_c", "battery_v",
                    "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")
        if not all(field in self.current for field in required):
            self.current = {}
            return None
        result = self.current.copy()
        self.current = {}
        return result


def serial_loop(ser: Any, stop_event: threading.Event) -> None:
    parser = RxLogParser()
    try:
        while not stop_event.is_set():
            try:
                text = ser.readline().decode("utf-8", "replace")
            except Exception as exc:  # serial port can vanish when unplugged
                if not stop_event.is_set():
                    with STATE.lock:
                        STATE.rx_error = f"Falha de leitura na serial: {exc}"
                break
            stripped = text.strip()
            if stripped:
                with STATE.lock:
                    STATE.rx_lines += 1
                    if "[RX] OK seq=" in stripped:
                        STATE.rx_headers += 1
                    if stripped.startswith("[RX_STATS]") or stripped.startswith("[RX] OK"):
                        STATE.rx_last_line = stripped[:180]
            sample = parser.feed(text)
            if sample:
                with STATE.lock:
                    STATE.rx_frames += 1
                    STATE.rx_last_frame_time = time.monotonic()
                save_sample(sample)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def set_source(mode: str, port: str = "COM3") -> dict[str, str]:
    """Change sources without opening the same exclusive Windows COM port twice.

    On Windows, opening COM3 while our existing serial handle is open raises
    PermissionError(13). Re-applying the active source must therefore be a no-op.
    """
    if mode not in ("sim", "rx"):
        raise ValueError("Fonte inválida")
    if mode == "rx" and not re.fullmatch(r"COM\d{1,3}", port, flags=re.IGNORECASE):
        raise ValueError("Porta inválida; use COM3, por exemplo")
    port = port.upper()

    with STATE.lock:
        prior = STATE.rx_serial
        prior_thread = STATE.rx_thread
        # IMPORTANT: serial.Serial() must NOT be called again if we already
        # own this port. Windows serial handles are exclusive.
        if (mode == "rx" and STATE.source == "rx" and STATE.port == port
                and prior is not None and getattr(prior, "is_open", False)
                and prior_thread is not None and prior_thread.is_alive()):
            STATE.rx_error = ""
            return {"message": f"A {port} já está conectada ao gateway. A porta não foi reaberta."}

        old_stop = STATE.rx_stop
        old_stop.set()
        STATE.rx_serial = None
        STATE.rx_thread = None
        STATE.rx_stop = threading.Event()
        # State must reflect reality if the next open fails.
        STATE.source = "sim"
        STATE.port = port
        STATE.rx_error = ""
        STATE.rx_lines = 0
        STATE.rx_headers = 0
        STATE.rx_frames = 0
        STATE.rx_last_line = ""
        STATE.rx_last_frame_time = None

    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    if prior_thread is not None and prior_thread.is_alive() and prior_thread is not threading.current_thread():
        prior_thread.join(timeout=1.5)

    if mode == "sim":
        return {"message": "Fonte simulada ativada; a COM3 está liberada pelo gateway."}

    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise ValueError("Instale pyserial no Python usado pelo gateway (python -m pip install pyserial).") from exc
    try:
        new_serial = serial.Serial(port, 115200, timeout=0.5)
    except Exception as exc:
        detail = (f"Não foi possível abrir {port}: {exc}. Se o gateway acabou de iniciar, "
                  "outro programa ou um driver pode estar usando a porta. "
                  "Execute DIAGNOSTICAR_COM3.bat com o gateway fechado.")
        with STATE.lock:
            STATE.rx_error = detail
        raise ValueError(detail) from exc

    with STATE.lock:
        STATE.rx_serial = new_serial
        STATE.source = "rx"
        STATE.rx_error = ""
        stop_event = STATE.rx_stop
        thread = threading.Thread(target=serial_loop, args=(new_serial, stop_event), daemon=True)
        STATE.rx_thread = thread
    thread.start()
    return {"message": f"Conexão serial {port} aberta com sucesso. Aguardando pacotes do RX."}


def line_protocol(sample: dict[str, Any]) -> str:
    """All fields are known numeric values; tags are controlled literals."""
    device = sample["device"]
    source = sample["source"]
    if device not in ("pc_simulator", "esp32_rx") or source not in ("sim", "rx_serial"):
        raise ValueError("Unknown tag")
    names = ("altitude_m", "pressure_pa", "temperature_c", "battery_v",
             "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")
    fields = [f"{name}={float(sample[name]):.6f}" for name in names]
    fields.append(f"sequence={int(sample['sequence'])}i")
    fields.append(f"gps_simulated={'true' if sample.get('gps_simulated', False) else 'false'}")
    if 'protocol_version' in sample:
        fields.append(f"protocol_version={int(sample['protocol_version'])}i")
    if 'gps_fix' in sample:
        fields.append(f"gps_fix={'true' if sample['gps_fix'] else 'false'}")
    for name in ('latitude', 'longitude', 'gps_altitude_m', 'humidity_pct'):
        if name in sample and math.isfinite(float(sample[name])):
            fields.append(f"{name}={float(sample[name]):.7f}")
    if 'satellites' in sample:
        fields.append(f"satellites={int(sample['satellites'])}i")
    return (
        f"{MEASUREMENT},device={device},source={source},session_id={SESSION_ID} " + ",".join(fields)
        + " " + str(sample["timestamp_ms"])
    )


def influx_write(sample: dict[str, Any], config: dict[str, str]) -> int:
    params = urllib.parse.urlencode({
        "org": config["org"], "bucket": config["bucket"], "precision": "ms"
    })
    url = config["url"] + "/api/v2/write?" + params
    request = urllib.request.Request(
        url,
        data=line_protocol(sample).encode("utf-8"),
        headers={
            "Authorization": "Token " + config["token"],
            "Content-Type": "text/plain; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status


def influx_loop() -> None:
    while True:
        sample = STATE.pending.get()
        try:
            with STATE.lock:
                cfg = STATE.config.copy() if STATE.config else None
            if cfg is None:
                continue
            try:
                status = influx_write(sample, cfg)
                if status < 200 or status >= 300:
                    raise ValueError(f"HTTP {status}")
                with STATE.lock:
                    STATE.influx_ok += 1
                    STATE.last_write = dt.datetime.now().strftime("%H:%M:%S")
                    STATE.influx_error = ""
                    STATE.influx_status = "Gravando no InfluxDB"
            except urllib.error.HTTPError as exc:
                detail = exc.read(240).decode("utf-8", "replace")
                with STATE.lock:
                    STATE.influx_failed += 1
                    STATE.influx_error = f"HTTP {exc.code}: {detail}"
                    STATE.influx_status = "Erro de gravação"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                with STATE.lock:
                    STATE.influx_failed += 1
                    STATE.influx_error = str(exc)[:260]
                    STATE.influx_status = "Erro de gravação"
        finally:
            STATE.pending.task_done()


def normalize_influx_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("Por segurança, use apenas http://127.0.0.1:8086 ou http://localhost:8086")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Informe somente a URL base, sem caminho ou parâmetros")
    if parsed.port is None:
        raise ValueError("Informe uma porta (normalmente 8086)")
    return f"http://{parsed.hostname}:{parsed.port}"


def configure_influx(data: dict[str, Any]) -> dict[str, str]:
    url = normalize_influx_url(str(data.get("url", "")))
    org, bucket, token = [str(data.get(x, "")).strip() for x in ("org", "bucket", "token")]
    if not org or not bucket or not token:
        raise ValueError("Preencha organização, bucket e token")
    # A local-only health check diagnoses an inactive InfluxDB before saving the token.
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as response:
            if response.status >= 400:
                raise ValueError("InfluxDB não está saudável")
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError(f"InfluxDB não respondeu em {url}. Verifique se influxd está executando: {exc}") from exc
    with STATE.lock:
        STATE.config = {"url": url, "org": org, "bucket": bucket, "token": token}
        STATE.influx_error = ""
        STATE.influx_status = "Configurado; aguardando primeira gravação"
    return {"message": "Configuração recebida. A próxima amostra será enviada ao InfluxDB."}


def snapshot() -> dict[str, Any]:
    with STATE.lock:
        cfg = STATE.config
        return {
            "source": STATE.source, "port": STATE.port, "session_id": SESSION_ID,
            "count": STATE.count, "samples": list(STATE.samples)[-240:],
            "csv_error": STATE.csv_error,
            "csv_path": str(DADOS),
            "influx_configured": cfg is not None,
            "influx_url": cfg["url"] if cfg else "",
            "influx_org": cfg["org"] if cfg else "",
            "influx_bucket": cfg["bucket"] if cfg else "",
            "influx_status": STATE.influx_status,
            "influx_error": STATE.influx_error,
            "influx_writes": STATE.influx_ok,
            "influx_failed": STATE.influx_failed,
            "last_write": STATE.last_write,
            "queue_drops": STATE.queue_drops,
            "rx_error": STATE.rx_error,
            "rx_port_open": bool(STATE.rx_serial is not None and getattr(STATE.rx_serial, "is_open", False)),
            "rx_thread_alive": bool(STATE.rx_thread is not None and STATE.rx_thread.is_alive()),
            "rx_lines": STATE.rx_lines,
            "rx_headers": STATE.rx_headers,
            "rx_frames": STATE.rx_frames,
            "rx_last_line": STATE.rx_last_line,
            "rx_last_age_s": (round(time.monotonic() - STATE.rx_last_frame_time, 1)
                              if STATE.rx_last_frame_time is not None else None),
            "sync": SYNC.status(),
            "firebase_public": public_firebase_config(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ECOTAM-Gateway/18"

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.path.startswith("/api/status"):
            print("[HTTP] " + fmt % args, flush=True)

    def _respond(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, data: dict[str, Any]) -> None:
        self._respond(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _brand_asset(self) -> None:
        # Arquivos oficiais do kit CANIDÉ: só leitura, só tipos conhecidos, sem sair da pasta.
        rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)[len("/assets/brand/"):]
        target = (BRAND_DIR / rel).resolve()
        ctype = BRAND_TYPES.get(target.suffix.lower())
        if ctype is None or not target.is_relative_to(BRAND_DIR) or not target.is_file():
            return self._json(404, {"error": "Não encontrado"})
        self._respond(200, target.read_bytes(), ctype)

    def do_GET(self) -> None:
        if self.path == "/":
            self._respond(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/dashboard.js":
            self._respond(200, (ROOT / "dashboard.js").read_bytes(), "text/javascript; charset=utf-8")
        elif self.path == "/dashboard.css":
            self._respond(200, (ROOT / "dashboard.css").read_bytes(), "text/css; charset=utf-8")
        elif self.path in ("/historico", "/historico/"):
            self._respond(200, (ROOT / "historico.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/historico.js":
            self._respond(200, (ROOT / "historico.js").read_bytes(), "text/javascript; charset=utf-8")
        elif self.path in ("/qualidade", "/qualidade/"):
            self._respond(200, (ROOT / "qualidade.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/qualidade.js":
            self._respond(200, (ROOT / "qualidade.js").read_bytes(), "text/javascript; charset=utf-8")
        elif self.path.startswith("/assets/brand/"):
            self._brand_asset()
        elif self.path == "/api/status":
            self._json(200, snapshot())
        else:
            self._json(404, {"error": "Não encontrado"})

    def do_POST(self) -> None:
        # Block cross-site POSTs even though server is loopback-only.
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{HOST}:{PORT}":
            return self._json(403, {"error": "Origem não autorizada"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096 or length <= 0:
                return self._json(413, {"error": "Requisição inválida"})
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Objeto JSON esperado")
            if self.path == "/api/connect":
                result = configure_influx(body)
            elif self.path == "/api/disconnect":
                with STATE.lock:
                    STATE.config = None
                    STATE.influx_status = "Não configurado"
                    STATE.influx_error = ""
                result = {"message": "InfluxDB desconectado; dashboard e CSV continuam."}
            elif self.path == "/api/source":
                result = set_source(str(body.get("mode", "sim")), str(body.get("port", "COM3")))
            elif self.path == "/api/history":
                with STATE.lock:
                    cfg = STATE.config.copy() if STATE.config else None
                result = ecotam_history.history_result(cfg, body)
            elif self.path == "/api/quality":
                with STATE.lock:
                    cfg = STATE.config.copy() if STATE.config else None
                result = ecotam_quality.quality_result(cfg, body)
            elif self.path == "/api/sync/connect":
                result = SYNC.configure(body)
            elif self.path == "/api/sync/disconnect":
                result = SYNC.disconnect()
            elif self.path == "/api/trajectory":
                action = str(body.get("action", ""))
                if action == "start":
                    result = SYNC.start(allow_sim=body.get("allowSim") is True)
                elif action == "pause":
                    result = SYNC.pause()
                elif action == "resume":
                    result = SYNC.resume()
                elif action == "finish":
                    result = SYNC.finish()
                else:
                    raise ValueError("Ação de trajetória inválida")
            else:
                return self._json(404, {"error": "Não encontrado"})
            self._json(200, result)
        except urllib.error.HTTPError as exc:
            detail = exc.read(240).decode("utf-8", "replace")
            if exc.code in (401, 403):
                message = "InfluxDB HTTP %s: token inválido ou sem permissão READ no bucket. Refaça a conexão em Tempo real com token Read/Write." % exc.code
            else:
                message = f"InfluxDB HTTP {exc.code}: {detail}"
            self._json(502, {"error": message})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._json(502, {"error": f"Consulta ao InfluxDB falhou: {exc}"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"Falha local: {exc}"})


class GatewayServer(ThreadingHTTPServer):
    # No Windows, SO_REUSEADDR deixa um segundo gateway ouvir a mesma porta em
    # silêncio, e o processo antigo continua respondendo. Sem ele, a nova
    # instância falha com a mensagem "Feche o gateway anterior".
    allow_reuse_address = os.name != "nt"


def main() -> None:
    print("=" * 56)
    print(" ECOTAM | SPRINT 18 | TELEMETRIA ONLINE (FIREBASE)")
    print(" Fonte inicial: sim. Para sensores reais, selecione RX / COM3.")
    print(" CSV local automático. Token só fica na memória do processo.")
    print("=" * 56)
    if "--rx" in sys.argv:
        try:
            message = set_source("rx", "COM3")
            print("[RX] " + message["message"], flush=True)
        except ValueError as exc:
            print("[ERRO RX] " + str(exc), flush=True)
            print("Feche os programas que usam COM3 e tente novamente.", flush=True)
            return
    try:
        server = GatewayServer((HOST, PORT), Handler)
    except OSError as exc:
        print("[ERRO WEB] Nao foi possivel abrir a porta 8765: " + str(exc), flush=True)
        print("Feche o gateway anterior e execute novamente.", flush=True)
        with STATE.lock:
            if STATE.rx_serial is not None:
                STATE.rx_stop.set()
                STATE.rx_serial.close()
        return
    threading.Thread(target=simulation_loop, daemon=True).start()
    threading.Thread(target=influx_loop, daemon=True).start()
    threading.Thread(target=SYNC.run_forever, daemon=True).start()
    print(f"[OK] Abra http://{HOST}:{PORT} no navegador")
    print("[INFO] Histórico: http://127.0.0.1:8765/historico")
    print("[INFO] Qualidade: http://127.0.0.1:8765/qualidade")
    print("[INFO] Ctrl+C nesta janela para encerrar o servidor.")
    threading.Thread(target=lambda: (time.sleep(0.7), webbrowser.open(f"http://{HOST}:{PORT}")), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n[ECOTAM] Servidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
