"""Test-session setup that must run before any HAL module is imported.

Several HAL packages create their storage directories at IMPORT time, under
paths that only exist on a body (`/root/local/...`). On a development host that
raises `OSError: Read-only file system: '/root'` during pytest COLLECTION,
which aborts the whole run — not just the file that touched it.

Individual test modules used to guard themselves with `os.environ.setdefault`,
but that only works when they happen to be imported first: whichever test pulls
the face package earliest wins, and the directory constant is already frozen by
the time the guard runs. Setting it here fixes the order — conftest is imported
before any test module.

`setdefault`, not assignment: a run that supplies its own location (the sim
server tests do) keeps it.
"""

import os
import tempfile

_TEST_ROOT = os.path.join(tempfile.gettempdir(), "autonomous-hal-test")

for _var, _leaf in (
    ("HAL_USERS_DIR", "users"),
    ("HAL_STRANGERS_DIR", "strangers"),
):
    os.environ.setdefault(_var, os.path.join(_TEST_ROOT, _leaf))
