# ERP traceability: print-run ledger, pieces, inventory, and Supabase sync — design

> **STATUS: DESIGN APPROVED 2026-07-24. Phases 1–3 SHIPPED; Phases 4–5 are
> still design only.** The paragraph below was written before any code
> existed and is kept as written; what has happened since:
>
> | Phase of §9 | State | As-built reference |
> |---|---|---|
> | 1 — ledger + run recording | ✅ shipped 2026-07-24, verified on the real A1 | `master.md` §13, `plans/2026-07-24-erp-traceability-phase1-ledger.md` |
> | 2 — parts catalogue + recipes | ✅ shipped 2026-07-24 (schema v2); **hardware gate open** | `master.md` §14, `plans/2026-07-24-erp-traceability-phase2-parts.md` |
> | 3 — filament spools + consumption | ✅ shipped 2026-07-25 (schema v3); **hardware gate open** | `master.md` §15, `plans/2026-07-25-erp-traceability-phase3-spools.md` |
> | 4 — Supabase sync | 📐 design only, no code | — |
> | 5 — arm ingest | 📐 design only, no code | — |
>
> *Original banner, 2026-07-24:* "NOT IMPLEMENTED. No code exists yet.
> Nothing described below has been built, and no claim in this file is
> verified on hardware. It is the agreed design for the next block of work,
> to be implemented in the five phases of §9 — Phase 1 first, each phase
> getting its own implementation plan."
>
> Historical record from the moment of writing, not maintained afterwards.
> **`master.md` is authoritative wherever this file disagrees with it.**

Date: 2026-07-24

---

## 1. The goal, and the gap it fills

The farm is becoming an automated production cell: a robotic arm lifts, cleans,
and replaces build plates when prints finish, so the machines run unattended
across many jobs. What is missing is any record that the production happened.

The requirement is a traceability scheme feeding a self-contained ERP:

- People submit **inbound orders** for pieces.
- Production against those orders is recorded — which printer, which part
  revision, which spool, how long, how many grams, what went wrong.
- **Every physical piece** gets identity and a human-confirmed verdict, with
  **badges** describing defects.
- The office can query, cost, and report on all of it.

### 1.1 Nothing in this repo currently records that a print happened

This is the load-bearing observation, and it is why the first phase is what it
is. Verified by reading the code on 2026-07-24:

| Where you might expect history | What is actually there |
|---|---|
| `PrintQueue` | Forward-looking only. Jobs are **removed** on confirmed start (`server/queue.py::remove`, called by the start route) |
| `SliceCoordinator` | Jobs are **runtime-only, never persisted**, and the oldest terminal ones are evicted past a cap (`master.md` §6.5) |
| The start route | Verifies a start, then stops watching. `master.md` §5.4: "What this does *not* do is watch the print afterwards" |
| `capture.py` runs | Frames and telemetry for **dataset building**, keyed by layer, not a production record. Discovered by mtime within a 30-minute window (`server/runs.py`) |
| `printers.json` / `queues.json` | Configuration and pending work. No history |

There is no completion event, no failure record, no grams-actually-used, and no
table of past runs anywhere in the system.

**The consequence that sets the build order: you cannot model data you never
captured.** A perfect ERP schema with nothing feeding it is a worse outcome
than a crude local log that records every run, and MQTT keeps no history to
replay — a print that ran while nothing was recording is gone. Every phase
after the first can be added later against a ledger that has been quietly
recording the whole time.

### 1.2 Decisions taken during design

| Question | Decision |
|---|---|
| Is there an external ERP? | **No.** Supabase *is* the ERP. Scope is entirely ours |
| Which way do orders flow? | **Inbound** — people order pieces from the farm. Outbound purchase orders for filament are **out of scope** (§11) |
| Finest traced unit? | **Per piece, human-confirmed.** Runs auto-create piece rows; verdicts are an operator action |
| Where does data live? | **Local-first.** SQLite is the farm's source of truth; Supabase is the sync target |
| Where does the arm's software live? | **Separate codebase**, reporting events in over HTTP |
| What is a "part"? | A catalogue record carrying the **model file and a slicing recipe**, so an order line is reproducible |
| How is filament tracked? | **Per physical spool**, with grams remaining |
| Where do humans work? | **Split** — print-floor actions in the existing dashboard, order entry and reporting office-side in Supabase |

---

## 2. Why local-first, and why the server is the only writer

`master.md` §2.1 records an architectural invariant: the printer is reachable
only at a private LAN address over MQTT, FTPS, and raw TCP, so something always
runs on the LAN. A hosted site cannot replace it.

Traceability is different from control — it is append-mostly data that can sync
asynchronously — so the cloud is a legitimate home for the ERP half. But the
farm must keep printing when the internet is down, and **the traceability
record must survive that gap**. Data whose entire value is completeness is
exactly the data that cannot be dropped on a network blip.

Hence: a local SQLite ledger is the source of truth for the farm node, and a
background worker pushes to Supabase.

**The frontend never talks to Supabase directly.** The dashboard already has
shared-password cookie auth (`master.md` §2.1), chosen as a cookie rather than
a bearer token specifically because browsers cannot set headers on a WebSocket
handshake. Adding `@supabase/supabase-js` to the browser would mean two auth
systems and a split-brain UI where half the data comes from `/api` on the LAN
and half from the cloud, with different failure modes and different login
states. The server is the only thing holding a Supabase credential.

> `FRONTEND-STACK-GUIDE.md` lists Supabase JS as part of the stack it describes
> and notes it does not apply here ("no auth, no Supabase — the server is
> LAN-only"). That remains true of the **frontend**. This design adds Supabase
> on the **server** side only.

### 2.1 Why SQLite rather than more JSON files

The repo's existing stores (`PrinterStore`, `QueueStore`) read the whole file
into memory on every load and rewrite it whole on every mutation. That is
correct for ten printers and wrong for an append-only event log that grows with
every layer-change-worthy event of every print forever.

SQLite is in the Python standard library, so it adds nothing to
`requirements.txt` and the PyInstaller desktop build (`master.md` §8) picks it
up with no new hidden imports. It is transactional, so a crash mid-write cannot
half-record a run — the same durability property the atomic temp-file+`os.replace`
machinery gives the JSON stores, but without hand-rolling it.

### 2.2 Why not local Postgres

Considered and rejected. One dialect instead of two would remove the schema-drift
hazard of §8.1, and sync would become Postgres-to-Postgres. But it puts a
Postgres server on a lab Windows box, and it breaks the desktop app outright —
`master.md` §8's entire point is a frozen backend with no runtime dependency on
the target machine. Not worth the cost.

### 2.3 Why dirty flags rather than an outbox table

Considered: a separate `outbox` table recording every change, drained in order.
It gives strict ordering, clean delete propagation, and an audit trail of the
sync itself. It also doubles the writes on every mutation and adds real
machinery.

This ledger is append-mostly, deletes are soft (§7.4), and volume is a few
hundred runs a month. Per-row dirty flags plus idempotent UUID-keyed upserts
buy the same practical guarantee for much less code. An outbox would be the
right answer for many writers or exact replay; neither is needed here.

---

## 3. Components

Three new server modules, joining the registry, the detection coordinator, and
the slice coordinator as long-lived parts of the server process.

The database file is `ledger.db`, in the same directory as `printers.json` and
`queues.json` — which means it follows `BAMBU_DATA_DIR` on the desktop build
(`master.md` §8) with no special handling, and is gitignored alongside them.

| Module | Owns | Depends on |
|---|---|---|
| `server/ledger.py` | The SQLite database: connection, schema, migrations, typed row helpers | Nothing. No network, no registry — the same purity `PrintQueue` has |
| `server/runlog.py` | `RunRecorder` — a daemon thread that turns `gcode_state` transitions into run and event rows | `ledger`, and read-only access to the registry and the detection snapshot |
| `server/ledgersync.py` | `LedgerSync` — the push/pull worker | `ledger`, plus an injected HTTP callable |

Each is independently testable: `ledger.py` against a temp file, `runlog.py`
against a fake registry emitting scripted summaries, `ledgersync.py` against a
fake HTTP callable. This mirrors the seam pattern already used by
`service_factory` in the registry, `spawn`/`clock` in `DetectorSupervisor`, and
`run`/`parse`/`clock` in `SliceCoordinator`.

### 3.1 Five invariants, each inherited from a rule the repo already enforces

**A ledger failure can never break a print.** Every ledger call from a print-path
site is wrapped and logged, never raised. This is `master.md` §11's "a corrupt
config file must never stop the boot" applied one layer up: a run you failed to
*record* is bad; a run that failed to *happen* is worse.

**`ledger=None` means inert.** The same convention as `queue=None`,
`detection=None`, and `slicer=None`. Every ledger route 404s, the recorder and
sync threads never start, and the desktop app (`master.md` §8) plus `--mock`
get that for free.

**No secrets in the ledger, and none in argv.** The Supabase key and the arm
ingest token arrive via environment variables only — `master.md` §11's
access-code rule, unchanged, for the same reason: a command line is visible to
any process listing. No printer access code is ever written to a ledger row,
the same guarantee `build_summary()` gives by simply not taking the parameter.

**Prints that did not come from the queue are still recorded.** `master.md`
§5.2 records that real cards are full of raw `.gcode` files startable only from
the printer's own screen. A recorder that only created rows from the start route
would make every screen-started print invisible. Any transition into a busy
state opens a run, marked `unattributed` when no queue job matches.

**A stated gap: a print that runs while the server is down is unrecorded and
unrecoverable.** MQTT has no history to replay. Written down here rather than
discovered later.

---

## 4. The data model

Fifteen tables. Twelve sync; three are local-only bookkeeping.

Portability note: SQLite stores UUIDs and timestamps as `TEXT` (ISO-8601 UTC);
Postgres uses `uuid` and `timestamptz`. The drift guard of §8.1 compares table
and column **names**, deliberately not types, which is what lets the two
dialects differ where they must.

Every synced table carries `id uuid PK` (generated locally, or by Postgres for
cloud-owned tables), `created_at`, and `updated_at`. Locally-owned tables also
carry `synced_at`.

### 4.1 Catalogue — locally owned

**`parts`**

| Column | Notes |
|---|---|
| `part_number`, `revision` | **Unique together.** A new geometry revision is a new row, so a run can always prove which revision it built |
| `name`, `notes` | |
| `model_filename`, `model_sha256`, `model_bytes` | Metadata only. The bytes live on local disk at `<data-dir>/parts/<part_id>/<model_filename>`; the hash is recorded for integrity and dedupe |
| `archived` | Soft delete. A part that runs reference is never hard-deleted |

**`part_recipes`** — `part_id`, `name`, `preset_tier`, `filament_material`,
`nozzle`, `bed_type`, `supports`, `copies_per_plate`, `expected_seconds`,
`expected_grams`, `is_default`, `archived`.

Kept separate from `parts` on purpose: changing a preset from Standard to Fine
is a *process* change, and folding it into `parts` would force it to masquerade
as a geometry revision. The fields deliberately mirror the existing slice
request (`master.md` §6.3, §6.4, §6.7) so a recipe can be handed to the
existing slicer unchanged.

### 4.2 Demand — cloud owned, pulled

**`orders`** — `order_number` (unique), `customer_name`, `requested_by`,
`due_date`, `status` (`draft`/`open`/`in_production`/`fulfilled`/`cancelled`),
`notes`, `archived`.

**`order_lines`** — `order_id`, `part_id`, `quantity_ordered`, `unit_price`,
`currency`, `notes`, `archived`.

`quantity_delivered` is deliberately **not** a column — see §4.6.

### 4.3 Production — locally owned

**`print_runs`**, the heart of the ledger:

| Column | Notes |
|---|---|
| `printer_serial`, `printer_name` | The name is a **denormalised snapshot**. A printer can be renamed or deleted; the historical record must not lose what it was called |
| `order_line_id`, `part_id`, `recipe_id` | All nullable. Null on an unattributed run, fillable afterwards (§6) |
| `source` | `queue` or `unattributed` |
| `queue_job_id`, `slice_job_id`, `sd_path`, `subtask_name` | Provenance back to the pipeline that produced the file |
| `spool_id` | Which spool was loaded, snapshotted at start |
| `planned_seconds`, `planned_grams`, `copies_planned` | From `Metadata/slice_info.config` via the existing `threemf.parse_slice_info` |
| `bed_type`, `nozzle`, `material` | Process conditions, snapshotted — all three are configured values (`master.md` §5.3, §6.4, §6.7), so the run must capture what they were at the time |
| `started_at`, `ended_at`, `last_layer`, `total_layers` | |
| `end_state` | Null while open. Then one of §5's values |
| `actual_grams`, `actual_grams_basis` | See §4.6 |
| `stopped_by_monitor` | From the `AutoStopController` latch |

**`run_events`** — append-only: `run_id` (nullable), `printer_serial`, `ts`,
`kind`, `payload` (JSON text), `source` (`server`/`detector`/`arm`/`operator`),
and `client_uuid` (nullable, unique — the idempotency key for ingested events,
§6.1).

Kinds: `state_change`, `hms_raised`, `hms_cleared`, `detection_fault`,
`autostop_fired`, `stop_sent`, `start_unconfirmed`, `slice_started`,
`slice_done`, `upload_done`, `queue_started`, `operator_note`, and the
`arm_*` family of §6.1.

**`pieces`** — `run_id`, `part_id`, `order_line_id`, `index_in_run` (1..N),
`status` (`pending_inspection`/`good`/`rework`/`scrap`), `inspected_by`,
`inspected_at`, `notes`.

Created when a run reaches a terminal state, not at start: how many copies were
actually produced depends on the outcome. Even on a `FAILED` run they default
to `pending_inspection` rather than `scrap`, because a failed print sometimes
still yields usable parts and that is the operator's call.

### 4.4 Badges

**`badges`** — cloud owned, pulled: `code` (unique), `label`, `severity`
(`info`/`warning`/`defect`), `auto` (whether the system may apply it),
`archived`.

Seed set: `spaghetti`, `stringing`, `layer_shift`, `warped`, `poor_adhesion`,
`under_extrusion`, `over_extrusion`, `nozzle_clog`, `detached`, `hms_error`,
`autostop`, `rework`, `scrap`.

**`run_badges`** — `run_id`, `badge_id`, `applied_by`, `applied_at`, `note`.
**`piece_badges`** — `piece_id`, `badge_id`, `applied_by`, `applied_at`, `note`.

**Two levels, and they mean different things.** Automatic badges — what the
detector saw, what HMS fired — attach to the **run**, because neither signal
can be attributed to a piece. A detection is `{cls, conf, box}` in frame
pixels (`master.md` §4.1) with no association to a model on the plate, and an
HMS code describes the machine, not a part. Human badges attach to the
**piece**, set by whoever inspected it. Letting the detector write
piece-level badges would be inventing attribution the system cannot have.
`badges.auto` is what enforces the split: only `auto` badges may be applied
with `applied_by = 'detector'`.

### 4.5 Inventory — locally owned

**`filament_spools`** — `spool_code` (unique; scanned or typed), `material`,
`colour`, `brand`, `filament_profile` (the slicer's own profile name, so it
ties to `available_filaments` from `master.md` §6.3), `initial_grams`,
`purchase_cost`, `currency`, `supplier`, `purchased_at`, `status`
(`sealed`/`in_use`/`empty`/`retired`), `printer_serial`, `ams_slot`, `archived`.

`remaining_grams` is deliberately **not** a column — see §4.6.

**`filament_consumption`** — `spool_id`, `run_id`, `grams`, `basis`,
`created_at`. One row per run that consumed filament.

**Spool identity cannot come from the printer.** `master.md` §6.3 records that
`detect_loaded_filament` returns `None` for any spool the RFID cannot identify
(most third-party filament), and on a multi-tray AMS returns the *first*
identifiable tray rather than the active one — so it "can report a material
other than the one actually feeding the nozzle" and must never be treated as
authoritative. It prefills the operator's choice and nothing more.

### 4.6 Derived quantities are never stored twice

Three values that a naive schema would keep as maintained counters:

| Value | Actually is |
|---|---|
| `order_lines.quantity_delivered` | A Postgres view over `pieces` where `status = 'good'` |
| `filament_spools.remaining_grams` | `initial_grams` minus the sum of that spool's `filament_consumption` |
| A run's `actual_seconds` | `ended_at - started_at` |

All three are the classic drift bug — a counter and the rows it counts
disagreeing forever after one failed transaction — and the fix is to not have
the counter. Locally these are computed on read; in Postgres they are views.

**`actual_grams` is an estimate, and the schema says so out loud.** The printer
does not report filament consumed. So:

| End state | `actual_grams` | `actual_grams_basis` |
|---|---|---|
| `FINISH` | `planned_grams` | `planned` |
| `FAILED` and friends | `planned_grams × (last_layer / total_layers)` | `proportional` |
| Operator override | whatever they entered | `manual` |

The proportional estimate is itself wrong in detail, because layers are not
equal mass — a tall thin part's last layers weigh far less than its first.
Recording the *basis* is what stops someone quoting an estimate as a
measurement six months from now, and is the same discipline as `master.md`
§12.3's "read the two columns together or they lie".

### 4.7 Local-only tables

**`schema_version`** — the applied migration number.
**`sync_state`** — one row per synced table: `table_name` (PK), `pull_cursor`,
`last_attempt_at`, `last_success_at`, `consecutive_failures`, `last_error`.
**`ledger_meta`** — key/value, for things like the database's creation stamp.

None of these are ever pushed.

---

## 5. How a run gets recorded

`RunRecorder` is a daemon thread polling `registry.summaries()` on a `TICK_S`
of 1.0 s, holding the previous tick's `gcode_state`, `hms` list, and
`layer_num` per serial. Everything it writes is derived from the difference
between two consecutive snapshots: no new printer I/O, no second MQTT
subscription, nothing that can disturb a print.

It reuses `printer.BUSY_STATES` (`RUNNING`, `PREPARE`, `PAUSE`, `PAUSED`,
`SLICING`) as the "a print is happening here" predicate rather than inventing a
second list that could drift from it.

| Transition | Written |
|---|---|
| not-busy → busy | Open a run, **or adopt** one the start route already opened |
| `layer_num` changed | Update `print_runs.last_layer` in place — **no event row** |
| new code in `hms` | `hms_raised`, code formatted by `bambu_link.decode_hms` |
| code gone from `hms` | `hms_cleared` |
| busy → `FINISH` | `end_state = FINISH`, `ended_at`, create `pieces`, write `filament_consumption` |
| busy → `FAILED` | Same, but `end_state = STOPPED_BY_MONITOR` when the detection snapshot's `stopped_by_monitor` latch is set, else `FAILED` |
| busy → `IDLE` with no `FINISH` seen | `end_state = UNKNOWN` |

`end_state` values: `FINISH`, `FAILED`, `STOPPED_BY_MONITOR`,
`STOPPED_BY_OPERATOR` (operator-set only), `START_UNCONFIRMED`, `UNKNOWN`.

### 5.1 Attribution belongs to the start route, not the recorder

The start route in `server/main.py` is the only place that knows the queue job,
the part, the recipe, and the loaded spool. So **it** creates the `print_runs`
row, *before* publishing the MQTT command, and the recorder adopts any
non-terminal row for that serial instead of opening a second one.

Without that ordering the recorder's 1 s tick could beat the route's commit and
produce a spurious unattributed run alongside the real one.

### 5.2 That ordering also closes a hole that exists today

`master.md` §5.4 records that when the printer never confirms a start, the job
stays queued and the response says so — deliberately, because "a command the
printer ignored must never silently eat a job". But nothing anywhere remembers
that it happened.

With the row created before publishing, a failed verification closes it as
`end_state = START_UNCONFIRMED` and writes a `start_unconfirmed` event. "This
printer swallowed four start commands last Tuesday" becomes a query instead of
a memory.

### 5.3 Layer progress updates a column; it does not append events

A 100-layer print would otherwise write 100 event rows containing no
information, and a 1,200-layer print 1,200. The run row carries `last_layer`;
the event log is reserved for things that are actually events.

### 5.4 Two limitations, stated rather than papered over

**An operator stopping a print at the printer's own screen is
indistinguishable from a genuine failure.** `master.md` §3.1 verified on
hardware that a stopped print reports `FAILED`, and there is no separate
signal. So the recorder writes the honest `FAILED`, and `end_state` is
operator-editable afterwards to `STOPPED_BY_OPERATOR`.

**A server restart mid-print leaves an open row — and reconciliation must not
be hasty about it.** The first, naive implementation closed every open run at
`start()`; on real hardware that mislabeled a *running* print `UNKNOWN` and
split it into a duplicate unattributed row, because at boot the MQTT links
have not delivered a report yet, so the printer still looked idle. Corrected:
reconciliation is **deferred and connection-aware**. `RunRecorder` snapshots
the open runs and resolves each only once its printer reports
`connection = "ok"` — a still-busy printer's run is left open so the adopt
path re-attaches to it (attribution survives the restart), an idle printer's
run is closed `UNKNOWN`, and a printer that never reports is closed `UNKNOWN`
only after a deadline. Leaving a run open forever would corrupt every "runs in
progress" query; closing a running one loses the record — the connection gate
is what tells the two apart.

---

## 6. Routes

All 404 when `ledger=None`. Model upload and download are sync `def`, so
FastAPI runs them on the threadpool — the same reason the FTPS and slice routes
are (`master.md` §3.2): reading a large STL off the wire must not stall the
event loop every WebSocket depends on.

| Group | Routes |
|---|---|
| Parts | `GET/POST /api/parts`, `PUT /api/parts/{id}`, `GET /api/parts/{id}/model`, recipe CRUD under `/api/parts/{id}/recipes` |
| Orders | `GET /api/orders` **only** |
| Runs | `GET /api/runs` (filter by serial and date range), `GET /api/runs/{id}`, `PATCH /api/runs/{id}`, `POST`/`DELETE /api/runs/{id}/badges` |
| Pieces | `PATCH /api/pieces/{id}`, `POST`/`DELETE /api/pieces/{id}/badges`, `POST /api/runs/{id}/pieces/bulk` |
| Spools | `GET/POST/PUT /api/spools`, `POST /api/printers/{serial}/spool`, `GET /api/spools/low` |
| Ingest | `POST /api/ingest/arm` |
| Health | `GET /api/ledger/sync` |

**The orders group is read-only, and the missing routes are the design.**
§7.1's single-writer rule is not enforced by convention — there is simply no
local code path that writes an order. Someone cannot violate it later without
noticing they are adding a route that should not exist.

**`PATCH /api/runs/{id}` is what makes unattributed runs useful.** A
screen-started print arrives with no order line and no part; if it could never
be attributed afterwards, every such job would be permanent dead weight in the
record. The same PATCH carries the operator's `end_state` correction (§5.4) and
the `actual_grams` override (§4.6).

**`POST /api/runs/{id}/pieces/bulk` exists because nobody clicks eight times.**
A plate of eight good parts must be one action — "all good", or "all good
except #3, scrapped". If confirming a plate is tedious the verdicts stop being
entered, and the piece-level traceability this design is built around quietly
becomes fiction. This route is what the human-confirmed model lives or dies on.

### 6.1 The arm's ingest endpoint, and why it fails closed

The arm is a separate machine on the LAN with no browser, so §2's cookie
session does not fit it. It gets a dedicated shared token from the environment,
`BAMBU_INGEST_TOKEN`, sent as an `X-Ingest-Token` header and compared with
`hmac.compare_digest` — exactly what `server/auth.py` already does for the
password, so a wrong token cannot be guessed character by character from
timing.

**If `BAMBU_INGEST_TOKEN` is unset, the ingest route does not exist.** Not
open, not permissive — absent. This is the same shape as `master.md` §2.1's
fail-closed rule and exists for the same reason: an unauthenticated write
endpoint appearing on a shared LAN because someone forgot an environment
variable is precisely the accident that rule exists to make impossible.

Three properties of the payload:

**Batched and idempotent.** The arm has the same offline problem the server
does, so it posts `{"events": [...]}` and each event carries an arm-generated
`client_uuid`. A duplicate is silently ignored (the unique index on
`run_events.client_uuid` is the enforcement), which makes "retry until it
works" a safe strategy for the arm rather than a way to double-count plate
cycles.

**The arm may only write `arm_*` kinds**, validated against an allowlist:
`arm_plate_removed`, `arm_plate_cleaned`, `arm_plate_replaced`,
`arm_cycle_started`, `arm_cycle_failed`. It has no business writing a
`state_change` or an `autostop_fired`, and a buggy or compromised arm must not
be able to forge the server's own observations.

**The arm posts a printer serial, not a run id** — it knows which machine it
just serviced, not what was printing on it. The server attaches the event to
that printer's most recent run, and to **no** run if there is not a recent one,
rather than guessing.

---

## 7. Sync

`LedgerSync` is a daemon thread on a 30 s tick (`--sync-interval`), with an
injected HTTP callable and clock so the whole state machine tests with no
network and no Supabase project.

### 7.1 Every table has exactly one writer

This is the decision that keeps sync simple enough to be correct. Order entry
happens office-side; runs and piece verdicts happen at the machine. Data
therefore moves both ways — and two-way sync with conflict resolution is a tar
pit.

| Direction | Tables |
|---|---|
| **Pulled** (cloud owns) | `orders`, `order_lines`, `badges` |
| **Pushed** (local owns) | `parts`, `part_recipes`, `filament_spools`, `print_runs`, `run_events`, `pieces`, `run_badges`, `piece_badges`, `filament_consumption` |

No row ever has two writers, so there are no conflicts to resolve — not
"conflicts we handle well", none possible by construction. Pulled rows land in
a local cache that is read-only: no local code path updates them. Offline,
production continues against orders already pulled.

### 7.2 Push

For each locally-owned table **in dependency order** — `parts`,
`part_recipes`, `filament_spools`, `print_runs`, `run_events`, `pieces`,
`run_badges`, `piece_badges`, `filament_consumption` — select up to 200 rows
where `synced_at IS NULL OR synced_at < updated_at` and POST them to
`{SUPABASE_URL}/rest/v1/{table}` with `Prefer: resolution=merge-duplicates`.

Because primary keys are UUIDs generated locally, the upsert is idempotent: a
request that timed out but actually landed costs nothing on retry, which is the
failure mode that dominates on a flaky connection.

**The lost-update guard.** Stamping `synced_at` is a compare-and-clear:

```sql
UPDATE <table> SET synced_at = :now WHERE id = :id AND updated_at = :seen
```

Without that predicate, an operator editing a piece verdict while its row was
in flight would have the edit marked synced and never pushed — a silent loss
that is nearly impossible to notice, because the row looks correct locally and
merely differs in the cloud forever.

### 7.3 Pull

`orders`, `order_lines`, and `badges` are cursored on the **cloud's own**
`updated_at`, maintained by a Postgres trigger created in the DDL rather than
by any client.

Two details:

**The cursor only ever compares cloud timestamps to cloud timestamps**, never
to the lab machine's clock, so skew between the two cannot skip rows.

**The filter is `gte`, not `gt`.** A timestamp tie between two rows would let
`gt` drop one. Re-fetching a row occasionally is free, because the local write
is an upsert; skipping one is not.

### 7.4 Deletes do not propagate, so the schema has none

A cursored pull cannot observe a row that no longer exists. Every table carries
`archived boolean` and the DDL uses soft deletes throughout, rather than
leaving a hard delete as a trap that silently desynchronises the cache.

### 7.5 Failure handling, and opt-in by absence

A failed tick logs, increments `consecutive_failures`, and backs off
exponentially to a 5-minute cap. The thread never dies and never raises into
anything else; the ledger keeps recording locally regardless, which is the
entire point of §2. The key never appears in a log line or an error message —
the same rule `SdError` follows (`master.md` §11).

**No `SUPABASE_URL` in the environment means `LedgerSync` never starts** and the
ledger simply records locally. So Phase 1 is not throwaway scaffolding; it is
this exact code with the worker inert, the same "None means inert" convention
as `queue`, `detection`, and `slicer`.

`GET /api/ledger/sync` returns last attempt, last success, consecutive
failures, last error, and pending row counts per table, so the dashboard can
show "42 rows pending" instead of leaving the operator to guess whether the
office is seeing today's production.

### 7.6 Credentials

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Project URL. Not a secret |
| `SUPABASE_SERVICE_KEY` | `service_role` key. **Full access, bypasses RLS** |
| `BAMBU_INGEST_TOKEN` | The arm's shared token (§6.1) |

By operational convention the key is a single line in a gitignored
`.supabase-key` at the repo root, read into the environment at launch — exactly
the habit `master.md` §8 documents for `.bambu-password`, and gitignored for the
same reason `printers.json` is. Nothing in the code knows that filename.

Because the server authenticates as `service_role`, it bypasses row-level
security entirely. That is correct for a single trusted writer, and it is also
why the key must never reach a browser. RLS policies become necessary only if a
separate Supabase-hosted app is built for office users later (§11).

### 7.7 Schema DDL

The Postgres schema ships as version-controlled SQL under a new `sql/`
directory, applied by pasting into Supabase's SQL editor. It is not created
imperatively by any tool or agent: a DDL file is reviewable, re-runnable, and
diffable, which an imperative mutation of a live project is not.

---

## 8. Failure handling and testing

### 8.1 The real hazard: two schemas drifting

The cost of Approach A over local Postgres (§2.2) is a SQLite schema and a
Postgres schema that must stay in step, where adding a column to one and not
the other fails silently — the push simply never carries the new field.

Handled the way this repo already handles doc rot: with a test. One canonical
table/column manifest, and `test_ledger.py` asserting that the SQLite schema
and the `sql/` DDL agree on table and column names, failing the suite the
moment they diverge. Types are deliberately not compared (§4). Same philosophy
as `server/tests/test_docs.py`: the guard exists because the failure is
otherwise invisible.

Per the discipline in that file, each guard is verified by deliberately
breaking the thing it protects and watching it fail. A guard that has never
been seen to fail is decoration.

### 8.2 Corruption, concurrency, migrations

**A corrupt database must not stop the boot, and must not be destroyed.** On
open, `PRAGMA integrity_check`. On failure, rename to
`ledger.db.corrupt-<timestamp>`, start a fresh database, and log loudly. That
is `master.md` §11's boot invariant plus one addition: a corrupt file is also a
forensic artifact, and deleting it discards the only evidence of what went
wrong.

**Concurrency.** WAL mode, one connection guarded by a `threading.Lock`, and
`busy_timeout` set. FastAPI routes run on a threadpool and `RunRecorder` writes
from its own thread, so this is a real race, not a theoretical one. The lock is
never held across network I/O, mirroring the registry's two-lock discipline.

**Migrations** are forward-only and numbered, applied on open against
`schema_version`.

### 8.3 Tests

None of these touch a socket, a camera, or a printer.

| File | Covers |
|---|---|
| `test_ledger.py` | Schema creation, forward migrations, corrupt-file recovery, and the §8.1 manifest guard |
| `test_runlog.py` | Every transition in §5, driven by a fake registry emitting scripted summaries: adopt-vs-create, `START_UNCONFIRMED`, HMS raise/clear, terminal states, restart reconciliation, and that a ledger exception never escapes into the print path |
| `test_ledgersync.py` | Push batching and dependency order, the §7.2 compare-and-clear guard, the `gte` cursor and its tie behaviour, backoff, and that the key never reaches a log line |
| `test_ledger_api.py` | Every route via `TestClient` against a temp database; the ingest token failing closed when unset; the `arm_*` allowlist; `client_uuid` idempotency; and that the order write routes are genuinely absent |

Frontend: any pure logic (yield arithmetic, piece-status rollups) is extracted
and unit-tested, following `roiGeometry.js`. The React components themselves
are verified by build and by eye, which is the same real gap `master.md` §10
already records rather than a new one.

### 8.4 The hardware gate

Per `master.md` §1.1's discipline, **none of this is "verified" until one real
print is recorded end to end** — correct `end_state`, correct layer count,
correct piece rows, correct consumption row. Until someone runs one, this
design and its implementation stay explicitly unverified, and the phase's plan
carries that as its own outstanding task rather than being marked complete.

---

## 9. Build order

Each phase gets its own implementation plan. The whole model is designed here
so that no phase paints a later one into a corner.

| Phase | Contents |
|---|---|
| **1** | Local ledger + run recording: `ledger.py`, `runlog.py`, start-route integration, run history routes, piece creation and verdicts including the bulk route, the seeded badge catalogue, and one new dashboard page. No cloud, no parts, no spools |
| **2** | Parts catalogue + recipes, model file storage, and pointing the existing slice flow at a stored recipe instead of an ad-hoc upload |
| **3** | Filament spools + consumption, the "which spool is loaded" control, and the low-stock view |
| **4** | Supabase: the `sql/` DDL, `LedgerSync`, the orders pull, sync health in the UI |
| **5** | Arm ingest |

**Phase 1 is first for a reason that is not about dependencies: it is the only
phase that is lossy to delay.** Everything else can be added later against a
ledger that has been recording all along. Every print that runs before Phase 1
lands is history no later work can recover (§1.1).

Phases 2–4 before 5 keeps the local model settled before the schema has to
exist in two dialects, minimising rework in the DDL. Phase 5 depends only on
run rows existing, so it can be pulled forward if the arm's own logging becomes
the priority.

---

## 10. Frontend scope

Split by where the person is standing.

**In the dashboard** (`frontend/src/app/pageRegistry.jsx` — pages are added
there and nowhere else), following the existing hand-rolled UI kit and design
tokens:

| Page | Phase | Purpose |
|---|---|---|
| History | 1 | Run list, drill-in to events and pieces, piece confirmation (the bulk action of §6), `end_state` correction |
| Parts | 2 | Catalogue and recipes, model upload |
| Inventory | 3 | Spools, "which spool is loaded", low stock |

**Office-side, in Supabase Studio**: order entry, fulfilment reporting,
costing, and cross-run analysis. Operators confirm verdicts at the machine
because that is the only place the verdict is knowable; the office does not need
to be on the LAN to read production.

This split is what keeps the frontend work proportionate — the alternative was
four or five new pages reimplementing what Studio gives for free.

---

## 11. Explicitly out of scope

- **Outbound purchase orders.** Orders flow inbound only (§1.2). Per-spool
  grams remaining supports a low-stock *view*; no PO or supplier document model
  is built. Revisit when restocking is actually painful.
- **A separate Supabase-hosted web app**, and therefore RLS policies and
  per-user accounts. `master.md` §2.1 records that per-user auth was
  deliberately declined in favour of a shared password; that decision is
  unchanged here.
- **Cost accounting beyond filament and machine time** — no labour rates, no
  overhead allocation, no depreciation.
- **Supabase Storage for model files.** Files stay local in Phase 2. Whether to
  mirror them to Storage depends on the project's tier and is deferred.
- **Attributing a detected defect to a specific piece.** Not a scoping choice
  but a physical impossibility with the current sensing (§4.4).
- **Backfilling history.** There is nothing to backfill from (§1.1).

---

## 12. Open questions

1. **Supabase project tier**, which decides whether model files are mirrored to
   Storage in a later phase (§11).
2. **Whether `parts` should be locally owned or cloud owned.** Local was chosen
   because the model file and the slicer both live on the LAN, and a part is
   normally defined by whoever has the STL. If parts turn out to be defined
   office-side in practice, this flips to a pulled table and the model file
   becomes a separate local concern.
3. **Whether a run should support more than one part** (a mixed plate). The
   current model assumes one part per run, with `copies_planned` copies. Mixed
   plates would need a `run_parts` join table. Deferred until it happens.

---

## 13. Documentation obligations

`server/tests/test_docs.py` will enforce these, so they are requirements, not
courtesies:

- This file carries a `STATUS:` banner in its first 800 characters. It does,
  and the banner says **not implemented** — because a spec marked "approved"
  for something never built is exactly what got picked up and started once
  already in this repo.
- Every relative markdown link resolves. Files that do not exist yet
  (`server/ledger.py`, the `sql/` DDL) are written as plain text here, not
  links.
- When Phase 1 ships, `master.md` gains a new numbered section and
  `docs/superpowers/README.md` gains an index row. Any `§N` added to master.md
  must match a real heading there.
