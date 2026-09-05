# Ship plan — Sep 1

Written Aug 17. Hard deadline: **Sep 1 — complete product, demo-ready for
VCs and clients, at a stage where 1–5 clients can pay.** 14 days.
Strategy inputs: [../ideas/money-map.md](../ideas/money-map.md) ·
[../ideas/product-flow-and-redteam.md](../ideas/product-flow-and-redteam.md) ·
[../ideas/defense-and-acquisition-plan.md](../ideas/defense-and-acquisition-plan.md).

## What "payable by Sep 1" means (the definition, so we don't lie to ourselves)

Not self-serve SaaS. **Paid design-partner pilots**: "we connect your coding
agent, build 10–15 scenarios from your repo, stress-test it, hand you the
evidence report with fixes, and prove the fixes with a re-run — 2-week
engagement, **$2.5k–15k**." Research says paid pilots convert BETTER than
free ones. Product does the work; service glue where the product is thin is
acceptable and invisible to the buyer.

## The v1 product flow (what the demo shows)

```
connect agent → suite from your repo → stress (N×M sandbox trials)
      → DIAGNOSE (grouped issues, evidence) → REPORT (the artifact)
      → apply fixes → RE-RUN → before/after proof
```

## Feature ledger — have / build / cut

### Already built (do not touch except bugs)
- Task format + oracle validation + isolated verifier (Harbor-compatible)
- Sandbox execution: Docker local + Daytona cloud, multi-trial `-n`,
  concurrency, SSE live progress, dashboard UI
- Agents: claude-code, codex, mini-swe, oracle, bash-loop, LangGraph adapter
- **Diagnose Phases A+B** (rules + LLM, evidence-cited, cheat detection),
  CLI `vault diagnose`, executor wiring, 25 tests
- Vault (runs, export, usage rollups), dataset runs + leaderboard

### BUILD — the Sep 1 six (in priority order; cut from the bottom if forced)
1. **Phase C — issue grouping** (2 days): occurrences → issues across
   trials/models per task and per job: fingerprint by (task, failure_class),
   counts, affected trials, worst evidence. The heart of the report.
2. **The Report** (2 days): `tracetensor report <job|run>` → one polished,
   self-contained HTML: pass@k matrix, grouped issues with evidence steps,
   cheat findings, cost/tokens, fix recommendations. THE sales artifact —
   what pilots pay for, what VCs see, what insurance buyers get later.
3. **`--improve` loop v1** (2 days): on fail, feed verifier stderr back to
   the agent, retry ≤K; record every attempt; before/after pass rate in the
   report. Completes the "61% → 93%" demo arc. (Self-repair only; GEPA
   later.)
4. **MCP server** (2 days): expose `run_task`, `diagnose_run`, `get_report`
   over MCP. The VC wow-moment: open Claude Code, say "stress-test my
   agent," watch TraceTensor do it. Also distribution + friction defense.
5. **MediaOS evidence pack** (1 day + API spend): the real demo. 10–15
   tasks from mediaos PRs, 5 trials × 2–3 models, diagnose, improve,
   re-run. Every number in the demo is real.
6. **Packaging** (2 days): landing page on existing website/ (one-liner,
   the report as hero, pilot offer), pricing page ($99–249/mo cloud
   placeholder + "$2.5k–15k pilot" CTA), 3-minute recorded demo, one-page
   pilot PDF.

### CUT — explicitly not before Sep 1 (say no cleanly)
- Scenario auto-generation (tasks are built manually/assisted for pilots)
- Langfuse/OTel trace import (post-Sep; it's the month-2 feature)
- Record-and-replay mode (month 2 — the wedge, but not needed to demo)
- rl-rollouts export, anti-contamination pack, insurance packaging
- Self-serve billing (pilots are invoiced by hand)
- New agent adapters beyond what runs today

## The calendar

**Week 1 — build the product (Aug 18–24)**
- Mon–Tue: Phase C (grouping engine + tests)
- Wed–Thu: Report generator (HTML, self-contained, both themes)
- Fri: `--improve` v1 wired into run + report
- Sat–Sun: MediaOS evidence runs (real API: Sonnet/Opus tiers, 5 trials,
  proper step budgets) → first REAL report generated; fix what breaks

**Week 2 — make it sellable (Aug 25–31)**
- Mon–Tue: MCP server + the Claude Code live-demo path
- Wed: landing + pricing pages live; pilot one-pager PDF
- Thu: record the 3-min demo (real report, real numbers); polish dashboard
  demo route
- Fri: dry-run the pitch end-to-end; fix the top 5 rough edges
- Sat–Sun: buffer (something WILL slip); draft outreach list — 10 warm
  targets: agent teams we can name, 2–3 VCs, MediaOS as reference

**Sep 1 — ship**
- Public repo (or private + demo access — decide Aug 29 based on polish)
- Demo meetings start; pilot offer goes to first 5 prospects

## The two demos (scripted)

**Client demo (10 min):** their pain first. "This is a real agent, real
repo. Watch: 15 scenarios, 5 trials each [dashboard live]. 61% pass — and
here's WHY, with the exact steps [report: grouped issues]. One was passing
by reading the test file — you'd have shipped that. Apply these two fixes…
re-run… 93%. This report is yours. Pilot: we do this on YOUR agent in two
weeks, $X."

**VC demo (10 min):** market first (Gartner 18→60%, the graveyard, the
empty quadrant slide from ideas/learnings), then the SAME live demo, then:
"Braintrust charges $30–150k for less closed a loop; Patronus raised $50M
for the shape without the verifier. We have ground truth, the evidence
engine, and this runs self-hosted." Close with the Claude Code MCP moment.

## Pricing on the table Sep 1
- **Pilot:** $2.5k (startup) / $15k (enterprise) — 2 weeks, agent connected,
  suite built, report + fix + proof delivered
- **Cloud (placeholder tiering):** Free OSS self-host · Team $99/mo ·
  Growth $249/mo (usage-metered trials/diagnoses)
- **Ongoing after pilot:** $500–2k/mo "agent CI" retainer (suite in CI,
  monthly report)

## Risks to this plan (honest)
1. **MediaOS runs eat time** — real API runs fail in boring ways (budgets,
   images). Mitigation: start them Sat of week 1, not later; Daytona backend
   already proven.
2. **`--improve` scope creep** — K-retry with stderr feedback ONLY; anything
   smarter is month 2.
3. **Report polish is a rabbit hole** — one great template, no options.
4. **Solo-founder bandwidth** — the cut list exists so week 2 never touches
   the build list. If week 1 slips 2+ days, cut #4 (MCP) first — the demo
   survives without it; the report does not.

## Success criteria for Sep 1 (check them, don't feel them)
- [ ] `tracetensor report` produces the artifact from a real run
- [ ] MediaOS pack: ≥10 tasks, ≥5 trials × 2 models, real diagnose findings
- [ ] `--improve` shows a real before/after delta on ≥1 task
- [ ] 3-min demo recorded; live demo rehearsed twice
- [ ] Pricing + pilot offer public; invoice template ready
- [ ] 10-target outreach list with first 5 emails sent
