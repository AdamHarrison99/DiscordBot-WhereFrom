---
name: wherefrom-project
description: "WhereFrom Discord bot - layout, what belongs in agentic/, and the README disclosure line"
metadata:
  node_type: memory
  type: project
---

The bot's code is the repo root; agent-facing docs and checks live in `agentic/`.
`agentic/CLAUDE.md` is the handoff — read it first when starting cold, and keep it lean:
only what an agent needs, not documentation, change logs, or reasoning narratives.

**Everything here is published, `agentic/` included.** Nothing in the tree may identify a
person: no personal names, emails or handles, no machine paths, placeholder names in test
fixtures and examples, and no references to paths outside the repo. That covers code
comments and docstrings as much as the docs, and commit messages as much as the files.
Sweep the whole tracked tree before staging, not only what changed.

The AI disclosure line belongs at the bottom of READMEs: `*This project was built with AI
code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*`

See [[confirm-before-git-write]].
