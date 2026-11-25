"""Duplo de teste do Firebase (Authentication REST + Realtime Database REST com streaming).

Implementa o suficiente do protocolo público para testar o sistema de ponta a ponta
sem a nuvem e sem o emulador oficial (que exige Java 21):

* POST accounts:signInWithPassword / token (Authentication REST);
* GET .json (com orderBy="$key" e startAt), PATCH multi-caminho na raiz, {".sv": "timestamp"};
* GET com Accept: text/event-stream: eventos put/patch e keep-alive, como o REST streaming;
* escrita só com o token do UID autorizado; leitura de /sessions sem listagem (como as regras);
* CORS para o navegador; /__control?mode=down simula o Firebase fora do ar para o transmissor.

Não avalia o arquivo de regras: ele é validado no emulador oficial ou no projeto real.

Uso: python tests/fake_rtdb.py 9000
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERS = {"tx@canide.test": ("senha-correta", "uid-tx"), "intruso@canide.test": ("senha-intruso", "uid-intruso")}
TRANSMITTERS = {"uid-tx"}


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tree: dict = {"access": {"transmitters": {u: True for u in TRANSMITTERS}}}
        self.subs: list[tuple[str, str, queue.Queue]] = []   # (caminho, startAt, fila)
        self.mode = "ok"
        self.writes = 0

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

    def patch(self, updates: dict) -> None:
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
            q.put(("put", {"path": "/", "data": self._filtered(sub_path, start_at)}))
        elif len(rel_updates) > 1 and all("/" not in rel for rel in rel_updates):
            # Vários filhos diretos de uma vez: um único "patch", como o servidor real.
            q.put(("patch", {"path": "/", "data": rel_updates}))
        else:
            for rel, value in rel_updates.items():
                q.put(("put", {"path": "/" + rel, "data": value}))

    def _filtered(self, path: str, start_at: str):
        node = self.node(path)
        if start_at and isinstance(node, dict):
            node = {k: v for k, v in node.items() if k >= start_at} or None
        return node


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, OPTIONS")

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
        return token[4:] if token.startswith("tok-") else None

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if "accounts:signInWithPassword" in self.path:
            data = json.loads(raw)
            user = USERS.get(data.get("email"))
            if not user or user[0] != data.get("password"):
                return self._send(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
            return self._send(200, {"idToken": "tok-" + user[1], "refreshToken": "ref-" + user[1],
                                    "expiresIn": "3600", "localId": user[1]})
        if "/token" in self.path:
            ref = dict(urllib.parse.parse_qsl(raw.decode())).get("refresh_token", "")
            uid = ref[4:]
            return self._send(200, {"id_token": "tok-" + uid, "refresh_token": ref, "expires_in": "3600", "user_id": uid})
        self._send(404, {})

    def do_GET(self):
        path, query = self._parse()
        if path == "__control":
            STORE.mode = query.get("mode", "ok")
            return self._send(200, {"mode": STORE.mode, "writes": STORE.writes})
        if path == "__dump":
            with STORE.lock:
                return self._send(200, STORE.tree)
        # Leitura pública só em live e sessions/{id}; o UID lê a própria autorização.
        uid = self._uid(query)
        parts = path.split("/") if path else []
        readable = (parts[:1] == ["live"] or (parts[:1] == ["sessions"] and len(parts) >= 2)
                    or (parts[:2] == ["access", "transmitters"] and len(parts) == 3 and parts[2] == uid))
        if not readable:
            return self._send(401, {"error": "Permission denied"})
        start_at = json.loads(query["startAt"]) if "startAt" in query else ""
        if "text/event-stream" in self.headers.get("Accept", ""):
            return self._stream(path, start_at)
        with STORE.lock:
            self._send(200, STORE._filtered(path, start_at))

    def _stream(self, path: str, start_at: str) -> None:
        q: queue.Queue = queue.Queue()
        with STORE.lock:
            STORE.subs.append((path, start_at, q))
            first = STORE._filtered(path, start_at)
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

    def do_PATCH(self):
        path, query = self._parse()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if STORE.mode == "down":
            return self._send(503, {"error": "Service Unavailable"})
        if self._uid(query) not in TRANSMITTERS:
            return self._send(401, {"error": "Permission denied"})
        if path:
            body = {f"{path}/{k}": v for k, v in body.items()}
        keys = list(body)
        if any(a != b and b.startswith(a + "/") for a in keys for b in keys):
            return self._send(400, {"error": "Path overlaps"})
        STORE.patch(body)
        if query.get("print") == "silent":
            return self._send(204)
        self._send(200, body)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # conexões encerradas pelo cliente são normais aqui
        pass


def serve(port: int) -> ThreadingHTTPServer:
    server = QuietServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 9000)
    print("[fake-rtdb] pronto", flush=True)
    threading.Event().wait()
