"""
Does the A1 mini honour live parameter overrides sent over MQTT?

This is the experiment that decides whether CAXTON-style self-labelling is
available to you on this platform. CAXTON (Brion & Pattinson, Nat Commun 2022)
built 1.2M auto-labelled images by perturbing flow / speed / Z / temperature
mid-print and labelling every frame with the deviation. That trick needs the
printer to accept the perturbation. Bambu's own forum has a standing feature
request to allow flow change during printing, which implies the answer is no
for M221 -- but "implies" is not "measured", and it costs one afternoon.

Run this DURING a print, on a big flat part (a 100x100x20 mm calibration cube
or a single-wall vase) so a flow change is visually obvious.

    python probe_gcode.py --host 192.168.1.42 --serial 0309xxxxxxxx \
                          --access-code 12345678

There is no ack. The printer never says "I ignored that". So the probe:
  1. records telemetry before/during/after each command
  2. tells you exactly when to look at the print and what to look for
  3. always restores the nominal value afterwards

Read the result off the PART, not off this script's output. For M104 you can
read it off telemetry (nozzle_temper will move). For M220 you can read it off
mc_remaining_time drifting. For M221 only your eyes work -- extrusion width.

Interpretation:
  M104 works, M220 works, M221 ignored  -> partial CAXTON: temp + speed heads
                                           only. Flow and Z must come from
                                           slicer-side induction, one label
                                           per print.
  all ignored                           -> no self-labelling on this platform.
                                           Borrow CAXTON's weights instead of
                                           reproducing its dataset.
  all work                              -> reproduce CAXTON on a closed-
                                           ecosystem printer. That is a paper.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from bambu_link import BambuLink  # noqa: E402

log = logging.getLogger("probe")

# (label, command, restore, hold_s, what to look for)
TESTS = [
    ("M104 nozzle temp -25C", "M104 S195", "M104 S220", 45,
     "watch nozzle_temper in the telemetry below. If it tracks toward 195, "
     "the printer honoured it."),
    ("M220 feedrate 50%", "M220 S50", "M220 S100", 45,
     "watch mc_remaining_time. If the ETA jumps up, it honoured it. Also "
     "audible: the machine gets quieter."),
    ("M221 flow 60%", "M221 S60", "M221 S100", 45,
     "LOOK AT THE PART. Extrusion should visibly thin / gap. Telemetry will "
     "NOT tell you. This is the one that matters."),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True)
    p.add_argument("--serial", required=True)
    p.add_argument("--access-code", required=True)
    p.add_argument("--out", type=pathlib.Path,
                   default=pathlib.Path("probe_log.jsonl"))
    p.add_argument("--only", help="run one test by substring, e.g. M221")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")

    fh = open(a.out, "a", buffering=1)
    link = BambuLink(a.host, a.serial, a.access_code)

    def snap(tag: str, cmd: str | None = None):
        rec = {"t": now(), "tag": tag, "cmd": cmd, "state": link.summary()}
        fh.write(json.dumps(rec) + "\n")
        s = rec["state"]
        log.info("%-14s nozzle=%s bed=%s layer=%s eta=%s state=%s",
                 tag, s["nozzle_temper"], s["bed_temper"], s["layer_num"],
                 s["mc_remaining_time"], s["gcode_state"])

    if not link.connect():
        log.error("no connect. LAN-only Mode ON? Developer Mode ON? "
                  "access code right? port 8883 open?")
        return 1
    time.sleep(2)

    st = link.summary()
    if st["gcode_state"] != "RUNNING":
        log.error("printer is %s, not RUNNING. Start a print first -- a big "
                  "flat part, so flow changes are visible.", st["gcode_state"])
        link.disconnect()
        return 2

    log.info("printing %s, layer %s. starting probe.",
             st["subtask_name"], st["layer_num"])

    tests = [t for t in TESTS if not a.only or a.only.lower() in t[0].lower()]
    for label, cmd, restore, hold, watch in tests:
        log.info("")
        log.info("=== %s ===", label)
        log.info(">>> %s", watch)
        snap("before", cmd)
        link.send_gcode(cmd)
        log.info("sent: %s   (holding %ds)", cmd, hold)
        for i in range(hold // 15):
            time.sleep(15)
            snap(f"during+{(i + 1) * 15}s", cmd)
        link.send_gcode(restore)
        log.info("restored: %s", restore)
        time.sleep(10)
        snap("after", restore)

    log.info("")
    log.info("done. raw log -> %s", a.out)
    log.info("M104/M220: read the answer off the telemetry above.")
    log.info("M221: read the answer off the PART. Nothing else will tell you.")
    link.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
