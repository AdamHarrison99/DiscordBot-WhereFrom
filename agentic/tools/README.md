# Tools

Things you run by hand while working on the bot. Nothing imports them, and nothing here
runs as part of the bot.

## `check-comments.mjs`

Enforces the comment rule from `agentic/CLAUDE.md`: a comment is context, not an
explanation. Node, no dependencies.

```bash
node agentic/tools/check-comments.mjs                        # lines this branch changed
COMMENT_LINT_BASE=HEAD node agentic/tools/check-comments.mjs  # uncommitted work only
node agentic/tools/check-comments.mjs bot.py                 # one file, whole
node agentic/tools/check-comments.mjs .                      # the whole tree
```

**Run it with a path when auditing.** Bare, it only looks at lines the diff touched, so it
reports clean while the rest of the tree is dirty.

What it flags:

| Rule | Limit |
| --- | --- |
| `comment-run` | more than 2 consecutive `//` or `#` lines |
| `comment-length` | a comment block over 120 characters |
| `docstring-lines` | a function or class docstring over 2 lines, a module one over 3 |
| `docstring-length` | over 160 characters, or 200 for a module |
| `rationale-word` | `because`, `otherwise`, `rather than`, `so that`, and similar |

The rationale words are the tell for a comment that has become an argument. A hit isn't
always wrong, but it means the line belongs in `agentic/CLAUDE.md` more often than not.
Exempt: `#!` lines, and everything from a `Usage:` line onward in a docstring — a tool's
help text is not an explanation. Exit 0 clean, 1 with violations, 2 on a bad path.

## `probe_gate.py`

Does a gate model actually score your criteria, or only notice whether it was addressed?
The ambient gate lives or dies on the model behind it and the catalogue can't tell you
which kind you have.

```bash
.venv/Scripts/python.exe agentic/tools/probe_gate.py
.venv/Scripts/python.exe agentic/tools/probe_gate.py <model> <model> --trials 5
.venv/Scripts/python.exe agentic/tools/probe_gate.py --show
```

It drives the real path — fake Discord messages through `to_record`, the real buffer, the
real `run_ambient` — so what reaches the model is what production sends, and the score
comes back off the log line rather than a reimplementation. The threshold is raised out of
reach first, so every scenario is scored and none is answered: the grid costs gate calls
only, about a cent a model. Uses `judge_template.md`, or wherever `JUDGE_TEMPLATE_FILE`
points.

Read the separation, not the scores. A model that answers 0 to everything and one that
answers 90 to everything are equally useless; what matters is that every scenario needing
a reply lands above every scenario that doesn't.

## `probe_media.py`

Does a model answer about media, and does it still reach for a tool while doing it? Run it
against any id before putting it in `OPENROUTER_IMAGE_MODEL` or `OPENROUTER_AUDIO_MODEL`.

```bash
.venv/Scripts/python.exe agentic/tools/probe_media.py
.venv/Scripts/python.exe agentic/tools/probe_media.py <model> --mode image --trials 6
```

Three modes: `audio` plays it a spoken clip, `image` shows it a picture, `source` asks
where a picture came from and watches for a `find_image_source` call. Costs a few cents.
Refusals are intermittent and a model that refuses advertises exactly the same
`input_modalities` and `tools` support as one that works, so one trial per arm proves
nothing — `probe_clip.wav` is the fixture, and several trials are the point.
