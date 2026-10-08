"""Explicit tool registry for the WhatsApp agent (WA-3, brief §9.3).

**Security constraint that comes with the SDK** (ODD doc, "Agent runtime:
Strands Agents SDK"): the Strands Agents SDK can load tools from a
directory (``load_tools_from_directory``) and a companion
``strands-agents-tools`` package ships file-editing, shell and HTTP
tools. The agent must be given an EXPLICIT list containing only this
project's tools — never the directory loader, never the vended tools —
otherwise the model could execute commands on the host. This module is
the single place that list is built; the pipeline passes exactly what
:func:`tool_list` returns.

**What is the SDK's and what is ours**: the tool-calling LOOP (deciding
when to call, executing, feeding results back) is the SDK's; ours is the
tool set itself — thin wrappers over the deterministic engine that return
typed outputs with provenance (``engine_version``, ``computed_at``, data
coverage), so the model never calculates a number (§3, §9.3).

``get_load_status`` is a STUB: the real engine-backed wrappers land with
WA-6 (Features 3-5 outputs). The stub already answers the insufficient-
data contract (§9.3): it reports what is missing instead of inventing a
value, and carries provenance so tests can assert the agent sees it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from strands.tools import tool

from app.core.settings import get_settings


@tool
def get_load_status(date_range: str = "7d", sport: str | None = None) -> dict[str, Any]:
    """Training-load status (CTL/ATL/TSB) for a date range, optionally per sport.

    IMPORTANT: this is a STUB delegation — the engine-backed version lands
    with WA-6. It always reports insufficient data rather than inventing a
    number (§3: the LLM never calculates a performance number).

    Args:
        date_range: Range to summarise, e.g. "7d", "30d" or "2026-09-01/2026-09-30".
        sport: Optional sport filter ("ride", "run", "swim"); omit for combined.

    Returns:
        A dict with ``status`` ("insufficient_data" for the stub), the
        engine_version of the pipeline that would compute it, the time of
        the computation and a human-readable ``detail`` explaining what is
        missing.
    """
    return {
        "status": "insufficient_data",
        "engine_version": get_settings().engine_version,
        "computed_at": datetime.now(UTC).isoformat(),
        "detail": "get_load_status is a stub until WA-6 wires the engine; "
        "no load values are available from this tool yet.",
    }


def tool_list() -> list[Any]:
    """The EXPLICIT tool list handed to the agent.

    Only this project's tools, in this list, ever. Never
    ``load_tools_from_directory``; never the ``strands-agents-tools``
    vended tools (shell/file/HTTP).
    """
    return [get_load_status]
