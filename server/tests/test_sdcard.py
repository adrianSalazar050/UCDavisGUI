import ftplib

import pytest

from server import sdcard
from server.sdcard import (SdError, normalize_path, parse_list_lines,
                           parse_mlsd, sort_entries)


# ---------- normalize_path ----------

def test_empty_path_is_root():
    assert normalize_path("") == "/"
    assert normalize_path(None) == "/"


def test_relative_path_becomes_absolute():
    assert normalize_path("timelapse") == "/timelapse"


def test_trailing_slash_removed():
    assert normalize_path("/timelapse/") == "/timelapse"


def test_backslashes_normalised():
    assert normalize_path("\\timelapse\\sub") == "/timelapse/sub"


def test_dotdot_is_rejected():
    for bad in ("/../etc", "/a/../../b", "..", "/timelapse/.."):
        with pytest.raises(SdError):
            normalize_path(bad)


def test_double_slash_collapsed():
    assert normalize_path("//a//b") == "/a/b"


def test_control_characters_rejected():
    # A raw '\r' or '\n' in a path handed to ftplib would land inside an FTP
    # command line. ftplib's own putline() blocks that with a bare
    # ValueError (not an ftplib.Error subclass -- see list_dir tests below),
    # so this must be rejected here as a clean SdError instead of relying on
    # that internal guard.
    for bad in ("/a\r\nDELE x", "/a\nb", "/a\rb", "/a\x00b", "/a\tb"):
        with pytest.raises(SdError):
            normalize_path(bad)


def test_non_string_path_raises_sderror_not_typeerror():
    # normalize_path is called from a route with user-controlled input; a
    # wrong-typed value must yield a clean SdError, never a raw
    # AttributeError/TypeError that would surface as an unhandled 500.
    for bad in (123, 12.5, [], {}, b"/timelapse"):
        with pytest.raises(SdError):
            normalize_path(bad)


def test_very_long_path_does_not_crash():
    long_path = "/" + "/".join(["a"] * 5000)
    # Should either normalize cleanly or raise SdError -- never a raw
    # exception. It contains no '..' so it must succeed.
    assert normalize_path(long_path).startswith("/a/a/a")


# ---------- parse_mlsd ----------

def test_parse_mlsd_file_and_dir():
    pairs = [
        ("timelapse", {"type": "dir", "modify": "20260716120000"}),
        ("Benchy.3mf", {"type": "file", "size": "1234",
                        "modify": "20260716130500"}),
    ]
    got = parse_mlsd(pairs)
    assert got[0] == {"name": "timelapse", "is_dir": True, "size": None,
                      "mtime": "2026-07-16T12:00:00"}
    assert got[1] == {"name": "Benchy.3mf", "is_dir": False, "size": 1234,
                      "mtime": "2026-07-16T13:05:00"}


def test_parse_mlsd_skips_cdir_and_pdir():
    pairs = [(".", {"type": "cdir"}), ("..", {"type": "pdir"}),
             ("real", {"type": "file", "size": "1"})]
    assert [e["name"] for e in parse_mlsd(pairs)] == ["real"]


def test_parse_mlsd_tolerates_missing_facts():
    got = parse_mlsd([("x.gcode", {})])
    assert got == [{"name": "x.gcode", "is_dir": False, "size": None,
                    "mtime": None}]


def test_parse_mlsd_bad_size_is_none():
    got = parse_mlsd([("x", {"type": "file", "size": "not-a-number"})])
    assert got[0]["size"] is None


def test_parse_mlsd_bad_modify_is_none():
    got = parse_mlsd([("x", {"type": "file", "modify": "garbage"})])
    assert got[0]["mtime"] is None


def test_parse_mlsd_tolerates_non_dict_facts():
    # facts comes from the printer's FTPS server; a malformed line could in
    # principle hand back something that isn't a dict. Must degrade to "no
    # facts", not raise AttributeError.
    got = parse_mlsd([("weird", "not-a-dict")])
    assert got == [{"name": "weird", "is_dir": False, "size": None,
                    "mtime": None}]


def test_parse_mlsd_tolerates_non_string_name():
    got = parse_mlsd([(123, {"type": "file", "size": "5"})])
    assert got[0]["name"] == "123"


# ---------- parse_list_lines ----------

def test_parse_list_unix_lines():
    lines = [
        "drwxr-xr-x    2 root     root         4096 Jul 16 12:00 timelapse",
        "-rw-r--r--    1 root     root      1048576 Jul 16 13:05 Benchy.3mf",
    ]
    got = parse_list_lines(lines)
    assert got[0] == {"name": "timelapse", "is_dir": True, "size": None,
                      "mtime": None}
    assert got[1] == {"name": "Benchy.3mf", "is_dir": False,
                      "size": 1048576, "mtime": None}


def test_parse_list_keeps_spaces_in_names():
    lines = ["-rw-r--r--    1 root root  12 Jul 16 13:05 my print v2.3mf"]
    assert parse_list_lines(lines)[0]["name"] == "my print v2.3mf"


def test_parse_list_skips_dot_entries_and_junk():
    lines = [
        "total 24",
        "drwxr-xr-x    2 root root 4096 Jul 16 12:00 .",
        "drwxr-xr-x    2 root root 4096 Jul 16 12:00 ..",
        "-rw-r--r--    1 root root   12 Jul 16 13:05 real.3mf",
        "this is not a listing line",
    ]
    assert [e["name"] for e in parse_list_lines(lines)] == ["real.3mf"]


def test_parse_list_year_form_date():
    lines = ["-rw-r--r--    1 root root   12 Jul 16  2025 old.3mf"]
    assert parse_list_lines(lines)[0]["name"] == "old.3mf"


# ---------- sort_entries ----------

def test_sort_puts_dirs_first_then_case_insensitive_name():
    entries = [
        {"name": "beta.3mf", "is_dir": False, "size": 1, "mtime": None},
        {"name": "Zulu", "is_dir": True, "size": None, "mtime": None},
        {"name": "alpha.3mf", "is_dir": False, "size": 1, "mtime": None},
        {"name": "acme", "is_dir": True, "size": None, "mtime": None},
    ]
    assert [e["name"] for e in sort_entries(entries)] == [
        "acme", "Zulu", "alpha.3mf", "beta.3mf"]


# ---------- list_dir (socket-free, via a fake FTP object) ----------
#
# The real socket/TLS path cannot be exercised without hardware and is
# deliberately untested. What CAN and must be tested without ever opening a
# socket is list_dir's control flow: it always constructs an
# ImplicitFTP_TLS, so substituting a fake in its place drives every branch
# with no network involved at all.

class _FakeFTP:
    """Stand-in for ImplicitFTP_TLS. Never touches a socket."""

    def __init__(self, *, login_effect=None, mlsd_effect=None,
                mlsd_pairs=None, dir_lines=None):
        self._login_effect = login_effect
        self._mlsd_effect = mlsd_effect
        self._mlsd_pairs = mlsd_pairs or []
        self._dir_lines = dir_lines or []
        self.dir_calls: list[str] = []
        self.closed = False

    def connect(self, host, port):
        pass

    def login(self, user, passwd):
        if self._login_effect is not None:
            raise self._login_effect

    def prot_p(self):
        pass

    def set_pasv(self, value):
        pass

    def mlsd(self, path):
        if self._mlsd_effect is not None:
            raise self._mlsd_effect
        return iter(self._mlsd_pairs)

    def dir(self, path, callback):
        self.dir_calls.append(path)
        for line in self._dir_lines:
            callback(line)

    def close(self):
        self.closed = True


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr(sdcard, "ImplicitFTP_TLS", lambda **kw: fake)


def test_list_dir_happy_path(monkeypatch):
    fake = _FakeFTP(mlsd_pairs=[
        ("timelapse", {"type": "dir"}),
        ("a.3mf", {"type": "file", "size": "10"}),
    ])
    _install_fake(monkeypatch, fake)
    got = sdcard.list_dir("10.0.0.5", "some-code", "/")
    assert [e["name"] for e in got] == ["timelapse", "a.3mf"]
    assert fake.closed


def test_list_dir_login_failure_does_not_leak_access_code(monkeypatch):
    secret = "S3CR3T-ACCESS-CODE"
    fake = _FakeFTP(login_effect=ftplib.error_perm("530 Login incorrect."))
    _install_fake(monkeypatch, fake)
    with pytest.raises(SdError) as ei:
        sdcard.list_dir("10.0.0.5", secret, "/")
    assert secret not in str(ei.value)
    assert fake.closed  # connection is always closed, even on failure


def test_list_dir_mlsd_500_falls_back_to_list(monkeypatch):
    fake = _FakeFTP(
        mlsd_effect=ftplib.error_perm("500 Unknown command MLSD."),
        dir_lines=["-rw-r--r--  1 root root  12 Jul 16 13:05 a.3mf"])
    _install_fake(monkeypatch, fake)
    got = sdcard.list_dir("10.0.0.5", "code", "/")
    assert [e["name"] for e in got] == ["a.3mf"]
    assert fake.dir_calls == ["/"]


def test_list_dir_mlsd_550_raises_directly_without_list_fallback(monkeypatch):
    # 550 (no such directory) is also an error_perm, but it is a real answer
    # from the server, not "MLSD unsupported" -- falling back to LIST would
    # just repeat the same failure a round trip later.
    fake = _FakeFTP(mlsd_effect=ftplib.error_perm("550 No such directory."))
    _install_fake(monkeypatch, fake)
    with pytest.raises(SdError):
        sdcard.list_dir("10.0.0.5", "code", "/nope")
    assert fake.dir_calls == []


def test_list_dir_unexpected_exception_becomes_sderror(monkeypatch):
    # Anything not in ftplib.all_errors (e.g. ftplib's own CRLF guard, which
    # raises a bare ValueError) must still come out as SdError, never raw.
    fake = _FakeFTP(mlsd_effect=ValueError("boom"))
    _install_fake(monkeypatch, fake)
    with pytest.raises(SdError):
        sdcard.list_dir("10.0.0.5", "code", "/")
    assert fake.closed


# ---------- fetch_file ----------

def test_fetch_file_returns_bytes(monkeypatch):
    import server.sdcard as sd

    class FakeFTP:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def login(self, *a, **k): pass
        def prot_p(self): pass
        def set_pasv(self, *a, **k): pass
        def retrbinary(self, cmd, cb):
            assert cmd == "RETR /Benchy.gcode.3mf"
            cb(b"PK\x03\x04zipbytes")
        def close(self): pass

    monkeypatch.setattr(sd, "ImplicitFTP_TLS", FakeFTP)
    assert sd.fetch_file("h", "code", "/Benchy.gcode.3mf") == b"PK\x03\x04zipbytes"


def test_fetch_file_rejects_traversal():
    import server.sdcard as sd
    import pytest
    with pytest.raises(sd.SdError):
        sd.fetch_file("h", "code", "/../etc/passwd")


# ---------- upload_file ----------
#
# The write counterpart of fetch_file, and the only route in this package that
# mutates the card. Same fake-FTP approach: no socket, no printer.

def test_upload_file_sends_stor_with_the_payload(monkeypatch):
    import server.sdcard as sd

    seen = {}

    class FakeFTP:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def login(self, *a, **k): pass
        def prot_p(self): pass
        def set_pasv(self, *a, **k): pass
        def storbinary(self, cmd, fp):
            seen["cmd"] = cmd
            seen["data"] = fp.read()
        def close(self): pass

    monkeypatch.setattr(sd, "ImplicitFTP_TLS", FakeFTP)
    sd.upload_file("h", "code", "/Benchy.gcode.3mf", b"PK\x03\x04zipbytes")
    assert seen["cmd"] == "STOR /Benchy.gcode.3mf"
    assert seen["data"] == b"PK\x03\x04zipbytes"


def test_upload_file_rejects_traversal():
    import server.sdcard as sd
    with pytest.raises(SdError):
        sd.upload_file("h", "code", "/../etc/passwd", b"x")


def test_upload_file_rejects_control_characters_in_name():
    # A \r or \n would otherwise reach ftplib.putline as a bare ValueError,
    # which is not an ftplib error subclass -- and on a STOR it is also a
    # command-injection shape. Guarded at the boundary, same as list_dir.
    import server.sdcard as sd
    with pytest.raises(SdError):
        sd.upload_file("h", "code", "/ok.3mf\r\nDELE important", b"x")


def test_upload_file_failure_does_not_leak_access_code_and_closes(monkeypatch):
    import server.sdcard as sd
    secret = "S3CR3T-ACCESS-CODE"
    state = {"closed": False}

    class FakeFTP:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def login(self, user, passwd):
            raise ftplib.error_perm("530 Login incorrect.")
        def prot_p(self): pass
        def set_pasv(self, *a, **k): pass
        def storbinary(self, *a, **k): pass
        def close(self): state["closed"] = True

    monkeypatch.setattr(sd, "ImplicitFTP_TLS", FakeFTP)
    with pytest.raises(SdError) as ei:
        sd.upload_file("h", secret, "/a.3mf", b"x")
    assert secret not in str(ei.value)
    assert state["closed"]


def test_upload_file_disk_full_becomes_sderror(monkeypatch):
    # The card fills up mid-STOR. Must surface as SdError like every other
    # failure, not as a raw ftplib exception escaping into a 500.
    import server.sdcard as sd

    class FakeFTP:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def login(self, *a, **k): pass
        def prot_p(self): pass
        def set_pasv(self, *a, **k): pass
        def storbinary(self, cmd, fp):
            raise ftplib.error_perm("552 Disk full.")
        def close(self): pass

    monkeypatch.setattr(sd, "ImplicitFTP_TLS", FakeFTP)
    with pytest.raises(SdError) as ei:
        sd.upload_file("h", "code", "/big.gcode.3mf", b"x" * 10)
    assert "552" in str(ei.value)
