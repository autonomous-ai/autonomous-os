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
import shutil
import tempfile

_TEST_ROOT = os.path.join(tempfile.gettempdir(), "autonomous-hal-test")

for _var, _leaf in (
    ("HAL_USERS_DIR", "users"),
    ("HAL_STRANGERS_DIR", "strangers"),
    # Boot-scoped switch sidecars (LED / mic / speaker / camera / sleep / scene).
    # They outlive the process on purpose, so on the shared default (/tmp) one
    # run that ended with the body asleep left every LATER run starting asleep —
    # the simulator tests then failed against a body that would not move, and
    # the suite's red count changed depending on what ran before it.
    ("HAL_STATE_DIR", "state"),
):
    os.environ.setdefault(_var, os.path.join(_TEST_ROOT, _leaf))

# Start every session from empty. A fixed path is what makes the run
# reproducible (subprocess bodies inherit it through os.environ.copy), but a
# fixed path also SURVIVES the session — and these sidecars are exactly the
# state that must not: one run that ended with the body asleep made the next
# run boot asleep, and the simulator tests then failed against a body that
# would not move. Only the directory this file owns is removed.
if os.environ["HAL_STATE_DIR"].startswith(_TEST_ROOT):
    shutil.rmtree(os.environ["HAL_STATE_DIR"], ignore_errors=True)
os.makedirs(os.environ["HAL_STATE_DIR"], exist_ok=True)
