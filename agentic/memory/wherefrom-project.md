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

**Everything in `agentic/` is published.** Anything written here has to be safe to read by
anyone who clones the repo: no personal names, emails or machine paths, placeholder names
in test fixtures and examples, and no references to paths outside the repo.

The AI disclosure line belongs at the bottom of READMEs: `*This project was built with AI
code development tools ([Claude Code](https://www.anthropic.com/claude-code)).*`

See [[confirm-before-git-write]].
