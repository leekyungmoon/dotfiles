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

macOS has no /proc: there bin/dotfiles (and installer/phases.py) read the
parent through installer/darwin_proc.py, libproc and sysctl, and so does
this test's own bookkeeping. On Linux the same refresh tests also run with
bin/dotfiles on that path, over LinuxLibSystem from test_darwin_proc.py,
which answers libproc's calls from /proc in macOS's layouts and conventions
(SimulatedMacosRefreshTerminalTests), and against scripted backends
(DarwinProbeTests). bin/dotfiles also runs install.py under a python 3.10
or newer, re-executing itself when its own is older (PythonVersionTests).
On macOS, 'dotfiles install' leaves out, with what to run instead, a tool
that tools.json does not install there (PlatformCommandTests).
"""

from __future__ import annotations

import ast
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import darwin_proc  # noqa: E402

DARWIN = sys.platform == "darwin"
LINUX_PROC = sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")
# LinuxLibSystem and use_library: libproc's calls answered from Linux's
# /proc, so bin/dotfiles' macOS code runs on Linux with the real darwin_proc.
_HELPERS = importlib.util.spec_from_file_location(
    "darwin_proc_test_helpers", Path(__file__).with_name("test_darwin_proc.py"))
DARWIN_PROC_TESTS = importlib.util.module_from_spec(_HELPERS)
_HELPERS.loader.exec_module(DARWIN_PROC_TESTS)

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
        for word in ("update", "github", "install", "status", "restore", "repair",
                     "services"):
            self.assertIn(word, out)
        self.assertIn("@leekyungmoon's", out)
        self.assertNotIn("wookayin", out)

    def test_help_texts_fit_linux_and_macos(self):
        out = self.dotfiles_cmd("--help").stdout.decode()
        self.assertIn("Available on Linux and macOS.", out)
        self.assertIn("(Linux only; install.py gui-apply)", out)
        self.assertNotIn("only in Linux", out)
        for command in ("update", "repair"):
            with self.subTest(command):
                text = self.dotfiles_cmd(command, "--help").stdout.decode()
                self.assertIn("Skip system packages (apt or Homebrew) and pinned tools",
                              " ".join(text.split()))
                self.assertNotIn("Skip apt packages", text)
        text = " ".join(self.dotfiles_cmd("services", "--help").stdout.decode().split())
        self.assertIn("status (the default), enable or disable, then tmux and/or "
                      "autosave (default: both)", text)

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

    def test_services_passes_through_with_its_exit_status(self):
        """'dotfiles services ...' is 'install.py services ...' (it checks the
        words), from anywhere, with install.py's exit status."""
        self.assertEqual(self.dotfiles_cmd("services").returncode, 0)
        self.assertEqual(self.dotfiles_cmd("services", "disable", "tmux").returncode, 0)
        self.assertEqual(self.dotfiles_cmd("services", "enable", "tmux",
                                           "autosave").returncode, 0)
        failed = self.dotfiles_cmd("services", "status", extra_env={"FAKE_INSTALL_RC": "1"})
        self.assertEqual(failed.returncode, 1)
        bad = self.dotfiles_cmd("services", "frobnicate", extra_env={"FAKE_INSTALL_RC": "2"})
        self.assertEqual(bad.returncode, 2)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], [
            ["services"], ["services", "disable", "tmux"],
            ["services", "enable", "tmux", "autosave"], ["services", "status"],
            ["services", "frobnicate"]])
        self.assertTrue(all(c["no_bytecode"] for c in calls))
        self.assertTrue(all(Path(c["cwd"]) == self.dotfiles for c in calls))
        # An option it does not take: refused here, install.py never runs.
        completed = self.dotfiles_cmd("services", "--json")
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"Unknown arguments: --json", completed.stdout)
        self.assertEqual(len(self.calls()), len(calls))

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


# What this test reads about a process itself (not the code under test):
# /proc on Linux, as it always did; installer/darwin_proc.py on macOS.

def process_argv0(pid):
    """argv[0] of pid, or None (not readable)."""
    if DARWIN:
        argv = darwin_proc.argv(pid)
        return argv[0] if argv else None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().split(b"\0")[0].decode(errors="surrogateescape")
    except OSError:
        return None


def process_tty(pid):
    """The resolved path of pid's stdin."""
    if DARWIN:
        paths = {entry.fd: entry.path for entry in darwin_proc.open_vnodes(pid) or ()}
        return os.path.realpath(paths[0])
    return os.path.realpath(os.readlink(f"/proc/{pid}/fd/0"))


def process_comm(pid):
    if DARWIN:
        info = darwin_proc.bsd_info(pid)
        return info.comm if info is not None else None
    with open(f"/proc/{pid}/comm") as handle:
        return handle.read().strip()


class CountingLibSystem(DARWIN_PROC_TESTS.LinuxLibSystem):
    """LinuxLibSystem counting the libproc and sysctl calls made through it:
    proof that an answer came from there and not from /proc."""

    calls = 0

    def proc_listpids(self, *args):
        self.calls += 1
        return super().proc_listpids(*args)

    def proc_pidinfo(self, *args):
        self.calls += 1
        return super().proc_pidinfo(*args)

    def proc_pidfdinfo(self, *args):
        self.calls += 1
        return super().proc_pidfdinfo(*args)

    def proc_pidpath(self, *args):
        self.calls += 1
        return super().proc_pidpath(*args)

    def sysctl(self, *args):
        self.calls += 1
        return super().sysctl(*args)


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
            if process_argv0(self.pid) == argv[0]:
                break
            time.sleep(0.02)
        self.tty = process_tty(self.pid)
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
        if DARWIN:
            # Decided once per process, by a self-check that compares
            # os.getppid(): before refresh() patches it.
            self.assertTrue(darwin_proc.available(), darwin_proc.unavailable_reason())

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
            if DARWIN:  # no " (deleted)" mark there; perhaps no name at all
                entries = darwin_proc.open_vnodes(zsh.pid)
                self.assertTrue(any(entry.nlink == 0 for entry in entries), entries)
            else:
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
            if process_comm(zsh.pid) == "bash":
                break
            time.sleep(0.05)
        self.assertFalse(self.mod._is_hooked(zsh.pid))
        self.assert_exec(self.refresh(zsh))

    def test_unreadable_fd_table_counts_as_hooked(self):
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        if DARWIN:
            real_open_vnodes = darwin_proc.open_vnodes

            def open_vnodes(pid):
                return None if pid == bash.pid else real_open_vnodes(pid)
            with mock.patch.object(darwin_proc, "open_vnodes", side_effect=open_vnodes):
                self.assert_no_exec(self.refresh(bash))
            return
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
        if DARWIN:
            # macOS's /bin/sh runs bash itself (as bash, by name and
            # executable): another shell stands in.
            parents["argv[0] bash, but sh"] = (["bash", "-i"], "/bin/ksh")
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


# --- macOS -----------------------------------------------------------------------

@unittest.skipUnless(LINUX_PROC, "LinuxLibSystem answers from Linux's /proc")
class SimulatedMacosRefreshTerminalTests(RefreshTerminalTests):
    """Every refresh test above with bin/dotfiles (and installer/phases.py,
    for the parity tests) on its macOS path: the real
    installer/darwin_proc.py over LinuxLibSystem instead of /proc. The same
    parents must get the same answer; a mark removed under a shell has no
    name there (as macOS may give it none), which counts as hooked too."""

    def setUp(self):
        super().setUp()
        self.library = CountingLibSystem()
        DARWIN_PROC_TESTS.use_library(self, self.library)
        self.mod._darwin_host = lambda: True
        from installer import phases
        patcher = mock.patch.object(phases, "_darwin_host", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_libproc_is_what_is_asked(self):
        self.require_login_shell()
        bash = self.parent(["bash", "--norc", "--noprofile", "-i"], exe=BASH)
        self.assert_exec(self.refresh(bash))
        self.assertGreater(self.library.calls, 0)
        self.assertIs(self.mod._darwin_proc(), darwin_proc)


class FakeLibproc:
    """A scripted installer/darwin_proc.py: each table answers by pid; as
    for the real module, a pid it does not describe gets None."""

    def __init__(self):
        self.infos, self.argvs, self.files, self.exes = {}, {}, {}, {}
        self.raising = set()

    def _answer(self, name, table, pid):
        if name in self.raising:
            raise RuntimeError(f"{name} failed")
        value = table.get(pid)
        return list(value) if isinstance(value, list) else value

    def bsd_info(self, pid):
        return self._answer("bsd_info", self.infos, pid)

    def argv(self, pid):
        return self._answer("argv", self.argvs, pid)

    def open_vnodes(self, pid):
        return self._answer("open_vnodes", self.files, pid)

    def exe(self, pid):
        return self._answer("exe", self.exes, pid)


def vnode(fd, path="", *, dev=0x7FFFFFF0, ino=0, nlink=1):
    return darwin_proc.VnodeFd(fd=fd, path=path, dev=dev, ino=ino or 1000 + fd,
                               nlink=nlink, mode=0o100600, size=0, cloexec=False,
                               uid=os.getuid(), openflags=darwin_proc.FREAD)


class DarwinProbeTests(unittest.TestCase):
    """bin/dotfiles' checks of its parent on macOS, against a scripted
    darwin_proc: whatever it cannot tell counts against the exec."""

    PID = 2 ** 22 + 77  # above Linux's pid_max: /proc never answers for it
    TTY = "/dev/ttys004"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-darwin-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.hook = self.root / "state" / "personal-dotfiles" / "shell-hook"
        self.hook.parent.mkdir(parents=True)
        self.hook.touch(mode=0o600)
        self.mod = load_dotfiles_module()
        self.fake = FakeLibproc()
        tty = self.TTY
        self.fake.infos[self.PID] = darwin_proc.BsdInfo(
            pid=self.PID, ppid=1, uid=os.getuid(), pgid=self.PID, tdev=4, tpgid=99,
            comm="bash", name="bash", status=darwin_proc.SSLEEP, start=1.0)
        self.fake.argvs[self.PID] = ["-bash"]
        self.fake.files[self.PID] = [vnode(0, tty), vnode(1, tty), vnode(2, tty)]
        self.fake.exes[self.PID] = "/bin/bash"
        self.backend = self.fake
        self.mod._darwin_host = lambda: True
        self.mod._darwin_proc = lambda: self.backend
        self.execs = []
        self.mod._execvp = lambda file, args: self.execs.append((file, list(args)))

    def hooked(self):
        return self.mod._is_hooked(self.PID, str(self.hook))

    def test_argv(self):
        self.fake.argvs[self.PID] = ["-zsh", "", "-l", "", ""]
        # As read from /proc: trailing empty arguments dropped, others kept.
        self.assertEqual(self.mod._cmdline(self.PID), ["-zsh", "", "-l"])
        self.assertFalse(self.mod.interactive_invocation(self.mod._cmdline(self.PID)))
        self.fake.argvs[self.PID] = None
        with self.assertRaises(OSError):
            self.mod._cmdline(self.PID)

    def test_shell_process(self):
        info = self.fake.infos[self.PID]
        for comm, exe, expected in (("bash", "/bin/bash", True),
                                    ("zsh", "/opt/homebrew/Cellar/zsh/5.9/bin/zsh", True),
                                    ("zsh", "/opt/homebrew/bin/python3", False),
                                    ("Python", "/bin/zsh", False)):
            with self.subTest(comm=comm, exe=exe):
                self.fake.infos[self.PID] = info._replace(comm=comm)
                self.fake.exes[self.PID] = exe
                self.assertIs(self.mod._shell_process(self.PID), expected)
        for table in ("infos", "exes"):
            with self.subTest(missing=table):
                getattr(self.fake, table)[self.PID] = None
                with self.assertRaises(OSError):
                    self.mod._shell_process(self.PID)

    def test_same_terminal(self):
        tty = self.TTY
        self.assertTrue(self.mod._same_terminal(self.PID, tty))
        for files in ([vnode(0, tty), vnode(1, "/tmp/out")], [vnode(1, tty)],
                      [vnode(0, tty), vnode(1, "")], None):
            with self.subTest(files=files):
                self.fake.files[self.PID] = files
                self.assertFalse(self.mod._same_terminal(self.PID, tty))

    def test_hooked(self):
        st = os.stat(self.hook)
        tty = self.TTY
        std = [vnode(0, tty), vnode(1, tty), vnode(2, tty)]
        cases = {
            "the hook file": (vnode(9, str(self.hook)), True),
            "another state directory's": (
                vnode(9, "/Volumes/other/state/personal-dotfiles/shell-hook"), True),
            "the same file under another name": (
                vnode(9, "/Volumes/x/mark", dev=st.st_dev & 0xFFFFFFFF, ino=st.st_ino), True),
            "a removed file without a name": (vnode(9, "", nlink=0), True),
            "a removed file with a name": (vnode(9, "/tmp/bash-heredoc", nlink=0), False),
            "a file merely named shell-hook": (vnode(9, str(self.root / "shell-hook")), False),
        }
        self.fake.files[self.PID] = list(std)
        self.assertFalse(self.hooked())
        for name, (entry, expected) in cases.items():
            with self.subTest(name):
                self.fake.files[self.PID] = std + [entry]
                self.assertIs(self.hooked(), expected)
        self.fake.files[self.PID] = None  # cannot be read: no exec
        self.assertTrue(self.hooked())
        # libproc gives the unsigned 32-bit device number.
        wide = os.stat_result((st.st_mode, st.st_ino, (1 << 32) | 77, 1, 0, 0, 0, 0, 0, 0))
        self.fake.files[self.PID] = std + [vnode(9, "/Volumes/x/mark", dev=77, ino=st.st_ino)]
        with mock.patch.object(self.mod.os, "stat", return_value=wide):
            self.assertTrue(self.hooked())

    def test_backend_trouble_never_execs(self):
        self.backend = None
        self.assertTrue(self.hooked())
        self.assertFalse(self.mod._same_terminal(self.PID, self.TTY))
        for probe in (self.mod._cmdline, self.mod._shell_process):
            with self.assertRaises(OSError):
                probe(self.PID)
        self.backend = self.fake
        for name in ("bsd_info", "argv", "open_vnodes", "exe"):
            with self.subTest(raising=name):
                self.fake.raising = {name}
                self.assertFalse(self.refresh())
        self.assertEqual(self.execs, [])

    def refresh(self):
        pgrp = os.getpgrp()
        with mock.patch("os.getppid", return_value=self.PID), \
                mock.patch("os.isatty", return_value=True), \
                mock.patch("os.ttyname", return_value=self.TTY), \
                mock.patch("os.tcgetpgrp", return_value=pgrp), \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            return self.mod._refresh_terminal()

    def test_refresh_terminal(self):
        shell = pwd.getpwuid(os.getuid()).pw_shell
        if not (os.path.isabs(shell) and os.access(shell, os.X_OK)):
            self.skipTest(f"passwd login shell {shell!r} is not executable")
        self.assertTrue(self.refresh())
        self.assertEqual(self.execs, [(shell, [os.path.basename(shell), "-l"])])
        self.execs.clear()
        self.fake.files[self.PID].append(vnode(9, str(self.hook)))
        self.assertFalse(self.refresh())  # hooked: it reloads itself
        self.assertEqual(self.execs, [])

    def test_backend_is_imported_once_and_checked(self):
        mod = load_dotfiles_module()
        loads = []

        class Module:
            def __init__(self, works):
                self.works = works

            def available(self):
                return self.works

        def importer(works):
            def load():
                loads.append(works)
                return Module(works)
            return load
        mod._import_darwin_proc = importer(True)
        backend = mod._darwin_proc()
        self.assertTrue(backend.works)
        self.assertIs(mod._darwin_proc(), backend)
        self.assertEqual(loads, [True])
        mod = load_dotfiles_module()
        mod._import_darwin_proc = importer(False)
        self.assertIsNone(mod._darwin_proc())  # its self-check failed
        mod = load_dotfiles_module()

        def broken():
            raise ImportError("no installer/darwin_proc.py in this checkout")
        mod._import_darwin_proc = broken
        self.assertIsNone(mod._darwin_proc())

    def test_linux_never_asks_libproc(self):
        if not LINUX_PROC:
            self.skipTest("needs Linux's /proc")

        def refuse():
            raise AssertionError("libproc asked on Linux")
        self.mod._darwin_host = lambda: False
        self.mod._darwin_proc = refuse
        pid = os.getpid()
        self.assertEqual(self.mod._cmdline(pid)[0], process_argv0(pid))
        self.mod._shell_process(pid)
        self.mod._same_terminal(pid, "/dev/null")
        self.assertFalse(self.mod._is_hooked(pid, str(self.hook)))


class DarwinImportTests(unittest.TestCase):
    """On macOS bin/dotfiles imports installer/darwin_proc.py from its own
    checkout, as tmux/resurrect-ai-session.py does: through any link to
    bin/dotfiles, ahead of any other package named installer, without
    writing bytecode into the checkout, and leaving sys.path as it was."""

    def test_imports_its_own_checkouts_module(self):
        with tempfile.TemporaryDirectory(prefix="pdf-import-") as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            (checkout / "bin").mkdir(parents=True)
            (checkout / "installer").mkdir()
            shutil.copy(DOTFILES, checkout / "bin" / "dotfiles")
            for name in ("__init__.py", "darwin_proc.py"):
                shutil.copy(REPO_ROOT / "installer" / name, checkout / "installer" / name)
            shadow = root / "shadow" / "installer"
            shadow.mkdir(parents=True)
            (shadow / "__init__.py").write_text("")
            (shadow / "darwin_proc.py").write_text("SHADOW = True\n")
            link = root / "local-bin" / "dotfiles"
            link.parent.mkdir()
            link.symlink_to(checkout / "bin" / "dotfiles")
            code = (
                "import importlib.util, json, sys\n"
                "from importlib.machinery import SourceFileLoader\n"
                "loader = SourceFileLoader('dotfiles_cmd', sys.argv[1])\n"
                "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "loader.exec_module(mod)\n"
                "sys.dont_write_bytecode = False\n"
                "before = list(sys.path)\n"
                "module = mod._import_darwin_proc()\n"
                "print(json.dumps({'file': module.__file__,\n"
                "                  'shadow': getattr(module, 'SHADOW', False),\n"
                "                  'path_kept': sys.path == before,\n"
                "                  'no_bytecode': sys.dont_write_bytecode}))\n")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "PYTHONPATH": str(root / "shadow"), "HOME": tmp, "LANG": "C"}
            done = subprocess.run([sys.executable, "-c", code, str(link)], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertEqual(result, {
                "file": str(checkout / "installer" / "darwin_proc.py"), "shadow": False,
                "path_kept": True, "no_bytecode": True})
            self.assertEqual(list(checkout.rglob("__pycache__")), [])


class PythonVersionTests(unittest.TestCase):
    """Under a python older than 3.10 (on macOS the Command Line Tools'
    3.9), every command but github re-executes itself, before any git
    action, under Homebrew's python3 or the newest python3.1x on PATH, or
    stops with exit status 2 and nothing changed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-python-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.mod = load_dotfiles_module()
        self.execs = []

        def execv(path, argv):
            self.execs.append((path, list(argv)))
            raise SystemExit("re-executed")
        self.mod._execv = execv
        self.mod._python_version = lambda: (3, 9)
        self.brew = self.root / "brew"
        (self.brew / "bin").mkdir(parents=True)
        self.path_dir = self.root / "path"
        self.path_dir.mkdir()
        self.log = self.root / "python.log"
        patcher = mock.patch.dict(os.environ, {"HOMEBREW_PREFIX": str(self.brew),
                                               "PATH": str(self.path_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def python(self, path, *, ok=True):
        """A python3 stand-in: the real one (logged), or one too old."""
        if ok:
            path.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{self.log}'\n"
                            f"exec '{sys.executable}' \"$@\"\n")
        else:
            path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
        return str(path)

    def test_new_enough_python_runs_on(self):
        self.mod._python_version = lambda: (3, 10)
        self.mod._require_python()
        self.assertEqual(self.execs, [])

    def test_homebrew_python_first(self):
        brew = self.python(self.brew / "bin" / "python3")
        self.python(self.path_dir / "python3.13")
        with mock.patch.object(sys, "argv", ["dotfiles", "update", "--fast"]):
            with self.assertRaises(SystemExit):
                self.mod._require_python()
        self.assertEqual(self.execs, [(brew, [brew, "-B", str(DOTFILES), "update",
                                              "--fast"])])
        # Probed as etc/install probes it.
        self.assertIn(f"-c {self.mod.PYTHON_CHECK}", self.log.read_text())

    def test_newest_python3_1x_on_path_next(self):
        self.python(self.brew / "bin" / "python3", ok=False)  # too old
        self.python(self.path_dir / "python3.11")
        newest = self.python(self.path_dir / "python3.13")
        (self.path_dir / "python3.14").write_text("#!/bin/sh\n")  # not executable
        same = self.path_dir / "python3.15"  # this very python: never re-executed
        same.symlink_to(os.path.realpath(sys.executable))
        with mock.patch.object(sys, "argv", ["dotfiles", "status"]):
            with self.assertRaises(SystemExit):
                self.mod._require_python()
        self.assertEqual([path for path, _ in self.execs], [newest])

    def test_none_found_stops_before_any_change(self):
        for darwin, remedy in ((True, "run 'brew install python3'"),
                               (False, "install python3 3.10 or newer")):
            with self.subTest(darwin=darwin):
                self.mod._darwin_host = lambda: darwin
                out = self.run_require()
                self.assertEqual(out[0], 2)
                text = " ".join(out[1].split())
                self.assertIn("dotfiles needs python 3.10 or newer", text)
                self.assertIn("this is python 3.9", text)
                self.assertIn("no newer one was found", text)
                self.assertIn(remedy, text)
                self.assertIn("Nothing was changed.", text)
        self.assertEqual(self.execs, [])

    def test_failed_exec_stops_too(self):
        self.python(self.path_dir / "python3.12")

        def failing(path, argv):
            raise OSError("exec format error")
        self.mod._execv = failing
        code, out = self.run_require()
        self.assertEqual(code, 2)
        self.assertIn("python3.12 failed (exec format error)", out)
        self.assertIn("Nothing was changed.", out)

    def run_require(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", ["dotfiles"]):
            try:
                self.mod._require_python()
            except SystemExit as exc:
                return exc.code, out.getvalue()
        return None, out.getvalue()

    def test_homebrew_prefix_rule_of_etc_install(self):
        cases = ((({"HOMEBREW_PREFIX": "/custom/brew//"}, "arm64"), "/custom/brew"),
                 (({"HOMEBREW_PREFIX": "/"}, "arm64"), "/"),
                 (({"HOMEBREW_PREFIX": "relative"}, "arm64"), "/opt/homebrew"),
                 (({}, "arm64"), "/opt/homebrew"),
                 (({}, "x86_64"), "/usr/local"),
                 (({}, "aarch64"), None))
        for (env, machine), expected in cases:
            with self.subTest(env=env, machine=machine):
                uname = os.uname_result(("Darwin", "mac", "25.0", "v", machine))
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(self.mod.os, "uname", return_value=uname):
                    self.assertEqual(self.mod._homebrew_prefix(), expected)

    def test_every_command_but_github_requires_it(self):
        mod = load_dotfiles_module()
        required = []
        mod._require_python = lambda: required.append(True)
        for command in ("update", "install", "status", "restore", "repair", "services",
                        "gui-apply", "github"):
            with self.subTest(command):
                required.clear()

                def fake(argv=(), **kwargs):
                    """fake command"""
                    self.assertTrue(required or command == "github")
                    return 0
                name = {"install": "install_local", "github": "open_github",
                        "gui-apply": "gui_apply"}.get(command, command)
                args = {"restore": ["--baseline"]}.get(command, [])
                with mock.patch.object(mod, name, fake), \
                        mock.patch.object(sys, "argv", ["dotfiles", command, *args]):
                    mod.main()
                self.assertEqual(bool(required), command != "github")


class ReexecEndToEndTests(DotfilesCmdCase):
    """The re-executed command line runs the same command under the newer
    python: install.py is called as it would have been."""

    def test_reexecuted_command_runs_install_py(self):
        wrapper_log = self.root / "wrapper.log"
        bindir = self.root / "newer"
        bindir.mkdir()
        wrapper = bindir / "python3.12"
        wrapper.write_text(f"#!/bin/sh\necho \"$*\" >> '{wrapper_log}'\n"
                           f"exec '{sys.executable}' \"$@\"\n")
        wrapper.chmod(0o755)
        mod = load_dotfiles_module()
        recorded = []

        def execv(path, argv):
            recorded.append(list(argv))
            raise SystemExit(0)
        mod._execv = execv
        mod._python_version = lambda: (3, 9)
        env = {"HOMEBREW_PREFIX": str(self.root / "no-brew"), "PATH": str(bindir)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "argv", ["dotfiles", "status", "--json"]):
            with self.assertRaises(SystemExit):
                mod.main()
        self.assertEqual(self.calls(), [])  # nothing ran under the old python
        (argv,) = recorded
        self.assertEqual(argv, [str(wrapper), "-B", str(DOTFILES), "status", "--json"])
        done = subprocess.run(argv, env=dict(self.env, PATH=f"{bindir}:{self.env['PATH']}"),
                              cwd=str(self.root), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], [["status", "--json"]])
        self.assertTrue(calls[0]["no_bytecode"])
        self.assertIn(f"-B {DOTFILES} status --json", wrapper_log.read_text())

    def test_no_newer_python_changes_nothing(self):
        before = self.head()
        self.push_upstream()
        mod = load_dotfiles_module()
        mod._python_version = lambda: (3, 9)
        mod._execv = lambda path, argv: self.fail("re-executed")
        env = {"HOMEBREW_PREFIX": str(self.root / "no-brew"),
               "PATH": str(self.root / "empty"), "HOME": str(self.home)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "argv", ["dotfiles", "update"]):
            with self.assertRaises(SystemExit) as raised:
                mod.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.head(), before)  # not even fetched and merged
        self.assertEqual(self.calls(), [])


class PlatformCommandTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_dotfiles_module()
        self.delegated = []
        self.mod._installer = lambda *args: self.delegated.append(list(args)) or 0
        # ~/.dotfiles is a scratch checkout whose tools.json has the real
        # nerd-font entry (skipped on macOS) and a plain fzf.
        tmp = tempfile.TemporaryDirectory(prefix="pdf-platform-")
        self.addCleanup(tmp.cleanup)
        manifests = Path(tmp.name) / "manifests"
        manifests.mkdir()
        real = json.loads((REPO_ROOT / "manifests" / "tools.json").read_text(encoding="utf-8"))
        self.font = real["tools"]["nerd-font"]
        (manifests / "tools.json").write_text(json.dumps(
            {"schema": 1, "tools": {"fzf": {}, "nerd-font": self.font}}))
        self.mod._dotfiles_dir = lambda: tmp.name

    def install(self, *targets, darwin):
        import contextlib
        import io
        self.mod._darwin_host = lambda: darwin
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(sys, "platform", "darwin" if darwin else "linux"):
            ret = self.mod.install_local(list(targets))
        return ret, " ".join(out.getvalue().split())

    def test_gui_apply_is_not_applicable_on_macos(self):
        self.mod._darwin_host = lambda: True
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.mod.gui_apply(), 2)
        self.assertIn("gui-apply is not applicable on macOS", out.getvalue())
        self.assertIn("Nothing was changed.", out.getvalue())
        self.assertEqual(self.delegated, [])
        self.mod._darwin_host = lambda: False
        self.assertEqual(self.mod.gui_apply(autostart=True), 0)
        self.assertEqual(self.delegated, [["gui-apply", "--autostart"]])

    def test_install_runs_on_linux_and_macos_only(self):
        self.mod._available_packages = lambda: ["fzf"]
        for platform, darwin, allowed in (("linux", False, True), ("darwin", True, True),
                                          ("freebsd13", False, False)):
            with self.subTest(platform):
                self.delegated.clear()
                self.mod._darwin_host = lambda: darwin
                with mock.patch.object(sys, "platform", platform):
                    if allowed:
                        self.mod.install_local(["fzf"])
                        self.assertEqual(self.delegated, [["packages", "--only", "fzf"]])
                    else:
                        with self.assertRaises(SystemExit):
                            self.mod.install_local(["fzf"])
                        self.assertEqual(self.delegated, [])

    def test_install_of_a_tool_macos_skips_changes_nothing(self):
        """install.py would report it SKIPPED and exit 0: no 'Installation
        successful' for a font that was not installed."""
        ret, out = self.install("nerd-font", darwin=True)
        self.assertEqual(ret, 2)
        self.assertEqual(self.delegated, [])
        self.assertIn("nerd-font is not installed on macOS: Core Text (Terminal, iTerm2, "
                      "Ghostty) does not read fontconfig directories", out)
        self.assertNotIn("not installed on macOS: Not installed on macOS", out)
        self.assertIn("Install it with 'brew install --cask font-jetbrains-mono-nerd-font'.", out)
        self.assertLess(out.index("Install it with"), out.index("Nothing was changed."))
        self.assertNotIn("successful", out)

    def test_dotfiles_install_nerd_font_exits_2_on_macos(self):
        import contextlib
        import io
        self.mod._darwin_host = lambda: True
        with mock.patch.object(sys, "argv", ["dotfiles", "install", "nerd-font"]), \
                mock.patch.object(sys, "platform", "darwin"), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.mod.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.delegated, [])

    def test_the_other_targets_are_still_installed_on_macos(self):
        ret, out = self.install("nerd-font", "fzf", darwin=True)
        self.assertIsNone(ret)
        self.assertEqual(self.delegated, [["packages", "--only", "fzf"]])
        self.assertIn("nerd-font is not installed on macOS", out)
        self.assertIn("[*] Installation successful: fzf", out)
        self.assertNotIn("Installation successful: nerd-font", out)
        self.assertNotIn("Nothing was changed.", out)
        self.assertLess(out.index("nerd-font is not installed"), out.index("successful: fzf"))

    def test_a_tool_without_a_darwin_skip_is_installed_on_macos(self):
        ret, out = self.install("fzf", darwin=True)
        self.assertIsNone(ret)
        self.assertEqual(self.delegated, [["packages", "--only", "fzf"]])
        self.assertNotIn("not installed on macOS", out)

    def test_linux_still_installs_the_font(self):
        self.assertTrue(self.font["platforms"]["darwin"]["skip"])
        ret, out = self.install("nerd-font", darwin=False)
        self.assertIsNone(ret)
        self.assertEqual(self.delegated, [["packages", "--only", "nerd-font"]])
        self.assertIn("[*] Installation successful: nerd-font", out)
        self.assertNotIn("macOS", out)


# The standard-library modules bin/dotfiles uses, for interpreters without
# sys.stdlib_module_names (3.9).
KNOWN_STDLIB = {
    "__future__", "argparse", "collections", "contextlib", "functools", "json", "os",
    "pwd", "shlex", "shutil", "stat", "subprocess", "sys", "tempfile", "webbrowser",
}


class Python39Tests(unittest.TestCase):
    """bin/dotfiles may start under the Command Line Tools' python3 (3.9) on
    macOS, before it re-executes itself: 3.9 grammar, standard library only
    (and installer.darwin_proc of its checkout)."""

    def test_python_3_9_grammar_and_standard_library(self):
        source = DOTFILES.read_text(encoding="utf-8")
        tree = ast.parse(source, filename="bin/dotfiles", feature_version=(3, 9))
        stdlib = KNOWN_STDLIB
        if hasattr(sys, "stdlib_module_names"):
            self.assertLessEqual(KNOWN_STDLIB, set(sys.stdlib_module_names))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                with self.subTest(module=module):
                    self.assertTrue(module.split(".")[0] in stdlib or module == "installer",
                                    module)


if __name__ == "__main__":
    unittest.main()
