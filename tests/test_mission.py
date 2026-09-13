"""The mission shim, the narrator, and the transcript sidecars."""

from __future__ import annotations

from pathlib import Path

from activegraph.core.event import Event

from agentgraph import (
    AgentSpec,
    Mission,
    ScriptedWorker,
    TranscriptWriter,
    narrate,
    narrate_path,
)

AGENTS = [
    AgentSpec("alpha", "look at area A", refs=("checklist",)),
    AgentSpec("beta", "look at area B"),
]


def scripted() -> ScriptedWorker:
    def responder(request, api):
        if request.worker == "synthesizer":
            return f"ranked {len(api.host.context.findings())} findings"
        refs = api.host.context.findings(topic="ref/checklist")
        api.emit_finding("area", f"{request.worker} confirmed one thing")
        return f"{request.worker} done, checklist={'yes' if refs else 'no'}"

    return ScriptedWorker(responder, delays={"alpha": 0.03})


def build_mission(**overrides) -> Mission:
    kwargs = dict(
        synthesis="rank the issues.",
        references={"checklist": "Checklist:\n- look for X\n- look for Y"},
        facts=[("inventory", "2 areas to cover", {"areas": ["A", "B"]})],
        max_turns=10,
    )
    kwargs.update(overrides)
    return Mission("shim-test", AGENTS, **kwargs)


def test_mission_runs_end_to_end(tmp_path: Path) -> None:
    """Declare two agents and a synthesis; the shim does the rest.

    The parts that used to be hand-wired per workflow — dispatch, join
    counting, the wave-2 trigger, finding indexing — all assert here through
    their effects: reports exist, synthesis ran after both agents, references
    were retrievable from the board.
    """
    result = build_mission().run(tmp_path / "run.jsonl", worker=scripted())

    assert result.host.agents_dispatched == 3  # two agents + synthesizer
    assert result.host.agents_failed == 0
    assert set(result.outputs) == {"alpha", "beta"}
    # The reference reached the agent through the board, not the prompt.
    assert "checklist=yes" in result.outputs["alpha"]
    # ...and the prompt is correspondingly small: brief only, no checklist.
    requested = [e for e in result.graph.events if e.type == "agent.requested"]
    for e in requested:
        assert "look for X" not in e.payload["identity"]["prompt"]
    # Synthesis fired itself once the second report landed.
    assert result.synthesis == "ranked 4 findings"  # 1 fact + 1 ref + 2 agents


def test_mission_interrupt_resume_and_replay(tmp_path: Path) -> None:
    """The three run modes, one mission object, no bespoke plumbing."""
    mission = build_mission()

    first = mission.run(
        tmp_path / "a.jsonl", worker=scripted(), interrupt_after=1
    )
    assert first.host.stopped_reason == "until"

    resumed = mission.resume(tmp_path / "a.jsonl", tmp_path / "b.jsonl", worker=scripted())
    assert resumed.host.cache_hits >= 1
    assert resumed.host.resumed_live >= 1
    assert set(resumed.outputs) == {"alpha", "beta"}
    assert resumed.synthesis is not None

    replayed = mission.replay(tmp_path / "b.jsonl", tmp_path / "c.jsonl")
    assert (tmp_path / "c.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
    assert replayed.host.cache_hits == 3


def test_narrate_tells_the_story(tmp_path: Path) -> None:
    result = build_mission().run(tmp_path / "run.jsonl", worker=scripted())
    story = narrate_path(str(tmp_path / "run.jsonl"))

    assert "# Run shim-test" in story
    assert "## alpha" in story and "## beta" in story
    assert "alpha confirmed one thing" in story
    assert "ref/checklist" in story  # the board seed section
    assert "ranked 4 findings" in story  # outputs section
    assert result.synthesis in story


def test_narrate_renders_cut_off_agents_with_their_partial_output() -> None:
    """A cut-off agent's surviving text must be in the story, not just the log."""
    events = [
        Event(id="evt_001", type="agent.requested", payload={
            "worker": "doomed",
            "args_hash": "x",
            "identity": {"prompt": "do a thing", "model": "m"},
        }),
        Event(id="evt_002", type="agent.responded", caused_by="evt_001", payload={
            "worker": "doomed",
            "output": None,
            "error": {
                "type": "AgentError",
                "message": "Reached maximum number of turns",
                "partial_output": "I had confirmed the first two defects when",
            },
            "cost_usd": "0.10",
        }),
    ]
    story = narrate(events)
    assert "never returned" not in story
    assert "failed" in story
    assert "What survived the cut-off" in story
    assert "confirmed the first two defects" in story


def test_narrate_marks_interrupted_agents(tmp_path: Path) -> None:
    mission = build_mission()
    mission.run(tmp_path / "a.jsonl", worker=scripted(), interrupt_after=1)
    story = narrate_path(str(tmp_path / "a.jsonl"))
    assert "never returned" in story
    assert "a resume will run this agent" in story


def test_transcript_writer_is_lazy_and_readable(tmp_path: Path) -> None:
    """No file until something happens; markdown once it does."""
    from decimal import Decimal

    from agentgraph.dispatcher import AgentResponse

    writer = TranscriptWriter(tmp_path / "worker.md")
    assert not (tmp_path / "worker.md").exists(), "eager file creation"
    # A cache hit ends with result() on a never-started writer: still no file.
    writer.result(AgentResponse(output="cached"))
    assert not (tmp_path / "worker.md").exists()

    writer = TranscriptWriter(tmp_path / "worker.md")
    writer.begin("worker", "survey the area", "claude-sonnet-4-5-20250929")
    writer.thinking("the area is large;\nstart with the north side")
    writer.tool_use("Read", {"file_path": "north.md"})
    writer.tool_result("north side is clear" * 100)
    writer.text("North is clear.")
    writer.result(
        AgentResponse(output="done", cost_usd=Decimal("0.05"), num_turns=3)
    )

    content = (tmp_path / "worker.md").read_text(encoding="utf-8")
    assert content.startswith("# worker")
    assert "> the area is large;" in content
    assert "**→ Read**" in content
    assert "[+" in content  # long tool result was excerpted
    assert "## Outcome: completed" in content
    assert "$0.05" in content
