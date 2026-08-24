---
name: one-commit-one-message
description: All outstanding work goes in one commit; never split it into several
metadata:
  node_type: memory
  type: feedback
---

Never split a commit message. Everything uncommitted goes into a single commit with a
single message, however many files or separate-looking concerns it touches.

**Why:** the split is judged here, not by the agent. Carving one session's work into
"logical" commits invents boundaries the author didn't ask for and turns a review of one
message into a review of several.

**How to apply:** when asked for a commit message, write one covering the whole
uncommitted tree. Don't offer a two-commit alternative, don't stage a subset, and don't
suggest that part of the work "belongs in its own commit". A subject line plus body
bullets is still one message. Then stop and ask, per [[confirm-before-git-write]].
