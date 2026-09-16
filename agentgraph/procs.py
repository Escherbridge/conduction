"""Child processes a mission spawns, and how to actually end them.

A mission spawns subprocesses in three places: gate commands, the probe gate's
server, and CLI agent workers. Each of those reaps its own child on the happy
path, and none of them survived contact with an interrupt or a killed server --
measured, not assumed:

  * `/interrupt` returned 202 and the gate subprocess was still running 20 s
    later. Stopping is cooperative (`host.run` checks the stop condition between
    quanta), so a thread blocked in `subprocess.run(timeout=600)` cannot observe
    it at all.
  * Killing the server ended it in 0.0 s on Windows -- `TerminateProcess` runs
    no Python -- and its gate child outlived it.

So this module keeps a registry of live children, attributed to the run that
spawned them, and ends them as a TREE. `terminate()` reaches the direct child
only; a probe server that forks workers would leak the grandchildren.

Attribution works because every mission runs in its own thread and calls
`asyncio.run()` synchronously inside it, so a `threading.local` set at the top
of the mission thread is visible to gates and to CLI workers alike.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading

WINDOWS = sys.platform == "win32"
TREE_KILL_TIMEOUT_SECONDS = 10

_current = threading.local()
_registry: dict[str, set[int]] = {}
_lock = threading.Lock()


def spawn_kwargs() -> dict:
    """Popen kwargs that make a child killable as a group.

    POSIX gets its own session so `killpg` reaches grandchildren; Windows gets a
    new process group, and `taskkill /T` walks the tree from the pid.
    """
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True, "preexec_fn": _die_with_parent}


# --- the OS-level backstop -------------------------------------------------
#
# Everything above depends on this process getting a chance to run cleanup
# code. It often does not: killing the server on Windows is TerminateProcess,
# which runs no Python at all -- measured, the server exited with
# 0xC000013A and its gate child outlived it.
#
# A Job Object with KILL_ON_JOB_CLOSE fixes that at the kernel level. Every
# child is assigned to the job; when this process dies for ANY reason the last
# handle closes and the OS terminates the whole job. No hook required.
#
# POSIX gets the closest equivalent per child: PR_SET_PDEATHSIG, so the kernel
# signals the child when its parent goes away.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PR_SET_PDEATHSIG = 1

_job_handle = None
_job_lock = threading.Lock()


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kill_on_close_job():
    """The process-wide job handle, created once. None if unavailable."""
    global _job_handle
    if not WINDOWS:
        return None
    with _job_lock:
        if _job_handle is not None:
            return _job_handle
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = _JobObjectExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(handle)
                return None
            _job_handle = handle
        except Exception:  # pragma: no cover - no kernel32, or policy forbids jobs
            _job_handle = None
        return _job_handle


def adopt(pid: int) -> bool:
    """Put `pid` under the kill-on-close job so it cannot outlive this process."""
    if not WINDOWS:
        return False
    job = _kill_on_close_job()
    if not job:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        return False


def _die_with_parent() -> None:  # pragma: no cover - POSIX child hook
    """PR_SET_PDEATHSIG: the kernel kills this child when its parent exits."""
    try:
        ctypes.CDLL("libc.so.6").prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:
        pass


def bind_run(run_id: str | None) -> None:
    """Attribute every child spawned by THIS thread to `run_id`."""
    _current.run_id = run_id


def current_run() -> str | None:
    return getattr(_current, "run_id", None)


def register(pid: int, run_id: str | None = None) -> None:
    """Record a live child so interrupt and shutdown can find it."""
    key = run_id or current_run() or "-unattributed-"
    # Adopt first: if this process is killed a microsecond later, the job is
    # what stops the child, not any code in here.
    adopt(pid)
    with _lock:
        _registry.setdefault(key, set()).add(pid)


def unregister(pid: int, run_id: str | None = None) -> None:
    """Forget a child that has already exited."""
    key = run_id or current_run() or "-unattributed-"
    with _lock:
        pids = _registry.get(key)
        if pids:
            pids.discard(pid)
            if not pids:
                _registry.pop(key, None)


def tracked(run_id: str | None = None) -> set[int]:
    with _lock:
        if run_id is None:
            return {pid for pids in _registry.values() for pid in pids}
        return set(_registry.get(run_id, ()))


def is_alive(pid: int) -> bool:
    if WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_tree(pid: int, timeout: float = TREE_KILL_TIMEOUT_SECONDS) -> bool:
    """End `pid` and everything it spawned. True if it is gone afterwards.

    Never raises: cleanup runs in `finally` blocks and during shutdown, where an
    exception would be worse than a surviving process.
    """
    try:
        if WINDOWS:
            # /T walks the tree, /F skips the polite request. There is no
            # portable POSIX-style group signal on Windows.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=timeout,
            )
        else:
            try:
                group = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                group = None
            if group is not None:
                try:
                    os.killpg(group, signal.SIGTERM)
                except OSError:
                    pass
                deadline = timeout
                while deadline > 0 and is_alive(pid):
                    threading.Event().wait(0.2)
                    deadline -= 0.2
                if is_alive(pid):
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except OSError:
                        pass
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    except Exception:  # pragma: no cover - cleanup must not raise
        pass
    return not is_alive(pid)


def terminate_run(run_id: str) -> int:
    """Kill every child still attributed to `run_id`. Returns how many."""
    killed = 0
    for pid in tracked(run_id):
        if terminate_tree(pid):
            killed += 1
        unregister(pid, run_id)
    return killed


def sweep_all() -> int:
    """Kill every tracked child, whatever run it belongs to."""
    killed = 0
    with _lock:
        snapshot = {key: set(pids) for key, pids in _registry.items()}
    for run_id, pids in snapshot.items():
        for pid in pids:
            if terminate_tree(pid):
                killed += 1
            unregister(pid, run_id)
    return killed


class tracked_process:
    """Context manager registering a Popen for the duration of its life.

    Guarantees the child is dead on exit -- including when the block is left by
    `CancelledError`, which is precisely the path that used to orphan it.
    """

    def __init__(self, process, run_id: str | None = None):
        self.process = process
        self.run_id = run_id or current_run()
        register(process.pid, self.run_id)

    def __enter__(self):
        return self.process

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.process.returncode is None:
                terminate_tree(self.process.pid)
        finally:
            unregister(self.process.pid, self.run_id)
        return False
