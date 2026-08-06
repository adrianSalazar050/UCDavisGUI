# bambu_monitor

A web dashboard for a fleet of Bambu Lab A1 / A1 mini printers: live status,
a camera view, a YOLO failure detector that can stop a print by itself,
automatic slicing, a print queue, and a traceability ledger that records every
part made and every gram of filament used.

## Install

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
```

## Run

```bash
python -m server --lan
```

Then open the URL it prints. It serves the dashboard to everyone on the
network, so anyone in the lab can use it from a browser or a phone — no
install on their side.

Other ways to start it:

```bash
python -m server           # this machine only, no password
python -m server --mock    # no hardware: three fake printers, for a demo
```

## First-time setup

**On the printer** — nothing works without these two:

1. **Settings → LAN-only Mode → on**, then power-cycle.
2. **Settings → Developer Mode → on.** It only appears once LAN-only is on.

Note the 8-character access code on the screen; you'll type it into the
dashboard. It rotates on some firmware updates, and that is the most common
cause of a connection that used to work.

**On this machine** — put a shared password in a file called
`.bambu-password` (one line, gitignored). `--lan` reads it from there, and
refuses to start without it rather than putting printer control on the network
unprotected. Windows also needs the port opened once, from an admin shell:

```powershell
New-NetFirewallRule -DisplayName "Bambu Monitor" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

**In the browser** — go to **Setup → Printers → Add a printer** and enter the
IP, serial, and access code. Printers are added here, not on the command line.
Once one is registered, the printer picker in the header switches which machine
every page is showing.

## Where to read more

**[`master.md`](master.md) explains the whole system** — architecture, every
module, the auto-stop state machine, slicing, the ledger, and the gotchas that
cost real time. Start there for anything beyond running it.

| | |
|---|---|
| [`master.md`](master.md) | **The full documentation.** Start here |
| [`CONNECTION.md`](CONNECTION.md) | Connection details, TLS specifics, troubleshooting |
| [`FAILURE_DETECTOR_REPORT.md`](FAILURE_DETECTOR_REPORT.md) | What the failure detector actually scores |
| [`desktop/README.md`](desktop/README.md) | Packaging it as an installable app |
| `docs/superpowers/` | Design specs and plans — historical records, not instructions |

## Status of the failure detector

The dashboard, slicing, the queue, and the ledger are all verified on real
hardware. **The failure detector is not ready to be armed.** The shipped model
is effectively blind on the printer's own camera (mAP50 0.0016), and the
fine-tuned replacement is measured on 9 test frames of a single physical
tangle. [`master.md` §12](master.md) is the full story, including how the
first evaluation of it turned out to be circular.

Detection is done at **print-level FPR < 1% over ≥30 successful prints** and
**time-to-detection < 5 min over ≥20 induced failures across ≥3 induction
methods**. That is the exit criterion, and it has not been met.

## Tests

```bash
python -m pytest -q          # no hardware required
cd frontend && npm test
```
