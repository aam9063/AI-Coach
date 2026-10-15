# ODD Feature: evidence-store

Status: pending | Feature 9 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Build the evidence store that lets the agent cite a source for every scientific claim (PROJECT_BRIEF.md §8): a markdown paper library curated by the owner, chunking and embedding into PostgreSQL `pgvector`, retrieval with citation strings, and the `search_evidence` tool, plus the citation rule wired into the agent's prompt.

## Problem

The brief's core principle requires that "every scientific claim in a reply must cite a source from the evidence store. If no source supports a claim, the agent says so explicitly instead of improvising" (PROJECT_BRIEF.md §3). Generic chatbots give advice with no evidence behind it (§2); the evidence store makes claims auditable and grounds the coach's reasoning in the sports-science literature the engine itself cites.

## Scope

- Paper library (§8): `evidence/papers/<slug>.md`, one file per paper, written/curated by the owner (summaries in own words, not copied full text); front matter: `title`, `authors`, `year`, `journal`, `doi`, `topics`, `key_findings`, `limitations`.
- Seed list minimum (§8): Banister 1975; Coggan (Training and Racing with a Power Meter); Foster 2001 (sRPE); Williams 2017 (EWMA ACWR); Impellizzeri 2020 (ACWR critique); Monod & Scherrer 1965; Jones 2019 (CP review); Wakayoshi 1992 (CSS); Daniels (Running Formula); Riegel 1981; Martin 1998 (cycling power model); Minetti 2002 (gradient running cost); Seiler 2010; Stöggl & Sperlich 2014; Plews 2013; Kiviniemi 2007; Maunder 2021 (durability).
- Ingestion pipeline under `backend/app/evidence/` (§6): loader, chunker, embeddings, retrieval; store chunks in `pgvector` with the citation string (§8).
- Retrieval tool (§8): returns chunks with citations; the agent must include at least one citation when making a scientific claim, formatted as `(Author, Year)` with DOI available on request.
- `evidence_chunk` DB model (§6): paper id, chunk text, embedding, citation string.
- `search_evidence(query, k)` tool (§9.3) returning chunks with citations as a typed Pydantic model with `computed_at`, `engine_version`, coverage info.
- Citation rule in the agent's system prompt (§12.9): every scientific claim cites at least one store source; no supporting evidence → explicit "no evidence in store" answer.

## Constraints

- Paper files are owner-curated summaries in their own words, **not copied full text** (§8) — no copyright-violating content.
- Citations must be formatted `(Author, Year)` with DOI available on request (§8).
- If no source supports a claim, the agent says so explicitly instead of improvising (§3, §12.9).
- Personal health data stays in the owner's infrastructure; embeddings contain paper content only, never athlete data or prompts (§14).
- **No Strava API data may reach embeddings, prompts or tool outputs** (§5.3, §14).
- Secrets (embedding provider keys) only via environment variables (§14).
- The LLM provider/embedding choice follows §15 open decisions; the embedding model must be switchable by config (§6 adapter principle).
- Depends on Features 1 (`project-scaffold`, pgvector Postgres) and 6 (`whatsapp-agent`, tool registry and prompt wiring); evals in Feature 11 verify citation compliance (§13).

## Checklist

- [x] `EV-1`: RED: front-matter loader tests — parse a sample `evidence/papers/<slug>.md` with `title`, `authors`, `year`, `journal`, `doi`, `topics`, `key_findings`, `limitations` and fail clearly on missing required fields (§8).
- [x] `EV-2`: GREEN: implement the markdown loader with front-matter validation (§8).
- [x] `EV-3`: RED+GREEN: chunker tests — deterministic chunking with overlap, chunk boundaries preserving sentences, each chunk carrying its paper's citation string (§8).
- [x] `EV-4`: RED+GREEN: embedding + pgvector storage tests against a fake embedding adapter; `evidence_chunk` rows with paper id, chunk text, embedding, citation string (§6, §8).
- [x] `EV-5`: RED+GREEN: retrieval tests — top-k by cosine similarity returns chunks with correct citation strings; empty store or no relevant results handled explicitly (§8).
- [x] `EV-6`: GREEN: implement the `search_evidence(query, k)` tool returning typed Pydantic output with `computed_at`, `engine_version`, coverage info (§9.3).
- [ ] ~~`EV-7`~~ **(DROPPED by owner decision, 2026-10-14)** -- the seventeen seed papers are not curated; see Owner decisions: Create the seed paper files for the §8 minimum list (owner-curated summaries, own words) under `evidence/papers/`.
- [x] `EV-8`: **(implemented with citations in `11a5ba1`, then rewritten WITHOUT them after the owner decision of 2026-10-14: the agent uses the store but shows no reference)** RED+GREEN: citation rule in the agent prompt — replies with scientific claims include at least one `(Author, Year)` citation; a question with no supporting evidence gets an explicit "no evidence in store" answer (§3, §8, §12.9), tested against the fake LLM adapter.
- [x] `EV-9`: Wire citation behavior through Feature 6's agent loop and document DOI-on-request behavior in the prompt (§8).
- [x] `EV-10`: Verify end-to-end: a scientific question returns a cited answer; an unsupported question returns the explicit no-evidence response (§12.9).

## Acceptance criteria

From PROJECT_BRIEF.md §12.9:

- Scientific claims in replies include at least one citation (checked by evals); a question with no supporting evidence gets an explicit "no evidence in store" answer.

Directly implied conditions:

- The seed paper list from §8 exists under `evidence/papers/` with the required front matter.
- Citations are formatted `(Author, Year)` with DOI available on request (§8).

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Owner decisions

- **2026-10-13 (two source kinds).** The store accepts BOTH papers and curated training documents, not only papers. Papers keep the brief's schema under `evidence/papers/<slug>.md`; training documents (for example the owner's *Pedalea con Ciencia* guide in `docs/exercises/`) live under a new sibling `evidence/guides/<slug>.md`, each file declaring its `kind` in the front matter. Validation must not demand paper-only fields (journal, doi) from a guide while still failing clearly on whatever is genuinely missing for that kind, and citations must stay truthful: a guide is a training document, never cited as if it were peer-reviewed literature. Curated summaries remain the owner's own words for both kinds and the original PDF stays an original file.
- **`EV-7` stays owner-curated.** No agent-written summary may be substituted for the seed papers: a model writing scientific claims attributed to real authors out of its own memory is precisely the failure this feature exists to prevent (and §8 requires summaries in the owner's words). The owner either writes them or supplies the sources for a grounded draft to review.

- **2026-10-14 (no citations in the replies).** The owner decided the agent must NOT display source references in its answers: no `(Author, Year)`, no guide form, no DOI on request -- he does not want scientific references surfacing in the chat -- and `EV-7`'s seventeen seed papers are dropped, since they would have to be curated by hand and he does not want that bibliography. What stays is everything that makes the store *useful*: the ingested material, `search_evidence`, the loader and the retrieval, and the honesty half of the rule (when nothing relevant comes back, or the store is genuinely empty, the agent says it has no source instead of answering from memory).
  **Deviations from the brief, recorded rather than hidden**: §3 and §8 require every scientific claim to cite a source as `(Author, Year)`; the reply no longer shows one. **The risk this creates**, stated once and in writing: without a visible citation the owner cannot tell an answer grounded in his own library from one the model made up -- and there is live evidence from the same day, because the agent answered "FC max = 220 - edad" while the ingested guide says Tanaka (208.75 - 0.73 x age) with nothing in the reply revealing it. Auditability moves from the message to the trace: the tool calls and their outputs are still recorded per turn in `message_log`, so a retrieval can be inspected after the fact even though the chat does not show it. Recovering in-message visibility later is cheap if wanted (for example a light "segun tu guia" mention, which is not a citation); it is deliberately not implemented now.

- Retrieval truthfulness fixed at `99ad887`, and the live failure that exposed it is the reason the fix matters: the owner asked how to compute his maximum heart rate, the model reused the PREVIOUS turn's query, the tool answered "the evidence store is empty" while ten chunks of his own guide were ingested, and the reply answered "220 - edad" from memory -- a formula the ingested guide contradicts. The tool's text had been written when the store really was empty and nothing ever made it look again; the suite stayed green because it pinned that wrong text as the expectation. Now the real chunk count comes from a COUNT query (never `len(results)`, which reported a ten-chunk store as three) and the empty-store wording is a **CHECKED claim** emitted only after a COUNT proves zero, so the model can never be told the store is empty while it holds chunks. The floor is documented as a cross-domain guard with its measured numbers -- genuinely answered 0.60-0.69, same-domain questions the guide does NOT cover 0.38-0.56, disjoint 0.05-0.19 -- which is why no floor can decide relevance and the agent-side rule (a chunk counts as a source only if its content supports the claim) carries that weight. Rule 4 also requires the owner's CURRENT question as the query. Parent-verified: `tests/agent` + `tests/evidence` green, ruff clean, mypy strict clean (187 files), one head, and the writer's live acceptance query returning the guide's HRmax chunk at similarity 0.596 instead of any emptiness claim.

## Progress

Not started.

- EV-1..EV-6 done at `2f28780`: the pipeline and its tool exist in TWO source kinds (owner decision recorded above). The loader walks `evidence/papers/` and the new `evidence/guides/`, fails naming file and field, demands journal and doi from a paper and never from a guide, and decides the citation once at load time so a training document is cited as `(Author, Year, training guide)` and can never pass for peer-reviewed literature. Ingestion is loader -> deterministic sentence-preserving chunker with overlap -> config-switchable embedding adapter (fake for every test, OpenAI for production, unknown value raises) -> idempotent pgvector upsert; the migration creates the extension and both tables and was applied to the dev database. Retrieval is top-k cosine with explicit `empty_store` and `no_relevant_results` statuses instead of a bare empty list, and `search_evidence` is the seventh tool in the explicit registry, its docstring (the model-facing schema) instructing the agent to say it has no source rather than improvise. Parent-verified: `tests/evidence` + `tests/agent` 230 passed, ruff clean, mypy strict clean (186 files), one head, the tool names read back from the registry. EV-7 content remains the owner's; - EV-8..EV-10 done at `11a5ba1`: rule 4 no longer claims the store does not exist -- that clause was false the moment `search_evidence` became a tool. The rule now requires searching BEFORE a scientific claim, citing only what the tool returned (`(Autor, Año)` for a paper, `(Autor, Año, guía de entrenamiento)` for a guide, never presented as peer-reviewed literature), and stating explicitly that there is no source in the store when it is empty or nothing is similar. **The parent's one pre-landing check paid for itself**: the tool did NOT carry the DOI the rule promises, so the retrieval result now carries `doi` populated from the joined source row (`None` for guides) and the tool's docstring documents it -- a prompt promising a DOI the tool cannot return would have been a promise the agent could not keep. `PROMPT_VERSION` went `1.0.0 -> 1.1.0` per that file's own versioning rule. End-to-end with the fake model and fake embeddings, zero network: a synthetic test-only paper ingested through the real pipeline yields a reply carrying its citation and DOI with the tool call audited; an empty store yields the explicit no-source answer with proof that no citation was invented. The synthetic fixture never touches the real store, which remains the owner's. Parent-verified: `tests/agent` + `tests/evidence` green, ruff clean, mypy strict clean (187 files), one head. Recorded limitation: the fake model scripts compliant replies, so this proves the pipeline and tool path, not real-model comprehension -- citation compliance by the real LLM is evals work (Feature 11).

Commits: 2f28780 (EV-1..EV-6), 11a5ba1 (EV-8..EV-10)
