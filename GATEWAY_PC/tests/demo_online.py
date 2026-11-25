#!/usr/bin/env python3
"""Demonstração local da transmissão online, sem antena e sem nuvem.

Sobe o duplo do Firebase, o gateway em 127.0.0.1:8765 e o site público montado
(porta 8080, ou outra livre se o Windows reservar essa), conectados entre si. Os pontos vêm de
uma gravação V3 já existente em dados/ (reproduzida a 1 Hz com horário atual) e
são marcados como fonte simulada; nada aqui é apresentado como antena real.

  python tests/demo_online.py            inicia uma trajetória automaticamente
  python tests/demo_online.py --manual   deixa o início para o painel do operador

Ctrl+C encerra. Abra o endereço do espectador em vários navegadores para ver a sincronização.
"""
from __future__ import annotations

import csv
import functools
import http.server
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "site"))

import build as site_build  # noqa: E402
import ecotam_gateway as gateway  # noqa: E402
import ecotam_sync  # noqa: E402
import fake_rtdb  # noqa: E402

NS = "demo-canide-default-rtdb"
RECORDING = sorted((ROOT / "dados").glob("ecotam_*_v3.csv"))


def replay_loop() -> None:
    rows = []
    for path in RECORDING:
        with path.open(encoding="utf-8") as fh:
            rows += list(csv.DictReader(fh))
    if not rows:
        print("[demo] nenhuma gravação V3 em dados/; use o simulador padrão", flush=True)
        return gateway.simulation_loop()
    num = lambda v: float(v) if v not in ("", None) else None  # noqa: E731
    i = 0
    while True:
        time.sleep(1)
        row = rows[i % len(rows)]
        i += 1
        sample = {"source": "sim", "device": "pc_simulator", "sequence": i, "gps_simulated": True, "protocol_version": 3,
                  "gps_fix": row.get("gps_fix") == "True"}
        for key in ("altitude_m", "pressure_pa", "temperature_c", "battery_v", "accel_x", "accel_y", "accel_z",
                    "gyro_x", "gyro_y", "gyro_z", "humidity_pct"):
            if num(row.get(key)) is not None:
                sample[key] = num(row[key])
        if sample["gps_fix"]:
            for key in ("latitude", "longitude", "gps_altitude_m"):
                if num(row.get(key)) is not None:
                    sample[key] = num(row[key])
            if row.get("satellites"):
                sample["satellites"] = int(float(row["satellites"]))
        gateway.save_sample(sample)


def bind(factory, port: int):
    try:
        return factory(port)
    except OSError:          # porta reservada ou em uso: usa uma livre
        return factory(0)


def main() -> None:
    manual = "--manual" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="canide_demo_"))
    rtdb = bind(fake_rtdb.serve, 9400)
    fb = f"http://127.0.0.1:{rtdb.server_port}"
    ecotam_sync.ALLOW_LOCAL = True
    ecotam_sync.AUTH_BASE = fb + "/identitytoolkit.googleapis.com/v1"
    ecotam_sync.TOKEN_BASE = fb + "/securetoken.googleapis.com/v1"
    gateway.DADOS = tmp
    gateway.SYNC = ecotam_sync.FirebaseSync(tmp, gateway.SESSION_ID, gateway.antenna_probe)
    gateway.FIREBASE_CONFIG = tmp / "firebase.config.json"

    site_dir = tmp / "site"
    site_build.build(site_dir, fb, NS)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site_dir))
    site = bind(lambda port: fake_rtdb.QuietServer(("127.0.0.1", port), handler), 8080)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    viewer_url = f"http://127.0.0.1:{site.server_port}/"

    gateway.FIREBASE_CONFIG.write_text(json.dumps({"apiKey": "chave-demo", "databaseURL": f"{fb}/?ns={NS}",
                                                   "publicSiteUrl": viewer_url}), encoding="utf-8")
    web = gateway.GatewayServer((gateway.HOST, gateway.PORT), gateway.Handler)
    for target in (web.serve_forever, replay_loop, gateway.influx_loop, gateway.SYNC.run_forever):
        threading.Thread(target=target, daemon=True).start()
    base = f"http://{gateway.HOST}:{gateway.PORT}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    print(post("/api/sync/connect", {"databaseURL": f"{fb}/?ns={NS}", "apiKey": "chave-demo",
                                     "email": "tx@canide.test", "password": "senha-correta"})["message"], flush=True)
    if not manual:
        time.sleep(2.5)
        print(post("/api/trajectory", {"action": "start", "allowSim": True})["message"], flush=True)
    print(f"[demo] operador: {base}   espectador: {viewer_url}   Ctrl+C encerra", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
