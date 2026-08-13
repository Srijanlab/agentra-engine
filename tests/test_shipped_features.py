"""Tests for Memory.shipped_features()/record_shipped()'s run_id
tracking -- added so a shipped feature can be traced back to the exact
run/log/agent-steps that produced it, ahead of eventually deriving
"latest work done" from GitHub Issues/Projects + shipped.json instead of
the (now largely retired) work_updates.json ledger.
"""

from agentra.memory import Memory


def test_record_shipped_stores_run_id(tmp_path):
    mem = Memory(tmp_path)

    mem.record_shipped("Dark mode", commit_sha="abc1234", run_id="run42")

    features = mem.shipped_features()
    assert features == [{"feature": "Dark mode", "commit_sha": "abc1234", "run_id": "run42", "ts": features[0]["ts"]}]


def test_shipped_features_normalizes_old_dict_entries_missing_run_id(tmp_path):
    mem = Memory(tmp_path)
    import json

    mem.shipped_path.write_text(json.dumps([{"feature": "Old feature", "commit_sha": "def5678", "ts": "2026-01-01T00:00:00+00:00"}]))

    features = mem.shipped_features()

    assert features == [{"feature": "Old feature", "commit_sha": "def5678", "run_id": None, "ts": "2026-01-01T00:00:00+00:00"}]


def test_shipped_features_normalizes_ancient_plain_string_entries(tmp_path):
    mem = Memory(tmp_path)
    import json

    mem.shipped_path.write_text(json.dumps(["Really old feature"]))

    features = mem.shipped_features()

    assert features == [{"feature": "Really old feature", "commit_sha": None, "run_id": None, "ts": None}]
