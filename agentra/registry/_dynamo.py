"""registry/_dynamo.py — a small, dependency-free DynamoDB helper: table access,
float<->Decimal conversion (boto3's DynamoDB resource has no native float
support), Firestore's merge=True analog, and the compare-and-swap primitive
inbox.py's claim logic needs. Parallel in role to _cache.py -- not a wrapper
around every operation, just the patterns duplicated across runs.py/loops.py/
core.py/inbox.py."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

_table_cache: dict[str, Any] = {}


def table(name: str) -> Any:
    """Memoized boto3 Table resource for `<AGENTRA_DYNAMODB_TABLE_PREFIX>{name}`."""
    prefix = os.environ.get("AGENTRA_DYNAMODB_TABLE_PREFIX") or ""
    full_name = f"{prefix}{name}"
    cached = _table_cache.get(full_name)
    if cached is not None:
        return cached
    from agentra.registry import core

    tbl = core._ddb.Table(full_name)
    _table_cache[full_name] = tbl
    return tbl


def to_item(value: Any) -> Any:
    """Recursively converts Python floats to Decimal -- boto3's DynamoDB
    resource raises TypeError on a bare float in any put_item/update_item
    value, nested or not (timestamps and costs are floats everywhere in this
    codebase, so this isn't an edge case, it's every write)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_item(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_item(v) for v in value]
    return value


def from_item(value: Any) -> Any:
    """The inverse of to_item -- Decimal back to float (or int, if it's a
    whole number) so callers get plain JSON-serializable Python values back,
    matching what Firestore's client already handed back natively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_item(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_item(v) for v in value]
    return value


def put_item(tbl: Any, item: dict) -> None:
    tbl.put_item(Item=to_item(item))


def get_item(tbl: Any, key: dict) -> dict | None:
    item = tbl.get_item(Key=key).get("Item")
    return from_item(item) if item is not None else None


def _update_expression(prefix: str, fields: dict) -> tuple[str, dict, dict]:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    parts: list[str] = []
    for i, (k, v) in enumerate(fields.items()):
        name_ph, value_ph = f"#{prefix}{i}", f":{prefix}{i}"
        names[name_ph] = k
        values[value_ph] = to_item(v)
        parts.append(f"{name_ph} = {value_ph}")
    return "SET " + ", ".join(parts), names, values


def merge_update(tbl: Any, key: dict, fields: dict) -> None:
    """Firestore's `.set(fields, merge=True)` analog: partially updates only the
    given fields on the item at `key` (creating it if absent). Every attribute
    name is aliased unconditionally -- DynamoDB reserves many common words
    (`status` among them), so this isn't optional for arbitrary field dicts."""
    if not fields:
        return
    expr, names, values = _update_expression("f", fields)
    tbl.update_item(Key=key, UpdateExpression=expr, ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def try_conditional_update(
    tbl: Any, key: dict, updates: dict, *, condition_attr: str, condition_value: Any
) -> bool:
    """Compare-and-swap: applies `updates` only if `condition_attr` on the
    existing item currently equals `condition_value`. Returns False (not an
    error) on a failed condition -- e.g. two dispatchers racing to claim the
    same inbox request -- and re-raises any other DynamoDB error."""
    from botocore.exceptions import ClientError

    expr, names, values = _update_expression("u", updates)
    names["#cond"] = condition_attr
    values[":cond_expected"] = to_item(condition_value)
    try:
        tbl.update_item(
            Key=key,
            UpdateExpression=expr,
            ConditionExpression="#cond = :cond_expected",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
