# Computer-Use Agent — Discovery, Capability Compilation, Deterministic Replay

**Detailed report**

---

## 0. Scope: built vs. designed

**Built and verified against a live browser (not only unit tests):**

- The **discovery loop** — an LLM driving a live web UI through a guarded `observe → decide → guard → act → settle → verify` cycle, with safety enforced in code and redacted evidence written per step.
- The **Capability artifact** and the deterministic **compiler** that distils a successful run into one.
- **Deterministic replay** — executes a capability with no model anywhere in its decision loop and returns a typed three-way result.

**Designed but not built:**

- Multi-tenant overlays (`TenantOverlay`, `artifact.merge()`).
- The desktop surface beyond its stub.

ADRs are summarised inline where they appear. The full ADR set, the module contracts, and the requirement-by-requirement acceptance mapping are internal working documents and are not part of this repository.

I have tried to be explicit throughout about which is which, because a design document that blurs *built* and *intended* is worth very little. Where an earlier draft of this document claimed something that turned out not to be true of the code, I have said so at that point rather than quietly correcting it.

---

## 1. Architecture

### 1.1 The through-line

A goal and a target go in. A discovery agent runs an LLM loop against a live surface, perceiving it through the accessibility tree. On success, a recorder distils that run — dropping retries, lifting concrete values into typed parameters — into a **Capability**: a typed, versioned JSON contract. In production, a replay engine executes that artifact with **no model in the decision loop**. Throughout, a policy layer enforces an allowlist and risk classification *in code* rather than in the prompt, and a redaction layer keeps secrets out of every persisted byte. When the automation cannot safely continue, it hands the live session to a person and takes it back afterwards.

That whole line is built and runs end to end. The multi-tenant overlay (§4) and the desktop surface (§4) are the parts that remain seams.

### 1.2 The loop is six stages, not three

`observe → decide → guard → act → settle → verify`

The two extra stages are the interesting ones:

- **Guard** sits between decide and act so that policy is a *pipeline stage* rather than a prompt instruction (§6).
- **Verify** exists because the planner must declare an expectation *before* it acts, and success is then a deterministic check of that expectation — never an LLM judging its own work.

That single constraint is what makes a discovery run mechanically convertible into a replayable artifact later: each step already carries a machine-checkable success condition.

### 1.3 Perception is the accessibility tree — not the DOM, not pixels (ADR-003)

Perception emits a flat list of interactive nodes — `{ref, role, accessible_name, value, state, bbox}` — plus page metadata. The model selects a snapshot-scoped `ref` and a typed action; it never emits coordinates and never sees raw HTML.

Three reasons:

1. `role` + `name` is exactly the model that OS accessibility APIs expose, so desktop becomes a new `Surface` rather than a redesign (§4).
2. Prompts stay small and stable compared to DOM dumps.
3. The same agent code runs unchanged across targets.

The cost is honest: the a11y tree is incomplete on legacy markup, so perception synthesises names from nearby labels, and a deterministic coverage check gates a vision tier. **A coverage failure is logged and the run continues; the vision tier itself is not built.**

### 1.4 A hand-rolled tool-call loop, no agent framework (ADR-004)

The planner sends `goal + observation + bounded history` and constrains the model to a single **forced tool call**, `propose_step`, whose JSON Schema is generated directly from the `Intent` pydantic model via `model_json_schema()` rather than hand-written — so the schema cannot drift from the types. **One LLM call per step, never two.**

The trade: I implement parse/repair myself (one corrective retry on a `ValidationError`, then a hard `PlannerError`), and I get full control of the transcript → artifact boundary, which is the whole point of the exercise.

### 1.5 Layering

`Surface` is the single seam to a UI technology (ADR-005): **nothing outside `src/surface/` imports Playwright.** `agent`, `policy` and `evidence` know only the protocol.

Four modules — `agent/verify.py`, `surface/coverage.py`, `policy/check.py`, `policy/redact.py` — are deliberately **pure** (no I/O, no browser), which is why they are the modules carrying unit tests. `loop.py`, `planner.py`, `evidence.py` and `cli.py` are exercised by the acceptance run instead.

One installable package, synchronous execution, no queue and no database (ADR-015): **every persistent thing is a reviewable file.**

### 1.6 Target choice, and where it moved

ADR-002 named **ParaBank** as the primary target. In practice the acceptance flow is driven against a local, intentionally legacy-hostile FastAPI app (`target_app/`): tables instead of semantic lists, no test IDs, results rendered inside an iframe, and injectable faults.

It was built for the perception work and turned out to be the better target — hostile *by construction* rather than by accident, needs no network, and cannot rate-limit me.

**Both targets are now load-bearing, and they find different things.** ParaBank is not a smoke-test any more: the full thread — discovery, compilation, deterministic replay, drift, escalation, handoff, completion — runs against the live site with no code changed for it (`evidence/parabank/`). No ParaBank branch, no site-specific selector, no adapter; only the origin is allowlisted, because policy is enforced in code rather than assumed. The local app remains where the error paths live, because faults can be injected there on demand and a third-party demo cannot be asked to return a 503.

What the split bought is in §3.7: every structural defect was found against the local app, and five defects of *meaning* were found only against ParaBank, because only a goal the system had not been shaped around could expose them. §6 also records a real safety bug ParaBank surfaced, re-verified against the live page. ADR-002 named ParaBank primary and the local app took over; the honest description now is two targets with different jobs, and I would rather record that than quietly rewrite the ADR to have been right.

**Reconciling ADR-002 formally is unfinished, and I would rather say so than quietly rewrite the ADR.**

### 1.7 One deviation worth naming

ADR-001/004 assumed the Anthropic SDK. The implementation calls an **OpenAI-compatible `/chat/completions` endpoint over plain `httpx`** instead — OpenAI directly, or OpenRouter as a gateway — selected by which key is configured. The request shape is identical either way, so **the provider is config rather than a code path**.

The one thing that is not portable is OpenRouter's model-routing extension (§3.6), which the config layer gates on the selected provider rather than letting the planner guess.

### 1.8 Acceptance evidence

Run **`20260908T235618Z-648a`**, kept as `evidence/target-app/discovery-success/`.

| | |
|---|---|
| Goal | "look up member 12345 and read their savings balance" |
| Mode | Headless, default config-driven model, no code path bypassed |
| Outcome | `RunOutcome.COMPLETED` in **10 steps** |
| Step 8 | A genuine read — `read_value == "$4,812.55"` |
| Step 9 | `done` |
| Bundle | `trace.jsonl`, `run.json`, one screenshot per step |

That run is also what the committed capability was compiled from, so **the artifact and the evidence describe the same run** rather than two different ones.

**The same thread, against a live third-party site.** The whole line was then run end to end against `parabank.parasoft.com`, a public demo bank nobody here controls, with **no code changed for it**. Goal: *"Find the total money in all accounts"*.

| Stage | Bundle | Result |
|---|---|---|
| Discovery | `evidence/parabank/discovery-success/` | Signed in and read the total in 5 steps |
| Replay, no model | `evidence/parabank/replay-success/` | `total_account_balance = '$515.50'` — exit `0` |
| Error path | `evidence/parabank/replay-target-not-found/` | `target_not_found`, naming the control — exit `1` |
| Escalation | `evidence/parabank/replay-escalated-handoff/` | Stuck → person fixed it in the live session → completed — exit `0` |

The drift is `artifacts/read_total_across_accounts_drifted.json`, a hand-edited twin with one control renamed (`Log In` → `Sign In`), standing in for a vendor relabelling a button between releases.

`pytest` (276 tests), `ruff` and `pyright` are clean.

---

## 2. Artifact schema

**Built** — `src/artifact/` (`models.py`, `compiler.py`, `store.py`). The compiled artifact for the acceptance run is committed at `artifacts/look_up_member_read.json`, and everything below can be read off that file.

### 2.1 Shape

A **Capability** is a pydantic model serialised as canonical JSON (sorted keys, trailing newline), carrying:

- `schema_version`, `id`, `version`, `title`, `description`
- `surface` — kind plus a **tenant-neutral `entry_path`**, with `recorded_origin` kept as a separate field
- `inputs: [ParamSpec]`, `outputs: [OutputSpec]`
- ordered `steps`
- a `checkpoint`
- `provenance`

### 2.2 Design commitments behind those fields

**Every step carries a human intent, a typed `Action`, a `TargetSpec`, and an `expectation`.** The expectation is not decoration — it is what replay asserts, and it exists in the artifact only because the discovery loop already forced the planner to declare it before acting (§1.2).

**Targets are described, never referenced (ADR-006).** A trace step points at `e7`, a ref meaningful only inside the snapshot that produced it. A `TargetSpec` instead carries `role`, accessible name, frame scope, and `match_index` — which match to take when a page has three links called "Open Record". `match_index` is recorded **even when there was exactly one match**, so a page that later grows a second one fails loudly rather than silently picking differently.

> There is no CSS and no XPath anywhere in this schema, deliberately: the whole system perceives through the accessibility tree, and a selector strategy that reached past it would be the one part of the pipeline a legacy DOM could break.

**A target whose name is its own value carries an `AnchorSpec` instead.** The savings balance is a bare table cell, so the accessibility tree reports it as `StaticText` named `$4,812.55` — which is not *where* the balance is, it is *what the balance was*. The anchor records the structural fact instead: **one element past the one named "Savings"**. Replay prefers it over the recorded name, because the name is the part known to be wrong. Still no CSS: a flat ordered list is what a11y perception gives, and position relative to a named neighbour is the relationship it keeps.

**A step's value is a parameter, a credential reference, or a constant** — a discriminated union, so an artifact can be read without guessing whether a string is a secret, an input, or a fixed literal. `CredentialRef` is what lets an artifact say "the password goes here" while containing no password; replay resolves it from configuration at run time. **This is a stronger property than the redaction it replaced — a secret that was never written cannot leak from a file, whatever the redactor does.**

**`provenance` records the run id, goal, model, timestamp and step counts — never the raw transcript.** An artifact is a contract, not a chat log; keeping the transcript would both bloat it and reintroduce the leakage problem redaction just solved. The step counts are review signal: a capability compiled from a run that needed six retries deserves a closer look than one that went straight through.

**`schema_version` is checked on load (`store.load`)**, so replay refuses an artifact it does not understand rather than misinterpreting one. Canonical serialisation means two compilations of the same trace are byte-identical, so a real change shows up in review as a real diff rather than as key reordering.

**Tenant specialisation is a separate sparse `TenantOverlay` merged at load, never an edit to the base** (§4).

### 2.3 What generating the first artifact actually found

The schema was not validated by inspection — it was validated by compiling one and reading it. That found four defects, and **all four were in the semantic layer rather than the structural one**.

1. **`username` and `password` were mislabelled.** The compiler assigned credential fields by *position* rather than by which field the value was actually typed into.
2. **Credentials leaked into expectation prose.** The step value was a clean `CredentialRef`, but the expectation's `description` and `expected` strings still carried the literal password — redaction had been scoped to one field rather than swept across the record.
3. **Expectations hardcoded the recorded input**, so a step's assertion could only ever pass for the member that happened to be recorded.
4. **The checkpoint asserted the recorded member's data.** It read `text_visible: "Dolores Haze"` — a checkpoint that proves the flow reached *member 12345's* record, not that it reached whichever record was requested. Demoting it to `text_visible: "Savings"` removed the overfitting and left the deeper problem: that assertion is true on *every* member's page, so a run that asked for 22841 and arrived at 12345 still reported success, with the wrong balance. Found by making it happen — escalate mid-flow, land the operator on the wrong record, resume.

### 2.4 Fixes

Defects 1–3 are fixed.

Defect 4 is the general case of the same mistake — *an assertion that captures record data where it should capture page structure* — and is fixed in three places:

- `TargetSpec.anchor` for a control whose accessible name is its own value.
- A model-labelled **demotion** for an expectation that quotes the recorded record.
- A **compiler rule** that a parameterised capability's checkpoint must name a parameter.

That last one is deterministic, not a judgement: if the capability takes an input and the checkpoint binds none, the checkpoint becomes `url_contains: {{member_id_or_name}}` — verified against the URL the recording actually ended on — and **if no parameter can be shown to identify the record, the compile fails** rather than emitting a check that passes for the wrong one.

The same falsification now returns `checkpoint_failed` (`evidence/target-app/replay-wrong-record-checkpoint-failed/`), while the control run that lands on the requested record still succeeds.

**The honest cost:** distillation is real work — parameterisation, output inference, checkpoint selection — and it was, as predicted, the part that needed the most iteration.

---

## 3. Determinism & error handling

### 3.1 Replay determinism — built (`src/replay/`, ADR-009)

`replay` has no import path to a model, and that is **enforced rather than merely intended**: `tests/test_replay_isolation.py` walks the AST of every module under `src/replay/` and fails if any of them imports a planner, the agent loop, or an HTTP client. It also **plants a violation in a temp file and asserts the detector sees it**, because a guard that can never fail is not a guard.

Each step is an explicit `resolve(TargetSpec) → perform(Action) → verify(expectation)`. Waiting is condition-based, never `sleep()`. The checkpoint is asserted before any `Success` is returned. No implicit waits and no "probably worked".

### 3.2 The result taxonomy is a typed union (ADR-008)

Conflating "no such member" with "the automation broke" is the classic failure of this kind of system.

- **`Success{outputs}`** — checkpoint verified, declared outputs extracted.
- **`BusinessOutcome{code, detail, step}`** — a legitimate answer. `RECORD_NOT_FOUND` is *data*, and the caller gets a clean typed value.
- **`HardFailure{step, expected, observed, evidence_ref, kind}`** — stop, with debug context by construction. `FailureKind` names seven distinct things to go and fix, from `TARGET_NOT_FOUND` to `POLICY_HALTED`.

**`Recoverable` is deliberately not in that union**: it is an internal branch handled inside replay with bounded retries and rolled up into the terminal result's `recoveries`.

Business-outcome detectors are **ordered rules from config, first match wins**, so a new expected outcome is a config line rather than a code change — the phrasing of "not found" is a property of the target system, and a second tenant running the same vendor app may word it differently.

### 3.3 Verified against the live app

Each result maps to a distinct **process exit code**, so a caller can branch without parsing text.

| Invocation | Result | Exit |
|---|---|---|
| `cua replay look_up_member_read --param member_id_or_name=12345` | Success — `savings_account_current_balance = '$4,812.55'` | 0 |
| `cua replay look_up_member_read --param member_id_or_name=22841` | Success — `savings_account_current_balance = '$918.40'` | 0 |
| `cua replay look_up_member_read --param member_id_or_name=99999` | BusinessOutcome — `RECORD_NOT_FOUND` at step 4 | 2 |
| `cua replay look_up_member_read` (no param) | HardFailure — `invalid_request` | 1 |

The third row is the one worth pausing on: **exit 2 is not an error code.** The flow ran correctly, the application answered definitively, and the answer was "no such member". A caller that retried that would retry forever.

### 3.4 Cross-input generalisation

The artifact is fitted to the record it was recorded with, and that is fixed in two places.

When the discovery loop repairs a wrong expectation mid-run, the repaired version is written from what the model could see *at that moment* — which includes the recorded member's name and balance. A capability compiled from that run replays for member 12345 and fails for 22841, not because the flow is wrong but because the assertions were fitted to one record.

Before the fix:

```
replay --param member_id_or_name=22841
  -> hard failure | expectation_failed at step 5
     expected : text 'Dolores Haze'
     observed : ... 'Marcus Whitfield' ...
```

The flow had worked. It had reached exactly the right record. **The assertion was wrong.**

Two different problems hide in that, and they belong to different halves of the pipeline.

**The overfitted target is a rule.** Step 6 reads the savings balance, and the balance is a bare table cell — the a11y tree reports it as `StaticText` whose accessible name *is* the number, because there is nothing else to call it. That is decidable by looking at it: a name shaped like currency, a bare amount, a date or a long digit string is a *value*, not an identifier. So the compiler detects it and records an `AnchorSpec` instead — the element one place after the one named "Savings", taken from the snapshot the run actually captured. Replay prefers the anchor over the recorded name. This is the **label-anchored strategy**, and it is possible only because a11y perception yields a flat ordered list: position relative to a named neighbour is the one structural relationship it preserves. There is still no CSS and no XPath.

**The overfitted expectation is a judgement.** "Is *Dolores Haze* page furniture or a datum" has no syntactic signature — a currency pattern catches `$4,812.55`, but nothing separates a person's name from a column heading except knowing what names look like. So **one narrow model call** labels each expectation `structural` or `record_data`, and a `record_data` expectation is **demoted rather than deleted**: a step with no expectation is a step replay takes on faith, which is the thing the declare-before-acting design exists to prevent. A navigating step falls back to a templated URL assertion — `{{member_id_or_name}}` — which is stronger than it looks, because it fails if the flow lands on the *wrong* record rather than merely on *some* page.

That split was not a preference. It is where the defects landed: **every structural bug in this project was one a model would have made silently, and every semantic one was a mistake a model would not have made at all** (§2.3, §3.7). Rules build the skeleton; the model labels meaning; a validator refuses any label naming a step the capability does not contain.

### 3.5 Three properties keep the model-in-compile-path from weakening the system

1. **Replay still never sees a model.** This runs once, at compile time. The AST test is unchanged and still passes.
2. **The artifact cannot get worse.** Every failure path — no key, no network, a malformed response, an ungrounded label — returns `None` and the deterministic compiler's output stands.
3. **Where a rule can settle it, the rule wins.** A check whose expected value is `{{param}}` or a credential placeholder is already input-following and cannot quote the recorded record, so it is never demoted regardless of the label.

That last guard was added because **the semantic pass is not deterministic, and I measured it**: run twice against the same artifact, the same model at temperature 0 called the credential and parameter checks `structural` once and `record_data` the next time. That is the honest cost of putting a model in the compile path, and the mitigation is not to trust it further than the artifact can check.

**The demotion ladder itself was wrong first time, and replaying it is what showed that.** It demoted a step to "the control this step used is visible" — a claim about the page *before* the action, when an expectation is asserted *after* it. Step 5 clicks "Open Record", and afterwards the session is on the member page where no such link exists, so the generalised expectation failed for **every** input including the recorded one. A generalisation strictly worse than the overfitting it replaced, and invisible to anything short of running it.

### 3.6 Result: one recording, three records

After both fixes, the same capability — recorded once, against member 12345 — returns the right answer for members it never saw:

| `--param member_id_or_name=` | Result |
|---|---|
| 22841 | success — `savings_account_current_balance = '$918.40'` |
| 30017 | success — `savings_account_current_balance = '$12,003.00'` |
| 12345 | success — `savings_account_current_balance = '$4,812.55'` |

That is the property the artifact is for. **A capability that only works for the record it was recorded with is a transcript with extra steps.**

### 3.7 Bugs found by running the thing, not by reading it

I record these because they are the ones that changed the design.

**From the discovery loop:**

1. **No-op detection was wrong for `TYPE`.** The loop compared a `text_digest` built from `document.body.innerText` to detect an action that did nothing — but typing into an `<input>` changes the element's *value*, which `innerText` never reflects. Two successful back-to-back `TYPE` steps shared a digest and were misread as failures. Fixed by restricting no-op detection to `{NAVIGATE, CLICK}` — the actions whose effect should appear in rendered text. Found by a scripted dry-run against the real app with **zero LLM calls spent**.

2. **`field_has_value` can never verify a password field.** Confirmed directly rather than assumed: perceiving the field after typing an 8-character password returns **eight literal U+2022 bullets** — Chrome's accessibility tree reports the mask, not the value. Any model asking `field_has_value` to prove a password was typed is asking an unanswerable question and will fail every time.
   - The *first* fix was itself broken: it told the model to "write your expectation about the next action instead", which is structurally impossible when one tool call proposes exactly one action and one expectation.
   - The *second* fix — a prompt rule forbidding it — **was ignored by the model even after a corrective retry**, which is the more instructive result: it is direct evidence for why enforcement in this system lives in code rather than in prompts.
   - It now lives in the verifier, which recognises that a credential field has no observable post-condition by design and accepts on the action having succeeded.
   - The cost of getting this wrong was not two wasted steps: the false failure consumed the retry and replan budget, so the *next* genuine mistake dead-ended the run. **A run that previously dead-ended now completes.**

3. **Rate limiting is a design input, not an accident.** Every free model tried was persistently 429'd, including across a deliberate 90-second backoff; OpenRouter's `/key` endpoint confirmed the account was not the thing being throttled, and a later sweep produced an explicit account-level free-models-per-day cap. The fix was to use **OpenRouter's own model-routing** — a `"models": [primary, ...fallbacks]` array on the same request, verified with raw `httpx` before touching the planner — rather than writing a retry loop. Because a fallback can answer, `Decision.model` records the **response's** model, not the configured primary. The routing array is an OpenRouter extension (OpenAI returns 400 for unrecognised body parameters), so `LlmSettings.supports_model_routing` decides whether to send it, and the planner takes that as an **explicit constructor argument** rather than sniffing the base URL. A portability seam is worth more as a typed decision than as a string comparison buried in the request builder.

**From building replay — which is what compiling an artifact and then executing it exposed:**

4. **Steps were selected by whether their *expectation* held, not by whether their *action* ran.** The compiler's job is to keep the path that worked, and it was deciding "worked" by looking at verification. So a step whose action succeeded but whose expectation was wrong got dropped — which silently removed **the login submit** from the capability. The distinction between "the action did what it said" and "the assertion about it was correct" is load-bearing, and collapsing the two produced an artifact that looked complete and was not.

5. **Credential binding was inconsistent between compile and replay.** The artifact stores a `CredentialRef` and a placeholder in the expectation; replay bound the credential into the *action* but compared the *expectation* against the unbound placeholder, so the login step failed verification against itself.

6. **Checkpoint resolution fell through to a bare success** when no checkpoint could be resolved — the same class of mistake as skipping the schema-version check: **guessing is worse than refusing to run.**

**From running the whole thread against ParaBank — a surface the local app could not stand in for:**

7. **A run could satisfy its goal on screen and return nothing.** Goal: *"Find the total money in all accounts"*. The run signed in, saw `$515.50` on the overview, and declared `done` with a `text_visible` check naming it — four steps, `outcome: completed`, exit `0`. The compiled capability had `outputs: []`. The planner was obeying the prompt exactly: it said `done` needs "an expectation that proves it (for example, that the value you were asked to find is visible)", and **visible is not retrieved**. Outputs are derived from `read` steps and from nothing else, so a goal answered by a glance compiles to a capability that answers nobody. The prompt now says a value the goal asks for is `read` first and `done` second. What makes this the worst of the five is that every signal said success: the summary said "completed", the exit code was `0`, and only the empty `outputs` list gave it away.

8. **The planner could not see what its own `read` steps returned.** Fixed 7, and the next run read the same total three times, compiling one number into two differently-named outputs. The history line handed back to the planner was `step 3: read on e42 -> ok` — action, ref, outcome, and never the value. From the planner's side the read had produced nothing, so reading again was the reasonable move; refs are renumbered every observation, so `e42` did not identify the row either. **An agent that cannot observe the results of its own actions will repeat them.** The history line now carries the read value and what was read, taken from the *redacted* record — history persists for the rest of the run, which is what turns a value the model saw once into one it is shown repeatedly.

9. **A `read` of a sensitive field was redacted going in and not coming out.** Found while fixing 8. `Redactor` scrubbed `intent.value` — what was typed *into* a field whose name matches `(?i)password|token|...` — and never `act.read_value`, what a `read` took *out* of one. Typing a token into a field named "API key" was redacted; reading it back out wrote it to `trace.jsonl` in full. §6 argues the value sweep carries the weight precisely because name-scoped rules are always a step behind; this was that argument holding against its own author.

10. **A checkpoint could still quote the recorded answer, if it quoted it the other way.** `_resolve_checkpoint` exists to stop a checkpoint asserting the specific value the run happened to read (§2.3, defect 4). It looked for that literal in `text_visible.text` and `field_has_value.expected` only — so a planner ending with `element_visible {role: StaticText, name_contains: "$515.50"}` sailed past it, and the capability asserted a specific balance on a public demo site whose figures move. Ten lines away, `_binds_a_parameter` already listed `name_contains` among the attributes holding a literal. **Two functions in one file disagreed about what counts as quoting a value**, and the one on the checkpoint path was the one that was wrong.

11. **And one layer down, the `read` step still asserted the balance it was about to return.** Fixing 10 made the checkpoint structural and left step 3 saying `element_visible {name_contains: "$515.50"}`. Replay verifies every step, not only the checkpoint, so the capability was still one balance change away from failing — and the assertion was vacuous besides, checking the number was present immediately before reading it. The cause was a single clause: `generalize.apply` skipped `ActionKind.READ` outright, so the demotion ladder never looked at read steps at all. It never needed to be a judgement — "this expectation quotes what this step's own read returned" is two recorded strings compared — so the rule now lives in the deterministic compiler, where it runs with no key and cannot be lost to a semantic pass that fails. It demotes to the anchor's own claim: the target is already located as *one past the element named `Total`*, so asserting `Total` is on the page is the structural fact the step was already relying on.

Bugs 4–6 are all **structural bookkeeping**, and each is a mistake a language model would have made silently. Defects 1–3 in §2.3 are all **semantic judgement**, and each is a mistake a language model would not have made at all. That split is the whole argument for where the model sits in the pipeline, and it was **observed rather than assumed**.

Bugs 7–11 add a third category, and it is the one I would most want to be judged on. All five are invisible to unit tests, to type checking, and to reading the code — 7 and 8 because the system **reported success**, 9 through 11 because the guard that should have caught them **existed, ran, and did not apply**. Each needed a real goal on a real site to surface. The local app could not have found any of them: it was built to be structurally hostile, and these are failures of *meaning* — "completed" that answered nothing, a history that omitted the one fact the reader needed, a rule scoped to one direction of travel, a whole action kind excluded from a pass by a clause nobody had revisited.

They also came in a **chain**, which is the part I would not have predicted: 7 was only visible once the goal demanded a value, 8 only once 7 was fixed, 9 only while fixing 8, and 11 only once 10 was fixed. Each fix moved the system far enough forward to expose the next thing wrong with it. That is an argument for running the whole thread repeatedly rather than once, and it is why the ParaBank capability in `artifacts/` was recompiled from a fresh run after the last fix rather than kept from the run that found it.

**One smaller correctness note, since it was nearly invisible:** the OpenRouter URL is built with an explicit f-string rather than `httpx.Client(base_url=...)`. Path-joining there follows RFC 3986 relative-merge rules, which would have silently dropped `/api/v1` from the configured base URL.

---

## 4. Heterogeneity & multi-tenant

### 4.1 Surface is the only seam to a UI technology (ADR-005)

`Observation`, `Action` and `TargetSpec` are surface-agnostic, so a Capability recorded against one surface is portable to any surface that can satisfy its strategies.

`WebSurface` (Playwright) is the single real implementation. `DesktopSurface` exists as a **stub raising `NotImplementedError`** — present specifically so the seam is a thing in the code rather than a claim in a document.

- **Legacy web** is a new Surface plus the same a11y-first perception. `target_app/` is the evidence that the perception model survives hostile markup: tables rather than semantic lists, no test IDs, and search results rendered inside an **iframe** (which is why `WebSurface` recurses into frames when building the tree).
- **Desktop** is a Surface over OS accessibility APIs. This is the real payoff of ADR-003: `role`+`name` is already the model those APIs expose, so the agent, the artifact, and the locator strategies do not change — only the driver does.
- **Canvas / Citrix** targets would defeat this, and the documented escape hatch is a dedicated **screenshot+coordinate Surface mode** rather than pretending the a11y approach generalises.

### 4.2 Multi-tenant is base + sparse overlay, not per-tenant recordings (ADR-014)

**This half is designed, not built** — there is no `TenantOverlay` and no `artifact.merge()` in the code. The brief is explicit that it does not expect multi-tenant support to be implemented, only that the abstractions do not preclude it.

What exists that makes it credible is **the schema**: `SurfaceRef` already splits `entry_path` from `recorded_origin`, so a capability is not welded to a host, and `TargetSpec` describes controls by role and accessible name rather than by anything deployment-specific.

**The design:** the base Capability stays tenant-neutral; a `TenantOverlay` is a sparse patch — allowlist deltas, extra recoverable conditions, and `capability_overrides[capability@version][step]` replacing a `TargetSpec` or a parameter default — merged at load. Merge semantics would be pinned deliberately: **overlay wins per leaf; lists add-only except `capability_overrides`, which replaces.**

The reason to prefer this over per-tenant recordings: **a re-record throws away the review that the original capability already passed.**

### 4.3 Drift detection is designed, and one piece of it is already load-bearing

`cua stability` does not exist. What does exist is the fact it would report on: **`TargetSpec.match_index` is recorded even when there was exactly one match**, so a page that later grows a second control with the same accessible name fails loudly instead of silently picking a different one. That is the ambiguity signal, already enforced per replay rather than aggregated across runs.

**The escalation path is what makes drift survivable today, and the evidence shows it:** renaming one target in a capability produces `LOCATOR_UNRESOLVED`, a person is handed the live session with the page and the missing target named, and the run finishes. The permanent fix for a flagged step would be an overlay entry rather than a re-record. There would be **no automatic overlay generation** — that is manual, informed by the report, and I would rather scope it honestly than claim it.

---

## 5. Escalation & handoff

**Built** — `src/escalation/`, `src/session/`, the operator verbs in `src/cli.py`, and `src/surface/web_handoff.py`. Demonstrated end to end in `evidence/target-app/replay-escalated-handoff/`: a replay got stuck, a person took the live session, fixed it by hand, handed it back, and the run completed.

Three pieces matter more than the UI.

### 5.1 Stuck is enumerated, not "max steps hit"

`StuckCondition` has six members — `LOCATOR_UNRESOLVED`, `CHECKPOINT_FAILED_TERMINAL`, `UNKNOWN_DIALOG`, `POLICY_BLOCK_NEEDS_HUMAN`, `NO_PROGRESS_N_STEPS`, `AUTH_EXPIRED`.

The test for whether the enum earns its size is not "are these distinguishable" but **"do they send the operator to do different things"**. Each carries the guidance for its own case, so the intervention tells a person what to do rather than making them decode an enum name.

A failure that no operator could act on **does not escalate at all**: `INVALID_REQUEST` is a caller bug, and waking someone to look at a missing parameter is noise.

**Two of those six were declared and unreachable**, which is the failure this enum exists to prevent. `NO_PROGRESS_N_STEPS` and `AUTH_EXPIRED` were raised nowhere: a member nothing can produce sends nobody anywhere, and an argument that the enum earns its size does not survive two of its members being decoration. Both now have detectors, and both are the kind a step-level check cannot make — **at each individual step nothing has gone wrong.**

- **No progress** compares the page's text digest across each acting step and counts how many in a row changed nothing. A click that no longer does anything, followed by an expectation loose enough to hold on the unchanged page, passes; the run walks on going nowhere. **Two in a row, not one**, because a form that re-serves an identical page is ordinary and the condition is about a *pattern*.
- **Auth expired** looks for a password field reappearing *after* the flow has already signed in. Narrow on purpose: a password box is unremarkable during login and means one thing after it. Demonstrated end to end rather than only unit-tested — a capability that signs in, clicks "Sign Out" mid-flow, then carries on reports `expectation_failed` at the step and escalates as `auth_expired`, where it would previously have escalated as `UNKNOWN_DIALOG` and sent the operator hunting for a dialog that does not exist.

**And auth-expired committed that same sin itself, which the ParaBank handoff caught.** "After the flow has signed in" was implemented as "after a step carrying a `CredentialRef` has run" — but typing a password is not signing in, submitting it is. So for the whole login sequence the detector was armed and watching the very field the run had just filled. The drifted ParaBank capability cannot resolve its renamed `Log In` button, escalates from the login page, and the operator was told:

> The session appears to have been signed out: a password field is on screen again partway through the flow. Sign back in, return to where the flow was, and resume.

Nothing had signed in. The real fault — a renamed button — was named correctly in the `why` line directly underneath, and contradicted by the headline above it. This section argues the enum earns its size because each member sends the operator somewhere different; **a member that sends them somewhere *wrong* is worse than one that does not exist, because they will go.** Being signed in is now *observed* rather than inferred: the password field has to have gone away at least once, which is what the event actually consists of and needs nothing from the capability to detect. The same handoff now escalates as `locator_unresolved` and tells the operator to look for the control under a different name, which is what they then did.

The unit test that should have caught this was passing throughout, and reading why is worth more than the fix: its "signed in" fixture was a page that **still had a password box on it**. The assertion was right, the world it asserted against was not, and no amount of running that test would have said so.

The mechanism is one field, **`HardFailure.stuck_hint`**. `kind` says what went wrong *at the step*; the hint says what is going on *across the run*, and when the engine has one it wins. Neither condition is a `FailureKind` of its own, because the step-level failure really is an expectation that did not hold — the hint is *why*.

### 5.2 Control is a single-writer lease, enforced rather than announced

`ControlLease {owner, holder_id, since, reason}` is checked **inside the surface's action executor** — the boundary every automated action passes through — so an automation step attempted while a human holds the session raises. That is the difference between *ceding* control and *intending to*.

"Who is in control" is a **read, not an inference**: `cua operator status <id>` is that read and nothing else. A second operator claiming a held session gets the 409 equivalent.

**Raising an intervention taught me that the lease needs a third state, not two.** Automation has to stop the instant it declares itself stuck — *before* any operator has seen the request — so there is a real window where the lease is owned by `HUMAN` and claimed by nobody. The first implementation treated that placeholder as a rival holder, and **the first real operator to run `take` was refused by the intervention that had just asked for them.**

### 5.3 The human gets the same session, and that is checkable

The run never restarts, relaunches or re-navigates the browser across the handoff; it blocks in `wait_for_resume` with the page exactly as it was — cookies, scroll position, half-filled form included.

The operator console — a CLI, mocked on purpose, which the brief permits — reaches that window over **CDP as a separate process**. That detail is the *proof* rather than an implementation convenience: an external process cannot go through this run's action executor, which is precisely what the lease is blocking, so the session it acts on is necessarily the same one.

### 5.4 Resume asks what the human did, because the obvious design is wrong

A run that *always retries* the stuck step performs it twice when the operator did it by hand — for a submit or a transfer that is not cosmetic. A run that *always skips* never completes a step for the operator who merely dismissed a dialog. **Only the human knows which.**

`cua operator resume <id>` takes `--performed`, **defaulting to retry**: repeating a completed step is visible and usually harmless, whereas skipping one that never happened produces a run that reports success having quietly missed something.

In the recorded evidence the operator clicked the renamed control, resumed with `--performed`, and the run skipped step 5 and finished with the right balance.

### 5.5 What the human did is recorded

Into `human-actions.jsonl` in the intervention directory — DOM events through an exposed binding, plus main-frame navigations. Two bugs there are worth recording because **both produced silently empty evidence rather than an error**:

1. **The recorder was deaf while it mattered most.** Playwright's sync API dispatches events only while the caller is inside it, and the run was waiting on `time.sleep`. The bundle came back with no human actions from a session in which a human had demonstrably clicked something. The wait now goes through the page so the event loop keeps turning.
2. **Only the top document was instrumented.** The target app serves its search results in an iframe, so the control an operator is most likely to click during a drift incident lives in a child frame — **the one action the recorder existed to capture was the one it could not see.**

**Passwords are dropped at the source rather than scrubbed afterwards:** a password field records *that* it was filled and never *with what*. The redactor still sweeps as a backstop, because an operator can type a secret into a field this process has no way to recognise as sensitive.

### 5.6 Honest limits

- The poll loop is a real cost, bounded by a timeout that **fails loudly rather than hanging**.
- The lease is a file with a compare-then-write — right for two local processes, **not a distributed lock**.
- A CDP port is open only while a handoff is possible: a deliberate narrowing, but still an open port.
- The operator console has no view of the browser except the window itself — acceptable at this scope, and the thing I would build next.

---

## 6. Safety

### 6.1 Enforcement is code at the driver boundary, never a prompt instruction (ADR-010)

This is the single most important safety property here: **a jailbroken or confused model still cannot exceed the allowlist, because the allowlist is not something it is asked to respect.**

`Policy.check()` runs as the **guard** stage between decide and act, before the intent ever reaches the surface. **Replay runs the same check on its own step path**, so a capability is not exempt from policy merely because no model is driving it.

`Policy.check()` runs four ordered checks:

1. **Origin allowlist** — both the current page and a `NAVIGATE`'s destination.
2. **Action-type allowlist.**
3. **An independently derived risk tier.**
4. **A halt on `IRREVERSIBLE`.**

"Independently derived" is deliberate: the tier is recomputed from the action and the observed element, and **`Intent.risk` — a field the model fills in — is never trusted.**

Dispositions: **block** (stop), **confirm** (stop and escalate `POLICY_BLOCK_NEEDS_HUMAN`), **flag** (proceed, mark the trace).

### 6.2 A real false positive, found by running against ParaBank

ParaBank's registration form has a password-confirmation field whose accessible name is literally **"Confirm:"** — perception still reports `textbox 'Confirm:'` there, re-checked on 2026-09-09, and the test now uses that exact string rather than a paraphrase of it. `irreversible_name_pattern` in config includes the bare word `confirm`, intended to catch a button like "Confirm Transfer". `_derive_tier` applied that pattern to **any** action's target, so a routine *type* into that field was classified `IRREVERSIBLE` and would have halted the run outright.

At the time there was no recovery path; there is now — a `confirm` disposition raises `POLICY_BLOCK_NEEDS_HUMAN` and hands the session to an operator (§5), which is the correct destination for a risk decision *even when the rule is right*. But escalating a false positive still wastes a person's attention, so **the rule was fixed too**, by restricting the name-pattern check to `click`/`select` — the action kinds that can actually commit something — on the reasoning that typing into a field is reversible no matter what the field is called, and the actual commit is always a separate click that this still catches. A regression test pins it.

I record this at length because it is the failure mode safety rules have: **an over-broad rule does not look unsafe, it looks like a mysteriously broken agent.**

### 6.3 Redaction has two mechanisms, and the one that carries the weight is the value sweep

`Redactor` (`src/policy/redact.py`) scrubs:

- **By value** — every secret the run was given is replaced in every string anywhere in the record.
- **By field name** — a step that typed into a field matching `(?i)password|passcode|pin|ssn|...` has its value replaced without needing to know it.

The name rule is the backstop; **the value sweep is what is relied on.**

It is that way round because the first version was the other way round, and the evidence disproved it. Scrubbing `intent.value` alone left the same password readable in **seven other places**: the model quoting it back in reasoning, its own `expectation.description`, the structured `check.expected`, the same expectation echoed under `verify`, the rendered `check_performed`, and **twice** in `state_before.elements[]` where perception had read it straight back off the DOM.

> A secret spreads into model prose, declared expectations and perceived element values; any scheme that scrubs one named field will always be a step behind it.

Both sinks route through the same code — `EvidenceWriter` for discovery, `ReplayEvidenceWriter` for replay, which **takes a `Redactor` as a required argument** so no caller can construct one that writes raw values by accident. Redaction is **copy-on-write and runs before anything reaches disk**, so the in-memory trace keeps the real values and the persisted copy never had them.

Passwords typed by a human during a handoff are dropped at the source rather than scrubbed: `web_handoff.py` records that a password field was filled and never what with. The sweep can only remove secrets it was told about, and an operator may type one this process has never seen.

### 6.4 Personal data is declared, not detected

`ParamSpec.sensitivity` is `public` (default), `pii` or `secret`. A `pii` parameter has its value masked as `***PII***` throughout the replay bundle — **the caller's value and the recorded example both**, because the planner's step prose quotes the example ("Type the member ID '12345'...").

Verified end to end: a replay of a capability with `sensitivity: pii` produces a bundle with **zero occurrences of either identifier**, while `savings_account_current_balance = '$918.40'` and the whole step sequence survive — the run stays auditable without the bundle carrying the identifier around.

**Nothing infers sensitivity.** "Is this string personal data" is a question about the tenant's data rather than about the flow, and defaulting it to `pii` would mask every member id in every trace, which is the state the evidence is least useful in. The shipped capability declares `public` for that reason: the member ids are fixture data.

### 6.5 Limits, stated plainly

- **Only credentials are redacted by default.** `sensitivity` exists and is honoured, but it defaults to `public` and nothing infers it, so an undeclared parameter is written in full. In the shipped evidence the member's name reaches the trace unmasked (`grep -c 'Dolores Haze' evidence/target-app/discovery-success/trace.jsonl` → 3), because it is *read off the record page* rather than passed in as a parameter — a value the capability never declared cannot be masked by declaring it. Masking values *perceived* rather than *supplied* would need a different mechanism than this one.
- **Screenshots pass through no redactor at all.** Every PNG in an evidence bundle is the page as rendered, names and balances included. A masking pass over the image — blanking the boxes perception already located — is the right design and is not built.
- **`sensitivity` is declared on the artifact**, so a mis-declared `public` leaks and nothing catches it. Declaring a parameter sensitive also does not retroactively sanitise the capability file: `example` stays in the artifact in plaintext.
- **Policy is enforced at two call sites** — the discovery loop's guard stage and replay's step path — rather than at one choke point inside the surface. The property that matters holds (enforcement is code, not prompt, and runs before any action executes), but a single boundary would be harder to bypass by accident than two that must be kept in step. *An earlier draft of this document asserted the check also ran inside `surface.perform()`. It does not, and `src/surface/` does not import `policy` at all.* I would move both call sites into the surface before adding a third caller.
- **Risk rule patterns are English-ish and target-specific.** They live in config and are tested, and the ParaBank incident above is exactly the class of bug they invite.
- **Some exceptional states are injected through a documented driver test seam** rather than triggered naturally. That is a test affordance, not production behaviour, and I would not want it read as "we handled a real 503".
- **Credentials are supplied to the planner** as "credentials available for this target" and redacted from every persisted trace. They are **never placed in the goal string.**

---

## 7. Cuts

### 7.1 Deliberately not built, with the reasoning

- **The operator console's chrome.** The handoff mechanism is built and demonstrated (§5); what is mocked is the UI. An operator sees the browser window itself and a CLI, not a co-browsing view. The brief permits this explicitly, and the distinction I care about is that **the lease, the transfer, the event capture and the resume are all real**.
- **The vision/OCR perception tier.** The coverage check that gates it is built, pure, and unit-tested; the tier itself is not. A coverage failure is logged and the run continues.
- **Multi-tenant plumbing** — one overlay design and `merge()` semantics, no tenant registry or database. The brief explicitly does not reward prematurely building that infrastructure.
- **Queues, services, horizontal scale.** Single package, synchronous, files on disk.
- **Assisted-LLM recovery inside replay.** Kept out specifically to keep the "no model in the replay decision loop" invariant clean and checkable.
- **A second *kind* of surface.** `DesktopSurface` is a stub and proves the seam *structurally* only. The web seam is now exercised across two real sites — the local app and live ParaBank, with no code changed for the second — so the claim "the same agent runs unchanged against a different target" is tested rather than argued. What remains untested is the harder version of it: a surface that is not a browser at all.

### 7.2 What I would build next, in order

1. **Differential recording.** Cross-input generalisation works (§3), but its semantic half rests on one model call whose answers I have measured varying between runs. Recording the same goal twice with different inputs and keeping only the text common to both would make the same judgement **structurally, without a model** — a better answer than the one shipped, and the first thing I would do with another day.
2. **Moving policy enforcement into the surface**, so there is one boundary instead of two. The control lease is already enforced there, which makes the asymmetry harder to justify.
3. **`TenantOverlay` and `merge()`** (§4) — the smallest piece of real multi-tenant work and the one the schema is already shaped for.
4. **Locator canonicalisation** (`/item/12345` → `/item/:id`) and **automatic overlay suggestions** from the stability report — both slot into existing seams rather than requiring new ones.
5. **A draft → approved gate** before any unattended replay is allowed to execute a risky step.

### 7.3 The one thing I would change about the approach, in hindsight

I treated model reliability as an environmental nuisance for too long. Rate limits, model-specific prompt-following failures, and the password-masking discovery are not incidental — **they are the operating conditions**. The loop's retry/replan recovery, not model quality, is what produced a completed run, and I would design for that from the first hour rather than the last.
