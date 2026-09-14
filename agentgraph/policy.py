"""Policy data: rules, goals, and schedules, versioned as JSON in the repos.

Two documents, same schema family:

  * **Ecosystem** -- ``<conduction>/.agentgraph/ecosystem.json``, the fleet-wide
    policy that applies to every target repo.
  * **Project** -- ``<target_repo>/.agentgraph/project.json``, the policy that
    applies to that repo only.

Rules are not prompt garnish: `apply_rules` prepends a single "RULES (binding)"
section to every agent brief (idempotently -- the marker line is the check) and
`rules_fact` puts the same text on the board as a seeded fact. Schedules are
evaluated with a dependency-free `every`/cron matcher.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from agentgraph.mission import AgentSpec

SCHEMA = 1
AGENTGRAPH_DIR = ".agentgraph"
ECOSYSTEM_FILENAME = "ecosystem.json"
PROJECT_FILENAME = "project.json"

#: The first line of the block `apply_rules` prepends; also its idempotency key.
RULES_MARKER = "RULES (binding)"

RULE_SCOPES = ("all", "writers", "readers")
GOAL_STATUSES = ("open", "done", "blocked")
SCHEDULE_KINDS = ("factory", "mission")
EVERY_SUFFIXES = {"m": 60, "h": 3600, "d": 86400}
WRITER_TOOLS = ("Edit", "Write")


# ---- documents ----------------------------------------------------------


def empty_ecosystem() -> dict[str, Any]:
    """A valid, empty ecosystem doc."""
    return {"schema": SCHEMA, "rules": [], "goals": [], "schedules": []}


def empty_project(name: str = "") -> dict[str, Any]:
    """A valid, empty project doc."""
    return {"schema": SCHEMA, "name": name, "rules": [], "goals": [], "schedules": []}


def ecosystem_path(app_root) -> Path:
    """Path of the ecosystem doc under `app_root`."""
    return Path(app_root) / AGENTGRAPH_DIR / ECOSYSTEM_FILENAME


def project_path(repo) -> Path:
    """Path of the project doc inside a target repo."""
    return Path(repo) / AGENTGRAPH_DIR / PROJECT_FILENAME


def _read_doc(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    for key in ("rules", "goals", "schedules"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data.setdefault("schema", SCHEMA)
    return data


def _write_atomic(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def load_ecosystem(app_root) -> dict[str, Any]:
    """Read the ecosystem doc; a missing file yields an empty one."""
    return _read_doc(ecosystem_path(app_root), empty_ecosystem())


def save_ecosystem(app_root, data: dict[str, Any]) -> Path:
    """Write the ecosystem doc atomically (temp file + os.replace)."""
    return _write_atomic(ecosystem_path(app_root), data)


def load_project(repo) -> dict[str, Any]:
    """Read a project doc; a missing file yields an empty one named for the repo."""
    return _read_doc(project_path(repo), empty_project(Path(repo).name))


def save_project(repo, data: dict[str, Any]) -> Path:
    """Write a project doc atomically (temp file + os.replace)."""
    return _write_atomic(project_path(repo), data)


# ---- validation ---------------------------------------------------------


def _validate_rules(rules: Any, errors: list[str]) -> None:
    if not isinstance(rules, list):
        errors.append("rules must be a list")
        return
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{where} must be a dict")
            continue
        if not isinstance(rule.get("id"), str) or not rule.get("id"):
            errors.append(f"{where}.id is required")
        if not isinstance(rule.get("text"), str) or not rule.get("text"):
            errors.append(f"{where}.text is required")
        if rule.get("scope") not in RULE_SCOPES:
            errors.append(f"{where}.scope must be one of {list(RULE_SCOPES)}")
        if not isinstance(rule.get("enabled"), bool):
            errors.append(f"{where}.enabled must be a boolean")


def _validate_goals(goals: Any, errors: list[str]) -> None:
    if not isinstance(goals, list):
        errors.append("goals must be a list")
        return
    for index, goal in enumerate(goals):
        where = f"goals[{index}]"
        if not isinstance(goal, dict):
            errors.append(f"{where} must be a dict")
            continue
        if not isinstance(goal.get("id"), str) or not goal.get("id"):
            errors.append(f"{where}.id is required")
        if not isinstance(goal.get("title"), str) or not goal.get("title"):
            errors.append(f"{where}.title is required")
        if goal.get("status") not in GOAL_STATUSES:
            errors.append(f"{where}.status must be one of {list(GOAL_STATUSES)}")
        if "linked_runs" in goal and not isinstance(goal.get("linked_runs"), list):
            errors.append(f"{where}.linked_runs must be a list")


def _validate_schedules(schedules: Any, errors: list[str], *, ecosystem: bool) -> None:
    if not isinstance(schedules, list):
        errors.append("schedules must be a list")
        return
    for index, schedule in enumerate(schedules):
        where = f"schedules[{index}]"
        if not isinstance(schedule, dict):
            errors.append(f"{where} must be a dict")
            continue
        if not isinstance(schedule.get("id"), str) or not schedule.get("id"):
            errors.append(f"{where}.id is required")
        if schedule.get("kind") not in SCHEDULE_KINDS:
            errors.append(f"{where}.kind must be one of {list(SCHEDULE_KINDS)}")
        if not isinstance(schedule.get("enabled"), bool):
            errors.append(f"{where}.enabled must be a boolean")
        every, cron = schedule.get("every"), schedule.get("cron")
        if every is None and cron is None:
            errors.append(f"{where}: one of every or cron is required")
        if every is not None:
            if not isinstance(every, str) or _parse_every(every) is None:
                errors.append(f"{where}.every must look like 30m, 6h or 1d")
        if cron is not None:
            if not isinstance(cron, str) or not _parse_cron(cron):
                errors.append(f"{where}.cron must be 5 fields of * / */N / A,B / A-B")
        if ecosystem and not isinstance(schedule.get("target_repo"), str):
            errors.append(f"{where}.target_repo is required on ecosystem schedules")


def validate_ecosystem(data: Any) -> list[str]:
    """Every problem with an ecosystem doc, as a list of messages."""
    if not isinstance(data, dict):
        return ["ecosystem must be a dict"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    _validate_rules(data.get("rules", []), errors)
    _validate_goals(data.get("goals", []), errors)
    _validate_schedules(data.get("schedules", []), errors, ecosystem=True)
    return errors


def validate_project(data: Any) -> list[str]:
    """Every problem with a project doc, as a list of messages."""
    if not isinstance(data, dict):
        return ["project must be a dict"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if not isinstance(data.get("name"), str):
        errors.append("name must be a string")
    _validate_rules(data.get("rules", []), errors)
    _validate_goals(data.get("goals", []), errors)
    _validate_schedules(data.get("schedules", []), errors, ecosystem=False)
    return errors


# ---- rules --------------------------------------------------------------


def effective_rules(
    ecosystem: Optional[dict], project: Optional[dict]
) -> list[dict[str, Any]]:
    """Enabled rules only, ecosystem first, then the project's."""
    merged: list[dict[str, Any]] = []
    for doc in (ecosystem or {}, project or {}):
        for rule in doc.get("rules", []) or []:
            if isinstance(rule, dict) and rule.get("enabled"):
                merged.append(rule)
    return merged


def _applies(rule: dict, *, writer: bool) -> bool:
    scope = rule.get("scope", "all")
    if scope == "all":
        return True
    return scope == "writers" if writer else scope == "readers"


def rules_block(rules: Sequence[dict], *, writer: bool) -> str:
    """The "RULES (binding)" section for one agent, or "" when none apply."""
    applicable = [r for r in rules or () if _applies(r, writer=writer)]
    if not applicable:
        return ""
    lines = [RULES_MARKER, ""]
    lines += [f"- {rule.get('text', '')}" for rule in applicable]
    return "\n".join(lines)


def is_writer(spec: AgentSpec) -> bool:
    """A writer is an agent whose tools include Edit or Write."""
    return any(tool in WRITER_TOOLS for tool in (spec.tools or ()))


def apply_rules(specs: Iterable[AgentSpec], rules: Sequence[dict]) -> list[AgentSpec]:
    """Prepend each agent's rules block to its brief -- once (marker-guarded)."""
    out: list[AgentSpec] = []
    for spec in specs:
        block = rules_block(rules, writer=is_writer(spec))
        if not block or RULES_MARKER in spec.brief:
            out.append(spec)
            continue
        out.append(
            AgentSpec(
                name=spec.name,
                brief=f"{block}\n\n{spec.brief}",
                tools=spec.tools,
                refs=spec.refs,
                skills=spec.skills,
                setting_sources=spec.setting_sources,
                model=spec.model,
                max_turns=spec.max_turns,
                meta=dict(spec.meta),
                owns=spec.owns,
            )
        )
    return out


def rules_fact(rules: Sequence[dict]) -> tuple[str, str, str]:
    """The `Mission(facts=...)` entry carrying every rule, scope included."""
    detail = "\n".join(
        f"- [{rule.get('scope', 'all')}] {rule.get('text', '')}" for rule in rules or ()
    )
    return ("rules", "Binding rules for this run", detail)


# ---- schedules ----------------------------------------------------------


def _parse_every(every: str) -> Optional[timedelta]:
    text = (every or "").strip().lower()
    if len(text) < 2 or text[-1] not in EVERY_SUFFIXES:
        return None
    try:
        amount = int(text[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    return timedelta(seconds=amount * EVERY_SUFFIXES[text[-1]])


_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _parse_cron_field(field: str, low: int, high: int) -> Optional[set[int]]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                return None
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            if not (start_text.isdigit() and end_text.isdigit()):
                return None
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None
        if start < low or end > high or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values or None


def _parse_cron(cron: str) -> Optional[list[set[int]]]:
    fields = (cron or "").split()
    if len(fields) != 5:
        return None
    parsed = []
    for field, (low, high) in zip(fields, _CRON_RANGES):
        values = _parse_cron_field(field, low, high)
        if values is None:
            return None
        parsed.append(values)
    return parsed


def _cron_matches(parsed: list[set[int]], moment: datetime) -> bool:
    minute, hour, dom, mon, dow = parsed
    if moment.minute not in minute or moment.hour not in hour:
        return False
    if moment.month not in mon:
        return False
    # Vixie cron: when both DOM and DOW are restricted, either may match.
    dom_restricted = len(dom) != 31
    dow_restricted = len(dow) != 7
    weekday = (moment.weekday() + 1) % 7  # Monday=0 -> cron Sunday=0
    dom_ok, dow_ok = moment.day in dom, weekday in dow
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _as_datetime(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def next_due(
    schedule: dict, now: datetime, last_run_at: Optional[Any] = None
) -> Optional[datetime]:
    """When this schedule should next fire, or None if it never will."""
    if not isinstance(schedule, dict):
        return None
    raw_last = last_run_at if last_run_at is not None else schedule.get("last_run_at")
    last = _as_datetime(raw_last)

    every = _parse_every(schedule.get("every") or "")
    if every is not None:
        return (last + every) if last is not None else now

    parsed = _parse_cron(schedule.get("cron") or "")
    if parsed is None:
        return None
    # Anchor on the last run when there is one, so a missed firing still fires
    # (catch-up) instead of being silently skipped forward to the next one.
    start = last if last is not None else now - timedelta(minutes=1)
    candidate = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # One year of minutes is the ceiling: no valid 5-field cron skips a whole year.
    for _ in range(366 * 24 * 60):
        if _cron_matches(parsed, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def is_due(schedule: dict, now: datetime) -> bool:
    """True when an enabled schedule's next firing is at or before `now`."""
    if not schedule.get("enabled"):
        return False
    due = next_due(schedule, now, schedule.get("last_run_at"))
    return due is not None and due <= now


def due_schedules(
    ecosystem: Optional[dict], projects: Any, now: datetime
) -> list[tuple[dict, Optional[str]]]:
    """Every enabled schedule due at `now`, paired with its target repo.

    `projects` maps target_repo -> project doc (or is an iterable of
    (target_repo, doc) pairs); ecosystem schedules carry their own target_repo.
    """
    out: list[tuple[dict, Optional[str]]] = []
    for schedule in (ecosystem or {}).get("schedules", []) or []:
        if isinstance(schedule, dict) and is_due(schedule, now):
            out.append((schedule, schedule.get("target_repo")))

    pairs = projects.items() if isinstance(projects, dict) else list(projects or ())
    for target_repo, doc in pairs:
        for schedule in (doc or {}).get("schedules", []) or []:
            if isinstance(schedule, dict) and is_due(schedule, now):
                out.append((schedule, target_repo))
    return out


__all__ = [
    "RULES_MARKER",
    "SCHEMA",
    "apply_rules",
    "due_schedules",
    "ecosystem_path",
    "effective_rules",
    "empty_ecosystem",
    "empty_project",
    "is_due",
    "is_writer",
    "load_ecosystem",
    "load_project",
    "next_due",
    "project_path",
    "rules_block",
    "rules_fact",
    "save_ecosystem",
    "save_project",
    "validate_ecosystem",
    "validate_project",
]
