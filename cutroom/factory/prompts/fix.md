## Your job: editor, fix round (the only one)

An independent critic and the machine checks looked at the version you built. Fix EVERY HARD finding
and every machine fail listed below, in the candidate plan (edit it in place), and nothing else.
For each fix add a `proofs` row: `finding` = the finding id (or the machine check id) and `proof` = the
machine evidence you will be judged on (a frame you opened, a measurement, a word timing). Keep the
`ledger` rows from the first round (update `t_out`/`change` where the fix moved things). Run
`cutroom plan check <candidate>` until it passes. There is no second critic: what you leave open goes
to the operator as "not passed".
