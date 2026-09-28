"""bin/dotfiles: argument table, delegation to install.py and the update flow.

Every test runs the working-tree ``bin/dotfiles`` with ``HOME`` pointing at a
temporary home whose ``~/.dotfiles`` is a scratch clone of a scratch bare
origin. Its ``install.py`` is a fake that records how it was called, so no
test ever installs anything. git runs with a private global config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
                             "[advice]\n\tdetachedHead = false\n")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "NO_COLOR": "1",
            "FAKE_INSTALL_LOG": str(self.log),
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


if __name__ == "__main__":
    unittest.main()
