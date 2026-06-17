import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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
        (self.repo / ".maestro").mkdir()
        (self.repo / ".maestro" / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [agents.fake]
                command = "{self.fake_agent}"
                args = ["--prompt", "{{prompt}}"]
                prompt_via = "arg"
                """
            )
        )
        (self.repo / "prompt.txt").write_text("add tests please\n")

    def tearDown(self):
        self.tmp.cleanup()

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

        deadline = time.time() + 5
        final_state = None
        while time.time() < deadline:
            ls_proc = run(["--json", "ls"], self.repo)
            self.assertEqual(ls_proc.returncode, 0, ls_proc.stderr)
            final_state = json.loads(ls_proc.stdout)[0]
            if final_state["status"] == "completed":
                break
            time.sleep(0.1)

        self.assertIsNotNone(final_state)
        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(final_state["run"]["exit_code"], 0)
        self.assertTrue((self.repo / final_state["run"]["log_path"]).with_suffix(".exit").exists())


if __name__ == "__main__":
    unittest.main()
