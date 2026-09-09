# cua — Computer-Use Automation

An LLM discovers how to accomplish a natural-language goal on a live web UI once, through a
guarded six-stage loop — **observe → decide → guard → act → settle → verify**. Perception is
the accessibility tree (not screenshots, not raw DOM); every action passes a deterministic
safety policy before it runs; every step is written to disk as redacted evidence.

A successful run is then distilled into a **typed, versioned Capability artifact** and
**replayed deterministically with no model in the loop**. When a replay gets stuck, it hands the
**same live session** to a human, who takes single-writer control, fixes it, and hands it back.

> **Status: the whole thread runs end to end** — goal → real LLM-driven discovery run → compiled
> capability → deterministic replay with typed outputs and a three-way outcome contract →
> human escalation on the live session. `uv run pytest` (276 tests), `ruff check .` and
> `pyright` are clean. Evidence for every stage is in [`evidence/`](evidence/), including a
> replay that hits a business outcome, one that hard-fails, and one that escalates to a person
> and completes. The same thread also runs end to end against **live ParaBank**, a public
> third-party site, with no code changed for it — see [`evidence/parabank/`](evidence/parabank/).
>
> Designed but deliberately not built: multi-tenant overlays and the desktop surface beyond its
> stub — see [`REPORT.md`](REPORT.md) §4 and §7 for what was cut and why.

## Setup

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Codezilla-dev/cua_web_agent.git && cd cua_web_agent

uv sync --extra dev           # creates .venv, installs runtime + dev deps
uv run playwright install chromium

cp .env.example .env          # see the note below: which layers need which parts
```

Config lives in [`config/default.yaml`](config/default.yaml) (allowlist, risk policy,
redaction pattern, run budget, perception coverage thresholds). Secrets and machine-specific
values (the LLM key, target credentials, headed/headless) come only from `.env`
(gitignored) — see `.env.example` for every variable.

**What each layer actually needs**, because "only for the LLM run" was not true:

| | LLM key | target credentials |
|---|---|---|
| Layers 1–3 (static gates, target app, the loop with a stub planner) | no | no |
| Layer 4 (a real LLM-driven discovery run) | **yes** | **yes** |
| Layers 5–6 (replay, escalation, the operator console) | no | **yes** |

Replay needing no key is a property worth checking rather than assuming: `src/replay/` cannot
import a model client, and a test walks the whole import graph to prove it. `load_settings` used
to raise without an API key, which meant `cua replay` would not start on a machine that had no
business having one — the refusal now lives in `require_llm`, which discovery calls and replay
does not.

**LLM provider (Layer 4 only).** The planner speaks OpenAI-compatible `/chat/completions`, so
it runs against **OpenAI** (`OPENAI_API_KEY` + `OPENAI_MODEL`) or **OpenRouter**
(`OPENROUTER_API_KEY` + `OPENROUTER_MODEL`). Set one; `LLM_PROVIDER` breaks the tie if both
are present. The model id is deliberately required rather than defaulted — a hardcoded slug
goes stale silently when the provider retires it, and then a run dies several steps in with a
404 that says nothing about configuration. OpenRouter additionally supports
`OPENROUTER_FALLBACK_MODELS` (its own model-routing feature: try the primary, then each
fallback, on the same request — added because a single free-tier model proved too
rate-limited to reliably complete a run; see [`REPORT.md`](REPORT.md) §3). That is an
OpenRouter extension and is never sent to OpenAI, which rejects unrecognised parameters
with a 400.

## Running it

Seven layers, cheapest first. **Layers 1–3 need no API key and cost nothing**; only Layers 4
and 7 call a model. Each layer exercises strictly more of the system than the one above it,
ending with the whole thread against a live third-party site.

### Layer 1 — static gates (~10s, no browser, no network)

```bash
uv run pytest          # 276 passed — compiler, replay engine, locator chain, artifact store,
                       #              verifier, policy, redaction, lease, handoff
uv run ruff check .    # clean
uv run pyright         # 0 errors
```

### Layer 2 — the target app on its own

```bash
uv run python -m target_app     # serves http://localhost:5000; login operator / vault-echo-77-quill
```

The path a run has to navigate: login → `/search` (results load **inside an iframe**) →
Open Record → `/member/12345`, where the savings balance (`$4,812.55`) is a readable element
a `read` action extracts. Open it in a browser to see what the agent is up against.

### Layer 3 — the whole loop, zero LLM calls

The real "is it actually built?" check. Both drive the real Playwright browser against the
real target app; neither needs `.env` or a key.

```bash
uv run python scripts/smoke_test.py     # config + target_app + WebSurface: login → perceive → read
uv run python scripts/loop_dry_run.py   # the real six-stage loop, with a hand-scripted planner
```

`loop_dry_run.py` is the one that exercises everything. Its stub planner feeds a deliberately
wrong `target_ref` on the first click, so the run goes through the **full failure path**
(verify fails → retry → replan) before finishing the happy path with a genuine `read` and a
complete `evidence/<run_id>/` bundle. Pass `--start-target-app` to let it manage the app
itself, `--headed` to watch the browser. `smoke_test.py` also runs Layer 1 first unless you
pass `--skip-static`.

### Layer 4 — a real LLM-driven run

Needs `.env` with one provider configured and target credentials set (see **LLM provider**
above). This is the only layer that needs a key. Two terminals:

```bash
# terminal 1
uv run python -m target_app

# terminal 2 — first, sanity-check the model wiring with a single call:
uv run python scripts/planner_live_check.py --start-target-app

# then the real run:
uv run python -m src.cli discover \
  --goal "look up member 12345 and read their savings balance" \
  --target http://localhost:5000 --headless --max-seconds 480
```

`discover` prints `outcome: completed | steps: N | run_id: ... | evidence: evidence/<run_id>`.
That directory holds `trace.jsonl` (one redacted `StepRecord` per line), `run.json` (the run
summary), and a screenshot per step. Flags: `--max-steps`, `--max-seconds`,
`--headed`/`--headless`, `--model` (override the configured model id for one run);
`uv run python -m src.cli discover --help` for the full list.

A successful run also **compiles a capability** into `artifacts/<id>.json`. That file is the
thing the next section replays.

### Layer 5 — the demo path: discover, then replay

This is the end-to-end thread. Terminal 1 runs the target app throughout
(`uv run python -m target_app`).

```bash
# 1. discover — one real LLM run, writes evidence/ and compiles artifacts/look_up_member_read.json
uv run python -m src.cli discover   --goal "look up member 12345 and read their savings balance"   --target http://localhost:5000 --headless --max-seconds 480

# 2. replay it deterministically, with a different input than the one recorded.
#    No LLM is involved: src/replay/ cannot import one, and a test enforces that.
uv run python -m src.cli replay look_up_member_read --param member_id_or_name=12345
#    -> success | savings_account_current_balance = '$4,812.55'                        exit 0

# 3. the same capability against a member who does not exist
uv run python -m src.cli replay look_up_member_read --param member_id_or_name=99999
#    -> business outcome | RECORD_NOT_FOUND at step 4                          exit 2

# 4. and with a required input missing
uv run python -m src.cli replay look_up_member_read
#    -> hard failure | invalid_request                                         exit 1
```

Three outcomes, three exit codes. Step 3 is the one worth pausing on: exit `2` is not an error.
The flow ran correctly and the application gave a definitive answer, so a caller gets a typed
`BusinessOutcome` with a stable `code` rather than an exception to retry forever.

Each replay writes `evidence/<run_id>/` with a `trace.jsonl` and a `result.json`.

### Layer 6 — escalation: a human takes the live session

When a replay hits something it cannot fix, it hands the browser window to a person. To see it,
use the deliberately drifted capability shipped for this purpose — it is the real one with a
single target renamed, as if a vendor had relabelled a control:

```bash
# terminal 2 — this pauses mid-flow and waits for an operator
uv run python -m src.cli replay look_up_member_read_drifted   --param member_id_or_name=12345 --escalate

# terminal 3 — see what is waiting, and take control of the live session
uv run python -m src.cli operator list
uv run python -m src.cli operator take <intervention-id>

#   now fix it. Either click "Open Record" yourself in the browser window that is
#   already open, or let the scripted stand-in do it over CDP:
uv run python scripts/operator_hands.py --click "Open Record"

#   then hand control back, saying you performed the step rather than merely
#   unblocking it, so the run moves on instead of repeating it:
uv run python -m src.cli operator resume <intervention-id> --performed
```

The run continues from where it paused and completes with the right balance. While the operator
holds the lease, automation is refused at the surface — `operator status <id>` says who has
control, and a second operator gets a conflict rather than silently taking over. Everything the
human did lands in `interventions/<id>/human-actions.jsonl`.

A recorded run of exactly this is in [`evidence/target-app/replay-escalated-handoff/`](evidence/).

### Layer 7 — the same thread against a live third-party site

Everything above drives `target_app/`, which is deliberately hostile but is still an app written
here. Layer 7 is the same thread against **ParaBank**, a public demo bank nobody here controls —
no local server, real network, markup written by strangers. **No code changes for it**: no
ParaBank branch, no site-specific selector, no adapter. Only the origin is allowlisted, in
`config/default.yaml`, because policy is enforced in code rather than assumed.

```bash
$env:TARGET_URL      = "https://parabank.parasoft.com/parabank/index.htm?ConnType=JDBC"
$env:TARGET_USERNAME = "dummy"
$env:TARGET_PASSWORD = "123"

# 1. discover — a real LLM run against the live site
uv run python -m src.cli discover `
  --goal "Find the total money in all accounts" `
  --target $env:TARGET_URL --headed --id read_total_across_accounts --max-steps 25 --max-seconds 480
#    -> outcome: completed | steps: 5 | artifact: artifacts/read_total_across_accounts.json

# 2. replay it deterministically — no model anywhere in the decision loop
uv run python -m src.cli replay read_total_across_accounts --headed
#    -> success | total_account_balance = '$515.50'                             exit 0

# 3. the drifted twin, with nobody available to fix it
uv run python -m src.cli replay read_total_across_accounts_drifted --headless
#    -> hard failure | target_not_found at step 2
#       expected : button named 'Sign In' in main (match #0)                   exit 1

# 4. the same drift, with a person available
uv run python -m src.cli replay read_total_across_accounts_drifted --escalate
#    -> escalation: locator_unresolved at step 2. waiting for an operator...
```

Then, in another terminal, the same operator verbs as Layer 6 — `operator take`, click the
control under its real name, `operator resume --performed` — and the run completes with
`total_account_balance = '$515.50'`, exit `0`.

`artifacts/read_total_across_accounts_drifted.json` is a hand-edited copy of the recorded capability with one
control renamed (`Log In` → `Sign In`), standing in for a vendor relabelling a button between
releases. The two files are otherwise identical, so the pair isolates drift from every other
reason a replay can fail.

Recorded runs of all four steps are in [`evidence/parabank/`](evidence/parabank/). Note the
credentials above are ParaBank's own public demo account, not a secret — and the password still
never reaches disk: the trace records `***REDACTED***` and the capability stores a
`CredentialRef`, never a value.

## Repo layout

| Path | |
|---|---|
| `src/` | the package (importable as `python -m src.cli`) — full module table below |
| `target_app/` | the local legacy-hostile FastAPI target, where the injectable error paths live |
| `config/default.yaml` | allowlist, risk policy, redaction, run budget, perception thresholds |
| `artifacts/*.json` | compiled capabilities — the reusable, reviewable contract |
| `evidence/` | curated bundles: the full thread against the local app, and again against live ParaBank |
| `interventions/<id>/` | a live handoff: request, lease, human actions, resolution (git-ignored) |
| `tests/` | unit tests for the pure modules (coverage check, verifier, policy, redaction) |
| `scripts/` | tooling, none of it required for `discover` itself: `smoke_test` and `loop_dry_run` (Layer 3), `planner_live_check` (one model call), `operator_hands` (the scripted stand-in for a human during a handoff), and `explore_ax_tree` — which dumps a live page's accessibility tree, and is what you point at a new site before asking whether the perception model can see it |
| `REPORT.md` | the design write-up: architecture, safety model, trade-offs, cuts — the deliverable, with the brief's seven headings |
| `Report/report-summary.md` | the same ground in ~150 lines, for a first read |
| `Report/report-detailed.md` | the same ground with numbered subsections, for looking one thing up |

### Modules

| Path | What |
|---|---|
| `src/types.py` | every model that crosses a stage boundary; `extra="forbid"` on all of them |
| `src/surface/base.py` | the `Surface` / `PerceptionProvider` / `ActionExecutor` / `SettleDetector` protocols |
| `src/surface/web.py` | Playwright implementation (CDP a11y tree + frame recursion) |
| `src/surface/desktop.py` | `NotImplementedError` stub proving the seam |
| `src/surface/coverage.py` | pure deterministic perception-coverage check |
| `src/agent/loop.py` | the six-stage loop, budgets, bounded recovery |
| `src/agent/planner.py` | one LLM call per step, strict tool schema, OpenAI/OpenRouter |
| `src/agent/verify.py` | pure deterministic expectation checking |
| `src/policy/check.py` | `Policy.check` — origin/action allowlist + independently-derived risk tier |
| `src/policy/redact.py` | `Redactor.redact_step` — copy-on-write scrub before a step reaches disk |
| `src/artifact/models.py` | the `Capability` schema — typed steps, inputs, outputs, checkpoint, provenance |
| `src/artifact/compiler.py` | deterministic trace → `Capability`; no model involved |
| `src/artifact/store.py` | canonical JSON, schema-version checked on load |
| `src/artifact/semantics.py` | one narrow model call: which expectations quote the recorded record |
| `src/artifact/generalize.py` | demotes those to assertions that hold for any input |
| `src/replay/engine.py` | `resolve → perform → verify` per step, checkpoint asserted before success |
| `src/replay/outcomes.py` | `Success` / `BusinessOutcome` / `HardFailure` — the result contract |
| `src/replay/escalate.py` | which failures a person can fix, and the handoff around them |
| `src/replay/evidence.py` | `ReplayEvidenceWriter` — `trace.jsonl` / `result.json`, redacted on write |
| `src/session/lease.py` | `ControlLease` — single-writer control, enforced at the surface |
| `src/escalation/conditions.py` | `StuckCondition` + the operator guidance each one implies |
| `src/escalation/intervention.py` | the intervention directory, and `wait_for_resume` |
| `src/surface/web_handoff.py` | records what the human did while they held the session |
| `src/evidence.py` | `EvidenceWriter` — `trace.jsonl` / `run.json` / per-step PNGs, redacted on write |
| `src/config.py` | typed load of `config/default.yaml` + env |
| `src/cli.py` | `discover`, `replay`, and the `operator` verbs |

### Invariants

- Nothing outside `src/surface/` imports Playwright.
- **`src/replay/` cannot reach a model.** `tests/test_replay_isolation.py` walks the AST of every
  module there and fails on an import of a planner, the loop, or an HTTP client — and proves its
  own detector catches a planted violation.
- An automated action cannot run while a human holds the control lease; the check is inside the
  surface's action executor, not in a caller.
- `agent/verify.py`, `surface/coverage.py`, `policy/check.py` and `policy/redact.py` are **pure**
  — no I/O, no browser — which is why they are the four modules carrying unit tests. `loop.py`,
  `planner.py`, `evidence.py` and `cli.py` are exercised by the acceptance run instead.
- Redaction runs before anything is written to disk.
- Runtime handles live on fields marked `Field(exclude=True)`; traces stay pure data.
- The planner never receives raw HTML, a nested tree, or a prior `UIState`. History is compacted
  to one line per step. One LLM call per step, never two.
