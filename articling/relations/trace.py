"""Opt-in local evidence for enrichment, without recording client credentials.

Each run and request has its own directory, so concurrent calls and repeated
runs cannot overwrite evidence. The request payload retains image data URLs;
image files are also saved for convenient visual inspection.
"""
from __future__ import annotations

import base64
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel, Field

from ..schema import ArticDocument, Edge, Node

logger = logging.getLogger(__name__)


class AnchorContext(BaseModel):
    anchor_id: str
    candidate_ids: list[str]


class CallRecord(BaseModel):
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    request: dict[str, Any]
    anchors: list[AnchorContext] = Field(default_factory=list)
    status: str = "pending"
    parsed: Any = None
    error_type: str | None = None


class HeadingOutcome(BaseModel):
    anchor_id: str
    parent_id: str
    rationale: str
    outcome: str


class ProposalRecord(BaseModel):
    proposals: list[Edge]


class SiblingGroups(BaseModel):
    groups: list[list[str]]


class TraceClient:
    """Only the Responses parse surface used by enrichment is wrapped."""

    def __init__(self, client, directory: Path):
        self._client = client
        self.directory = directory
        self.responses = self

    def save(self, name: str, record: BaseModel) -> None:
        try:
            destination = self.directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            # A diagnostic failure must not discard a successful judgment.
            logger.warning("Could not write enrichment trace %s", name)

    def parse(self, *, _anchors: list[AnchorContext] | None = None, **kwargs):
        call_id = f"calls/{uuid4().hex}"
        request = dict(kwargs)
        request["text_format"] = kwargs["text_format"].model_json_schema()
        record = CallRecord(request=request, anchors=_anchors or [])
        self.save(f"{call_id}/call.json", record)
        try:
            image_index = 0
            for message in kwargs.get("input", []):
                content = message.get("content", [])
                if isinstance(content, str):
                    continue
                for block in content:
                    url = block.get("image_url", "")
                    if block.get("type") == "input_image" and url.startswith("data:image/"):
                        header, encoded = url.split(",", 1)
                        extension = "png" if "image/png" in header else "jpg"
                        (self.directory / call_id / f"image-{image_index}.{extension}").write_bytes(base64.b64decode(encoded))
                        image_index += 1
        except (OSError, ValueError):
            logger.warning("Could not save trace image preview; request retains original image payload")
        try:
            response = self._client.responses.parse(**kwargs)
        except Exception as exc:
            record.status = "error"
            record.finished_at = datetime.now(timezone.utc)
            # Exception messages/HTTP headers may contain secrets. Record
            # only the exception class; never serialize the client itself.
            record.error_type = type(exc).__name__
            self.save(f"{call_id}/call.json", record)
            raise
        record.status = "null" if response.output_parsed is None else "parsed"
        record.finished_at = datetime.now(timezone.utc)
        record.parsed = response.output_parsed
        self.save(f"{call_id}/call.json", record)
        return response


def parse_with_context(client, items: list[tuple[Node, list[Node]]], **kwargs):
    if isinstance(client, TraceClient):
        return client.parse(
            _anchors=[AnchorContext(anchor_id=a.id, candidate_ids=[c.id for c in candidates]) for a, candidates in items],
            **kwargs,
        )
    return client.responses.parse(**kwargs)


@contextmanager
def trace_session(client, directory: str | Path, document: ArticDocument) -> Iterator[TraceClient]:
    run_dir = Path(directory) / f"run-{uuid4().hex}"
    run_dir.mkdir(parents=True)
    traced = TraceClient(client, run_dir)
    traced.save("before.json", document)
    try:
        yield traced
    finally:
        traced.save("after.json", document)
