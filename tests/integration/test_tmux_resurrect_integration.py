"""Exercise the pinned upstream tmux-resurrect/continuum with this repo's files.

Isolation: every tmux server here is private (-S <socket in a temp dir>), the
inherited TMUX/TMUX_PANE are dropped, and the environment is rebuilt from
scratch with a temporary HOME/XDG tree.  Upstream scripts and the hooks call a
bare `tmux`; they reach only the private server through the TMUX variable set
below (TMUX_TMPDIR also points into the temp dir).  `claude`, `codex` and
`systemctl` are fakes on PATH, so no real AI session, user manager or live tmux
server is touched.  The pinned plugins are cloned read-only from GitHub; the
tests skip when that is impossible.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TMUX_DIR = REPO_ROOT / "tmux"
PLUGINS = ("tmux-resurrect", "tmux-continuum")
ID_A = "11111111-2222-4333-8444-555555555555"
ID_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SHELL = "bash --noprofile --norc -i"

FAKE_AI = """#!/bin/bash
printf '%s\\n' "$(basename "$0") $*" >> "$HOME/ai-trace"
exec -a "$0" python3 -c 'import time; time.sleep(600)' "$@"
"""
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/systemctl.log"
case "$*" in
    *is-enabled*) [ -e "$HOME/systemctl-enabled" ] ;;
    *) exit 0 ;;
esac
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class ResurrectIntegration(unittest.TestCase):
    plugin_dir: Path
    tmux_binary: str
    _class_tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        tmux = shutil.which("tmux")
        missing = [
            name for name in ("git", "bash", "flock", "python3")
            if shutil.which(name) is None
        ]
        if tmux is None or missing:
            raise unittest.SkipTest(f"missing tools: tmux={tmux} {missing}")
        cls.tmux_binary = tmux
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="tmux-restore-plugins-")
        cls.plugin_dir = Path(cls._class_tmp.name) / "plugins"
        manifest = json.loads(
            (REPO_ROOT / "manifests/tmux-plugins.json").read_text(encoding="utf-8")
        )
        pins = {plugin["name"]: plugin for plugin in manifest["plugins"]}
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": cls._class_tmp.name,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        for name in PLUGINS:
            dest = cls.plugin_dir / name
            try:
                subprocess.run(
                    ["git", "clone", "--quiet", "--no-checkout",
                     pins[name]["url"], str(dest)],
                    env=git_env, check=True, capture_output=True, timeout=180,
                )
                subprocess.run(
                    ["git", "-C", str(dest), "checkout", "--quiet",
                     pins[name]["commit"]],
                    env=git_env, check=True, capture_output=True, timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                cls._class_tmp.cleanup()
                raise unittest.SkipTest(f"cannot clone pinned {name}: {error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_tmp.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tmux-restore-test-")
        root = Path(self.tmp.name)
        self.root = root
        self.socket = str(root / "socket")
        self.home = root / "home"
        self.data = root / "data"
        self.config = self.home / ".config"
        self.bin = root / "bin"
        self.work = root / "work"
        for directory in (self.home, self.config, self.bin, self.data / "tmux"):
            directory.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma", "delta"):
            (self.work / name).mkdir(parents=True)
        (self.home / ".dotfiles").symlink_to(REPO_ROOT)
        (self.data / "tmux/plugins").symlink_to(self.plugin_dir)
        self.state = self.data / "tmux/resurrect"
        (self.bin / "tmux").symlink_to(self.tmux_binary)
        write_executable(self.bin / "claude", FAKE_AI)
        write_executable(self.bin / "codex", FAKE_AI)
        write_executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        # Built from scratch: nothing from the live session (TMUX, TMUX_PANE,
        # DISPLAY, CODEX_HOME, ...) leaks into the private server.
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "SHELL": "/bin/bash",
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TMUX_TMPDIR": str(root),
            "TMUX": f"{self.socket},0,0",
        }
        self.bootstrap()

    def tearDown(self) -> None:
        self.tmux("kill-server", check=False)
        time.sleep(0.1)
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------

    def run_command(self, argv, check=True, timeout=60):
        return subprocess.run(
            [str(arg) for arg in argv], env=self.env, check=check,
            capture_output=True, text=True, timeout=timeout,
        )

    def tmux(self, *args, check=True):
        return self.run_command(
            [self.tmux_binary, "-S", self.socket, *args], check=check
        ).stdout.strip()

    def option(self, name):
        return self.tmux("show-option", "-gqv", name)

    def bootstrap(self):
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "bootstrap", SHELL)
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        for name, value in {
            "base-index": "1",
            "pane-base-index": "1",
            "default-shell": "/bin/bash",
            "default-command": SHELL,
        }.items():
            self.tmux("set-option", "-g", name, value)

    def plugin_script(self, plugin, relative):
        return self.data / "tmux/plugins" / plugin / relative

    def save(self, check=True):
        return self.run_command([TMUX_DIR / "resurrect-save"], check=check)

    def restore(self):
        return self.run_command(
            [self.plugin_script("tmux-resurrect", "scripts/restore.sh")]
        )

    def panes(self):
        return sorted(self.tmux(
            "list-panes", "-a", "-F",
            "#{session_name}|#{window_index}|#{pane_index}|#{pane_current_path}",
        ).splitlines())

    def geometry(self):
        return sorted(self.tmux(
            "list-panes", "-a", "-F",
            "#{session_name}|#{window_index}|#{pane_index}|#{pane_left}|"
            "#{pane_top}|#{pane_width}|#{pane_height}",
        ).splitlines())

    def window_names(self):
        return sorted(self.tmux(
            "list-windows", "-a", "-F",
            "#{session_name}|#{window_index}|#{window_name}",
        ).splitlines())

    def trace(self):
        path = self.home / "ai-trace"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def wait_for_trace(self, expected, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.trace()) >= expected:
                time.sleep(0.5)  # let the processes exec; catch extra launches
                return self.trace()
            time.sleep(0.1)
        self.fail(f"AI launches not observed: {self.trace()!r}")

    def fixture(self, with_ai=False):
        self.tmux("new-session", "-d", "-s", "shell", "-c", self.work / "alpha")
        self.tmux("split-window", "-d", "-t", "shell:1", "-c", self.work / "beta")
        self.tmux("new-session", "-d", "-s", "weekly", "-c", self.work / "gamma")
        self.tmux("new-window", "-d", "-t", "weekly:2", "-c", self.work / "delta")
        if with_ai:
            self.tmux("new-session", "-d", "-s", "ai", "-n", "claude-a",
                      "-c", self.work / "alpha")
            self.tmux("new-window", "-d", "-t", "ai:2", "-n", "codex-b",
                      "-c", self.work / "beta")
            self.tmux("new-window", "-d", "-t", "ai:3", "-n", "claude-unknown",
                      "-c", self.work / "gamma")
            self.tmux("send-keys", "-t", "ai:1.1", f"claude --resume {ID_A}", "Enter")
            self.tmux("send-keys", "-t", "ai:2.1", f"codex resume {ID_B}", "Enter")
            self.tmux("send-keys", "-t", "ai:3.1", "claude", "Enter")
            self.wait_for_trace(3)
        self.tmux("kill-session", "-t", "bootstrap")

    def restart_server(self):
        self.tmux("kill-server")
        time.sleep(0.2)
        self.bootstrap()

    # tests -----------------------------------------------------------------

    def test_config_resolves_portable_paths_and_keeps_wrapper_binding(self):
        self.assertEqual(self.option("@resurrect-dir"), str(self.state))
        self.assertEqual(self.option("@continuum-save-interval"), "0")
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(self.option("@continuum-restore"), "on")
        self.assertEqual(
            self.run_command(
                ["bash", "-c", 'eval "printf %s $1"', "-",
                 self.option("@resurrect-hook-post-save-layout")]
            ).stdout,
            str(self.home / ".dotfiles/tmux/resurrect-ai-session.py"),
        )
        # Plugin load (what TPM runs) must not replace the wrapper binding.
        self.run_command([self.plugin_script("tmux-resurrect", "resurrect.tmux")])
        save_key = self.tmux("list-keys", "-T", "prefix", "C-s")
        self.assertIn("resurrect-save", save_key)
        self.assertNotIn("save.sh", save_key)
        self.assertIn("restore.sh", self.tmux("list-keys", "-T", "prefix", "C-r"))

    def test_continuum_boot_never_writes_or_toggles_the_unit(self):
        pid1 = subprocess.run(["ps", "-o", "comm=", "-p", "1"],
                              capture_output=True, text=True).stdout.strip()
        if pid1 != "systemd":
            self.skipTest("continuum's boot handling only runs under systemd")
        handler = self.plugin_script(
            "tmux-continuum", "scripts/handle_tmux_automatic_start.sh"
        )
        unit = self.config / "systemd/user/tmux.service"
        log = self.home / "systemctl.log"

        def continuum_calls():
            log.unlink(missing_ok=True)
            self.run_command([handler])
            return log.read_text().splitlines() if log.exists() else []

        # No managed unit: "off", continuum's disable is a no-op, nothing written.
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(continuum_calls(), ["--user disable tmux.service"])
        self.assertFalse(unit.exists())

        # Managed and enabled: "on", continuum only checks is-enabled.
        unit.parent.mkdir(parents=True)
        unit_bytes = (REPO_ROOT / "systemd/user/tmux.service").read_bytes()
        unit.write_bytes(unit_bytes)
        (self.home / "systemctl-enabled").touch()
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        self.assertEqual(self.option("@continuum-boot"), "on")
        self.assertEqual(continuum_calls(), ["--user is-enabled tmux.service"])
        self.assertEqual(unit.read_bytes(), unit_bytes)

        # Disabled by the user: stays disabled (only a no-op disable).
        (self.home / "systemctl-enabled").unlink()
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(continuum_calls(), ["--user disable tmux.service"])
        self.assertEqual(unit.read_bytes(), unit_bytes)

    def test_headless_roundtrip_restores_layout_paths_and_proven_ai(self):
        self.fixture(with_ai=True)
        expected = self.panes()
        geometry = self.geometry()
        names = self.window_names()
        self.assertEqual(len(expected), 7)
        self.assertEqual(self.tmux("list-clients"), "")
        self.save()

        last = self.state / "last"
        self.assertTrue(last.is_file())
        self.assertTrue((self.state / "pane_contents.tar.gz").is_file())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(last.stat().st_mode), 0o600)
        commands = {
            (fields[1], fields[2]): fields[10]
            for fields in (line.split("\t") for line in last.read_text().splitlines())
            if fields[0] == "pane"
        }
        self.assertEqual(commands[("ai", "1")], f":{self.bin}/claude --resume {ID_A}")
        self.assertEqual(commands[("ai", "2")], f":codex resume {ID_B}")
        self.assertEqual(commands[("ai", "3")], ":")

        self.restart_server()
        (self.home / "ai-trace").unlink()
        self.restore()
        self.tmux("kill-session", "-t", "bootstrap")
        self.assertEqual(self.panes(), expected)
        self.assertEqual(self.geometry(), geometry)
        self.assertEqual(self.window_names(), names)
        self.assertEqual(self.tmux("list-clients"), "")
        # Only proven IDs are resumed; the unresolved Claude pane stays a shell.
        self.assertEqual(sorted(self.wait_for_trace(2)), sorted([
            f"claude --resume {ID_A}", f"codex resume {ID_B}",
        ]))

        before = self.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        self.restore()
        self.assertEqual(
            self.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"), before
        )
        time.sleep(0.5)
        self.assertEqual(len(self.trace()), 2)

    def test_failed_hook_keeps_previous_snapshot(self):
        self.fixture()
        self.tmux("set-environment", "-g", "TMUX_PLUGIN_MANAGER_PATH",
                  f"{self.data}/tmux/plugins/")
        self.save()
        previous = os.readlink(self.state / "last")
        previous_data = (self.state / "last").read_bytes()
        self.tmux("new-window", "-d", "-t", "weekly:3")
        self.tmux("set-option", "-g", "@resurrect-hook-post-save-layout", "/bin/false")
        time.sleep(1.1)  # a new one-second snapshot name
        failed = self.save(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("previous snapshot preserved", failed.stderr)
        self.assertEqual(os.readlink(self.state / "last"), previous)
        self.assertEqual((self.state / "last").read_bytes(), previous_data)

    def test_login_bootstrap_is_removed_only_after_restore(self):
        self.fixture()
        expected = self.panes()
        self.save()
        self.restart_server()
        self.tmux("rename-session", "-t", "bootstrap", "__continuum_startup")
        hook = self.option("@resurrect-hook-post-restore-all")
        self.assertIn("__continuum_startup", hook)
        # With nothing restored yet, cleanup must keep the only live session.
        self.run_command(["/bin/bash", "-c", hook])
        self.assertEqual(
            self.tmux("list-sessions", "-F", "#{session_name}"), "__continuum_startup"
        )
        self.restore()  # runs the same hook via resurrect after restoring
        self.assertEqual(self.panes(), expected)
        self.assertEqual(self.tmux("list-clients"), "")


if __name__ == "__main__":
    unittest.main()
