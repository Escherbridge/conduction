"""Make `import agentgraph` resolve to *this* repo's vendored copy.

Without this, pytest's default rootdir insertion puts `tests/` itself on
`sys.path` (since it has no `__init__.py`), which does not help — and if
anything on `sys.path` already points at the original
`Projects/agentgraph` checkout (e.g. an editor-injected path, or running
the wrong interpreter), imports would silently resolve there instead of
the copy under test here. Inserting the conduction repo root at position 0
makes the vendored `agentgraph/` package under `conduction/` win
unconditionally.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
