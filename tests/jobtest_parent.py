"""Helper for test_children_die_with_a_hard_killed_parent.

A standalone script rather than an inline source string: this gets hard-killed
mid-flight, and a real file is far easier to read than an escaped heredoc.

Spawns one tracked child, prints its pid, then blocks. The test kills THIS
process with TerminateProcess and asserts the child died with it.
"""

import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agentgraph import procs  # noqa: E402

MARKER = "PROCS_JOBTEST_MARKER"

child = subprocess.Popen(
    [sys.executable, "-c", f"# {MARKER}\nimport time; time.sleep(120)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    **procs.spawn_kwargs(),
)
procs.register(child.pid)
print(child.pid, flush=True)
time.sleep(120)
