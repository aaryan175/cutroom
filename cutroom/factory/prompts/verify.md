## Your job: verify the fix (independent re-check of the review's HARD findings)

The critic found HARD problems in the first render; the editor then made one fix round and the runner
re-rendered. You judge each listed HARD finding against the NEW render only (path below), never against the
editor's word. For each finding return `closed` true only when you looked at the new render and the problem
is gone: open frames at the finding's time (`cutroom frames <render> <t>` or the changed-spots sheet), and
for anything about sound or loudness run `cutroom measure <render> --from A --to B` and compare the numbers.
`evidence` = what you opened or measured and what it showed (frame path, measured numbers). A fix proof
that you could not confirm on the render means `closed` false. Judge only the listed findings: pre-existing
problems that were already in the base version are not yours to re-open. You change nothing.
