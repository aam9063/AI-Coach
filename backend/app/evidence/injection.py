"""Per-turn evidence injection backstop (Feature 9, live finding 2026-10-14).

**Why this module exists.** Prompt rules failed THREE times (2026-10-14)
for the same behaviour: the store held the owner's guide, retrieval
demonstrably returned the right chunk at similarity 0.597 for the exact
question — and the real model answered from memory without calling
``search_evidence`` (the citation rule, the re-search-every-time rule and
the tools-outrank-your-memory rule all failed, exactly as the
medical-disclosure rule failed before its deterministic backstop,
:mod:`app.agent.medical_backstop`). Conclusion: stop asking, guarantee.

**What it does.** For every inbound turn, BEFORE the model is invoked, the
pipeline calls :func:`build_evidence_injection` with the owner's CURRENT
message: the SAME retrieval and embedding adapters the ``search_evidence``
tool uses (no second query path, no new SQL) fetch the top-k chunks, and
they are returned as a clearly-labelled block the pipeline appends to THAT
turn's system prompt — visible on the DELIVERED prompt, per-turn, never
carried forward.

**Fail-open, always.** If the embedding adapter raises, the retrieval
raises, the store is empty, or nothing is above the similarity floor, the
function returns an EMPTY string — the turn runs exactly as it does today,
the same guarantee tracing follows. Injection is never a failure path, and
NOTHING is injected (not even an empty heading) when nothing qualifies.

**Grounding, never printed** (OWNER DECISION 2026-10-14): the block
carries the material plus the rule that it is used ONLY if its content
actually supports the claim, is never cited or mentioned as a reference,
and that with no supporting material the agent says explicitly it has no
source in the store instead of improvising. The standing prompt rules
(still useful) are NOT replaced by this block.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.evidence.embeddings import create_embedding
from app.evidence.retrieval import RetrievalResult, retrieve_chunks

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

INJECTION_HEADER = (
    "MATERIAL DE LA BIBLIOTECA DEL PROPIETARIO, RECUPERADO AUTOMÁTICAMENTE "
    "PARA ESTA PREGUNTA (solo este turno; nunca se arrastra a otro turno; "
    "el sistema lo entrega antes de tu respuesta, no es el resultado de una "
    "llamada tuya a search_evidence):"
)
"""The unmistakable label: owner's library, THIS question, per-turn,
never carried forward."""

_INTERNAL_HINT = "(coincidencia: {score:.2f}; dato interno, nunca impreso)"

INJECTION_RULE = (
    "Cómo usar este material: es SOLO una base de apoyo — úsalo únicamente "
    "si su contenido realmente apoya la afirmación que vas a hacer; si no "
    "la apoya, ignóralo. Es información interna para fundamentar tu "
    "respuesta: NUNCA lo cites ni lo menciones como referencia "
    "bibliográfica en tu respuesta (decisión del propietario: las "
    "respuestas no muestran citas). Cuando ningún material apoye la "
    "afirmación, o no se haya recuperado nada, di explícitamente que no "
    "tienes una fuente en el almacén, en vez de improvisar. Si necesitas "
    "más evidencia para esta pregunta, llama a search_evidence con la "
    "pregunta actual."
)
"""The grounding rule riding the block: use only what supports the claim,
never print it, and say explicitly there is no source otherwise."""


def _format_block(retrieval: RetrievalResult) -> str:
    """The labelled block for an ``ok`` retrieval — material + rule."""
    lines = [INJECTION_HEADER]
    for chunk in retrieval.results:
        lines.append(
            f"- «{chunk.text}» " + _INTERNAL_HINT.format(score=chunk.similarity)
        )
    lines.append(INJECTION_RULE)
    return "\n".join(lines)


async def build_evidence_injection(
    session_factory: SessionFactory, settings: Settings, query: str
) -> str:
    """Retrieve the top-k chunks for ``query`` and format the labelled
    per-turn injection block.

    Reuses the ONE embedding adapter and the ONE retrieval path the
    ``search_evidence`` tool uses (§6 adapter principle; no second query
    path, no new SQL). ``k`` is owner configuration
    (``settings.evidence_injection_k``); the similarity floor is the
    retrieval module's own.

    Fail-open: ANY problem — a blank query, an adapter/retrieval exception,
    an empty store, nothing above the floor — returns an EMPTY string, so
    the caller appends nothing and the turn runs exactly as it would
    without this module. Never raises.

    Args:
        session_factory: Async session factory on the application database.
        settings: Application settings (embedding provider, injection k).
        query: The owner's CURRENT message for this turn.

    Returns:
        The block to append to this turn's system prompt, or ``""`` when
        nothing qualifies or anything failed.
    """
    if not query.strip():
        return ""
    try:
        adapter = create_embedding(settings)
        query_embedding = adapter.embed([query])[0]
        async with session_factory() as session:
            retrieval = await retrieve_chunks(
                session,
                query_embedding,
                k=settings.evidence_injection_k,
            )
    except Exception:
        logger.warning(
            "evidence injection: retrieval failed; the turn runs without "
            "injected material",
            exc_info=True,
        )
        return ""
    if retrieval.status != "ok" or not retrieval.results:
        return ""
    return _format_block(retrieval)


__all__ = [
    "INJECTION_HEADER",
    "INJECTION_RULE",
    "build_evidence_injection",
]
