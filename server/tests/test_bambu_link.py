import bambu_link


class FakeClient:
    """Captures publishes instead of touching a socket."""
    def __init__(self, *a, **k):
        self.published = []
    def username_pw_set(self, *a, **k): pass
    def tls_set(self, *a, **k): pass
    def tls_insecure_set(self, *a, **k): pass
    def publish(self, topic, payload): self.published.append((topic, payload))


def link(monkeypatch):
    monkeypatch.setattr(bambu_link.mqtt, "Client",
                        lambda *a, **k: FakeClient())
    return bambu_link.BambuLink("h", "SER", "code")


def test_stop_print_publishes_stop_command(monkeypatch):
    import json
    lk = link(monkeypatch)
    lk.stop_print()
    topic, payload = lk.client.published[-1]
    assert topic == "device/SER/request"
    body = json.loads(payload)
    assert body["print"]["command"] == "stop"
    assert "sequence_id" in body["print"]


def test_stop_print_never_carries_the_access_code(monkeypatch):
    lk = link(monkeypatch)
    lk.stop_print()
    _, payload = lk.client.published[-1]
    assert "code" not in payload


# --------------------------------------------------------------------------
# project_file: starting a print from a file already on the microSD.
#
# The url scheme below is VERIFIED against a real A1 mini (2026-07-19): the
# printer went FAILED -> PREPARE and reported the file as subtask_name. Both
# widely-cited public sources are wrong for this printer -- OpenBambuAPI's
# mqtt.md documents file:///mnt/sdcard (X1) and davglass/bambu-cli sends
# ftp:///<path>. Do not "correct" these tests to match those.
# --------------------------------------------------------------------------

from bambu_link import SD_URL_PREFIX, build_project_file_command


def test_project_file_targets_the_sdcard_url_verified_on_hardware():
    cmd = build_project_file_command("Benchy.gcode.3mf")["print"]
    assert cmd["command"] == "project_file"
    assert cmd["url"] == "file:///sdcard/Benchy.gcode.3mf"
    assert SD_URL_PREFIX == "file:///sdcard/"


def test_project_file_selects_the_plate():
    assert build_project_file_command("a.3mf", plate=3)["print"]["param"] \
        == "Metadata/plate_3.gcode"
    assert build_project_file_command("a.3mf")["print"]["param"] \
        == "Metadata/plate_1.gcode"


def test_project_file_strips_a_leading_slash():
    # sdcard.list_dir hands back paths that may be rooted; the url must not
    # end up with a doubled slash.
    cmd = build_project_file_command("/sub/dir/a.3mf")["print"]
    assert cmd["url"] == "file:///sdcard/sub/dir/a.3mf"


def test_project_file_subtask_name_defaults_to_the_filename():
    cmd = build_project_file_command("/x/smallCylinder.gcode.3mf")["print"]
    assert cmd["subtask_name"] == "smallCylinder"


def test_project_file_sends_both_leveling_spellings():
    # Which spelling the A1 firmware reads is unconfirmed, so send both with
    # the same value: the one it knows wins, the other is ignored.
    cmd = build_project_file_command("a.3mf", bed_leveling=True)["print"]
    assert cmd["bed_leveling"] is True and cmd["bed_levelling"] is True
    off = build_project_file_command("a.3mf", bed_leveling=False)["print"]
    assert off["bed_leveling"] is False and off["bed_levelling"] is False


def test_project_file_local_print_ids_are_zero():
    cmd = build_project_file_command("a.3mf")["print"]
    for k in ("project_id", "profile_id", "task_id", "subtask_id"):
        assert cmd[k] == "0", k
    assert cmd["bed_type"] == "auto"


def test_project_file_defaults_are_conservative():
    cmd = build_project_file_command("a.3mf")["print"]
    assert cmd["use_ams"] is False        # never assume an AMS is attached
    assert cmd["timelapse"] is False
    assert cmd["bed_leveling"] is True    # the one calibration worth the time


def test_start_print_publishes_the_command(monkeypatch):
    import json
    lk = link(monkeypatch)
    lk.start_print("a.3mf", plate=2)
    topic, payload = lk.client.published[-1]
    assert topic == "device/SER/request"
    body = json.loads(payload)
    assert body["print"]["command"] == "project_file"
    assert body["print"]["param"] == "Metadata/plate_2.gcode"
    assert body["print"]["sequence_id"]


def test_start_print_never_carries_the_access_code(monkeypatch):
    # A distinctive secret, not the shared fixture's "code": the payload
    # legitimately contains the substring "code" (Metadata/plate_1.gcode), so a
    # naive check would pass while a real leak slipped through.
    monkeypatch.setattr(bambu_link.mqtt, "Client", lambda *a, **k: FakeClient())
    lk = bambu_link.BambuLink("h", "SER", "SUPERSECRET42")
    lk.start_print("a.3mf")
    _, payload = lk.client.published[-1]
    assert "SUPERSECRET42" not in payload
