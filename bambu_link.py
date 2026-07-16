"""
Minimal Bambu Lab LAN MQTT client.

Requires on the printer:  Settings -> LAN-only Mode  AND  Developer Mode.
Both. Without Developer Mode the MQTT channel is closed by the Authorization
Control System and this will connect but never receive a report.

Protocol notes that are NOT obvious and cost people days:

  * Port 8883, TLS, self-signed cert -> verification must be disabled.
  * Username is literally "bblp". Password is the 8-char LAN Access Code
    shown on the printer screen. It rotates on some firmware updates.
  * The printer sends PARTIAL updates. Most report messages contain only the
    fields that changed. You MUST deep-merge into a running state dict or you
    will see layer_num vanish every other message and conclude the printer is
    broken. It isn't. This is the single most common integration bug.
  * "pushall" returns the full state. Send it once on connect. Spamming it
    will get you throttled.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

MQTT_PORT = 8883
MQTT_USER = "bblp"


def decode_hms(attr: int, code: int) -> str:
    """Bambu HMS codes are packed into two 32-bit ints.

    Unpack to the AAAA_BBBB_CCCC_DDDD form used by the Bambu wiki lookup:
    https://wiki.bambulab.com/en/x1/troubleshooting/how-to-enter-hms-code
    """
    return (
        f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_"
        f"{code >> 16:04X}_{code & 0xFFFF:04X}"
    )


def deep_merge(base: dict, patch: dict) -> dict:
    """Merge a partial report into the running state.

    Lists are replaced wholesale, not merged elementwise -- the printer sends
    complete lists for `hms` and `ams.tray` when they change at all.
    """
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class BambuLink:
    """Connects, maintains merged state, fires callbacks on change.

    Callbacks:
        on_state(state: dict, patch: dict)   every report, after merge
        on_layer(layer_num: int, state: dict) only when layer_num increases
    """

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        on_state: Callable[[dict, dict], None] | None = None,
        on_layer: Callable[[int, dict], None] | None = None,
    ):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.on_state = on_state
        self.on_layer = on_layer

        self.state: dict[str, Any] = {}
        self._last_layer: int | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self.connected = threading.Event()

        self.topic_report = f"device/{serial}/report"
        self.topic_request = f"device/{serial}/request"

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-monitor-{int(time.time())}",
        )
        self.client.username_pw_set(MQTT_USER, access_code)
        # Printer presents a self-signed cert. There is no CA to pin against.
        # This is fine on a trusted LAN and is exactly why Bambu calls
        # Developer Mode "unsupported, you assume the risk".
        self.client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.tls_insecure_set(True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # ---------------- lifecycle ----------------

    def connect(self, timeout: float = 10.0) -> bool:
        self.client.connect(self.host, MQTT_PORT, keepalive=60)
        self.client.loop_start()
        return self.connected.wait(timeout)

    def disconnect(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    # ---------------- mqtt callbacks ----------------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("MQTT connect failed rc=%s (bad access code, or Developer "
                      "Mode is off)", rc)
            return
        log.info("connected to %s", self.host)
        client.subscribe(self.topic_report)
        self.connected.set()
        self.push_all()

    def _on_disconnect(self, client, userdata, *args):
        self.connected.clear()
        log.warning("disconnected")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            log.warning("non-JSON payload (%d bytes)", len(msg.payload))
            return

        patch = payload.get("print")
        if not isinstance(patch, dict):
            return  # info/system replies, not status

        with self._lock:
            deep_merge(self.state, patch)
            snapshot = json.loads(json.dumps(self.state))  # cheap deep copy

        if self.on_state:
            self.on_state(snapshot, patch)

        layer = snapshot.get("layer_num")
        if isinstance(layer, int) and layer != self._last_layer:
            # Only fire forward. Layer resets to 0 between prints; treat a
            # decrease as a new print, handled by the caller via gcode_state.
            prev = self._last_layer
            self._last_layer = layer
            if self.on_layer and (prev is None or layer > prev):
                self.on_layer(layer, snapshot)

    # ---------------- commands ----------------

    def _next_seq(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _publish(self, obj: dict) -> None:
        self.client.publish(self.topic_request, json.dumps(obj))

    def push_all(self) -> None:
        self._publish({"pushing": {"sequence_id": self._next_seq(),
                                   "command": "pushall"}})

    def send_gcode(self, line: str) -> None:
        """Send a raw G-code line.

        Whether the printer HONOURS a given line is a separate question --
        see probe_gcode.py. Publishing always 'succeeds'; the printer simply
        may ignore it. There is no ack.
        """
        if not line.endswith("\n"):
            line += "\n"
        self._publish({"print": {"sequence_id": self._next_seq(),
                                 "command": "gcode_line",
                                 "param": line}})

    # ---------------- convenience views ----------------

    def summary(self) -> dict:
        s = self.state
        hms = [decode_hms(h.get("attr", 0), h.get("code", 0))
               for h in s.get("hms", []) or []]
        return {
            "gcode_state": s.get("gcode_state"),
            "layer_num": s.get("layer_num"),
            "total_layer_num": s.get("total_layer_num"),
            "mc_percent": s.get("mc_percent"),
            "mc_remaining_time": s.get("mc_remaining_time"),
            "nozzle_temper": s.get("nozzle_temper"),
            "bed_temper": s.get("bed_temper"),
            "print_error": s.get("print_error"),
            "fail_reason": s.get("fail_reason"),
            "subtask_name": s.get("subtask_name"),
            "gcode_file": s.get("gcode_file"),
            "hms": hms,
        }
