# Codex Builder Brief — v0.1 fixes

> **Status: completed (2026-06-17).** All blockers (B0–B5) and optional hardening (B6) were
> implemented and merged to `main` via PRs #14–#17. This brief is kept for traceability only.
> Current status: [`v0.1-status-report.md`](v0.1-status-report.md).

> Author: Claude Staff Engineer · Date: 2026-06-17
> Pairs with: PR #13 (review findings) + Decision 0001 (custom dirs)
> Base branch: `main` · Open the implementation PR with `gh pr create --base main`

This is the actionable handoff for the Codex Builder. It consolidates the v0.1
**blockers** from the Sonnet Reviewer (PR #13) with the Staff Engineer rulings, in
the order they should be executed. Each item lists the file, the fix, and the
acceptance test. Do not expand scope beyond what is listed; flag anything ambiguous
back to the Staff Engineer before implementing.

## Order of execution

Spec first (so behavior is normative before code), then blocking code fixes, then
the optional hardening.

---

### B0 — Spec: apply fail-closed corrupt-state wording  *(blocker, spec)*

**File:** `docs/maestro-v0.1-spec.md` — §1.8 final paragraph + the E16 row.
**Source of truth:** PR #4 §1 (the fail-closed change only — **not** the reserved-dir change, see Decision 0001).

- Replace the §1.8 "back up + recreate" paragraph with the fail-closed wording:
  every command (read + mutating) aborts non-zero on an invalid state file; Maestro
  never auto-backs-up, truncates, or recreates it; message names the file and points
  to `git worktree list` for recovery.
- Update the E16 row to match.

**Acceptance:** spec no longer contains "back up" / "recreate empty" for state.json.

> Note for Manager (D2 in Decision 0001): PR #4 must be split — merge §1 (this), drop §2 (reserved dir keys).

---

### B1 — H1: `reconcile_state()` orphans terminal workspaces  *(blocker)*

**File:** `maestro_cli/cli.py:422–432`
**Bug:** the worktree-missing → `orphaned` transition runs before the
`status != "running"` guard, so `completed`/`failed`/`stopped` workspaces get
reclassified as `orphaned` when their worktree is removed.
**Fix:** only apply the orphaned transition to `running` workspaces; terminal-status
workspaces keep their status regardless of worktree presence (spec §1.6).
**Test:** `test_reconcile_does_not_orphan_completed_workspace_on_missing_worktree`.

---

### B2 — #6 / B0: corrupt state must fail closed  *(blocker)*

**File:** `maestro_cli/cli.py` — the state read path (`repair_corrupt` usage).
**Bug:** mutating commands currently recover/recreate on corrupt state; `ls` differs.
**Fix:** per the §1.8 wording from B0, **all** commands abort non-zero on invalid
state. No auto-backup/truncate/recreate. Make the read path uniform.
**Test:** corrupt `state.json` → every command (`ls`, `run`, `logs`, `open`, `stop`,
`rm`) exits non-zero with the actionable message and mutates nothing on disk.

---

### B3 — #5: Ctrl-C during `run` does not roll back  *(blocker)*

**File:** `maestro_cli/cli.py` — `cmd_run` rollback path.
**Bug:** `except Exception` misses `BaseException` (e.g. `KeyboardInterrupt`), so a
Ctrl-C between worktree creation and spawn leaves an orphaned worktree+branch.
**Fix:** ensure the worktree/branch rollback also fires on `KeyboardInterrupt`
(catch `BaseException` or add a `finally`/`except (Exception, KeyboardInterrupt)`),
per spec §3.2 / E14.
**Test:** simulate interrupt after `git worktree add`, assert worktree+branch removed.

---

### B4 — #8: race + rollback clobbers the winner  *(blocker)*

**File:** `maestro_cli/cli.py` — the lock gap between the existence checks and
`git worktree add`.
**Bug:** the branch-exists / path-exists check and the `git worktree add` are not
serialized under the same lock, so a losing `run` can roll back and delete the
winner's freshly created worktree/branch.
**Fix:** hold the state lock across the check→create→persist window (spec §3.2 +
E13) so concurrent `run`s serialize; the loser must error without touching the
winner's resources.
**Test:** two concurrent `run`s for the same id/branch → one succeeds, the other
exits non-zero, winner's worktree+branch+state survive intact.

---

### B5 — M1: `defaults.agent` is dead code  *(blocker per review recommendation)*

**File:** `maestro_cli/cli.py:705, 1002`
**Bug:** `--agent` is `required=True`, so the `args.agent or config defaults.agent`
fallback at line 705 is unreachable; the documented `defaults.agent` does nothing.
**Fix:** make `--agent` not required at the argparse layer; keep the fallback;
error clearly only if neither `--agent` nor `defaults.agent` is present.
**Test:** `test_defaults_agent_config_is_used_when_agent_not_passed`.

---

### B6 — Hardening of `relative_config_dir`  *(optional, low — Decision 0001 D4)*

**File:** `maestro_cli/cli.py:511–515`
**Context:** Decision 0001 keeps custom `worktree_dir`/`log_dir` (Issue #7 is
outdated). The repo-root boundary is already closed (absolute + `..` rejected).
**Optional fix:** also reject dirs that resolve inside `.git`, and reject
`worktree_dir == log_dir`.
**Test:** custom dir pointing into `.git` or equal worktree/log dirs → `run` aborts.
**Do NOT** add any "custom dir not supported" reservation — that is explicitly
rejected by Decision 0001.

---

## Out of scope for this brief

- **Issue #7 reserved keys** — rejected (Decision 0001). Keep
  `test_custom_worktree_and_log_dirs_are_supported` as the regression guard.
- **Issue #9 (`_supervise` hiding)** — already handled; `--help` hides it
  (`test_supervise_is_hidden_from_public_help` passes). Reviewer/Manager call, no code.
- **L1–L4** and the broader missing-test list (T1–T18) — backlog, not v0.1 blockers.

## Definition of done

- All new tests above pass; existing 14 tests still pass.
- `docs/maestro-v0.1-spec.md` reflects the fail-closed §1.8/E16.
- PR opened with `--base main`, summarizing what changed + test results.
