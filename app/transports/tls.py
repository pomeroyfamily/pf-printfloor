"""TLS helpers for printers with self-signed certificates."""
from __future__ import annotations

import hashlib
import socket
import ssl
from typing import Optional


def insecure_context() -> ssl.SSLContext:
    """Printers use self-signed certs (Bambu: CN = serial). Encrypt, don't verify."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_cert(host: str, port: int, timeout: float = 8) -> Optional[bytes]:
    """The peer's certificate (DER), or None if it can't be reached."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with insecure_context().wrap_socket(raw) as tls:
                return tls.getpeercert(binary_form=True) or None
    except OSError:
        return None


def cert_fingerprint(der: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(der).hexdigest() if der else None


def peer_common_name(host: str, port: int, timeout: float = 8) -> Optional[str]:
    """Return the subject CN of the peer certificate (Bambu: the printer serial)."""
    return common_name(peer_cert(host, port, timeout) or b"")


def common_name(der: bytes) -> Optional[str]:
    cns, oid, i = [], b"\x06\x03\x55\x04\x03", 0
    while (i := der.find(oid, i)) != -1:          # OID 2.5.4.3 commonName
        j = i + len(oid)
        if j + 2 <= len(der) and der[j] in (0x0C, 0x13, 0x14):
            ln = der[j + 1]
            cns.append(der[j + 2:j + 2 + ln].decode("utf-8", "replace"))
        i = j
    for cn in reversed(cns):                       # subject CN comes after issuer CN
        if cn.isalnum() and 10 <= len(cn) <= 20:
            return cn.upper()
    return None
