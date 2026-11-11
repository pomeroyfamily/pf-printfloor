"""Implicit FTPS (port 990) client used for Bambu job thumbnails.

Bambu P1/A1 FTPS is slow (~70-200 KB/s) and a job's .3mf can be 30 MB+ (verified).
Three ways to get one member out of a zip on the printer, fastest first:
  * ranged  - REST offsets: read only the zip directory + the member (P1P refuses REST: 502)
  * stream  - read from the start, parse local headers, hang up once the member arrived
  * full    - download everything (only if the zip uses data descriptors)
"""
from __future__ import annotations

import ftplib
import io
import logging
import ssl
import struct
import zipfile
import zlib
from typing import Callable, Optional

from .tls import insecure_context

log = logging.getLogger(__name__)


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP over implicit TLS: the control connection is TLS from the first byte."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def connect(host: str, user: str, password: str, port: int = 990, timeout: float = 20) -> ImplicitFTP_TLS:
    ftp = ImplicitFTP_TLS(context=insecure_context(), timeout=timeout)
    ftp.connect(host, port)
    ftp.login(user, password)
    ftp.prot_p()
    return ftp


# --------------------------------------------------------------------------- transfers
# ftplib's own retrlines/retrbinary call SSLSocket.unwrap() after a TLS data transfer; when a
# server closes without a TLS close_notify that raises *before* the 226 reply is read, and the
# control channel then answers every later command one reply late. These helpers read data
# until EOF, tolerate a missing close_notify, and re-sync the control channel with NOOP.
def _sync(ftp: ftplib.FTP) -> None:
    ftp.putcmd("NOOP")
    for _ in range(4):
        try:
            if ftp.getresp().startswith("200"):
                return
        except ftplib.all_errors:
            continue


def _transfer(ftp: ftplib.FTP, cmd: str, rest: Optional[int] = None, limit: Optional[int] = None) -> bytes:
    conn = ftp.transfercmd(cmd, rest=rest)
    buf = bytearray()
    try:
        while limit is None or len(buf) < limit:
            want = 65536 if limit is None else min(65536, limit - len(buf))
            try:
                chunk = conn.recv(want)
            except (ssl.SSLError, OSError):
                break                                  # EOF without close_notify / reset
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    try:
        ftp.voidresp()                                 # 226, or 426/451 if we stopped early
    except ftplib.all_errors:
        pass
    _sync(ftp)
    return bytes(buf)


def list_names(ftp: ftplib.FTP, folder: str) -> list[str]:
    try:
        ftp.voidcmd("TYPE A")
        raw = _transfer(ftp, f"NLST {folder}")
    except ftplib.error_perm:                          # 550: folder does not exist
        return []
    names = raw.decode(ftp.encoding or "utf-8", "replace").splitlines()
    return [n.rsplit("/", 1)[-1] for n in names if n.strip()]


# --------------------------------------------------------------------------- ranged reads
def read_range(ftp: ftplib.FTP, path: str, offset: int, length: int) -> bytes:
    """Read `length` bytes at `offset` (REST + RETR), then close the data connection early."""
    ftp.voidcmd("TYPE I")
    return _transfer(ftp, f"RETR {path}", rest=offset, limit=length)


def _parse_central_directory(tail: bytes, tail_offset: int, file_size: int) -> Optional[dict[str, tuple]]:
    """name -> (method, compressed_size, local_header_offset). None if tail is too short."""
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        return None
    cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:      # zip64 -> let caller fall back
        return None
    start = cd_offset - tail_offset
    if start < 0:
        return None
    cd = tail[start:start + cd_size]
    out, i = {}, 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", cd[i + 10:i + 12])[0]
        csize = struct.unpack("<I", cd[i + 20:i + 24])[0]
        nlen, xlen, clen = struct.unpack("<HHH", cd[i + 28:i + 34])
        lho = struct.unpack("<I", cd[i + 42:i + 46])[0]
        name = cd[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        out[name] = (method, csize, lho)
        i += 46 + nlen + xlen + clen
    return out


def zip_index_ranged(ftp: ftplib.FTP, path: str, tail_bytes: int = 256 * 1024) -> Optional[dict[str, tuple]]:
    ftp.voidcmd("TYPE I")          # many servers refuse SIZE in ASCII mode
    size = ftp.size(path)
    if not size:
        return None
    off = max(0, size - tail_bytes)
    return _parse_central_directory(read_range(ftp, path, off, size - off), off, size)


def zip_member_ranged(ftp: ftplib.FTP, path: str, entry: tuple) -> bytes:
    method, csize, lho = entry
    head = read_range(ftp, path, lho, 30)
    if head[:4] != b"PK\x03\x04":
        raise ValueError("bad local header")
    nlen, xlen = struct.unpack("<HH", head[26:30])
    data = read_range(ftp, path, lho + 30 + nlen + xlen, csize)
    if method == 0:
        return data
    if method == 8:
        return zlib.decompress(data, -15)
    raise ValueError(f"unsupported zip method {method}")


def download(ftp: ftplib.FTP, path: str) -> bytes:
    ftp.voidcmd("TYPE I")
    return _transfer(ftp, f"RETR {path}")


class ZipOnFtp:
    """Read members of a zip (.3mf) on the printer, ranged when possible."""

    def __init__(self, ftp: ftplib.FTP, path: str, allow_ranged: bool = True):
        self.ftp, self.path = ftp, path
        self.index: Optional[dict[str, tuple]] = None
        self._full: Optional[zipfile.ZipFile] = None
        self.mode = "ranged"
        self.fallback_reason: Optional[str] = None
        if allow_ranged:
            try:
                self.index = zip_index_ranged(ftp, path)
                if self.index is None:
                    self.fallback_reason = "zip directory not found in file tail (zip64 or unusual layout)"
            except ftplib.all_errors + (ValueError, struct.error, KeyError) as e:
                self.index = None
                self.fallback_reason = f"{type(e).__name__}: {e}"
                _sync(ftp)
            if self.fallback_reason:
                log.info("ranged read not possible for %s (%s) - downloading whole file", path,
                         self.fallback_reason)
        if self.index is None:
            self.mode = "full"
            self._full = zipfile.ZipFile(io.BytesIO(download(ftp, path)))

    def names(self) -> list[str]:
        return list(self.index) if self.index is not None else self._full.namelist()  # type: ignore[union-attr]

    def read(self, name: str) -> bytes:
        if self.index is not None:
            return zip_member_ranged(self.ftp, self.path, self.index[name])
        return self._full.read(name)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- streamed reads
class StreamNotPossible(Exception):
    """Entry can't be walked sequentially (unusual zip method/layout) - caller should download fully."""


ZipStop = Callable[[dict[str, bytes], list[str]], bool]
_SIGS = (b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b"PK\x06\x06")


def stream_zip_members(ftp: ftplib.FTP, path: str, keep: Callable[[str], bool], stop: ZipStop,
                       max_keep_bytes: int = 8 * 1024 * 1024, trace: Optional[list] = None
                       ) -> tuple[dict[str, bytes], list[str], int]:
    """Stream RETR from byte 0, parsing zip local headers on the fly.

    Keeps members for which keep(name) is True (decompressed), skips the rest, and closes the
    data connection as soon as stop(kept, names_seen) returns True or the zip directory starts.
    Handles data descriptors (sizes after the data, as Bambu Studio writes them) for deflated
    entries by inflating to find the end. Returns (kept, names_seen, bytes_read).
    """
    ftp.voidcmd("TYPE I")
    conn = ftp.transfercmd(f"RETR {path}")
    buf = bytearray()
    total = 0
    kept: dict[str, bytes] = {}
    names: list[str] = []
    eof = False

    def more() -> bool:
        nonlocal total, eof
        if eof:
            return False
        try:
            chunk = conn.recv(65536)
        except (ssl.SSLError, OSError):
            chunk = b""
        if not chunk:
            eof = True
            return False
        total += len(chunk)
        buf.extend(chunk)
        return True

    def need(n: int) -> bool:
        while len(buf) < n and more():
            pass
        return len(buf) >= n

    def skip(n: int) -> None:
        while n > 0:
            if not buf and not more():
                return
            drop = min(n, len(buf))
            del buf[:drop]
            n -= drop

    def inflate_to_end(want: bool) -> Optional[bytes]:
        """Inflate an entry of unknown size; leaves buf positioned right after its deflate stream."""
        d = zlib.decompressobj(-15)
        out = bytearray() if want else None
        while True:
            if not buf and not more():
                raise StreamNotPossible("file ended inside an entry")
            data = d.decompress(bytes(buf))
            if out is not None:
                out += data
                if len(out) > max_keep_bytes:
                    out = None
            if d.eof:
                rest = d.unused_data
                buf[:] = rest
                return bytes(out) if out is not None else None
            buf.clear()

    def skip_descriptor() -> None:
        need(4 + 4 + 16 + 4)
        off = 4 if bytes(buf[:4]) == b"PK\x07\x08" else 0
        for size_len in (off + 12, off + 20):          # 32-bit sizes, else zip64 sizes
            if bytes(buf[size_len:size_len + 4]) in _SIGS:
                del buf[:size_len]
                return
        del buf[:off + 12]

    def stored_to_end(want: bool) -> Optional[bytes]:
        """Stored entry of unknown size: its end is the 'PK\\x07\\x08' descriptor whose CRC and
        size match the bytes before it. Leaves buf positioned right after the descriptor."""
        out = bytearray() if want else None
        crc, n = 0, 0

        def take(k: int) -> None:
            nonlocal crc, n, out
            piece = bytes(buf[:k])
            crc = zlib.crc32(piece, crc)
            n += k
            if out is not None:
                out += piece
                if len(out) > max_keep_bytes:
                    out = None
            del buf[:k]

        while True:
            idx = buf.find(b"PK\x07\x08")
            if idx < 0:
                if len(buf) > 3:
                    take(len(buf) - 3)                 # keep a tail in case the signature is split
                if not more():
                    raise StreamNotPossible("file ended inside a stored entry")
                continue
            if not need(idx + 24) and len(buf) < idx + 16:
                raise StreamNotPossible("file ended inside a stored entry")
            d_crc, d_size32 = struct.unpack("<II", buf[idx + 4:idx + 12])
            d_size64 = struct.unpack("<Q", buf[idx + 8:idx + 16])[0] if len(buf) >= idx + 16 else -1
            if d_crc == zlib.crc32(bytes(buf[:idx]), crc) and \
                    (d_size32 == (n + idx) & 0xFFFFFFFF or d_size64 == n + idx):
                take(idx)
                skip_descriptor()
                return bytes(out) if out is not None else None
            take(idx + 1)                              # signature bytes inside the data: keep going

    try:
        while need(30):
            if bytes(buf[:4]) != b"PK\x03\x04":
                break                                   # central directory: no more entries
            at = total - len(buf)                       # file offset of this local header
            flags, method = struct.unpack("<HH", buf[6:10])
            csize = struct.unpack("<I", buf[18:22])[0]
            nlen, xlen = struct.unpack("<HH", buf[26:30])
            if not need(30 + nlen + xlen):
                break
            name = bytes(buf[30:30 + nlen]).decode("utf-8", "replace")
            extra = bytes(buf[30 + nlen:30 + nlen + xlen])
            del buf[:30 + nlen + xlen]
            names.append(name)
            if trace is not None:
                trace.append((name, at, method, flags))
            want = keep(name)
            if not want and stop(kept, names):
                break                                   # decided from the header: skip nothing more
            if flags & 0x08:                           # data descriptor: size unknown up front
                if method == 8:
                    data = inflate_to_end(want)
                elif method == 0:
                    data = stored_to_end(want)
                else:
                    raise StreamNotPossible(f"{name}: zip method {method}")
                if want and data is not None:
                    kept[name] = data
                if method == 8:
                    skip_descriptor()
            else:
                if csize == 0xFFFFFFFF:                 # zip64: real size in extra field 0x0001
                    i = 0
                    while i + 4 <= len(extra):
                        hid, hlen = struct.unpack("<HH", extra[i:i + 4])
                        if hid == 0x0001 and hlen >= 16:
                            csize = struct.unpack("<Q", extra[i + 12:i + 20])[0]
                            break
                        i += 4 + hlen
                if want and csize <= max_keep_bytes:
                    if not need(csize):
                        break
                    raw = bytes(buf[:csize])
                    del buf[:csize]
                    kept[name] = raw if method == 0 else zlib.decompress(raw, -15)
                else:
                    skip(csize)
            if stop(kept, names):
                break
    finally:
        conn.close()
        try:
            ftp.voidresp()
        except ftplib.all_errors:
            pass
        try:
            _sync(ftp)
        except (ftplib.all_errors + (EOFError,)):
            pass
    return kept, names, total
