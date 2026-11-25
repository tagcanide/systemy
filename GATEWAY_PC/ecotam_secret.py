"""Proteção da credencial do transmissor com o DPAPI do Windows.

O DPAPI cifra com AES-256 usando uma chave derivada da conta Windows e do computador.
O arquivo protegido só é decifrado pela mesma conta no mesmo computador: copiado para
outro lugar (OneDrive, pendrive, e-mail, repositório), não serve para nada.
Uma entropia própria do aplicativo impede que outro programa da mesma conta decifre o
arquivo por engano chamando o DPAPI sem esse complemento.

Somente biblioteca padrão (ctypes). Fora do Windows, available() é False.
"""
from __future__ import annotations

import base64
import ctypes
import os
import sys

FORMAT = "dpapi-v1"
_ENTROPY = b"CANIDE/ECOTAM transmissor firebase v1"
_UI_FORBIDDEN = 0x1


def available() -> bool:
    return sys.platform == "win32"


def this_computer() -> str:
    return (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").upper()


if available():
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(fn, data: bytes, protect: bool) -> bytes:
        src, _keep = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        # O 2º parâmetro é a descrição (entrada ao proteger, saída opcional ao decifrar).
        description = ctypes.c_wchar_p("CANIDE") if protect else None
        if not fn(ctypes.byref(src), description, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise ValueError(f"DPAPI falhou (erro {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> dict[str, str]:
    if not available():
        raise ValueError("Proteção DPAPI disponível só no Windows")
    return {"format": FORMAT, "data": base64.b64encode(_call(_crypt32.CryptProtectData, data, True)).decode("ascii")}


def unprotect(doc: dict) -> bytes:
    if doc.get("format") != FORMAT or not available():
        raise ValueError("Formato de proteção desconhecido")
    try:
        return _call(_crypt32.CryptUnprotectData, base64.b64decode(doc["data"]), False)
    except ValueError as exc:
        raise ValueError("Credencial protegida para outro computador ou outra conta do Windows. "
                         "Gere uma nova credencial e traga para este computador.") from exc
