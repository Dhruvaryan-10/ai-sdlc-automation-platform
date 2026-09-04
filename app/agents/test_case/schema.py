"""
Transient Pydantic schema for the QA / Test Case Agent (LangChain structured-output pilot).

WHY THIS EXISTS: Phase 6 has the QA agent return a JSON *string* which
`TestCaseService._parse_and_validate` then checks by hand. This module lets the
agent ask Gemini (through LangChain's `with_structured_output`) to emit a value
that already conforms to the shape, so malformed output is rejected at the LLM
boundary instead of only in the service.

DELIBERATELY NARROW SCOPE:
    * These models mirror the EXISTING QA JSON structure exactly — the same
      fields, in the same shape, as the `SCHEMA` block in
      `prompts/generate_test_cases.txt`. No new fields, no renames.
    * `priority` / `test_type` stay plain non-empty strings (NOT `Literal`
      enums) — the Phase 6 service only checks non-emptiness, and this pilot
      must preserve that behavioural contract.
    * Duplicate-id / ordering validation is intentionally NOT done here.
      `TestCaseService._parse_and_validate` owns that and stays the second line
      of defense.
    * Nothing here is ever persisted. `TestCaseService` still renders Markdown
      via `_render_markdown` and stores that through `VersionService`. These
      objects live only between `_invoke_structured` and `json.dumps`.
"""

from pydantic import BaseModel, Field

# Same identifier shape the Phase 6 service enforces (`_TC_ID_PATTERN`).
_TC_ID_PATTERN = r"^TC-\d{3}$"


class TestCaseModel(BaseModel):
    """One test case — mirrors a single object in the prompt's `test_cases` array."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    id: str = Field(pattern=_TC_ID_PATTERN)
    title: str = Field(min_length=1)
    # Required traceability: every test case must name the primary thing it
    # verifies (e.g. "FR-3" or "US-002"). The other *_reference fields stay
    # optional because HLD / LLD / User Stories are optional context.
    requirement_or_story_ref: str = Field(min_length=1)
    brd_reference: str | None = None
    user_story_reference: str | None = None
    hld_reference: str | None = None
    lld_reference: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    test_data: list[str] = Field(default_factory=list)
    test_steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    priority: str = Field(min_length=1)
    test_type: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    notes: str = ""


class TestCaseList(BaseModel):
    """The full agent payload: a non-empty list of test cases."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    test_cases: list[TestCaseModel] = Field(min_length=1)
