from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__

try:
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None


ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TOKEN_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
VALID_TOKENS = {"{prompt}", "{prompt_file}", "{branch}", "{workspace}", "{base}", "{worktree}"}
VALID_STATUSES = {"running", "completed", "failed", "stopped", "orphaned"}
DEFAULT_GITIGNORE_ENTRIES = [".maestro/state.json", ".maestro/logs/", ".maestro/worktrees/"]

DEFAULT_CONFIG: dict[str, Any] = {
    "defaults": {
        "agent": "claude",
        "worktree_dir": ".maestro/worktrees",
        "log_dir": ".maestro/logs",
    },
    "agents": {
        "claude": {"command": "claude", "args": ["-p", "{prompt}"], "prompt_via": "arg"},
        "codex": {"command": "codex", "args": ["exec", "{prompt}"], "prompt_via": "arg"},
        "gemini": {"command": "gemini", "args": ["--prompt", "{prompt}"], "prompt_via": "arg"},
        "kimi": {"command": "kimi", "args": ["--prompt", "{prompt}"], "prompt_via": "arg"},
    },
}


class MaestroError(Exception):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration(start: str | None, end: str | None = None) -> str:
    if not start:
        return "-"
    stop = parse_time(end) if end else dt.datetime.now(dt.timezone.utc)
    seconds = max(0, int((stop - parse_time(start)).total_seconds()))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_cmd(args: list[str], *, cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise MaestroError(detail or f"command failed: {' '.join(args)}")
    return proc


def repo_root() -> Path:
    try:
        return Path(run_cmd(["git", "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    except MaestroError as exc:
        raise MaestroError("not a git repository") from exc


def has_commit(root: Path) -> bool:
    return run_cmd(["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"], check=False).returncode == 0


def branch_exists(root: Path, branch: str) -> bool:
    return run_cmd(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0


def validate_branch(branch: str) -> None:
    if not branch:
        raise MaestroError("branch name is required")
    proc = run_cmd(["git", "check-ref-format", "--branch", branch], check=False)
    if proc.returncode != 0:
        raise MaestroError(f"invalid branch name: {branch}")


def slugify(branch: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", branch.lower()).strip("-._")[:64]
    if not slug:
        raise MaestroError("could not derive a workspace id from branch; pass --name")
    return slug


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if tomllib:
        with path.open("rb") as f:
            return tomllib.load(f)
    return load_simple_toml(path)


def strip_comment(line: str) -> str:
    in_str = False
    esc = False
    out = []
    for ch in line:
        if ch == "\\" and in_str and not esc:
            esc = True
            out.append(ch)
            continue
        if ch == '"' and not esc:
            in_str = not in_str
        if ch == "#" and not in_str:
            break
        out.append(ch)
        esc = False
    return "".join(out).strip()


def split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    esc = False
    for ch in value:
        if ch == "\\" and in_str and not esc:
            esc = True
            buf.append(ch)
            continue
        if ch == '"' and not esc:
            in_str = not in_str
        elif not in_str and ch in "[{":
            depth += 1
        elif not in_str and ch in "]}":
            depth -= 1
        if ch == "," and not in_str and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        esc = False
    if buf:
        parts.append("".join(buf).strip())
    return parts


def parse_toml_value(value: str) -> Any:
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        result: dict[str, str] = {}
        inner = value[1:-1].strip()
        if inner:
            for item in split_top_level(inner):
                key, raw = item.split("=", 1)
                result[key.strip()] = str(parse_toml_value(raw))
        return result
    if value.startswith("[") and value.endswith("]"):
        return [parse_toml_value(item) for item in split_top_level(value[1:-1]) if item]
    if value.startswith('"') and value.endswith('"'):
        return ast.literal_eval(value)
    if value in {"true", "false"}:
        return value == "true"
    return value


def load_simple_toml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current = root
    for raw in path.read_text().splitlines():
        line = strip_comment(raw)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = root
            for part in line[1:-1].split("."):
                current = current.setdefault(part, {})
            continue
        if "=" not in line:
            raise MaestroError(f"unsupported TOML syntax in {path}: {raw}")
        key, value = line.split("=", 1)
        current[key.strip()] = parse_toml_value(value)
    return root


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(root: Path, extra_config: str | None = None) -> dict[str, Any]:
    config = DEFAULT_CONFIG
    for path in [Path.home() / ".maestro" / "config.toml", root / ".maestro" / "config.toml"]:
        config = deep_merge(config, load_toml(path))
    if extra_config:
        config = deep_merge(config, load_toml(Path(extra_config).expanduser()))
    return config


def validate_recipe(name: str, recipe: dict[str, Any], *, check_path: bool = True) -> None:
    command = recipe.get("command")
    args = recipe.get("args", [])
    prompt_via = recipe.get("prompt_via")
    if not isinstance(command, str) or not command:
        raise MaestroError(f"agents.{name}.command must be a non-empty string")
    if check_path and shutil.which(command) is None:
        raise MaestroError(f"agents.{name}.command not found on PATH: {command}")
    if prompt_via not in {"arg", "file", "stdin"}:
        raise MaestroError(f"agents.{name}.prompt_via must be one of arg, file, stdin")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise MaestroError(f"agents.{name}.args must be an array of strings")
    setup = recipe.get("setup")
    if setup is not None and (not isinstance(setup, str) or not setup):
        raise MaestroError(f"agents.{name}.setup must be a non-empty string")
    env = recipe.get("env", {})
    if env and (not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
        raise MaestroError(f"agents.{name}.env must be a string table")
    tokens = [token for arg in args for token in TOKEN_RE.findall(arg)]
    unknown = sorted(set(tokens) - VALID_TOKENS)
    if unknown:
        raise MaestroError(f"agents.{name}.args uses unknown token: {unknown[0]}")
    prompt_count = tokens.count("{prompt}")
    prompt_file_count = tokens.count("{prompt_file}")
    if prompt_via == "arg" and prompt_count != 1:
        raise MaestroError(f"agents.{name}.args must contain exactly one {{prompt}} when prompt_via=arg")
    if prompt_via == "arg" and prompt_file_count:
        raise MaestroError(f"agents.{name}.args must not contain {{prompt_file}} when prompt_via=arg")
    if prompt_via == "file" and prompt_file_count != 1:
        raise MaestroError(f"agents.{name}.args must contain exactly one {{prompt_file}} when prompt_via=file")
    if prompt_via == "file" and prompt_count:
        raise MaestroError(f"agents.{name}.args must not contain {{prompt}} when prompt_via=file")
    if prompt_via == "stdin" and (prompt_count or prompt_file_count):
        raise MaestroError(f"agents.{name}.args must not contain prompt tokens when prompt_via=stdin")


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / ".maestro"
        self.path = self.dir / "state.json"
        self.lock_path = self.dir / "state.lock"
        self._lock_fd: int | None = None

    def ensure_dirs(self) -> None:
        self.dir.mkdir(exist_ok=True)
        (self.root / ".maestro" / "logs").mkdir(exist_ok=True)
        (self.root / ".maestro" / "worktrees").mkdir(exist_ok=True)

    def ensure_gitignore(self, extra_entries: list[str] | None = None) -> None:
        gitignore = self.root / ".gitignore"
        existing = gitignore.read_text().splitlines() if gitignore.exists() else []
        entries = list(dict.fromkeys(DEFAULT_GITIGNORE_ENTRIES + list(extra_entries or [])))
        additions = [entry for entry in entries if entry not in existing]
        if additions:
            with gitignore.open("a") as f:
                if existing and existing[-1] != "":
                    f.write("\n")
                for entry in additions:
                    f.write(f"{entry}\n")

    @contextlib.contextmanager
    def lock(self):
        self.ensure_dirs()
        deadline = time.time() + 5
        while True:
            try:
                self._lock_fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.write(self._lock_fd, str(os.getpid()).encode())
                break
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if time.time() >= deadline:
                    raise MaestroError(f"timed out acquiring state lock: {self.lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()

    def empty_state(self) -> dict[str, Any]:
        now = utcnow()
        return {"version": 1, "updated_at": now, "workspaces": []}

    def read(self, *, repair_corrupt: bool = False) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty_state()
        try:
            state = json.loads(self.path.read_text())
            validate_state(state)
            return state
        except Exception as exc:
            raise MaestroError(
                f"malformed state file: {self.path}; refusing to continue. "
                "Inspect or repair the file manually; use `git worktree list` to recover worktrees."
            ) from exc

    def write(self, state: dict[str, Any]) -> None:
        self.ensure_dirs()
        state["updated_at"] = utcnow()
        validate_state(state)
        fd, tmp_name = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
            dir_fd = os.open(str(self.dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)


def validate_state(state: dict[str, Any]) -> None:
    if state.get("version") != 1:
        raise MaestroError("state file version is unsupported; please upgrade Maestro")
    if not isinstance(state.get("workspaces"), list):
        raise MaestroError("state.workspaces must be an array")
    seen: set[str] = set()
    parse_time(state.get("updated_at", ""))
    for ws in state["workspaces"]:
        wid = ws.get("id")
        if not isinstance(wid, str) or not ID_RE.match(wid):
            raise MaestroError(f"invalid workspace id in state: {wid}")
        if wid in seen:
            raise MaestroError(f"duplicate workspace id in state: {wid}")
        seen.add(wid)
        if not ws.get("branch"):
            raise MaestroError(f"workspace {wid} has empty branch")
        if not Path(ws.get("worktree_path", "")).is_absolute():
            raise MaestroError(f"workspace {wid} worktree_path must be absolute")
        if ws.get("status") not in VALID_STATUSES:
            raise MaestroError(f"workspace {wid} has invalid status")
        parse_time(ws.get("created_at", ""))
        parse_time(ws.get("updated_at", ""))
        run = ws.get("run")
        if run:
            log_path = run.get("log_path", "")
            if not log_path or Path(log_path).is_absolute() or ".." in Path(log_path).parts:
                raise MaestroError(f"workspace {wid} has invalid run.log_path")
            parse_time(run.get("started_at", ""))
            if run.get("ended_at"):
                parse_time(run["ended_at"])


def find_workspace(state: dict[str, Any], wid: str) -> dict[str, Any]:
    for ws in state["workspaces"]:
        if ws["id"] == wid:
            return ws
    raise MaestroError(f"unknown workspace: {wid}")


def sentinel_path(root: Path, run: dict[str, Any]) -> Path:
    log = root / run["log_path"]
    return log.with_suffix(".exit")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_exit_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def reconcile_state(root: Path, state: dict[str, Any]) -> bool:
    changed = False
    now = utcnow()
    for ws in state["workspaces"]:
        run = ws.get("run")
        if ws.get("status") != "running" or not run:
            continue
        if not Path(ws["worktree_path"]).exists():
            if ws["status"] == "orphaned":
                continue
            ws["status"] = "orphaned"
            run["pid"] = None
            if not run.get("ended_at"):
                run["ended_at"] = now
            ws["updated_at"] = now
            changed = True
            continue
        if run.get("pid") is None and run.get("ended_at") is None:
            try:
                age = (dt.datetime.now(dt.timezone.utc) - parse_time(run["started_at"])).total_seconds()
            except Exception:
                age = 999
            if age < 30:
                continue
        if pid_alive(run.get("pid")):
            continue
        sent = sentinel_path(root, run)
        if sent.exists():
            code = read_exit_code(sent)
            ws["status"] = "completed" if code == 0 else "failed"
            run["exit_code"] = code
            run["ended_at"] = dt.datetime.fromtimestamp(sent.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        else:
            ws["status"] = "orphaned"
            run["exit_code"] = None
            run["ended_at"] = now
        run["pid"] = None
        ws["updated_at"] = now
        changed = True
    return changed


def locked_state(root: Path, mutate: bool = False) -> tuple[StateStore, dict[str, Any]]:
    store = StateStore(root)
    state = store.read(repair_corrupt=mutate)
    return store, state


def resolve_recipe(config: dict[str, Any], agent: str) -> dict[str, Any]:
    agents = config.get("agents", {})
    recipe = agents.get(agent)
    if not isinstance(recipe, dict):
        raise MaestroError(f"unknown agent recipe: {agent}")
    return recipe


def interpolated_args(recipe: dict[str, Any], values: dict[str, str]) -> list[str]:
    result: list[str] = []
    for arg in recipe.get("args", []):
        for token, value in values.items():
            arg = arg.replace(token, value)
        result.append(arg)
    return result


def materialize_log_paths(root: Path, log_dir: str, wid: str) -> tuple[Path, str]:
    log_slug = utcnow().replace(":", "-")
    log_rel = Path(log_dir) / wid / f"{log_slug}.log"
    log_abs = root / log_rel
    log_abs.parent.mkdir(parents=True, exist_ok=True)
    return log_abs, log_rel.as_posix()


def write_sentinel(path: Path, code: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{code}\n")
    os.replace(tmp, path)


def update_workspace(root: Path, wid: str, mutator) -> dict[str, Any]:
    store = StateStore(root)
    with store.lock():
        state = store.read()
        reconcile_state(root, state)
        ws = find_workspace(state, wid)
        mutator(ws)
        ws["updated_at"] = utcnow()
        store.write(state)
        return ws


def relative_config_dir(root: Path, value: str, field: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise MaestroError(f"defaults.{field} must be a relative path inside the repo")
    resolved = (root / path).resolve()
    git_dir = (root / ".git").resolve()
    if resolved == git_dir or git_dir in resolved.parents:
        raise MaestroError(f"defaults.{field} must not point inside .git")
    return path


def default_dirs(root: Path, defaults: dict[str, Any]) -> tuple[Path, Path]:
    worktree_dir = relative_config_dir(root, defaults.get("worktree_dir", ".maestro/worktrees"), "worktree_dir")
    log_dir = relative_config_dir(root, defaults.get("log_dir", ".maestro/logs"), "log_dir")
    if worktree_dir == log_dir:
        raise MaestroError("defaults.worktree_dir and defaults.log_dir must be different paths")
    return worktree_dir, log_dir


def gitignore_dir(path: Path) -> str:
    return path.as_posix().rstrip("/") + "/"


def append_gitignore_if_needed(root: Path, defaults: dict[str, Any]) -> None:
    store = StateStore(root)
    store.ensure_dirs()
    worktree_dir, log_dir = default_dirs(root, defaults)
    store.ensure_gitignore([gitignore_dir(worktree_dir), gitignore_dir(log_dir)])


def rollback_workspace(root: Path, branch: str, worktree_path: Path) -> None:
    if worktree_path.exists():
        run_cmd(["git", "-C", str(root), "worktree", "remove", "--force", str(worktree_path)], check=False)
    run_cmd(["git", "-C", str(root), "worktree", "prune"], check=False)
    if branch_exists(root, branch):
        run_cmd(["git", "-C", str(root), "branch", "-D", branch], check=False)


def resolve_prompt(path: str) -> tuple[bytes, dict[str, Any]]:
    if path == "-":
        data = sys.stdin.buffer.read()
        return data, {"source": "stdin", "ref": None, "sha256": sha256_bytes(data)}
    data = Path(path).read_bytes()
    return data, {"source": "file", "ref": path, "sha256": sha256_bytes(data)}


def build_process(root: Path, ws: dict[str, Any], recipe: dict[str, Any], prompt_text: str, extra_args: list[str]) -> tuple[list[str], list[str], dict[str, str]]:
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    prompt_file = (root / ws["run"]["log_path"]).parent / "prompt.txt"
    values = {
        "{prompt}": prompt_text,
        "{prompt_file}": str(prompt_file.resolve()),
        "{branch}": ws["branch"],
        "{workspace}": ws["id"],
        "{base}": ws["base"]["ref"],
        "{worktree}": ws["worktree_path"],
    }
    command = recipe["command"]
    args = interpolated_args(recipe, values) + extra_args
    redacted_args = []
    for template in recipe.get("args", []):
        redacted = template.replace("{prompt}", "<prompt>")
        for token, value in values.items():
            if token != "{prompt}":
                redacted = redacted.replace(token, value)
        redacted_args.append(redacted)
    env = os.environ.copy()
    env.update(recipe.get("env", {}))
    return [command] + args, [command] + redacted_args + extra_args, env


def supervise_foreground(root: Path, wid: str, argv: list[str], env: dict[str, str], prompt_bytes: bytes | None) -> int:
    store = StateStore(root)
    with store.lock():
        state = store.read()
        ws = find_workspace(state, wid)
        log_abs = root / ws["run"]["log_path"]
    with log_abs.open("ab", buffering=0) as log:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=ws["worktree_path"],
                env=env,
                stdin=subprocess.PIPE if prompt_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"maestro: failed to spawn agent: {exc}\n".encode())

            def fail_spawn(target: dict[str, Any]) -> None:
                target["status"] = "failed"
                target["run"]["pid"] = None
                target["run"]["exit_code"] = 127
                target["run"]["ended_at"] = utcnow()

            update_workspace(root, wid, fail_spawn)
            run = find_workspace(StateStore(root).read(), wid)["run"]
            write_sentinel(sentinel_path(root, run), 127)
            return 127

        def set_pid(target: dict[str, Any]) -> None:
            target["run"]["pid"] = proc.pid

        update_workspace(root, wid, set_pid)
        if prompt_bytes is not None and proc.stdin:
            proc.stdin.write(prompt_bytes)
            proc.stdin.close()
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.readline(), b""):
            log.write(chunk)
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except BrokenPipeError:
                pass
        code = proc.wait()
    run = find_workspace(StateStore(root).read(), wid)["run"]
    write_sentinel(sentinel_path(root, run), code)

    def finish(target: dict[str, Any]) -> None:
        target["status"] = "completed" if code == 0 else "failed"
        target["run"]["pid"] = None
        target["run"]["exit_code"] = code
        target["run"]["ended_at"] = utcnow()

    update_workspace(root, wid, finish)
    return code


def supervise_detached(root: Path, wid: str, argv: list[str], env: dict[str, str], prompt_bytes: bytes | None) -> None:
    store = StateStore(root)
    with store.lock():
        state = store.read()
        ws = find_workspace(state, wid)
        log_abs = root / ws["run"]["log_path"]
    with log_abs.open("ab") as log:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=ws["worktree_path"],
                env=env,
                stdin=subprocess.PIPE if prompt_bytes is not None else subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"maestro: failed to spawn agent: {exc}\n")

            def fail_spawn(target: dict[str, Any]) -> None:
                target["status"] = "failed"
                target["run"]["pid"] = None
                target["run"]["exit_code"] = 127
                target["run"]["ended_at"] = utcnow()

            update_workspace(root, wid, fail_spawn)
            run = find_workspace(StateStore(root).read(), wid)["run"]
            write_sentinel(sentinel_path(root, run), 127)
            return
        if prompt_bytes is not None and proc.stdin:
            proc.stdin.write(prompt_bytes)
            proc.stdin.close()

        def set_pid(target: dict[str, Any]) -> None:
            target["run"]["pid"] = proc.pid

        update_workspace(root, wid, set_pid)
        code = proc.wait()
    run = find_workspace(StateStore(root).read(), wid)["run"]
    write_sentinel(sentinel_path(root, run), code)

    def finish(target: dict[str, Any]) -> None:
        if target["status"] == "running":
            target["status"] = "completed" if code == 0 else "failed"
            target["run"]["pid"] = None
            target["run"]["exit_code"] = code
            target["run"]["ended_at"] = utcnow()

    update_workspace(root, wid, finish)


def cmd_supervise(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.payload).read_text())
    supervise_detached(
        Path(payload["root"]),
        payload["id"],
        payload["argv"],
        payload["env"],
        bytes.fromhex(payload["prompt_hex"]) if payload["prompt_hex"] else None,
    )
    with contextlib.suppress(FileNotFoundError):
        Path(args.payload).unlink()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    if not has_commit(root):
        raise MaestroError("repository has no commits to branch from")
    validate_branch(args.branch)
    config = load_config(root, args.config)
    agent = args.agent or config.get("defaults", {}).get("agent")
    if not agent:
        raise MaestroError("agent is required; pass --agent or set defaults.agent")
    recipe = resolve_recipe(config, agent)
    validate_recipe(agent, recipe)
    prompt_bytes, prompt_meta = resolve_prompt(args.prompt)
    prompt_text = prompt_bytes.decode(errors="replace")
    wid = args.name or slugify(args.branch)
    if not ID_RE.match(wid):
        raise MaestroError(f"invalid workspace id: {wid}")
    defaults = config.get("defaults", {})
    worktree_dir, log_dir = default_dirs(root, defaults)
    worktree_path = (root / worktree_dir / wid).resolve()
    base_sha = run_cmd(["git", "-C", str(root), "rev-parse", "--verify", f"{args.base}^{{commit}}"]).stdout.strip()
    store = StateStore(root)
    created = False
    log_abs: Path | None = None
    try:
        with store.lock():
            state = store.read()
            reconcile_state(root, state)
            if any(ws["id"] == wid for ws in state["workspaces"]):
                raise MaestroError(f"workspace id already exists: {wid}; pass --name")
            if branch_exists(root, args.branch):
                raise MaestroError(f"branch already exists: {args.branch}; choose a different --branch")
            if worktree_path.exists():
                raise MaestroError(f"worktree path already exists: {worktree_path}")
            created = True
            run_cmd(["git", "-C", str(root), "worktree", "add", "-b", args.branch, str(worktree_path), base_sha])
            log_abs, log_rel = materialize_log_paths(root, log_dir.as_posix(), wid)
            prompt_file = log_abs.parent / "prompt.txt"
            prompt_file.write_bytes(prompt_bytes)
            now = utcnow()
            ws = {
                "id": wid,
                "branch": args.branch,
                "base": {"ref": args.base, "sha": base_sha},
                "worktree_path": str(worktree_path),
                "agent": agent,
                "prompt": prompt_meta,
                "status": "running",
                "run": {
                    "pid": None,
                    "detached": bool(args.detach),
                    "started_at": now,
                    "ended_at": None,
                    "exit_code": None,
                    "log_path": log_rel,
                    "command": [],
                },
                "created_at": now,
                "updated_at": now,
                "maestro_version": __version__,
            }
            state["workspaces"].append(ws)
            store.write(state)
        append_gitignore_if_needed(root, defaults)
        setup = args.setup if args.setup is not None else recipe.get("setup")
        if setup:
            with log_abs.open("ab") as log:
                log.write(b"==== maestro setup ====\n")
                setup_proc = subprocess.run(setup, cwd=worktree_path, shell=True, stdout=log, stderr=subprocess.STDOUT)
            if setup_proc.returncode != 0:
                def fail_setup(target: dict[str, Any]) -> None:
                    target["status"] = "failed"
                    target["run"]["exit_code"] = setup_proc.returncode
                    target["run"]["ended_at"] = utcnow()
                update_workspace(root, wid, fail_setup)
                return setup_proc.returncode
        argv, redacted, env = build_process(root, ws, recipe, prompt_text, list(args.extra_args or []))
        def set_command(target: dict[str, Any]) -> None:
            target["run"]["command"] = redacted
            target["run"]["started_at"] = utcnow()
        update_workspace(root, wid, set_command)
        stdin_prompt = prompt_bytes if recipe.get("prompt_via") == "stdin" else None
        if args.detach:
            payload_fd, payload_name = tempfile.mkstemp(prefix="maestro-supervise.", suffix=".json", dir=root / ".maestro")
            with os.fdopen(payload_fd, "w") as f:
                json.dump({"root": str(root), "id": wid, "argv": argv, "env": env, "prompt_hex": stdin_prompt.hex() if stdin_prompt else ""}, f)
            subprocess.Popen(
                [sys.executable, "-m", "maestro_cli.cli", "_supervise", "--payload", payload_name],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                with store.lock():
                    state = store.read()
                    current = find_workspace(state, wid)
                    run_state = current.get("run") or {}
                    if run_state.get("pid") or current.get("status") != "running":
                        ws = current
                        break
                time.sleep(0.05)
            print(json.dumps(ws) if args.json else wid)
            return 0
        print(f"created workspace: {wid}   (branch {args.branch})")
        return supervise_foreground(root, wid, argv, env, stdin_prompt)
    except BaseException:
        if created:
            with contextlib.suppress(BaseException):
                with store.lock():
                    state = store.read()
                    if not any(ws["id"] == wid and ws.get("status") == "failed" for ws in state["workspaces"]):
                        state["workspaces"] = [ws for ws in state["workspaces"] if ws["id"] != wid]
                        store.write(state)
            with contextlib.suppress(BaseException):
                state = store.read()
                preserve_setup_failure = any(ws["id"] == wid and ws.get("status") == "failed" for ws in state["workspaces"])
                if not preserve_setup_failure:
                    rollback_workspace(root, args.branch, worktree_path)
        raise


def cmd_ls(args: argparse.Namespace) -> int:
    root = repo_root()
    store = StateStore(root)
    with store.lock():
        state = store.read(repair_corrupt=False)
        changed = reconcile_state(root, state)
        if changed:
            store.write(state)
    workspaces = state["workspaces"]
    if args.status:
        workspaces = [ws for ws in workspaces if ws["status"] == args.status]
    if args.json:
        print(json.dumps(workspaces, indent=2))
        return 0
    if not workspaces:
        print("no maestro workspaces")
        return 0
    print(f"{'ID':<18} {'BRANCH':<28} {'AGENT':<10} {'STATUS':<10} {'STARTED':<20} {'DURATION':<9} LOG")
    for ws in workspaces:
        run = ws.get("run") or {}
        print(
            f"{ws['id']:<18} {ws['branch']:<28} {ws['agent']:<10} {ws['status']:<10} "
            f"{run.get('started_at', '-'):<20} {duration(run.get('started_at'), run.get('ended_at')):<9} {run.get('log_path', '-')}"
        )
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    root = repo_root()
    store = StateStore(root)
    with store.lock():
        state = store.read()
        changed = reconcile_state(root, state)
        if changed:
            store.write(state)
        ws = find_workspace(state, args.id)
    run = ws.get("run")
    if not run:
        raise MaestroError(f"workspace has no run: {args.id}")
    log_path = root / run["log_path"]
    if not log_path.exists():
        raise MaestroError(f"log file is missing: {log_path}")
    if args.lines is not None:
        lines = log_path.read_text(errors="replace").splitlines()
        print("\n".join(lines[-args.lines :]))
    else:
        sys.stdout.write(log_path.read_text(errors="replace"))
    if args.follow:
        pos = log_path.stat().st_size
        while True:
            time.sleep(0.5)
            with store.lock():
                state = store.read()
                changed = reconcile_state(root, state)
                if changed:
                    store.write(state)
                status = find_workspace(state, args.id)["status"]
            with log_path.open("r", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if status != "running":
                break
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    root = repo_root()
    store = StateStore(root)
    with store.lock():
        state = store.read()
        changed = reconcile_state(root, state)
        if changed:
            store.write(state)
        ws = find_workspace(state, args.id)
    path = Path(ws["worktree_path"])
    if not path.exists():
        raise MaestroError(f"worktree directory is missing: {path}")
    print(path)
    return 0


def stop_workspace(root: Path, wid: str, *, quiet: bool = False) -> None:
    store = StateStore(root)
    with store.lock():
        state = store.read()
        changed = reconcile_state(root, state)
        ws = find_workspace(state, wid)
        if changed:
            store.write(state)
        if ws["status"] != "running":
            if not quiet:
                print(f"workspace {wid} is not running ({ws['status']})")
            return
        pid = ws["run"]["pid"]
    if pid:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.time() + 10
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.1)
        code = 128 + signal.SIGTERM
        if pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            code = 128 + signal.SIGKILL
    else:
        code = 128 + signal.SIGTERM
    def mark_stopped(target: dict[str, Any]) -> None:
        run = target["run"]
        write_sentinel(sentinel_path(root, run), code)
        target["status"] = "stopped"
        run["pid"] = None
        run["exit_code"] = code
        run["ended_at"] = utcnow()
    update_workspace(root, wid, mark_stopped)
    if not quiet:
        print(f"stopped workspace: {wid}")


def cmd_stop(args: argparse.Namespace) -> int:
    stop_workspace(repo_root(), args.id)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    root = repo_root()
    store = StateStore(root)
    with store.lock():
        state = store.read()
        changed = reconcile_state(root, state)
        if changed:
            store.write(state)
        ws = find_workspace(state, args.id)
    if ws["status"] == "running":
        if not args.force:
            raise MaestroError(f"workspace is running: {args.id}; pass --force to stop and remove")
        stop_workspace(root, args.id, quiet=True)
        with store.lock():
            state = store.read()
            ws = find_workspace(state, args.id)
    worktree = Path(ws["worktree_path"])
    if worktree.exists():
        dirty = run_cmd(["git", "-C", str(worktree), "status", "--porcelain"], check=False).stdout.strip()
        if dirty and not args.force:
            raise MaestroError(f"worktree has uncommitted changes: {args.id}; pass --force to discard")
        cmd = ["git", "-C", str(root), "worktree", "remove"]
        if args.force:
            cmd.append("--force")
        cmd.append(str(worktree))
        run_cmd(cmd)
    run_cmd(["git", "-C", str(root), "worktree", "prune"], check=False)
    branch_deleted = False
    if args.delete_branch:
        delete_flag = "-D" if args.force else "-d"
        proc = run_cmd(["git", "-C", str(root), "branch", delete_flag, ws["branch"]], check=False)
        if proc.returncode != 0 and "not found" not in proc.stderr.lower():
            raise MaestroError(proc.stderr.strip())
        branch_deleted = proc.returncode == 0
    with store.lock():
        state = store.read()
        state["workspaces"] = [item for item in state["workspaces"] if item["id"] != args.id]
        store.write(state)
    if args.delete_branch and branch_deleted:
        print(f"removed worktree and branch {ws['branch']}")
    else:
        print(f"removed worktree for {args.id} (branch {ws['branch']} kept)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro")
    parser.add_argument("--json", action="store_true", help="print JSON output where supported")
    parser.add_argument("--config", help="load an extra TOML config file after default/user/project config")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--agent")
    run_p.add_argument("--branch", required=True)
    run_p.add_argument("--prompt", required=True)
    run_p.add_argument("--base", default="HEAD")
    run_p.add_argument("--name")
    run_p.add_argument("--detach", "--background", action="store_true")
    run_p.add_argument("--setup")
    run_p.add_argument("extra_args", nargs=argparse.REMAINDER)
    run_p.set_defaults(func=cmd_run)

    ls_p = sub.add_parser("ls")
    ls_p.add_argument("--status", choices=sorted(VALID_STATUSES))
    ls_p.set_defaults(func=cmd_ls)

    logs_p = sub.add_parser("logs")
    logs_p.add_argument("id")
    logs_p.add_argument("-f", "--follow", action="store_true")
    logs_p.add_argument("-n", "--lines", type=int)
    logs_p.set_defaults(func=cmd_logs)

    open_p = sub.add_parser("open")
    open_p.add_argument("id")
    open_p.set_defaults(func=cmd_open)

    stop_p = sub.add_parser("stop")
    stop_p.add_argument("id")
    stop_p.set_defaults(func=cmd_stop)

    rm_p = sub.add_parser("rm")
    rm_p.add_argument("id")
    rm_p.add_argument("--force", action="store_true")
    rm_p.add_argument("--delete-branch", action="store_true")
    rm_p.set_defaults(func=cmd_rm)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "_supervise":
        parser = argparse.ArgumentParser(prog="maestro _supervise")
        parser.add_argument("_command")
        parser.add_argument("--payload", required=True)
        args = parser.parse_args(raw_argv)
        try:
            return cmd_supervise(args)
        except MaestroError as exc:
            eprint(f"maestro: {exc}")
            return 1
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130
    except MaestroError as exc:
        if not getattr(args, "quiet", False):
            eprint(f"maestro: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
