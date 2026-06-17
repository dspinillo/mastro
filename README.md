# Maestro CLI

Terminal-first orchestrator for running CLI coding agents in parallel across isolated
[git worktrees](https://git-scm.com/docs/git-worktree).

**v0.1 commands:** `run`, `ls`, `logs`, `open`, `stop`, `rm`

## Quick start

**Prerequisites:** git, Python ≥ 3.10, and at least one agent CLI on `PATH`
(e.g. `claude`, `codex`) — or a [fake agent for local testing](docs/maestro-guide.md#1-fake-agent-local-testing-no-api-keys).

```sh
# From repo root — development invocation
export PYTHONPATH="$(pwd)"

# Inside any git repo with ≥ 1 commit
python3 -m maestro_cli.cli run \
  --agent claude \
  --branch task/my-feature \
  --prompt ./prompts/task.md

python3 -m maestro_cli.cli ls
python3 -m maestro_cli.cli logs task-my-feature -f
cd "$(python3 -m maestro_cli.cli open task-my-feature)" && git diff
python3 -m maestro_cli.cli rm task-my-feature
```

Install as a command (optional):

```sh
python3 -m pip install .
python3 -m maestro_cli.cli --help   # or `maestro --help` if on PATH
```

Configure agents in `.maestro/config.toml` (committable). See the
[Practical Guide](docs/maestro-guide.md#configure-agents).

## Documentation

| Doc | Audience |
|---|---|
| [**Practical Guide**](docs/maestro-guide.md) | Day-to-day usage, examples, recovery |
| [v0.1 Spec](docs/maestro-v0.1-spec.md) | Normative implementation reference |
| [Design Doc](docs/maestro-design.md) | Architecture, risks, roadmap |

## Tests

```sh
python3 -m pytest -q
# or: python3 -m unittest -v
```

## What Maestro is not

- No sandbox — agents run with your local permissions and credentials
- No auto-approval flags by default — opt in via config or `run ... -- <flags>`
- No PR/commit helpers in v0.1 (planned for v0.2+)

See [Risks and limitations](docs/maestro-guide.md#risks-and-limitations) in the guide.
