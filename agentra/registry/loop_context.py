"""registry/loop_context.py — the durable structured per-loop context object."""

from __future__ import annotations

import time
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from agentra.registry import loops

MAX_STRING_CHARS = 4000
MAX_LIST_ENTRIES = 100

_Text = Annotated[str, StringConstraints(max_length=MAX_STRING_CHARS)]
_TextList = Annotated[list[_Text], Field(max_length=MAX_LIST_ENTRIES)]
_Ref = Annotated[str, StringConstraints(max_length=MAX_STRING_CHARS)] | int | None


class InvalidLoopContext(ValueError):
    """A context update failed validation."""


class LoopRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: _Ref = None
    pr: _Ref = None
    branch: _Text | None = None


class LoopContextUpdate(BaseModel):
    """The client-writable fields of a loop's context; unset fields are left untouched."""

    model_config = ConfigDict(extra="forbid")

    objective: _Text | None = None
    current_step: _Text | None = None
    state: _Text | None = None
    decisions: _TextList = []
    findings: _TextList = []
    refs: LoopRefs = LoopRefs()
    last_outcome: _Text | None = None


def default_context() -> dict[str, Any]:
    return {
        "objective": None,
        "current_step": None,
        "state": None,
        "decisions": [],
        "findings": [],
        "refs": LoopRefs().model_dump(),
        "last_outcome": None,
        "updated_at": None,
    }


def get_loop_context(loop_id: str) -> dict | None:
    """The loop's context (defaults when never written), or None if the loop doesn't exist."""
    doc = loops._get_loop_doc(loop_id)
    if doc is None:
        return None
    stored = doc.get("context") or {}
    context = default_context()
    context.update({k: stored[k] for k in context if k in stored})
    return _with_full_refs(context)


def _with_full_refs(context: dict) -> dict:
    context["refs"] = {**LoopRefs().model_dump(), **(context["refs"] or {})}
    return context


def set_loop_context(loop_id: str, **fields: Any) -> dict | None:
    """Merge the supplied fields into the loop's context and return it, or None if the loop doesn't exist."""
    try:
        update = LoopContextUpdate(**fields).model_dump(exclude_unset=True)
    except ValidationError as exc:
        raise InvalidLoopContext(str(exc)) from exc
    context = get_loop_context(loop_id)
    if context is None:
        return None
    context.update(update)
    context["updated_at"] = time.time()
    _with_full_refs(context)
    loops._write_loop(loop_id, {"context": context})
    return context
