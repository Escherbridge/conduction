"""Claim-path validation hardening for `Host.worker_claim`.

Today `Host.worker_claim` (host.py:473-496) only checks the ledger for a
conflicting *owner* — it never checks that the claimed path is sane: it can
be outside the mission's `claim_root`, an MSYS-style `/c/...` path that
`Path.resolve()` silently mangles on Windows, or a path whose parent
directory does not even exist (a near-certain typo). None of that is
validated yet, so most assertions here fail against today's code — that is
the point: they pin the still-to-land contract.

Requests also carry a `meta` dict (`AgentRequest.meta` -> `agent.requested`
event payload's `"meta"` key, see dispatcher.py). The hardening under test
reads an `owns=("prefix/",)` entry out of that meta to restrict what a
worker may claim to its declared partition, on top of the root/existence
checks above.

Everything here talks to `Host.worker_claim` directly with a hand-built
`agent.requested`-shaped `Event`, so it never needs a live worker or the
async run loop.
"""

from __future__ import annotations

from pathlib import Path

from activegraph import Event, FrozenClock, Graph, IDGen, Runtime

from agentgraph import Host, ScriptedWorker


def make_host(*, claim_root: str) -> tuple[Host, Graph]:
    graph = Graph(ids=IDGen(), clock=FrozenClock("2026-08-21T00:00:00Z"), run_id="RUN-CLAIMS")
    runtime = Runtime(graph, behaviors=[])
    host = Host(runtime, ScriptedWorker(lambda r, a: "ok"), claim_root=claim_root)
    return host, graph


def make_request_event(graph: Graph, *, worker: str = "w", meta: dict | None = None) -> Event:
    return Event(
        id=graph.ids.event(),
        type="agent.requested",
        payload={"worker": worker, "meta": dict(meta or {})},
        actor=worker,
        timestamp=graph.clock.now(),
    )


def test_claim_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host, graph = make_host(claim_root=str(root))
    event = make_request_event(graph)

    outside = str(tmp_path / "sibling" / "escape.py")
    result = host.worker_claim("w", [outside], event)

    assert result["granted"] is False


def test_msys_style_path_is_rejected_with_a_windows_drive_hint(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host, graph = make_host(claim_root=str(root))
    event = make_request_event(graph)

    # MSYS/Git-Bash rewrites `C:/Users/...` as `/c/Users/...`; a worker that
    # copy-pastes one of these into graph_claim must be told to use the
    # Windows form, not have it silently resolved to nonsense.
    msys_path = "/c/Users/dev/projects/conduction/agentgraph/host.py"
    result = host.worker_claim("w", [msys_path], event)

    assert result["granted"] is False
    invalid = result.get("invalid") or []
    reason = " ".join(entry.get("reason", "") for entry in invalid)
    assert "C:/" in reason or "C:\\" in reason, (
        f"rejection reason did not point the worker at the Windows form: {result!r}"
    )


def test_claim_whose_parent_directory_is_missing_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host, graph = make_host(claim_root=str(root))
    event = make_request_event(graph)

    missing_parent = str(root / "does_not_exist" / "file.py")
    result = host.worker_claim("w", [missing_parent], event)

    assert result["granted"] is False


def test_claim_of_a_valid_new_file_under_root_is_granted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host, graph = make_host(claim_root=str(root))
    event = make_request_event(graph)

    new_file = str(root / "new_file.py")  # does not exist yet; that's fine
    result = host.worker_claim("w", [new_file], event)

    assert result["granted"] is True


def test_owns_partition_restricts_claims_to_the_declared_prefix(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "other").mkdir(parents=True)
    host, graph = make_host(claim_root=str(root))
    event = make_request_event(graph, meta={"owns": ("src/",)})

    in_partition = host.worker_claim("w", [str(root / "src" / "x.py")], event)
    out_of_partition = host.worker_claim("w", [str(root / "other" / "y.py")], event)

    assert in_partition["granted"] is True
    assert out_of_partition["granted"] is False
