"""Policy data: validation, round-trips, rule application, and schedules."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentgraph import policy
from agentgraph.factory import FactoryRunner, FactorySpec, FactoryWave
from agentgraph.log import read_events
from agentgraph.manifest import AGENTGRAPH_GITIGNORE, read_mission_manifest
from agentgraph.mission import EDIT_TOOLS, READ_TOOLS, AgentSpec


def rule(rule_id, text, scope="all", enabled=True):
    return {"id": rule_id, "text": text, "scope": scope, "enabled": enabled}


def schedule(schedule_id="s1", **overrides):
    base = {
        "id": schedule_id,
        "kind": "factory",
        "factory_path": ".agentgraph/factory.json",
        "every": "30m",
        "cron": None,
        "enabled": True,
        "last_run_at": None,
        "last_factory_run_id": None,
    }
    base.update(overrides)
    return base


# ---- validation ---------------------------------------------------------


def test_valid_docs_have_no_errors():
    ecosystem = policy.empty_ecosystem()
    ecosystem["rules"] = [rule("r1", "always run tests")]
    ecosystem["goals"] = [
        {
            "id": "g1",
            "title": "ship",
            "description": "",
            "status": "open",
            "linked_runs": [],
            "updated_at": "2026-08-21T00:00:00Z",
        }
    ]
    ecosystem["schedules"] = [schedule(target_repo="C:/repo")]
    assert policy.validate_ecosystem(ecosystem) == []

    project = policy.empty_project("demo")
    project["rules"] = [rule("r2", "no new deps", scope="writers")]
    project["schedules"] = [schedule("s2", every=None, cron="0 2 * * *")]
    assert policy.validate_project(project) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d.update(schema=2), "schema must be 1"),
        (
            lambda d: d["rules"].append({"id": "", "text": "x", "scope": "all", "enabled": True}),
            "rules[0].id",
        ),
        (lambda d: d["rules"].append(rule("r", "t", scope="nobody")), "scope must be"),
        (
            lambda d: d["rules"].append({"id": "r", "text": "t", "scope": "all", "enabled": "yes"}),
            "enabled must be a boolean",
        ),
        (lambda d: d["goals"].append({"id": "g", "title": "t", "status": "wat"}), "status must be"),
        (
            lambda d: d["schedules"].append(schedule(every=None, cron=None, target_repo="r")),
            "one of every or cron",
        ),
        (
            lambda d: d["schedules"].append(schedule(every="17x", target_repo="r")),
            "every must look like",
        ),
        (
            lambda d: d["schedules"].append(schedule(every=None, cron="0 2 * *", target_repo="r")),
            "cron must be 5 fields",
        ),
        (
            lambda d: d["schedules"].append(schedule(kind="nonsense", target_repo="r")),
            "kind must be",
        ),
        (lambda d: d["schedules"].append(schedule()), "target_repo is required"),
    ],
)
def test_ecosystem_validation_errors(mutate, fragment):
    doc = policy.empty_ecosystem()
    mutate(doc)
    errors = policy.validate_ecosystem(doc)
    assert any(fragment in error for error in errors), errors


def test_project_validation_rejects_non_dict_and_bad_name():
    assert policy.validate_project(["nope"]) == ["project must be a dict"]
    doc = policy.empty_project("x")
    doc["name"] = 7
    assert "name must be a string" in policy.validate_project(doc)


# ---- load / save --------------------------------------------------------


def test_missing_files_load_as_empty_docs(tmp_path):
    assert policy.load_ecosystem(tmp_path) == policy.empty_ecosystem()
    project = policy.load_project(tmp_path)
    assert project["schema"] == 1 and project["rules"] == []
    assert project["name"] == tmp_path.name
    assert policy.validate_project(project) == []


def test_save_load_round_trip_is_atomic(tmp_path):
    ecosystem = policy.empty_ecosystem()
    ecosystem["rules"] = [rule("r1", "cite path:line")]
    path = policy.save_ecosystem(tmp_path, ecosystem)
    assert path == tmp_path / ".agentgraph" / "ecosystem.json"
    assert not path.with_suffix(".json.tmp").exists()
    assert policy.load_ecosystem(tmp_path) == ecosystem

    project = policy.empty_project("demo")
    project["goals"] = [
        {
            "id": "g",
            "title": "t",
            "description": "",
            "status": "done",
            "linked_runs": ["run-1"],
            "updated_at": "2026-08-21T00:00:00Z",
        }
    ]
    policy.save_project(tmp_path, project)
    assert policy.load_project(tmp_path) == project


def test_corrupt_file_falls_back_to_empty(tmp_path):
    target = policy.project_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    assert policy.load_project(tmp_path)["rules"] == []


def test_gitignore_versions_both_policy_docs():
    for name in ("!project.json", "!ecosystem.json", "!factory.json"):
        assert name in AGENTGRAPH_GITIGNORE.splitlines()


# ---- effective_rules / rules_block -------------------------------------


def test_effective_rules_orders_ecosystem_first_and_drops_disabled():
    ecosystem = {"rules": [rule("e1", "eco one"), rule("e2", "off", enabled=False)]}
    project = {"rules": [rule("p1", "proj one"), rule("p2", "off", enabled=False)]}
    merged = policy.effective_rules(ecosystem, project)
    assert [r["id"] for r in merged] == ["e1", "p1"]
    assert policy.effective_rules(None, None) == []


def test_rules_block_filters_by_scope():
    rules = [
        rule("a", "everyone"),
        rule("w", "writers only", scope="writers"),
        rule("r", "readers only", scope="readers"),
    ]
    writer_block = policy.rules_block(rules, writer=True)
    reader_block = policy.rules_block(rules, writer=False)
    assert writer_block.startswith(policy.RULES_MARKER)
    assert "writers only" in writer_block and "readers only" not in writer_block
    assert "readers only" in reader_block and "writers only" not in reader_block
    assert policy.rules_block([], writer=True) == ""
    assert policy.rules_block([rule("w", "w", scope="writers")], writer=False) == ""


# ---- apply_rules --------------------------------------------------------


def specs():
    return [
        AgentSpec(name="reader", brief="read things", tools=READ_TOOLS),
        AgentSpec(name="writer", brief="write things", tools=EDIT_TOOLS, owns=("a.py",)),
    ]


def test_apply_rules_only_gives_writers_writer_scoped_rules():
    rules = [rule("a", "everyone"), rule("w", "writers only", scope="writers")]
    reader, writer = policy.apply_rules(specs(), rules)
    assert "everyone" in reader.brief and "writers only" not in reader.brief
    assert "everyone" in writer.brief and "writers only" in writer.brief
    assert reader.brief.endswith("read things")
    assert writer.owns == ("a.py",) and writer.tools == EDIT_TOOLS


def test_apply_rules_is_idempotent():
    rules = [rule("a", "everyone")]
    once = policy.apply_rules(specs(), rules)
    twice = policy.apply_rules(once, rules)
    assert [s.brief for s in once] == [s.brief for s in twice]
    assert once[0].brief.count(policy.RULES_MARKER) == 1


def test_apply_rules_with_no_rules_returns_briefs_untouched():
    assert [s.brief for s in policy.apply_rules(specs(), [])] == [
        "read things",
        "write things",
    ]


def test_rules_fact_shape():
    topic, summary, detail = policy.rules_fact([rule("w", "no deps", scope="writers")])
    assert (topic, summary) == ("rules", "Binding rules for this run")
    assert "[writers] no deps" in detail


# ---- next_due -----------------------------------------------------------

NOW = datetime(2026, 8, 21, 12, 0)


@pytest.mark.parametrize(
    "every, delta",
    [("30m", timedelta(minutes=30)), ("6h", timedelta(hours=6)), ("1d", timedelta(days=1))],
)
def test_next_due_every(every, delta):
    sched = schedule(every=every)
    last = datetime(2026, 8, 21, 8, 0)
    assert policy.next_due(sched, NOW, last) == last + delta
    # Never run before: due immediately.
    assert policy.next_due(sched, NOW, None) == NOW


@pytest.mark.parametrize(
    "cron, now, expected",
    [
        ("0 2 * * *", datetime(2026, 8, 21, 12, 0), datetime(2026, 8, 22, 2, 0)),
        ("0 2 * * *", datetime(2026, 8, 21, 1, 30), datetime(2026, 8, 21, 2, 0)),
        ("*/15 * * * *", datetime(2026, 8, 21, 12, 1), datetime(2026, 8, 21, 12, 15)),
        ("*/15 * * * *", datetime(2026, 8, 21, 12, 46), datetime(2026, 8, 21, 13, 0)),
        # DOM 1-5 or DOW Mon/Wed -- Vixie union. 2026-08-21 is a Friday.
        ("0 9 1-5 * 1,3", datetime(2026, 8, 21, 12, 0), datetime(2026, 8, 24, 9, 0)),
        ("0 9 1-5 * 1,3", datetime(2026, 8, 31, 12, 0), datetime(2026, 9, 1, 9, 0)),
    ],
)
def test_next_due_cron(cron, now, expected):
    assert policy.next_due(schedule(every=None, cron=cron), now, None) == expected


def test_next_due_cron_never_repeats_the_last_run():
    sched = schedule(every=None, cron="0 2 * * *")
    last = datetime(2026, 8, 21, 2, 0)
    assert policy.next_due(sched, datetime(2026, 8, 21, 2, 0), last) == datetime(2026, 8, 22, 2, 0)


def test_next_due_returns_none_without_a_valid_trigger():
    assert policy.next_due(schedule(every=None, cron=None), NOW, None) is None
    assert policy.next_due("not a schedule", NOW, None) is None


# ---- due_schedules ------------------------------------------------------


def test_due_schedules_picks_only_enabled_and_due():
    ecosystem = {
        "schedules": [
            schedule(
                "eco-due", every="30m", last_run_at="2026-08-21T10:00:00", target_repo="C:/eco"
            ),
            schedule(
                "eco-not-yet", every="6h", last_run_at="2026-08-21T11:00:00", target_repo="C:/eco"
            ),
            schedule(
                "eco-disabled", every="30m", enabled=False, last_run_at=None, target_repo="C:/eco"
            ),
        ]
    }
    projects = {
        "C:/proj": {
            "schedules": [
                schedule(
                    "proj-due", every=None, cron="0 2 * * *", last_run_at="2026-08-20T02:00:00"
                ),
                schedule("proj-disabled", every="30m", enabled=False),
            ]
        },
        "C:/other": {"schedules": []},
    }
    due = policy.due_schedules(ecosystem, projects, NOW)
    assert [(s["id"], repo) for s, repo in due] == [
        ("eco-due", "C:/eco"),
        ("proj-due", "C:/proj"),
    ]


def test_due_schedules_accepts_pairs_and_empty_inputs():
    assert policy.due_schedules(None, None, NOW) == []
    pairs = [("C:/p", {"schedules": [schedule("s", every="30m")]})]
    assert [repo for _, repo in policy.due_schedules(None, pairs, NOW)] == ["C:/p"]


# ---- FactoryRunner wiring ----------------------------------------------


def factory_spec():
    return FactorySpec(
        slug="policyfac",
        waves=[
            FactoryWave(
                slug="w1",
                agents=[
                    {"name": "reader", "brief": "look around", "tools": list(READ_TOOLS)},
                    {"name": "scribe", "brief": "change a file", "tools": list(EDIT_TOOLS)},
                ],
                gate={},
            )
        ],
    )


def recording_worker_factory(_run_dir, _specs):
    async def worker(request, api):
        from agentgraph.dispatcher import AgentResponse

        return AgentResponse(output=f"{request.worker} done")

    return worker


def test_factory_runner_prepends_rules_to_every_brief(tmp_path):
    rules = [rule("a", "cite path:line"), rule("w", "run the tests", scope="writers")]
    runner = FactoryRunner(
        factory_spec(),
        tmp_path,
        factory_run_id="fr-1",
        worker_factory=recording_worker_factory,
        rules=rules,
    )
    state = runner.run()
    assert state.status == "completed", state.to_dict()

    run_dir = tmp_path / ".agentgraph" / "runs" / "policyfac-w1"
    manifest = read_mission_manifest(run_dir)
    briefs = {agent["name"]: agent["brief"] for agent in manifest["agents"]}
    assert briefs["reader"].startswith(policy.RULES_MARKER)
    assert "cite path:line" in briefs["reader"]
    assert "run the tests" not in briefs["reader"]
    assert "run the tests" in briefs["scribe"]
    assert briefs["scribe"].endswith("change a file")

    prompts = {
        event.payload["worker"]: event.payload["identity"]["prompt"]
        for event in read_events(run_dir / "run.jsonl")
        if event.type == "agent.requested"
    }
    assert prompts["reader"] == briefs["reader"]
    assert prompts["scribe"] == briefs["scribe"]
    assert "run the tests" not in prompts["reader"]

    rules_findings = [
        event.payload
        for event in read_events(run_dir / "run.jsonl")
        if event.type == "finding.recorded" and event.payload.get("topic") == "rules"
    ]
    assert rules_findings and "cite path:line" in rules_findings[0]["detail"]


def test_factory_runner_without_rules_leaves_briefs_alone(tmp_path):
    runner = FactoryRunner(
        factory_spec(),
        tmp_path,
        factory_run_id="fr-2",
        worker_factory=recording_worker_factory,
    )
    runner.run()
    manifest = read_mission_manifest(tmp_path / ".agentgraph" / "runs" / "policyfac-w1")
    assert [a["brief"] for a in manifest["agents"]] == ["look around", "change a file"]
