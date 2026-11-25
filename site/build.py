#!/usr/bin/env python3
"""Monta o site público estático (GitHub Pages) em uma pasta de saída.

Reaproveita os arquivos do gateway (dashboard.css, dashboard.js e o kit de marca)
sem copiá-los para o repositório: o site e o gateway usam a mesma identidade.

Uso:
  python site/build.py _site
  FIREBASE_DATABASE_URL=https://x-default-rtdb.firebaseio.com python site/build.py _site
  python site/build.py _site --database-url http://127.0.0.1:9000 --namespace canide   (emulador)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

SITE = Path(__file__).resolve().parent
REPO = SITE.parent
GATEWAY = REPO / "GATEWAY_PC"
SITE_FILES = ("index.html", "acompanhar.js", "sync-core.js", "admin.html", "admin.js")
PAGES = ("index.html", "admin.html")
SHARED_FILES = ("dashboard.css", "dashboard.js")
DEFAULT_DB = "https://canide-default-rtdb.firebaseio.com"
AUTH_ORIGINS = "https://identitytoolkit.googleapis.com https://securetoken.googleapis.com"


def default_api_key() -> str:
    """Chave de API web (pública) do mesmo arquivo usado pelo gateway."""
    try:
        return json.loads((GATEWAY / "firebase.config.json").read_text(encoding="utf-8")).get("apiKey", "")
    except (OSError, ValueError):
        return ""


def connect_origins(db_url: str) -> str:
    parts = urllib.parse.urlsplit(db_url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host.endswith(".firebaseio.com"):
        # O REST streaming pode redirecionar para outro servidor do mesmo domínio.
        return f"https://{host} https://*.firebaseio.com"
    if parts.scheme == "https" and host.endswith(".firebasedatabase.app"):
        return f"https://{host} https://*.firebasedatabase.app"
    if parts.scheme == "http" and host in ("127.0.0.1", "localhost"):
        return f"http://{host}:{parts.port}"
    raise SystemExit(f"Endereço do Realtime Database não suportado: {db_url}")


def build(out: Path, db_url: str, namespace: str = "", api_key: str = "") -> None:
    db_url = db_url.strip().rstrip("/")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in SITE_FILES:
        shutil.copy2(SITE / name, out / name)
    for name in SHARED_FILES:
        shutil.copy2(GATEWAY / name, out / name)
    shutil.copytree(GATEWAY / "assets" / "brand", out / "assets" / "brand")  # inclui as licenças das fontes

    config: dict[str, object] = {"databaseURL": db_url, "apiKey": api_key or default_api_key()}
    auth_src = AUTH_ORIGINS
    if db_url.startswith("http://"):          # duplo de teste local: autenticação no mesmo endereço
        config["allowLocal"] = True
        config["authBase"] = db_url + "/identitytoolkit.googleapis.com/v1"
        config["tokenBase"] = db_url + "/securetoken.googleapis.com/v1"
        auth_src = ""
        if namespace:
            config["namespace"] = namespace
    (out / "config.js").write_text(
        "/* Gerado por site/build.py. Somente valores públicos de leitura. */\n"
        f"window.CANIDE_CONFIG = Object.freeze({json.dumps(config, ensure_ascii=False)});\n",
        encoding="utf-8")

    refs: list[str] = []
    for page in PAGES:
        html = (out / page).read_text(encoding="utf-8")
        if "__CONNECT_SRC__" not in html:
            raise SystemExit(f"{page} sem o marcador __CONNECT_SRC__ da política de segurança")
        html = html.replace("__CONNECT_SRC__", connect_origins(db_url)).replace(" __AUTH_SRC__", " " + auth_src if auth_src else "")
        (out / page).write_text(html, encoding="utf-8")
        refs += re.findall(r'(?:href|src)="(?!https?:|#|data:)([^"]+)"', html)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    # Verificação: toda referência local do HTML existe na saída.
    missing = [r for r in refs if not (out / r).is_file()]
    if missing:
        raise SystemExit("Arquivos ausentes no site: " + ", ".join(missing))
    print(f"[site] {out} pronto · banco {db_url}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--database-url", default=os.environ.get("FIREBASE_DATABASE_URL") or DEFAULT_DB)
    ap.add_argument("--namespace", default=os.environ.get("FIREBASE_NAMESPACE", ""))
    ap.add_argument("--api-key", default=os.environ.get("FIREBASE_API_KEY", ""))
    args = ap.parse_args(argv)
    build(args.out.resolve(), args.database_url, args.namespace, args.api_key)


if __name__ == "__main__":
    main(sys.argv[1:])
