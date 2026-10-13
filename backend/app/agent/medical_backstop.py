"""Deterministic medical-disclosure backstop (WA-9, §3).

**Why this module exists (live evidence, owner turn 2026-10).** The model
produced a medically careful reply — reduce load, skip the intense
session, consult a doctor or physiotherapist, no diagnosis — but OMITTED
the explicit "no soy un profesional médico" disclosure that
``SYSTEM_PROMPT`` rule 5 requires. A prompt pin proves the rule reaches
the model; it does not make the model obey it. The guarantee therefore
lives where it cannot be forgotten, in two layers the pipeline applies
per turn (:mod:`app.agent.pipeline`):

1. **Prompt reinforcement** — when the inbound message carries a medical
   signal, ``MEDICAL_PROMPT_REINFORCEMENT`` is appended to the turn's
   system prompt: an explicit, mandatory sentence naming the disclosure
   the reply must include.
2. **Deterministic outcome** — after the model replies,
   :func:`ensure_medical_disclosure` APPENDS :data:`MEDICAL_DISCLOSURE`
   to the reply when the signal was detected and the reply does not
   already contain the disclosure. It never replaces or rewrites the
   model's content, and the pipeline records the intervention in the
   outbound audit payload (``medical_disclosure_backstop: true``) instead
   of leaving it silent.

**The detector is a safety BACKSTOP — the trade-off is explicit.** A
false positive costs at most one appended sentence the model should have
said anyway (rule 5); a false negative is TODAY'S behaviour (the
unguaranteed prompt rule, i.e. exactly the observed defect). The signal
list is therefore small and conservative — only explicit pain / injury /
illness words — but matching is prefix-based over accent-stripped,
lower-cased tokens so inflections are caught:

- Spanish pain: ``dolor`` (dolor, dolores), ``duele``/``duelen`` (stem
  ``duel`` handled as the forms ``duele``, ``duelen``), ``doler``-family
  (doler, dolerá, dolería), ``dolía``, ``doliendo``;
- Spanish injury: ``lesión``-family (lesión, lesiones, lesionado, lesioné);
- Spanish illness/discomfort: ``enfermo/enferma/enfermedad`` (stem
  ``enferm``), ``molestia/molestias/molesta`` (stem ``molest``),
  ``mareo/mareado`` (stem ``mare``);
- English: ``pain`` (pain, painful), ``hurt`` (hurt, hurts, hurting),
  ``injur`` (injury, injured), ``sick``, ``illness``, ``ache``
  (ache, aching), ``sore`` (sore, soreness).

Known accepted false-positive risk (part of the trade-off): everyday words
sharing a stem with a signal in a training chat are unlikely
(``dólar``/``dolmen`` are deliberately NOT signals — the Spanish pain
family uses the longer forms above, not the bare ``dol`` stem). Implicit
signals with no pain/injury/illness word ("no me encuentro bien", "me
siento raro") are documented false negatives: they are today's behaviour,
not a regression this module introduces.
"""

from __future__ import annotations

import unicodedata
from typing import Final

# --- The disclosure ---------------------------------------------------------

DISCLOSURE_PHRASE: Final[str] = "no soy un profesional médico"
"""The exact phrase prompt rule 5 requires; containment is checked on the
accent-stripped, lower-cased, whitespace-collapsed reply, so case and
accent variations of the same sentence still count."""

MEDICAL_DISCLOSURE: Final[str] = (
    "Recuerda: no soy un profesional médico; si el dolor continúa, "
    "consulta a un médico o fisioterapeuta."
)
"""The sentence appended to a medical reply that forgot the disclosure.
Appended as one more natural sentence — the model's content is never
replaced, truncated or rewritten."""

MEDICAL_PROMPT_REINFORCEMENT: Final[str] = (
    "REGLA 5, REFUERZO DE ESTE TURNO: el mensaje del atleta contiene una "
    "señal de dolor, lesión o enfermedad. Tu respuesta DEBE incluir, "
    "literalmente, la frase «no soy un profesional médico»."
)
"""Appended to the turn's system prompt when the inbound message carries a
medical signal (layer 1): the instruction is explicit for that turn."""

# --- The detector -----------------------------------------------------------

_MEDICAL_SIGNALS: Final[tuple[str, ...]] = (
    # Spanish: pain (long forms on purpose — see the module trade-off note).
    "dolor",   # dolor, dolores
    "duele",   # duele, duelen
    "doler",   # doler, dolerá, dolería
    "dolia",   # dolía, dolíamos
    "doliendo",
    # Spanish: injury.
    "lesion",  # lesión, lesiones, lesionado, lesioné
    # Spanish: illness / discomfort.
    "enferm",  # enfermo, enferma, enfermedad
    "molest",  # molestia, molestias, molesta, molestan
    "mare",    # mareo, mareado, mareos
    # English.
    "pain",    # pain, pains, painful
    "hurt",    # hurt, hurts, hurting
    "injur",   # injury, injured, injuries
    "sick",    # sick, sickness
    "illness",
    "ache",    # ache, aches
    "aching",  # aching ('ache' is not a prefix of 'aching')
    "sore",    # sore, soreness
)
"""Signal prefixes (accent-stripped, lower-cased): a token STARTING with
any of these is a medical signal. See the module docstring for the
documented trade-off (false positive = one extra sentence; false negative
= today's behaviour)."""


def _normalize(text: str) -> str:
    """Lower-case, strip accents and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _tokens(text: str) -> list[str]:
    """Alphanumeric tokens of the normalized text (punctuation split)."""
    return "".join(ch if ch.isalnum() else " " for ch in _normalize(text)).split()


def detect_medical_signal(text: str) -> bool:
    """True when ``text`` carries an explicit pain/injury/illness signal.

    Conservative, Spanish + English, accent- and inflection-tolerant:
    matching is token-prefix based over the accent-stripped, lower-cased
    text (``"Lesión"``/``"lesionado"``/``"LESIÓN"`` all match). The
    documented trade-off (safety backstop): a false positive costs one
    appended sentence the model should have said anyway; a false negative
    is today's behaviour — so the small signal list prefers recall on
    inflections while keeping only explicit pain/injury/illness words.
    """
    return any(
        token.startswith(signal)
        for token in _tokens(text)
        for signal in _MEDICAL_SIGNALS
    )


def reply_contains_disclosure(reply: str) -> bool:
    """True when the reply already contains the required disclosure phrase
    (case-, accent- and whitespace-tolerant)."""
    return _normalize(DISCLOSURE_PHRASE) in _normalize(reply)


def ensure_medical_disclosure(reply: str) -> tuple[str, bool]:
    """Guarantee the disclosure in a medical turn's reply.

    Returns ``(reply, applied)``. When the reply already contains the
    disclosure it is returned unchanged (never duplicated); otherwise
    :data:`MEDICAL_DISCLOSURE` is appended as one more natural sentence
    and ``applied`` is True (the pipeline records the intervention in the
    outbound audit payload — never silent).
    """
    if reply_contains_disclosure(reply):
        return reply, False
    ensured = reply.rstrip() + " " + MEDICAL_DISCLOSURE
    return ensured, True


__all__ = [
    "DISCLOSURE_PHRASE",
    "MEDICAL_DISCLOSURE",
    "MEDICAL_PROMPT_REINFORCEMENT",
    "detect_medical_signal",
    "ensure_medical_disclosure",
    "reply_contains_disclosure",
]
