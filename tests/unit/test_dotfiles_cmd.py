"""bin/dotfiles: argument table, delegation to install.py, the update flow and
the refresh of the invoking terminal.

Every test runs the working-tree ``bin/dotfiles`` with ``HOME`` pointing at a
temporary home whose ``~/.dotfiles`` is a scratch clone of a scratch bare
origin. Its ``install.py`` is a fake that records how it was called, so no
test ever installs anything. git runs with a private global config.

The terminal-refresh tests load bin/dotfiles as a module and give it real
parent processes (bash/zsh on a pseudo-terminal, in a temporary HOME and
XDG_STATE_HOME) and a fake ``execvp``. A "hooked" parent holds a file
descriptor on the temporary {state}/personal-dotfiles/shell-hook, either
inherited from the test or opened by the real zsh/zsh.d/dotfiles-reload.zsh.
The end-to-end tests type ``dotfiles repair`` into real shells on a pty and
check that the passwd login shell takes over the terminal only for an
unhooked, interactive, foreground parent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pty
import pwd
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTFILES = REPO_ROOT / "bin" / "dotfiles"

FAKE_INSTALL = r'''
import json, os, sys
with open(os.environ["FAKE_INSTALL_LOG"], "a") as log:
    log.write(json.dumps({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "no_bytecode": bool(sys.flags.dont_write_bytecode),
        "tracked": open("tracked.txt").read(),
    }) + "\n")
if os.environ.get("FAKE_GENERATION"):  # as install.py records the generation
    folder = os.path.join(os.environ["XDG_STATE_HOME"], "personal-dotfiles")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    with open(os.path.join(folder, "generation"), "w") as handle:
        handle.write(os.environ["FAKE_GENERATION"] + "\n")
sys.exit(int(os.environ.get("FAKE_INSTALL_RC", "0")))
'''

TOOLS = {"schema": 1, "tools": {"node": {}, "neovim": {}, "fzf": {}}}


class DotfilesCmdCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-cmd-")
        root = Path(self._tmp.name)
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.log = root / "install.log"
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Fixture\n\temail = f@example.invalid\n"
                             "[init]\n\tdefaultBranch = main\n"
                             "[advice]\n\tdetachedHead = false\n"
                             "[protocol \"file\"]\n\tallow = always\n")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "NO_COLOR": "1",
            "FAKE_INSTALL_LOG": str(self.log),
            "XDG_STATE_HOME": str(root / "state"),
        }
        # upstream work tree -> bare origin -> ~/.dotfiles clone
        self.work = root / "work"
        self.git("init", "--quiet", str(self.work))
        (self.work / "install.py").write_text(FAKE_INSTALL)
        (self.work / "tracked.txt").write_text("original\n")
        (self.work / "other.txt").write_text("v1\n")
        (self.work / "manifests").mkdir()
        (self.work / "manifests" / "tools.json").write_text(json.dumps(TOOLS))
        self.git("-C", str(self.work), "add", ".")
        self.git("-C", str(self.work), "commit", "--quiet", "-m", "initial")
        self.origin = root / "origin.git"
        self.git("clone", "--quiet", "--bare", str(self.work), str(self.origin))
        self.git("-C", str(self.work), "remote", "add", "origin", str(self.origin))
        self.git("-C", str(self.work), "fetch", "--quiet", "origin")
        self.git("-C", str(self.work), "branch", "--quiet", "-u", "origin/main")
        self.dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.origin), str(self.dotfiles))

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, check=True) -> str:
        completed = subprocess.run(["git", *args], env=self.env, check=check,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=60)
        return completed.stdout.decode().strip()

    def head(self, repo=None) -> str:
        return self.git("-C", str(repo or self.dotfiles), "rev-parse", "HEAD")

    def push_upstream(self, name="other.txt", text="v2\n", message="upstream change"):
        (self.work / name).write_text(text)
        self.git("-C", str(self.work), "commit", "--quiet", "-am", message)
        self.git("-C", str(self.work), "push", "--quiet", "origin", "HEAD:main")
        return self.head(self.work)

    def add_submodule(self):
        """Give upstream (and the clone) a submodule ``sub`` with a tracked file."""
        sub = self.root / "subsrc"
        self.git("init", "--quiet", str(sub))
        (sub / "f").write_text("sub v1\n")
        self.git("-C", str(sub), "add", "f")
        self.git("-C", str(sub), "commit", "--quiet", "-m", "sub")
        self.git("-C", str(self.work), "submodule", "--quiet", "add", str(sub), "sub")
        self.git("-C", str(self.work), "commit", "--quiet", "-m", "add submodule")
        self.git("-C", str(self.work), "push", "--quiet", "origin", "HEAD:main")
        self.git("-C", str(self.dotfiles), "pull", "--quiet", "--ff-only")
        self.git("-C", str(self.dotfiles), "submodule", "--quiet", "update", "--init")

    def stash_list(self) -> list[str]:
        out = self.git("-C", str(self.dotfiles), "stash", "list", "--format=%H %gs")
        return out.splitlines()

    def dotfiles_cmd(self, *args, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run([sys.executable, "-B", str(DOTFILES), *args], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120, cwd=str(self.root))

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class ArgumentTableTests(DotfilesCmdCase):
    def test_help_lists_upstream_and_delegating_commands(self):
        completed = self.dotfiles_cmd("--help")
        out = completed.stdout.decode()
        for word in ("update", "github", "install", "status", "restore", "repair"):
            self.assertIn(word, out)
        self.assertIn("@leekyungmoon's", out)
        self.assertNotIn("wookayin", out)

    def test_no_arguments_prints_help_and_fails(self):
        completed = self.dotfiles_cmd()
        self.assertEqual(completed.returncode, 1)
        self.assertIn(b"Available commands", completed.stdout)

    def test_update_flags(self):
        out = self.dotfiles_cmd("update", "--help").stdout.decode()
        for flag in ("--fast", "--skip-zplug", "--skip-vimplug"):
            self.assertIn(flag, out)

    def test_no_bytecode_written_into_the_checkout(self):
        self.dotfiles_cmd("status")
        self.assertEqual(list(REPO_ROOT.joinpath("bin").glob("__pycache__")), [])
        self.assertEqual(list(self.dotfiles.rglob("__pycache__")), [])


class DelegationTests(DotfilesCmdCase):
    def test_install_without_target_lists_pinned_tools(self):
        completed = self.dotfiles_cmd("install")
        self.assertEqual(completed.returncode, 1)
        out = completed.stdout.decode()
        self.assertIn("Available packages:", out)
        for name in ("node", "neovim", "fzf"):
            self.assertIn(f"- {name}", out)
        self.assertEqual(self.calls(), [])

    def test_install_rejects_unknown_target(self):
        completed = self.dotfiles_cmd("install", "fzf", "not-a-real-package")
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"nothing was changed", completed.stdout)
        self.assertEqual(self.calls(), [])

    def test_install_delegates_each_target(self):
        completed = self.dotfiles_cmd("install", "fzf", "node", "--force")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls],
                         [["packages", "--only", "fzf", "--force"],
                          ["packages", "--only", "node", "--force"]])
        self.assertTrue(all(c["no_bytecode"] for c in calls))
        self.assertTrue(all(Path(c["cwd"]) == self.dotfiles for c in calls))
        out = completed.stdout.decode()
        self.assertIn("[*] Installation successful: fzf", out)
        self.assertIn("[*] Installation successful: node", out)

    def test_install_failure_stops(self):
        completed = self.dotfiles_cmd("install", "fzf", "node",
                                      extra_env={"FAKE_INSTALL_RC": "3"})
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn(b"Installation successful", completed.stdout)

    def test_status_restore_repair_delegate(self):
        self.assertEqual(self.dotfiles_cmd("status", "--json").returncode, 0)
        self.assertEqual(self.dotfiles_cmd("restore", "--baseline", "--id", "zshrc",
                                           "--id", "vimrc", "--force").returncode, 0)
        self.assertEqual(self.dotfiles_cmd("restore", "--run",
                                           "20260101T000000Z-0123abcd").returncode, 0)
        self.assertEqual(self.dotfiles_cmd("repair", "--no-gui",
                                           "--skip-vimplug").returncode, 0)
        self.assertEqual([c["argv"] for c in self.calls()], [
            ["status", "--json"],
            ["restore", "--baseline", "--force", "--id", "zshrc", "--id", "vimrc"],
            ["restore", "--run", "20260101T000000Z-0123abcd"],
            ["repair", "--skip-vimplug", "--no-gui"],
        ])

    def test_unknown_arguments_are_refused(self):
        completed = self.dotfiles_cmd("status", "--bogus")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_github_opens_or_prints_the_url(self):
        record = self.root / "browser.log"
        ok = self.root / "fake-browser"
        ok.write_text(f"#!/bin/sh\necho \"$1\" >> '{record}'\n")
        ok.chmod(0o755)
        bad = self.root / "bad-browser"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        base = {"PATH": str(self.root / "no-such-dir"), "DISPLAY": "",
                "WAYLAND_DISPLAY": "", "TERM": "dumb"}
        completed = self.dotfiles_cmd("github", extra_env=dict(base, BROWSER=f"{ok} %s"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(record.read_text().strip(),
                         "https://github.com/leekyungmoon/dotfiles")
        completed = self.dotfiles_cmd("github", extra_env=dict(base, BROWSER=str(bad)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(b"https://github.com/leekyungmoon/dotfiles", completed.stdout)


class UpdateFlowTests(DotfilesCmdCase):
    def test_clean_fast_forward_update(self):
        old = self.head()
        new = self.push_upstream()
        completed = self.dotfiles_cmd("update")
        out = completed.stdout.decode()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.head(), new)
        self.assertIn("[*] Update complete!", out)
        self.assertIn(f"Changelog: {old[:7]}", out)
        self.assertIn("upstream change", out)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], [[]])
        self.assertTrue(calls[0]["no_bytecode"])
        self.assertEqual(Path(calls[0]["cwd"]), self.dotfiles)
        # upstream's set -x echo of the commands
        err = completed.stderr.decode()
        self.assertIn("git fetch origin", err)
        self.assertIn("git merge --ff-only", err)
        self.assertIn("git submodule update --init --recursive", err)
        self.assert_trace_stops_after_install(err)

    def assert_trace_stops_after_install(self, err):
        """The trace ends with the install step: the bookkeeping after it
        ('[ 0 = 1 ]', 'exit 0', the stash checks) is not echoed."""
        traced = [line for line in err.splitlines() if line.startswith("+")]
        installs = [i for i, line in enumerate(traced) if "install.py" in line]
        self.assertTrue(installs, traced)
        after = traced[installs[-1] + 1:]
        self.assertTrue(all(re.fullmatch(r"\+ ret=\d+", line) for line in after), after)
        for line in traced:
            self.assertNotRegex(line, r"\bexit\b|'\[' 0 = 1|\[ 0 = 1|set \+x|stash apply"
                                      r"|stash drop|\$stashed|\"\$stashed\"")

    def test_fast_passes_skip_flags(self):
        self.push_upstream()
        completed = self.dotfiles_cmd("update", "--fast")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.calls()[0]["argv"], ["--skip-zplug", "--skip-vimplug"])

    def test_up_to_date(self):
        head = self.git("-C", str(self.dotfiles), "rev-parse", "--short", "HEAD")
        completed = self.dotfiles_cmd("update", "--skip-zplug")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"[*] dotfiles is up-to-date ({head}).", completed.stdout.decode())
        self.assertEqual(self.calls()[0]["argv"], ["--skip-zplug"])

    def test_dirty_tree_is_stashed_and_popped(self):
        (self.dotfiles / "tracked.txt").write_text("local edit\n")
        self.git("-C", str(self.dotfiles), "add", "tracked.txt")  # staged, kept --index
        (self.dotfiles / "untracked.txt").write_text("mine\n")
        new = self.push_upstream()
        completed = self.dotfiles_cmd("update")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.head(), new)
        # install.py ran on the clean, updated tree ...
        self.assertEqual(self.calls()[0]["tracked"], "original\n")
        self.assertIn("DOTFILES_UPDATE", completed.stderr.decode())
        # ... and the local edit came back, still staged.
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "local edit\n")
        staged = self.git("-C", str(self.dotfiles), "diff", "--cached", "--name-only")
        self.assertEqual(staged, "tracked.txt")
        self.assertEqual((self.dotfiles / "untracked.txt").read_text(), "mine\n")
        self.assertEqual(self.git("-C", str(self.dotfiles), "stash", "list"), "")

    def test_clean_tree_is_not_stashed(self):
        self.push_upstream()
        completed = self.dotfiles_cmd("update")
        self.assertNotIn("git stash push", completed.stderr.decode())

    def test_diverged_history_is_refused(self):
        (self.dotfiles / "local.txt").write_text("local commit\n")
        self.git("-C", str(self.dotfiles), "add", "local.txt")
        self.git("-C", str(self.dotfiles), "commit", "--quiet", "-m", "local")
        local = self.head()
        self.push_upstream()
        completed = self.dotfiles_cmd("update")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("[*] installer has failed. Check the log.", completed.stdout.decode())
        self.assertEqual(self.head(), local)
        self.assertEqual(self.calls(), [])

    def test_installer_failure_is_reported_and_stash_restored(self):
        (self.dotfiles / "tracked.txt").write_text("local edit\n")
        self.push_upstream()
        completed = self.dotfiles_cmd("update", extra_env={"FAKE_INSTALL_RC": "5"})
        self.assertEqual(completed.returncode, 5)
        self.assertIn("[*] installer has failed. Check the log.", completed.stdout.decode())
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "local edit\n")
        self.assertEqual(self.git("-C", str(self.dotfiles), "stash", "list"), "")
        self.assert_trace_stops_after_install(completed.stderr.decode())

    def test_stashed_update_does_not_trace_the_restore(self):
        (self.dotfiles / "tracked.txt").write_text("local edit\n")
        self.push_upstream()
        completed = self.dotfiles_cmd("update")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "local edit\n")
        self.assert_trace_stops_after_install(completed.stderr.decode())


class UpdateStashIdentityTests(DotfilesCmdCase):
    """Only the stash entry that this update created is applied and dropped."""

    def make_user_stash(self):
        (self.dotfiles / "tracked.txt").write_text("precious experiment\n")
        self.git("-C", str(self.dotfiles), "stash", "push", "--quiet", "-m", "my-old-experiment")
        entries = self.stash_list()
        self.assertEqual(len(entries), 1)
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "original\n")
        return entries[0]

    def test_submodule_only_dirt_does_not_pop_an_older_user_stash(self):
        self.add_submodule()
        user_stash = self.make_user_stash()
        (self.dotfiles / "sub" / "f").write_text("dirty submodule content\n")
        # the reported case: status shows the submodule, stash push saves nothing
        self.assertEqual(self.git("-C", str(self.dotfiles), "status", "--porcelain",
                                  "--untracked-files=no"), "M sub")  # ' M sub', stripped
        new = self.push_upstream()
        completed = self.dotfiles_cmd("update")
        out = completed.stdout.decode()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[*] Update complete!", out)
        self.assertEqual(self.head(), new)
        # the user's stash is untouched and nothing of it leaked into the tree
        self.assertEqual(self.stash_list(), [user_stash])
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "original\n")
        self.assertEqual((self.dotfiles / "sub" / "f").read_text(), "dirty submodule content\n")
        self.assertNotIn("git stash apply", completed.stderr.decode())

    def test_submodule_only_dirt_without_any_stash_is_a_successful_update(self):
        self.add_submodule()
        (self.dotfiles / "sub" / "f").write_text("dirty submodule content\n")
        self.push_upstream()
        completed = self.dotfiles_cmd("update")
        out = completed.stdout.decode()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[*] Update complete!", out)
        self.assertNotIn("installer has failed", out)
        self.assertEqual(self.stash_list(), [])
        self.assertEqual(len(self.calls()), 1)

    def test_own_stash_is_restored_and_older_user_stash_kept(self):
        user_stash = self.make_user_stash()
        (self.dotfiles / "tracked.txt").write_text("local edit\n")
        new = self.push_upstream()
        completed = self.dotfiles_cmd("update")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.head(), new)
        self.assertEqual(self.calls()[0]["tracked"], "original\n")
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "local edit\n")
        self.assertEqual(self.stash_list(), [user_stash])


class UpdateStashConflictTests(DotfilesCmdCase):
    """A local edit that conflicts with upstream never leaves conflict markers."""

    def setUp(self):
        super().setUp()
        (self.dotfiles / "tracked.txt").write_text("my local edit\n")
        self.new = self.push_upstream("tracked.txt", "upstream edit\n", "upstream edit")

    def assert_clean_and_recoverable(self, completed):
        out = completed.stdout.decode()
        self.assertEqual(self.head(), self.new)
        # the live file is the updated upstream version, without markers
        self.assertEqual((self.dotfiles / "tracked.txt").read_text(), "upstream edit\n")
        self.assertEqual(self.git("-C", str(self.dotfiles), "status", "--porcelain",
                                  "--untracked-files=no"), "")
        # the edit is kept in this update's stash entry, and the output says how
        entries = self.stash_list()
        self.assertEqual(len(entries), 1)
        sha, label = entries[0].split(" ", 1)
        self.assertIn("DOTFILES_UPDATE", label)
        self.assertEqual(self.git("-C", str(self.dotfiles), "show", f"{sha}:tracked.txt"),
                         "my local edit")
        self.assertIn("could not be re-applied", out)
        self.assertIn("no conflict markers were left behind", out)
        self.assertIn(f"stash@{{0}} ({sha})", out)
        self.assertIn(f"cd {self.dotfiles}", out)
        self.assertIn(f"git stash apply --index {sha}", out)
        self.assertIn("git stash drop stash@{0}", out)
        return out

    def test_conflicting_pop_restores_clean_tree_and_keeps_stash(self):
        completed = self.dotfiles_cmd("update")
        self.assertEqual(completed.returncode, 3, completed.stderr)
        out = self.assert_clean_and_recoverable(completed)
        self.assertNotIn("installer has failed", out)
        self.assertIn("[*] Update complete!", out)
        self.assertEqual(self.calls()[0]["tracked"], "upstream edit\n")

    def test_conflict_and_installer_failure_are_both_reported(self):
        completed = self.dotfiles_cmd("update", extra_env={"FAKE_INSTALL_RC": "5"})
        self.assertEqual(completed.returncode, 5, completed.stderr)
        out = self.assert_clean_and_recoverable(completed)
        self.assertIn("[*] installer has failed. Check the log.", out)

    def test_printed_recovery_brings_the_edit_back(self):
        completed = self.dotfiles_cmd("update")
        self.assertEqual(completed.returncode, 3, completed.stderr)
        sha = self.stash_list()[0].split(" ", 1)[0]
        apply = subprocess.run(["git", "-C", str(self.dotfiles), "stash", "apply", sha],
                               env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(apply.returncode, 0)  # the conflict is now the user's to resolve
        text = (self.dotfiles / "tracked.txt").read_text()
        self.assertIn("my local edit", text)
        self.assertIn("upstream edit", text)


# --- the invoking terminal ---------------------------------------------------

def load_dotfiles_module():
    sys.dont_write_bytecode = True
    loader = SourceFileLoader("dotfiles_cmd_under_test", str(DOTFILES))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeParent:
    """A real process on its own pseudo-terminal (fds 0/1/2), standing in for
    the shell that ran ``dotfiles``. With ``hold``, the child opens that file
    read-only (inherited across its exec), as a hooked zsh holds its mark."""

    def __init__(self, argv, env, exe=None, hold=None):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                if hold is not None:
                    os.set_inheritable(os.open(hold, os.O_RDONLY), True)
                os.execve(exe or argv[0], argv, env)
            finally:
                os._exit(127)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{self.pid}/cmdline", "rb") as handle:
                    if handle.read().split(b"\0")[0] == argv[0].encode():
                        break
            except OSError:
                pass
            time.sleep(0.02)
        self.tty = os.path.realpath(os.readlink(f"/proc/{self.pid}/fd/0"))
        self.buf = b""

    def read_until(self, pattern, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            match = re.search(pattern, self.buf)
            if match:
                out, self.buf = self.buf[:match.end()], self.buf[match.end():]
                return out.decode(errors="replace")
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {pattern!r} in {self.buf[-1500:]!r}")
            ready, _, _ = select.select([self.fd], [], [], left)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    raise AssertionError(f"terminal closed; output {self.buf[-1500:]!r}")
                self.buf += data

    def send(self, text):
        os.write(self.fd, text.encode())

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        os.close(self.fd)


BASH = shutil.which("bash") or "/bin/bash"
ZSH = shutil.which("zsh") or "/usr/bin/zsh"

# argv -> interactive? (bin/dotfiles and installer/phases.py must agree)
INVOCATIONS = [
    (["zsh"], True), (["-zsh"], True), (["zsh", "-l"], True), (["zsh", "-il"], True),
    (["bash", "-i"], True), (["-bash"], True), (["bash", "--login"], True),
    (["bash", "--norc", "--noprofile", "-i"], True), (["zsh", "+x"], True),
    (["-bash", "-c", "dotfiles update; true"], False), (["-zsh", "-c", "x"], False),
    (["bash", "-c", "x"], False), (["bash", "-ic", "x"], False), (["zsh", "-lc", "x"], False),
    (["bash", "script.sh"], False), (["-bash", "script.sh"], False),
    (["zsh", "-o", "vi"], False), (["bash", "--rcfile", "f"], False),
    (["bash", "--rcfile=f"], False), (["bash", "--init-command"], False),
    (["zsh", "-"], False), (["zsh", "--"], False), (["bash", "-s"], True),
    ([], False), ([""], False),
]


class RefreshTerminalTests(unittest.TestCase):
    """_refresh_terminal(): exec the login shell only for an interactive,
    unhooked zsh/bash parent in the foreground of this terminal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-refresh-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.zdot = self.root / "zdot"
        self.home.mkdir()
        self.zdot.mkdir()
        (self.zdot / ".zshenv").write_text("unsetopt GLOBAL_RCS\n")
        self.state = self.root / "state"
        self.hook = self.state / "personal-dotfiles" / "shell-hook"
        self.hook.parent.mkdir(parents=True, mode=0o700)
        self.hook.write_text("")
        self.uid = os.getuid()
        self.env = {"HOME": str(self.home), "ZDOTDIR": str(self.zdot), "TERM": "dumb",
                    "PATH": "/usr/bin:/bin", "LANG": "C", "PS1": "@@P@@ ",
                    "XDG_STATE_HOME": str(self.state)}
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state),
                                               "HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load_dotfiles_module()
        self.execs = []
        self.mod._execvp = lambda file, args: self.execs.append((file, list(args)))
        self.parents = []
        self.login_shell = pwd.getpwuid(self.uid).pw_shell

    def tearDown(self):
        for parent in self.parents:
            parent.close()
        self._tmp.cleanup()

    def parent(self, argv, exe=None, hold=None):
        p = FakeParent(argv, self.env, exe=exe, hold=hold)
        self.parents.append(p)
        return p

    def refresh(self, parent, stdin_tty=True, stdout_tty=True, tty=None, foreground=True):
        ttys = {0: stdin_tty, 1: stdout_tty}
        pgrp = os.getpgrp()
        with mock.patch("os.getppid", return_value=parent.pid), \
                mock.patch("os.isatty", side_effect=lambda fd: ttys.get(fd, False)), \
                mock.patch("os.ttyname", side_effect=lambda fd: tty or parent.tty), \
                mock.patch("os.tcgetpgrp",
                           side_effect=lambda fd: pgrp if foreground else pgrp + 1):
            return self.mod._refresh_terminal()

    def assert_exec(self, result):
        self.assertTrue(result)
        self.assertEqual(self.execs, [(self.login_shell,
                                       [os.path.basename(self.login_shell), "-l"])])

    def assert_no_exec(self, result):
        self.assertFalse(result)
        self.assertEqual(self.execs, [])

    def require_login_shell(self):
        if not (os.path.isabs(self.login_shell) and os.access(self.login_shell, os.X_OK)):
            self.skipTest(f"passwd login shell {self.login_shell!r} is not executable")

    # -- unhooked interactive zsh/bash: exec --------------------------------

    def test_unhooked_interactive_shells_get_the_login_shell(self):
        self.require_login_shell()
        for argv, exe in ((["bash", "--norc", "--noprofile", "-i"], BASH),
                          (["-bash", "--norc", "--noprofile"], BASH),
                          (["-zsh", "-f"], ZSH), (["zsh", "-f", "-l"], ZSH)):
            with self.subTest(argv):
                self.execs.clear()
                self.assert_exec(self.refresh(self.parent(argv, exe=exe)))

    # -- hooked parents: nothing (they reload themselves) -------------------

    def test_hooked_parent_is_left_to_its_own_reload(self):
        other = self.root / "other-state" / "personal-dotfiles" / "shell-hook"
        other.parent.mkdir(parents=True)
        other.write_text("")
        alias = self.state / "personal-dotfiles" / "same-inode"
        os.link(self.hook, alias)
        cases = {
            "bash holding the mark": (["bash", "--norc", "--noprofile", "-i"], BASH, self.hook),
            "zsh holding the mark": (["-zsh", "-f"], ZSH, self.hook),
            "mark of another XDG_STATE_HOME": (["-zsh", "-f"], ZSH, other),
            "same inode, other name": (["zsh", "-f"], ZSH, alias),
        }
        for name, (argv, exe, hold) in cases.items():
            with self.subTest(name):
                self.assert_no_exec(self.refresh(self.parent(argv, exe=exe, hold=hold)))
        with self.subTest("mark deleted after the shell opened it"):
            zsh = self.parent(["zsh", "-f"], exe=ZSH, hold=other)
            other.unlink()
            links = [os.readlink(f"/proc/{zsh.pid}/fd/{n}")
                     for n in os.listdir(f"/proc/{zsh.pid}/fd")]
            self.assertIn(f"{other} (deleted)", links)
            self.assert_no_exec(self.refresh(zsh))

    def test_real_hooked_zsh_is_left_alone(self):
        """A zsh that sourced zsh/zsh.d/dotfiles-reload.zsh holds the mark."""
        self.hook.unlink()
        (self.zdot / ".zshrc").write_text(
            f"source {REPO_ROOT / 'zsh/zsh.d/dotfiles-reload.zsh'}\n"
            "PS1='@@HOOKED@@ '\n")
        zsh = self.parent(["-zsh"], exe=ZSH)
        zsh.read_until(rb"@@HOOKED@@ ")
        self.assertTrue(self.hook.is_file())
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.mod._is_hooked(zsh.pid))
        self.assert_no_exec(self.refresh(zsh))
        # ... and 'exec bash' drops the mark (close-on-exec): exec again.
        self.require_login_shell()
        zsh.send("exec bash --norc --noprofile -i\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with open(f"/proc/{zsh.pid}/comm") as handle:
                if handle.read().strip() == "bash":
                    break
            time.sleep(0.05)
        self.assertFalse(self.mod._is_hooked(zsh.pid))
        self.assert_exec(self.refresh(zsh))

    def test_unreadable_fd_table_counts_as_hooked(self):
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        real_listdir = os.listdir

        def listdir(path):
            if str(path) == f"/proc/{bash.pid}/fd":
                raise PermissionError(13, "denied")
            return real_listdir(path)
        with mock.patch("os.listdir", side_effect=listdir):
            self.assert_no_exec(self.refresh(bash))

    # -- never without a terminal, a foreground job, an interactive parent --

    def test_no_terminal_no_exec(self):
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        self.assert_no_exec(self.refresh(bash, stdin_tty=False))
        self.assert_no_exec(self.refresh(bash, stdout_tty=False))
        self.assert_no_exec(self.refresh(bash, stdin_tty=False, stdout_tty=False))
        # Our terminal is not the parent's (e.g. run from another pane's shell).
        self.assert_no_exec(self.refresh(bash, tty="/dev/pts/999999"))

    def test_background_job_no_exec(self):
        for argv, exe in ((["bash", "--norc", "--noprofile", "-i"], BASH), (["-zsh", "-f"], ZSH)):
            with self.subTest(argv):
                self.assert_no_exec(self.refresh(self.parent(argv, exe=exe), foreground=False))

    def test_non_interactive_parents_are_left_alone(self):
        script = self.root / "script.sh"
        script.write_text("read -r answer\n")
        parents = {
            "-bash -c (su - -c, sudo -i CMD)":
                (["-bash", "-c", "read -r x; dotfiles update; true"], BASH),
            "-zsh -c": (["-zsh", "-c", "read -r x; true"], ZSH),
            "bash -c": (["bash", "-c", "read -r x; sleep 60"], BASH),
            "bash -ic": (["bash", "-ic", "read -r x"], BASH),
            "zsh -lc": (["zsh", "-lc", "read -r x"], ZSH),
            "bash script": (["bash", str(script)], BASH),
            "-bash script": (["-bash", str(script)], BASH),
            "zsh -o operand": (["zsh", "-f", "-o", "vi"], ZSH),
            "not a shell": (["sleep", "60"], shutil.which("sleep") or "/bin/sleep"),
            "argv[0] zsh, but python": (["zsh", "-i"], sys.executable),
            "argv[0] bash, but sh": (["bash", "-i"], shutil.which("sh") or "/bin/sh"),
        }
        for name, (argv, exe) in parents.items():
            with self.subTest(name):
                self.assert_no_exec(self.refresh(self.parent(argv, exe=exe)))

    def test_interactive_invocation_table(self):
        for argv, expected in INVOCATIONS:
            with self.subTest(argv):
                self.assertIs(self.mod.interactive_invocation(argv), expected)

    def test_interactive_invocation_parity_with_the_installer(self):
        sys.path.insert(0, str(REPO_ROOT))
        self.addCleanup(sys.path.remove, str(REPO_ROOT))
        from installer import phases
        words = ["-", "--", "+", "++", "-i", "-l", "-il", "-c", "-ic", "-lc", "-o", "vi",
                 "-x", "+x", "--login", "--norc", "--rcfile", "--rcfile=f", "--command",
                 "--init-command", "script.sh", "-1", "-s", "--posix"]
        argvs = [argv for argv, _ in INVOCATIONS]
        for zero in ("zsh", "-zsh", "bash", "-bash"):
            argvs.append([zero])
            for a in words:
                argvs.append([zero, a])
                for b in words[:12]:
                    argvs.append([zero, a, b])
        for argv in argvs:
            self.assertIs(self.mod.interactive_invocation(argv),
                          phases.interactive_invocation(argv), argv)

    def test_hook_detection_parity_with_the_installer(self):
        """bin/dotfiles and installer/phases.py agree on who is hooked (the
        installer's "cannot tell" counts as hooked, as it does there)."""
        sys.path.insert(0, str(REPO_ROOT))
        self.addCleanup(sys.path.remove, str(REPO_ROOT))
        from installer import phases
        other = self.root / "other-state" / "personal-dotfiles" / "shell-hook"
        other.parent.mkdir(parents=True)
        other.write_text("")
        gone = self.root / "gone-state" / "personal-dotfiles" / "shell-hook"
        gone.parent.mkdir(parents=True)
        gone.write_text("")
        procs = {
            "unhooked": self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH),
            "hooked": self.parent(["-zsh", "-f"], exe=ZSH, hold=self.hook),
            "other state": self.parent(["zsh", "-f"], exe=ZSH, hold=other),
            "deleted": self.parent(["zsh", "-f"], exe=ZSH, hold=gone),
            "stray file": self.parent(["zsh", "-f"], exe=ZSH, hold=self.root / "zdot" / ".zshenv"),
        }
        gone.unlink()
        expected = {"unhooked": False, "hooked": True, "other state": True, "deleted": True,
                    "stray file": False}
        for name, proc in procs.items():
            with self.subTest(name):
                mine = self.mod._is_hooked(proc.pid, str(self.hook))
                theirs = phases.shell_hooked(proc.pid, self.hook) is not False
                self.assertEqual((mine, theirs), (expected[name], expected[name]))

    def test_unusable_login_shell_no_exec(self):
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        for shell in ("", "/nonexistent/zsh", "/usr/sbin/nologin", "relative/zsh"):
            with self.subTest(shell):
                fake = mock.Mock(pw_shell=shell)
                with mock.patch("pwd.getpwuid", return_value=fake):
                    self.assert_no_exec(self.refresh(bash))

    def test_failed_exec_is_harmless(self):
        self.require_login_shell()
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)

        def failing(file, args):
            raise OSError("exec failed")
        self.mod._execvp = failing
        self.assertFalse(self.refresh(bash))

    def test_hook_file_follows_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            self.assertEqual(self.mod._shell_hook_file(),
                             str(self.home / ".local/state/personal-dotfiles/shell-hook"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            self.assertEqual(self.mod._shell_hook_file(), str(self.hook))

    # -- main(): only update and repair, only after success -----------------

    def write_generation(self, value):
        path = self.state / "personal-dotfiles" / "generation"
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value + "\n")

    def run_main(self, parent, command, ret, generation="next"):
        """main() with a fake command that sets the generation file to
        ``generation`` ("next": a new value; None: no file)."""
        def fake(argv=(), **kwargs):
            """fake command"""
            if generation == "next":
                self.counter = getattr(self, "counter", 0) + 1
                self.write_generation(f"gen-{self.counter}")
            else:
                self.write_generation(generation)
            return ret
        fake.__name__ = command
        pgrp = os.getpgrp()
        with mock.patch.object(self.mod, command, fake), \
                mock.patch.object(sys, "argv", ["dotfiles", command]), \
                mock.patch("os.getppid", return_value=parent.pid), \
                mock.patch("os.isatty", return_value=True), \
                mock.patch("os.ttyname", return_value=parent.tty), \
                mock.patch("os.tcgetpgrp", return_value=pgrp):
            try:
                self.mod.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def test_main_execs_only_after_a_successful_update_or_repair(self):
        self.require_login_shell()
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        for command in ("update", "repair"):
            with self.subTest(f"{command} succeeded"):
                self.execs.clear()
                self.assertEqual(self.run_main(bash, command, 0), 0)
                self.assertEqual(len(self.execs), 1)
            for rc in (1, 3, 5):
                with self.subTest(f"{command} failed with {rc}"):
                    self.execs.clear()
                    self.assertEqual(self.run_main(bash, command, rc), rc)
                    self.assertEqual(self.execs, [])
        with self.subTest("other commands"):
            self.execs.clear()
            self.assertEqual(self.run_main(bash, "status", 0), 0)
            self.assertEqual(self.execs, [])
        with self.subTest("hooked parent"):
            self.execs.clear()
            hooked = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH,
                                 hold=self.hook)
            self.assertEqual(self.run_main(hooked, "repair", 0), 0)
            self.assertEqual(self.execs, [])

    def test_main_execs_only_when_the_generation_changed(self):
        self.require_login_shell()
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        for command in ("update", "repair"):
            with self.subTest(f"{command}: unchanged"):
                self.write_generation("same")
                self.execs.clear()
                self.assertEqual(self.run_main(bash, command, 0, generation="same"), 0)
                self.assertEqual(self.execs, [])
            with self.subTest(f"{command}: the first generation"):
                self.write_generation(None)
                self.execs.clear()
                self.assertEqual(self.run_main(bash, command, 0, generation="first"), 0)
                self.assertEqual(len(self.execs), 1)
            with self.subTest(f"{command}: none recorded after the run"):
                self.write_generation("before")
                self.execs.clear()
                self.assertEqual(self.run_main(bash, command, 0, generation=None), 0)
                self.assertEqual(self.execs, [])

    def test_read_generation(self):
        path = self.state / "personal-dotfiles" / "generation"
        self.assertIsNone(self.mod._read_generation())
        path.write_text("abc\nrest\n")
        self.assertEqual(self.mod._read_generation(), "abc")
        path.write_text("")
        self.assertIsNone(self.mod._read_generation())
        path.unlink()
        os.mkfifo(path)  # never blocks
        self.assertIsNone(self.mod._read_generation())
        path.unlink()
        (self.root / "elsewhere").write_text("planted\n")
        path.symlink_to(self.root / "elsewhere")
        self.assertIsNone(self.mod._read_generation())


class RefreshTerminalEndToEndTests(DotfilesCmdCase):
    """'dotfiles repair' typed into a real interactive shell on a pty: the
    passwd login shell takes over that terminal, without any message, only
    for an unhooked, interactive, foreground parent."""

    def setUp(self):
        super().setUp()
        shell = pwd.getpwuid(os.getuid()).pw_shell
        if os.path.basename(shell) != "zsh" or not os.access(shell, os.X_OK):
            self.skipTest(f"needs zsh as the passwd login shell, not {shell!r}")
        zdot = self.root / "zdot"
        zdot.mkdir()
        self.zdot = zdot
        (zdot / ".zshenv").write_text("unsetopt GLOBAL_RCS\n")
        (zdot / ".zshrc").write_text(
            'print -r -- "NEW-""LOGIN-SHELL login=${options[login]} tty=$TTY"\n'
            "PS1='@@Z@@ '\n")
        self.state = self.root / "state"
        self.hook = self.state / "personal-dotfiles" / "shell-hook"
        self.hook.parent.mkdir(parents=True, mode=0o700)
        self.hook.write_text("")
        self.env.update({"ZDOTDIR": str(zdot), "TERM": "dumb", "PS1": "@@P@@ ",
                         "XDG_STATE_HOME": str(self.state),
                         "FAKE_GENERATION": "new-generation"})
        self.env.pop("NO_COLOR", None)

    def start(self, argv=("bash", "--norc", "--noprofile", "-i"), exe=BASH, hold=None,
              ready=rb"@@P@@ "):
        parent = FakeParent(list(argv), self.env, exe=exe, hold=hold)
        self.addCleanup(parent.close)
        if ready:
            parent.read_until(ready)
        return parent

    def repair_line(self, suffix="", **extra):
        prefix = "".join(f"{k}={v} " for k, v in extra.items())
        return (f"{prefix}{sys.executable} -B {DOTFILES} repair{suffix}; "
                "echo \"BACK-IN-\"\"PARENT rc=$?\"\n")

    def test_unhooked_parent_ends_in_the_login_shell(self):
        bash = self.start()
        bash.send(self.repair_line())
        out = bash.read_until(rb"@@Z@@ |BACK-IN-PARENT")
        self.assertIn("NEW-LOGIN-SHELL login=on", out)
        self.assertIn(f"tty={bash.tty}", out)
        self.assertNotIn("BACK-IN-PARENT", out)
        self.assertNotIn("exec zsh", out)
        self.assertNotRegex(out, r"(?i)old (setup|shell)|predates|restart|new terminal")
        self.assertEqual([c["argv"] for c in self.calls()], [["repair"]])
        bash.send("exit\n")
        self.assertIn("rc=0", bash.read_until(rb"rc=\d+"))

    def test_unchanged_generation_stays_in_the_parent(self):
        gen = self.state / "personal-dotfiles" / "generation"
        gen.write_text("new-generation\n")  # what the repair records again
        bash = self.start()
        bash.send(self.repair_line())
        out = bash.read_until(rb"@@Z@@ |BACK-IN-PARENT rc=\d+")
        self.assertIn("BACK-IN-PARENT rc=0", out)
        self.assertNotIn("NEW-LOGIN-SHELL", out)

    def test_failed_repair_stays_in_the_parent(self):
        bash = self.start()
        bash.send(self.repair_line(FAKE_INSTALL_RC=4))
        out = bash.read_until(rb"@@Z@@ |BACK-IN-PARENT rc=\d+")
        self.assertIn("BACK-IN-PARENT rc=4", out)
        self.assertNotIn("NEW-LOGIN-SHELL", out)

    def test_hooked_parent_stays(self):
        bash = self.start(hold=self.hook)
        bash.send(self.repair_line())
        out = bash.read_until(rb"@@Z@@ |BACK-IN-PARENT rc=\d+")
        self.assertIn("BACK-IN-PARENT rc=0", out)
        self.assertNotIn("NEW-LOGIN-SHELL", out)

    def test_real_hooked_zsh_parent_stays(self):
        hooked = self.root / "zdot-hooked"
        hooked.mkdir()
        (hooked / ".zshenv").write_text("unsetopt GLOBAL_RCS\n")
        (hooked / ".zshrc").write_text(
            f"source {REPO_ROOT / 'zsh/zsh.d/dotfiles-reload.zsh'}\nPS1='@@H@@ '\n")
        self.env["ZDOTDIR"] = str(hooked)
        zsh = self.start(argv=["-zsh"], exe=ZSH, ready=rb"@@H@@ ")
        self.env["ZDOTDIR"] = str(self.zdot)
        zsh.send(f"ZDOTDIR={self.zdot} " + self.repair_line())
        out = zsh.read_until(rb"@@Z@@ |BACK-IN-PARENT rc=\d+")
        self.assertIn("BACK-IN-PARENT rc=0", out)
        self.assertNotIn("NEW-LOGIN-SHELL", out)

    def test_background_job_stays(self):
        bash = self.start()
        bash.send(f"{sys.executable} -B {DOTFILES} repair & wait $!; "
                  "echo \"BACK-IN-\"\"PARENT rc=$?\"\n")
        out = bash.read_until(rb"@@Z@@ |BACK-IN-PARENT rc=\d+")
        self.assertIn("BACK-IN-PARENT rc=0", out)
        self.assertNotIn("NEW-LOGIN-SHELL", out)

    def test_login_style_one_shot_command_runs_to_its_end(self):
        """'su - -c CMD' / 'sudo -i CMD' run '-bash -c CMD': the rest of CMD
        must still run, no login shell is started in its middle."""
        line = (f"{sys.executable} -B {DOTFILES} repair; "
                "echo \"REST-OF-\"\"COMMAND-RAN rc=$?\"")
        for exe, name in ((BASH, "-bash"), (ZSH, "-zsh")):
            with self.subTest(name):
                parent = self.start(argv=[name, "-c", line], exe=exe, ready=None)
                out = parent.read_until(rb"@@Z@@ |REST-OF-COMMAND-RAN rc=\d+")
                self.assertIn("REST-OF-COMMAND-RAN rc=0", out)
                self.assertNotIn("NEW-LOGIN-SHELL", out)


if __name__ == "__main__":
    unittest.main()
