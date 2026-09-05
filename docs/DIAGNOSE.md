# Diagnose — failure-finding stage (design)

Status: **Phases A and B implemented.** Phase C (grouping + quality gate)
remains a proposal.

| Piece | Where |
|---|---|
| Rules engine | `services/diagnose.py` (taxonomy lives in its `SEVERITY_ORDER`) |
| LLM extractor | `services/diagnose_llm.py` (injectable `llm_fn`, never raises) |
| Server wiring | `services/executor.py` — rules inline before the trial insert (free); LLM tier after `trial_done`, off the sandbox semaphore; both emit a `diagnose` SSE event |
| Config | `DIAGNOSE_ENABLED` (default on), `DIAGNOSE_LLM_ENABLED` (default off — it spends money), `DIAGNOSE_MODEL` |
| CLI | `tracetensor vault diagnose [run] [--max-steps N] [--llm] [--write]` |
| Tests | `tests/test_diagnose.py` (rules + extractor with a fake model) |

## The problem

A trial today ends with `passed` / `reward` and nothing else. The *why* lives
in the trajectory JSON, and extracting it is manual archaeology: open
`result.json`, read every step, infer "the agent spent all 12 steps grepping
and never edited a file." That analysis is exactly what an eval platform
should produce — and (for `passed=True` trials) it's also the only way to
catch an agent that passed by cheating, which is a task-quality problem for
any Harbor listing.

Prior art: Lemma (docs.uselemma.ai) builds its whole product on inferring
failures from production traces *without* ground truth. We have it easier —
the verifier already labels every trial. Diagnose is **classification**
("why did this fail?"), not detection ("did this fail?").

## The pipeline change (one new stage, async)

```
Register → Run → Score → trial recorded ──(async)──► Diagnose ──► Issues
                              │                          │
                              └ returns as fast          └ occurrences per trial,
                                as today                   grouped per job/task
```

- `trial_runner.run_trial` is **untouched**. Trials complete and persist
  exactly as now; diagnosis never gates or slows a run.
- A background pass (same pattern as the existing job worker /
  `WORKER_EMBEDDED` loop) picks up completed trials and writes a
  `failure_analysis` block into `Trial.trajectory` (JSONB —
  `Trajectory.model_config extra="allow"` already tolerates it; **no
  migration needed for phase 1**).
- Progress surfaces over the existing `event_bus` → SSE stream as a
  `diagnose` event, mirroring how trial phases stream today.

## Failure taxonomy (the checklist comes first)

Fixed enum, one label per occurrence. Deterministic where possible; the LLM
is only for what rules can't see.

| `failure_class` | Detected by | Signal |
|---|---|---|
| `infra_failure` | rule | `Trial.error` set / build or network-switch failure (already distinct in `trial_runner`) |
| `step_budget_exhausted` | rule | agent steps == `max_steps` and no `DONE`; without a known `max_steps`, inferred from call/step accounting (every LLM call produced an executed command ⇒ no DONE arrived) and worded as "likely" |
| `time_budget_exhausted` | rule | `agent_error` records a session timeout (added during Phase A — a timeout is a budget, not infra) |
| `never_edited` | rule | no write-ish command (`>`/`>>`/`sed -i`/`tee`/editor/patch/`cp` into workdir) in any agent step |
| `network_blocked` | rule | agent stderr matches resolver/connect failures while `network_mode=no-network` |
| `verifier_tamper_attempt` | rule | agent step touches `/tests` or `reward.*` paths (extends `guardrails` categories) |
| `read_test_file` | rule | agent step `cat`/`grep`s `tests/` content — **also fires on passed trials** (cheat signal) |
| `loop_repetition` | rule | ≥3 consecutive identical/near-identical commands |
| `cost_blowout` | rule | `llm_usage_summary` exceeds a task/job budget threshold |
| `gave_up_early` | LLM | agent said DONE with work visibly unfinished |
| `wrong_target` | LLM | edits landed in the wrong file/function for the instruction |
| `misread_task` | LLM | solution addresses a different problem than `instruction.md` |
| `env_assumption` | LLM | assumed tools/paths not present in the image |
| `other` | LLM | fallback; rationale required |

Rules run always (free). The LLM call runs only when rules found nothing
conclusive **or** the trial failed with reward < 1.0 — i.e., at most one model
call per trial, usually zero for infra failures and clear-cut rule hits.

## Data model

### Per trial: `trajectory.failure_analysis` (JSONB, additive)

```json
{
  "status": "completed",            // running | completed | skipped | error
  "engine": "rules+llm",            // rules | rules+llm
  "diagnosed_at": "2026-08-17T…Z",
  "judge_model": "openai/gpt-4.1-mini",
  "cost_usd": 0.0012,               // null when unknown — cost.py rules apply
  "occurrences": [
    {
      "failure_class": "step_budget_exhausted",
      "title": "Ran out of steps while still exploring",
      "rationale": "All 12 agent steps are grep/sed reads; no edit commands.",
      "evidence_steps": [0, 11],    // indices into trajectory.steps
      "detector": "rule"            // rule | llm
    }
  ]
}
```

Design rules carried over from the codebase:
- `cost_usd: null` means unknown, never 0 (see `services/cost.py`).
- Evidence must point at concrete step indices — an occurrence with no
  evidence is dropped, same spirit as `guardrails` recording the command.
- The block is a **record**, not a mutable object; re-diagnosing replaces it
  wholesale (no field-level merge — same delivery rule the trajectory has).

### Per job/task: issues (phase 2, derived — no new table initially)

Occurrences group by `(task_id, failure_class)` fingerprint at read time:
"`step_budget_exhausted` in 4/5 trials across haiku + sonnet-5." Served by
vault/leaderboard queries first; a persisted `issues` table with lifecycle
(open/resolved) only if read-time grouping proves too slow or we need
cross-run history.

## The LLM extractor

A sibling of `llm_judge.judge()` — same shape, different prompt:

- **Input:** `instruction.md` (capped like `MAX_RUBRIC_CHARS`), the trial's
  reward + verifier stderr tail, and a compacted step transcript
  (command + exit_code + truncated output; caps like `MAX_OUTPUT_CHARS`).
- **Output (JSON only):** list of `{failure_class, title, rationale,
  evidence_steps}`, `failure_class` constrained to the enum.
- Cost captured per call like the judge does; prompt-injection posture same
  as the judge (agent output is untrusted content inside the prompt).
- Default model: cheap tier (`DEFAULT_JUDGE_MODEL` class); configurable via
  `DIAGNOSE_MODEL` env, off entirely via `DIAGNOSE_ENABLED=false`.

## Surfaces

| Surface | Change |
|---|---|
| CLI `tracetensor run` output | one line per failed trial: `why: step_budget_exhausted — ran out of steps while exploring` (upgrades `trial_hints.failure_hint_from_result`, which stays as the zero-cost fallback) |
| `result.json` / vault export | `failure_analysis` block rides along inside `trajectory` — no format change |
| `GET /examine/job/{id}` | trials include the block automatically (JSONB passthrough) |
| Leaderboard / dataset summary | phase 2: failure-mode counts per (agent, model) column |
| Passed-but-flagged | phase 2: surfaced as a task-quality warning in vault + Harbor listing checks |

## Phasing

1. **Phase A — rules only, inline read path.** Deterministic checks as a pure
   function over a trial dict (usable on stored `runs/*/result.json` too —
   retro-diagnosis of the existing 20 runs is the acceptance test). CLI line
   + `failure_analysis` block. No LLM, no worker, no migration.
2. **Phase B — LLM extractor, async.** Background pass after trial
   completion; SSE `diagnose` events; `DIAGNOSE_*` config.
3. **Phase C — grouping + quality gate.** Read-time issue rollups in vault
   and leaderboard; passed-but-flagged warnings wired into dataset/Harbor
   listing checks.

## Non-goals (deliberately not copying Lemma)

- No ingest SDK/contract — we own the runner; the trajectory shape is
  enforced at the source.
- No threads/conversations — trials-within-a-job already group.
- No server-side pricing tables — `cost.py`'s decision stands; extractor cost
  is vendor-reported (our own API call), which is the one case we do record.
- No lanes/validation/ticketing lifecycle — revisit only if issues get a
  persisted table and human triage.

## Open questions

1. Should `read_test_file` fire on *all* trials or only `passed=True`?
   (Reading tests is legitimate strategy in some tasks; proposal: always
   record, only *warn* when passed.)
2. Where does the Phase B worker live — inside the existing embedded worker
   loop, or a separate poller keyed on trials missing `failure_analysis`?
3. Retro-diagnosis CLI: `tracetensor vault diagnose <run>` vs automatic on
   `vault show`? (Proposal: explicit command; diagnosis costs money when the
   LLM is on.)
