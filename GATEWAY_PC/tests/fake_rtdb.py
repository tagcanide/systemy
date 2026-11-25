"""Duplo de teste do Firebase (Authentication REST + Realtime Database REST com streaming).

Implementa o suficiente do protocolo público para testar o sistema de ponta a ponta
sem a nuvem e sem o emulador oficial (que exige Java 21):

* Authentication: signUp anônimo (identidade de cada computador), signInWithPassword
  (administrador), renovação de token; contas podem ser apagadas (revogação);
* Realtime Database: GET (orderBy="$key", startAt), PUT, PATCH multi-caminho, DELETE,
  {".sv": "timestamp"} e streaming SSE (put/patch/keep-alive);
* permissões equivalentes às de firebase/database.rules.json (papéis admin e transmissor,
  pedidos de pareamento, auditoria só de acréscimo, leitura pública só de live e sessions/{id});
* CORS; /__control?mode=down simula indisponibilidade; /__control?delete_user=UID apaga a conta.

Não avalia o arquivo de regras em si: ele é validado no projeto real (ver docs).

Uso: python tests/fake_rtdb.py 9400
"""
from __future__ import annotations

import itertools
import json
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_UID = "admin@canide.test", "senha-admin", "uid-admin"


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.tree: dict = {"admins": {ADMIN_UID: True}}
        self.subs: list[tuple[str, str, queue.Queue]] = []
        self.mode = "ok"
        self.writes = 0
        self.users: dict[str, str] = {}          # refreshToken -> uid
        self.deleted: set[str] = set()
        self.counter = itertools.count(1)
        self.expire_next = False

    def node(self, path: str):
        node = self.tree
        for part in [p for p in path.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    @staticmethod
    def resolve(value):
        if isinstance(value, dict):
            if value == {".sv": "timestamp"}:
                return int(time.time() * 1000)
            out = {k: Store.resolve(v) for k, v in value.items()}
            return {k: v for k, v in out.items() if v is not None} or None
        return value

    def write(self, updates: dict) -> None:
        with self.lock:
            resolved = {}
            for path, value in updates.items():
                parts = [p for p in path.split("/") if p]
                value = self.resolve(value)
                node = self.tree
                for part in parts[:-1]:
                    if not isinstance(node.get(part), dict):
                        node[part] = {}
                    node = node[part]
                if value is None:
                    node.pop(parts[-1], None)
                else:
                    node[parts[-1]] = value
                resolved["/".join(parts)] = value
            self.writes += 1
            for sub_path, start_at, q in list(self.subs):
                self._notify(sub_path, start_at, q, resolved)

    def _notify(self, sub_path: str, start_at: str, q: queue.Queue, resolved: dict) -> None:
        rel_updates = {}
        whole = False
        for path, value in resolved.items():
            if sub_path == "" or path.startswith(sub_path + "/"):
                rel = path[len(sub_path):].lstrip("/")
                if start_at and rel.split("/")[0] < start_at:
                    continue
                rel_updates[rel] = value
            elif sub_path == path or sub_path.startswith(path + "/"):
                whole = True
        if whole:
            q.put(("put", {"path": "/", "data": self.filtered(sub_path, start_at)}))
        elif len(rel_updates) > 1 and all("/" not in rel for rel in rel_updates):
            q.put(("patch", {"path": "/", "data": rel_updates}))   # vários filhos diretos: um "patch"
        else:
            for rel, value in rel_updates.items():
                q.put(("put", {"path": "/" + rel, "data": value}))

    def filtered(self, path: str, start_at: str):
        node = self.node(path)
        if start_at and isinstance(node, dict):
            node = {k: v for k, v in node.items() if k >= start_at} or None
        return node

    # ---- papéis (como nas regras)
    def is_admin(self, uid) -> bool:
        return bool(uid) and self.node(f"admins/{uid}") is True

    def is_tx(self, uid) -> bool:
        return bool(uid) and self.node(f"access/transmitters/{uid}") is not None

    def can_read(self, uid, parts: list[str]) -> bool:
        if parts[:1] == ["live"] or (parts[:1] == ["sessions"] and len(parts) >= 2):
            return True
        if parts[:1] in (["pairing"], ["devices"], ["audit"]) or parts[:2] == ["access", "transmitters"]:
            own = (parts[:1] == ["pairing"] and len(parts) >= 2 and parts[1] == uid) or \
                  (parts[:2] == ["access", "transmitters"] and len(parts) >= 3 and parts[2] == uid)
            return own or self.is_admin(uid)
        if parts[:1] == ["admins"] and len(parts) >= 2:
            return parts[1] == uid
        return False

    def can_write(self, uid, path: str) -> bool:
        parts = [p for p in path.split("/") if p]
        head = parts[:1]
        if not uid:
            return False
        if head in (["live"], ["sessions"]):
            return self.is_tx(uid)
        if parts[:2] == ["access", "transmitters"] and len(parts) >= 3:
            return self.is_admin(uid)
        if head == ["pairing"] and len(parts) >= 2:
            return parts[1] == uid or self.is_admin(uid)
        if head == ["devices"] and len(parts) >= 2:
            return (parts[1] == uid and self.is_tx(uid)) or self.is_admin(uid)
        if head == ["audit"] and len(parts) == 2:
            return (self.node(path) is None and
                    (self.is_tx(uid) or self.is_admin(uid) or self.node(f"pairing/{uid}") is not None))
        return False


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, PATCH, POST, DELETE, OPTIONS")

    def _send(self, code: int, body=None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204)

    def _parse(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        return (path[:-5] if path.endswith(".json") else path).strip("/"), query

    def _uid(self, query) -> str | None:
        token = query.get("auth", "")
        if STORE.expire_next and token.startswith("tok-"):
            STORE.expire_next = False
            return "EXPIRED"
        uid = token[4:] if token.startswith("tok-") else None
        return None if uid in STORE.deleted else uid

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_POST(self):
        raw = self._body()
        if STORE.mode == "down":
            return self._send(503, {"error": {"message": "UNAVAILABLE"}})
        if "accounts:signUp" in self.path:
            uid = f"anon-{next(STORE.counter)}"
            STORE.users["ref-" + uid] = uid
            return self._send(200, {"idToken": "tok-" + uid, "refreshToken": "ref-" + uid, "expiresIn": "3600", "localId": uid})
        if "accounts:signInWithPassword" in self.path:
            data = json.loads(raw)
            if (data.get("email"), data.get("password")) != (ADMIN_EMAIL, ADMIN_PASSWORD):
                return self._send(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
            STORE.users["ref-" + ADMIN_UID] = ADMIN_UID
            return self._send(200, {"idToken": "tok-" + ADMIN_UID, "refreshToken": "ref-" + ADMIN_UID,
                                    "expiresIn": "3600", "localId": ADMIN_UID})
        if "/token" in self.path:
            ref = dict(urllib.parse.parse_qsl(raw.decode())).get("refresh_token", "")
            uid = STORE.users.get(ref)
            if uid is None or uid in STORE.deleted:
                return self._send(400, {"error": {"message": "USER_NOT_FOUND" if uid else "INVALID_REFRESH_TOKEN"}})
            return self._send(200, {"id_token": "tok-" + uid, "refresh_token": ref, "expires_in": "3600", "user_id": uid})
        self._send(404, {})

    def do_GET(self):
        path, query = self._parse()
        if path == "__control":
            if "mode" in query:
                STORE.mode = query["mode"]
            if "delete_user" in query:
                STORE.deleted.add(query["delete_user"])
            return self._send(200, {"mode": STORE.mode, "writes": STORE.writes})
        if path == "__dump":
            with STORE.lock:
                return self._send(200, STORE.tree)
        if STORE.mode == "down" and "auth" in query:
            return self._send(503, {"error": "Service Unavailable"})
        uid = self._uid(query)
        if uid == "EXPIRED":
            return self._send(401, {"error": "Auth token is expired"})
        if not STORE.can_read(uid, path.split("/") if path else []):
            return self._send(401, {"error": "Permission denied"})
        start_at = json.loads(query["startAt"]) if "startAt" in query else ""
        if "text/event-stream" in self.headers.get("Accept", ""):
            return self._stream(path, start_at)
        with STORE.lock:
            self._send(200, STORE.filtered(path, start_at))

    def _stream(self, path: str, start_at: str) -> None:
        q: queue.Queue = queue.Queue()
        with STORE.lock:
            STORE.subs.append((path, start_at, q))
            first = STORE.filtered(path, start_at)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self._event("put", {"path": "/", "data": first})
            while True:
                try:
                    kind, data = q.get(timeout=15)
                    self._event(kind, data)
                except queue.Empty:
                    self._event("keep-alive", None)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with STORE.lock:
                STORE.subs = [s for s in STORE.subs if s[2] is not q]

    def _event(self, kind: str, data) -> None:
        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _write(self, method: str):
        path, query = self._parse()
        raw = self._body()
        if STORE.mode == "down":
            return self._send(503, {"error": "Service Unavailable"})
        uid = self._uid(query)
        if uid == "EXPIRED":
            return self._send(401, {"error": "Auth token is expired"})
        if method == "PATCH":
            body = json.loads(raw)
            updates = {(f"{path}/{k}" if path else k): v for k, v in body.items()}
            keys = list(updates)
            if any(a != b and b.startswith(a + "/") for a in keys for b in keys):
                return self._send(400, {"error": "Path overlaps"})
        elif method == "PUT":
            updates = {path: json.loads(raw)}
        else:
            updates = {path: None}
        if not all(STORE.can_write(uid, p) for p in updates):
            return self._send(401, {"error": "Permission denied"})
        audit_ok = all(v is None or not p.startswith("audit/") or v.get("uid") == uid for p, v in updates.items())
        if not audit_ok:
            return self._send(400, {"error": "Validation failed"})
        STORE.write(updates)
        if query.get("print") == "silent":
            return self._send(204)
        self._send(200, None if method == "DELETE" else json.loads(raw) if raw else None)

    def do_PATCH(self):
        self._write("PATCH")

    def do_PUT(self):
        self._write("PUT")

    def do_DELETE(self):
        self._write("DELETE")


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # conexões encerradas pelo cliente são normais aqui
        pass


def serve(port: int) -> ThreadingHTTPServer:
    server = QuietServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 9400)
    print("[fake-rtdb] pronto", flush=True)
    threading.Event().wait()
