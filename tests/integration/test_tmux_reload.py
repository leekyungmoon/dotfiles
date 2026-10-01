"""A tmux server that predates the install must pick up the new ~/.tmux.conf.

Each test runs a real tmux server on the *default* socket inside a private
TMUX_TMPDIR (so the code under test can address "the user's default server"
exactly as on a real machine) with TMUX/TMUX_PANE removed, and kills it only
through that socket. The caller's real tmux server is never addressed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import phases  # noqa: E402
from installer.platform import Target  # noqa: E402
from installer.runner import Runner  # noqa: E402

TMUX = shutil.which("tmux", path="/usr/bin:/bin") or shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is not installed")
class ReloadRunningTmuxTests(unittest.TestCase):
    def setUp(self):
        # Short paths: a unix socket path must fit in 107 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="pdfrl", dir="/tmp"))
        self.home = self.root / "h"
        self.home.mkdir()
        self.tmpdir = self.root / "t"
        self.tmpdir.mkdir(mode=0o700)
        self.socket = self.tmpdir / f"tmux-{os.getuid()}" / "default"
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                    "TERM": "xterm-256color", "SHELL": "/bin/sh",
                    "TMUX_TMPDIR": str(self.tmpdir)}
        self.target = Target(uid=os.getuid(), gid=os.getgid(), username="fixture",
                             home=self.home, data_home=self.home / ".local/share",
                             state_home=self.home / ".local/state",
                             config_home=self.home / ".config",
                             cache_home=self.home / ".cache")
        self.conf = self.home / ".tmux.conf"

    def tearDown(self):
        if self.socket.exists() and str(self.socket).startswith(str(self.tmpdir)):
            subprocess.run([TMUX, "-S", str(self.socket), "kill-server"],
                           env=self.env, capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def tmux(self, *args):
        return subprocess.run([TMUX, "-S", str(self.socket), *args], env=self.env,
                              capture_output=True, text=True)

    def start_server(self):
        # No -S/-L: the default socket, inside the private TMUX_TMPDIR.
        subprocess.run([TMUX, "new-session", "-d", "-s", "work"], env=self.env,
                       check=True, capture_output=True)
        self.assertTrue(self.socket.exists())

    def binding(self, key):
        out = self.tmux("list-keys", "-T", "prefix", key).stdout.strip()
        return out.split(None, 4)[-1] if out else ""

    def write_conf(self, path: Path, text: str):
        path.write_text(text)
        if self.conf.is_symlink() or self.conf.exists():
            self.conf.unlink()
        self.conf.symlink_to(path)

    def test_stale_server_gets_the_new_bindings_and_keeps_sessions(self):
        # Before install: an older config where s picks a session, v is unbound.
        self.write_conf(self.home / "old.conf",
                        "bind-key s choose-tree -Zs\nunbind-key v\n")
        self.start_server()
        self.assertIn("choose-tree", self.binding("s"))
        self.assertEqual(self.binding("v"), "")
        # The install points ~/.tmux.conf at the new config; then post-install.
        self.write_conf(self.home / "new.conf",
                        'bind-key s split-window -v\nbind-key v split-window -h\n')
        reasons, outcome = phases.reload_running_tmux(self.target, Runner(), self.env)
        self.assertEqual(outcome, "reloaded")
        self.assertEqual(reasons, [phases.TMUX_RELOADED])
        self.assertIn("split-window -v", self.binding("s"))
        self.assertIn("split-window -h", self.binding("v"))
        self.assertEqual(self.tmux("list-sessions", "-F", "#{session_name}").stdout.split(),
                         ["work"])

    def test_inherited_tmux_variable_never_redirects_the_reload(self):
        self.write_conf(self.home / "old.conf", "bind-key s choose-tree -Zs\n")
        self.start_server()
        self.write_conf(self.home / "new.conf", "bind-key s split-window -v\n")
        env = dict(self.env, TMUX="/nonexistent/socket,1,0", TMUX_PANE="%1")
        _, outcome = phases.reload_running_tmux(self.target, Runner(), env)
        self.assertEqual(outcome, "reloaded")
        self.assertIn("split-window -v", self.binding("s"))

    def test_server_running_a_config_outside_this_home_is_left_alone(self):
        outside = self.root / "elsewhere.conf"
        outside.write_text("bind-key s choose-tree -Zs\n")
        subprocess.run([TMUX, "-f", str(outside), "new-session", "-d", "-s", "x"],
                       env=self.env, check=True, capture_output=True)
        self.write_conf(self.home / "new.conf", "bind-key s split-window -v\n")
        reasons, outcome = phases.reload_running_tmux(self.target, Runner(), self.env)
        self.assertEqual((reasons, outcome), ([], "running-server-uses-another-config"))
        self.assertIn("choose-tree", self.binding("s"))

    def test_no_running_server_is_a_no_op(self):
        self.write_conf(self.home / "new.conf", "bind-key s split-window -v\n")
        reasons, outcome = phases.reload_running_tmux(self.target, Runner(), self.env)
        self.assertEqual((reasons, outcome), ([], "no-running-server"))
        self.assertFalse(self.socket.exists())

    def test_broken_new_config_is_reported_not_fatal(self):
        self.write_conf(self.home / "old.conf", "bind-key s choose-tree -Zs\n")
        self.start_server()
        self.write_conf(self.home / "new.conf", "this-is-not-a-tmux-command\n")
        reasons, outcome = phases.reload_running_tmux(self.target, Runner(), self.env)
        self.assertEqual(outcome, "reload-failed")
        self.assertTrue(reasons and "could not reload" in reasons[0])


if __name__ == "__main__":
    unittest.main()
