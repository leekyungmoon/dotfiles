"""Opt-in Sidekick runtime integration with a fake Copilot CLI.

This test executes real lazy.nvim and real folke/sidekick.nvim at pinned
revisions in a disposable HOME.  It never starts the paid/authenticated
GitHub Copilot CLI: ``~/.local/bin/copilot`` is a fixture that records its
arguments, cwd and stdin.  tmux is reached only through a wrapper that adds a
private ``-L`` socket name under a private ``TMUX_TMPDIR``; teardown addresses
and kills only that server.

Run explicitly with ``DOTFILES_SIDEKICK_RUNTIME_TEST=1``.  Network access is
needed once to fetch the two pinned plugin revisions.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
NVIM = shutil.which("nvim")
TMUX = shutil.which("tmux")
GIT = shutil.which("git")
LAZY_VERSION = "v9.25.1"
SIDEKICK_COMMIT = "3d80a47e6375f6d647c9695d3afea6fa1b3275df"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sidekick" / "runtime.lua"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(os.environ.get("DOTFILES_SIDEKICK_RUNTIME_TEST") == "1",
                     "opt in to the real Sidekick runtime test")
@unittest.skipUnless(NVIM and TMUX and GIT, "nvim, tmux and git are required")
class SidekickRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._plugins_tmp = tempfile.TemporaryDirectory(prefix="sidekick-plugins-")
        cls.plugins = Path(cls._plugins_tmp.name)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(cls.plugins / "home"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        (cls.plugins / "home").mkdir()
        try:
            subprocess.run(
                [GIT, "clone", "--quiet", "--depth", "1", "--branch", LAZY_VERSION,
                 "https://github.com/folke/lazy.nvim.git", str(cls.plugins / "lazy.nvim")],
                env=env, check=True, capture_output=True, timeout=120)
            sidekick = cls.plugins / "sidekick.nvim"
            subprocess.run([GIT, "init", "--quiet", str(sidekick)], env=env, check=True,
                           capture_output=True, timeout=30)
            subprocess.run([GIT, "-C", str(sidekick), "remote", "add", "origin",
                            "https://github.com/folke/sidekick.nvim.git"], env=env,
                           check=True, capture_output=True, timeout=30)
            subprocess.run([GIT, "-C", str(sidekick), "fetch", "--quiet", "--depth", "1",
                            "origin", SIDEKICK_COMMIT], env=env, check=True,
                           capture_output=True, timeout=120)
            subprocess.run([GIT, "-C", str(sidekick), "checkout", "--quiet", "--detach",
                            "FETCH_HEAD"], env=env, check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            cls._plugins_tmp.cleanup()
            raise unittest.SkipTest(f"cannot fetch pinned Sidekick fixtures: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._plugins_tmp.cleanup()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sidekick-runtime-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.cache = self.root / "cache"
        self.bin = self.root / "bin"
        self.project = self.root / "project"
        self.tmux_tmp = self.root / "tmux"
        for path in (self.home / ".local/bin", self.state, self.data, self.config,
                     self.cache, self.bin, self.project, self.tmux_tmp):
            path.mkdir(parents=True, exist_ok=True)
        self.tmux_tmp.chmod(0o700)
        self.socket_name = f"sidekick-{os.getpid()}-{id(self) & 0xffff:x}"
        self.log = self.root / "fake-copilot.log"
        self.result = self.root / "result.json"
        self.sample = self.project / "sample.lua"
        self.sample.write_text("alpha selected token\nbeta\n", encoding="utf-8")

        _write_executable(self.home / ".local/bin/copilot", """#!/bin/sh
printf 'START\\t%s\\t%s\\n' "$PWD" "$*" >> "$SIDEKICK_TEST_LOG"
while IFS= read -r line; do
  printf 'INPUT\\t%s\\n' "$line" >> "$SIDEKICK_TEST_LOG"
done
""")
        _write_executable(self.bin / "tmux", f"""#!/bin/sh
printf 'TMUX\\t%s\\n' "$*" >> "$SIDEKICK_TEST_LOG"
case "${{1-}}" in -L|-S) exit 90;; esac
exec {TMUX} -L "$SIDEKICK_TEST_SOCKET" "$@"
""")

        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "USER": pwd.getpwuid(os.getuid()).pw_name,
            "LOGNAME": pwd.getpwuid(os.getuid()).pw_name,
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.cache),
            "TMUX_TMPDIR": str(self.tmux_tmp),
            "SIDEKICK_TEST_SOCKET": self.socket_name,
            "SIDEKICK_TEST_LOG": str(self.log),
            "SIDEKICK_TEST_RESULT": str(self.result),
            "SIDEKICK_TEST_ROOT": str(REPO_ROOT),
            "SIDEKICK_TEST_LAZY": str(self.plugins / "lazy.nvim"),
            "SIDEKICK_TEST_PLUGIN": str(self.plugins / "sidekick.nvim"),
            "SIDEKICK_TEST_PROJECT": str(self.project),
        }

    def tearDown(self) -> None:
        subprocess.run([TMUX, "-L", self.socket_name, "kill-server"], env=self.env,
                       check=False, capture_output=True, timeout=15)
        self._tmp.cleanup()

    def _run(self, enabled: bool) -> subprocess.CompletedProcess[str]:
        choices = self.state / "personal-dotfiles" / "choices.json"
        choices.parent.mkdir(parents=True, exist_ok=True)
        choices.write_text(json.dumps({"copilot": False, "copilot-cli": enabled}),
                           encoding="utf-8")
        env = dict(self.env, SIDEKICK_TEST_ENABLED="1" if enabled else "0")
        return subprocess.run(
            [NVIM, "--headless", "-u", "NONE", "-l", str(FIXTURE)],
            cwd=self.project, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=90)

    def test_disabled_choice_does_not_enable_sidekick(self) -> None:
        completed = self._run(False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result, {"enabled": False, "loaded": False})
        self.assertFalse(self.log.exists())

    def test_real_sidekick_uses_fake_copilot_and_private_tmux(self) -> None:
        completed = self._run(True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertTrue(result["enabled"])
        self.assertFalse(result["nes"])
        self.assertTrue(result["dirty_preserved"])
        self.assertEqual(result["starts"], 1)
        self.assertTrue(result["file_context"])
        self.assertTrue(result["selection_context"])
        self.assertTrue(result["diagnostic_context"])

        log = self.log.read_text(encoding="utf-8")
        self.assertIn(f"START\t{self.project}\t--banner", log)
        self.assertIn("INPUT\t@sample.lua", log)
        self.assertIn("alpha selected token", log)
        self.assertIn("runtime diagnostic token", log)
        self.assertNotRegex(log, r"TMUX\t.*send-keys.*(?:Enter|C-m)",
                            "context mappings should leave text for review, not submit it")
        self.assertNotRegex(log, r"--(?:dangerously-)?(?:allow|bypass|yolo)")
        socket = self.tmux_tmp / f"tmux-{os.getuid()}" / self.socket_name
        self.assertTrue(socket.exists(), "Sidekick did not use the private tmux socket")


if __name__ == "__main__":
    unittest.main()
