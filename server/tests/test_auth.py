import pytest

from server.auth import Auth, is_loopback


# --- loopback detection -----------------------------------------------------
# This decides whether a password is REQUIRED (see __main__), so a host it
# wrongly calls local would silently expose printer control to the network.

def test_loopback_hosts_are_recognised():
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "", None,
                 "  127.0.0.1  "):
        assert is_loopback(host) is True


def test_routable_hosts_are_not_loopback():
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", "::"):
        assert is_loopback(host) is False


# --- password checking ------------------------------------------------------

def test_the_right_password_is_accepted():
    assert Auth("hunter2").check_password("hunter2") is True


def test_a_wrong_password_is_rejected():
    a = Auth("hunter2")
    for bad in ("hunter", "hunter22", "", "HUNTER2", None, 42, ["hunter2"]):
        assert a.check_password(bad) is False


def test_an_empty_password_is_refused_at_construction():
    # An Auth with no secret would accept "" and be worse than no auth at all.
    for bad in ("", None):
        with pytest.raises(ValueError):
            Auth(bad)


def test_the_plaintext_password_is_not_retained():
    a = Auth("hunter2")
    assert "hunter2" not in repr(vars(a))


# --- sessions ---------------------------------------------------------------

def test_login_with_the_right_password_issues_a_token():
    a = Auth("hunter2")
    token = a.login("hunter2")
    assert token and a.valid(token) is True


def test_login_with_a_wrong_password_issues_nothing():
    a = Auth("hunter2")
    assert a.login("nope") is None


def test_tokens_are_unique_per_login():
    a = Auth("hunter2")
    assert a.login("hunter2") != a.login("hunter2")


def test_an_unknown_or_malformed_token_is_invalid():
    a = Auth("hunter2")
    for bad in ("", None, 42, "not-a-real-token"):
        assert a.valid(bad) is False


def test_logout_invalidates_only_that_session():
    a = Auth("hunter2")
    keep, drop = a.login("hunter2"), a.login("hunter2")
    a.logout(drop)
    assert a.valid(drop) is False
    assert a.valid(keep) is True


def test_logging_out_an_unknown_token_is_harmless():
    a = Auth("hunter2")
    a.logout("never-issued")   # must not raise


# --- the fail-closed rule ---------------------------------------------------
# The single most important property in this feature: you must not be able to
# expose printer control to the network by forgetting a flag.

from server.__main__ import build_auth


def test_binding_the_lan_without_a_password_refuses_to_start():
    for host in ("0.0.0.0", "192.168.1.10", "::"):
        with pytest.raises(SystemExit):
            build_auth(host, None)
        with pytest.raises(SystemExit):
            build_auth(host, "")


def test_binding_the_lan_with_a_password_builds_an_auth():
    a = build_auth("0.0.0.0", "hunter2")
    assert a is not None and a.check_password("hunter2") is True


def test_loopback_needs_no_password_and_stays_inert():
    # The desktop app and the dev workflow: auth is None -> every route open.
    for host in ("127.0.0.1", "localhost", "::1"):
        assert build_auth(host, None) is None


def test_a_password_on_loopback_is_still_honoured():
    # Opt in to a password even locally if you want one.
    assert build_auth("127.0.0.1", "hunter2") is not None


# --- --lan, the typing shortcut that must not become a security shortcut ----

from server.__main__ import (DEFAULT_HOST, LAN_HOST, lan_url_lines,
                             read_password_file, resolve_host,
                             resolve_password)


def test_lan_binds_the_world_and_no_flag_binds_loopback():
    assert resolve_host(None, True) == LAN_HOST
    assert resolve_host(None, False) == DEFAULT_HOST


def test_an_explicit_host_overrides_lan():
    # `--lan --host 192.168.1.5` narrows the bind; --lan must not widen it back.
    assert resolve_host("192.168.1.5", True) == "192.168.1.5"
    # And an explicit loopback stays loopback -- this is why --host defaults to
    # None rather than DEFAULT_HOST, so "given" is distinguishable from "not".
    assert resolve_host("127.0.0.1", True) == "127.0.0.1"


def test_password_file_is_read_and_stripped(tmp_path):
    f = tmp_path / ".bambu-password"
    f.write_text("hunter2\n", encoding="utf-8")
    assert read_password_file(f) == "hunter2"


def test_password_file_tolerates_a_bom_and_crlf(tmp_path):
    # A Windows editor adds both. A BOM glued to the front of a password is a
    # wrong password with no visible cause, which is worth a test.
    f = tmp_path / ".bambu-password"
    f.write_bytes(b"\xef\xbb\xbfhunter2\r\n")
    assert read_password_file(f) == "hunter2"


def test_password_file_uses_only_the_first_line(tmp_path):
    f = tmp_path / ".bambu-password"
    f.write_text("hunter2\nsome note about rotation\n", encoding="utf-8")
    assert read_password_file(f) == "hunter2"


def test_a_missing_or_blank_password_file_is_none(tmp_path):
    assert read_password_file(tmp_path / "nope") is None
    blank = tmp_path / "blank"
    blank.write_text("", encoding="utf-8")
    assert read_password_file(blank) is None
    spaces = tmp_path / "spaces"
    spaces.write_text("   \n", encoding="utf-8")
    assert read_password_file(spaces) is None


def test_the_environment_beats_the_password_file(tmp_path):
    f = tmp_path / ".bambu-password"
    f.write_text("from-file", encoding="utf-8")
    assert resolve_password("from-env", lan=True, password_file=f) == "from-env"


def test_without_lan_the_password_file_is_ignored(tmp_path):
    # Nothing about --lan may change how the documented BAMBU_PASSWORD path
    # behaves, including for the desktop build, which never passes --lan.
    f = tmp_path / ".bambu-password"
    f.write_text("from-file", encoding="utf-8")
    assert resolve_password(None, lan=False, password_file=f) is None


def test_lan_reads_the_password_file(tmp_path):
    f = tmp_path / ".bambu-password"
    f.write_text("from-file", encoding="utf-8")
    assert resolve_password(None, lan=True, password_file=f) == "from-file"


def test_lan_cannot_open_a_hole(tmp_path):
    """THE point of the flag's design. --lan is a shortcut for typing, not a
    way around the fail-closed rule: with no password anywhere it must still
    refuse to start, exactly as a bare `--host 0.0.0.0` would."""
    for missing in (tmp_path / "nope", tmp_path / "blank"):
        if missing.name == "blank":
            missing.write_text("  \n", encoding="utf-8")
        password = resolve_password(None, lan=True, password_file=missing)
        assert password is None
        with pytest.raises(SystemExit):
            build_auth(resolve_host(None, True), password)


def test_url_lines_mark_the_address_that_shares_a_printers_subnet():
    lines = lan_url_lines(["10.22.188.243", "192.168.137.30"], 8000,
                          ["192.168.137.152"])
    assert lines[0] == "http://10.22.188.243:8000"
    assert lines[1].startswith("http://192.168.137.30:8000")
    assert "same subnet" in lines[1]


def test_url_lines_mark_nothing_without_printers():
    lines = lan_url_lines(["10.22.188.243"], 8000, [])
    assert lines == ["http://10.22.188.243:8000"]


def test_url_lines_survive_a_non_ip_printer_host():
    # --mock's seeded printers have names, not addresses, as their host.
    lines = lan_url_lines(["10.22.188.243"], 8000, ["mock-bench"])
    assert lines == ["http://10.22.188.243:8000"]
