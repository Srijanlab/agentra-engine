"""agentra.agents.brain.tools._as_bool / _optional_str: coerce tool-call args from NIM,
which stringifies JSON booleans ('False') and nulls ('None') that Claude sends as real
JSON types."""

from agentra.agents.brain.tools import _as_bool, _optional_str


def test_as_bool_handles_stringified_booleans():
    assert _as_bool("False") is False
    assert _as_bool("true") is True
    assert _as_bool("1") is True
    assert _as_bool("") is False


def test_as_bool_passes_through_real_booleans():
    assert _as_bool(True) is True
    assert _as_bool(False) is False


def test_optional_str_treats_string_sentinels_as_unset():
    assert _optional_str("None") == ""
    assert _optional_str("null") == ""
    assert _optional_str(None) == ""
    assert _optional_str("  ") == ""


def test_optional_str_keeps_real_values():
    assert _optional_str("  dev/foo  ") == "dev/foo"
    assert _optional_str("42") == "42"
