# The checkpoint catches arrival at the wrong record

This bundle is a falsification that used to pass.

The request asks for member **22841**. The capability is the drifted twin, so
replay cannot resolve the "Open Record" link and escalates. A human takes the
session and — this is the injected fault — lands it on member **12345** instead,
then reports the step as performed. The flow then walks to the end and reads a
balance.

Before the checkpoint bound its parameter, that run reported:

    outcome: success
    savings_account_current_balance: $4,812.55     <- member 12345's balance

for a request that named member 22841. The checkpoint asserted that the text
"Savings" was visible, which is true on every member's page, so nothing in the
system could tell the two runs apart.

It now reports:

    result: hard failure | checkpoint_failed at step 7
      expected : looked for '22841' in the URL (case-insensitive)
      observed : URL was 'http://localhost:5000/member/12345'

`--escalate` is on, so the checkpoint failure is itself raised to a human first
(`intervention/step07-checkpoint-failed`) — a flow that walked every step but
landed wrong is worth a person's look. The operator cannot make 12345 be 22841,
so control comes back unfixed, the checkpoint is re-asserted, and the run ends
as a hard failure rather than a wrong answer.

## The control

The same capability, the same request, the same drift, the same handoff — with
the operator landing on the record that was actually asked for — still succeeds:

    result: success | steps=7
      savings_account_current_balance = '$918.40'   <- member 22841's balance

That pair is the point. The checkpoint is strong enough to reject the wrong
record and not so strong that it rejects the right one.

## Reproducing it

    python -m target_app
    python -m src.cli replay look_up_member_read_drifted \
        --param member_id_or_name=22841 --escalate --headed

    # when it escalates at step 5:
    python -m src.cli operator take <intervention-id>
    python scripts/operator_hands.py --goto http://localhost:5000/member/12345
    python -m src.cli operator resume <intervention-id> --performed

    # when it escalates again at the checkpoint:
    python -m src.cli operator resume <intervention-id>

Replacing `12345` with `22841` in the `--goto` produces the control run.
