"""Shared Pydantic base giving every A2A model A2A-spec camelCase JSON field names."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class A2ABaseModel(BaseModel):
    """Base model whose serialized form mirrors the A2A specification (v0.2.x) wire shape."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        use_enum_values=True,
    )
