# routes/ — API blueprints

Each module here is a Sanic `Blueprint` registered in `app.py` after the shared
helpers exist. Handlers reach app-level helpers with a lazy `from app import ...`
*inside* the function: `app.py` imports these modules, so a module-level import
would be circular.

## config

Rules, goals and schedules — ecosystem-wide (`ecosystem.json`) and per-project
(`<repo>/.agentgraph/project.json`).

## observe

Fleet observability: live agent counts, activity feed, cross-run summaries.
Registered inside a `try/except ImportError` so the app still boots without it.

## scheduler

The 60-second ticker that launches due schedules. `CONDUCTION_SCHEDULER=0`
disables it; the test fixtures always do, so an explicit "run now" assertion is
never racing the background ticker.

## fsapi

Backs the folder picker (`static/js/folder-picker.js`): `GET /api/fs/roots`,
`GET /api/fs/list`, `GET /api/fs/validate`, `POST /api/fs/pick`.

**Why a server-side filesystem API at all.** A browser cannot hand a page a
real directory path — `<input type="file" webkitdirectory>` yields file blobs
with relative names, never the absolute path the mission launcher needs. But
Conduction's server runs on the same machine as the browser, so the server can
read the filesystem and even open the real OS chooser on that machine's
desktop. That is what these endpoints do.

**Threat model.** Every endpoint here reads the server's filesystem and there is
no authentication anywhere in the app. The default bind is loopback, but
`CONDUCTION_HOST=0.0.0.0` is a documented, supported setting — and under it a
LAN client could otherwise walk `GET /api/fs/list?path=C:\Users` and enumerate
every account on the box. So **all four endpoints are gated on
`require_local()`** (loopback remote address) and return 403 otherwise. LAN
users keep every other page; they just type paths by hand.

**Why `listing_allowed()` accepts ancestors.** The gate for *browsing* is
deliberately wider than the gate for *launching*. `listing_allowed()` accepts a
path that is an allowed root, inside one, **or an ancestor of one**, because a
user who starts at `C:\` must be able to walk down to the allowed root — a
browser that refuses to list the parent cannot navigate. This widening is safe
only because of the loopback gate above; it is not a second layer of defence in
its own right. Launching is unaffected: `POST /api/runs` still goes through
`app.validate_target_repo()`, which requires the repo be an allowed root, under
one, or already registered. Browsing somewhere never makes it launchable.

**Native dialog caveats.**

- Tcl/Tk is not safe to drive from several threads at once, and a second
  `Tk()` racing the first in the same process can abort the whole server
  (`app.run(single_process=True)`). One module-level `ThreadPoolExecutor(1)`
  owns every dialog and an `asyncio.Lock` allows one at a time; a second
  concurrent pick gets 409, not a race.
- macOS Cocoa requires Tk on the process main thread, which a Sanic handler
  never is. `NATIVE_DIALOG_SUPPORTED` is therefore false on `darwin`, and
  `/api/fs/roots` reports it so the client goes straight to the in-browser
  browser instead of offering a button that would hang.
- Every failure path returns `{"available": false}` rather than an error, so the
  client falls back silently. A *cancelled* dialog is distinct
  (`{"available": true, "cancelled": true}`) and means "never mind" — the client
  must not then pop the fallback browser.
- The dialog blocks its thread until a human answers; `DIALOG_TIMEOUT_SECONDS`
  (300) caps that.

**Untrusted data.** Directory names come from the filesystem and can contain
anything, including `<img src=x onerror=...>`. The client renders every name and
path through `escapeHtml`. Never interpolate a listing entry raw.
