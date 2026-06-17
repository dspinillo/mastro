# Maestro v0.1 — Implementation Spec

> Companion to `docs/maestro-design.md`. This document is **normative** for v0.1.
> Scope is strictly the six MVP commands: `run`, `ls`, `logs`, `open`, `stop`,
> `rm`. An implementer should be able to build v0.1 from this without making
> further architecture decisions. Where this spec and the design doc disagree on
> a detail, **this spec wins** (noted inline).

Conventions:
- **MUST / SHOULD / MAY** per RFC 2119.
- Paths shown POSIX-style; behavior identical on macOS and Linux.
- "Repo root" = output of `git rev-parse --show-toplevel` from the invocation cwd.
- All timestamps are **RFC 3339 / ISO 8601 UTC** with `Z` suffix, second
  precision (e.g. `2026-06-16T10:03:42Z`).

---

## Part 1 — State-File JSON Schema

### 1.1 Location & lifecycle

- Path: `<repo-root>/.maestro/state.json`.
- Created on first `run` if absent (along with `.maestro/`, `.maestro/worktrees/`,
  `.maestro/logs/`).
- MUST be gitignored. On first creation, Maestro SHOULD append `.maestro/state.json`
  and `.maestro/logs/` and `.maestro/worktrees/` to `<repo-root>/.gitignore` if a
  matching entry is absent. (`.maestro/config.toml` is NOT ignored — it is committable.)
- The file is the source of truth for "which workspaces exist," but every read
  MUST be **reconciled** against git and process reality (see §1.6).

### 1.2 Top-level shape

```json
{
  "version": 1,
  "updated_at": "2026-06-16T10:03:42Z",
  "workspaces": [ /* Workspace objects, see §1.3 */ ]
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `version` | integer | yes | MUST be `1` for v0.1. Unknown/higher version → Maestro MUST refuse to operate and print an upgrade message. |
| `updated_at` | string (RFC3339 UTC) | yes | Last time the file was written. |
| `workspaces` | array<Workspace> | yes | MAY be empty `[]`. Order is insertion order (newest last). |

### 1.3 Workspace object

```json
{
  "id": "add-tests",
  "branch": "task/add-tests",
  "base": { "ref": "main", "sha": "9fceb02..." },
  "worktree_path": "/abs/repo/.maestro/worktrees/add-tests",
  "agent": "claude",
  "prompt": { "source": "file", "ref": "prompts/tests.md", "sha256": "ab12..." },
  "status": "running",
  "run": {
    "pid": 48213,
    "detached": false,
    "started_at": "2026-06-16T10:03:42Z",
    "ended_at": null,
    "exit_code": null,
    "log_path": ".maestro/logs/add-tests/2026-06-16T10-03-42Z.log",
    "command": ["claude", "-p", "<prompt>"]
  },
  "created_at": "2026-06-16T10:03:41Z",
  "updated_at": "2026-06-16T10:03:42Z",
  "maestro_version": "0.1.0"
}
```

#### Field rules

| Field | Type | Required | Rules |
|---|---|---|---|
| `id` | string | yes | Workspace identifier. MUST match `^[a-z0-9][a-z0-9._-]{0,63}$`. MUST be unique within the file. Default derivation: slug of `--branch` (see §1.4). |
| `branch` | string | yes | Git branch name created for this workspace. MUST pass `git check-ref-format --branch`. |
| `base` | object | yes | `{ "ref": string, "sha": string }`. `ref` = user-supplied/default base (e.g. `main`, `HEAD`, a tag). `sha` = full 40-char commit SHA `ref` resolved to at creation time. Immutable after creation. |
| `worktree_path` | string | yes | **Absolute** path to the git worktree directory. |
| `agent` | string | yes | Name of the agent recipe used. MUST exist in the merged config at run time; stored verbatim even if later removed from config. |
| `prompt` | object | yes | See §1.5. |
| `status` | string (enum) | yes | One of: `running`, `completed`, `failed`, `stopped`, `orphaned`. See §1.6. |
| `run` | object \| null | yes | The most recent run record (§1.3.1). `null` only in the brief window between worktree creation and process spawn; persisted records always have a `run`. v0.1 keeps **one** run per workspace (re-running replaces it). |
| `created_at` | string (RFC3339 UTC) | yes | When the workspace/worktree was created. Immutable. |
| `updated_at` | string (RFC3339 UTC) | yes | Last mutation of this workspace object. |
| `maestro_version` | string (semver) | yes | Version of Maestro that created the workspace. |

#### 1.3.1 `run` object

| Field | Type | Required | Rules |
|---|---|---|---|
| `pid` | integer \| null | yes | OS process id of the spawned agent. `null` after the process has been reaped and a terminal status recorded. |
| `detached` | boolean | yes | `true` if started with `--detach`. Affects exit-code capture (§1.6). |
| `started_at` | string (RFC3339 UTC) | yes | Spawn time. |
| `ended_at` | string (RFC3339 UTC) \| null | yes | Null while `running`; set when a terminal status is reached. |
| `exit_code` | integer \| null | yes | Process exit code. `null` while running or if unrecoverable (`orphaned`). `stopped` runs record the code if known, else `null`. |
| `log_path` | string | yes | Path to the verbatim log file, **relative to repo root**. |
| `command` | array<string> | yes | The fully-resolved argv actually spawned, with the prompt value redacted to the literal token `<prompt>` if `prompt_via` was `arg` (to avoid leaking long/secret prompts into state). Other args verbatim. |

### 1.4 ID derivation (default)

When `--name` is not given, `id` derives from `--branch`:
1. Take the branch string.
2. Lowercase.
3. Replace any run of characters not in `[a-z0-9._-]` with `-`.
4. Strip leading/trailing `-`, `.`, `_`.
5. Truncate to 64 chars.

Example: `task/Phase1-Tests` → `task-phase1-tests`. If the result collides with an
existing `id`, `run` MUST error and instruct the user to pass `--name`.

### 1.5 `prompt` object

| Field | Type | Required | Rules |
|---|---|---|---|
| `source` | string (enum) | yes | `file` or `stdin`. (Inline prompt strings are NOT a v0.1 feature; `--prompt` takes a path, or `-` for stdin.) |
| `ref` | string \| null | yes | For `file`: the path **as supplied by the user** (may be relative to invocation cwd). For `stdin`: `null`. |
| `sha256` | string | yes | Hex SHA-256 of the exact prompt bytes delivered to the agent. Enables reproducibility/audit without storing prompt contents in state. |

For `stdin` and `file` prompts alike, Maestro MUST also persist the literal prompt
bytes to `.maestro/logs/<id>/prompt.txt` at run time (verbatim, alongside the log)
so the run is reproducible. State stores only the hash.

### 1.6 Status semantics & reconciliation (CRITICAL)

Because Maestro is a stateless CLI (no daemon), the on-disk `status` can be stale.
**Every command that reads state MUST reconcile before acting/printing.**

Terminal vs. live:
- `running` is the only non-terminal status.
- `completed`, `failed`, `stopped`, `orphaned` are terminal.

Reconciliation algorithm for a workspace whose stored status is `running`:

1. **Worktree check:** if `worktree_path` no longer exists on disk → set
   `status = orphaned`, `run.pid = null`, `ended_at = now`, persist.
2. **Process check:** probe `run.pid` liveness (signal 0 / equivalent).
   - **Alive** AND the process still corresponds to our run (see §1.6.1) →
     remains `running`. No change.
   - **Not alive** → look for the **exit sentinel** file
     `.maestro/logs/<id>/<run-id>.exit` (written by the supervisor; see §1.6.2):
     - Sentinel present → read integer exit code. `0` → `completed`; non-zero →
       `failed`. Set `exit_code`, `ended_at` (sentinel mtime), `pid = null`.
     - Sentinel absent → `status = orphaned`, `exit_code = null`, `pid = null`,
       `ended_at = now`. (Maestro or the machine died before reaping.)

Foreground (`detached=false`) runs: the `run` command process supervises the
child directly and writes the terminal status itself; reconciliation is the
fallback if Maestro was interrupted.

Detached (`detached=true`) runs: the supervisor double-forks and is responsible
for writing the exit sentinel on child exit; reconciliation is the **primary**
mechanism for learning the outcome.

#### 1.6.1 PID reuse guard

A bare PID can be reused by the OS. To avoid misreporting a reused PID as "our
agent still running," Maestro SHOULD additionally verify process identity using
**`started_at` vs. process start time** when the platform exposes it (best
effort). If identity can't be confirmed and the PID is alive but its start time
predates `started_at`, treat as not-our-process → fall through to sentinel check.
v0.1 MAY implement only the bare liveness probe and document the limitation.

#### 1.6.2 Exit sentinel

- Path: `.maestro/logs/<id>/<run-id>.exit` where `<run-id>` is the basename of the
  log file (the timestamp slug).
- Content: a single integer (the child exit code) followed by a newline.
- For signal-terminated children, write `128 + signal_number` (shell convention).
- Written atomically (temp + rename).

### 1.7 Concurrency & atomic writes

Multiple `maestro` processes can run simultaneously (parallel agents).
- All writes to `state.json` MUST be serialized via an advisory lock file
  `.maestro/state.lock` (e.g. `flock`/`O_EXCL` create-and-hold). Lock acquisition
  timeout: 5s, then error with a clear message.
- Write pattern MUST be **read → modify → write-temp → fsync → atomic rename**
  over `state.json` to avoid torn writes.
- A writer MUST re-read the file under lock immediately before modifying (do not
  trust a copy read before the lock was held).

### 1.8 Validation rules (load time)

On load, Maestro MUST validate and, on violation, refuse to mutate (read-only
commands MAY proceed with a warning):
- `version == 1`.
- Every `id` matches the id regex and is unique.
- Every `branch` is non-empty.
- `worktree_path` is absolute.
- `status` ∈ the enum.
- `run.log_path` is relative (no leading `/`, no `..` escaping `.maestro/logs`).
- Timestamps parse as RFC3339.
A malformed `state.json` MUST NOT be silently overwritten; Maestro SHOULD back it
up to `state.json.corrupt-<timestamp>` before recreating, and tell the user.

---

## Part 2 — Built-in Agent Recipes

### 2.1 Recipe contract (recap, normative for v0.1)

A recipe resolves to: `command` + interpolated `args`, spawned with `cwd =
worktree_path`, environment = inherited ⊕ `env`, and the prompt delivered via
`prompt_via`.

Interpolation tokens (only these exist in v0.1):

| Token | Expands to |
|---|---|
| `{prompt}` | The full prompt text (single argv element). Only valid when `prompt_via = "arg"`. |
| `{prompt_file}` | Absolute path to the prompt file Maestro materialized (`.maestro/logs/<id>/prompt.txt`). Only valid when `prompt_via = "file"`. |
| `{branch}` | The workspace branch name. |
| `{workspace}` | The workspace `id`. |
| `{base}` | The base ref string. |
| `{worktree}` | Absolute worktree path. |

`prompt_via` values:
- `arg` — the recipe `args` MUST contain exactly one `{prompt}`; Maestro substitutes the text.
- `file` — Maestro materializes the prompt to a temp file and the recipe references `{prompt_file}`.
- `stdin` — Maestro pipes the prompt bytes to the child's stdin; `args` MUST NOT reference `{prompt}`/`{prompt_file}`.

Validation: exactly one delivery mechanism MUST be satisfiable. A recipe that
declares `prompt_via = "arg"` but has zero or multiple `{prompt}` tokens is
invalid and MUST fail fast at run time with a clear message.

> **Implementer note on flags:** the `command`/`args` below target each agent's
> documented **non-interactive** mode. Exact flags drift between agent versions.
> These ship as *defaults the user can override in config* — they are data, not
> code. The implementer MUST NOT hardcode them in Go/Rust; load them from an
> embedded default config that the user config overrides. Fields marked
> **VERIFY** should be confirmed against the locally installed agent version
> during M1, and the chosen value recorded.

### 2.2 Default recipes (shipped TOML)

```toml
# ---- Claude Code -------------------------------------------------------------
[agents.claude]
command    = "claude"
args       = ["-p", "{prompt}"]   # -p / --print = non-interactive print mode
prompt_via = "arg"
# Notes:
#  - Headless flag: -p (a.k.a. --print). VERIFY on installed version.
#  - For UNATTENDED edits the user may add a permission flag, e.g.
#    "--permission-mode", "acceptEdits"  (or --dangerously-skip-permissions).
#    NOT enabled by default — see §2.4 safety.
#  - Model override: append "--model", "<id>" via config or `run -- --model ...`.

# ---- Codex CLI (OpenAI) ------------------------------------------------------
[agents.codex]
command    = "codex"
args       = ["exec", "{prompt}"]  # `exec` subcommand = non-interactive run
prompt_via = "arg"
# Notes:
#  - `codex exec <prompt>` runs headless. VERIFY subcommand name/flags.
#  - Codex checks for a git repo; the worktree IS a git repo, so OK. If a future
#    case needs it, "--skip-git-repo-check" can be added via config.
#  - For full automation the user may add "--full-auto" (or the bypass flag).
#    NOT default — see §2.4.

# ---- Gemini CLI (Google) -----------------------------------------------------
[agents.gemini]
command    = "gemini"
args       = ["--prompt", "{prompt}"]  # -p / --prompt = non-interactive
prompt_via = "arg"
# Notes:
#  - Headless flag: -p / --prompt. VERIFY. Gemini also accepts a piped prompt on
#    stdin, so `prompt_via = "stdin"` with args=[] is a valid alternative recipe.
#  - Auto-approve edits: "--yolo" (-y). NOT default — see §2.4.

# ---- Kimi (Kimi CLI / Kimi-Code, Moonshot) -----------------------------------
[agents.kimi]
command    = "kimi"
args       = ["--prompt", "{prompt}"]
prompt_via = "arg"
# Notes:
#  - **VERIFY HEAVILY.** Kimi's CLI surface is the least standardized of the four;
#    confirm the binary name (`kimi` vs `kimi-code`), the non-interactive flag,
#    and whether it prefers stdin. If it reads stdin, prefer:
#       command = "kimi"; args = []; prompt_via = "stdin"
#  - Treat this recipe as a starting template to be corrected during M1.
```

### 2.3 Per-agent summary table

| Agent | Binary (default) | Non-interactive invocation | `prompt_via` | Confidence | Must VERIFY |
|---|---|---|---|---|---|
| Claude Code | `claude` | `claude -p "<prompt>"` | `arg` | High | permission/auto-edit flag for unattended use |
| Codex CLI | `codex` | `codex exec "<prompt>"` | `arg` | High | `exec` flags; auto-approve flag |
| Gemini CLI | `gemini` | `gemini --prompt "<prompt>"` | `arg` (or `stdin`) | Med-High | `-p` vs stdin; `--yolo` |
| Kimi | `kimi` | `kimi --prompt "<prompt>"` | `arg`/`stdin` | Low | binary name + headless flag + stdin pref |

### 2.4 Safety stance on auto-approval flags

v0.1 default recipes MUST NOT enable destructive auto-approval/"yolo"/skip-permission
flags. Reasons: agents run with the user's full local privileges and credentials,
and Maestro provides no sandbox (per design doc non-goals). Users who want fully
unattended runs opt in explicitly by adding the flag in their config or via
`run ... -- <extra flags>`. The default behavior is whatever each agent does in
its standard non-interactive mode.

### 2.5 Recipe validation rules

At config load (and again at `run`), each used recipe MUST satisfy:
1. `command` is a non-empty string and resolvable on `PATH` (checked at `run`;
   a missing binary → fail before creating a worktree).
2. `prompt_via` ∈ {`arg`, `file`, `stdin`}.
3. Token/`prompt_via` consistency (§2.1).
4. `args` is an array of strings; every interpolation token used is in the
   allowed set; unknown tokens (e.g. `{foo}`) → error.
5. `env`, if present, is a flat table of string→string.
6. `setup`, if present, is a non-empty string (run via the user's shell, see
   Part 3 §3.3).

Failure → `run` exits non-zero with the offending recipe field named, and **no
worktree is created**.

---

## Part 3 — Git Worktree Lifecycle Spec

All git operations shell out to the system `git` (no libgit2 in v0.1). Maestro
MUST capture git stderr and surface it on failure. Maestro MUST run git commands
with explicit `-C <repo-root>` (or `-C <worktree>` as noted) rather than relying
on cwd.

### 3.1 Preconditions (all commands)

- Invocation cwd MUST be inside a git repository. `git rev-parse --show-toplevel`
  failing → Maestro exits with "not a git repository".
- The repo MUST have at least one commit (worktrees need a base commit). A repo
  with no commits → error on `run`.
- Maestro does **not** require the main working tree to be clean. `git worktree
  add` operates regardless of uncommitted changes in other worktrees. (This
  supersedes any earlier suggestion of a clean-tree requirement.)

### 3.2 `run` — create worktree + launch (happy path)

Ordered steps. Each numbered step that fails aborts the operation and rolls back
prior steps where noted.

1. **Resolve repo root.** `git rev-parse --show-toplevel`.
2. **Load + validate config and the selected recipe** (Part 2 §2.5). Fail fast.
3. **Resolve prompt bytes.** From `--prompt <file>` or `--prompt -` (stdin).
   Compute SHA-256. (Do not write to disk yet.)
4. **Derive `id`** (§1.4). Under the state lock, ensure `id` is unique and the
   branch is not already present (`git rev-parse --verify --quiet refs/heads/<branch>`
   returns empty). Collisions → error (suggest `--name` / different `--branch`).
5. **Resolve base.** `git rev-parse --verify <base>^{commit}` → full SHA. Default
   `--base` is `HEAD`. Unresolvable ref → error.
6. **Compute worktree path:** `<repo-root>/<defaults.worktree_dir>/<id>`
   (default `.maestro/worktrees/<id>`). The path MUST NOT already exist; if it
   does → error.
7. **Create the worktree + branch:**
   `git -C <repo-root> worktree add -b <branch> <worktree_path> <base-sha>`.
   On failure → abort (nothing to roll back yet). On success continue.
8. **Materialize prompt** to `.maestro/logs/<id>/prompt.txt` and create the log
   dir. Write the state entry with `status = running`, `run = null` → then spawn.
9. **Run setup hook** (if recipe `setup` set), see §3.3. Setup failure →
   `status = failed`, do NOT spawn the agent, leave the worktree in place for
   inspection, exit non-zero.
10. **Spawn the agent** (Part 2 resolution), cwd = `worktree_path`, stdout+stderr
    → log file (tee to terminal when foreground), prompt via `prompt_via`. Record
    `run` (pid, started_at, log_path, command). Persist state under lock.
11. **Supervise / detach:**
    - Foreground (default): stream/tee output, `wait()` for exit, write exit
      sentinel, set terminal status + `exit_code` + `ended_at`, persist. Maestro's
      own exit code mirrors the agent's.
    - `--detach`: double-fork the supervisor, which owns the child, writes the
      exit sentinel on exit, and updates state under lock. The `run` command
      returns immediately after printing the `id`.

Rollback rule: if step 7 succeeds but a later step (8–10) fails *before* a
successful spawn, Maestro SHOULD remove the just-created worktree and branch
(§3.6 logic) so a failed `run` leaves no orphan — UNLESS the failure is the setup
hook (step 9), where the worktree is intentionally preserved for debugging.

### 3.3 Setup hook

- Executed once, after worktree creation, before agent spawn.
- Run via the user's shell: `sh -c "<setup>"` (or `$SHELL -c`), cwd =
  `worktree_path`, env inherited.
- Output appended to the same log file, clearly delimited
  (`==== maestro setup ====`).
- Non-zero exit → `run` fails per step 9.
- Timeout: none in v0.1 (document that a hanging setup hangs `run`; user can
  Ctrl-C, which triggers rollback per §3.2 rollback rule).

### 3.4 `ls`

1. Acquire state lock (shared/exclusive — exclusive is fine for v0.1).
2. Reconcile every workspace (§1.6); persist any status changes.
3. Cross-check against `git -C <repo-root> worktree list --porcelain`:
   - A workspace in state whose `worktree_path` is absent from git's list AND
     absent on disk → status `orphaned`.
   - A worktree present in git but absent from state (created out-of-band) → NOT
     shown as a Maestro workspace (Maestro only manages what it created); MAY be
     surfaced under a separate "untracked worktrees" note. Not required for v0.1.
4. Print a table: `ID  BRANCH  AGENT  STATUS  STARTED  DURATION  LOG`. `--json`
   prints the reconciled workspace array verbatim.

### 3.5 `logs`, `open`, `stop`

**`logs <id> [-f] [-n N]`**
- Resolve `id` in state (error if unknown).
- Print the contents of `run.log_path`. `-n N` limits to last N lines. `-f`
  follows (tail) the file; following SHOULD continue until the workspace reaches
  a terminal status or the user interrupts.
- If the log file is missing → error explaining it may have been removed.

**`open <id>`**
- Resolve `id`; print the absolute `worktree_path` to stdout and exit 0. This is
  the canonical form (enables `cd "$(maestro open <id>)"`). No subshell spawning
  in v0.1.
- Unknown `id` or missing worktree dir → error, non-zero exit.

**`stop <id>`**
- Resolve `id`; reconcile.
- If status ≠ `running` → no-op with an informational message, exit 0.
- If running: send `SIGTERM` to `run.pid`. Wait a grace period (default **10s**).
  If still alive → `SIGKILL`. (v0.1 MAY target only the direct child; process-group
  termination is preferred where available to catch descendants.)
- On termination, write/normalize the exit sentinel (`128+SIGTERM` or `128+SIGKILL`
  if no clean code), set `status = stopped`, `ended_at = now`, `pid = null`,
  persist.

### 3.6 `rm` — teardown

`rm <id> [--force] [--delete-branch]`

> **Branch-deletion default (supersedes design doc's `--keep-branch` sketch):**
> v0.1 `rm` **preserves the branch by default** (agent work is not destroyed).
> Branch deletion is opt-in via `--delete-branch`.

Steps:
1. Resolve `id`; reconcile.
2. **Running guard:** if status == `running` → refuse unless `--force`. With
   `--force`, perform `stop` semantics (§3.5) first, then continue.
3. **Dirty guard:** check the worktree for uncommitted changes
   (`git -C <worktree_path> status --porcelain`). If non-empty → refuse unless
   `--force` (warn that uncommitted work will be lost).
4. **Remove worktree:**
   `git -C <repo-root> worktree remove <worktree_path>` (add `--force` when the
   user passed `--force`, e.g. for dirty/locked worktrees).
5. **Prune admin state:** `git -C <repo-root> worktree prune`.
6. **Branch:** if `--delete-branch`, run `git -C <repo-root> branch -d <branch>`
   (safe delete; fails if unmerged). With `--force`, use `-D` (force delete).
   Without `--delete-branch`, leave the branch intact.
7. **State:** remove the workspace entry from `state.json` (under lock). Log
   files under `.maestro/logs/<id>/` are retained by default in v0.1 (cheap, aids
   post-mortem); `--force` MAY also delete them. (Document this.)

### 3.7 Edge cases (normative handling)

| # | Situation | Required behavior |
|---|---|---|
| E1 | Branch name already exists (`run`) | Error before worktree creation; suggest a different `--branch`/`--name`. No partial state. |
| E2 | Worktree path already exists on disk (`run`) | Error; do not touch the existing dir. |
| E3 | `id` collides with existing workspace (`run`) | Error; instruct `--name`. |
| E4 | Base ref invalid/unresolvable (`run`) | Error at step 5; nothing created. |
| E5 | Repo has zero commits (`run`) | Error: "repository has no commits to branch from". |
| E6 | Dirty main/other worktree (`run`) | Allowed; no clean-tree requirement (§3.1). |
| E7 | User deleted the worktree dir manually | Reconcile → `orphaned`. `rm` cleans state and runs `git worktree prune`. `rm` MUST tolerate a missing worktree dir (skip step 4 if absent, still prune + drop state). |
| E8 | User deleted the branch manually | `rm` step 6 (`branch -d`) MAY report "branch not found"; treat as non-fatal. |
| E9 | Maestro/machine crashed mid-run | Reconcile: pid dead + sentinel present → terminal status from code; sentinel absent → `orphaned`. |
| E10 | PID reused by OS | Best-effort start-time guard (§1.6.1); if unconfirmable, prefer sentinel/`orphaned` over false "running". |
| E11 | Worktree is `locked` (git) | `git worktree remove` fails; instruct user, or `--force` to override. |
| E12 | Disk full while writing log/state | Abort the current command with the git/IO error; state writes are atomic so the prior state is intact. |
| E13 | Two `run`s race for the same `id`/branch | State lock + the branch-exists/path-exists checks serialize them; the loser errors per E1/E3. |
| E14 | `setup` hook hangs | `run` blocks; Ctrl-C → rollback per §3.2 (remove worktree+branch). No automatic timeout in v0.1. |
| E15 | Agent binary not on PATH | Detected at step 2/10 before/at spawn; if before worktree creation, nothing created; if the recipe was valid but binary vanished, fail spawn → `status = failed`, worktree preserved. |
| E16 | `.maestro/state.json` corrupt | Back up to `state.json.corrupt-<ts>`, recreate empty, warn (§1.8). Existing worktrees become invisible to Maestro until manually reconciled — document `git worktree list` as the recovery aid. |
| E17 | Nested repo / submodule cwd | Use `git rev-parse --show-toplevel`; operate on the resolved root. Submodule worktrees are out of scope for v0.1. |

### 3.8 Worked example (end-to-end)

```
$ maestro run --agent claude --branch task/add-tests --prompt ./prompts/tests.md
# steps 1-7: creates branch task/add-tests + worktree .maestro/worktrees/add-tests
# step 8-10: materializes prompt, spawns `claude -p "<prompt bytes>"` (cwd=worktree)
# foreground: streams logs, waits, records exit
created workspace: add-tests   (branch task/add-tests)

$ maestro ls
ID         BRANCH           AGENT   STATUS     STARTED               DURATION
add-tests  task/add-tests   claude  running    2026-06-16T10:03:42Z  00:00:18

$ maestro logs add-tests -f
...agent output...

$ cd "$(maestro open add-tests)" && git diff

$ maestro stop add-tests        # if still running
stopped workspace: add-tests

$ maestro rm add-tests          # preserves branch by default
removed worktree for add-tests (branch task/add-tests kept)

$ maestro rm add-tests --delete-branch
removed worktree and branch task/add-tests
```

Resulting state entry for a completed foreground run:

```json
{
  "id": "add-tests",
  "branch": "task/add-tests",
  "base": { "ref": "HEAD", "sha": "9fceb02de2e3c0e1..." },
  "worktree_path": "/Users/dev/repo/.maestro/worktrees/add-tests",
  "agent": "claude",
  "prompt": { "source": "file", "ref": "./prompts/tests.md", "sha256": "ab12cd..." },
  "status": "completed",
  "run": {
    "pid": null,
    "detached": false,
    "started_at": "2026-06-16T10:03:42Z",
    "ended_at": "2026-06-16T10:07:09Z",
    "exit_code": 0,
    "log_path": ".maestro/logs/add-tests/2026-06-16T10-03-42Z.log",
    "command": ["claude", "-p", "<prompt>"]
  },
  "created_at": "2026-06-16T10:03:41Z",
  "updated_at": "2026-06-16T10:07:09Z",
  "maestro_version": "0.1.0"
}
```

---

## Part 4 — v0.1 Done Criteria (checklist)

- [ ] `state.json` read/write with locking, atomic writes, schema validation (Part 1).
- [ ] Reconcile-on-read with sentinel + liveness logic (§1.6).
- [ ] Embedded default recipes for claude/codex/gemini/kimi, overridable via config (Part 2).
- [ ] Recipe validation + PATH check before worktree creation (§2.5).
- [ ] `run` full lifecycle incl. rollback and setup hook (§3.2–3.3).
- [ ] `ls`, `logs (-f/-n)`, `open`, `stop` (SIGTERM→SIGKILL), `rm` (guards + `--delete-branch`).
- [ ] All E1–E17 edge cases handled as specified.
- [ ] Foreground exit code mirrored; `--detach` writes sentinel via supervisor.

> Anything not listed here is out of v0.1 scope (see design doc §10).
