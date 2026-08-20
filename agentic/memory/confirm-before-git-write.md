---
name: confirm-before-git-write
description: Ask before any git commit or push; never do either unprompted
metadata:
  node_type: memory
  type: feedback
---

Never run `git commit` or `git push` without explicit confirmation for that specific
action. Make the file changes, then report what's ready and wait.

**Why:** approval for one git action is not approval for the next, and changes should be
reviewable before they enter history at all — not merely before they become public.

**How to apply:** edit files, run the checks, summarize what changed and what a commit
would contain — then stop and ask. Don't chain `git add && git commit` onto the end of a
work command. The same goes for other outward-facing git actions: creating repos, opening
PRs, changing visibility. See [[wherefrom-project]].
