"""memory/specs.py — per-repo `.agentra/` spec files + the state.json ledger."""

import json

from agentra.memory import Memory
from agentra.memory.core import spec_header, strip_spec_header


def test_read_write_spec_round_trip(tmp_path):
    mem = Memory(tmp_path / "repo")
    assert mem.read_spec("architecture") is None
    mem.write_spec("architecture", "# r\n## Purpose\nx")
    assert mem.read_spec("architecture") == "# r\n## Purpose\nx\n"
    assert mem.spec_path("architecture") == tmp_path / "repo" / ".agentra" / "architecture.md"


def test_write_spec_rejects_unknown_name(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        Memory(tmp_path / "repo").write_spec("nonsense", "x")


def test_state_shallow_merge(tmp_path):
    mem = Memory(tmp_path / "repo")
    assert mem.read_state() == {}
    mem.write_state({"indexed_sha": "abc"})
    mem.write_state({"last_local_test": {"status": "pass"}})
    state = mem.read_state()
    assert state["indexed_sha"] == "abc"
    assert state["last_local_test"] == {"status": "pass"}
    assert mem.indexed_sha() == "abc"
    # valid JSON on disk
    assert json.loads((tmp_path / "repo" / ".agentra" / "state.json").read_text())["indexed_sha"] == "abc"


def test_spec_header_round_trip():
    h = spec_header("agent:codebase", "abc1234")
    body = "# repo — Architecture\n## Purpose\nx"
    assert strip_spec_header(h + body) == body
    assert strip_spec_header(body) == body  # no header -> unchanged
    assert strip_spec_header(spec_header("human", None) + body) == body
