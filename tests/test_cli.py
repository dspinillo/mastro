import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from maestro_cli import cli


ROOT = Path(__file__).resolve().parents[1]


def run(args, cwd, *, input_text=None, env=None):
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "maestro_cli.cli", *args],
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged_env,
    )


def start(args, cwd, *, env=None):
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.Popen(
        [sys.executable, "-m", "maestro_cli.cli", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged_env,
    )


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc


class MaestroCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "tests@example.com")
        git(self.repo, "config", "user.name", "Tests")
        (self.repo / "README.md").write_text("repo\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")

        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.fake_agent = self.bin / "fake-agent"
        self.fake_agent.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import sys
                print("fake agent ran")
                print("args=" + repr(sys.argv[1:]))
                data = sys.stdin.read()
                if data:
                    print("stdin=" + data)
                """
            )
        )
        self.fake_agent.chmod(0o755)
        self.slow_agent = self.bin / "slow-agent"
        self.slow_agent.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import time
                print("slow agent started", flush=True)
                time.sleep(30)
                """
            )
        )
        self.slow_agent.chmod(0o755)
        self.fail_agent = self.bin / "fail-agent"
        self.fail_agent.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import sys
                print("fail agent ran")
                sys.exit(7)
                """
            )
        )
        self.fail_agent.chmod(0o755)
        (self.repo / ".maestro").mkdir()
        self.write_config()
        (self.repo / "prompt.txt").write_text("add tests please\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, body=None):
        if body is None:
            body = f"""\
            [agents.fake]
            command = "{self.fake_agent}"
            args = ["--prompt", "{{prompt}}"]
            prompt_via = "arg"

            [agents.slow]
            command = "{self.slow_agent}"
            args = ["--prompt", "{{prompt}}"]
            prompt_via = "arg"

            [agents.fail]
            command = "{self.fail_agent}"
            args = ["--prompt", "{{prompt}}"]
            prompt_via = "arg"

            [agents.stdin]
            command = "{self.fake_agent}"
            args = []
            prompt_via = "stdin"
            """
        (self.repo / ".maestro" / "config.toml").write_text(textwrap.dedent(body))

    def state(self):
        return json.loads((self.repo / ".maestro" / "state.json").read_text())

    def workspace(self, workspace_id):
        for item in self.state()["workspaces"]:
            if item["id"] == workspace_id:
                return item
        raise AssertionError(f"workspace not found: {workspace_id}")

    def wait_for_status(self, workspace_id, status, timeout=5):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            ls_proc = run(["--json", "ls"], self.repo)
            self.assertEqual(ls_proc.returncode, 0, ls_proc.stderr)
            workspaces = json.loads(ls_proc.stdout)
            last = next((item for item in workspaces if item["id"] == workspace_id), None)
            if last and last["status"] == status:
                return last
            time.sleep(0.1)
        self.fail(f"workspace {workspace_id} did not reach {status}; last={last}")

    def test_run_records_completed_workspace_and_logs(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/add-tests", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("created workspace: task-add-tests", proc.stdout)
        self.assertIn("fake agent ran", proc.stdout)

        state = json.loads((self.repo / ".maestro" / "state.json").read_text())
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(state["workspaces"]), 1)
        ws = state["workspaces"][0]
        self.assertEqual(ws["id"], "task-add-tests")
        self.assertEqual(ws["status"], "completed")
        self.assertEqual(ws["run"]["exit_code"], 0)
        self.assertEqual(ws["run"]["pid"], None)
        self.assertEqual(ws["run"]["command"], [str(self.fake_agent), "--prompt", "<prompt>"])
        self.assertEqual(ws["prompt"]["source"], "file")
        self.assertTrue((self.repo / ".maestro" / "logs" / "task-add-tests" / "prompt.txt").exists())
        self.assertTrue((self.repo / ws["run"]["log_path"]).exists())
        self.assertTrue((self.repo / ws["run"]["log_path"]).with_suffix(".exit").exists())

        ls_proc = run(["--json", "ls"], self.repo)
        self.assertEqual(ls_proc.returncode, 0, ls_proc.stderr)
        listed = json.loads(ls_proc.stdout)
        self.assertEqual(listed[0]["status"], "completed")

        logs_proc = run(["logs", "task-add-tests", "-n", "1"], self.repo)
        self.assertEqual(logs_proc.returncode, 0, logs_proc.stderr)
        self.assertIn("args=", logs_proc.stdout)

        open_proc = run(["open", "task-add-tests"], self.repo)
        self.assertEqual(open_proc.returncode, 0, open_proc.stderr)
        self.assertTrue(Path(open_proc.stdout.strip()).exists())

    def test_run_errors_before_worktree_when_branch_exists(self):
        first = run(["run", "--agent", "fake", "--branch", "task/add-tests", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run(["run", "--agent", "fake", "--branch", "task/add-tests", "--prompt", "prompt.txt", "--name", "other"], self.repo)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("branch already exists", second.stderr)
        state = json.loads((self.repo / ".maestro" / "state.json").read_text())
        self.assertEqual(len(state["workspaces"]), 1)

    def test_rm_preserves_branch_by_default(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/add-tests", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rm_proc = run(["rm", "task-add-tests"], self.repo)
        self.assertEqual(rm_proc.returncode, 0, rm_proc.stderr)
        self.assertIn("branch task/add-tests kept", rm_proc.stdout)
        state = json.loads((self.repo / ".maestro" / "state.json").read_text())
        self.assertEqual(state["workspaces"], [])
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/add-tests").returncode, 0)
        self.assertFalse((self.repo / ".maestro" / "worktrees" / "task-add-tests").exists())

    def test_detached_run_is_supervised_to_completion(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/detached", "--prompt", "prompt.txt", "--detach"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "task-detached")

        final_state = self.wait_for_status("task-detached", "completed")
        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(final_state["run"]["exit_code"], 0)
        self.assertTrue((self.repo / final_state["run"]["log_path"]).with_suffix(".exit").exists())

    def test_custom_worktree_and_log_dirs_are_supported(self):
        self.write_config(
            f"""\
            [defaults]
            worktree_dir = "custom/worktrees"
            log_dir = "custom/logs"

            [agents.fake]
            command = "{self.fake_agent}"
            args = ["--prompt", "{{prompt}}"]
            prompt_via = "arg"
            """
        )
        proc = run(["run", "--agent", "fake", "--branch", "task/custom-dirs", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ws = self.workspace("task-custom-dirs")
        self.assertEqual(ws["status"], "completed")
        self.assertTrue((self.repo / "custom" / "worktrees" / "task-custom-dirs").exists())
        self.assertTrue(ws["run"]["log_path"].startswith("custom/logs/task-custom-dirs/"))
        self.assertTrue((self.repo / ws["run"]["log_path"]).exists())
        self.assertTrue((self.repo / "custom" / "logs" / "task-custom-dirs" / "prompt.txt").exists())

    def test_supervise_is_hidden_from_public_help(self):
        proc = run(["--help"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("_supervise", proc.stdout)
        for command in ["run", "ls", "logs", "open", "stop", "rm"]:
            self.assertIn(command, proc.stdout)

    def test_stop_running_workspace_marks_stopped(self):
        proc = run(["run", "--agent", "slow", "--branch", "task/slow", "--prompt", "prompt.txt", "--detach"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_for_status("task-slow", "running")
        stop_proc = run(["stop", "task-slow"], self.repo)
        self.assertEqual(stop_proc.returncode, 0, stop_proc.stderr)
        self.assertIn("stopped workspace: task-slow", stop_proc.stdout)
        ws = self.workspace("task-slow")
        self.assertEqual(ws["status"], "stopped")
        self.assertEqual(ws["run"]["pid"], None)
        self.assertIn(ws["run"]["exit_code"], [128 + 15, 128 + 9])

    def test_non_zero_agent_exit_becomes_failed(self):
        proc = run(["run", "--agent", "fail", "--branch", "task/fail", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 7)
        self.assertIn("fail agent ran", proc.stdout)
        ws = self.workspace("task-fail")
        self.assertEqual(ws["status"], "failed")
        self.assertEqual(ws["run"]["exit_code"], 7)

    def test_prompt_can_be_read_from_stdin(self):
        proc = run(["run", "--agent", "stdin", "--branch", "task/stdin", "--prompt", "-"], self.repo, input_text="from stdin\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stdin=from stdin", proc.stdout)
        ws = self.workspace("task-stdin")
        self.assertEqual(ws["prompt"]["source"], "stdin")
        self.assertEqual(ws["prompt"]["ref"], None)
        self.assertTrue((self.repo / "task-stdin").exists() is False)
        self.assertEqual((self.repo / ".maestro" / "logs" / "task-stdin" / "prompt.txt").read_text(), "from stdin\n")

    def test_rm_delete_branch_removes_branch(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/delete-me", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rm_proc = run(["rm", "task-delete-me", "--delete-branch"], self.repo)
        self.assertEqual(rm_proc.returncode, 0, rm_proc.stderr)
        self.assertIn("removed worktree and branch task/delete-me", rm_proc.stdout)
        self.assertNotEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/delete-me", check=False).returncode, 0)

    def test_rm_delete_branch_tolerates_manually_deleted_branch(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/deleted-branch", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        worktree = Path(run(["open", "task-deleted-branch"], self.repo).stdout.strip())
        git(worktree, "switch", "--detach")
        git(self.repo, "branch", "-D", "task/deleted-branch")

        rm_proc = run(["rm", "task-deleted-branch", "--delete-branch"], self.repo)
        self.assertEqual(rm_proc.returncode, 0, rm_proc.stderr)
        self.assertIn("branch task/deleted-branch already absent", rm_proc.stdout)
        self.assertFalse(worktree.exists())
        self.assertEqual(self.state()["workspaces"], [])

    def test_dirty_worktree_blocks_rm_without_force(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/dirty", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        worktree = Path(run(["open", "task-dirty"], self.repo).stdout.strip())
        (worktree / "dirty.txt").write_text("dirty\n")
        rm_proc = run(["rm", "task-dirty"], self.repo)
        self.assertNotEqual(rm_proc.returncode, 0)
        self.assertIn("worktree has uncommitted changes", rm_proc.stderr)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.workspace("task-dirty")["status"], "completed")

    def test_running_workspace_missing_worktree_reconciles_to_orphaned(self):
        proc = run(["run", "--agent", "slow", "--branch", "task/orphan", "--prompt", "prompt.txt", "--detach"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_for_status("task-orphan", "running")
        worktree = Path(run(["open", "task-orphan"], self.repo).stdout.strip())
        git(self.repo, "worktree", "remove", "--force", str(worktree))
        ls_proc = run(["--json", "ls"], self.repo)
        self.assertEqual(ls_proc.returncode, 0, ls_proc.stderr)
        ws = next(item for item in json.loads(ls_proc.stdout) if item["id"] == "task-orphan")
        self.assertEqual(ws["status"], "orphaned")

    def test_reconcile_does_not_orphan_completed_workspace_on_missing_worktree(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/completed-missing", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        worktree = Path(run(["open", "task-completed-missing"], self.repo).stdout.strip())
        git(self.repo, "worktree", "remove", "--force", str(worktree))
        ls_proc = run(["--json", "ls"], self.repo)
        self.assertEqual(ls_proc.returncode, 0, ls_proc.stderr)
        ws = next(item for item in json.loads(ls_proc.stdout) if item["id"] == "task-completed-missing")
        self.assertEqual(ws["status"], "completed")

    def test_invalid_recipe_fails_before_creating_worktree(self):
        self.write_config(
            f"""\
            [agents.invalid]
            command = "{self.fake_agent}"
            args = []
            prompt_via = "arg"
            """
        )
        proc = run(["run", "--agent", "invalid", "--branch", "task/invalid", "--prompt", "prompt.txt"], self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must contain exactly one {prompt}", proc.stderr)
        self.assertFalse((self.repo / ".maestro" / "worktrees" / "task-invalid").exists())
        self.assertNotEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/invalid", check=False).returncode, 0)

    def test_corrupt_state_fails_closed_for_all_commands(self):
        proc = run(["run", "--agent", "fake", "--branch", "task/corrupt-seed", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state_path = self.repo / ".maestro" / "state.json"
        state_path.write_text("{not json")
        commands = [
            ["ls"],
            ["run", "--agent", "fake", "--branch", "task/corrupt-state", "--prompt", "prompt.txt"],
            ["logs", "task-corrupt-seed"],
            ["open", "task-corrupt-seed"],
            ["stop", "task-corrupt-seed"],
            ["rm", "task-corrupt-seed"],
        ]
        for command in commands:
            with self.subTest(command=command):
                result = run(command, self.repo)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("malformed state file", result.stderr)
                self.assertIn("git worktree list", result.stderr)
                self.assertEqual(state_path.read_text(), "{not json")
                self.assertEqual(list((self.repo / ".maestro").glob("state.json.corrupt-*")), [])
        self.assertNotEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/corrupt-state", check=False).returncode, 0)

    def test_defaults_agent_config_is_used_when_agent_not_passed(self):
        self.write_config(
            f"""\
            [defaults]
            agent = "fake"

            [agents.fake]
            command = "{self.fake_agent}"
            args = ["--prompt", "{{prompt}}"]
            prompt_via = "arg"
            """
        )
        proc = run(["run", "--branch", "task/default-agent", "--prompt", "prompt.txt"], self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fake agent ran", proc.stdout)
        self.assertEqual(self.workspace("task-default-agent")["agent"], "fake")

    def test_ctrl_c_after_worktree_add_rolls_back_branch_and_worktree(self):
        args = SimpleNamespace(
            agent="fake",
            branch="task/interrupted",
            prompt="prompt.txt",
            base="HEAD",
            name=None,
            detach=False,
            setup=None,
            extra_args=[],
            config=None,
            json=False,
        )
        original_cwd = Path.cwd()
        original_run_cmd = cli.run_cmd

        def interrupt_after_worktree_add(cmd, **kwargs):
            result = original_run_cmd(cmd, **kwargs)
            if len(cmd) > 4 and cmd[0] == "git" and "worktree" in cmd and "add" in cmd:
                raise KeyboardInterrupt
            return result

        try:
            os.chdir(self.repo)
            with mock.patch.object(cli, "run_cmd", side_effect=interrupt_after_worktree_add):
                with self.assertRaises(KeyboardInterrupt):
                    cli.cmd_run(args)
        finally:
            os.chdir(original_cwd)

        self.assertFalse((self.repo / ".maestro" / "worktrees" / "task-interrupted").exists())
        self.assertNotEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/interrupted", check=False).returncode, 0)
        state_path = self.repo / ".maestro" / "state.json"
        if state_path.exists():
            self.assertEqual(json.loads(state_path.read_text())["workspaces"], [])

    def test_concurrent_runs_for_same_branch_preserve_winner(self):
        first = start(["run", "--agent", "fake", "--branch", "task/race", "--prompt", "prompt.txt"], self.repo)
        second = start(["run", "--agent", "fake", "--branch", "task/race", "--prompt", "prompt.txt"], self.repo)
        first_out, first_err = first.communicate(timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        results = [(first.returncode, first_out, first_err), (second.returncode, second_out, second_err)]
        self.assertEqual(sum(1 for code, _, _ in results if code == 0), 1, results)
        self.assertEqual(sum(1 for code, _, _ in results if code != 0), 1, results)
        loser_err = next(err for code, _, err in results if code != 0)
        self.assertTrue("branch already exists" in loser_err or "workspace id already exists" in loser_err, loser_err)
        state = self.state()
        self.assertEqual([ws["id"] for ws in state["workspaces"]], ["task-race"])
        self.assertTrue((self.repo / ".maestro" / "worktrees" / "task-race").exists())
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "refs/heads/task/race").returncode, 0)

    def test_config_dirs_must_not_point_inside_git_or_match(self):
        cases = [
            (
                f"""\
                [defaults]
                worktree_dir = ".git/worktrees"
                log_dir = "custom/logs"
                """,
                "must not point inside .git",
            ),
            (
                f"""\
                [defaults]
                worktree_dir = "same"
                log_dir = "same"
                """,
                "must be different paths",
            ),
        ]
        for index, (defaults, message) in enumerate(cases):
            with self.subTest(message=message):
                self.write_config(
                    defaults
                    + f"""

                    [agents.fake]
                    command = "{self.fake_agent}"
                    args = ["--prompt", "{{prompt}}"]
                    prompt_via = "arg"
                    """
                )
                branch = f"task/bad-dir-{index}"
                proc = run(["run", "--agent", "fake", "--branch", branch, "--prompt", "prompt.txt"], self.repo)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(message, proc.stderr)
                self.assertNotEqual(git(self.repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
