"""HTTP route tests for conduction web app.

Tests all public routes against conduction's own historical runs under
.agentgraph/runs/factory-build/, which are visible when the server starts
with no env overrides. The runs have status "stale" (old runs this process
did not launch), and their IDs follow the pattern MISSION-<slug>@<8hex>.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from urllib.parse import quote, urlencode

import pytest


def http_get(base_url: str, path: str, timeout: float = 5.0) -> tuple[int, dict, bytes]:
    """
    Make a GET request and return (status_code, headers_dict, body_bytes).

    Uses http.client for fine control, particularly for SSE streams.
    """
    # Parse base_url to extract host and port
    if base_url.startswith("http://"):
        base_url = base_url[7:]
    host, port_str = base_url.split(":")
    port = int(port_str)

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        body = response.read()
        return response.status, headers, body
    finally:
        conn.close()


def http_post(base_url: str, path: str, json_body: dict | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    """Make a POST request with JSON body and return (status_code, body_bytes)."""
    if base_url.startswith("http://"):
        base_url = base_url[7:]
    host, port_str = base_url.split(":")
    port = int(port_str)

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        body = json.dumps(json_body or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        response_body = response.read()
        return response.status, response_body
    finally:
        conn.close()


def test_list_runs(app_server_factory):
    """GET /api/runs returns a list of runs with required fields."""
    server = app_server_factory()

    status, _headers, body = http_get(server.base_url, "/api/runs")
    assert status == 200, f"Expected 200, got {status}"

    runs = json.loads(body)
    assert isinstance(runs, list), f"Expected list, got {type(runs)}"
    assert len(runs) >= 1, "Expected at least 1 historical run"

    for run in runs:
        assert "run_id" in run, f"Missing run_id in {run}"
        assert "slug" in run
        assert "started_at" in run
        assert "status" in run
        assert "target_repo" in run

        # Every run_id is composite: MISSION-<slug>@<8hex>
        assert "@" in run["run_id"], f"run_id should contain '@': {run['run_id']}"


def test_get_run_agents_percent_encoded(app_server_factory):
    """GET /api/runs/<id>/agents works with percent-encoded and raw run IDs."""
    server = app_server_factory()

    # First get a run_id
    status, _headers, body = http_get(server.base_url, "/api/runs")
    assert status == 200
    runs = json.loads(body)
    assert len(runs) >= 1

    # Pick wave-a-vendor if available (known to have agents), else first run
    run_id = None
    for run in runs:
        if "wave-a-vendor" in run["run_id"]:
            run_id = run["run_id"]
            break
    if run_id is None:
        run_id = runs[0]["run_id"]

    # Test with percent-encoded ID
    encoded_id = quote(run_id, safe="")
    status, _headers, body = http_get(server.base_url, f"/api/runs/{encoded_id}/agents")
    assert status == 200, f"Percent-encoded request failed: {status}"

    data_encoded = json.loads(body)
    assert "agents" in data_encoded
    assert "target_repo" in data_encoded
    agents_encoded = data_encoded["agents"]

    # Test with raw ID (@ is safe in path, but testing both)
    status, _headers, body = http_get(server.base_url, f"/api/runs/{run_id}/agents")
    assert status == 200, f"Raw ID request failed: {status}"

    data_raw = json.loads(body)
    assert "agents" in data_raw
    assert "target_repo" in data_raw
    agents_raw = data_raw["agents"]

    # Both should return the same number of agents
    assert len(agents_encoded) == len(agents_raw), \
        "Encoded and raw requests returned different agent counts"

    # wave-a-vendor is known to have >0 agents
    if "wave-a-vendor" in run_id:
        assert len(agents_encoded) > 0, "wave-a-vendor should have agents"


def test_get_run_findings(app_server_factory):
    """GET /api/runs/<id>/findings returns findings."""
    server = app_server_factory()

    # Get wave-a-vendor run if available
    status, _headers, body = http_get(server.base_url, "/api/runs")
    assert status == 200
    runs = json.loads(body)

    run_id = None
    for run in runs:
        if "wave-a-vendor" in run["run_id"]:
            run_id = run["run_id"]
            break

    if run_id is None:
        pytest.skip("wave-a-vendor run not found")

    encoded_id = quote(run_id, safe="")
    status, _headers, body = http_get(server.base_url, f"/api/runs/{encoded_id}/findings")
    assert status == 200

    findings = json.loads(body)
    assert isinstance(findings, list)
    # wave-a-vendor is expected to have findings, but test the structure regardless
    for finding in findings:
        assert "seq" in finding
        assert "worker" in finding
        assert "topic" in finding
        assert "summary" in finding


def test_stream_run_events_sse(app_server_factory):
    """
    GET /api/runs/<id>/events streams SSE frames:
    - First frame: event: run-event with JSON data having seq/type/actor/ts/payload
    - Within 10s: event: run-complete with status "stale" for historical runs

    Uses http.client in a thread to handle the streaming response.
    """
    server = app_server_factory()

    # Get a run_id
    status, _headers, body = http_get(server.base_url, "/api/runs")
    assert status == 200
    runs = json.loads(body)
    assert len(runs) >= 1

    run_id = runs[0]["run_id"]
    encoded_id = quote(run_id, safe="")

    # Parse server URL
    base_url = server.base_url
    if base_url.startswith("http://"):
        base_url = base_url[7:]
    host, port_str = base_url.split(":")
    port = int(port_str)

    frames = []
    error_holder = [None]

    def read_stream():
        try:
            conn = http.client.HTTPConnection(host, port, timeout=15)
            conn.request("GET", f"/api/runs/{encoded_id}/events")
            response = conn.getresponse()

            # Check headers
            assert response.status == 200
            content_type = response.getheader("Content-Type")
            assert content_type == "text/event-stream", f"Wrong content type: {content_type}"

            # Read SSE frames line by line with a deadline
            deadline = time.monotonic() + 10
            current_event = None
            current_data = None

            while time.monotonic() < deadline:
                line = response.readline()
                if not line:
                    break

                line = line.decode("utf-8").rstrip("\r\n")

                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    current_data = line[5:].strip()
                elif line == "" and current_event and current_data:
                    # Frame complete
                    frames.append({
                        "event": current_event,
                        "data": json.loads(current_data)
                    })

                    # If we got run-complete, we're done
                    if current_event == "run-complete":
                        break

                    current_event = None
                    current_data = None

            conn.close()
        except Exception as e:
            error_holder[0] = e

    thread = threading.Thread(target=read_stream)
    thread.start()
    thread.join(timeout=12)

    if error_holder[0]:
        raise error_holder[0]

    assert len(frames) > 0, "Expected at least one SSE frame"

    # First frame should be run-event with proper structure
    first_frame = frames[0]
    assert first_frame["event"] == "run-event", f"First event should be run-event, got {first_frame['event']}"

    event_data = first_frame["data"]
    assert "seq" in event_data
    assert "type" in event_data
    assert "actor" in event_data
    assert "ts" in event_data
    assert "payload" in event_data

    # Payload should be an object, not a string
    assert isinstance(event_data["payload"], dict), \
        f"payload should be a dict, got {type(event_data['payload'])}"

    # Last frame should be run-complete with status "stale"
    last_frame = frames[-1]
    assert last_frame["event"] == "run-complete", \
        f"Last event should be run-complete, got {last_frame['event']}"
    assert last_frame["data"]["status"] == "stale", \
        f"Historical run should have status 'stale', got {last_frame['data']['status']}"


def test_stream_unknown_run_404(app_server_factory):
    """GET /api/runs/<unknown>/events returns 404 JSON."""
    server = app_server_factory()

    unknown_id = "MISSION-nonexistent@deadbeef"
    encoded_id = quote(unknown_id, safe="")

    status, _headers, body = http_get(server.base_url, f"/api/runs/{encoded_id}/events")
    assert status == 404

    data = json.loads(body)
    assert "error" in data


def test_post_runs_validation(app_server_factory):
    """POST /api/runs validates slug, target_repo, and agents."""
    server = app_server_factory()

    # Missing target_repo → 400
    status, body = http_post(server.base_url, "/api/runs", {
        "slug": "test-mission",
        "agents": [{"name": "agent1", "brief": "Do X"}]
    })
    assert status == 400
    data = json.loads(body)
    assert "error" in data

    # Slug with ".." → 400
    status, body = http_post(server.base_url, "/api/runs", {
        "slug": "../escape",
        "target_repo": "C:\\Users\\atooz\\Programming\\conduction",
        "agents": [{"name": "agent1", "brief": "Do X"}]
    })
    assert status == 400
    data = json.loads(body)
    assert "slug" in data["error"]

    # Slug with "/" → 400
    status, body = http_post(server.base_url, "/api/runs", {
        "slug": "bad/slug",
        "target_repo": "C:\\Users\\atooz\\Programming\\conduction",
        "agents": [{"name": "agent1", "brief": "Do X"}]
    })
    assert status == 400
    data = json.loads(body)
    assert "error" in data

    # target_repo outside allowed roots → 400 mentioning allowed roots
    status, body = http_post(server.base_url, "/api/runs", {
        "slug": "test-mission",
        "target_repo": "C:\\Windows",
        "agents": [{"name": "agent1", "brief": "Do X"}]
    })
    assert status == 400
    data = json.loads(body)
    assert "error" in data
    assert "allowed" in data["error"].lower() or "root" in data["error"].lower()

    # Empty agents → 400
    status, body = http_post(server.base_url, "/api/runs", {
        "slug": "test-mission",
        "target_repo": "C:\\Users\\atooz\\Programming\\conduction",
        "agents": []
    })
    assert status == 400
    data = json.loads(body)
    assert "error" in data


def test_interrupt_unknown_run_404(app_server_factory):
    """POST /api/runs/<unknown>/interrupt returns 404."""
    server = app_server_factory()

    unknown_id = "MISSION-nonexistent@deadbeef"
    encoded_id = quote(unknown_id, safe="")

    status, body = http_post(server.base_url, f"/api/runs/{encoded_id}/interrupt")
    assert status == 404

    data = json.loads(body)
    assert "error" in data


def test_query_findings(app_server_factory):
    """GET /api/query/findings?text=... returns matching findings."""
    server = app_server_factory()

    # Query for "relocation" (a term likely in factory-build findings)
    query_string = urlencode({"text": "relocation"})
    status, _headers, body = http_get(server.base_url, f"/api/query/findings?{query_string}")
    assert status == 200

    findings = json.loads(body)
    assert isinstance(findings, list)
    # Expect at least one match (factory-build has relocation findings)
    assert len(findings) >= 1, "Expected findings matching 'relocation'"

    for finding in findings:
        assert "run_id" in finding
        assert "seq" in finding
        assert "worker" in finding
        assert "topic" in finding
        assert "summary" in finding
        assert "slug" in finding


def test_query_costs(app_server_factory):
    """GET /api/query/costs returns cost aggregations."""
    server = app_server_factory()

    status, _headers, body = http_get(server.base_url, "/api/query/costs")
    assert status == 200

    data = json.loads(body)
    assert "by_run" in data
    assert "by_agent" in data

    assert isinstance(data["by_run"], list)
    assert isinstance(data["by_agent"], list)

    # Each by_run entry should have the expected fields
    for entry in data["by_run"]:
        assert "run_id" in entry
        assert "slug" in entry
        assert "total_cost_usd" in entry
        assert "total_turns" in entry

    # Each by_agent entry should have the expected fields
    for entry in data["by_agent"]:
        assert "agent_name" in entry
        assert "total_cost_usd" in entry
        assert "total_turns" in entry
        assert "run_count" in entry


def test_query_claim_conflicts(app_server_factory):
    """GET /api/query/claims/conflicts returns claim conflicts."""
    server = app_server_factory()

    status, _headers, body = http_get(server.base_url, "/api/query/claims/conflicts")
    assert status == 200

    conflicts = json.loads(body)
    assert isinstance(conflicts, list)

    # Structure check (may be empty if no conflicts)
    for conflict in conflicts:
        assert "run_id" in conflict
        assert "slug" in conflict
        assert "path" in conflict
        assert "owner" in conflict
        assert "status" in conflict
        assert "seq" in conflict


def test_html_routes(app_server_factory):
    """GET /, /runs, /runs/<id>, /query return HTML pages."""
    server = app_server_factory()

    # GET / → 200 text/html
    status, headers, body = http_get(server.base_url, "/")
    assert status == 200
    content_type = headers.get("content-type", "")
    assert "text/html" in content_type
    assert b"<!DOCTYPE html>" in body or b"<html" in body.lower()

    # GET /runs → 200 text/html
    status, headers, body = http_get(server.base_url, "/runs")
    assert status == 200
    content_type = headers.get("content-type", "")
    assert "text/html" in content_type

    # GET /runs/<id> → 200 text/html
    # Get a real run_id first
    status, _headers, body = http_get(server.base_url, "/api/runs")
    assert status == 200
    runs = json.loads(body)
    assert len(runs) >= 1
    run_id = runs[0]["run_id"]
    encoded_id = quote(run_id, safe="")

    status, headers, body = http_get(server.base_url, f"/runs/{encoded_id}")
    assert status == 200
    content_type = headers.get("content-type", "")
    assert "text/html" in content_type

    # GET /query → 200 text/html
    status, headers, body = http_get(server.base_url, "/query")
    assert status == 200
    content_type = headers.get("content-type", "")
    assert "text/html" in content_type
