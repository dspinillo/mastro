# Decision 0001 — Custom `worktree_dir` / `log_dir` in v0.1

> Author: Claude Staff Engineer · Date: 2026-06-17
> Status: **Accepted**
> Resolves: review-v0.1-findings.md "Open Question for Claude Staff Engineer"
> Affects: Issue #7, PR #4 (spec), spec §1.1 / §1.8 / §3.2

## Question

The Sonnet Reviewer flagged a contradiction: `test_custom_worktree_and_log_dirs_are_supported`
**passes** (custom dirs work end-to-end), but Issue #7 + PR #4 propose **reserving**
`defaults.worktree_dir` / `defaults.log_dir` in v0.1 (any non-default value must abort `run`).
Two mutually exclusive options:

- **(a)** Reserve the keys → disable the feature, add an error-on-custom-dir test, close #7 WONTFIX.
- **(b)** Keep the feature → close #7 as outdated/invalid.

## Decision

**(b) Keep the feature. Custom `worktree_dir` / `log_dir` remain supported in v0.1.**

## Rationale

1. **Issue #7 and PR #4 describe code that no longer exists.** Both were written against the
   gap-analysis snapshot. Issue #7's two cited root causes are now false in the current tree:
   - It claims `validate_state` hardcodes `log_path.startswith(".maestro/logs/")`. The code
     (`cli.py:380–382`) instead enforces the generic invariant "relative + no `..`". No fixed prefix.
   - It claims `build_process` hardcodes the prompt-file path. The code derives `prompt_file`
     from the configured `log_dir` (`materialize_log_paths`, then `cli.py:549` / `734`).
   The feature was repaired between the gap analysis and PR #2/#3. The dispute is stale.

2. **The security premise of PR #4 ("traversal-prone") is already addressed.** `relative_config_dir`
   (`cli.py:511–515`) rejects absolute paths and any `..` component for *both* directories at
   config-parse time. The repo-root boundary is closed. `config.toml` is user-authored and
   committable (not untrusted input), so a user setting a self-defeating value is foot-shooting,
   not a security-boundary violation.

3. **The §1.8 containment invariant does not need a fixed prefix.** It is enforced generically
   (`log_path` relative, no `..`). A custom `log_dir = "custom/logs"` yields
   `custom/logs/<id>/<slug>.log` — relative, no escape. PR #4's claim that a fixed `.maestro/logs`
   prefix is "what makes the invariant trivially enforceable" is no longer accurate.

4. **Reserving is net-negative engineering.** It means writing a guard *plus* a test to *disable*
   functionality that already works, is tested, and is traversal-guarded — with no correctness or
   security gain. v0.1 scope discipline is a real value, but it does not justify removing a
   coherent, covered feature.

## Consequences / actions

PR #4 bundles **two** normative changes. They must be **split**:

- ✅ **Keep** PR #4 §1 — corrupt/invalid `state.json` → *fail closed* (all commands abort,
  no auto-backup/recreate). This is correct, aligns with Issue #6, and is the normative basis for
  that fix. The spec on this branch still has the **old** §1.8 ("back up + recreate") and the old
  E16 row — they must be updated to the fail-closed wording.
- ❌ **Drop** PR #4 §2 — the reserved-keys changes to §1.1, §3.2 step 6, and the design-doc
  config annotation. Do **not** merge them.

## Residual hardening (optional, non-blocking for v0.1)

`relative_config_dir` closes repo-escape but still allows in-repo footguns
(e.g. `log_dir = ".git/..."` or `worktree_dir == log_dir`). Track as a low-severity follow-up,
not a v0.1 blocker. The decision to keep the feature does not depend on it.

## Actionable tasks

| # | Owner | Task |
|---|-------|------|
| D1 | Codex Manager | Close **Issue #7** as *outdated* — both cited hardcodes are gone; feature works + is tested. Link this decision. |
| D2 | Codex Manager | Amend/split **PR #4**: drop the reserved-keys edits (§1.1 limitation bullet, §3.2 step 6, design-doc annotation); keep only the fail-closed §1.8 + E16 edits. |
| D3 | Codex Builder | Apply the fail-closed §1.8/E16 spec wording to `docs/maestro-v0.1-spec.md` on the working branch (currently still says "back up + recreate"), then implement the §1.6 read-path fix per Issue #6. |
| D4 | Codex Builder | (Optional, low) Add an in-repo containment check to `relative_config_dir` rejecting dirs that resolve inside `.git` and reject `worktree_dir == log_dir`. Add a test. |
| D5 | Sonnet Reviewer | Keep `test_custom_worktree_and_log_dirs_are_supported` as the regression guard for this decision. Drop test item T-"error-on-custom-dir" — not applicable under (b). |

## Note on Issue #9 (out of scope here)

The Reviewer's "mechanism wrong / severity high" note on #9 (`_supervise` hiding) is a
review-severity call, not an architecture decision. Leaving it to the Reviewer/Manager; no
Staff Engineer ruling required. The current `--help` already hides `_supervise`
(`test_supervise_is_hidden_from_public_help` passes).
