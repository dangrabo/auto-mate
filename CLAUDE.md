# CLAUDE.md

Guidance for working in the `auto-mate` repo. Read `PLAN.md` for the full architecture and the *why* behind every decision below.

## What this is

auto-mate turns work tickets into pull requests automatically. It polls a GitHub repo for
issues labelled for automation, decides whether each one needs code changes, runs Claude Code
in an isolated worktree to do the work, then opens a PR. Everything runs locally for now.

## Status

Greenfield. The initial commit is bare. Most of the layout below does **not exist yet** —
create files to match it as the code gets written. When something here conflicts with reality,
reality wins: update this file.

## Tech stack

| Area | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | The author is learning Python — favour clear, conventional code over clever code. |
| Env / packaging | `uv` | `uv sync`, `uv run <cmd>`, `uv add <pkg>`. No `pip`, no manual venv. |
| DB | SQLite + SQLAlchemy 2.0 (ORM) + Alembic | Single local file `automate.db`. Alembic for every schema change. |
| GitHub API | `httpx` against the REST API directly | No PyGithub — the surface we need is tiny and raw REST teaches more. |
| Scheduler | plain `asyncio` loop (`while True: ... await asyncio.sleep(300)`) | No APScheduler yet. |
| Coding agent | `claude -p` as a subprocess via `asyncio.create_subprocess_exec` | Not the Agent SDK yet — see PLAN.md decision D2. |
| Triage LLM | Anthropic API, model `claude-haiku-4-5` | Cheap classification call: "does this ticket need code changes?" |
| Tests | `pytest` | |
| Lint / format | `ruff` (both) | `ruff check` and `ruff format`. |

## Intended repo layout

```
auto-mate/
  CLAUDE.md            # this file
  PLAN.md              # architecture + decision log
  pyproject.toml
  .env.example         # documented, committed
  .env                 # real secrets, gitignored
  automate.db          # SQLite, gitignored
  src/automate/
    config.py          # loads .env, typed settings object
    db/
      models.py        # SQLAlchemy models (Job, etc.)
      session.py       # engine + session factory
    github.py          # thin GitHub REST client (list issues, labels, create PR)
    poller.py          # the 5-minute loop: find new tickets, create Job rows
    triage.py          # LLM call: does this ticket need code?
    worker.py          # picks up queued Jobs, drives one to completion
    claude_runner.py   # spawns `claude -p`, streams + parses its output
    pr.py              # push branch + open the pull request
    workspace.py       # per-repo clone + per-job git worktree management
    cli.py             # entrypoint (start the poller + worker)
  migrations/          # Alembic
  workspaces/          # per-repo clones and per-job worktrees, gitignored
  tests/
```

## Common commands

```bash
uv sync                          # install/refresh dependencies
uv run automate                  # start the poller + worker (once cli.py exists)
uv run pytest                    # run tests
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
```

## Architecture in one paragraph

One process, one worker, one target repo. The **poller** asks GitHub every 5 minutes for issues
with the automation label updated since our last cursor, and inserts a `Job` row (status
`queued`) for any it hasn't seen. The **worker** takes one queued job at a time: **triage**
classifies it; if no code is needed it comments on the issue and stops (`needs_human`). Otherwise
it creates a git **worktree** on a new branch, runs **`claude -p`** there with a system prompt
built from the ticket, and on success **pushes the branch and opens a PR**. Job status is
mirrored back onto the issue as labels. See `PLAN.md` for the full pipeline, data model, and
state machine.

## Conventions

- **Type hints on every function signature.** Run `ruff` before considering work done.
- **Async by default** for anything doing I/O (HTTP, subprocess, sleeping). The poller and
  worker are async. DB calls can stay sync for the MVP (SQLite, one worker) — don't add async
  SQLAlchemy yet.
- **Config comes from `config.py` only.** Never read `os.environ` elsewhere. Add new settings
  to both `config.py` and `.env.example`.
- **Secrets never touch git.** `GITHUB_PAT` and `ANTHROPIC_API_KEY` live in `.env`. If you add a
  secret, confirm `.env` is gitignored and document the key (not the value) in `.env.example`.
- **The DB is the source of truth** for job state; issue labels are a read-only mirror for
  humans. Never read state back *from* labels.
- **Every job transition is a resumable step.** On startup the worker resets orphaned `running`
  jobs to `queued`. Don't design a step that can't be re-run safely.
- **Errors:** catch specific exceptions, not bare `except`. A failed job goes to `failed` with
  the error recorded and a retry count — it does not crash the process.
- **Keep the flow linear and debuggable.** No callbacks-into-callbacks, no premature
  abstraction. If you're tempted to generalise (multi-repo, multi-worker, plugin system), check
  PLAN.md — it's probably a deliberately deferred v2 item.

## Vocabulary

- **Ticket** — a GitHub issue carrying the automation label. The human-facing unit of work.
- **Job** — auto-mate's internal record for one ticket, with lifecycle and retries. One job per
  ticket.
- **Worktree** — the isolated `git worktree` + branch where one job's code changes happen.
- **Target repo** — the single repo auto-mate reads tickets from and opens PRs against (MVP:
  hardcoded in config; may be the same repo that hosts the code).

## Guardrails

- Don't add a web server, frontend, message queue, or Postgres yet — all deferred (PLAN.md v2).
- Don't switch to the Claude Agent SDK or an in-process MCP tool without revisiting decision D1/D2.
- Don't commit `automate.db`, `.env`, or anything under `workspaces/`.
- `claude -p` runs with real permissions in the worktree — keep it confined to the worktree
  directory and never point it at this repo or the user's home.
