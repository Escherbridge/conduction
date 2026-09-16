"""Tests for the folder-picker filesystem API (routes/fsapi.py).

Every test drives a real server with CONDUCTION_ALLOWED_ROOTS pinned at a tmp
tree, so the assertions never depend on the developer's home directory -- and
so the allow/deny boundary is a real boundary rather than "everything under
$HOME happens to pass".
"""

from __future__ import annotations

import pytest
import requests


@pytest.fixture
def sandbox(tmp_path):
    """A tiny repo tree: allowed/repo-a (git + runs), allowed/plain, outside/."""
    allowed = tmp_path / "allowed"
    repo_a = allowed / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    (repo_a / ".agentgraph").mkdir()
    (repo_a / "src").mkdir()
    (allowed / "plain").mkdir()
    (allowed / ".hidden").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return {"root": tmp_path, "allowed": allowed, "repo_a": repo_a, "outside": outside}


@pytest.fixture
def server(app_server_factory, sandbox):
    return app_server_factory(
        {
            "CONDUCTION_ALLOWED_ROOTS": str(sandbox["allowed"]),
            "CONDUCTION_DRY_RUN": "1",
            "CONDUCTION_SCHEDULER": "0",
        }
    )


def test_roots_lists_allowed_root_and_reports_native_dialog(server, sandbox):
    data = requests.get(server.base_url + "/api/fs/roots", timeout=5).json()
    paths = [entry["path"] for entry in data["roots"]]
    assert str(sandbox["allowed"]) in paths
    # Tests connect over loopback, so the native dialog is on the table.
    assert data["native_dialog"] is True
    assert data["home"]


def test_list_returns_only_directories_with_annotations(server, sandbox):
    data = requests.get(
        server.base_url + "/api/fs/list",
        params={"path": str(sandbox["allowed"])},
        timeout=5,
    ).json()
    by_name = {entry["name"]: entry for entry in data["entries"]}
    assert set(by_name) == {"repo-a", "plain"}  # .hidden is filtered out
    assert by_name["repo-a"]["is_git"] is True
    assert by_name["repo-a"]["has_agentgraph"] is True
    assert by_name["plain"]["is_git"] is False


def test_list_offers_parent_only_while_it_stays_browsable(server, sandbox):
    """The parent of an allowed root is browsable (you must be able to navigate
    down to it); its sibling subtree is not."""
    at_root = requests.get(
        server.base_url + "/api/fs/list", params={"path": str(sandbox["allowed"])}, timeout=5
    ).json()
    assert at_root["parent"] == str(sandbox["root"])

    denied = requests.get(
        server.base_url + "/api/fs/list", params={"path": str(sandbox["outside"])}, timeout=5
    )
    assert denied.status_code == 403
    assert "allowed roots" in denied.json()["error"]


def test_list_rejects_missing_and_empty_paths(server, sandbox):
    assert requests.get(server.base_url + "/api/fs/list", timeout=5).status_code == 400
    missing = requests.get(
        server.base_url + "/api/fs/list",
        params={"path": str(sandbox["allowed"] / "nope")},
        timeout=5,
    )
    assert missing.status_code == 404


def test_validate_accepts_an_allowed_repo_and_describes_it(server, sandbox):
    data = requests.get(
        server.base_url + "/api/fs/validate",
        params={"path": str(sandbox["repo_a"])},
        timeout=5,
    ).json()
    assert data["ok"] is True
    assert data["is_git"] is True
    assert data["has_agentgraph"] is True
    assert data["path"] == str(sandbox["repo_a"])


def test_validate_distinguishes_not_found_from_not_allowed(server, sandbox):
    outside = requests.get(
        server.base_url + "/api/fs/validate", params={"path": str(sandbox["outside"])}, timeout=5
    ).json()
    assert outside["ok"] is False
    assert outside["exists"] is True  # it is there, it is just off-limits

    missing = requests.get(
        server.base_url + "/api/fs/validate",
        params={"path": str(sandbox["allowed"] / "ghost")},
        timeout=5,
    ).json()
    assert missing["ok"] is False
    assert missing["exists"] is False


def test_validate_requires_a_path(server):
    data = requests.get(server.base_url + "/api/fs/validate", timeout=5).json()
    assert data["ok"] is False
    assert "required" in data["error"]


def test_every_endpoint_is_gated_on_loopback():
    """CONDUCTION_HOST=0.0.0.0 must not turn this into a LAN filesystem browser:
    a non-loopback caller is refused by `require_local`, not just by /pick."""
    from routes.fsapi import require_local

    class FakeRequest:
        def __init__(self, addr):
            self.remote_addr = addr
            self.ip = addr

    assert require_local(FakeRequest("127.0.0.1")) is None
    denied = require_local(FakeRequest("10.1.2.3"))
    assert denied is not None and denied.status == 403


def test_pick_is_gated_on_loopback():
    """The native dialog opens on the SERVER's desktop, so a LAN client must be
    refused and fall back to the in-browser browser.

    Asserted against the predicate rather than over HTTP: a real POST to
    /api/fs/pick from a loopback test client would open a modal dialog and
    block until a human dismissed it.
    """
    from routes.fsapi import is_local_request

    class FakeRequest:
        def __init__(self, addr):
            self.remote_addr = addr
            self.ip = addr

    assert is_local_request(FakeRequest("127.0.0.1")) is True
    assert is_local_request(FakeRequest("::1")) is True
    # A peerless request fails CLOSED: empty `request.ip` happens on UNIX
    # sockets and ASGI servers that report no client, and treating that as local
    # would hand the filesystem API to any such deployment.
    assert is_local_request(FakeRequest("")) is False
    assert is_local_request(FakeRequest("10.1.2.3")) is False
    assert is_local_request(FakeRequest("192.168.0.7")) is False


def test_native_dialog_failure_degrades_instead_of_erroring(monkeypatch):
    """A headless server must report `available: False` so the UI silently
    switches to the in-browser browser -- never a dead-end error."""
    import builtins

    from routes import fsapi

    real_import = builtins.__import__

    def no_tkinter(name, *args, **kwargs):
        if name == "tkinter":
            raise ImportError("no display")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tkinter)
    result = fsapi.run_native_dialog("")
    assert result["available"] is False
    assert "unavailable" in result["error"]
