"""Unit tests for installer.runner: captured execution and dry-run recording."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer.runner import DryRunRunner, Runner, RunnerError  # noqa: E402

PY = sys.executable


class RunnerTests(unittest.TestCase):
    def test_captures_stdout_and_stderr(self):
        result = Runner().run(
            [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"out\n")
        self.assertEqual(result.stderr, b"err\n")

    def test_failure_carries_stderr_tail_but_not_stdout(self):
        script = (
            "import sys; print('SECRET-PAYLOAD'); "
            "sys.stderr.write('x' * 5000 + 'the-end'); sys.exit(3)"
        )
        with self.assertRaises(RunnerError) as ctx:
            Runner().run([PY, "-c", script], timeout=30)
        err = ctx.exception
        self.assertEqual(err.returncode, 3)
        self.assertTrue(err.stderr_tail.endswith("the-end"))
        self.assertLessEqual(len(err.stderr_tail), 2000)
        self.assertNotIn("SECRET-PAYLOAD", str(err))
        self.assertNotIn("SECRET-PAYLOAD", err.stderr_tail)
        self.assertEqual(err.argv[0], PY)

    def test_check_false_returns_nonzero(self):
        result = Runner().run([PY, "-c", "raise SystemExit(4)"], timeout=30, check=False)
        self.assertEqual(result.returncode, 4)

    def test_timeout_raises(self):
        with self.assertRaises(RunnerError) as ctx:
            Runner().run([PY, "-c", "import time; time.sleep(10)"], timeout=0.3)
        self.assertIsNone(ctx.exception.returncode)
        self.assertIn("timed out", str(ctx.exception))

    def test_missing_executable(self):
        with self.assertRaises(RunnerError) as ctx:
            Runner().run(["/nonexistent/definitely-not-here"], timeout=5)
        self.assertEqual(ctx.exception.returncode, 127)

    def test_env_is_complete_replacement_and_input_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import os, sys; data = sys.stdin.buffer.read(); "
                "print(os.environ.get('PD_TEST'), 'HOME' in os.environ, "
                "os.getcwd(), data.decode())"
            )
            result = Runner().run(
                [PY, "-c", script],
                timeout=30,
                env={"PD_TEST": "yes", "PATH": os.environ.get("PATH", "")},
                input=b"hello",
                cwd=Path(tmp),
            )
            self.assertEqual(
                result.stdout.decode().split(),
                ["yes", "False", os.path.realpath(tmp), "hello"],
            )

    def test_stdin_is_not_inherited(self):
        result = Runner().run(
            [PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=30
        )
        self.assertEqual(result.stdout.strip(), b"''")

    def test_which(self):
        self.assertIsNotNone(Runner().which("sh"))
        self.assertIsNone(Runner().which("definitely-not-a-command-xyz"))

    def test_empty_argv(self):
        with self.assertRaises(RunnerError):
            Runner().run([], timeout=1)


class DryRunRunnerTests(unittest.TestCase):
    def test_mutating_calls_are_recorded_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            runner = DryRunRunner()
            result = runner.run(["touch", str(marker)], timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertFalse(marker.exists())
            self.assertEqual(runner.recorded, [["touch", str(marker)]])

    def test_read_only_calls_execute(self):
        runner = DryRunRunner()
        result = runner.run([PY, "-c", "print(42)"], timeout=30, read_only=True)
        self.assertEqual(result.stdout, b"42\n")
        self.assertEqual(runner.recorded, [])

    def test_read_only_failure_still_raises(self):
        runner = DryRunRunner()
        with self.assertRaises(RunnerError):
            runner.run([PY, "-c", "raise SystemExit(2)"], timeout=30, read_only=True)


class ScriptedRunnerTests(unittest.TestCase):
    """Tests elsewhere subclass Runner; make sure the seam stays subclassable."""

    def test_subclass_override(self):
        import subprocess

        class Scripted(Runner):
            def __init__(self):
                self.calls = []

            def run(self, argv, *, timeout, check=True, env=None, input=None,
                    cwd=None, read_only=False):
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(list(argv), 0, b"ok", b"")

        runner = Scripted()
        self.assertEqual(runner.run(["apt-get", "install"], timeout=1).stdout, b"ok")
        self.assertEqual(runner.calls, [["apt-get", "install"]])


if __name__ == "__main__":
    unittest.main()
