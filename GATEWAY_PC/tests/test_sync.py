"""Transmissão online: sessão, autorização, fila offline, ordem e ausência de duplicação.

Um servidor local imita o Firebase Authentication e o REST do Realtime Database,
inclusive falhas (indisponível, token expirado, escrita negada).
"""
import http.server
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ecotam_sync as sync


def resolve(value):
    if isinstance(value, dict):
        if value == {".sv": "timestamp"}:
            return int(time.time() * 1000)
        return {k: resolve(v) for k, v in value.items()}
    return value


class FakeFirebase(http.server.BaseHTTPRequestHandler):
    tree = {}
    mode = "ok"            # ok | down | deny
    expire_next = False
    patches = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _node(self, path):
        node = self.tree
        for part in [p for p in path.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def do_POST(self):
        raw = self._body()
        if "accounts:signInWithPassword" in self.path:
            data = json.loads(raw)
            if data["password"] != "senha-correta":
                return self._send(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
            return self._send(200, {"idToken": "tok-1", "refreshToken": "ref-1", "expiresIn": "3600", "localId": "uid-tx"})
        if "/token" in self.path:
            if dict(urllib.parse.parse_qsl(raw.decode())).get("refresh_token", "")[:4] != "ref-":
                return self._send(400, {"error": {"message": "INVALID_REFRESH_TOKEN"}})
            return self._send(200, {"id_token": "tok-2", "refresh_token": "ref-2", "expires_in": "3600", "user_id": "uid-tx"})
        self._send(404, {})

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        with self.lock:
            self._send(200, self._node(url.path[:-5]))

    def do_PATCH(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(self._body())
        cls = self.__class__
        if cls.mode == "down":
            return self._send(503, {"error": "indisponível"})
        if cls.expire_next and query.get("auth") == "tok-1":
            cls.expire_next = False
            return self._send(401, {"error": "Auth token is expired"})
        if cls.mode == "deny":
            return self._send(403, {"error": "Permission denied"})
        with cls.lock:
            cls.patches.append(body)
            for path, value in body.items():
                parts = path.split("/")
                node = cls.tree
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = resolve(value)
        self._send(204)


def sample(seq, t, fix=True, source="rx_serial"):
    rec = {"source": source, "device": "esp32_rx", "sequence": seq, "timestamp_ms": t,
           "altitude_m": 1137.5, "pressure_pa": 88385.12, "temperature_c": 29.36,
           "protocol_version": 3, "gps_fix": fix, "humidity_pct": 54.5}
    if fix:
        rec.update(latitude=-15.8308605, longitude=-48.057597, gps_altitude_m=1185.8, satellites=10)
    return rec


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeFirebase)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.server.server_port}"
        sync.ALLOW_LOCAL = True
        sync.AUTH_BASE = base + "/identitytoolkit.googleapis.com/v1"
        sync.TOKEN_BASE = base + "/securetoken.googleapis.com/v1"
        cls.db = base + "/?ns=canide-test"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        sync.ALLOW_LOCAL = False

    def setUp(self):
        FakeFirebase.tree = {"access": {"transmitters": {"uid-tx": True}}}
        FakeFirebase.mode = "ok"
        FakeFirebase.expire_next = False
        FakeFirebase.patches = []
        self.tmp = tempfile.TemporaryDirectory()
        self.antenna = {"source": "rx_serial", "state": "ok", "ageS": 0.4, "hasSample": True}
        self.cred = Path(self.tmp.name) / "cred" / "transmissor.json"
        self.s = sync.FirebaseSync(Path(self.tmp.name), "20261124T120000_abcdef", lambda: dict(self.antenna), self.cred)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return self.s.configure({"apiKey": "chave-web", "databaseURL": self.db,
                                 "email": "tx@canide.test", "password": "senha-correta"})

    def drain(self):
        for _ in range(100):
            if not self.s.flush_once():
                break

    def live(self):
        return FakeFirebase.tree.get("live")

    def track(self):
        sid = self.live()["sessionId"]
        return FakeFirebase.tree["sessions"][sid].get("track", {})

    # ---------------------------------------------------------------- validação de entrada
    def test_database_url_only_firebase_in_production(self):
        sync.ALLOW_LOCAL = False
        try:
            self.assertEqual(sync.normalize_database_url("https://canide-default-rtdb.firebaseio.com")[0],
                             "https://canide-default-rtdb.firebaseio.com")
            self.assertEqual(sync.normalize_database_url("https://x.europe-west1.firebasedatabase.app/")[0],
                             "https://x.europe-west1.firebasedatabase.app")
            for bad in ("http://canide-default-rtdb.firebaseio.com", "https://evil.com",
                        "https://canide.firebaseio.com/live", "http://127.0.0.1:9000/?ns=x",
                        "https://a.firebaseio.com.evil.com"):
                with self.assertRaises(ValueError, msg=bad):
                    sync.normalize_database_url(bad)
        finally:
            sync.ALLOW_LOCAL = True

    def test_wrong_password_rejected_and_password_never_exposed(self):
        with self.assertRaises(ValueError):
            self.s.configure({"apiKey": "k", "databaseURL": self.db, "email": "tx@canide.test", "password": "errada"})
        self.connect()
        status = json.dumps(self.s.status())
        self.assertNotIn("senha-correta", status)
        self.assertNotIn("tok-1", status)
        self.assertNotIn("ref-1", status)

    def test_account_without_transmitter_permission_does_not_write(self):
        FakeFirebase.tree = {}
        out = self.connect()
        self.assertIn("sem autorização", out["message"])
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000))
        self.drain()
        self.assertEqual(FakeFirebase.patches, [])
        self.assertEqual(self.s.status()["pending"] > 0, True)

    # ---------------------------------------------------------------- início e checagens
    def test_start_requires_live_antenna_and_valid_data(self):
        self.antenna = {"source": "rx_serial", "state": "stale", "ageS": 12.0, "hasSample": True}
        with self.assertRaises(ValueError):
            self.s.start()
        self.antenna = {"source": "rx_serial", "state": "ok", "ageS": 0.2, "hasSample": False}
        with self.assertRaises(ValueError):
            self.s.start()
        self.antenna = {"source": "sim", "state": "simulated", "ageS": None, "hasSample": True}
        with self.assertRaises(ValueError):
            self.s.start()
        self.s.start(allow_sim=True)
        self.assertEqual(self.s.status()["trajectory"]["source"], "sim")
        with self.assertRaises(ValueError):
            self.s.start(allow_sim=True)

    def test_normal_session_lifecycle(self):
        self.connect()
        self.s.start()
        self.drain()
        live = self.live()
        self.assertEqual(live["status"], "preparing")
        self.assertRegex(live["sessionId"], r"^\d{8}T\d{6}Z_[0-9a-f]{6}$")
        base = 1_790_000_000_000
        for i in range(5):
            self.s.on_sample(sample(100 + i, base + i * 1000))
        self.drain()
        live = self.live()
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["points"], 5)
        self.assertEqual(live["antenna"]["state"], "ok")
        track = self.track()
        self.assertEqual([p["seq"] for _, p in sorted(track.items())], [100, 101, 102, 103, 104])
        self.assertEqual(sorted(track)[-1], live["lastKey"])
        first = track[sorted(track)[0]]
        self.assertEqual(first["lat"], -15.8308605)    # precisão original preservada
        self.assertEqual(first["t"], base)
        self.assertTrue(first["fix"])

        self.s.pause()
        self.s.on_sample(sample(105, base + 5000))   # pausada: não publica
        self.drain()
        self.assertEqual(self.live()["status"], "paused")
        self.assertEqual(len(self.track()), 5)
        self.s.resume()
        self.drain()
        self.assertEqual(self.live()["status"], "resuming")
        self.s.on_sample(sample(106, base + 6000))
        self.drain()
        self.assertEqual(self.live()["status"], "running")
        self.s.finish()
        self.drain()
        sid = self.live()["sessionId"]
        self.assertEqual(self.live()["status"], "finished")
        info = FakeFirebase.tree["sessions"][sid]["info"]
        for field in ("startedAt", "pausedAt", "resumedAt"):
            self.assertIsInstance(info[field], int)
        self.assertIn("finishedAt", info)
        events = [e["type"] for _, e in sorted(FakeFirebase.tree["sessions"][sid]["events"].items())]
        self.assertEqual(events, ["start", "pause", "resume", "finish"])
        journal = (Path(self.tmp.name) / "trajetorias.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["event"] for x in journal], ["start", "pause", "resume", "finish"])
        self.assertFalse((Path(self.tmp.name) / "trajetoria_ativa.json").exists())

    def test_no_fix_means_no_coordinates(self):
        self.connect()
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000, fix=False))
        self.drain()
        point = next(iter(self.track().values()))
        self.assertFalse(point["fix"])
        for key in ("lat", "lon", "ga", "sat"):
            self.assertNotIn(key, point)
        self.assertEqual(point["tc"], 29.36)

    def test_ignores_samples_from_other_source(self):
        self.connect()
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000, source="sim"))
        self.drain()
        self.assertEqual(self.track(), {})

    # ---------------------------------------------------------------- resiliência
    def test_firebase_down_keeps_order_and_recovers_without_duplicates(self):
        self.connect()
        self.s.start()
        self.drain()
        FakeFirebase.mode = "down"
        base = 1_790_000_000_000
        for i in range(900):        # vários lotes acumulados
            self.s.on_sample(sample(i, base + i * 1000))
        self.assertFalse(self.s.flush_once())
        st = self.s.status()
        self.assertEqual(st["link"], "retrying")
        self.assertEqual(st["pending"] >= 900, True)
        FakeFirebase.mode = "ok"
        self.s.next_try = 0
        self.drain()
        track = self.track()
        self.assertEqual(len(track), 900)
        seqs = [p["seq"] for _, p in sorted(track.items())]
        self.assertEqual(seqs, list(range(900)))
        ns = [p["n"] for _, p in sorted(track.items())]
        self.assertEqual(ns, list(range(1, 901)))
        self.assertEqual(self.live()["points"], 900)
        self.assertEqual(self.s.status()["pending"], 0)

    def test_resend_after_ambiguous_failure_is_idempotent(self):
        self.connect()
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000))
        payload, count, _, _ = self.s._take_chunk()
        # Simula um lote que chegou ao servidor mas cuja resposta se perdeu: reenviado igual.
        self.s._request("PATCH", "", payload, {"print": "silent"})
        self.drain()
        self.assertEqual(len(self.track()), 1)

    def test_expired_token_is_refreshed(self):
        self.connect()
        self.s.start()
        FakeFirebase.expire_next = True
        self.drain()
        self.assertEqual(self.live()["status"], "preparing")
        self.assertEqual(self.s.id_token, "tok-2")

    def test_denied_write_reported(self):
        self.connect()
        self.s.start()
        FakeFirebase.mode = "deny"
        self.assertFalse(self.s.flush_once())
        self.assertEqual(self.s.status()["link"], "denied")
        self.assertIn("recusou", self.s.status()["error"])

    def test_heartbeat_reports_antenna_loss(self):
        self.connect()
        self.s.start()
        self.drain()
        self.antenna = {"source": "rx_serial", "state": "stale", "ageS": 9.0, "hasSample": False}
        self.s.last_heartbeat = 0
        self.assertTrue(self.s.flush_once())
        self.assertEqual(self.live()["antenna"], {"state": "stale", "ageS": 9.0})
        self.assertIsInstance(self.live()["heartbeatAt"], int)

    def test_overlapping_paths_split_into_separate_patches(self):
        self.connect()
        self.s.start()
        self.s.finish()
        self.s.start()
        self.drain()
        for body in FakeFirebase.patches:
            keys = list(body)
            for a in keys:
                for b in keys:
                    self.assertFalse(a != b and b.startswith(a + "/"), (a, b))
        self.assertEqual(self.live()["status"], "preparing")

    def test_restart_restores_open_trajectory(self):
        self.s.start()
        sid = self.s.status()["trajectory"]["id"]
        again = sync.FirebaseSync(Path(self.tmp.name), "20261124T130000_fedcba", lambda: dict(self.antenna), self.cred)
        self.assertEqual(again.status()["trajectory"]["id"], sid)
        self.assertEqual(again.status()["trajectory"]["gatewaySession"], "20261124T130000_fedcba")

    def test_on_sample_never_touches_network(self):
        # Sem configuração e com o servidor fora do ar, gravar amostras continua instantâneo.
        FakeFirebase.mode = "down"
        self.s.start()
        t0 = time.perf_counter()
        for i in range(2000):
            self.s.on_sample(sample(i, 1_790_000_000_000 + i))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreaterEqual(self.s.status()["pending"], 2000)

    # ---------------------------------------------------------------- conexão lembrada
    def test_remembered_connection_reconnects_without_password(self):
        out = self.connect()
        self.assertIn("lembrado", out["message"])
        saved = self.cred.read_text(encoding="utf-8")
        self.assertNotIn("senha-correta", saved)
        self.assertNotIn("tok-1", saved)          # só o token de renovação
        self.assertIn("ref-1", saved)
        # Gateway reiniciado: nova instância conecta sozinha e publica.
        again = sync.FirebaseSync(Path(self.tmp.name), "20261124T140000_aaaaaa", lambda: dict(self.antenna), self.cred)
        self.assertTrue(again.remembered)
        self.assertTrue(again.auto_connect())
        self.assertEqual(again.status()["link"], "ok")
        again.start()
        while again.flush_once():
            pass
        self.assertEqual(self.live()["status"], "preparing")
        self.assertIn("ref-2", self.cred.read_text(encoding="utf-8"))   # token renovado regravado

    def test_offline_at_boot_keeps_remembered_connection(self):
        self.connect()
        again = sync.FirebaseSync(Path(self.tmp.name), "x", lambda: dict(self.antenna), self.cred)
        old = sync.TOKEN_BASE
        sync.TOKEN_BASE = "http://127.0.0.1:9/securetoken"      # porta fechada: sem internet
        try:
            self.assertFalse(again.auto_connect())
        finally:
            sync.TOKEN_BASE = old
        self.assertTrue(self.cred.exists())
        self.assertIn("Aguardando internet", again.status()["error"])
        self.assertTrue(again.auto_connect())

    def test_revoked_token_is_forgotten(self):
        self.connect()
        self.cred.write_text(self.cred.read_text(encoding="utf-8").replace("ref-1", "revogado"), encoding="utf-8")
        again = sync.FirebaseSync(Path(self.tmp.name), "x", lambda: dict(self.antenna), self.cred)
        self.assertFalse(again.auto_connect())
        self.assertFalse(self.cred.exists())
        self.assertIn("Conecte o transmissor novamente", again.status()["error"])

    def test_disconnect_forgets_and_opt_out_does_not_save(self):
        self.connect()
        self.s.disconnect()
        self.assertFalse(self.cred.exists())
        self.s.configure({"apiKey": "k", "databaseURL": self.db, "email": "tx@canide.test",
                          "password": "senha-correta", "remember": False})
        self.assertFalse(self.cred.exists())

    def test_queue_is_bounded(self):
        old = sync.MAX_OPS
        sync.MAX_OPS = 50
        try:
            self.s.start()
            for i in range(80):
                self.s.on_sample(sample(i, 1_790_000_000_000 + i))
            st = self.s.status()
            self.assertEqual(st["pending"], 50)
            self.assertGreater(st["dropped"], 0)
        finally:
            sync.MAX_OPS = old


class GatewayIntegrationTests(unittest.TestCase):
    def test_save_sample_records_locally_even_if_sync_fails(self):
        import ecotam_gateway as gateway

        class Broken:
            def on_sample(self, record):
                raise RuntimeError("falha simulada")

            def status(self):
                return {}

        old_sync, old_dir = gateway.SYNC, gateway.DADOS
        with tempfile.TemporaryDirectory() as tmp:
            gateway.SYNC, gateway.DADOS = Broken(), Path(tmp)
            try:
                before = gateway.STATE.count
                gateway.save_sample(gateway.build_simulated_sample(1))
                self.assertEqual(gateway.STATE.count, before + 1)
                self.assertTrue(any(Path(tmp).glob("ecotam_*.csv")))
            finally:
                gateway.SYNC, gateway.DADOS = old_sync, old_dir
                gateway.STATE.csv_error = ""

    def test_antenna_probe_distinguishes_states(self):
        import ecotam_gateway as gateway
        with gateway.STATE.lock:
            old = (gateway.STATE.source, gateway.STATE.rx_serial, gateway.STATE.rx_last_frame_time)
            try:
                gateway.STATE.source = "sim"
                self.assertEqual(gateway.antenna_probe()["state"], "simulated")
                gateway.STATE.source = "rx"
                gateway.STATE.rx_serial = None
                self.assertEqual(gateway.antenna_probe()["state"], "disconnected")

                class Port:
                    is_open = True
                gateway.STATE.rx_serial = Port()
                gateway.STATE.rx_last_frame_time = None
                self.assertEqual(gateway.antenna_probe()["state"], "none")
                gateway.STATE.rx_last_frame_time = time.monotonic() - 1
                self.assertEqual(gateway.antenna_probe()["state"], "ok")
                gateway.STATE.rx_last_frame_time = time.monotonic() - 30
                self.assertEqual(gateway.antenna_probe()["state"], "stale")
            finally:
                gateway.STATE.source, gateway.STATE.rx_serial, gateway.STATE.rx_last_frame_time = old


if __name__ == "__main__":
    unittest.main()
