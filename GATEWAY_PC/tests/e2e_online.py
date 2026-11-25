#!/usr/bin/env python3
"""Teste ponta a ponta da transmissão online, com dados simulados e sem a nuvem.

Sobe o gateway real (HTTP, simulador, gravação CSV, envio ao InfluxDB e sincronização),
um InfluxDB falso, o duplo do Firebase (tests/fake_rtdb.py) e quatro espectadores
Node com EventSource (site/tests/viewer-sim.js). Cobre os testes 1 a 7 do plano:

  A, B  abertos antes do início (sessão normal, pausa e retomada)
  C     entra no meio da sessão (recupera o percurso anterior)
  D     perde a internet por 6 s e volta (recupera só o que faltou)
  Firebase indisponível por 20 s para o transmissor (CSV e InfluxDB continuam; fila reenviada)

Uso: python tests/e2e_online.py        (requer Node 22 ou superior)
"""
from __future__ import annotations

import csv
import http.server
import json
import subprocess
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

import ecotam_gateway as gateway  # noqa: E402
import ecotam_sync  # noqa: E402
import fake_rtdb  # noqa: E402

NS = "demo-canide-default-rtdb"


class FakeInflux(http.server.BaseHTTPRequestHandler):
    writes = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        FakeInflux.writes += 1
        self.send_response(204)
        self.end_headers()


def serve(handler) -> http.server.ThreadingHTTPServer:
    srv = fake_rtdb.QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="canide_e2e_"))
    rtdb = fake_rtdb.serve(0)
    fb = f"http://127.0.0.1:{rtdb.server_port}"
    influx = serve(FakeInflux)

    ecotam_sync.ALLOW_LOCAL = True
    ecotam_sync.AUTH_BASE = fb + "/identitytoolkit.googleapis.com/v1"
    ecotam_sync.TOKEN_BASE = fb + "/securetoken.googleapis.com/v1"
    gateway.DADOS = tmp
    gateway.SYNC = ecotam_sync.FirebaseSync(tmp, gateway.SESSION_ID, gateway.antenna_probe)
    web = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler)
    gateway.PORT = web.server_port
    base = f"http://127.0.0.1:{gateway.PORT}"
    for target in (web.serve_forever, gateway.simulation_loop, gateway.influx_loop, gateway.SYNC.run_forever):
        threading.Thread(target=target, daemon=True).start()

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": base})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def control(mode):
        urllib.request.urlopen(f"{fb}/__control?mode={mode}", timeout=5).read()

    def status():
        with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
            return json.loads(r.read())

    def csv_rows():
        rows = []
        for f in sorted(tmp.glob("ecotam_*.csv")):
            with f.open(encoding="utf-8") as fh:
                rows += list(csv.DictReader(fh))
        return rows

    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("[OK]   " if ok else "[FALHA] ") + name + (f" · {detail}" if detail else ""), flush=True)

    t0 = time.monotonic()
    at = lambda s: time.sleep(max(0, s - (time.monotonic() - t0)))  # noqa: E731

    print(f"[e2e] gateway {base} · firebase de teste {fb} · dados {tmp}", flush=True)
    code, out = post("/api/connect", {"url": f"http://127.0.0.1:{influx.server_port}", "org": "o", "bucket": "b", "token": "t"})
    check("InfluxDB (falso) conectado", code == 200, out.get("message", out.get("error", "")))

    # Teste de segurança: conta autenticada sem autorização não publica.
    code, out = post("/api/sync/connect", {"databaseURL": f"{fb}/?ns={NS}", "apiKey": "chave-web",
                                           "email": "intruso@canide.test", "password": "senha-intruso"})
    check("Conta sem autorização de transmissor é recusada", "sem autorização" in out.get("message", ""), out.get("message", ""))
    anon = urllib.request.Request(f"{fb}/live.json?ns={NS}", data=b'{"status":"running"}', method="PATCH")
    try:
        urllib.request.urlopen(anon, timeout=5)
        check("Escrita anônima bloqueada", False)
    except urllib.error.HTTPError as exc:
        check("Escrita anônima bloqueada", exc.code in (401, 403), f"HTTP {exc.code}")
    code, out = post("/api/sync/connect", {"databaseURL": f"{fb}/?ns={NS}", "apiKey": "chave-web",
                                           "email": "tx@canide.test", "password": "senha-correta"})
    check("Transmissor autenticado e autorizado", code == 200 and "autorizado" in out["message"], out["message"])

    node = ["node", "--experimental-eventsource", str(REPO / "site" / "tests" / "viewer-sim.js"), "--base", fb, "--ns", NS]
    viewers = {}

    def viewer(name, *extra):
        viewers[name] = subprocess.Popen(node + ["--name", name, "--timeout", "150000", *extra],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    viewer("A")
    viewer("B")
    viewer("D", "--drop-at", "9000", "--drop-for", "6000")
    at(3)
    code, out = post("/api/trajectory", {"action": "start"})
    check("Início recusado com fonte simulada sem permissão de teste", code == 400, out.get("error", ""))
    code, out = post("/api/trajectory", {"action": "start", "allowSim": True})
    check("Trajetória iniciada no transmissor", code == 200, out.get("message", out.get("error", "")))
    code, out = post("/api/trajectory", {"action": "start", "allowSim": True})
    check("Segunda trajetória simultânea recusada", code == 400, out.get("error", ""))
    at(9)
    viewer("C")                                         # novo espectador no meio da sessão
    at(12)
    check("Pausa", post("/api/trajectory", {"action": "pause"})[0] == 200)
    at(16)
    check("Retomada", post("/api/trajectory", {"action": "resume"})[0] == 200)
    at(20)
    control("down")                                     # Firebase indisponível para o transmissor
    csv_before, influx_before = len(csv_rows()), FakeInflux.writes
    at(40)
    st = status()
    check("Firebase fora: CSV continua gravando", len(csv_rows()) >= csv_before + 15, f"{len(csv_rows()) - csv_before} novas linhas")
    check("Firebase fora: InfluxDB continua gravando", FakeInflux.writes >= influx_before + 15, f"{FakeInflux.writes - influx_before} gravações")
    check("Firebase fora: fila de envio retém os pontos", st["sync"]["pending"] >= 15 and st["sync"]["link"] == "retrying",
          f"{st['sync']['pending']} operações pendentes · {st['sync']['link']}")
    control("ok")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and status()["sync"]["pending"]:
        time.sleep(0.5)
    check("Firebase de volta: fila esvaziada", status()["sync"]["pending"] == 0, status()["sync"]["link"])
    at(time.monotonic() - t0 + 4)
    check("Encerramento", post("/api/trajectory", {"action": "finish"})[0] == 200)

    reports = {}
    for name, proc in viewers.items():
        out, err = proc.communicate(timeout=60)
        try:
            reports[name] = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            reports[name] = {"error": err[-500:]}
    time.sleep(1)
    with urllib.request.urlopen(f"{fb}/__dump", timeout=5) as r:
        tree = json.loads(r.read())
    sid = tree["live"]["sessionId"]
    fb_track = tree["sessions"][sid]["track"]
    fb_keys = sorted(fb_track)
    n_values = [fb_track[k]["n"] for k in fb_keys]
    traj_points = status()["sync"]["trajectory"]["points"]
    check("Firebase: numeração contínua, sem lacunas nem duplicatas",
          n_values == list(range(1, len(fb_keys) + 1)) and len(fb_keys) == traj_points, f"{len(fb_keys)} pontos")
    check("Firebase: ordem cronológica", [fb_track[k]["t"] for k in fb_keys] == sorted(fb_track[k]["t"] for k in fb_keys))
    events = [e["type"] for _, e in sorted(tree["sessions"][sid]["events"].items())]
    check("Eventos de sessão na ordem", events == ["start", "pause", "resume", "finish"], ", ".join(events))

    rows = csv_rows()
    by_seq = {}
    for row in rows:
        by_seq.setdefault(int(row["sequence"]), row)
    exact = all(abs(float(by_seq[p["seq"]]["temperature_c"]) - p["tc"]) < 1e-9 and
                abs(float(by_seq[p["seq"]]["pressure_pa"]) - p["pa"]) < 1e-9 for p in fb_track.values())
    check("Precisão: cada ponto publicado é idêntico ao registro oficial (CSV)", exact)
    check("Registro oficial completo: InfluxDB recebeu todas as amostras do CSV",
          FakeInflux.writes == len(rows), f"CSV {len(rows)} · InfluxDB {FakeInflux.writes}")
    check("Pausa não publica pontos (registro oficial continua)", len(rows) > len(fb_keys), f"CSV {len(rows)} · publicados {len(fb_keys)}")

    for name, rep in reports.items():
        if "error" in rep:
            check(f"Espectador {name}", False, rep["error"])
            continue
        same = rep["keys"] == fb_keys
        check(f"Espectador {name}: mesma sessão e mesmo percurso do transmissor",
              same and rep["sessions"] == [sid] and rep["gaps"] == 0,
              f"{len(rep['keys'])}/{len(fb_keys)} pontos · repetidos descartados {rep['duplicates']} · "
              f"reconexões {rep['reconnects']} · estados {'>'.join(rep['statuses'])}")
    for name in ("A", "B"):
        seq = reports[name].get("statuses", [])
        ordered = [s for s in seq if s in ("running", "paused", "resuming", "finished")]
        check(f"Espectador {name}: acompanhou pausa, retomada e fim sem clicar em nada",
              "paused" in ordered and ordered.index("paused") < len(ordered) - 1 and ordered[-1] == "finished",
              ">".join(seq))
    check("Espectador C (entrou depois) recuperou o percurso anterior",
          reports.get("C", {}).get("keys", [None])[:1] == fb_keys[:1])

    failed = [r for r in results if not r[1]]
    print(f"\n[e2e] {len(results) - len(failed)}/{len(results)} verificações aprovadas", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
