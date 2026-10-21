# ODD Feature: workout-planner

Status: design only, nothing implemented | Feature 12 (beyond the brief) | Source: owner request 2026-10-21

## Specs

- **S1** — The owner asked, verbatim: *"dime cuando puedo empezar a pedirle entrenamientos"*. So the goal is that the athlete can ASK for a session and get one, not that a random question gets a plausible-looking answer.
- **S2** — The owner answered, verbatim, when asked how strict the coach should be about unsourced claims: *"el que sea confiable"*. Applied here: a session is never invented. If the engine cannot decide, the coach says what is missing.
- **S3** — The owner wrote, verbatim, that today the answer is honest and negative: it must stay that way until this feature ships — *"todavía no"* — because asking for a workout today produces improvisation, one day after the coach learned to label improvised science.
- **S4** — Architectural rule of this project, unchanged: **the engine decides, the LLM explains**. The session (sport, type, zone targets, duration, the reason) is computed from persisted engine outputs; the model receives the finished decision and writes prose. Any number in the reply must match the tool output, which the existing §13 numeric-faithfulness gate already enforces at 100%.
- **S5** — Insufficient data is an explicit result naming what is missing and how to get it, never a fabricated plan (same contract as `get_readiness` and `get_load_status`).
- **S6** — Concrete gym/cycling content, when used, comes from the owner's curated library (see `odd/tasks/workout-library.md` for the loader and the parsers): the planner may REFERENCE a library workout by id and name, never invent exercises.

## Objective

Give the agent one tool, `plan_workout(sport=None, day=None)`, whose result is decided by a pure engine module from persisted data, and which the model only explains — so "¿qué entreno mañana?" is answered from the athlete's readiness, load, zones and library instead of from the model's imagination.

## Design

### Inputs (all persisted, all engine-owned)

| Input | Source | Missing means |
|---|---|---|
| Readiness verdict + suggestion | `readiness_snapshot` (Feature 5) | cannot plan a quality session |
| CTL / ATL / TSB for the day | `daily_load` (Feature 4) | cannot plan at all |
| Zone targets | the owner's configured thresholds (Feature 3) | cannot express intensity |
| Recent activity mix (last 7 days) | `activity` rows | sport balance is skipped |
| Library workouts | the WLK loader (`odd/tasks/workout-library.md`) | plan without concrete content |

### The decision table (v1, deliberately small and testable)

| Condition (in order) | Session |
|---|---|
| `readiness.suggest_reduce_intensity` is true | **recovery/endurance** — Zone 1–2 only, duration capped at 60 min |
| TSB below the engine's overreached band | **rest or mobility** — no intervals, honest reason stated |
| TSB neutral/positive AND readiness ok | **quality** — Zone 3, or the library workout whose target zone matches, for the planned sport |
| Sport share > 70% of the last 7 days | the OTHER sport is preferred, unless the athlete named one explicitly |
| readiness or load missing | `insufficient_data` naming exactly what to connect or wait for |
| thresholds unset | `insufficient_data` naming the two thresholds to set |

Order matters and is part of the contract: the first matching row wins, and each row gets its own test.

### The tool (thin, like every other tool)

`plan_workout(sport: str | None = None, day: str | None = None) -> dict` returning a typed result with `status` (`ok` | `insufficient_data` | `error`), the chosen `session` (sport, type, `zone_targets` with min/max values, `duration_s`, `kind`), the engine's `reason` (verbatim, e.g. "TSB -18 with a reduce-intensity readiness verdict"), an optional `library_workout` reference (id + name), plus `coverage`, `engine_version` and `computed_at` — the same envelope the other engine-backed tools already use. Registered in the explicit `tool_list()`; the tool's own docstring is what the model reads, so it states the contract.

### What the model receives and what it may do

It receives the finished session and explains it: what it is, why the engine chose it (quoting the reason), and what the athlete should feel. It does not negotiate, does not add sessions, does not change zones or durations.

## Tasks

| ID | Task | Specs | Route | Commit |
|---|---|---|---|---|
| T1 | `app/engine/planner.py`: pure decision function + typed result; the decision table above with one test per row and one for the ordering | S4, S5 | worker | |
| T2 | Wire the WLK loader: match a library workout to a decided session by target zone and duration; a test proving a decided session references a real library entry | S6 | worker | |
| T3 | `plan_workout` in `app/agent/tools.py` + registration in `tool_list()`; a test proving it is model-facing and returns the documented envelope | S4 | worker | |
| T4 | Prompt/tool-docstring wording that forbids inventing or altering the session | S2, S4 | inline | |
| T5 | Golden-set cases: one per decision branch plus an insufficient-data case, so the §13 gates cover it | S2, S5 | worker | |
| T6 | Feature doc + Log update as each task lands | — | inline | |

## Out of scope (v1, stated so it does not creep)

- Multi-week periodization and plan files; the planner answers one day at a time.
- Automatic delivery in the morning brief; the athlete asks.
- Load modelling beyond the existing CTL/ATL/TSB and the readiness verdict.
- New tables, new migrations, new settings, unless T1 proves one is unavoidable (then the reason is recorded in the Log).

## Log

- **L1** — Owner, verbatim: *"Seguimos? quiero empezar a comprobar si todo funciona bien antes de desplegarlo en railway o render. Levanta el tunel para twilio"*.
- **L2** — Owner, verbatim, on how strict the coach must be about unsourced claims: *"el que sea confiable"*. Recorded because it governs S2 and the whole planner: no invented sessions, no invented numbers.
- **L3** — Owner, verbatim: *"dime cuando puedo empezar a pedirle entrenamientos"*. The honest answer on 2026-10-21 was "todavía no": no tool exists, and asking would produce improvisation (see `odd/tasks/whatsapp-agent.md` WA-10 for the same discipline applied to voice).
- **L4** — Context measured the same day, relevant to T1's inputs: the live coach answered "¿cómo estoy de forma?" with CTL 8.2 / ATL 17.9 / TSB −12.2, matching `daily_load` exactly; `search_evidence` returned an irrelevant chunk while the model correctly refused to use it; and a real reply improvised "algunos estudios sugieren…" after admitting it had no source, which is why the deterministic evidence label exists. The planner inherits that discipline: the engine decides, the model labels and explains.
