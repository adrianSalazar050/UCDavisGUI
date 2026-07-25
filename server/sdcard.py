"""Read-only microSD listing over the printer's FTPS server.

The MQTT protocol exposes NO file listing -- the only way to read the card is
FTPS on port 990 with IMPLICIT TLS, using the same bblp + access-code
credentials as MQTT. See:
https://github.com/Doridian/OpenBambuAPI/blob/main/ftp.md

Two things here cost people days:

  * IMPLICIT vs EXPLICIT TLS. ftplib.FTP_TLS speaks explicit TLS: it connects
    in plaintext and issues AUTH TLS. Bambu's server expects TLS from the very
    first byte. Hence ImplicitFTP_TLS below, which wraps the socket the moment
    it is assigned rather than after a command.
  * The cert is self-signed and its common name is the SERIAL, not the IP, so
    verification is off -- same trust model as bambu_link.py.

Everything testable (path guard, parsers, sorting) is a pure function. The
socket path (list_dir) is thin on purpose -- it cannot be exercised against
real hardware without it being connected -- but its *control flow* is still
tested by substituting a fake for ImplicitFTP_TLS, since that never opens a
socket either. See server/tests/test_sdcard.py.

list_dir() must ALWAYS raise SdError, never let a raw exception through: it
is called on a threadpool from a sync route (Task 6) with a user-controlled
path, and an unhandled exception there is an ugly 500 where a clean 400/502
belongs. SdError's message must also never contain the access code -- it is a
password, same rule as server/store.py.
"""
from __future__ import annotations

import datetime as dt
import ftplib
import io
import logging
import posixpath
import re
import ssl

log = logging.getLogger("server.sdcard")

FTPS_PORT = 990
FTP_USER = "bblp"
TIMEOUT_S = 10.0

# Bounded politeness window for the post-upload TLS unwrap, NOT a transfer
# timeout -- see ImplicitFTP_TLS.storbinary for why this exists at all. It
# only needs to be long enough for a well-behaved server's close_notify to
# arrive; on hardware that reply (when it comes at all) is near-instant, so
# 2s is generous headroom, not a tuned value that needs to survive a slow
# link.
UNWRAP_TIMEOUT_S = 2.0

# "-rw-r--r--  1 root root  1048576 Jul 16 13:05 Benchy.3mf"
#  type       links owner group size  month day time-or-year  name
_LIST_RE = re.compile(
    r"^([-dl])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"\w{3}\s+\d{1,2}\s+(?:\d{4}|\d{1,2}:\d{2})\s+(.+?)\s*$")

# C0 controls plus DEL. '\r'/'\n' are the load-bearing ones: ftplib sends a
# path verbatim as an FTP command argument, and a path containing them would
# either inject a second command onto the control channel or (since
# ftplib.FTP.putline() itself refuses any line containing '\r'/'\n') raise a
# bare ValueError deep inside list_dir -- NOT an ftplib.Error subclass, so it
# would escape uncaught instead of becoming a clean SdError. Reject the whole
# range here, at the boundary, instead of relying on that internal guard.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class SdError(Exception):
    """Anything that stopped us listing the card. Message is user-facing, so
    it must never contain the access code."""


def normalize_path(path: str | None) -> str:
    """Absolute, slash-normalised, no traversal.

    `..` is rejected outright rather than collapsed. posixpath.normpath would
    silently clamp '/a/../../b' to '/b', which is safe but hides a caller bug;
    a read-only endpoint is still no excuse for accepting an escaping path.
    """
    if path is not None and not isinstance(path, str):
        # Called from a route with user-controlled input. A wrong-typed
        # value must yield a clean SdError, not a raw AttributeError/
        # TypeError from .strip()/.replace() below.
        raise SdError(f"path must be a string, got {type(path).__name__}")
    raw = (path or "/").strip().replace("\\", "/")
    if _CONTROL_CHARS_RE.search(raw):
        raise SdError("path may not contain control characters")
    # Collapse repeats BEFORE normpath: POSIX says a leading "//" is
    # implementation-defined and posixpath.normpath deliberately preserves it,
    # so normpath("//a//b") is "//a/b", not "/a/b".
    raw = re.sub(r"/+", "/", raw)
    if not raw.startswith("/"):
        raw = "/" + raw
    if ".." in raw.split("/"):
        raise SdError("path may not contain '..'")
    return posixpath.normpath(raw)


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _mlsd_time(v) -> str | None:
    """MLSD 'modify' fact is YYYYMMDDHHMMSS -> ISO-8601."""
    try:
        return dt.datetime.strptime(str(v), "%Y%m%d%H%M%S").isoformat()
    except (TypeError, ValueError):
        return None


def sort_entries(entries: list[dict]) -> list[dict]:
    """Folders first, then case-insensitive by name."""
    return sorted(entries,
                 key=lambda e: (not e["is_dir"], str(e["name"]).lower()))


def parse_mlsd(pairs) -> list[dict]:
    """(name, facts) pairs from ftplib's mlsd() -> entry dicts.

    name/facts are printer-controlled. ftplib's own mlsd() always hands back
    a (str, dict) pair by construction, but that is an implementation detail,
    not a contract -- treat both defensively so a surprising line from some
    printer's FTPS server degrades to "skip/coerce", never to an
    AttributeError that would turn odd MLSD output into an unhandled 500.
    """
    out = []
    for name, facts in pairs:
        if not isinstance(facts, dict):
            facts = {}
        name = str(name)
        typ = str(facts.get("type") or "").lower()
        if typ in ("cdir", "pdir") or name in (".", ".."):
            continue
        is_dir = typ == "dir"
        out.append({
            "name": name,
            "is_dir": is_dir,
            "size": None if is_dir else _int_or_none(facts.get("size")),
            "mtime": _mlsd_time(facts.get("modify")),
        })
    return sort_entries(out)


def parse_list_lines(lines) -> list[dict]:
    """Unix-style LIST output -> entry dicts.

    Fallback for servers without MLSD. mtime is None here on purpose: LIST
    omits the year for recent files ("Jul 16 13:05"), and guessing it would
    produce confidently wrong dates. Names and sizes are what was asked for.
    """
    out = []
    for line in lines:
        m = _LIST_RE.match(line.strip())
        if not m:
            continue  # "total 24" banners and anything else non-conforming
        kind, size, name = m.group(1), m.group(2), m.group(3)
        if name in (".", ".."):
            continue
        is_dir = kind == "d"
        out.append({"name": name, "is_dir": is_dir,
                    "size": None if is_dir else _int_or_none(size),
                    "mtime": None})
    return sort_entries(out)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False          # cert CN is the serial, not the IP
    ctx.verify_mode = ssl.CERT_NONE     # self-signed; same as bambu_link.py
    return ctx


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS that negotiates TLS at connect time instead of after AUTH TLS,
    and reuses the control session on the data channel.

    ftplib has no implicit-TLS mode. The documented trick is to intercept the
    socket assignment and wrap it, which is what the `sock` property does.
    FTP_TLS.login() then skips its AUTH TLS step on its own, because it checks
    whether the socket is already an SSLSocket.
    """

    def __init__(self, *args, **kwargs):
        # BEFORE super().__init__: the base class's __init__ does
        # `self.sock = None` as one of its first statements, which lands in
        # the property setter below. Pre-seeding _sock means the getter can
        # never observe a missing attribute if anything reads self.sock
        # before that assignment runs.
        self._sock = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        """Wrap the data connection reusing the control connection's TLS
        session.

        Verified against this repo's Python 3.11.9: the stdlib's
        FTP_TLS.ntransfercmd calls wrap_socket WITHOUT `session=`, so every
        data transfer starts a brand-new TLS session. Servers configured to
        require session reuse (vsftpd's `require_ssl_reuse`, which several
        Bambu firmwares behave like) then accept the login and fail or hang the
        moment you LIST. Skipping straight to FTP.ntransfercmd and passing the
        session ourselves is the fix.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Same as ftplib.FTP_TLS.storbinary, except the post-transfer TLS
        unwrap is bounded and its failure is tolerated instead of fatal.

        Measured on a real A1, phase by phase, with the stdlib's unbounded
        unwrap() and a 120s socket timeout:

            connect      0.87s
            login        0.02s
            prot_p       0.03s
            transfercmd  0.28s
            sendall      1.74s     <- the data goes across fine
            unwrap     120.01s     <- TimeoutError: the read operation timed out
            close        0.00s
            voidresp     0.04s  -> "226"    <- the server said Transfer
                                                complete the whole time

        stdlib FTP_TLS.storbinary calls conn.unwrap() unconditionally and
        lets whatever it raises propagate. unwrap() blocks waiting for the
        peer's TLS close_notify on the data channel, and this printer never
        sends one -- so every upload "failed" after waiting out the full
        socket timeout, even though the file had already landed correctly
        (confirmed byte-identical: MD5 match, plus the .gcode.3mf's internal
        gcode checksum). Re-tested with the fix: short-timeout unwrap then
        close totalled 4.51s, response "226", file byte-identical.

        We still attempt unwrap() -- bounded to UNWRAP_TIMEOUT_S -- rather
        than skip it outright (also verified on hardware: 3.78s total, same
        clean result). unwrap() is what tells a well-behaved server the data
        stream ended deliberately rather than being truncated, and some FTPS
        servers require it before they will accept the file. Sending it and
        declining to wait long for a reply is correct for both kinds of
        server; skipping it entirely would be a gamble on every server that
        isn't this one. The real verdict on the upload was always the
        server's own reply below, via voidresp() -- that call is left
        completely alone, so a genuine STOR failure (e.g. "552 Disk full.")
        still raises and still becomes an SdError upstream.
        """
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while 1:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            _SSLSocket = ftplib._SSLSocket
            if _SSLSocket is not None and isinstance(conn, _SSLSocket):
                try:
                    conn.settimeout(UNWRAP_TIMEOUT_S)
                    conn.unwrap()
                except (OSError, ValueError):
                    pass
        return self.voidresp()


def list_dir(host: str, access_code: str, path: str = "/") -> list[dict]:
    """List one directory on the card. Always raises SdError on failure.

    Opens a fresh connection per call: this is an on-demand endpoint, not a
    poller, and holding an FTP session open against a printer that may power
    off mid-print buys nothing.
    """
    target = normalize_path(path)
    ftp = ImplicitFTP_TLS(context=_ssl_context(), timeout=TIMEOUT_S)
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        try:
            return parse_mlsd(list(ftp.mlsd(target)))
        except ftplib.error_perm as e:
            # Only "command not recognised" (500/502) means "this server has
            # no MLSD, fall back to LIST". Any other 5xx (550 = bad path,
            # 530 = not logged in, ...) is a real answer from the server;
            # retrying it as LIST would just repeat the same failure one
            # round trip later, and would report a confusingly different
            # message than the one that actually explains the failure.
            if str(e)[:3] not in ("500", "502"):
                raise
            lines: list[str] = []
            ftp.dir(target, lines.append)
            return parse_list_lines(lines)
    except SdError:
        raise  # can't currently originate inside this block; don't rewrap it
    except ftplib.all_errors as e:
        # Never interpolate access_code into this message. ftplib's own
        # exceptions carry only the server's reply text (e.g. "530 Login
        # incorrect."), which is never an echo of a credential we sent it --
        # so host + str(e) here is safe.
        raise SdError(f"Could not list {target} on {host}: {e}") from e
    except Exception as e:
        # Belt and suspenders: ftplib.all_errors is (Error, OSError, EOFError,
        # SSLError) and does NOT include every exception ftplib's own code
        # can raise -- notably putline() raises a bare ValueError for a
        # newline in a command argument. A stdlib quirk like that, or a bug
        # in the parsers above, must still surface as SdError.
        raise SdError(f"Could not list {target} on {host}: unexpected "
                      f"error ({type(e).__name__})") from e
    finally:
        try:
            ftp.close()
        except Exception:  # close() must never mask the real error above
            pass


def upload_file(host: str, access_code: str, path: str, data: bytes) -> None:
    """Upload one file to the card over FTPS (STOR). Same contract as
    fetch_file: always raises SdError on failure, and the message never
    contains the access code. Path is traversal-guarded via normalize_path.

    This is the only function in this package that WRITES to the card. It
    overwrites silently if the name already exists, because that is what STOR
    does and the printer offers no rename -- callers that care must list first.

    Uploading to a subdirectory is allowed by normalize_path but is usually a
    mistake: the verified start_print URL scheme is file:///sdcard/<filename>
    with no path component (master.md 5.4), so a file parked in a subdirectory
    can be listed but not started.
    """
    target = normalize_path(path)
    ftp = ImplicitFTP_TLS(context=_ssl_context(), timeout=TIMEOUT_S)
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        ftp.storbinary(f"STOR {target}", io.BytesIO(data))
    except SdError:
        raise
    except ftplib.all_errors as e:
        raise SdError(f"Could not upload {target} to {host}: {e}") from e
    except Exception as e:  # putline ValueError etc. -> clean SdError
        raise SdError(f"Could not upload {target} to {host}: unexpected "
                      f"error ({type(e).__name__})") from e
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def fetch_file(host: str, access_code: str, path: str) -> bytes:
    """Download one file off the card over FTPS. Always raises SdError on
    failure (same contract as list_dir); the message never contains the
    access code. Path is traversal-guarded via normalize_path."""
    target = normalize_path(path)
    ftp = ImplicitFTP_TLS(context=_ssl_context(), timeout=TIMEOUT_S)
    chunks: list[bytes] = []
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        ftp.retrbinary(f"RETR {target}", chunks.append)
        return b"".join(chunks)
    except SdError:
        raise
    except ftplib.all_errors as e:
        raise SdError(f"Could not fetch {target} on {host}: {e}") from e
    except Exception as e:  # putline ValueError etc. -> clean SdError
        raise SdError(f"Could not fetch {target} on {host}: unexpected "
                      f"error ({type(e).__name__})") from e
    finally:
        try:
            ftp.close()
        except Exception:
            pass
