# Bambu Lab A1 mini — LAN Connection Notes

How the LAN connection to the printer works, the exact values it needs, and what
to check when it doesn't.

> **On the scripts named below.** The connection was originally proven by two
> throwaway diagnostics, `testing.py` (full MQTT diagnostic, strict TLS) and
> `serial.py` (raw TLS handshake check). **Neither is in the repo any more** —
> `bambu_link.py` superseded both. They are described in §"Historical" because
> the trust-model contrast with `bambu_link.py` is the useful part, not because
> you can run them.

## Printer prerequisites (must be done on the printer first)

1. Settings -> WLAN/Network -> **LAN-only Mode** -> ON. Power-cycle the
   printer after the first time you enable it.
2. Same menu -> **Developer Mode** -> ON. The toggle must be green.
3. A microSD or USB stick inserted (needed for file uploads / capture, not
   for the connection itself).

Developer Mode only exists on certain firmware versions (X1 >= 01.08.03.00,
A1 >= 01.05.00.00, P1 >= 01.08.02.00, H2D >= 01.01.00.01; P2 ships with it).
The dashboard shows the firmware version on the printer card once connected, so
you can double-check.

## Connection parameters used

| Value | Used here | Where to find it on the printer |
|---|---|---|
| IP | `192.168.137.2` | Settings -> WLAN |
| Serial | `0300CA633005010` | Also the TLS certificate's common name |
| LAN Access Code | `<read from printer screen>` | Shown on the printer screen; **rotates on some firmware updates** |
| Port | `8883` (fixed) | MQTT over TLS |
| Username | `bblp` (fixed) | Same for every printer |

If the access code stops being accepted, re-read it off the printer screen —
it's the most common cause of a rejected connection after everything else
worked before.

## What the code actually uses today

Everything below the "Historical" heading is background. The live path is:

| Channel | Port | Module | Notes |
|---|---|---|---|
| Telemetry + control | 8883 | `bambu_link.py` | MQTT over TLS, self-signed cert accepted |
| microSD listing/transfer | 990 | `server/sdcard.py` | FTPS, implicit TLS, read-only in the UI |
| Built-in camera | 6000 | `detect.py` (`BambuCameraSource`) | TLS, 80-byte `bblp` auth packet, then length-prefixed JPEGs |

All three authenticate with the same `bblp` + LAN-access-code pair. See
[`master.md`](master.md) §2 for how they fit together.

## Historical: the two diagnostics that proved this

Neither script is in the repo any more — `bambu_link.py` replaced both. Kept
here because the TLS trust-model contrast is worth understanding.

### 1. `testing.py` — full diagnostic, strict TLS verification

- Downloads a CA bundle once:
  `curl.exe -O https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/examples/ca_cert.pem`
  (must be `curl.exe`, not bare `curl` — PowerShell aliases that to
  `Invoke-WebRequest`).
- Builds an SSL context from that CA file, but:
  - `check_hostname = False`, because the certificate's common name is the
    printer's **serial number**, not its IP, so automatic hostname matching
    can't work when connecting by address. The script verifies the CN by
    hand in `on_connect` instead of disabling checks entirely.
  - `verify_flags &= ~ssl.VERIFY_X509_STRICT`, because the CA bundle's
    legacy "BBL CA" entry has no `keyUsage` extension, which Python 3.13+
    rejects under strict rules (no-op on 3.11).
- Connects with `paho-mqtt` (`CallbackAPIVersion.VERSION2`, `MQTTv311`),
  `username_pw_set("bblp", CODE)`, subscribes to `device/{SERIAL}/report`,
  and publishes `get_version` + `pushall` requests to
  `device/{SERIAL}/request`.
- Optionally blinks the chamber LED as a write-command sanity check (reads
  aren't gated by Developer Mode the way control commands are, so this is
  what actually proves Developer Mode is working, not just LAN-only mode).
- Requires: `py -3.11 -m pip install paho-mqtt`, and `ca_cert.pem` sitting
  next to the script. Run with `py -3.11 testing.py`.

### 2. `serial.py` — minimal raw TLS handshake check

- No `paho-mqtt` dependency. Opens a raw TLS socket to `IP:8883` using the
  same CA file and the same relaxed hostname/strict-flag settings, then
  prints the peer certificate's subject and issuer common names.
- Useful as a first, fast sanity check (TLS + cert identity only, no MQTT
  auth) before running the fuller `testing.py`.

### 3. What `bambu_link.py` does instead (the live path)

The dashboard's `BambuLink` class connects the same way but does **not** pin the
CA at all:

- `cert_reqs=ssl.CERT_NONE` + `tls_insecure_set(True)` — accepts the
  printer's self-signed cert without verification, instead of validating
  against `ca_cert.pem`. This is intentional: Bambu's own docs call
  Developer Mode "unsupported, you assume the risk," and the code comment
  treats that as acceptable specifically because it's a trusted LAN.
- Same fixed pieces as above: port `8883`, username `bblp`, password = LAN
  access code, same `device/{serial}/report` / `device/{serial}/request`
  topics, same "send `pushall` once on connect" pattern.
- Adds what the diagnostic scripts don't need: a deep-merge of the running
  state dict (the printer sends **partial** updates — most report messages
  only contain the fields that changed) and an `on_layer` callback that
  fires only when `layer_num` increases.

So: same credentials, same port/topics, different trust model for the TLS
cert. No need to copy `ca_cert.pem` into `GUI_UCDavis` — `bambu_link.py`
doesn't use it.

## Plugging these values into GUI_UCDavis

The dashboard no longer takes the printer on the command line. Start it:

```
python -m server
```

Then open http://localhost:8000, go to **Overview → Add printer**, and type:

| Field | Value |
|---|---|
| IP address | `192.168.137.2` |
| Serial | `0300CA633005010` |
| LAN access code | `<your 8-digit code>` |
| Name (optional) | anything, e.g. `A1-bench` |
| Camera checkbox | tick it if the webcam points at this printer |

The printer is saved to `printers.json` (gitignored — it holds the access code
in plaintext) and reconnects automatically on every restart. Add up to a
handful of printers this way; the Overview page shows all of them at once.

If a printer sits on red **Offline**, the card tells you which failure it is:
"Unreachable" means the IP is wrong or LAN-only Mode is off; "No response"
means the TLS handshake worked but the access code is likely wrong, or
Developer Mode is off. That maps to the troubleshooting list below.

**microSD files** are read over FTPS (port 990), not MQTT — MQTT exposes no
file listing at all. Same `bblp` + access-code credentials. The SD Files page
is read-only.

Add `--port 8000` to change the port, `--runs-dir runs/` to change where
capture frames are read from, `--printers-file` to move the printer list.
Without hardware, `python -m server --mock` seeds three fake printers
(running / stale / offline) so the whole UI can be exercised.

Then, for frontend dev, run `npm run dev` inside `GUI_UCDavis/frontend`
(port 5173, proxies `/api` and `/ws` to the backend on port 8000). For a
normal/prod run, `npm run build` once and the single `python -m server`
process serves everything on `http://localhost:8000`.

See `GUI_UCDavis/docs/superpowers/specs/2026-07-16-bambu-dashboard-design.md`
for the v1 dashboard design, and
`docs/superpowers/specs/2026-07-16-multi-printer-sd-browser-design.md` for the
multi-printer + SD browser design this connection feeds into.

## Troubleshooting

- **Broker rejects the connection** -> almost always a wrong access code;
  re-read it off the printer screen.
- **TLS connects but no status report within the timeout** -> auth may be
  fine but the MQTT channel itself is gated; confirm the Developer Mode
  toggle is green, not just LAN-only mode.
- **Cannot reach `IP:8883` at all** -> Developer Mode off, LAN-only Mode
  off, or wrong IP.
- **Certificate rejected** -> a wrong or truncated `ca_cert.pem` (this
  usually means the download silently saved an HTML error page instead of
  the real file — check it contains `BEGIN CERTIFICATE`). Don't "fix" this
  by disabling verification with `CERT_NONE`; that's a different, deliberate
  trust model (see `bambu_link.py` above), not a patch for a bad download.
- **No SD card / USB detected** -> file uploads / capture will fail; insert
  media.
