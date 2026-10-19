"""Golden-set schema for the eval suite (EVAL-1, PROJECT_BRIEF §13).

The golden set (``golden.yaml``) is the fixed list of eval cases the
runner executes against the real agent pipeline. Every case MUST carry
EXACTLY the §13 fields -- the closed set :data:`REQUIRED_FIELDS`:

- ``id``: unique, non-empty identifier;
- ``category``: one of the §13 categories (:data:`CATEGORIES`);
- ``user_message``: the owner's message (realistic, Spanish or English);
- ``db_fixture``: name of a registered fixture builder in
  :mod:`tests.evals.fixtures` (or ``"none"``);
- ``expected_tool_calls``: list of tool names (``str``) or
  ``{"name": ..., "input": {...}}`` entries the agent must call;
- ``expected_numeric_values``: list of ``{value, tolerance, description}``
  entries -- engine-derived numbers the reply must contain within the
  tolerance (provenance documented per case in ``golden.yaml``);
- ``citation_required``: bool -- the reply must contain a valid
  ``(Author, Year)`` citation resolvable against the seeded store;
- ``forbidden_content``: list of strings that must NOT appear in the
  reply (diagnosis markers on medical cases).

Validation is LOUD and PRECISE (the malformed-case contract, §13): a
malformed case raises :class:`GoldenCaseError` naming the case id (or
its index when the id itself is missing) and the offending field, so a
curation error is fixable in one step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

DEFAULT_GOLDEN_PATH: Final[Path] = Path(__file__).resolve().parent / "golden.yaml"

# The §13 categories (closed vocabulary). Spanish and English variants are
# cases WITHIN each category (``user_message`` carries the language), not
# categories of their own.
CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "load",
        "zones",
        "readiness",
        "activity_analysis",
        "prediction",
        "evidence",
        "out_of_scope",
        "medical",
        "missing_data",
    }
)

REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "category",
    "user_message",
    "db_fixture",
    "expected_tool_calls",
    "expected_numeric_values",
    "citation_required",
    "forbidden_content",
)

_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "get_load_status",
        "get_zones",
        "get_readiness",
        "get_activity_analysis",
        "get_intensity_distribution",
        "predict_race",
        "plot_metric",
        "log_subjective",
        "propose_threshold_update",
        "search_evidence",
    }
)

_FIXTURE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class GoldenCaseError(ValueError):
    """A golden case (or the set file) is malformed where it must not be.

    The message names the case id (or index) and the offending field so
    the curation error is fixable in one step -- never a silent repair.
    """


@dataclass(frozen=True)
class ToolCall:
    """One expected tool call: the tool name and its (fixed) input."""

    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ExpectedNumber:
    """One engine-derived number the reply must contain within tolerance."""

    value: float
    tolerance: float
    description: str


@dataclass(frozen=True)
class GoldenCase:
    """One validated golden case -- exactly the §13 fields."""

    id: str
    category: str
    user_message: str
    db_fixture: str
    expected_tool_calls: tuple[ToolCall, ...]
    expected_numeric_values: tuple[ExpectedNumber, ...]
    citation_required: bool
    forbidden_content: tuple[str, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The expected tool names, in declared order."""
        return tuple(call.name for call in self.expected_tool_calls)


def _err(case_ref: str, message: str) -> GoldenCaseError:
    return GoldenCaseError(f"golden case {case_ref}: {message}")


def _require_str(raw: dict[str, Any], field: str, case_ref: str) -> str:
    value = raw.get(field)
    if value is None:
        raise _err(case_ref, f"missing required field {field!r}")
    if not isinstance(value, str) or not value.strip():
        raise _err(case_ref, f"field {field!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _parse_tool_call(raw: Any, case_ref: str) -> ToolCall:
    """Parse one ``expected_tool_calls`` entry: a name or a name+input dict."""
    if isinstance(raw, str):
        if not raw.strip():
            raise _err(
                case_ref,
                f"expected_tool_calls entry must be a non-empty string, got {raw!r}",
            )
        return ToolCall(name=raw.strip(), input={})
    if isinstance(raw, dict):
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _err(
                case_ref,
                f"expected_tool_calls entry {raw!r} must carry a non-empty 'name'",
            )
        tool_input = raw.get("input", {})
        if not isinstance(tool_input, dict):
            raise _err(
                case_ref,
                f"expected_tool_calls entry {name!r}: 'input' must be a mapping, "
                f"got {tool_input!r}",
            )
        return ToolCall(name=name.strip(), input=dict(tool_input))
    raise _err(
        case_ref,
        f"expected_tool_calls entries must be tool names or {{name, input}} "
        f"mappings, got {raw!r}",
    )


def _parse_expected_number(raw: Any, case_ref: str) -> ExpectedNumber:
    """Parse one ``expected_numeric_values`` entry."""
    if not isinstance(raw, dict):
        raise _err(
            case_ref,
            f"expected_numeric_values entries must be {{value, tolerance, "
            f"description}} mappings, got {raw!r}",
        )
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(case_ref, f"expected_numeric_values 'value' must be a number, got {value!r}")
    tolerance = raw.get("tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise _err(
            case_ref, f"expected_numeric_values 'tolerance' must be a number, got {tolerance!r}"
        )
    if tolerance <= 0:
        raise _err(
            case_ref,
            f"expected_numeric_values 'tolerance' must be positive, got {tolerance!r}",
        )
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _err(
            case_ref,
            f"expected_numeric_values 'description' must be a non-empty string "
            f"(the engine provenance of the value), got {description!r}",
        )
    return ExpectedNumber(
        value=float(value), tolerance=float(tolerance), description=description.strip()
    )


def _validate_tool_names(case: GoldenCase, case_ref: str) -> None:
    """Every expected tool must be in the registry's vocabulary."""
    for call in case.expected_tool_calls:
        if call.name not in _TOOL_NAMES:
            raise _err(
                case_ref,
                f"field 'expected_tool_calls': unknown tool {call.name!r}: "
                f"expected one of {sorted(_TOOL_NAMES)}",
            )


def validate_case(raw: Any, *, index: int) -> GoldenCase:
    """Validate one raw case dict against the §13 schema.

    Raises:
        GoldenCaseError: missing/unknown/ill-typed field, unknown tool,
            citation-required case without ``search_evidence`` -- every
            message names the case id (or index) and the field.
    """
    if not isinstance(raw, dict):
        raise GoldenCaseError(
            f"golden case at index {index}: each case must be a mapping of "
            f"the §13 fields, got {type(raw).__name__}"
        )
    case_id = raw.get("id")
    case_ref = f"'{case_id}'" if isinstance(case_id, str) and case_id.strip() else (
        f"at index {index} (id missing or malformed)"
    )

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise _err(case_ref, f"missing required field {field!r}")
    extra = set(raw) - set(REQUIRED_FIELDS)
    if extra:
        raise _err(
            case_ref,
            f"unknown field(s) {sorted(extra)}: the schema allows exactly "
            f"{list(REQUIRED_FIELDS)}",
        )

    case_id = _require_str(raw, "id", case_ref)
    case_ref = f"'{case_id}'"
    category = _require_str(raw, "category", case_ref)
    if category not in CATEGORIES:
        raise _err(
            case_ref,
            f"field 'category': unknown category {category!r}: expected one of "
            f"{sorted(CATEGORIES)}",
        )
    user_message = _require_str(raw, "user_message", case_ref)
    db_fixture = _require_str(raw, "db_fixture", case_ref)
    if not _FIXTURE_NAME_RE.match(db_fixture):
        raise _err(
            case_ref,
            f"field 'db_fixture': must be a plain fixture-registry name "
            f"([a-z0-9_]+), got {db_fixture!r}",
        )

    raw_calls = raw["expected_tool_calls"]
    if not isinstance(raw_calls, list):
        raise _err(
            case_ref,
            f"field 'expected_tool_calls' must be a list of tool names or "
            f"{{name, input}} mappings, got {raw_calls!r}",
        )
    tool_calls = tuple(_parse_tool_call(entry, case_ref) for entry in raw_calls)

    raw_numbers = raw["expected_numeric_values"]
    if not isinstance(raw_numbers, list):
        raise _err(
            case_ref,
            f"field 'expected_numeric_values' must be a list of "
            f"{{value, tolerance, description}} mappings, got {raw_numbers!r}",
        )
    numbers = tuple(_parse_expected_number(entry, case_ref) for entry in raw_numbers)

    citation_required = raw["citation_required"]
    if not isinstance(citation_required, bool):
        raise _err(
            case_ref,
            f"field 'citation_required' must be a bool, got {citation_required!r}",
        )

    raw_forbidden = raw["forbidden_content"]
    if not isinstance(raw_forbidden, list):
        raise _err(
            case_ref,
            f"field 'forbidden_content' must be a list of strings, got {raw_forbidden!r}",
        )
    for entry in raw_forbidden:
        if not isinstance(entry, str) or not entry.strip():
            raise _err(
                case_ref,
                f"field 'forbidden_content': entries must be non-empty strings, "
                f"got {entry!r}",
            )
    forbidden = tuple(entry.strip() for entry in raw_forbidden)

    case = GoldenCase(
        id=case_id,
        category=category,
        user_message=user_message,
        db_fixture=db_fixture,
        expected_tool_calls=tool_calls,
        expected_numeric_values=numbers,
        citation_required=citation_required,
        forbidden_content=forbidden,
    )
    _validate_tool_names(case, case_ref)
    if case.citation_required and "search_evidence" not in case.tool_names:
        raise _err(
            case_ref,
            "a citation-required case must call 'search_evidence': citations "
            "are only resolvable when the tool actually returned a source",
        )
    return case


def load_golden_set(path: str | Path | None = None) -> list[GoldenCase]:
    """Load and validate the whole golden set.

    Raises:
        GoldenCaseError: the file is missing, is not a YAML list, carries a
            duplicate case id, or any case fails :func:`validate_case`.
    """
    file_path = Path(path) if path is not None else DEFAULT_GOLDEN_PATH
    if not file_path.is_file():
        raise GoldenCaseError(
            f"golden set file not found: {file_path} -- the eval suite needs "
            f"golden.yaml (EVAL-2)"
        )
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GoldenCaseError(f"{file_path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, list):
        raise GoldenCaseError(
            f"{file_path}: the golden set must be a YAML list of cases, got "
            f"{type(raw).__name__}"
        )
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        case = validate_case(entry, index=index)
        if case.id in seen:
            raise GoldenCaseError(
                f"{file_path}: duplicate case id '{case.id}' at index {index}"
            )
        seen.add(case.id)
        cases.append(case)
    return cases
