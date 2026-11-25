"""Transmissão online: identidade do computador, autorização online, revogação, sessão,
fila offline, ordem e ausência de duplicação. O Firebase é imitado por tests/fake_rtdb.py.
"""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ecotam_secret  # noqa: E402
import ecotam_sync as sync  # noqa: E402
import fake_rtdb  # noqa: E402
from fake_rtdb import STORE  # noqa: E402


def sample(seq, t, fix=True, source="rx_serial"):
    rec = {"source": source, "device": "esp32_rx", "sequence": seq, "timestamp_ms": t,
           "altitude_m": 1137.5, "pressure_pa": 88385.12, "temperature_c": 29.36,
           "protocol_version": 3, "gps_fix": fix, "humidity_pct": 54.5}
    if fix:
        rec.update(latitude=-15.8308605, longitude=-48.057597, gps_altitude_m=1185.8, satellites=10)
    return rec


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fake_rtdb.serve(0)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        sync.ALLOW_LOCAL = True
        sync.AUTH_BASE = cls.base + "/identitytoolkit.googleapis.com/v1"
        sync.TOKEN_BASE = cls.base + "/securetoken.googleapis.com/v1"
        cls.public = {"apiKey": "chave-web-publica", "databaseURL": cls.base + "/?ns=canide-test"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        sync.ALLOW_LOCAL = False

    def setUp(self):
        STORE.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.antenna = {"source": "rx_serial", "state": "ok", "ageS": 0.4, "hasSample": True}
        self.s = self.make("pc-antena")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, session="20261124T120000_abcdef"):
        return sync.FirebaseSync(Path(self.tmp.name) / name, session, lambda: dict(self.antenna),
                                 identity_path=Path(self.tmp.name) / name / "identidade.json", public_config=self.public)

    # Administrador: aprova pela API do banco, como a página admin.html.
    def admin(self, method, path, body=None):
        req = urllib.request.Request(f"{self.base}/{path}.json?auth=tok-{fake_rtdb.ADMIN_UID}",
                                     data=None if body is None else json.dumps(body).encode(), method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read()

    def approve(self, s, name="PC da antena"):
        self.admin("PUT", f"access/transmitters/{s.uid}",
                   {"name": name, "approvedAt": {".sv": "timestamp"}, "approvedBy": fake_rtdb.ADMIN_UID})
        self.admin("DELETE", f"pairing/{s.uid}")

    def authorize(self, s=None):
        s = s or self.s
        self.assertEqual(s.authenticate(), "pending")
        self.approve(s)
        self.assertEqual(s.authenticate(), "authorized")
        return s

    def drain(self, s=None):
        s = s or self.s
        for _ in range(100):
            if not s.flush_once():
                break

    def live(self):
        return STORE.tree.get("live")

    def track(self):
        return STORE.tree["sessions"][self.live()["sessionId"]].get("track", {})


class IdentityTests(Base):
    def test_first_run_creates_own_identity_and_requests_authorization(self):
        self.assertEqual(self.s.authenticate(), "pending")
        st = self.s.status()["device"]
        self.assertTrue(st["uid"].startswith("anon-"))
        self.assertRegex(st["code"], r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        req = STORE.tree["pairing"][st["uid"]]
        self.assertEqual(req["code"], st["code"])
        self.assertEqual(req["computer"], self.s.computer)
        self.assertIn("pairing_request", [a["type"] for a in STORE.tree["audit"].values()])
        # Pendente: nada é publicado, mesmo com trajetória iniciada.
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000))
        self.drain()
        self.assertNotIn("live", STORE.tree)

    def test_identity_is_protected_at_rest_and_survives_restart(self):
        self.authorize()
        uid = self.s.uid
        stored = (Path(self.tmp.name) / "pc-antena" / "identidade.json").read_text(encoding="utf-8")
        if ecotam_secret.available():
            self.assertEqual(json.loads(stored)["format"], "dpapi-v1")
            self.assertNotIn("ref-", stored)            # token não legível no disco
        again = self.make("pc-antena", "20261125T080000_000000")   # reinício do computador
        self.assertEqual(again.authenticate(), "authorized")
        self.assertEqual(again.uid, uid)                  # mesma identidade, sem nova aprovação

    def test_each_computer_has_independent_identity(self):
        self.authorize()
        other = self.make("pc-reserva")
        self.assertEqual(other.authenticate(), "pending")
        self.assertNotEqual(other.uid, self.s.uid)        # aprovar um não autoriza o outro
        self.approve(other, "PC reserva")
        self.assertEqual(other.authenticate(), "authorized")

    def test_revocation_blocks_transmission_immediately(self):
        self.authorize()
        self.s.start()
        self.drain()
        self.assertEqual(self.live()["status"], "preparing")
        self.admin("DELETE", f"access/transmitters/{self.s.uid}")      # revogação remota
        self.s.on_sample(sample(1, 1_790_000_000_000))
        self.assertFalse(self.s.flush_once())
        st = self.s.status()
        self.assertEqual((st["device"]["state"], st["authorized"]), ("revoked", False))
        self.assertNotIn("track", STORE.tree["sessions"][self.live()["sessionId"]])
        self.assertEqual(self.s.authenticate(), "revoked")              # volta a pedir autorização
        self.assertIn(self.s.uid, STORE.tree["pairing"])
        self.approve(self.s)                                            # reautorizado: retoma a fila
        self.assertEqual(self.s.authenticate(), "authorized")
        self.s.next_try = 0
        self.drain()
        self.assertEqual(len(self.track()), 1)

    def test_deleted_account_gets_new_identity_needing_approval(self):
        self.authorize()
        old = self.s.uid
        urllib.request.urlopen(f"{self.base}/__control?delete_user={old}").read()
        self.s.token_exp = 0                                            # força renovação
        self.assertEqual(self.s.authenticate(), "revoked")
        self.assertEqual(self.s.authenticate(), "pending")              # nova identidade
        self.assertNotEqual(self.s.uid, old)

    def test_unauthorized_account_cannot_write(self):
        self.assertEqual(self.s.authenticate(), "pending")
        for path in ("live", f"access/transmitters/{self.s.uid}", "sessions/20261124T120000Z_abcdef/track/x"):
            req = urllib.request.Request(f"{self.base}/{path}.json?auth=tok-{self.s.uid}", data=b'{"a":1}', method="PUT")
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 401)

    def test_offline_at_boot_then_recovers(self):
        STORE.mode = "down"
        self.assertEqual(self.s.authenticate(), "offline")
        self.assertIn("Sem", self.s.status()["error"])
        STORE.mode = "ok"
        self.assertEqual(self.s.authenticate(), "pending")

    def test_session_renewal_on_expired_token(self):
        self.authorize()
        self.s.start()
        STORE.expire_next = True
        self.drain()
        self.assertEqual(self.live()["status"], "preparing")

    def test_reset_identity_requires_new_approval(self):
        self.authorize()
        old = self.s.uid
        self.s.reset_identity()
        self.assertEqual(self.s.authenticate(), "pending")
        self.assertNotEqual(self.s.uid, old)

    def test_unconfigured_without_public_config(self):
        s = sync.FirebaseSync(Path(self.tmp.name) / "x", "s", lambda: dict(self.antenna),
                              identity_path=Path(self.tmp.name) / "x" / "id.json")
        self.assertEqual(s.authenticate(), "unconfigured")

    def test_status_never_exposes_tokens(self):
        self.authorize()
        text = json.dumps(self.s.status())
        self.assertNotIn("tok-", text)
        self.assertNotIn("ref-", text)

    def test_database_url_only_firebase_in_production(self):
        sync.ALLOW_LOCAL = False
        try:
            self.assertEqual(sync.normalize_database_url("https://canide-default-rtdb.firebaseio.com")[0],
                             "https://canide-default-rtdb.firebaseio.com")
            for bad in ("http://canide-default-rtdb.firebaseio.com", "https://evil.com",
                        "https://canide.firebaseio.com/live", "http://127.0.0.1:9000/?ns=x",
                        "https://a.firebaseio.com.evil.com"):
                with self.assertRaises(ValueError, msg=bad):
                    sync.normalize_database_url(bad)
        finally:
            sync.ALLOW_LOCAL = True

    def test_identity_file_outside_repository_and_onedrive(self):
        self.assertNotIn("OneDrive", str(sync.IDENTITY_PATH))
        repo = Path(sync.__file__).resolve().parents[1]
        self.assertNotIn(str(repo), str(sync.IDENTITY_PATH))


class TelemetryTests(Base):
    def setUp(self):
        super().setUp()
        self.authorize()

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
        with self.assertRaises(ValueError):
            self.s.start(allow_sim=True)

    def test_normal_session_lifecycle(self):
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
        self.assertEqual((live["status"], live["points"], live["antenna"]["state"]), ("running", 5, "ok"))
        track = self.track()
        self.assertEqual([p["seq"] for _, p in sorted(track.items())], [100, 101, 102, 103, 104])
        first = track[sorted(track)[0]]
        self.assertEqual((first["lat"], first["t"], first["fix"]), (-15.8308605, base, True))
        self.s.pause()
        self.s.on_sample(sample(105, base + 5000))
        self.drain()
        self.assertEqual((self.live()["status"], len(self.track())), ("paused", 5))
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
        events = [e["type"] for _, e in sorted(STORE.tree["sessions"][sid]["events"].items())]
        self.assertEqual(events, ["start", "pause", "resume", "finish"])
        audit = [a["type"] for _, a in sorted(STORE.tree["audit"].items())]
        for kind in ("auth_ok", "trajectory_start", "trajectory_pause", "trajectory_resume", "trajectory_finish"):
            self.assertIn(kind, audit)
        self.assertTrue(all(a["uid"] == self.s.uid for a in STORE.tree["audit"].values()))
        journal = (Path(self.tmp.name) / "pc-antena" / "trajetorias.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["event"] for x in journal], ["start", "pause", "resume", "finish"])

    def test_no_fix_means_no_coordinates(self):
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000, fix=False))
        self.drain()
        point = next(iter(self.track().values()))
        self.assertFalse(point["fix"])
        for key in ("lat", "lon", "ga", "sat"):
            self.assertNotIn(key, point)

    def test_ignores_samples_from_other_source(self):
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000, source="sim"))
        self.drain()
        self.assertNotIn("track", STORE.tree["sessions"][self.live()["sessionId"]])

    def test_firebase_down_keeps_order_and_recovers_without_duplicates(self):
        self.s.start()
        self.drain()
        STORE.mode = "down"
        base = 1_790_000_000_000
        for i in range(900):
            self.s.on_sample(sample(i, base + i * 1000))
        self.assertFalse(self.s.flush_once())
        self.assertEqual(self.s.status()["link"], "retrying")
        STORE.mode = "ok"
        self.s.next_try = 0
        self.drain()
        track = self.track()
        self.assertEqual([p["seq"] for _, p in sorted(track.items())], list(range(900)))
        self.assertEqual([p["n"] for _, p in sorted(track.items())], list(range(1, 901)))
        self.assertEqual((self.live()["points"], self.s.status()["pending"]), (900, 0))

    def test_resend_after_ambiguous_failure_is_idempotent(self):
        self.s.start()
        self.s.on_sample(sample(1, 1_790_000_000_000))
        payload, _, _, _ = self.s._take_chunk()
        payload = {k: (dict(v, uid=self.s.uid) if k.startswith("audit/") else v) for k, v in payload.items()}
        self.s._request("PATCH", "", payload, {"print": "silent"})   # chegou, resposta perdida
        self.drain()
        self.assertEqual(len(self.track()), 1)

    def test_heartbeat_reports_antenna_loss(self):
        self.s.start()
        self.drain()
        self.antenna = {"source": "rx_serial", "state": "stale", "ageS": 9.0, "hasSample": False}
        self.s.last_heartbeat = 0
        self.assertTrue(self.s.flush_once())
        self.assertEqual(self.live()["antenna"], {"state": "stale", "ageS": 9.0})

    def test_overlapping_paths_split_into_separate_patches(self):
        self.s.start()
        self.s.finish()
        self.s.start()
        self.drain()
        self.assertEqual(self.live()["status"], "preparing")

    def test_restart_restores_open_trajectory(self):
        self.s.start()
        sid = self.s.status()["trajectory"]["id"]
        again = self.make("pc-antena", "20261124T130000_fedcba")
        self.assertEqual(again.status()["trajectory"]["id"], sid)

    def test_on_sample_never_touches_network(self):
        STORE.mode = "down"
        self.s.start()
        t0 = time.perf_counter()
        for i in range(2000):
            self.s.on_sample(sample(i, 1_790_000_000_000 + i))
        self.assertLess(time.perf_counter() - t0, 1.0)

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

    def test_gateway_loads_public_config_without_manual_setup(self):
        import ecotam_gateway as gateway
        self.assertIsNotNone(gateway.SYNC.cfg)                  # firebase.config.json do repositório
        self.assertNotIn("password", json.dumps(gateway.public_firebase_config()).lower())

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
