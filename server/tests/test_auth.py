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
