"""Model-facing instructions and structured-output descriptions stay English.

Document text itself may be in any language; this test only covers Articling's
own instructions and schema descriptions.
"""
from __future__ import annotations

import json
import re

from articling.relations.caption_images import ImageDescription, _INSTRUCTIONS as CAPTION_INSTRUCTIONS
from articling.relations.propose import (
    _CaptionDisambiguation,
    _DISAMBIGUATION_INSTRUCTIONS,
    _EdgeProposal,
    _FragmentMergeDecision,
    _HEADING_INSTRUCTIONS,
    _HeadingParentChoice,
    _INSTRUCTIONS,
    _MERGE_INSTRUCTIONS,
    _SEMANTIC_MERGE_INSTRUCTIONS,
    _SemanticRegionDecision,
    _SemanticTextGroup,
)
from articling.relations.table_structure import _INSTRUCTIONS_OPENAI, _TableReadResult


_HANGUL = re.compile(r"[가-힣]")


def test_model_instruction_constants_are_english() -> None:
    instructions = (
        _INSTRUCTIONS,
        _HEADING_INSTRUCTIONS,
        _MERGE_INSTRUCTIONS,
        _SEMANTIC_MERGE_INSTRUCTIONS,
        _DISAMBIGUATION_INSTRUCTIONS,
        CAPTION_INSTRUCTIONS,
        _INSTRUCTIONS_OPENAI,
    )

    assert all(_HANGUL.search(text) is None for text in instructions)


def test_structured_output_descriptions_are_english() -> None:
    models = (
        _EdgeProposal,
        _HeadingParentChoice,
        _FragmentMergeDecision,
        _SemanticTextGroup,
        _SemanticRegionDecision,
        _CaptionDisambiguation,
        ImageDescription,
        _TableReadResult,
    )

    for model in models:
        schema = json.dumps(model.model_json_schema(), ensure_ascii=False)
        assert _HANGUL.search(schema) is None, model.__name__
