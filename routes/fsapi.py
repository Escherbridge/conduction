"""Filesystem API backing the folder picker: native OS dialog, directory
browsing, and repo path validation.

Rationale, threat model, and the native-dialog caveats live in
`routes/AGENTS.md` -- see "fsapi".
"""

from __future__ import annotations

import asyncio
import os
import string
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sanic import Blueprint
from sanic.response import json as sanic_json

bp = Blueprint("fsapi", url_prefix="/api/fs")

# The native dialog blocks its thread until the human answers it.
DIALOG_TIMEOUT_SECONDS = 300
MAX_LISTING_ENTRIES = 500

# Tk is not safe to drive from several threads at once, and on macOS it demands
# the thread that owns it. One dedicated worker thread owns every dialog, and
# one lock means one dialog at a time -- a second concurrent pick is refused
# rather than racing a second Tcl interpreter into the same process.
_dialog_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fs-dialog")
_dialog_lock = asyncio.Lock()

# Cocoa requires Tk on the process main thread; a worker thread there hangs or
# aborts the process. Everywhere else a dedicated thread is fine.
NATIVE_DIALOG_SUPPORTED = sys.platform != "darwin"


def require_local(request):
    """403 body for a non-local caller, or None when the caller is local.

    Every endpoint here reads the server's filesystem, so all of them are
    local-only -- and they stay that way once the server is reachable over a
    private network. Browsing the machine's disk is not part of the remote
    scope. See routes/AGENTS.md "fsapi" and AGENTS.md "access scopes".
    """
    if is_local_request(request):
        return None
    return sanic_json(
        {"error": "the filesystem API is only served to a browser on this machine"},
        status=403,
    )


def is_local_request(request) -> bool:
    """True when the caller is on this machine.

    Delegates to access.scope_for so there is one definition of "local", and so
    trust is decided on the real socket peer rather than a forwarded header.
    """
    import access

    return access.scope_for(request) == access.SCOPE_LOCAL


def describe_dir(path: Path) -> dict:
    """One directory entry, annotated with what makes it interesting to pick."""
    try:
        is_git = (path / ".git").exists()
    except OSError:
        is_git = False
    try:
        has_agentgraph = (path / ".agentgraph").is_dir()
    except OSError:
        has_agentgraph = False
    return {
        "name": path.name or str(path),
        "path": str(path),
        "is_git": is_git,
        "has_agentgraph": has_agentgraph,
    }


def drive_roots() -> list[Path]:
    """Windows has no single filesystem root; enumerate live drive letters."""
    if sys.platform != "win32":
        return [Path("/")]
    roots = []
    for letter in string.ascii_uppercase:
        candidate = Path(letter + ":\\")
        try:
            if candidate.exists():
                roots.append(candidate)
        except OSError:
            continue
    return roots


def listing_allowed(path: Path) -> bool:
    """Browsing is deliberately broader than launching: an ANCESTOR of an
    allowed root is listable so the user can navigate down into it, and each
    root's subtree is listable. Launching still goes through
    `validate_target_repo`, which is the narrower gate."""
    from app import allowed_repo_roots, read_known_repos

    anchors = list(allowed_repo_roots())
    for entry in read_known_repos():
        try:
            anchors.append(Path(entry).resolve())
        except OSError:
            continue
    for anchor in anchors:
        try:
            if path == anchor or path.is_relative_to(anchor) or anchor.is_relative_to(path):
                return True
        except (OSError, ValueError):
            continue
    return False


@bp.get("/roots")
async def fs_roots(request):
    """Starting points for the browser: allowed roots, known repos, drives."""
    denied = require_local(request)
    if denied is not None:
        return denied
    from app import allowed_repo_roots, read_known_repos

    seen: set[str] = set()
    roots: list[dict] = []

    def add(path: Path, kind: str) -> None:
        key = str(path).lower()
        if key in seen:
            return
        try:
            if not path.is_dir():
                return
        except OSError:
            return
        seen.add(key)
        entry = describe_dir(path)
        entry["kind"] = kind
        roots.append(entry)

    for root in allowed_repo_roots():
        add(root, "allowed-root")
    for entry in read_known_repos():
        try:
            add(Path(entry).resolve(), "known-repo")
        except OSError:
            continue
    for drive in drive_roots():
        add(drive, "drive")

    return sanic_json(
        {
            "roots": roots,
            "home": str(Path.home()),
            "native_dialog": NATIVE_DIALOG_SUPPORTED,
            "separator": os.sep,
        }
    )


@bp.get("/list")
async def fs_list(request):
    """Child directories of `path`, sorted, with git/agentgraph annotations.

    Directory names are attacker-shaped data (a folder can be named
    `<img src=x onerror=...>`); the client renders every one of these through
    escapeHtml. Never interpolate an entry raw.
    """
    denied = require_local(request)
    if denied is not None:
        return denied
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return sanic_json({"error": "path is required"}, status=400)
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return sanic_json({"error": "not a usable path: " + raw}, status=400)
    if not path.is_dir():
        return sanic_json({"error": "not a directory: " + raw}, status=404)
    if not listing_allowed(path):
        return sanic_json(
            {
                "error": f"outside the allowed roots: {path}. "
                "Set CONDUCTION_ALLOWED_ROOTS to permit it."
            },
            status=403,
        )

    entries = []
    truncated = False
    try:
        with os.scandir(path) as scanner:
            for item in scanner:
                if item.name.startswith(".") and item.name != ".agentgraph":
                    continue
                try:
                    if not item.is_dir():
                        continue
                except OSError:
                    continue
                entries.append(describe_dir(Path(item.path)))
                if len(entries) >= MAX_LISTING_ENTRIES:
                    truncated = True
                    break
    except PermissionError:
        return sanic_json({"error": f"permission denied: {path}"}, status=403)
    except OSError as error:
        return sanic_json({"error": f"cannot read {path}: {error}"}, status=400)

    entries.sort(key=lambda item: item["name"].lower())
    parent = path.parent
    show_parent = parent != path and listing_allowed(parent)
    return sanic_json(
        {
            "path": str(path),
            "parent": str(parent) if show_parent else None,
            "entries": entries,
            "truncated": truncated,
            "self": describe_dir(path),
        }
    )


@bp.get("/validate")
async def fs_validate(request):
    """Does this path work as a target repo, and what is it? Powers the inline
    badge beside every path field, so a bad path is visible before submit."""
    denied = require_local(request)
    if denied is not None:
        return denied
    from app import validate_target_repo

    raw = (request.args.get("path") or "").strip()
    if not raw:
        return sanic_json({"ok": False, "error": "path is required"})
    resolved, error = validate_target_repo(raw)
    if error:
        try:
            exists = Path(raw).expanduser().resolve().is_dir()
        except OSError:
            exists = False
        return sanic_json({"ok": False, "error": error, "exists": exists})
    info = describe_dir(resolved)
    info["ok"] = True
    return sanic_json(info)


def run_native_dialog(initial: str) -> dict:
    """Blocking tkinter directory chooser, run in Sanic's worker executor.

    Any failure degrades to `available: False` so the UI silently falls back to
    the in-browser directory browser instead of surfacing a dead end.
    """
    if not NATIVE_DIALOG_SUPPORTED:
        return {"available": False, "error": "native dialog needs the main thread on this platform"}
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as error:  # pragma: no cover - headless server
        return {"available": False, "error": f"native dialog unavailable: {error}"}

    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        chosen = filedialog.askdirectory(
            title="Select target repository",
            initialdir=initial or str(Path.home()),
            mustexist=True,
            parent=root,
        )
    except Exception as error:  # pragma: no cover - no display available
        return {"available": False, "error": f"native dialog failed: {error}"}
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    if not chosen:
        return {"available": True, "cancelled": True}
    picked = Path(chosen).resolve()
    result = describe_dir(picked)
    result["available"] = True
    result["cancelled"] = False
    return result


@bp.post("/pick")
async def fs_pick(request):
    """Open the OS folder chooser on the machine running the server."""
    if not is_local_request(request):
        return sanic_json(
            {
                "available": False,
                "error": "the native dialog opens on the server's desktop; use the browser instead",
            },
            status=403,
        )
    if _dialog_lock.locked():
        return sanic_json(
            {"available": False, "error": "a folder dialog is already open"}, status=409
        )
    payload = request.json if request.body else None
    initial = ""
    if isinstance(payload, dict):
        initial = str(payload.get("initial") or "").strip()
    try:
        initial_dir = str(Path(initial).expanduser().resolve()) if initial else ""
    except (OSError, ValueError):
        initial_dir = ""

    loop = asyncio.get_running_loop()
    async with _dialog_lock:
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_dialog_executor, run_native_dialog, initial_dir),
                timeout=DIALOG_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return sanic_json(
                {"available": True, "cancelled": True, "error": "dialog timed out"}, status=504
            )
    return sanic_json(result)
