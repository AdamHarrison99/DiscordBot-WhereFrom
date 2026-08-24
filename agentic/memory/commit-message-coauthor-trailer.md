---
name: commit-message-coauthor-trailer
description: Every commit message handed over ends with the Claude Co-Authored-By trailer
metadata:
  node_type: memory
  type: feedback
---

Every commit message written here ends with the co-author trailer, on its own line after
a blank line:

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

**Why:** the message is pasted and committed as given, so a trailer left off the draft is
a trailer missing from history. It has been missed that way before, and the commit is
public before anyone notices.

**How to apply:** include it in the drafted message itself, not as a note beside it, and
whether the message is one line or a subject plus bullets. It is part of the message, so
it does not make the message "split" — see [[one-commit-one-message]].
