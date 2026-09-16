"""Ghost user dirs (#425).

Per-user loggers (audio history, mood, music suggestions) used to create
`/root/local/users/<slug>/` for any label the agent put in `person` — including
example names copied out of skill prompts. `/face/owners` then listed those
log-only folders as enrolled people. Two guards: `canonicalize_person` maps an
unmatched label to the shared `unknown` bucket instead of a fresh slug, and
`/face/owners` only lists directories with enrollment evidence.
"""

import json

import pytest

from hal.drivers.voice import music_service as ms


@pytest.fixture
def users_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_USERS_DIR", tmp_path)
    return tmp_path


# --- canonicalize_person -----------------------------------------------------


def test_unmatched_label_falls_back_to_unknown(users_dir):
    # "leo" is the literal example from music-suggestion/SKILL.md; nobody is enrolled.
    assert ms.canonicalize_person("leo") == "unknown"


def test_unmatched_label_with_no_users_dir_falls_back_to_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_USERS_DIR", tmp_path / "does-not-exist")
    assert ms.canonicalize_person("leo") == "unknown"


def test_exact_slug_match_on_existing_dir(users_dir):
    (users_dir / "gray").mkdir()
    assert ms.canonicalize_person("Gray") == "gray"


def test_longest_token_match_on_existing_dir(users_dir):
    (users_dir / "gray").mkdir()
    assert ms.canonicalize_person("i am gray") == "gray"


def test_telegram_id_match_via_metadata(users_dir):
    d = users_dir / "gray"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"telegram_id": "123456"}))
    assert ms.canonicalize_person("Some Display Name (123456)") == "gray"


def test_empty_label_is_unknown(users_dir):
    assert ms.canonicalize_person("") == "unknown"


def test_log_play_event_never_creates_ghost_dir(users_dir):
    ms._log_play_event("Weightless Marconi Union", "Weightless", 1.0, 200.0, "end", person="leo")
    assert not (users_dir / "leo").exists()
    files = list((users_dir / "unknown" / "audio_history").glob("*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text().splitlines()[0])
    assert entry["person"] == "unknown"


def test_log_play_event_keeps_enrolled_person(users_dir):
    (users_dir / "gray").mkdir()
    ms._log_play_event("chill acoustic", "Chill", 1.0, 60.0, "user", person="gray")
    assert list((users_dir / "gray" / "audio_history").glob("*.jsonl"))
    assert not (users_dir / "unknown").exists()
