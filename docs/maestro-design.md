# Maestro — Technical Design Document

> A lightweight, model-agnostic, terminal-first orchestrator for running multiple
> CLI coding agents in parallel across isolated git worktrees.

Status: **Draft v0.1** · Audience: maintainers & early contributors

---

## 1. Verdict: Is the idea technically sound?

**Yes — and deliberately so, because Maestro does not try to be smart.**

The core insight is that the hard, valuable part is already solved by two mature
primitives:

1. **`git worktree`** gives you N isolated working directories backed by one
   repository and object store. Cheap, native, well-understood.
2. **CLI coding agents** (Claude Code, Codex, Gemini CLI, Kimi) are already
   self-contained processes that read a prompt, edit files, and exit.

Maestro is the thin layer that **binds a task to a worktree to a process**, then
captures logs and status. That is process supervision + git plumbing + a config
registry. There is no AI, no model hosting, no novel algorithm. Every piece is a
known, boring engineering problem.

The risk is **not** "can this be built" — it can, in a weekend for the MVP. The
risk is **scope discipline**: the gravity well of this project pulls toward
becoming a TUI dashboard, a session manager, a multiplexer, and eventually a
worse tmux. The design below is structured to resist that.

**Soundness conclusion:** Technically sound, low novelty, high execution risk
around scope. Proceed.

---

## 2. Risks & Limitations

### Technical risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Interactive vs. headless agents.** Some agents expect a TTY and human input; orchestration wants non-interactive runs. | High | Require a per-agent "non-interactive invocation" recipe in config (e.g. Claude `-p`, Codex `exec`). Agents without one are "attended only" in v1. |
| **No stable headless contract.** Agent CLIs change flags/output between versions. | High | Treat each agent adapter as config (a template), not code. Pin/record the agent version in run metadata. Don't parse agent stdout semantically — capture it verbatim. |
| **Worktree + shared state collisions.** `node_modules`, build caches, `.env`, dev servers binding the same port. | Medium | Worktrees share the repo, not the filesystem siblings. Document that per-workspace setup (install deps, copy env) is the user's hook. Provide a `setup` command hook in config. |
| **Concurrent git operations.** Parallel agents touching the same index/lock. | Medium | Each worktree has its own index; the shared `.git` object store is concurrency-safe for normal ops. Avoid Maestro itself running git in the main worktree while agents run. |
| **Long-running / hanging processes.** An agent waits forever on a prompt. | Medium | Timeouts, heartbeat via log mtime, explicit `maestro stop`. |
| **Secrets & permissions.** Agents run with full local credentials and can execute arbitrary commands. | High | This is inherent to running coding agents. Document it loudly. Don't add a false sense of sandboxing in v1. Optionally surface the agent's own permission flags. |
| **Cost / runaway loops.** Parallel paid agents burn tokens unattended. | Medium | Out of scope to meter, but surface run duration and make killing trivial. |

### Product / scope limitations (intentional v1 non-goals)

- No sandboxing or containerization.
- No web UI / rich TUI dashboard.
- No multi-machine / remote execution.
- No agent output *parsing* or quality evaluation.
- No conflict resolution or auto-merge intelligence.
- No scheduling, queues, or dependency graphs between tasks.

### Hard limitations to accept

- Maestro is only as good as the agent's own non-interactive mode. If an agent
  has no headless mode, Maestro can launch it attended but can't fully automate it.
- Disk: each worktree is a full checkout of tracked files (objects are shared).
  Large repos × many worktrees = real disk usage.

---

## 3. The Smallest Useful MVP

The MVP must let a solo developer do this loop and feel the value immediately:

> "Spin up an isolated branch, run an agent on a prompt, watch the logs, see the
> diff, keep or throw it away."

**MVP = 6 commands:**

```
maestro run     # create worktree + branch, launch agent with a prompt
maestro ls      # list workspaces and their status
maestro logs    # tail/print captured logs for a workspace
maestro open    # cd helper / print path to a workspace (or open a shell there)
maestro stop    # kill a running agent process
maestro rm      # remove a worktree (with safety checks)
```

Plus:
- A config file declaring agents (the invocation recipes).
- A state file tracking workspaces and runs.
- Verbatim log capture to disk.

**Explicitly NOT in the MVP:** PR creation, commit helpers, TUI, multi-agent
fan-out, templates. Those come in v0.2+. The MVP proves the worktree↔agent↔log
binding works and is pleasant. Everything else is additive.

A realistic first session:

```
maestro run --agent claude --branch task/add-tests --prompt ./prompts/tests.md
maestro ls
maestro logs task/add-tests -f
cd "$(maestro open task/add-tests)"
git diff
maestro rm task/add-tests
```

---

## 4. Architecture

### 4.1 Mental model

```
                ┌─────────────────────────────────────────────┐
                │                   maestro CLI                  │
                │  (arg parsing, command dispatch, UX/output)    │
                └───────────────┬───────────────────────────────┘
                                │
        ┌───────────────┬───────┴────────┬──────────────────┐
        ▼               ▼                ▼                  ▼
  ┌───────────┐   ┌───────────┐    ┌───────────┐     ┌────────────┐
  │  Worktree │   │   Agent   │    │  Process  │     │   State /   │
  │  Manager  │   │  Registry │    │ Supervisor│     │   Store     │
  │ (git)     │   │ (config)  │    │ (spawn)   │     │ (json/db)   │
  └─────┬─────┘   └─────┬─────┘    └─────┬─────┘     └─────┬──────┘
        │               │                │                 │
        ▼               ▼                ▼                 ▼
   git worktree   resolve agent    child process     ~/.maestro or
   add/remove     -> argv template  + log pipes       .maestro/state
```

### 4.2 Components

**1. CLI / Command layer**
Parses args, dispatches to commands, formats human + `--json` output. Thin.

**2. Worktree Manager**
Wraps `git worktree add/list/remove` and branch creation. Owns naming,
collision detection, and where worktrees physically live (default:
`<repo>/.maestro/worktrees/<slug>` or a sibling dir). Validates the repo is
clean enough and the branch is free.

**3. Agent Registry**
Loads agent definitions from config (built-in defaults + user overrides).
Resolves `--agent codex` into a concrete command template and how to inject the
prompt (arg, stdin, or file). Pure data + templating; no agent-specific code.

**4. Process Supervisor**
Spawns the agent as a child process with cwd = worktree, wires stdout/stderr to
a log file (and optionally the terminal), records PID, start/end time, exit
code. Handles `stop` (SIGTERM→SIGKILL), timeouts, and detached/background runs.

**5. State Store**
Single source of truth for "what workspaces exist and what's their status."
Plain JSON file (or SQLite later). Each entry: id, branch, worktree path, agent,
prompt ref, status, pid, timestamps, exit code, log path. Must survive process
restarts and reconcile against actual git worktree + process reality on read.

**6. Logging**
Append-only per-run log files under the workspace's metadata dir. Verbatim, no
parsing. `logs` reads/tails them.

### 4.3 Key design decisions

- **Stateless CLI, persistent state file.** Each `maestro` invocation is a fresh
  process; truth lives on disk. No long-running daemon in v1. This keeps it
  simple and crash-safe. (A daemon is a deliberate later option, not a v1 need.)
- **Reconcile on read.** `ls` cross-checks the state file against `git worktree
  list` and live PIDs, so manual git operations or killed processes don't cause
  drift/lies.
- **Agents are data, not plugins.** No dynamic code loading in v1. Adding an
  agent = adding a config block. This is the single most important decision for
  "no vendor lock-in / extensible."
- **Logs are sacred and dumb.** Capture bytes; never interpret. Interpretation
  is where agent coupling and breakage live.

### 4.4 On-disk layout

```
<repo>/
  .maestro/
    config.toml            # project-level config (committable)
    state.json             # workspace/run state (gitignored)
    worktrees/
      add-tests/           # git worktree for one task
    logs/
      add-tests/
        2026-06-16T10-03-runId.log
~/.maestro/
    config.toml            # user-global config (agents, defaults)
```

Project config is committable so a team shares agent recipes; state and logs are
local/gitignored.

---

## 5. Command Structure

Top-level: `maestro <command> [args] [flags]`. Global flags: `--json`,
`--config`, `--quiet/-q`, `--verbose`.

### MVP commands

```
maestro run
  --agent <name>            # required: which agent recipe to use
  --branch <name>           # branch to create (also derives workspace id)
  --prompt <file|->         # prompt source: file path, or - for stdin
  --base <ref>              # base branch/commit (default: current HEAD)
  --name <id>              # override workspace id (default: slug of branch)
  --detach / --background   # run agent without attaching to terminal
  --setup <cmd>            # optional one-time setup command in the new worktree
  -- <extra args>          # passthrough args appended to the agent invocation

maestro ls [--status <s>] [--json]      # list workspaces + status
maestro logs <id> [-f] [-n <lines>]      # print/tail logs
maestro open <id>                        # print worktree path (for cd/subshell)
maestro stop <id>                        # terminate the running agent
maestro rm <id> [--force] [--keep-branch]# remove worktree (+ optionally branch)
```

### v0.2+ commands (designed now, built later)

```
maestro commit <id> [-m ...]    # stage + commit agent's work (thin git wrapper)
maestro pr <id> [--draft]      # push branch + open PR via gh/glab
maestro agents                 # list configured agents
maestro attach <id>            # re-attach terminal to a backgrounded run
maestro exec <id> -- <cmd>     # run a command inside a workspace (tests, etc.)
maestro init                   # scaffold .maestro/config.toml
```

### UX principles

- Every command accepts a workspace id; ids are short, human-typed slugs.
- `run` is the only "create" verb; `rm` the only destroy. Keep the surface tiny.
- `--json` on everything for scripting/composability (this is the
  "terminal-first, GUI-later" hedge — a future UI is just a `--json` consumer).
- Non-zero exit codes mirror agent failures so Maestro composes in shell scripts.

---

## 6. Configuration Format

**Format: TOML.** Rationale: human-friendly, comments allowed, no significant
whitespace footguns (vs YAML), less verbose than JSON. Layered resolution:

1. Built-in defaults (shipped agent recipes).
2. `~/.maestro/config.toml` (user global).
3. `<repo>/.maestro/config.toml` (project, committable).
4. CLI flags (highest precedence).

### Shape

```toml
[defaults]
agent = "claude"
worktree_dir = ".maestro/worktrees"  # reserved in v0.1: must equal the default (see v0.1 spec §1.1)
log_dir = ".maestro/logs"            # reserved in v0.1: must equal the default (see v0.1 spec §1.1)

# An agent recipe = how to invoke a CLI agent non-interactively.
[agents.claude]
command = "claude"
# {prompt}, {prompt_file}, {branch}, {workspace}, {base} are interpolated.
args = ["-p", "{prompt}"]
prompt_via = "arg"          # arg | stdin | file
# optional lifecycle hooks
setup = "npm install"        # run once after worktree creation
env = { ANTHROPIC_LOG = "info" }

[agents.codex]
command = "codex"
args = ["exec", "{prompt}"]
prompt_via = "arg"

[agents.gemini]
command = "gemini"
args = ["--prompt", "{prompt_file}"]
prompt_via = "file"

[agents.kimi]
command = "kimi"
args = ["run", "-"]
prompt_via = "stdin"
```

### Why this shape works

- **An agent is fully described by: binary + arg template + how the prompt is
  delivered + optional env/setup.** That's the entire abstraction. No code.
- New/future agents need zero Maestro changes — only a config block.
- `prompt_via` cleanly handles the three real-world delivery modes (flag arg,
  stdin pipe, temp file path), which is where agents actually differ.
- Interpolation tokens are a small, documented set — not a templating language.

A `[agents.*.attended]` variant can later hold the interactive invocation for
agents run with a human in the loop, keeping headless/attended explicit.

---

## 7. How Agents Should Be Abstracted

The abstraction is a **declarative invocation recipe**, resolved at runtime into
a concrete `argv` + I/O wiring. Maestro's contract with any agent is minimal:

**Maestro promises the agent:**
- A working directory (the isolated worktree) as cwd.
- The prompt, delivered via the configured channel (arg/stdin/file).
- An environment (inherited + recipe `env` overrides).
- A clean process lifecycle (it will be started, supervised, and killable).

**The agent promises Maestro:**
- It runs as a normal process and exits with a meaningful status code.
- It does its work in cwd.

That's it. Crucially, Maestro **does not** understand the agent's output,
internal protocol, or capabilities. This is what delivers model-agnosticism and
prevents lock-in.

### The resolution pipeline

```
recipe (config) + run inputs (prompt, branch, worktree)
        │
        ▼
   interpolate tokens  →  build argv  →  decide prompt channel
        │
        ▼
   spawn(command, argv, { cwd: worktree, env, stdio })
        │
        ▼
   pipe stdout/stderr → log file ; record pid/exit
```

### Why not a plugin interface / code adapters in v1?

A code-based `AgentAdapter` interface is tempting and **wrong for v1**. It turns
"add an agent" into "write and ship code," reintroduces per-agent coupling, and
invites parsing the agent's output. The config recipe covers ~95% of real CLI
agents because they overwhelmingly share the same shape (binary + prompt + cwd).

**Escape hatch:** for the rare agent needing pre/post logic, allow a `wrapper`
field pointing at a user shell script that Maestro execs instead of the binary.
The script gets the same tokens via env vars. This preserves "no code in
Maestro" while handling edge cases — the complexity lives in the user's script,
not Maestro's core. A formal plugin API is a v2 decision, only if reality demands it.

---

## 8. Implementation Language

**Recommendation: Go.**

| Criterion | Go | Rust | Node/TS | Python |
|---|---|---|---|---|
| Single static binary, easy install | ✅ best | ✅ | ⚠️ runtime | ⚠️ runtime |
| Process supervision / signals | ✅ excellent | ✅ | ✅ | ✅ |
| Cross-platform (macOS+Linux) | ✅ | ✅ | ✅ | ✅ |
| Dev speed for this scope | ✅ high | ⚠️ slower | ✅ high | ✅ high |
| Distribution to non-devs | ✅ `brew`/binary | ✅ | ⚠️ npm/node | ❌ env hell |
| Ecosystem fit (CLI/git tooling) | ✅ (cobra, gh) | ✅ | ✅ | ✅ |

**Why Go:** the killer feature for an OSS CLI is *frictionless install* — a
single static binary with no runtime, trivially distributed via Homebrew, `go
install`, or GitHub Releases. Process spawning, signal handling, and concurrency
(for parallel runs/log streaming) are first-class. It's the lingua franca of git
tooling (`gh`, `lazygit`, `git` itself adjacent), so contributors and patterns
abound. Mature CLI libs (cobra/urfave) and config (viper/BurntSushi-toml).

**Rust** is a fine alternative (same distribution story) but slower to iterate
for a project whose value is breadth, not performance. **Node/TS** maximizes
contributor pool and speed but the runtime dependency hurts the "lightweight
install" promise. **Python** is the worst fit for distribution despite being
quick to prototype.

Decision: **Go**, unless the maintainer is markedly more productive in Rust.

---

## 9. Execution Roadmap

### Milestone 0 — Skeleton (½ day)
- CLI scaffold, command routing, config loading (TOML, layered), `--json`.
- Define state file schema; read/write with reconciliation stub.

### Milestone 1 — Core loop / MVP (2–3 days)
- `run`: create worktree+branch → resolve agent recipe → spawn → log → record.
- `ls`, `logs (-f)`, `open`, `stop`, `rm` with safety checks.
- Ship built-in recipes for **Claude Code, Codex, Gemini, Kimi**.
- Reconcile-on-read (git worktree list ↔ state ↔ live PIDs).
- **Deliverable: usable end-to-end for a solo dev. Tag v0.1.**

### Milestone 2 — Git productivity (2 days)
- `commit` (thin wrapper), `pr` (shell out to `gh`/`glab`), `exec`.
- `setup` hook execution on worktree creation.
- Background/detached runs + `attach`.
- **Tag v0.2.**

### Milestone 3 — Parallelism ergonomics (2–3 days)
- Run multiple agents/tasks; aggregate status view; concurrent log streaming.
- `wrapper` escape-hatch for custom agents; `agents` listing; `init` scaffold.
- Per-workspace status richness (diff stats, dirty/clean, ahead/behind).
- **Tag v0.3.**

### Milestone 4 — Polish & adoption (ongoing)
- Docs site, recipe cookbook (community-contributed agent configs), shell
  completions, Homebrew tap, CI release pipeline.
- Optional: lightweight TUI (`maestro tui`) that is *purely* a `--json` consumer.

Sequencing principle: **value before features.** M1 must stand alone as
something the maintainer uses daily before anything else is built.

---

## 10. Explicitly NOT in v1

These are deferred *by design*; listing them is part of the architecture.

- ❌ **Any AI / model logic.** Maestro orchestrates; it never calls a model API.
- ❌ **Sandboxing / containers / VMs.** Agents run with full local privileges.
  Document the risk; don't fake safety.
- ❌ **GUI or rich TUI dashboard.** Terminal commands + `--json` only. A UI, if
  ever, is a separate consumer of the JSON contract.
- ❌ **Long-running daemon / server.** Stateless CLI + state file. No background
  service to manage, crash, or secure.
- ❌ **Output parsing / quality evaluation / agent scoring.** Logs are verbatim.
- ❌ **Code-based plugin SDK for agents.** Config recipes + optional wrapper
  script only. Plugin API reconsidered in v2 if genuinely needed.
- ❌ **Remote / multi-machine execution, scheduling, queues, task DAGs.**
- ❌ **Conflict resolution / auto-merge / multi-agent voting.**
- ❌ **Cost metering / token budgets / rate limiting.**
- ❌ **Cross-workspace shared dependency caching magic.** Leave dep setup to the
  `setup` hook and the user.
- ❌ **Windows-native support.** macOS + Linux first (WSL acceptable).

---

## Appendix A — One-line summary

> Maestro = `git worktree` + a process supervisor + a TOML registry of agent
> invocation recipes, with verbatim logging and a tiny stateless CLI. The
> discipline is the product: it stays dumb so it can support every agent.

## Appendix B — The single biggest design bet

Treating **agents as configuration data, not code**. If that abstraction holds
(and the evidence — every target agent shares the binary+prompt+cwd shape — says
it will), Maestro achieves model-agnosticism and extensibility essentially for
free, and the project's long-term maintenance cost stays near zero. If it
breaks, the `wrapper` script escape hatch absorbs the exceptions without
compromising the core.
