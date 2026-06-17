# Maestro v0.1 — Code Review Findings

> Reviewer: Claude Sonnet Reviewer · Date: 2026-06-17  
> Branch reviewed: `maestro-cli-architecture` (post PR #2 + #3)  
> Spec: `docs/maestro-v0.1-spec.md`  
> Tests run: 14 passed, 0 failed

---

## Test Results

```
Ran 14 tests in 6.361s — OK
```

All existing tests pass. No regressions.

---

## Findings by Severity

### High

#### H1 — `reconcile_state()` orphans completed/failed/stopped workspaces on missing worktree

**File:** `maestro_cli/cli.py:422–432`  
**Spec:** §1.6 — reconciliation is defined only for `running` workspaces

The worktree-missing → `orphaned` transition runs before the `status != "running"` guard,
so any workspace in a terminal state (`completed`, `failed`, `stopped`) whose worktree
is deleted manually gets incorrectly reclassified as `orphaned`.

```python
# Bug: worktree check fires for ALL statuses
if not Path(ws["worktree_path"]).exists():
    if ws["status"] == "orphaned":
        continue
    ws["status"] = "orphaned"   # ← should only apply to "running"
    ...
    continue
if ws.get("status") != "running" or not run:   # guard comes too late
    continue
```

**Expected:** terminal-status workspaces retain their status regardless of worktree presence.  
**Test needed:** `test_reconcile_does_not_orphan_completed_workspace_on_missing_worktree`

---

### Medium

#### M1 — `defaults.agent` config key is dead code; `--agent` is always required

**File:** `maestro_cli/cli.py:705, 1002`

`--agent` is declared `required=True` in argparse (line 1002), so argparse aborts
before reaching `cmd_run`. The fallback `args.agent or config.get("defaults", {}).get("agent")`
at line 705 is unreachable. The documented `defaults.agent` config key does nothing.

**Fix:** change `required=False` (or omit `required=True`) and keep the fallback.  
**Test needed:** `test_defaults_agent_config_is_used_when_agent_not_passed`

#### M2 — Stale lock file after SIGKILL blocks all future maestro operations

**File:** `maestro_cli/cli.py:291–313`

The lock file is released in a `finally` block, which handles normal exit,
exceptions, and `KeyboardInterrupt`. But `SIGKILL` bypasses `finally`. A
`kill -9 <maestro-pid>` leaves `.maestro/state.lock` on disk. Every subsequent
`maestro` command spins for 5 seconds and exits with "timed out acquiring state
lock". The lock file already contains the holder's PID (line 298); recovery is
possible by checking if that PID is still alive before spinning.

---

### Low

#### L1 — `stop_workspace()` with `pid=None` writes sentinel without killing anything

**File:** `maestro_cli/cli.py:917–930`

During the window between state creation (`pid=null`) and `set_pid()`, a `stop`
call finds `pid=None`, skips the kill, but still writes the sentinel with
`128+SIGTERM` and marks the workspace `stopped`. No process was actually signaled.

#### L2 — `locked_state()` is defined but never called (dead code)

**File:** `maestro_cli/cli.py:462–465`

The function `locked_state(root, mutate)` has no callers. Safe to remove.

#### L3 — Detach polling timeout leaves `ws` stale for `--json` output

**File:** `maestro_cli/cli.py:793–803`

If the spawned supervisor doesn't set the PID within 5 seconds, the polling
loop exits without updating `ws`. `run --detach --json` then prints the
pre-spawn workspace object (`run.pid=null`, `run.command=[]`).

#### L4 — Reconcile changes in `cmd_run` first lock block are never persisted

**File:** `maestro_cli/cli.py:724–730`

`cmd_run` calls `reconcile_state()` under a lock but never calls `store.write(state)`.
All other commands persist reconcile changes with `if changed: store.write(state)`.

---

## Existing Issues (already filed #5–#10) — status after review

| Issue | Confirmed? | Notes |
|-------|-----------|-------|
| #5 — Ctrl-C no rollback | ✓ Confirmed | `except Exception` misses `BaseException` |
| #6 — Corrupt state wiped on mutating commands | ✓ Confirmed | All commands except `ls` use `repair_corrupt=True` |
| #7 — Custom `log_dir` breaks run | ⚠ Disputed | `test_custom_worktree_and_log_dirs_are_supported` **passes**. Custom dirs work end-to-end in current code. Issue may reflect a spec-level decision (reserving keys in v0.1) that diverged from the implementation. Needs Staff Engineer decision. |
| #8 — Race + rollback clobbers winner | ✓ Confirmed | Lock gap between check and `git worktree add` is real |
| #9 — `_supervise` in public help | ⚠ Mechanism wrong | `_supervise` is NOT a registered subparser. It's handled by early-dispatch in `main()` (line 1041). `--help` already hides it. Test passes. Concern is real but severity Medium is high. |
| #10 — Low findings + test gaps | ✓ Confirmed | F7/F8/F9 valid; F10 test gap list is correct but incomplete (misses H1/M1/M2) |

---

## Missing Test Coverage (normative edge cases with no test)

| # | Scenario | Spec ref |
|---|----------|---------|
| T1 | Worktree path already exists on disk | E2 |
| T2 | `--name` ID collision | E3 |
| T3 | Invalid `--base` ref | E4 |
| T4 | Repo with zero commits | E5 |
| T5 | `rm` when branch deleted manually | E8 |
| T6 | Crash recovery via sentinel (pid dead, sentinel present) | E9 |
| T7 | Setup hook failure (status=failed, worktree preserved) | §3.2 step 9 |
| T8 | `rm --force` on running workspace | §3.6 step 2 |
| T9 | `rm --force` on dirty worktree | §3.6 step 3 |
| T10 | `stop` on completed/orphaned → exit 0 + info message | §3.5 |
| T11 | `ls --status` filter | §3.4 |
| T12 | State `version != 1` → refuse to operate | §1.8 |
| T13 | `prompt_via = "file"` recipe with `{prompt_file}` | §2.1 |
| T14 | `logs -f` terminates when workspace reaches terminal status | §3.5 |
| T15 | Invalid workspace ID in `logs`, `open`, `stop`, `rm` | §3.5 |
| T16 | `--name` override | §1.4 |
| T17 | `defaults.agent` fallback (after M1 fix) | §6 |
| T18 | `reconcile_state` does not orphan completed workspace (H1) | §1.6 |

---

## Open Question for Claude Staff Engineer

**Issue #7 vs. passing test:** `test_custom_worktree_and_log_dirs_are_supported` demonstrates
that custom `log_dir`/`worktree_dir` works correctly in the current code. Issue #7 proposes
reserving these keys in v0.1 (based on "spec PR #4"). Staff Engineer needs to decide:

- (a) Reserve the keys → close the feature test, add error-on-custom-dir test, and close #7 as WONTFIX.
- (b) Keep the feature → close #7 as invalid/outdated.

---

## Recommendation: Request Changes

The happy path is solid (14/14 tests pass). Four issues block v0.1 completeness:
**H1** (reconcile wrongly orphaning terminal workspaces), **#5** (Ctrl-C rollback),
**#6** (corrupt state handling), and **#8** (concurrency race + rollback clobber).
**M1** (`defaults.agent` non-functional) is a documented feature that is silently broken.
All five should be fixed before tagging v0.1.
