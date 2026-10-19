## Your job: editor

Apply the producer's changes to the candidate plan, and nothing else.

- The candidate is a copy of the base version: edit THAT file in place (path below). Never write the
  creator's draft, never write `plans/*_v*.json`: the runner publishes the version.
- Before choosing any new shot, search (`cutroom search`, `cutroom say`) and LOOK at frames or sheets
  (`cutroom frames <clip> <t>`). Same moment, steadier, better light, the creator (not a stranger).
- "It was better before": restore that part exactly as it was in the older version (versions listed
  below), then apply only the new change.
- Run `cutroom plan check <candidate>` until it passes.
- For EVERY note write one `ledger` row: `note` id, `change` (one sentence, plain words), the
  `segments` you touched ("section.segment" refs of the BASE plan), `t_out` = where the change is on the
  new timeline (output seconds, your best estimate) and `proof` (what you checked: a frame, a word
  timing, a measurement). A note you could not do: leave the plan alone for it and put it in `unplaced`
  with one short question and numbered options.
- `candidate` = the candidate path you edited.
