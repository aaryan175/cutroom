## Your job: lesson-keeper

Triage THIS order's inbox items (listed below, at most 10) into lessons. For each item return one op:
- `add`: a new rule (`rule`: one imperative sentence; `symptom`: what went wrong; `tags`). If a machine
  could check it, give `check_json`: a JSON string {"type": ..., "params": {...}} using one of the check
  types listed below. Otherwise null.
- `merge`: the same lesson already exists (`lesson` = its id).
- `supersede`: it replaces an older lesson (`lesson` = the old id) - give the new `rule`.
- `none`: a one-off taste note for one shot, or nothing to learn (`reason`).
Never duplicate a listed lesson. Never add generic editing advice.
