"""LLM-as-judge for the ONLY qualitative eval dimensions: tone and clarity
(EVAL-9, PROJECT_BRIEF §13).

Hard limits, by design:

- The judge NEVER scores factual correctness, numeric faithfulness, tool
  selection, citations or safety — those are the deterministic checks in
  :mod:`tests.evals.runner` (§13: the LLM path may only judge
  qualitative dimensions).
- The judge runs ONLY in calibration: its agreement rate against the
  owner-labelled seed set (``judge_labels.yaml``) is REPORTED, never
  gated (§13 requires reporting, not a threshold).
- The judge needs a REAL provider; it skips when none is configured
  (CI has no LLM key — the deterministic gates do not need one).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from strands import Agent

from app.agent.model_factory import create_model
from app.core.settings import Settings

_JUDGE_LABELS_PATH: Final[Path] = Path(__file__).parent / "judge_labels.yaml"

JUDGE_SYSTEM_PROMPT: Final[str] = """Eres un evaluador de TONO y CLARIDAD de
respuestas de un coach de triatlón por WhatsApp. Tu ÚNICO trabajo es
calificar dos dimensiones cualitativas:

1. Tono: cálido y directo, como un coach humano que conoce al atleta;
   sin tono robótico ni excesivamente formal.
2. Claridad: la respuesta es entendible de un vistazo en un teléfono,
   accionable, sin jerga innecesaria.

NO evalúes NUNCA si los números o los hechos son correctos — eso no es
tu trabajo (otros checks deterministas lo cubren). NO evalúes estilo de
citas: las respuestas nunca muestran citas de fuentes (decisión del
dueño).

Responde EXCLUSIVAMENTE un objeto JSON:
{"verdict": "acceptable" | "unacceptable", "reason": "<una frase>"}
"""


def judge_reply(reply: str, settings: Settings) -> dict[str, str]:
    """Rate ONE reply on tone/clarity via the configured real provider.

    Returns ``{"verdict": ..., "reason": ...}``; raises ``ValueError``
    when the model's output is not the required JSON shape (the caller
    counts that as a calibration mismatch, never as a silent pass)."""
    model = create_model(settings)
    agent = Agent(model=model, system_prompt=JUDGE_SYSTEM_PROMPT)
    result = agent(f"Califica esta respuesta:\n\n{reply}")
    text = str(result).strip()
    # The model may wrap the JSON in prose or fences: take the outermost
    # braces and parse.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"judge reply is not JSON: {text[:200]!r}")
    parsed: dict[str, Any] = json.loads(text[start : end + 1])
    verdict = parsed.get("verdict")
    if verdict not in ("acceptable", "unacceptable") or not parsed.get("reason"):
        raise ValueError(f"judge verdict malformed: {parsed!r}")
    return {"verdict": str(verdict), "reason": str(parsed["reason"])}


def agreement_rate(verdicts: list[dict[str, str]], labels: list[str]) -> float:
    """Fraction of judged verdicts matching the owner's labels (1.0 when
    the seed set is empty — nothing to disagree with)."""
    if not labels:
        return 1.0
    matches = sum(
        1 for v, label in zip(verdicts, labels, strict=True) if v["verdict"] == label
    )
    return matches / len(labels)
