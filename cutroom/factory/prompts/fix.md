## Your job: editor, fix round (the only one)

An independent critic and the machine checks looked at the version you built. Fix EVERY HARD finding
and every machine fail listed below, in the candidate plan (edit it in place), and nothing else.
For each fix add a `proofs` row: `finding` = the finding id (or the machine check id) and `proof` = the
machine evidence you will be judged on (a frame you opened, a measurement, a word timing). Keep the
`ledger` rows from the first round (update `t_out`/`change` where the fix moved things). Run
`cutroom plan check <candidate>` until it passes. There is no second critic: what you leave open goes
to the operator as "not passed".

Numbers: measure with `cutroom` commands (`cutroom measure <video> --from A --to B`, `cutroom qa`), never
estimate. You cannot render: a finding about the render's numbers is fixed by changing the plan, and your
proof names the setting you changed and the measured base value, not a predicted result. A finding of the
form "states a number the render does not show" is fixed either by making the render match (change the
plan) or by correcting the sentence to the measured number. The runner re-measures every number you write.
