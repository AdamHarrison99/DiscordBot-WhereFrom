---
name: never-revert-a-users-edit
description: Never overwrite a user's own change without explicit permission for that change
metadata:
  node_type: memory
  type: feedback
---

Never overwrite a change the user made without explicit permission for that specific
change. If a file holds something other than what the last agent write put there, that
difference is theirs and it stays until they say otherwise.

**Why:** their edit is a decision, not a defect. Reverting it destroys work silently and
costs trust far more than a wrong-looking value ever costs the code. Noticing it came from
them and changing it anyway is worse than not noticing.

**How to apply:** before editing, consider whether the region was authored by the user —
config values, prompt files, anything they have open. Touch only what the task requires
and leave the rest byte-for-byte. If a user-set value looks wrong, say so in one line and
let them decide, and wait for a yes before touching it; never fix it as a side effect of
other work. Noticing the difference and proceeding is the failure — the notice is the
moment to stop and ask. Files under active editing
(`.env`, `.env.example`, `judge_template.md`, `agent_context.md`) deserve the most care.
See [[confirm-before-git-write]].
