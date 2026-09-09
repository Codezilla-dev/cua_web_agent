# Evidence

The full thread, twice, against two different surfaces: a goal → a real LLM-driven run that
completed it → the capability compiled from that run → deterministic replays of it, each ending
in a different one of the three result classes → one that got stuck and needed a person → and one
where the person put the session on the wrong record and the checkpoint caught it.

Two directories, one per surface, with the same bundle names inside each — so the pairing is
visible in the layout rather than only claimed in prose:

    evidence/
      target-app/   discovery-success, replay-success, replay-record-not-found,
                    replay-invalid-request, replay-escalated-handoff,
                    replay-wrong-record-checkpoint-failed
      parabank/     discovery-success, replay-success, replay-target-not-found,
                    replay-escalated-handoff

Raw run directories are named `<timestamp>-<id>` and stay local; the bundles here are named
and were selected deliberately. Nothing here was hand-edited. The compiled capabilities are
**not** copied in here — they live once, in [`artifacts/`](../artifacts/), because a second copy
of a file is a second thing to keep in step.

## Against the local target app

`target_app/` is hostile by construction — tables instead of semantic lists, no test IDs, results
in an iframe, and injectable faults. It is where the error paths are reproducible on demand.

Capability: [`artifacts/look_up_member_read.json`](../artifacts/look_up_member_read.json),
drifted twin [`..._drifted.json`](../artifacts/look_up_member_read_drifted.json).

| `target-app/` | What it shows |
|---|---|
| `discovery-success/` | The genuine LLM run the capability was compiled from. |
| `replay-success/` | Deterministic replay against member **22841** — a record the capability was never recorded with. Exit `0`. |
| `replay-record-not-found/` | Replay against a member that does not exist. A **business outcome**, not a failure. Exit `2`. |
| `replay-invalid-request/` | Replay with a required parameter missing. A **hard failure**. Exit `1`. |
| `replay-escalated-handoff/` | A replay that got stuck, handed the live session to a human, and finished after they unblocked it. |
| `replay-wrong-record-checkpoint-failed/` | The same handoff with the operator landing on the **wrong member**. The checkpoint rejects it. Exit `1`. |

## Against ParaBank, a live third-party site

`https://parabank.parasoft.com` is a public demo bank nobody here controls: real network, real
latency, markup written by strangers. The same thread runs against it with **no code changed for
it** — no ParaBank branch, no site-specific selector, no adapter. That is the claim the `Surface`
abstraction and a11y-first perception are making, and this is where it is tested rather than
asserted.

Capability: [`artifacts/read_total_across_accounts.json`](../artifacts/read_total_across_accounts.json),
drifted twin [`..._drifted.json`](../artifacts/read_total_across_accounts_drifted.json).

| `parabank/` | What it shows |
|---|---|
| `discovery-success/` | A real LLM run against the live site: sign in, read the total across all accounts. Goal *"Find the total money in all accounts"*. |
| `replay-success/` | The same flow replayed with **no model in the decision loop**. Returns `total_account_balance = '$515.50'`. Exit `0`. |
| `replay-target-not-found/` | The drifted twin, replayed without an operator available. A **hard failure** naming the control it could not find. Exit `1`. |
| `replay-escalated-handoff/` | The same drift, with a person available: stuck → human takes the live session → clicks the renamed control → hands back → run completes. Exit `0`. |

The drift is `read_total_across_accounts_drifted.json`, a hand-edited twin of the recorded
capability with one control renamed — `Log In` → `Sign In` — standing in for a vendor relabelling a
button between releases. Everything else about the two files is identical, so the pair isolates
drift from every other reason a replay can fail.

Worth reading in `parabank/replay-escalated-handoff/result.json`: the escalation is recorded as a
**recovery**, not as a failure the run survived by accident.

    step 2 escalated: target_not_found -> human took the session
    (20260909T181040Z-d71a-step02) and said the step was performed

`Recoverable` is deliberately not one of the three terminal outcomes — it is an internal branch,
rolled up into whichever terminal result the run reaches. So a run that needed a person still
returns a clean `success` to its caller, and the fact that it needed one is in the record rather
than in the exit code. Against a live site this list also picks up recoveries nobody arranged: an
earlier run of this same bundle recorded a second one at step 3, where the target was not present
on the first look and replay settled again and re-perceived. That is real network latency being
absorbed where it should be, and it is not reproducible on demand — which is the honest difference
between this bundle and the target-app ones.

## What is in a bundle

**Discovery** bundles are written by `src/evidence.py`:

- `run.json` — the run without its steps: goal, target, start/finish, outcome.
- `trace.jsonl` — one `StepRecord` per line. Each carries the state before the step, the
  planner's decision (reasoning, intent, and the expectation it declared *before* acting), the
  policy verdict, the action result, and the verification result.
- `step_NN.png` — a screenshot per step.

**Replay** bundles are written by `src/replay/evidence.py`:

- `result.json` — the invocation and its typed outcome, flattened so the first lines answer
  "what happened".
- `trace.jsonl` — one `ReplayStepRecord` per line: the target as the capability described it,
  whether it resolved, the policy verdict, whether the action ran, and what the expectation
  check actually observed.
- Two fields on that record are about drift rather than about the step. `strategy_used` names the
  rung of the locator chain that resolved the target — `exact`, `normalised`, `contains`, or
  `anchor` — so a capability that still works only because the name is now matched loosely says
  so in its own trace. `no_progress_steps` counts acting steps in a row that changed nothing,
  which is the one thing no single step can see about itself.

A replay record is a different shape from a discovery record, deliberately. A discovery step is
built around a planner `Decision`; a replay step has none, because the capability already said
what to do. Reusing the discovery record here would have meant writing a `Decision` that no
model produced — fiction in an evidence bundle.

## The replays are the point

The same artifact, different inputs, three different result classes:

```
replay look_up_member_read --param member_id_or_name=22841
  → success | savings_account_current_balance = '$918.40'            exit 0

replay look_up_member_read --param member_id_or_name=99999
  → business outcome | RECORD_NOT_FOUND at step 4            exit 2

replay look_up_member_read
  → hard failure | invalid_request                           exit 1
```

**The first row is the one that took the most work.** The capability was recorded against member
12345, and 22841 is a record it has never seen. Getting that to work meant fixing two kinds of
overfitting: a `read` target whose accessible name was the recorded balance itself
(`$4,812.55` — which is not *where* the balance is, it is what it *was*), now anchored to the
neighbouring cell that says `Savings`; and expectations that quoted the recorded member's name.
The same artifact returns `$918.40` for 22841, `$12,003.00` for 30017 and `$4,812.55` for 12345.
A capability that only worked for the record it was recorded with would be a transcript with
extra steps.

The middle one is the one worth reading. Exit `2` is not an error: the flow ran correctly, the
application answered definitively, and the answer was "no such member". A caller that treated
that as a failure would retry it forever. `result.json` records it as
`"outcome": "business_outcome"` with a stable `code`, so a calling agent can branch on it
without parsing prose.

## The handoff bundle

`replay-escalated-handoff/` is the one to read for requirement 3.6. It is a replay of a
deliberately drifted capability -- one target renamed, as if a vendor had relabelled a control
between versions -- so the run cannot resolve step 5 and has nothing left to try.

    intervention/request.json        what the operator was told: condition, step, live URL,
                                     page title, a 21-element state summary, and the exact
                                     target that could not be found (including its frame)
    intervention/state.png           the screen at the moment it stopped
    intervention/lease.json          who holds control
    intervention/human-actions.jsonl what the person actually did
    intervention/resolved.json       who handed it back, and what they said about the step
    console.log                      the run's own output across the whole handoff
    trace.jsonl, result.json         the replay record, as for any other replay

`human-actions.jsonl` shows a click on `Open Record` followed by a navigation to
`/member/22841` — the record the caller asked for, which is the point: the operator's hands put
the session on the requested member, not on the one the capability was recorded against. That
click happened **inside an iframe**, in the browser window the run had already opened, driven by
a separate process attached over CDP -- which is how the operator console reaches the session
without going through the action executor the lease is blocking.
The final result is `success`, and `result.json` records the recovery:

    step 5 escalated: target_not_found -> human took the session and said the step was performed

`resolved.json` carries `"step_disposition": "performed"`, which is load-bearing. The operator
did that step by hand, so the run skipped it rather than repeating it. Had they only dismissed a
dialog, they would have resumed without `--performed` and the run would have retried. Only the
human knows which of those they did, so `cua operator resume` asks.

To reproduce it, in three terminals: run the replay with `--escalate`, then
`cua operator take <id>`, then `python scripts/operator_hands.py --click "Open Record"` (or do
it with your own hands in the window that is already open), then
`cua operator resume <id> --performed`.

## When the human puts it on the wrong record

`replay-wrong-record-checkpoint-failed/` is the same handoff with one thing changed: the operator
lands the session on member **12345** while the request named **22841**, then reports the step as
performed. Every remaining step runs and a balance is read.

That used to return `success` with `$4,812.55` — member 12345's money, for a request that named
22841 — because the checkpoint asserted that the text `Savings` was visible, which is true on
every member's page. Nothing in the system could tell the two runs apart. It now returns:

    result: hard failure | checkpoint_failed at step 7
      expected : looked for '22841' in the URL (case-insensitive)
      observed : URL was 'http://localhost:5000/member/12345'

The checkpoint failure is itself escalated first (`intervention/step07-checkpoint-failed/`) — a
flow that walked every step but landed wrong is worth a person's look. The operator cannot make
12345 be 22841, so control comes back unfixed and the run ends as a hard failure rather than a
wrong answer.

Read it next to `replay-escalated-handoff/`, which is the same capability, the same request, the
same drift and the same handoff, differing only in which record the human landed on. That pair is
the claim: the checkpoint is strong enough to reject the wrong record and not so strong that it
rejects the right one.

## Credentials

Grep these bundles for the target app's password and you will not find it. Two mechanisms do
that (`src/policy/redact.py`): a value sweep that removes every known secret from every string
in a record, and a name rule that scrubs values typed into fields that *look* sensitive. The
sweep is the one relied on — an earlier version scrubbed only `intent.value` and left the same
password readable in seven other places, including the model's own prose and the element values
read back off the page.

One note on the fixture, because it changes what this check is worth. The demo password used
to be the word `operator`, and grepping for it matched the CLI verb in `cua operator take` and
the phrase "waiting for an operator" -- so a real leak and a false positive looked identical,
and the check proved nothing. It is now a distinctive non-dictionary string (see `TARGET_PASSWORD`
in `.env`, or `OPERATOR_PASSWORD` in `target_app/data.py`), so the grep above is a test rather
than an argument. Deliberately not written out here either: a document that quotes the secret in
order to describe how well the secret is hidden fails its own check.

    grep -r "$(grep '^TARGET_PASSWORD=' ../.env | cut -d= -f2-)" .   # returns nothing

In `discovery-success/trace.jsonl` the accessible name `Operator ID` survives intact while the
credential is replaced. That is deliberate: replacement is literal and case-sensitive, so a
secret that resembles a UI label does not blank out the label.
