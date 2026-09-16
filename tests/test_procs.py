"""Tests for agentgraph/procs.py -- child-process cleanup.

Every assertion here is about a real OS process, not a mock. The bug being
fixed was that code which *looked* like it cleaned up did not: `terminate()`
reaches the direct child only, and a cooperative stop flag is invisible to a
thread blocked in `subprocess.run`. Only a real process tree can show that.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

import pytest

from agentgraph import procs


def spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    """A child that will outlive the test unless something kills it."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **procs.spawn_kwargs(),
    )


def spawn_parent_with_child(seconds: int = 60) -> subprocess.Popen:
    """A child that spawns a GRANDCHILD and waits on it.

    The grandchild is the point: `Popen.terminate()` would leave it running.
    """
    code = (
        "import subprocess, sys, time\n"
        f"kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})'])\n"
        "print(kid.pid, flush=True)\n"
        "kid.wait()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **procs.spawn_kwargs(),
    )


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    procs.sweep_all()
    procs.bind_run(None)


def wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not procs.is_alive(pid):
            return True
        time.sleep(0.2)
    return not procs.is_alive(pid)


def test_terminate_tree_kills_a_plain_child():
    process = spawn_sleeper()
    assert procs.is_alive(process.pid)
    assert procs.terminate_tree(process.pid)
    assert wait_gone(process.pid)


def test_terminate_tree_kills_grandchildren():
    """The whole reason this is a tree kill. `terminate()` signals the direct
    child; a probe gate's dev server that forks workers would leak every one."""
    parent = spawn_parent_with_child()
    grandchild_pid = int(parent.stdout.readline().strip())
    assert procs.is_alive(grandchild_pid)

    procs.terminate_tree(parent.pid)

    assert wait_gone(parent.pid), "parent survived"
    assert wait_gone(grandchild_pid), "grandchild survived the tree kill"


def test_terminate_tree_is_safe_on_a_dead_pid():
    """Cleanup runs in `finally` blocks and during shutdown; raising there would
    be worse than a surviving process."""
    process = spawn_sleeper(1)
    process.wait()
    assert procs.terminate_tree(process.pid) is True
    assert procs.terminate_tree(999_999_999) in (True, False)  # must not raise


def test_children_are_attributed_to_the_bound_run():
    procs.bind_run("MISSION-a@1111")
    first = spawn_sleeper()
    procs.register(first.pid)

    procs.bind_run("MISSION-b@2222")
    second = spawn_sleeper()
    procs.register(second.pid)

    assert procs.tracked("MISSION-a@1111") == {first.pid}
    assert procs.tracked("MISSION-b@2222") == {second.pid}


def test_terminate_run_kills_only_that_runs_children():
    """Interrupting one mission must not kill another's gate."""
    procs.bind_run("MISSION-doomed@1111")
    doomed = spawn_sleeper()
    procs.register(doomed.pid)

    procs.bind_run("MISSION-spared@2222")
    spared = spawn_sleeper()
    procs.register(spared.pid)

    assert procs.terminate_run("MISSION-doomed@1111") == 1
    assert wait_gone(doomed.pid)
    assert procs.is_alive(spared.pid), "an unrelated run's child was killed"
    assert procs.tracked("MISSION-doomed@1111") == set()


def test_sweep_all_clears_every_run():
    procs.bind_run("MISSION-x@1111")
    one = spawn_sleeper()
    procs.register(one.pid)
    procs.bind_run("MISSION-y@2222")
    two = spawn_sleeper()
    procs.register(two.pid)

    assert procs.sweep_all() == 2
    assert wait_gone(one.pid) and wait_gone(two.pid)
    assert procs.tracked() == set()


def test_tracked_process_kills_on_exception_including_cancellation():
    """The path that used to orphan children: host.run cancels in-flight tasks
    when a mission stops, and CancelledError is a BaseException that sailed past
    `except (OSError, ValueError)`."""
    import asyncio

    process = spawn_sleeper()
    with pytest.raises(asyncio.CancelledError):
        with procs.tracked_process(process):
            raise asyncio.CancelledError()

    assert wait_gone(process.pid), "child survived a cancelled block"
    assert process.pid not in procs.tracked()


def test_tracked_process_deregisters_a_child_that_exited_normally():
    process = spawn_sleeper(1)
    with procs.tracked_process(process):
        process.wait()
    assert procs.tracked() == set()


# --- the OS-level backstop -------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_children_die_with_a_hard_killed_parent():
    """The case no shutdown hook can cover.

    Killing the server on Windows is TerminateProcess: it runs no Python, so
    `before_server_stop` never fires and every in-flight child is orphaned --
    measured, before this fix, as a gate subprocess outliving its server by
    minutes. A Job Object with KILL_ON_JOB_CLOSE moves the guarantee into the
    kernel, where a hard kill cannot skip it.
    """
    helper = pathlib.Path(__file__).resolve().parent / "jobtest_parent.py"
    parent = subprocess.Popen([sys.executable, str(helper)], stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline().strip())
        assert procs.is_alive(child_pid)

        # TerminateProcess: the parent gets no chance to clean up after itself.
        subprocess.run(["taskkill", "/F", "/PID", str(parent.pid)], capture_output=True)
        parent.wait(timeout=30)

        assert wait_gone(child_pid), (
            "child outlived a hard-killed parent; the job object is not holding it"
        )
    finally:
        if parent.poll() is None:
            procs.terminate_tree(parent.pid)
