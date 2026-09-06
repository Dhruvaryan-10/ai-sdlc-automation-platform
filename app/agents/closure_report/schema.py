"""
Transient Pydantic schema for the Closure Report Agent (LangChain structured-output).

WHY THIS EXISTS: Gemini is asked (through LangChain's `with_structured_output`)
to return a value that already conforms to this narrow shape, so a malformed
response is rejected at the LLM boundary rather than only in the service.

DELIBERATELY NARROW SCOPE:
    * Every field is a plain non-empty string. Gemini writes prose ONLY.
    * There is NO field here for a count, a percentage, a version number, a
      coverage figure, a closure status, or a readiness verdict. Those are
      deterministic facts computed by `ClosureReportService` and must never be
      produced by the model (see `app/agents/closure_report/prompts/closure_report.txt`).
    * Nothing here is ever persisted. `ClosureReportService` renders a Markdown
      document (deterministic facts + this narrative) and stores THAT through
      `VersionService`. A `ClosureNarrative` object lives only between
      `_invoke_structured` and `json.dumps`.

Mirrors the structure of `app/agents/test_case/schema.py` (the repo's first
structured-output agent) without sharing a base class - the deliberate
per-agent duplication described in CLAUDE.md.
"""

from pydantic import BaseModel, Field


class ClosureNarrative(BaseModel):
    """The narrative-only payload Gemini produces for a closure report.

    Six short prose sections. The service supplies every objective fact; the
    model only phrases the human-readable synthesis around those facts.
    """

    executive_summary: str = Field(min_length=1)
    scope_summary: str = Field(min_length=1)
    findings_summary: str = Field(min_length=1)
    outstanding_items_summary: str = Field(min_length=1)
    closure_summary: str = Field(min_length=1)
    limitations: str = Field(min_length=1)
