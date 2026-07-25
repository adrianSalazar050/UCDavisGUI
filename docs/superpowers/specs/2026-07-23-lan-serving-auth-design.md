# Serving the dashboard to the lab, with a shared password — design

> **STATUS: SHIPPED (2026-07-23, commit `ff87e8d`).** Implemented as designed
> below and verified in a real browser against a password-protected server:
> the login screen renders, a wrong password is rejected with the server's
> own message, the correct password reveals the dashboard, and logout
> re-closes the API. Supersedes nothing — it adds a second, simpler way to
> reach the app alongside the desktop installer.
>
> Historical record, not maintained. **`master.md` §2.1 is authoritative
> wherever this file disagrees with it.**

Date: 2026-07-23

---

## 1. The goal, and why this beats shipping an installer

"Make it accessible to all people" started as *package a desktop app for
everyone*. But the app **is already a website**: FastAPI serves the built React
frontend, and the Electron app is only a window pointed at it (`master.md`, the
desktop section: "for people who won't run a terminal").

For a shared lab the better answer is therefore to run **one** server on the LAN
and let everyone open a URL — no install, any OS, works on a phone, one place to
update, and the detector (when it exists) runs once instead of on every desk.

**The constraint that rules out a hosted website.** The printer is reachable only
at a private address over MQTT (8883), FTPS (990), and raw TCP (6000). A browser
cannot open those sockets and the public internet cannot route to `192.168.x.x`.
*Something must run on the LAN.* The only real question is where that process
lives; hosting the UI elsewhere changes nothing about it.

## 2. What blocks it today

1. `server/__main__.py` hardcodes `uvicorn.run(app, host="127.0.0.1", ...)`.
   There is no way to bind anywhere else.
2. **There is no authentication anywhere in the API.** Binding to `0.0.0.0` as
   it stands would let anyone on the campus network stop a print, upload files,
   or command a printer.

## 3. Design

### 3.1 `--host`, defaulting to localhost

A `--host` flag on `python -m server`, default `127.0.0.1`. Exposure is opt-in;
it can never happen by forgetting a flag.

### 3.2 Fail closed

If `--host` resolves to anything other than a loopback address **and no password
is configured**, the server **refuses to start** and says why. Binding a
printer-control API to a shared network with no auth must not be a thing you can
do by accident. This is the single most important rule in this document.

### 3.3 Localhost stays unauthenticated

Loopback binds require no password. This keeps the desktop app working (it
spawns its own backend on a random port and would have nowhere to type one) and
leaves the dev workflow untouched.

Consequence to be explicit about: anyone with a shell on the server machine can
reach the API without logging in. For a lab PC that is an acceptable trade —
they already have the machine.

### 3.4 The password

Read from **`BAMBU_PASSWORD`** in the environment. Never a committed file, never
a CLI argument (argv is visible to any process listing — the same reasoning
`DetectorSupervisor.build_env` already applies to the printer access code).

Only a SHA-256 hash is held in memory, compared with `hmac.compare_digest` so a
wrong guess can't be timed character-by-character.

### 3.5 Session cookie, and why not a bearer token

Login sets an **HttpOnly** cookie holding a random `secrets.token_urlsafe(32)`,
tracked in an in-memory set. A restart invalidates sessions, which is fine.

The mechanism is forced by a real constraint: the dashboard's live updates run
over a **WebSocket**, and browsers cannot attach custom headers to a WS
handshake. A bearer token in `Authorization` therefore cannot protect `/ws`
without inventing a side channel. Cookies ride the handshake automatically, so
one mechanism covers `/api/*` and `/ws` alike.

### 3.6 Shape of the code

A new `server/auth.py` holding the whole concern: hashing, session issue and
check. `create_app(..., auth=None)` gains the parameter, and **`None` means
inert** — no authentication at all — exactly the convention `queue=None`,
`detection=None`, and `slicer=None` already use in this file.

Enforcement is **middleware**, not a route dependency, because FastAPI
dependencies do not cover WebSocket routes the same way. It protects `/api/*`
and `/ws`, and deliberately does *not* protect the static frontend — the login
page has to load before anyone can log in.

Routes: `POST /api/login` (`{"password": ...}`) → 200 + `Set-Cookie`, or 401;
`POST /api/logout` → clears it. Cookie is `HttpOnly`, `SameSite=Lax`, `Path=/`.

### 3.7 Frontend

A login screen shown when any API call returns 401, posting to `/api/login` and
then re-mounting the app. The cookie is HttpOnly, so the frontend stores no
credential itself.

## 4. The limitation, stated plainly

This is **plain HTTP over the LAN**. The password stops casual access — someone
wandering onto the network cannot stop your prints — but it is **not
encrypted**, so anyone able to sniff campus traffic can capture it. Real TLS
means certificates and self-signed browser warnings, which is a bigger change.

For a lab network this is a reasonable trade, and it is a deliberate, recorded
one rather than an oversight.

Brute-force protection is **out of scope**: there is no login rate limit. A
small delay on failed attempts is an easy follow-up if the network is less
trusted than assumed.

## 5. Testing

- Unauthenticated requests to every protected route family return 401 —
  including the WebSocket handshake.
- `/api/login` accepts the right password and rejects a wrong one; comparison
  uses `compare_digest`.
- Static assets and `/api/login` stay reachable without a session.
- `auth=None` leaves every route open (the desktop app path).
- The fail-closed rule: constructing the server non-local without a password
  raises rather than binding.
- Loopback detection treats `127.0.0.1`, `localhost`, and `::1` as local.

## 6. Out of scope

- TLS / HTTPS (see §4).
- Per-user accounts. A shared password was chosen deliberately; **Supabase is
  not needed** for this design and adds an external dependency, an internet
  requirement, and a place for LAN credentials to leak.
- Remote access from outside the LAN. That needs a relay and a much more
  serious security design; it is a separate project if it is ever wanted.
- The ONNX detector backend — a separate spec. This one only makes the app
  reachable; it does not change what the app does.
