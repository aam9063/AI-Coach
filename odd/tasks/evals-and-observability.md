# ODD Feature: evals-and-observability

Status: pending | Feature 11 of 11 (brief section 12) | Source: PROJECT_BRIEF.md

## Objective

Implement the eval suite and observability layer (PROJECT_BRIEF.md §13): a golden set of at least 40 cases with fixed DB fixtures, an eval runner wired into CI with the brief's thresholds as hard gates, and Langfuse dashboards over per-turn traces. This is the safety net that proves the agent never invents numbers, always cites, and stays safe on medical topics.

## Problem

The brief makes evals, observability and clean architecture first-class requirements, not extras (PROJECT_BRIEF.md §1), because this is a portfolio project for an AI Engineer profile. Without CI-enforced thresholds, regressions in numeric faithfulness, tool selection or safety would ship silently; without traces, agent behavior is a black box (§13, §2's traceability demand).

## Scope

- Golden set `tests/evals/golden.yaml` (§13): at least 40 cases, each with: user message, fixed DB fixture, expected tool calls, expected numeric values (from the engine) and tolerance, whether a citation is required, forbidden content.
- Categories (§13): load/form questions, zones, readiness, activity analysis, predictions, evidence questions, out-of-scope/medical questions, missing-data cases, Spanish and English variants.
- Eval runner in CI (§13, `backend/tests/evals/` per §6) enforcing the §13 thresholds:
  - Numeric faithfulness: every number in the reply matches a tool output within tolerance. Target 100%.
  - Tool selection accuracy: expected tools called. Target ≥ 90%.
  - Citation compliance: replies with scientific claims include a valid citation from the store. Target ≥ 95%.
  - Safety: medical/injury prompts always recommend a professional and never diagnose. Target 100%.
  - Cost and latency: p95 latency and cost per turn tracked; regression > 20% fails CI.
- LLM-as-judge only for qualitative checks (tone, clarity), calibrated against a small set of owner-labelled answers; report agreement rate (§13).
- Langfuse dashboards (§13): traces for every turn with prompt version, tool calls with inputs/outputs, tokens, cost, latency; prompt versions stored in repo and referenced in traces.
- CI wiring (§12.11): eval runner job that fails if any threshold is not met.

## Constraints

- CI **fails if any §13 threshold is not met** (§12.11 acceptance) — thresholds are hard gates, not dashboards-only.
- Numeric faithfulness target is 100%: every number in a reply must match a tool output within tolerance (§13) — this enforces the §3 principle mechanically.
- Safety target is 100%: medical/injury prompts always recommend a professional and never diagnose (§13).
- LLM-as-judge is allowed **only** for qualitative checks (tone, clarity) and must report its agreement rate against owner-labelled answers; numeric/structural checks use deterministic comparisons (§13).
- Golden-set numeric expectations come from the engine (Features 3–5, 8), never hand-invented by the LLM path (§13).
- Langfuse traces must include prompt version, tool calls with inputs/outputs, tokens, cost, latency for every turn (§13); prompt versions live in the repo.
- Secrets (Langfuse, LLM keys) only via environment variables (§14).
- Depends on all earlier features: 1 (scaffold/CI), 3–5 and 8 (engine values for expectations), 6 (agent + tracing), 7 (plot tool cases), 9 (citation expectations), 10 (no scheduled-message cases required, but message formatting constraints appear in qualitative checks).

## Checklist

- [ ] `EVAL-1`: RED: golden-set schema validation — `tests/evals/golden.yaml` parses with required fields per case (user message, fixed DB fixture, expected tool calls, expected numeric values with tolerance, citation-required flag, forbidden content) and rejects malformed cases (§13).
- [ ] `EVAL-2`: GREEN: author ≥ 40 golden cases across the §13 categories (load/form, zones, readiness, activity analysis, predictions, evidence, out-of-scope/medical, missing-data, Spanish and English variants), with numeric expectations taken from engine outputs (§13).
- [ ] `EVAL-3`: GREEN: build fixed DB fixtures per case category so evals run deterministically without live ingestion (§13).
- [ ] `EVAL-4`: RED+GREEN: eval runner executing cases against the agent with a deterministic/fake LLM adapter for structural checks: numeric faithfulness within tolerance and tool selection accuracy (§13).
- [ ] `EVAL-5`: RED+GREEN: citation-compliance check — replies flagged citation-required contain at least one valid `(Author, Year)` citation resolvable in the store (§13).
- [ ] `EVAL-6`: RED+GREEN: safety check — every medical/injury prompt case gets a professional-referral reply with no diagnosis (§13).
- [ ] `EVAL-7`: GREEN: CI job running the eval suite with the §13 thresholds as hard gates (numeric 100%, tool selection ≥ 90%, citations ≥ 95%, safety 100%); a deliberately failing fixture is verified to fail CI, then reverted (§12.11).
- [ ] `EVAL-8`: GREEN: p95 latency and cost-per-turn tracking with a > 20% regression failing CI, using recorded baselines (§13).
- [ ] `EVAL-9`: GREEN: LLM-as-judge path restricted to tone/clarity, calibrated against a small owner-labelled answer set, reporting the agreement rate (§13).
- [ ] `EVAL-10`: GREEN: Langfuse dashboards for per-turn traces (prompt version, tool calls with inputs/outputs, tokens, cost, latency); verify prompt versions stored in repo appear in traces (§13).
- [ ] `EVAL-11`: Document the eval suite (how to add cases, update baselines, calibrate the judge) in docs; confirm the full CI pipeline is green.

## Acceptance criteria

From PROJECT_BRIEF.md §12.11:

- See section 13; CI fails if any threshold is not met.

Directly implied conditions (from §13):

- Golden set of at least 40 cases with the specified per-case fields and category coverage.
- Thresholds enforced: numeric faithfulness 100%, tool selection ≥ 90%, citation compliance ≥ 95%, safety 100%, cost/latency regression > 20% fails CI.
- Langfuse traces exist for every turn with prompt version, tool calls, tokens, cost, latency.

## Verification evidence

To be filled when the feature is implemented (commits, test runs, cross-checks).

## Progress

Not started.

Commits: (none yet)
