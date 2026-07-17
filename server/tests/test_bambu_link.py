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
