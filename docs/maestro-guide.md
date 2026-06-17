# Maestro v0.1 — Practical Guide

> Terminal-first guide for running CLI coding agents in isolated git worktrees.
> For normative behavior, see [`maestro-v0.1-spec.md`](maestro-v0.1-spec.md).
> For architecture and design rationale, see [`maestro-design.md`](maestro-design.md).

---

## What is Maestro?

Maestro is a lightweight CLI that binds three things together:

1. **A git worktree + branch** — isolated workspace for one task
2. **A coding agent process** — Claude Code, Codex, Gemini CLI, Kimi, or any binary you configure
3. **Logs and status on disk** — so you can list, tail, stop, and tear down runs later

It does **not** call model APIs, parse agent output, or manage PRs. It orchestrates
processes and git worktrees so you can run several agents in parallel without
stepping on each other.

Typical loop:

```sh
maestro run --agent claude --branch task/add-tests --prompt ./prompts/tests.md
maestro ls
maestro logs task-add-tests -f
cd "$(maestro open task-add-tests)" && git diff
maestro rm task-add-tests
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **git** | Must be installed and on `PATH`. Maestro shells out to `git worktree`. |
| **Python ≥ 3.10** | v0.1 is implemented in Python (`maestro-cli` package). |
| **At least one agent CLI** | e.g. `claude`, `codex`, `gemini`, or `kimi` on `PATH`, **or** a custom/fake agent for local testing. |
| **A git repo with ≥ 1 commit** | Run Maestro from inside the repo (any subdirectory is fine). |

Maestro works on **macOS and Linux**. Windows native support is out of scope for v0.1.

---

## Install (development)

From the repository root:

```sh
# Option A — run via module (recommended for development)
export PYTHONPATH="$(pwd)"
python3 -m maestro_cli.cli --help

# Option B — install the package
python3 -m pip install .
# If `maestro` is not found, add your user scripts dir to PATH, or use:
python3 -m maestro_cli.cli --help

# Option C — editable install (requires pip ≥ 22 + setuptools ≥ 64)
python3 -m pip install -e ".[dev]"
maestro --help
```

Optional dev dependencies (pytest):

```sh
python3 -m pip install pytest
```

---

## Run tests

Official test suite:

```sh
python3 -m pytest -q
```

Also compatible with the stdlib runner:

```sh
python3 -m unittest -v
```

Tests invoke Maestro as `python3 -m maestro_cli.cli` with `PYTHONPATH` set to the repo root — the same pattern works for manual local testing.

---

## On-disk layout

After the first `run`, Maestro creates (and gitignores) local state under `.maestro/`:

```
<repo>/
  .maestro/
    config.toml          # project agent recipes (committable)
    state.json           # workspace/run state (gitignored)
    state.lock           # advisory lock (transient)
    worktrees/
      <id>/              # git worktree for each workspace
    logs/
      <id>/
        prompt.txt       # exact prompt bytes sent to the agent
        <timestamp>.log  # verbatim agent stdout/stderr
        <timestamp>.exit # exit code written when run finishes
  ~/.maestro/
    config.toml          # optional user-global overrides
```

Project config (`.maestro/config.toml`) can be committed so a team shares agent recipes.
State and logs stay local.

---

## Configure agents

Agent recipes live in TOML. Maestro merges config in this order (later wins):

1. Built-in defaults (claude, codex, gemini, kimi)
2. `~/.maestro/config.toml`
3. `<repo>/.maestro/config.toml`
4. `--config /path/to/extra.toml` (CLI flag)

### Minimal project config

Create `.maestro/config.toml`:

```toml
[defaults]
agent = "claude"
worktree_dir = ".maestro/worktrees"
log_dir = ".maestro/logs"

[agents.claude]
command = "claude"
args = ["-p", "{prompt}"]
prompt_via = "arg"

[agents.codex]
command = "codex"
args = ["exec", "{prompt}"]
prompt_via = "arg"
```

### Interpolation tokens

| Token | Expands to |
|---|---|
| `{prompt}` | Full prompt text (when `prompt_via = "arg"`) |
| `{prompt_file}` | Absolute path to `.maestro/logs/<id>/prompt.txt` (when `prompt_via = "file"`) |
| `{branch}` | Workspace branch name |
| `{workspace}` | Workspace id |
| `{base}` | Base ref string passed to `--base` |
| `{worktree}` | Absolute worktree path |

### Prompt delivery (`prompt_via`)

- **`arg`** — substitute `{prompt}` in `args` (Claude, Codex defaults)
- **`file`** — agent reads `{prompt_file}` (useful for agents that want a path)
- **`stdin`** — Maestro pipes prompt bytes to the agent's stdin; `args` must not contain prompt tokens

### Optional recipe fields

```toml
[agents.my-agent]
command = "my-agent"
args = ["run", "{prompt}"]
prompt_via = "arg"
setup = "npm install"           # runs once in the worktree before the agent
env = { MY_FLAG = "1" }        # extra env vars for the agent process
```

Pass extra agent flags at run time after `--`:

```sh
maestro run --agent claude --branch task/x --prompt p.txt -- --model claude-sonnet-4-20250514
```

### Auto-approval / yolo flags

**Not enabled by default.** Agents run with your local credentials and file permissions.
For unattended edits, opt in explicitly in config or via passthrough flags, e.g.:

```toml
# Claude — example only; verify against your installed version
args = ["-p", "{prompt}", "--permission-mode", "acceptEdits"]

# Codex — example only
args = ["exec", "{prompt}", "--full-auto"]

# Gemini — example only
args = ["--prompt", "{prompt}", "--yolo"]
```

---

## Commands (v0.1)

Global flags (all commands): `--json`, `--config`, `-q` / `--quiet`, `--verbose`.

| Command | Purpose |
|---|---|
| `run` | Create worktree + branch, spawn agent |
| `ls` | List workspaces (reconciles status first) |
| `logs <id>` | Print or tail run log (`-f`, `-n N`) |
| `open <id>` | Print worktree path (for `cd "$(maestro open <id>)"`) |
| `stop <id>` | SIGTERM → SIGKILL running agent |
| `rm <id>` | Remove worktree; branch kept unless `--delete-branch` |

### `run`

```sh
maestro run \
  --agent <name> \
  --branch <branch-name> \
  --prompt <file|-> \
  [--base HEAD] \
  [--name <workspace-id>] \
  [--detach] \
  [--setup "<shell command>"] \
  [-- <extra agent args>]
```

- **`--branch`** — new branch created in a new worktree (must not already exist).
- **`--prompt`** — path to a prompt file, or `-` to read from stdin.
- **`--base`** — ref to branch from (default: `HEAD`).
- **`--name`** — override workspace id (default: slug of `--branch`, e.g. `task/add-tests` → `task-add-tests`).
- **`--detach` / `--background`** — return immediately; agent keeps running in the background.
- Foreground runs stream logs to the terminal and exit with the agent's exit code.

Workspace id rules: `^[a-z0-9][a-z0-9._-]{0,63}$`.

### `ls`

```sh
maestro ls
maestro ls --status running
maestro ls --json
```

### `logs`

```sh
maestro logs <id>           # print full log
maestro logs <id> -n 50     # last 50 lines
maestro logs <id> -f        # follow until run reaches terminal status
```

### `open`

```sh
cd "$(maestro open <id>)"
```

Prints the absolute worktree path to stdout. Errors if the directory is missing.

### `stop`

```sh
maestro stop <id>
```

No-op with an informational message if the workspace is not `running`.

### `rm`

```sh
maestro rm <id>                        # remove worktree; keep branch
maestro rm <id> --delete-branch        # also delete branch (safe `-d`)
maestro rm <id> --force                # stop if running; discard dirty changes
maestro rm <id> --force --delete-branch
```

Logs under `.maestro/logs/<id>/` are **retained** after `rm` (useful for post-mortems).

---

## Practical examples

### 1. Fake agent (local testing, no API keys)

Use a shell script or small program on `PATH`. Example `.maestro/config.toml`:

```toml
[agents.fake]
command = "/path/to/fake-agent.sh"
args = ["--prompt", "{prompt}"]
prompt_via = "arg"
```

```sh
# fake-agent.sh
#!/bin/sh
echo "fake agent ran with args: $*"
```

```sh
echo "add tests please" > prompt.txt

maestro run --agent fake --branch task/add-tests --prompt prompt.txt
maestro ls
maestro logs task-add-tests
cd "$(maestro open task-add-tests)" && git status
maestro rm task-add-tests
```

The test suite uses a similar Python fake agent — see `tests/test_cli.py`.

### 2. Codex

```sh
# Requires `codex` on PATH and authenticated per Codex CLI docs
maestro run --agent codex --branch task/refactor-auth --prompt prompts/auth.md
```

With extra Codex flags (user opt-in for automation):

```sh
maestro run --agent codex --branch task/refactor-auth --prompt prompts/auth.md -- --full-auto
```

### 3. Claude Code

```sh
maestro run --agent claude --branch task/add-tests --prompt prompts/tests.md
```

Model override via passthrough:

```sh
maestro run --agent claude --branch task/docs --prompt prompts/docs.md -- --model claude-sonnet-4-20250514
```

### 4. Prompt from a file

```sh
maestro run --agent claude --branch task/feature --prompt ./prompts/feature.md
```

The path is stored in state as you typed it. Prompt bytes are also saved to
`.maestro/logs/<id>/prompt.txt`.

### 5. Prompt from stdin (`--prompt -`)

```sh
echo "Fix the failing test in pkg/foo" | \
  maestro run --agent claude --branch task/fix-foo --prompt -
```

Or from an editor/heredoc:

```sh
maestro run --agent codex --branch task/review --prompt - <<'EOF'
Review the changes on this branch for security issues.
Focus on auth middleware.
EOF
```

For `prompt_via = "stdin"` recipes, Maestro pipes bytes to the agent's stdin instead of passing `{prompt}` on the command line.

### 6. Detached / background run

```sh
maestro run --agent claude --branch task/long-job --prompt prompt.txt --detach
# prints workspace id, e.g.: task-long-job

maestro ls
maestro logs task-long-job -f
maestro stop task-long-job    # if needed
```

With `--json`, detached `run` prints the workspace object as JSON.

### 7. Remove worktree and delete branch

```sh
maestro rm task-long-job --delete-branch
# removed worktree and branch task/long-job
```

If the branch has unmerged commits, `-d` fails unless you also pass `--force` (uses `-D`).

---

## Risks and limitations

Read this before running agents on real projects.

| Topic | Reality |
|---|---|
| **No sandbox** | Maestro does not containerize or restrict agents. They run as **your user** with your SSH keys, cloud creds, and filesystem access. |
| **No auto-approval by default** | Default recipes use each agent's standard non-interactive mode. Yolo / skip-permissions / full-auto flags are **opt-in** in config or CLI passthrough. |
| **Agent CLI drift** | Flags change between agent versions. Recipes are config — verify against your installed CLI and pin versions in team docs. |
| **Disk usage** | Each worktree is a full checkout of tracked files (objects are shared, but trees add up). Clean up with `maestro rm`. |
| **Ports, env, dependencies** | Worktrees do not magically isolate `node_modules`, `.env`, dev servers, or database URLs. Use the `setup` hook to install deps or copy env files; avoid port collisions yourself. |
| **No daemon** | Maestro is a stateless CLI. Status is reconciled from `state.json`, live PIDs, and exit sentinel files on each command. |
| **Parallel runs** | Supported. Each workspace has its own worktree and log. State writes are serialized with a file lock (5s timeout). |
| **Unattended cost** | Parallel paid agents can burn tokens. Use `maestro ls`, `maestro stop`, and duration in `ls` output to monitor runs. |

---

## Operational recovery

### Worktree deleted manually

If you `rm -rf .maestro/worktrees/<id>` or run `git worktree remove` outside Maestro:

1. `maestro ls` reconciles the workspace to **`orphaned`**.
2. Clean up with:

```sh
maestro rm <id>              # tolerates missing worktree dir
git -C /path/to/repo worktree prune
```

### `state.json` corrupted

On **`run`** (mutating command), Maestro backs up the bad file to
`.maestro/state.json.corrupt-<timestamp>`, starts from an empty state, and prints a warning.

Recovery steps:

1. Inspect the backup and any remaining worktrees:

```sh
git -C /path/to/repo worktree list
ls -la .maestro/worktrees/
```

2. Remove stale Maestro entries or re-run tasks as needed.
3. Manually merge recovered workspace metadata from the backup only if you understand the JSON schema (see spec Part 1).

**Note:** `ls` and other read commands currently **fail** on corrupt state instead of auto-repairing. Use `run` once to trigger backup + reset, or restore/fix `state.json` by hand.

### Find logs and saved prompt

```sh
# From repo root
ls .maestro/logs/<workspace-id>/
cat .maestro/logs/<workspace-id>/prompt.txt
maestro logs <workspace-id>
maestro ls --json    # includes run.log_path per workspace
```

Exit sentinel: `.maestro/logs/<id>/<timestamp>.exit` (integer exit code).

### Stale worktree registrations

```sh
git worktree list
git worktree prune
```

Use `git worktree list --porcelain` for scripting. Maestro runs `git worktree prune` during `rm`.

### Agent stuck or zombie run

```sh
maestro stop <id>
# if status is wrong: maestro ls  (reconciles)
# last resort: kill PID from maestro ls --json, then maestro ls again
```

### PID reuse limitation

Maestro v0.1 checks whether a recorded agent process is alive with a bare
`kill(pid, 0)`-style liveness probe. It does not compare the recorded
`run.started_at` value with the operating system process start time. On systems
that quickly reuse process IDs, `maestro ls` can temporarily treat a reused PID
as the original agent process; once that PID exits, normal sentinel/orphan
reconciliation applies. This is the v0.1 limitation allowed by spec §1.6.1.

### Dirty worktree blocks `rm`

```sh
maestro rm <id> --force
```

---

## Scripting with `--json`

```sh
# Count workspaces
maestro ls --json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"

# Detached run — id on stdout (plain) or full workspace object (--json)
ID=$(maestro run --agent fake --branch task/x --prompt p.txt --detach)
WS=$(maestro ls --json | python3 -c "import sys,json; print(next(w for w in json.load(sys.stdin) if w['id']=='task-x'))")
```

Detached `run` without `--json` prints only the workspace id on stdout.

---

## Further reading

- [`maestro-v0.1-spec.md`](maestro-v0.1-spec.md) — normative schema, reconciliation, edge cases
- [`maestro-design.md`](maestro-design.md) — architecture, non-goals, roadmap

---

## Pendências para engenharia

Inconsistências observadas entre documentação anterior e a implementação v0.1 atual
(revisão pós-fixes do Codex Builder):

| # | Área | Spec / design doc | Implementação atual |
|---|---|---|---|
| 1 | Flag de branch em `rm` | Design doc §5 cita `--keep-branch` | v0.1 usa `--delete-branch` (opt-in); spec prevalece |
| 2 | `--agent` default | Design doc §5 e config `defaults.agent` sugerem agente opcional | CLI exige `--agent` (`required=True`); fallback em código nunca é alcançado |
| 3 | Linguagem | Design doc §8 recomenda Go | v0.1 entregue em Python (`maestro-cli`) |
| 4 | Supervisor detached | Spec §1.6 / §3.2 descreve double-fork | Re-exec via subcomando interno `_supervise` + `subprocess.Popen` |
| 5 | PID reuse guard | Spec §1.6.1 (best-effort start-time check) | Apenas probe de liveness (`kill(pid, 0)`); limitação documentada acima |
| 6 | Logs em `rm --force` | Spec §3.6: `--force` MAY apagar logs | Logs sempre retidos após `rm` |
| 7 | `state.json` corrupto | Spec §1.8: comandos read-only MAY continuar com warning | `ls`/`logs`/etc. falham (`repair_corrupt=False`); só `run` faz backup + reset |
| 8 | Nome do repositório | README anterior: "Mastro" | Produto/comando: **Maestro** (`maestro-cli`) |
| 9 | Instalação editable | — | `pip install -e .` falha com pip/setuptools antigos; documentado workaround via `PYTHONPATH` |
| 10 | Saída de `run --detach` | Spec exemplo §3.8: mensagem estilo `created workspace:` | Imprime só o workspace id (ou JSON com `--json`) |
