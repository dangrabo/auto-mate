# auto-mate — Architecture & Decision Log

This document explains **what** auto-mate is, **how** it's built, and **why** each choice was
made. It's the reference for future-you asking "why did we do it this way?". When a decision is
revisited, update its entry and add a dated note — don't delete the history.

Last updated: 2026-08-29

---

## 1. Vision

Automate the boring path from ticket to pull request:

> A ticket is filed → auto-mate reads it → if it needs code, an AI agent writes the code in
> isolation → a PR appears for human review.

The human stays in the loop at the **review** step (and, later, at guidance checkpoints for jobs
that get stuck). auto-mate never merges.

## 2. MVP scope



### In

- Poll **one** GitHub repo for issues labelled `automate`.
- Track every ticket as a `Job` in a local SQLite DB with an explicit status lifecycle.
- Triage each ticket with a cheap LLM call: does it need code changes?
- For code tickets: run `claude -p` in an isolated git worktree/branch to make the changes.
- Push the branch and open a PR (`Closes #<issue>`), via a GitHub Personal Access Token.
- Mirror job status back onto the issue as labels so a human can see progress.
- Run as a single local process with one worker.



### Out (deferred — see §8)

- Web frontend / API.
- Human "step into a stuck process" interaction.
- Multiple target repos.
- Parallel workers.
- Postgres, message queues, containers, any hosted deployment.
- GitHub webhooks (we poll instead).
- The custom MCP tool + webhook PR-creation path from the original sketch.



## 3. Architecture overview

Single process. Two long-running async tasks (poller, worker) sharing the SQLite DB.

```
                    ┌──────────────────────────────────────────────┐
                    │                 auto-mate process            │
                    │                                              │
  GitHub Issues ───►│  POLLER (every 5 min)                        │
   (label: automate)│   • list issues updated since cursor         │
                    │   • insert Job(status=queued) for new ones   │
                    │            │                                 │
                    │            ▼                                 │
                    │        SQLite (automate.db) ◄──── source of  │
                    │            │                     truth       │
                    │            ▼                                 │
                    │  WORKER (one job at a time)                  │
                    │   1. triage  ── no code ──► comment + label  │
                    │      │                       (needs_human)   │
                    │      │ needs code                            │
                    │      ▼                                       │
                    │   2. create worktree + branch               │
                    │   3. run `claude -p` in the worktree         │
                    │   4. push branch                             │
                    │   5. open PR ──────────────────► GitHub      │
                    │   6. status = pr_open, mirror label          │
                    └──────────────────────────────────────────────┘
```



## 4. Components


| Module             | Responsibility                                                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py`        | Load `.env` into one typed settings object. Only place env vars are read.                                                                                |
| `github.py`        | Thin `httpx` wrapper over the GitHub REST endpoints we use: list issues (`since`, `labels`), add/remove labels, create comment, create PR.               |
| `poller.py`        | The 5-minute loop. Reads the stored cursor, fetches updated labelled issues, dedupes against existing `Job` rows, inserts new ones, advances the cursor. |
| `triage.py`        | One Anthropic API call (`claude-haiku-4-5`) returning a structured verdict: `needs_code: bool` + short reason.                                           |
| `workspace.py`     | Ensure a local clone of the target repo exists; create/tear down a `git worktree` + branch per job.                                                      |
| `claude_runner.py` | Build the system prompt from the ticket, spawn `claude -p`, stream and parse its JSON output, enforce a timeout, kill on hang.                           |
| `pr.py`            | Push the job's branch and open the pull request; return the PR URL.                                                                                      |
| `worker.py`        | The job state machine. Pulls one `queued` job, drives it through the steps, records transitions, handles failure/retry.                                  |
| `cli.py`           | Entrypoint: run migrations check, start poller + worker tasks.                                                                                           |




## 5. Data model & state machine



### `Job` (initial columns — refine as needed)


| Column                      | Type             | Purpose                                        |
| --------------------------- | ---------------- | ---------------------------------------------- |
| `id`                        | int PK           |                                                |
| `issue_number`              | int, unique      | The GitHub issue this job serves. Dedup key.   |
| `issue_title`               | str              | Snapshot for prompt building / display.        |
| `status`                    | enum (see below) |                                                |
| `branch`                    | str, nullable    | `automate/issue-<n>` once created.             |
| `pr_url`                    | str, nullable    | Set when the PR is opened.                     |
| `triage_reason`             | str, nullable    | Why triage decided code / no code.             |
| `error`                     | str, nullable    | Last failure message.                          |
| `attempts`                  | int, default 0   | Incremented on each run; cap before giving up. |
| `created_at` / `updated_at` | datetime         |                                                |


Plus a tiny `PollState` table (or a one-row key/value) holding the **cursor**: the timestamp of
the most recent issue update we've processed.

### Status lifecycle

```
queued ──► running ──► pr_open
   ▲          │
   │          ├──► needs_human   (triage: no code, or agent asked for help)
   │          └──► failed ──► (retry) ──► queued        [while attempts < max]
   │                              └────► needs_human    [attempts exhausted]
   └── on startup: any orphaned `running` job is reset here
```

- `queued` — known ticket, not started.
- `running` — worker is actively on it (triage → worktree → claude → PR).
- `pr_open` — PR created; auto-mate is done. Terminal for the MVP.
- `needs_human` — needs a person: no code required, ambiguous, or retries exhausted.
- `failed` — transient failure; eligible for retry.

Every status maps to a mirrored issue label: `automate:queued`, `automate:running`,
`automate:pr-open`, `automate:needs-human`, `automate:failed`.

## 6. Decision log

Each decision: the context, the options weighed, what we chose, why, and what would make us
revisit it. The overarching tie-breaker for the MVP is **"which is easiest to trace end-to-end
in a debugger?"** — the author is learning Python, and linear, owned control flow beats clever
indirection.

---



### D1 — PR creation: orchestrator creates it directly

**Context.** The original sketch had Claude call a custom MCP tool → local webhook → PR
creation. Everything runs on one machine.

**Options.**

1. **Orchestrator creates the PR directly** — Claude only writes code + commits; Python pushes
  the branch and calls the GitHub create-PR API after the subprocess exits.
2. In-process MCP tool — Claude calls a `create_pull_request` tool implemented as a Python
  function; Claude owns the "I'm done" decision and the PR title/body.
3. MCP tool + local webhook — as originally sketched.

**Chosen: 1.**

**Why.** Locally, "MCP tool → webhook → PR" is a hop to nowhere (localhost calling localhost to
make one API call). Option 1 keeps the entire flow in one linear Python path you can breakpoint
anywhere. It teaches the fundamentals — `subprocess`, `httpx`, git plumbing, exception handling,
state machines — without the agent loop bouncing control in and out of your code. Option 2 is a
genuinely nice design but the debugging story is worse before you know what "normal" looks like.

**Revisit when.** We want Claude to decide *when* a ticket is done and author the PR
description itself, or we adopt the Agent SDK (D2) — then move to option 2 (in-process tool, still
no webhook). The webhook (option 3) only earns its place if PR creation becomes a separate
service, which is not on the roadmap.

---



### D2 — Coding agent runtime: shell out to `claude -p`

**Context.** The worker needs to run Claude Code to make the code changes for a ticket.

**Options.**

1. `claude -p` **as a subprocess** (`asyncio.create_subprocess_exec`), system prompt via
  `--append-system-prompt`, `--output-format stream-json` parsed line by line.
2. **Claude Agent SDK for Python** (`claude-agent-sdk`) — Claude Code as a library: agent loop,
  built-in tools, streaming, hooks, permission callbacks, in-process MCP tools.

**Chosen: 1.**

**Why.** The process boundary is unambiguous — a command goes in, JSON lines come out — and
subprocess handling (spawn, stream, timeout, kill a hung child) is a transferable Python
fundamental. The Agent SDK is the better long-term integration but it's newer (less
Stack Overflow), and it hides the agent loop, which is exactly the part a learner benefits from
seeing explicitly first. D1 and D2 reinforce each other: "direct orchestrator + `claude -p`"
gives one clean, linear pipeline with clear seams.

**Revisit when.** We want hooks (e.g. progress events for a frontend), permission prompts routed
to a human, or in-process MCP tools (D1 option 2). That's the natural v2 bundle: Agent SDK +
in-process tools + FastAPI progress stream.

---



### D3 — Claude authentication: Anthropic API key

**Context.** Both the triage call and `claude -p` need Anthropic credentials, running headless.

**Options.**

1. `ANTHROPIC_API_KEY` — metered pay-as-you-go.
2. Reuse the author's Claude Code subscription login.

**Chosen: 1.**

**Why.** One env var, always works headless, never demands an interactive re-auth in the middle
of a poll cycle. It also forces good secret hygiene early (`.env`, `.gitignore`,
`.env.example`). Cost is real but small at MVP test volume, and mitigated by using
`claude-haiku-4-5` for triage and capping `max_tokens`. Watching token spend is a useful habit
to build now.

**Revisit when.** Test-run costs become annoying, or we want humans to run jobs under their own
subscription. Could support both via a config switch later.

---



### D4 — Target repos: one fixed repo

**Context.** auto-mate reads tickets from a repo and opens PRs against a repo.

**Options.**

1. **One repo**, hardcoded as `owner/repo` in config. The job-board repo may be the same as the
  code repo.
2. Multiple repos from day one.

**Chosen: 1.**

**Why.** Multi-repo forces a config system, workspace routing, and per-repo credential scoping
before the domain is even understood — premature abstraction. One repo keeps `github.py` and
`workspace.py` trivial. "Make this work for N repos" is a clean, well-defined refactor once the
single-repo path is solid.

**Revisit when.** The single-repo pipeline is reliable and there's a real second repo to serve.

---



### D5 — Job board: GitHub Issues, polled every ~5 min

**Context.** auto-mate needs a queue of incoming work.

**Options.** GitHub Issues (polled) · GitHub Issues (webhooks) · a separate task system ·
a homemade board.

**Chosen: GitHub Issues, polled.**

**Why.** Keeps everything inside GitHub — one set of credentials, no extra infra, tickets and
PRs live side by side. Polling (vs webhooks) avoids needing a public HTTPS endpoint / tunnel for
a local app. Five minutes is a fine latency for this workload.

**Dedup / "only new tickets".** We get identity almost for free: GitHub issue numbers are
monotonic and the Issues API has a `since` filter on `updated_at`. So:

- An **opt-in label** (`automate`) — auto-mate only touches issues a human has marked.
- A stored **cursor** — the newest `updated_at` we've processed; next poll asks for issues
updated since then.
- `issue_number` is unique on `Job`, so re-seeing an issue is a no-op.

**Revisit when.** We need sub-minute latency (move to webhooks + a tunnel) or outgrow Issues as
a work-tracking UI.

---



### D6 — Storage: SQLite + SQLAlchemy + Alembic

**Context.** Job state must survive restarts and be queryable.

**Chosen.** SQLite file, SQLAlchemy 2.0 ORM, Alembic migrations. Sync DB access (no async
SQLAlchemy) for the MVP — one worker, one process, local file.

**Why.** Zero setup, ships with Python, perfect for a single local process. SQLAlchemy is the
Python ORM worth learning and transfers directly to Postgres later. Alembic from day one so
schema history is never ad hoc.

**Revisit when.** We add a frontend/API process or parallel workers (concurrent writers) — then
Postgres, and possibly async SQLAlchemy.

---



### D7 — Isolation: one clone per repo, one `git worktree` + branch per job

**Context.** Each job mutates a working tree; jobs must not step on each other or on the user's
own checkout.

**Chosen.** Maintain a dedicated clone of the target repo under `workspaces/`. Per job, create a
`git worktree` on a fresh branch `automate/issue-<n>`; remove it when the job reaches a terminal
state.

**Why.** Worktrees are cheap and share the object store, so per-job isolation costs almost
nothing. `claude -p` is confined to that directory and never sees this repo or `$HOME`.

**Revisit when.** Parallel workers need stronger isolation (separate clones or containers per
job).

---



### D8 — Concurrency: a single serial worker

**Context.** How many jobs run at once.

**Chosen.** One. The worker processes one job start-to-finish before taking the next. The queue
(`queued` rows) is designed so more workers *could* be added later.

**Why.** No race conditions, no locking, no interleaved logs to untangle while learning. Throughput
is not an MVP concern.

**Revisit when.** Job volume genuinely exceeds what one worker clears in a reasonable time.

---



### D9 — Language & tooling: Python 3.12+, `uv`, `httpx`, `ruff`, `pytest`

**Context.** The author picked Python specifically to learn it.

**Chosen.** Python 3.12+; `uv` for env + packaging; `httpx` for GitHub REST (not PyGithub);
`ruff` for lint + format; `pytest`. `asyncio` loop for scheduling (not APScheduler).

**Why.** These are the current mainstream choices — what the author would want to know for any
future Python work. Raw REST over `httpx` teaches HTTP fundamentals and the GitHub surface we
need is small. Skipping APScheduler keeps the loop something you can read in five lines.

**Revisit when.** Scheduling needs get real (cron-like rules, backoff) — reconsider APScheduler.

---



### D10 — Triage is its own cheap LLM call

**Context.** "Decide if the ticket requires code changes" — the original architecture step.

**Chosen.** A dedicated `claude-haiku-4-5` call returning a structured `{needs_code, reason}`.
On `needs_code = false`: comment on the issue with the reason, set `needs_human`, stop.

**Why.** Cheap, fast, and keeps the expensive `claude -p` run for tickets that actually need it.
Structured output makes the branch trivial to code and test.

**Revisit when.** Triage accuracy is poor — upgrade the model, or fold triage into the main
agent run with an early-exit tool.

---



## 7. Risks & open questions

- **Prompt quality** — turning a terse issue into a good system prompt for `claude -p` is the
make-or-break. Expect to iterate. Consider including repo README / CONTRIBUTING in the prompt.
- `claude -p` **permissions** — headless runs need a permission story (`--permission-mode` /
allowed tools / sandbox). Decide before the first real run; keep it scoped to the worktree.
- **Partial work on failure** — if `claude -p` half-finishes, do we open a draft PR, discard the
branch, or leave it for a human? Leaning: discard branch, record error, retry; escalate to
`needs_human` after N attempts.
- **PR review loop** — the MVP stops at `pr_open`. Handling review comments / requested changes
is a v2 question.
- **Secret scope** — the PAT needs `repo` scope (and `workflow` if touching Actions). Document
the minimal scope in `.env.example`.
- **Rate limits** — GitHub REST is 5000 req/hr authenticated; a 5-minute poll is nowhere near,
but back off on 403/secondary limits anyway.



## 8. v2 roadmap (rough order)

1. **Agent SDK migration** (D2) — replace `claude -p` subprocess with `claude-agent-sdk`; gain
  hooks + permission callbacks.
2. **In-process MCP tool** (D1 option 2) — `create_pull_request` as a Python function Claude
  calls; retire the direct-creation step if desired.
3. **FastAPI backend** — expose jobs, logs, and a live progress stream (SSE) from the hooks.
4. **React + Next frontend** — dashboard; "step into" a `needs_human` job and give guidance,
  then resume.
5. **Review loop** — react to PR review comments with follow-up agent runs.
6. **Multi-repo** (D4).
7. **Parallel workers** (D8) → Postgres (D6), stronger per-job isolation (D7).
8. **Deployment** — move off "local only" (container, hosted, webhooks instead of polling).

