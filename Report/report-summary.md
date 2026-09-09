# Computer-Use Agent — Discovery, Capability Compilation, Deterministic Replay

**Summary report**

---

## What this is

A goal and a target go in. A **discovery agent** runs an LLM loop against a live surface, perceiving it through the accessibility tree. On success, a deterministic **compiler** distils that run — dropping retries, lifting concrete values into typed parameters — into a **Capability**: a typed, versioned JSON contract. In production, a **replay engine** executes that artifact with **no model in the decision loop**. Policy is enforced in code rather than in the prompt, secrets never reach disk, and when automation cannot safely continue it hands the live session to a person and takes it back.

**Built and verified against a live browser:** the discovery loop, the Capability artifact and compiler, deterministic replay, escalation/handoff, policy and redaction.
**Designed, not built:** multi-tenant overlays; the desktop surface beyond its stub; the vision/OCR perception tier.

---

## Architecture

**The loop is six stages, not three:** `observe → decide → guard → act → settle → verify`. The two extra stages carry the design.

- **Guard** makes policy a pipeline stage rather than a prompt instruction.
- **Verify** forces the planner to declare an expectation *before* it acts, so success is a deterministic check and never an LLM judging its own work. That single constraint is what makes a discovery run mechanically convertible into a replayable artifact — every step already carries a machine-checkable success condition.

**Perception is the accessibility tree, not the DOM and not pixels (ADR-003).** A flat list of `{ref, role, accessible_name, value, state, bbox}` nodes. The model picks a snapshot-scoped ref and a typed action; it never emits coordinates and never sees HTML. Three payoffs: `role`+`name` is exactly what OS accessibility APIs expose, so desktop is a new `Surface` rather than a redesign; prompts stay small; the same agent code runs across targets. The cost is honest — a11y is incomplete on legacy markup, so a deterministic coverage check gates a vision tier that is not built (a failure is logged and the run continues).

**No agent framework (ADR-004).** A hand-rolled loop sends `goal + observation + bounded history` and forces one tool call, `propose_step`, whose JSON Schema is generated from the `Intent` pydantic model so it cannot drift from the types. One LLM call per step, never two.

**Layering (ADR-005, ADR-015).** `Surface` is the only seam to a UI technology — nothing outside `src/surface/` imports Playwright. Four modules are deliberately pure (`agent/verify.py`, `surface/coverage.py`, `policy/check.py`, `policy/redact.py`), which is why they carry the unit tests; the rest is exercised by the acceptance run. One package, synchronous, no queue, no database — every persistent thing is a reviewable file.

**Two honest deviations from the ADRs.** (1) ADR-002 named ParaBank as primary target; the local, intentionally legacy-hostile FastAPI app took over — hostile by construction, no network, no rate limits, and faults injectable on demand. Both are now load-bearing and they find different things: the full thread also runs end to end against live ParaBank with no code changed for it, and that is where five defects of *meaning* surfaced that the local app could not have found. The honest description is two targets with different jobs. (2) ADR-001/004 assumed the Anthropic SDK; the implementation calls an OpenAI-compatible `/chat/completions` endpoint over plain `httpx`, so the provider is config rather than a code path.

**Acceptance evidence:** run `20260908T235618Z-648a` (`evidence/target-app/discovery-success/`) — goal "look up member 12345 and read their savings balance", headless, `RunOutcome.COMPLETED` in 10 steps, step 8 a genuine read of `$4,812.55`. The committed capability was compiled from that same run. 276 tests, `ruff` and `pyright` clean.

---

## The Capability artifact

Canonical JSON (sorted keys) carrying `schema_version`, id/version/title/description, a `surface` with a **tenant-neutral `entry_path`** separate from `recorded_origin`, `inputs`/`outputs`, ordered `steps`, a `checkpoint`, and `provenance`.

Key commitments:

- **Targets are described, never referenced (ADR-006).** No refs, **no CSS, no XPath** — `role`, accessible name, frame scope, and `match_index`, recorded even when there was exactly one match, so a page that later grows a second control fails loudly instead of picking differently.
- **`AnchorSpec` for targets whose name is their own value.** The savings balance is a bare cell named `$4,812.55` — that is not *where* the balance is, it is *what it was*. The anchor records "one element past the one named Savings" instead.
- **Values are a discriminated union** — parameter, `CredentialRef`, or constant. An artifact can say "the password goes here" while containing no password. Stronger than redaction: a secret never written cannot leak.
- **`provenance` records run id, goal, model, timestamp and step counts — never the transcript.** An artifact is a contract, not a chat log. Step counts are review signal.
- **`schema_version` is checked on load**, so replay refuses what it does not understand rather than misinterpreting it.

**Compiling the first artifact found four defects — all in the semantic layer, none structural:** credential fields assigned by position rather than by which field was typed into; the literal password surviving in expectation prose; expectations hardcoding the recorded input; and a checkpoint asserting the recorded member's data (`text_visible: "Dolores Haze"`). The last is the general case — *an assertion capturing record data where it should capture page structure* — and is fixed in three places: `TargetSpec.anchor`, a model-labelled demotion, and a deterministic compiler rule that **a parameterised capability's checkpoint must name a parameter**, or the compile fails rather than emitting a check that passes for the wrong record.

---

## Determinism and error handling

**Replay never sees a model, and that is enforced.** `tests/test_replay_isolation.py` walks the AST of every module under `src/replay/` and fails on any import of a planner, the agent loop, or an HTTP client — and plants a violation in a temp file to prove the detector fires, because a guard that can never fail is not a guard. Each step is `resolve → perform → verify`; waiting is condition-based, never `sleep()`; the checkpoint is asserted before any `Success`.

**The result is a typed three-way union (ADR-008)**, because conflating "no such member" with "the automation broke" is the classic failure of these systems: `Success{outputs}`, `BusinessOutcome{code, detail, step}`, `HardFailure{step, expected, observed, evidence_ref, kind}`. `Recoverable` is deliberately *not* in the union — it is an internal branch with bounded retries, rolled up into the terminal result. Business-outcome detectors are ordered config rules, so a new expected outcome is a config line, not a code change.

Each maps to a distinct exit code, verified live:

| Invocation | Result | Exit |
|---|---|---|
| `--param member_id_or_name=12345` | Success — `$4,812.55` | 0 |
| `--param member_id_or_name=22841` | Success — `$918.40` | 0 |
| `--param member_id_or_name=99999` | `RECORD_NOT_FOUND` at step 4 | 2 |
| no param | HardFailure — `invalid_request` | 1 |

**Exit 2 is not an error.** The flow ran, the application answered definitively, the answer was "no such member". A caller that retried it would retry forever.

**Cross-input generalisation.** A capability recorded against member 12345 initially failed for 22841 — not because the flow was wrong but because the assertions were fitted to one record. Two problems hide in that, and they belong to different halves of the pipeline:

- **The overfitted target is a rule.** A name shaped like currency, a bare amount, a date or a long digit string is a *value*, not an identifier. The compiler detects it and records an `AnchorSpec` — possible only because a11y perception yields a flat ordered list, where position relative to a named neighbour is the one structural relationship preserved.
- **The overfitted expectation is a judgement.** Nothing syntactic separates "Dolores Haze" from a column heading. One narrow model call labels each expectation `structural` or `record_data`, and a `record_data` expectation is **demoted, not deleted** — a step with no expectation is a step replay takes on faith. A navigating step falls back to a templated URL assertion, which fails if the flow lands on the *wrong* record rather than merely on *some* page.

Three guards keep the model in the compile path from weakening anything: replay still never sees a model (the AST test is unchanged); every failure path returns `None` and the deterministic output stands; and where a rule can settle it, the rule wins. That last guard exists because **the semantic pass is measurably non-deterministic** — the same model at temperature 0 labelled the same checks differently across two runs.

The demotion ladder was also wrong first time, in a way only replay could expose: it demoted to "the control this step used is visible", a claim about the page *before* the action when expectations are asserted *after* it — a generalisation strictly worse than the overfitting it replaced.

**After both fixes, one recording answers for records it never saw:** 22841 → `$918.40`, 30017 → `$12,003.00`, 12345 → `$4,812.55`. That is the property the artifact is for. *A capability that only works for the record it was recorded with is a transcript with extra steps.*

### Bugs found by running it, not reading it

- **No-op detection was wrong for `TYPE`** — the digest came from `innerText`, which never reflects an input's value, so two successful typing steps were misread as failures. Restricted to `{NAVIGATE, CLICK}`. Found by a scripted dry-run with zero LLM calls spent.
- **`field_has_value` can never verify a password field** — Chrome's a11y tree returns eight literal bullets for an 8-character password. The first fix was structurally impossible; the second (a prompt rule) **was ignored by the model even after a corrective retry** — direct evidence for why enforcement here lives in code, not prompts. It now lives in the verifier. The cost of the bug was not two wasted steps: the false failure consumed the retry budget so the next genuine mistake dead-ended the run.
- **Rate limiting is a design input, not an accident.** Every free model was persistently 429'd across a 90-second backoff; the fix was OpenRouter's own model-routing array rather than a retry loop, gated by a typed `supports_model_routing` flag passed explicitly to the planner rather than sniffed from the base URL.
- **From building replay:** steps were selected by whether their *expectation* held rather than whether their *action* ran (this silently dropped the login submit); credential binding was inconsistent between compile and replay, so the login step failed verification against itself; and checkpoint resolution fell through to a bare success when nothing could be resolved — guessing is worse than refusing to run.

**From running the whole thread against live ParaBank** — five defects, and none of them findable on the local app, because they are failures of *meaning* rather than structure:

- **A run could satisfy its goal on screen and return nothing.** Goal: "Find the total money in all accounts". It signed in, saw `$515.50`, declared `done` with a check naming it — `completed`, exit `0`, and a compiled capability with `outputs: []`. The prompt had said `done` needs "an expectation that proves it (for example, that the value you were asked to find is visible)", and visible is not retrieved. Outputs come from `read` steps and nothing else. Every signal said success; only the empty `outputs` list gave it away.
- **The planner could not see what its own `read` steps returned.** Fixed the above, and the next run read the same total three times. The history line said `read on e42 -> ok` and never the value; refs are renumbered each observation, so the ref did not identify the row either. An agent that cannot observe the results of its own actions repeats them.
- **A `read` of a sensitive field was redacted going in and not coming out.** `Redactor` scrubbed `intent.value` and never `act.read_value` — typing a token into a field named "API key" was redacted, reading it back out wrote it to `trace.jsonl` in full.
- **A checkpoint could still quote the recorded answer, if it quoted it the other way.** The guard looked at `text_visible.text` and `field_has_value.expected` but not `element_visible.name_contains` — while `_binds_a_parameter`, ten lines away, already listed it. Two functions in one file disagreed about what counts as quoting a value.
- **And one layer down, the `read` step asserted the balance it was about to return** — vacuous *and* brittle. `generalize.apply` skipped `ActionKind.READ` outright, so the demotion ladder never looked at read steps at all.

They arrived as a chain: each fix moved the system far enough forward to expose the next thing wrong with it. That is an argument for running the whole thread repeatedly rather than once.

**The split is the argument.** Every structural bug here was one a model would have made silently; every semantic one was a mistake a model would not have made at all. Rules build the skeleton, the model labels meaning, a validator refuses ungrounded labels. That was observed, not assumed. The ParaBank five add a third category: defects where the guard existed, ran, and did not apply — invisible to unit tests, to type checking, and to reading the code.

---

## Heterogeneity and multi-tenant

`Observation`, `Action` and `TargetSpec` are surface-agnostic, so a Capability is portable to any surface that can satisfy its strategies. `WebSurface` (Playwright) is the one real implementation; `DesktopSurface` is a stub raising `NotImplementedError`, present so the seam exists in code rather than in a document. Desktop is the real payoff of a11y-first perception — only the driver changes. Canvas/Citrix would defeat this, and the documented escape hatch is a dedicated screenshot+coordinate mode rather than pretending a11y generalises.

**Multi-tenant is designed, not built (ADR-014).** No `TenantOverlay`, no `merge()`. What makes it credible is the schema: `entry_path` is already split from `recorded_origin`, and targets are described by role and name rather than anything deployment-specific. The design is a sparse overlay merged at load — overlay wins per leaf, lists add-only except `capability_overrides`, which replaces. Preferred over per-tenant recordings because **a re-record throws away the review the original capability already passed.**

**Drift:** `cua stability` does not exist, but the signal it would report on is already load-bearing — `match_index` is recorded even for a unique match, so new ambiguity fails loudly. Escalation is what makes drift survivable today: renaming a target produces `LOCATOR_UNRESOLVED`, a person takes the live session, and the run finishes.

---

## Escalation and handoff

Built and demonstrated end to end against both surfaces — `evidence/target-app/replay-escalated-handoff/` and `evidence/parabank/replay-escalated-handoff/`, the latter on a live third-party site: a replay got stuck on a renamed control, a person took the live session, clicked it, handed back, and the run completed.

**Stuck is enumerated, not "max steps hit".** Six `StuckCondition` members, each carrying its own operator guidance — the test is not "are these distinguishable" but "do they send the operator to do different things". `INVALID_REQUEST` deliberately does not escalate: waking someone for a caller bug is noise. **Two members were declared and unreachable** (`NO_PROGRESS_N_STEPS`, `AUTH_EXPIRED`) — the exact failure the enum exists to prevent. Both now have detectors, and both detect things a step-level check cannot see, because at each individual step nothing has gone wrong: no-progress compares text digests across acting steps (two in a row, not one, since a form re-serving an identical page is ordinary); auth-expired looks for a password field reappearing *after* the flow has already signed in. The mechanism is one field, `HardFailure.stuck_hint` — `kind` says what went wrong at the step, the hint says what is happening across the run.

**And auth-expired committed that same sin itself, which the ParaBank handoff caught.** "After signing in" was implemented as "after a step carrying a `CredentialRef` ran" — but typing a password is not signing in, submitting it is, so for the whole login sequence the detector was armed and watching the field the run had just filled. A replay that could not find its renamed `Log In` button told the operator the session had expired and to sign back in; the real fault was named correctly in the line directly underneath and contradicted by the headline above it. A member that sends the operator somewhere *wrong* is worse than one that does not exist, because they will go. Being signed in is now *observed* — the password field has to have gone away — rather than inferred. The unit test that should have caught it passed throughout: its "signed in" fixture was a page that still had a password box on it.

**Control is a single-writer lease, enforced not announced.** `ControlLease` is checked inside the surface's action executor, so an automated step attempted while a human holds the session raises — the difference between ceding control and intending to. "Who is in control" is a read (`cua operator status`), not an inference. Raising a real intervention showed the lease needs a **third state**: automation stops the instant it declares itself stuck, before any operator has seen the request, and the first implementation treated that placeholder as a rival holder — refusing the very operator it had just asked for.

**The human gets the same session, and it is checkable.** The run never restarts or re-navigates; it blocks in `wait_for_resume` with cookies, scroll position and half-filled form intact. The operator console reaches it over CDP as a separate process — which is the *proof*, not a convenience: an external process cannot go through the action executor the lease is blocking, so the session it acts on is necessarily the same one.

**Resume asks what the human did**, because always-retry performs a submit twice and always-skip never completes the step. `--performed` defaults to retry: repeating a completed step is visible and usually harmless; skipping one that never happened reports success having quietly missed something.

**Human actions are recorded** to `human-actions.jsonl`. Two bugs there both produced *silently empty* evidence rather than an error: the recorder was deaf because the run waited on `time.sleep` instead of through the page, and only the top document was instrumented — so the iframe control an operator is most likely to click was the one action it could not see. Passwords are dropped at the source, never scrubbed after.

**Limits:** the lease is a file with compare-then-write — right for two local processes, not a distributed lock; a CDP port is open while handoff is possible; the console has no view of the browser beyond the window itself.

---

## Safety

**Enforcement is code at the driver boundary, never a prompt instruction (ADR-010).** A jailbroken or confused model cannot exceed the allowlist because the allowlist is not something it is asked to respect. `Policy.check()` runs as the guard stage before the intent reaches the surface, and **replay runs the same check** — a capability is not exempt because no model is driving it.

Four ordered checks: origin allowlist (current page *and* a `NAVIGATE`'s destination), action-type allowlist, an **independently derived** risk tier, then a halt on `IRREVERSIBLE`. "Independently derived" is deliberate: the tier is recomputed from the action and observed element, and `Intent.risk` — a field the model fills in — is never trusted. Dispositions are block, confirm (escalate to a human), and flag.

**A real false positive.** ParaBank's password-confirmation field is named literally `Confirm:`, and the `irreversible_name_pattern` intended to catch "Confirm Transfer" matched it — classifying a routine *type* as `IRREVERSIBLE` and halting the run. Two fixes: a `confirm` disposition now escalates to an operator instead of dying, and the name-pattern check was restricted to `click`/`select`, since typing into a field is reversible no matter what the field is called and the actual commit is always a separate click. Recorded at length because it is the failure mode safety rules have: **an over-broad rule does not look unsafe, it looks like a mysteriously broken agent.**

**Redaction: the value sweep carries the weight.** Every secret the run was given is replaced in every string anywhere in the record; a field-name rule is the backstop. It is that way round because the first version was the other way round and the evidence disproved it — scrubbing `intent.value` alone left the password readable in **seven other places**, including the model quoting it back in reasoning, the declared expectation, and twice in perceived element values read straight off the DOM. Any scheme that scrubs one named field will always be a step behind. Both sinks route through the same code, `ReplayEvidenceWriter` takes a `Redactor` as a **required** argument, and redaction is copy-on-write before anything reaches disk.

**Personal data is declared, not detected.** `ParamSpec.sensitivity` is `public`/`pii`/`secret`; a `pii` parameter is masked as `***PII***` throughout the bundle — the caller's value *and* the recorded example, since step prose quotes the example. Verified: zero occurrences of either identifier, while the outputs and step sequence survive. Nothing infers sensitivity, because "is this personal data" is a question about the tenant's data, and defaulting to `pii` would mask every id in every trace — the state the evidence is least useful in.

**Limits, plainly:**

- Only credentials are redacted by default; an undeclared parameter is written in full. In the shipped evidence the member's name appears unmasked three times in the trace, because it is *read off the page* rather than passed in — masking perceived values would need a different mechanism.
- **Screenshots pass through no redactor at all.** A masking pass over the boxes perception already located is the right design and is not built.
- `sensitivity` is declared on the artifact, so a mis-declared `public` leaks and nothing catches it; `example` also stays in the artifact in plaintext.
- **Policy is enforced at two call sites, not one choke point in the surface.** The property that matters holds, but two sites must be kept in step. *An earlier draft of this document claimed the check also ran inside `surface.perform()`. It does not — `src/surface/` does not import `policy` at all.*
- Risk patterns are English-ish and target-specific; the ParaBank incident is exactly the class of bug they invite.
- Some exceptional states are injected through a documented test seam — a test affordance, not "we handled a real 503".

---

## Cuts, and what comes next

**Cut deliberately:** the operator console's chrome (the lease, transfer, event capture and resume are all real — only the UI is mocked, which the brief permits); the vision/OCR tier (its coverage gate is built and tested); multi-tenant plumbing; queues, services, horizontal scale; assisted-LLM recovery inside replay, specifically to keep the no-model invariant checkable; and a second *kind* of surface — the web seam is now exercised across two real sites, the local app and live ParaBank with no code changed for the second, so "the same agent runs unchanged against a different target" is tested rather than argued; what stays untested is the harder version, a surface that is not a browser at all.

**Next, in order:**

1. **Differential recording** — record the same goal twice with different inputs and keep only the common text, making the generalisation judgement structurally instead of with a model call whose answers I have measured varying between runs.
2. **Move policy enforcement into the surface**, one boundary instead of two. The control lease is already enforced there, which makes the asymmetry hard to justify.
3. **`TenantOverlay` and `merge()`** — the smallest piece of real multi-tenant work, and the one the schema is already shaped for.
4. **Locator canonicalisation** (`/item/12345` → `/item/:id`) and automatic overlay suggestions from the stability report.
5. **A draft → approved gate** before unattended replay executes a risky step.

**In hindsight:** I treated model reliability as an environmental nuisance for too long. Rate limits, prompt-following failures, and the password-masking discovery are not incidental — they are the operating conditions. The loop's retry/replan recovery, not model quality, is what produced a completed run, and I would design for that from the first hour rather than the last.
