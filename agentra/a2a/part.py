"""A2A message/artifact Part models: text, file, and structured-data parts."""

from __future__ import annotations

from typing import Literal, Optional, Union

from agentra.a2a._base import A2ABaseModel


class TextPart(A2ABaseModel):
    """A plain-text segment of a Message or Artifact."""

    kind: Literal["text"] = "text"
    text: str
    metadata: Optional[dict] = None


class FileWithBytes(A2ABaseModel):
    """Inline file content, base64-encoded."""

    name: Optional[str] = None
    mime_type: Optional[str] = None
    bytes: str


class FileWithUri(A2ABaseModel):
    """File content referenced by URI."""

    name: Optional[str] = None
    mime_type: Optional[str] = None
    uri: str


class FilePart(A2ABaseModel):
    """A file segment of a Message or Artifact."""

    kind: Literal["file"] = "file"
    file: Union[FileWithBytes, FileWithUri]
    metadata: Optional[dict] = None


class DataPart(A2ABaseModel):
    """A structured-data (JSON) segment of a Message or Artifact."""

    kind: Literal["data"] = "data"
    data: dict
    metadata: Optional[dict] = None


Part = Union[TextPart, FilePart, DataPart]
