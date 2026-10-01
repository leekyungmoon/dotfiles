"""End-to-end install flow against a temporary home.

The repository under test is a local bare mirror cloned from this checkout's
committed HEAD, plus one scratch commit (made only in a temporary clone) that
overlays the current working-tree installer files and the not-yet-committed
files the install needs. Each test clones that mirror into ``<temp
home>/.dotfiles`` exactly as ``etc/install`` does and runs ``install.main``
for that checkout. Submodules come from local bare copies fetched once from
their public GitHub origins and wired in with ``url.<local>.insteadOf`` in a
private git config.

Nothing here touches the real home: the Target comes from a fake passwd entry
for the temp home, git runs with ``GIT_CONFIG_GLOBAL`` pointing into the temp
tree, and zsh, tmux, systemctl and chsh are answered by a fake runner.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from installer import ui  # noqa: E402
from installer.platform import Platform, Target  # noqa: E402
from installer.runner import Runner  # noqa: E402
from installer import transaction  # noqa: E402

# Working-tree files carried by the scratch overlay commit.
OVERLAY_GLOBS = [
    "install.py",
    "installer/*.py",
    "bin/dotfiles",
    "etc/install",
    "manifests/*.json",
    "systemd/**/*",
    "git/gitconfig.stub",
    "tmux/resurrect*",
    "desktop/**/*",
    ".gitignore",
]
FAKE = "/nonexistent-fake-bin"
FLAGS = ["--no-packages", "--no-gui", "--no-shell-change"]


def _git_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "githome"),
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
    }


def _git(env, *args, cwd=None) -> str:
    completed = subprocess.run(["git", *args], env=env, cwd=cwd, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=600)
    return completed.stdout.decode().strip()


def overlay_files() -> list[str]:
    seen = []
    for pattern in OVERLAY_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                rel = str(path.relative_to(REPO_ROOT))
                if rel not in seen:
                    seen.append(rel)
    return seen


class FakeRunner(Runner):
    """Real git (isolated config); everything else recorded and answered."""

    def __init__(self, git_env: dict[str, str], *, systemctl: bool = True,
                 zsh_rc: int = 0):
        self.git_env = git_env
        self.systemctl = systemctl
        self.zsh_rc = zsh_rc
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if Path(argv[0]).name == "git":
            merged = dict(env or {})
            merged.update(self.git_env)
            return super().run(argv, timeout=timeout, check=check, env=merged,
                               input=input, cwd=cwd, read_only=read_only)
        if argv[0] == f"{FAKE}/zsh" and argv[1:] == ["-i", "-c", "exit"]:
            return subprocess.CompletedProcess(argv, self.zsh_rc, b"",
                                               b"zshrc: parse error" if self.zsh_rc else b"")
        if argv[0].startswith(FAKE) or argv[0] in ("chsh", "systemctl", "sudo",
                                                   "apt-get", "passwd"):
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError(f"unexpected command {argv}")

    def which(self, name):
        if name == "git":
            return shutil.which("git")
        if name == "systemctl":
            return f"{FAKE}/systemctl" if self.systemctl else None
        if name in ("zsh", "tmux"):
            return f"{FAKE}/{name}"
        return None

    def ran(self, name) -> list[list[str]]:
        return [c for c in self.calls if Path(c[0]).name == name]


def _tree_digest(root: Path, skip=()) -> dict[str, str]:
    out = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        if path.is_symlink():
            out[rel] = "L:" + os.readlink(path)
        elif path.is_file():
            out[rel] = "F:" + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "D"
    return out


class InstallFlowTests(unittest.TestCase):
    DETACHED_BRANCH = "pdf-flow-detached"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pdf-flow-")
        root = Path(cls._tmp.name)
        cls.root = root
        (root / "githome").mkdir()
        env = _git_env(root)
        cls.git_env = env

        modules = subprocess.run(
            ["git", "config", "-f", str(REPO_ROOT / ".gitmodules"), "--get-regexp",
             r"^submodule\..*\.url$"], stdout=subprocess.PIPE, check=True,
            env=env).stdout.decode().split("\n")
        config = ["[protocol \"file\"]", "\tallow = always",
                  "[user]", "\tname = Fixture", "\temail = fixture@example.invalid",
                  "[init]", "\tdefaultBranch = main", "[advice]",
                  "\tdetachedHead = false"]
        cache = root / "submodule-cache"
        cache.mkdir()
        for line in filter(None, modules):
            key, url = line.split(" ", 1)
            name = key[len("submodule."):-len(".url")]
            local = cache / (name.replace("/", "_") + ".git")
            try:
                _git(env, "clone", "--bare", "--quiet", url, str(local))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                cls._tmp.cleanup()
                raise unittest.SkipTest(f"cannot fetch submodule {url}: {exc}")
            config += [f"[url \"{local}\"]", f"\tinsteadOf = {url}"]
        (root / "gitconfig").write_text("\n".join(config) + "\n")

        # Local tmux plugin fixture pinned by the overlay manifest.
        plugin = root / "plugin-src"
        _git(env, "init", "--quiet", str(plugin))
        (plugin / "plugin.tmux").write_text("# fixture plugin\n")
        _git(env, "-C", str(plugin), "add", ".")
        _git(env, "-C", str(plugin), "commit", "--quiet", "-m", "plugin")
        cls.plugin_commit = _git(env, "-C", str(plugin), "rev-parse", "HEAD")

        # Mirror of committed HEAD + scratch overlay commit.
        mirror = root / "mirror.git"
        _git(env, "clone", "--bare", "--quiet", str(REPO_ROOT), str(mirror))
        branch = _git(env, "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            # Detached checkout (CI, 'git checkout <sha>'): name the commit
            # in the scratch mirror only; REPO_ROOT is read, never changed.
            branch = cls.DETACHED_BRANCH
            _git(env, "-C", str(mirror), "fetch", "--quiet", str(REPO_ROOT),
                 f"+HEAD:refs/heads/{branch}")
        work = root / "work"
        _git(env, "clone", "--quiet", "--branch", branch, str(mirror), str(work))
        for rel in overlay_files():
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, dst)
        if (work / "bootstrap.sh").exists():
            (work / "bootstrap.sh").unlink()
        plugins = {"schema": 1, "plugins": [
            {"name": "tpm", "url": str(plugin), "commit": cls.plugin_commit}]}
        (work / "manifests" / "tmux-plugins.json").write_text(json.dumps(plugins))
        _git(env, "-C", str(work), "add", "-A")
        _git(env, "-C", str(work), "commit", "--quiet", "-m", "test overlay")
        _git(env, "-C", str(work), "push", "--quiet", "origin", f"HEAD:{branch}")
        cls.mirror = mirror
        cls.branch = branch
        cls.head = _git(env, "-C", str(work), "rev-parse", "HEAD")
        with open(work / "manifests" / "managed-paths.json") as handle:
            cls.manifest = json.load(handle)["entries"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def setUp(self):
        self._home_tmp = tempfile.TemporaryDirectory(prefix="pdf-home-")
        home = Path(self._home_tmp.name) / "home"
        home.mkdir()
        self.home = home
        self.target = Target(
            uid=os.getuid(), gid=os.getgid(), username="fixture", home=home,
            data_home=home / ".local" / "share", state_home=home / ".local" / "state",
            config_home=home / ".config", cache_home=home / ".cache")
        self.checkout = home / ".dotfiles"
        self.shell = "/usr/bin/zsh"
        self.chsh_calls: list[list[str]] = []

    def tearDown(self):
        self._home_tmp.cleanup()

    def clone(self, dest: Path | None = None) -> Path:
        dest = dest or self.checkout
        _git(self.git_env, "clone", "--quiet", "--recursive", "-j8", "--branch",
             self.branch, str(self.mirror), str(dest))
        return dest

    def getpwuid(self, uid):
        if uid != os.getuid():
            raise KeyError(uid)
        return pwd.struct_passwd(("fixture", "x", uid, os.getgid(), "",
                                  str(self.home), self.shell))

    def main(self, *argv, runner=None, here=None) -> tuple[int, str, FakeRunner]:
        runner = runner or FakeRunner(self.git_env)
        out = io.StringIO()

        def interactive(args):
            self.chsh_calls.append(list(args))
            return 0

        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "NO_COLOR": "1"}
        with redirect_stdout(out), redirect_stderr(out):
            rc = install.main(list(argv), env=env, euid=os.getuid(),
                              getpwuid=self.getpwuid, runner=runner,
                              detect=lambda: Platform("ubuntu", "24.04", "amd64"),
                              here=here or self.checkout, prompt=False,
                              interactive=interactive)
        self.last_output = out.getvalue()
        return rc, out.getvalue(), runner

    def install(self, *extra, runner=None) -> tuple[int, str, FakeRunner]:
        return self.main(*FLAGS, *extra, runner=runner)

    def dest(self, entry) -> Path:
        t = self.target
        roots = {"{home}": t.home, "{config}": t.config_home, "{data}": t.data_home,
                 "{state}": t.state_home, "{cache}": t.cache_home}
        token, _, rest = entry["dest"].partition("/")
        return roots[token] / rest

    def status(self) -> dict:
        return json.loads((self.target.state_root / "status.json").read_text())

    def phases(self) -> dict:
        return {p["phase"]: p for p in self.status()["phases"]}

    def assert_installed(self, *, systemd=True):
        checkout = self.checkout
        self.assertEqual(self.target.repo_root, checkout)
        self.assertTrue((checkout / ".git").is_dir())
        for entry in self.manifest:
            dest = self.dest(entry)
            if entry.get("condition") == "systemd-user" and not systemd:
                self.assertFalse(os.path.lexists(dest), entry["id"])
                continue
            if entry["kind"] == "symlink":
                # Exactly upstream's links: absolute ~/.dotfiles/<source>.
                self.assertEqual(os.readlink(dest), str(checkout / entry["source"]),
                                 entry["id"])
                self.assertTrue(dest.exists(), entry["id"])
            elif entry["kind"] == "copy":
                self.assertTrue(dest.is_file() and not dest.is_symlink(), entry["id"])
                self.assertEqual(dest.read_bytes(),
                                 (checkout / entry["source"]).read_bytes(), entry["id"])
            elif entry["kind"] == "link":
                self.assertTrue(dest.is_symlink(), entry["id"])
            elif entry["kind"] == "remove":
                self.assertFalse(os.path.lexists(dest), entry["id"])
        # The installer never writes into the checkout.
        self.assertEqual(_git(self.git_env, "-C", str(checkout), "status",
                              "--porcelain", "--ignored"), "")

    # -- tests -----------------------------------------------------------------

    # The symlinks upstream wookayin/dotfiles' install.py creates, relative to
    # ~/.dotfiles (plug.vim is written through the ~/.vim link, as upstream does).
    UPSTREAM_LINKS = {
        ".bashrc": "bashrc", ".screenrc": "screenrc", ".vimrc": "vim/vimrc",
        ".vim": "vim", ".config/nvim": "nvim", ".gitconfig": "git/gitconfig",
        ".gitignore": "git/gitignore", ".zsh": "zsh", ".zlogin": "zsh/zlogin",
        ".zlogout": "zsh/zlogout", ".zpreztorc": "zsh/zpreztorc",
        ".zprofile": "zsh/zprofile", ".zshenv": "zsh/zshenv", ".zshrc": "zsh/zshrc",
        ".local/bin/dotfiles": "bin/dotfiles", ".local/bin/fasd": "zsh/fasd/fasd",
        ".Xmodmap": "Xmodmap", ".gtkrc-2.0": "gtkrc-2.0",
        ".config/kitty": "config/kitty", ".config/alacritty": "config/alacritty",
        ".config/wezterm": "config/wezterm", ".tmux": "tmux",
        ".tmux.conf": "tmux/tmux.conf", ".config/terminator": "config/terminator",
        ".config/pudb/pudb.cfg": "config/pudb/pudb.cfg",
        ".pythonrc.py": "python/pythonrc.py", ".pylintrc": "python/pylintrc",
        ".condarc": "python/condarc", ".config/pycodestyle": "python/pycodestyle",
        ".config/ptpython/config.py": "python/ptpython.config.py",
    }

    def test_home_with_an_upstream_wookayin_install_is_overwritten(self):
        # The first real machine had wookayin/dotfiles installed before: every
        # upstream link now points into the new ~/.dotfiles. The install must
        # take all of them over (backing up what it replaces), not refuse.
        self.clone()
        for rel, source in self.UPSTREAM_LINKS.items():
            dest = self.home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(self.checkout / source)
        fzf_home = self.home / ".fzf" / "bin"
        fzf_home.mkdir(parents=True)
        (fzf_home / "fzf").write_text("#!/bin/sh\n")
        (self.home / ".local/bin/fzf").symlink_to(fzf_home / "fzf")
        rc, out, _ = self.install()
        self.assertEqual(rc, 0, out)
        status = self.status()
        transaction = next(p for p in status["phases"] if p["phase"] == "transaction")
        self.assertEqual(transaction["status"], "PASS", out)
        for entry in self.manifest:
            if entry.get("condition") == "systemd-user":
                continue  # no user manager in this harness: reported as skipped
            dest = self.dest(entry)
            with self.subTest(entry=entry["id"]):
                if entry["kind"] == "symlink":
                    self.assertTrue(dest.is_symlink(), entry["dest"])
                    self.assertEqual(os.readlink(dest), str(self.checkout / entry["source"]))
                elif entry["kind"] == "copy":
                    self.assertFalse(dest.is_symlink(), entry["dest"])
                    self.assertEqual(dest.read_bytes(),
                                     (self.checkout / entry["source"]).read_bytes())
        # Nothing was written into the checkout through an old link.
        dirty = _git(self.git_env, "-C", str(self.checkout), "status", "--porcelain",
                     "--untracked-files=no")
        self.assertEqual(dirty, "", dirty)

    def test_empty_home_install(self):
        self.clone()
        rc, out, runner = self.install()
        self.assertEqual(rc, 0, out)
        self.assert_installed()
        self.assertIn("@leekyungmoon's", out.splitlines()[1])
        headers = ["Checking platform", "Installing packages", "Creating symbolic links",
                   "Post actions", "GNOME settings"]
        positions = [out.index(f"┃ {h}  ┃") for h in headers]
        positions.append(out.index("You are all set!"))
        self.assertEqual(positions, sorted(positions))
        zshrc_line = "{:60s} : {}".format(
            str(self.home / ".zshrc"),
            f"symlink created from '{self.checkout / 'zsh/zshrc'}'")
        self.assertIn(zshrc_line, out)
        status = self.status()
        self.assertEqual(status["overall"], "SKIPPED")  # packages/gui skipped
        by_phase = self.phases()
        self.assertEqual(list(by_phase), ["preflight", "packages", "transaction",
                                          "post-install", "smoke", "login-shell",
                                          "git-identity", "auth", "gui"])
        self.assertEqual(by_phase["auth"]["status"], "SKIPPED")  # no AI CLIs here
        self.assertEqual(by_phase["packages"]["status"], "SKIPPED")
        self.assertEqual(by_phase["gui"]["status"], "SKIPPED")
        self.assertEqual(by_phase["transaction"]["status"], "PASS")
        self.assertEqual(by_phase["transaction"]["details"]["commit"], self.head)
        self.assertEqual(by_phase["smoke"]["status"], "PASS")
        self.assertEqual(by_phase["login-shell"]["status"], "PASS")
        self.assertEqual(by_phase["git-identity"]["status"], "SKIPPED")
        self.assertIn("git config --file", out)
        self.assertEqual((self.target.state_root / "status.json").stat().st_mode & 0o777,
                         0o600)
        # No staging and no durable copy: ~/.dotfiles is the checkout.
        self.assertFalse(self.target.staging_root.exists())
        self.assertFalse((self.target.data_home / "personal-dotfiles").exists())
        # Post-install: plugin at pin, resurrect dir private, daemon-reload ran.
        plugin = self.target.data_home / "tmux" / "plugins" / "tpm"
        self.assertEqual(_git(self.git_env, "-C", str(plugin), "rev-parse", "HEAD"),
                         self.plugin_commit)
        resurrect = self.target.data_home / "tmux" / "resurrect"
        self.assertEqual(resurrect.stat().st_mode & 0o777, 0o700)
        self.assertIn(["systemctl", "--user", "daemon-reload"], runner.calls)
        # REQ-4: the newly wanted autosave timer is started, after the reload.
        start = ["systemctl", "--user", "start", "tmux-resurrect-autosave.timer"]
        self.assertIn(start, runner.calls)
        self.assertLess(runner.calls.index(["systemctl", "--user", "daemon-reload"]),
                        runner.calls.index(start))
        self.assertEqual(by_phase["post-install"]["details"]["autosave_timer"], "started")
        # zsh plugin prefill ran (fake zsh), nvim is absent here.
        self.assertTrue(any(c[1:2] == ["-c"] and "antidote" in c[-1]
                            for c in runner.ran("zsh")))
        # The running-server reload probed the user's default server (no -L/-S),
        # read-only; the fake answers with no config inside this home, so
        # nothing was sourced into it.
        tmux = runner.ran("tmux")
        probes = [c for c in tmux if c[1:2] == ["display-message"]]
        self.assertEqual(probes, [[f"{FAKE}/tmux", "display-message", "-p", "#{config_files}"]])
        self.assertEqual(by_phase["post-install"]["details"]["tmux_reload"],
                         "running-server-uses-another-config")
        self.assertFalse(any("source-file" in c and "-L" not in c for c in tmux))
        # Smoke checks used an isolated -L socket and killed exactly it.
        smoke = [c for c in tmux if "-L" in c]
        sockets = {c[c.index("-L") + 1] for c in smoke}
        self.assertEqual(len(sockets), 1)
        self.assertEqual(smoke[-1][-1], "kill-server")
        # The repo submodules were never touched by the plugin step.
        self.assertTrue((self.checkout / "tmux/plugins/tpm/tpm").exists())
        # ~/.gitconfig is a small copied stub that includes the tracked file.
        gitconfig = (self.home / ".gitconfig").read_text()
        self.assertIn("path = ~/.dotfiles/git/gitconfig", gitconfig)

    def test_conflicts_backed_up_exactly_and_sentinel_untouched(self):
        self.clone()
        home = self.home
        (home / ".zshrc").write_text("old zshrc\n")
        os.chmod(home / ".zshrc", 0o640)
        (home / ".vim").mkdir()
        (home / ".vim" / "keep.vim").write_text("x")
        os.symlink("/usr/share/doc", home / ".bashrc")
        os.symlink(str(home / "missing-target"), home / ".gitconfig")
        (home / ".ptpython").mkdir()
        (home / ".ptpython" / "config.py").write_text("legacy")
        (home / ".config" / "kitty").mkdir(parents=True)
        (home / ".config" / "kitty" / "kitty.conf").write_text("mine")
        dropin = home / ".config/systemd/user/tmux.service.d/login.conf"
        dropin.parent.mkdir(parents=True)
        dropin.write_text("[Service]\nExecStart=\n")
        sentinel = home / "unmanaged.txt"
        sentinel.write_text("do not touch")
        before = _tree_digest(home, skip=[".dotfiles"])

        with mock.patch.object(ui, "colors_wanted", return_value=True):
            rc, colored, _ = self.install()
        self.assertEqual(rc, 0, colored)
        self.assertIn("\033[0;33mbacked up to ", colored)
        ui.configure(enabled=False)
        self.assert_installed()
        self.assertEqual(sentinel.read_text(), "do not touch")
        run_id = self.status()["run_id"]
        runs = self.target.state_root / "backups" / "runs" / run_id
        baseline = self.target.state_root / "backups" / "baseline"
        # zshrc was first seen in this run, so its backup is the baseline copy.
        zshrc_backup = baseline / "zshrc" / "object"
        self.assertEqual(zshrc_backup.read_text(), "old zshrc\n")
        plain = colored.replace("\033[0;33m", "").replace("\033[0;32m", "") \
            .replace("\033[0;34m", "").replace("\033[0m", "")
        self.assertIn(f"backed up to {zshrc_backup}, replaced", plain)
        # An existing tmux.service.d drop-in is the user's: left exactly as it was.
        self.assertEqual(dropin.read_text(), "[Service]\nExecStart=\n")
        self.assertNotIn("login.conf", plain)
        self.assertTrue(runs.is_dir())

        rc, out, _ = self.main("restore", "--baseline")
        self.assertEqual(rc, 0, out)
        after = _tree_digest(home, skip=[".dotfiles", ".local/state", ".gitconfig.secret",
                                         ".local/share/tmux"])
        for key in ("unmanaged.txt", ".zshrc", ".vim", ".vim/keep.vim", ".bashrc",
                    ".gitconfig", ".ptpython/config.py", ".config/kitty/kitty.conf",
                    ".config/systemd/user/tmux.service.d/login.conf"):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual((home / ".zshrc").stat().st_mode & 0o777, 0o640)
        # Restore never removes the checkout itself.
        self.assertTrue((self.checkout / ".git").is_dir())

    def test_rerun_twice_converges_and_baseline_is_immutable(self):
        self.clone()
        (self.home / ".zshrc").write_text("old")
        rc, _, _ = self.install()
        self.assertEqual(rc, 0)
        baseline = self.target.state_root / "backups" / "baseline"
        first_baseline = _tree_digest(baseline)
        first_ids = set(transaction.load_status(self.target)["entries"])
        for _ in range(2):
            rc, out, _ = self.install()
            self.assertEqual(rc, 0, out)
            self.assert_installed()
            self.assertEqual(_tree_digest(baseline), first_baseline)
            state = transaction.load_status(self.target)
            self.assertEqual(set(state["entries"]), first_ids)
            self.assertEqual(state["drifted"], [])
            tx = self.phases()["transaction"]["details"]["result"]
            self.assertEqual(tx["changed"], [])
            self.assertIn("zshrc", tx["unchanged"])
            self.assertNotIn("backed up to", out)
            self.assertIn("{:60s} : {}".format(str(self.home / ".zshrc"),
                                               "already up-to-date"), out)

    def test_restore_baseline_then_reinstall(self):
        self.clone()
        (self.home / ".tmux.conf").write_text("user tmux\n")
        self.assertEqual(self.install()[0], 0)
        rc, out, _ = self.main("restore", "--baseline")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.home / ".tmux.conf").read_text(), "user tmux\n")
        self.assertFalse(os.path.lexists(self.home / ".zshrc"))
        self.assertTrue((self.checkout / ".git").is_dir())
        rc, out, _ = self.install()
        self.assertEqual(rc, 0, out)
        self.assert_installed()

    def test_repair_restores_deleted_link(self):
        self.clone()
        self.assertEqual(self.install()[0], 0)
        (self.home / ".zshrc").unlink()
        rc, out, _ = self.main("repair", *FLAGS)
        self.assertEqual(rc, 0, out)
        self.assert_installed()
        self.assertEqual(self.status()["command"], "repair")

    def test_login_shell_change_reports_relogin(self):
        self.clone()
        self.shell = "/bin/bash"
        rc, out, _ = self.main("--no-packages", "--no-gui")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.chsh_calls), 1)
        self.assertEqual(self.chsh_calls[0][:2], ["chsh", "-s"])
        self.assertIn("Please type your password if you wish to change the default "
                      "shell to ZSH", out)
        self.assertEqual(self.phases()["login-shell"]["status"], "RELOGIN_REQUIRED")
        self.assertIn("Log out and back in so zsh becomes your login shell.", out)

    def test_failed_smoke_keeps_the_login_shell(self):
        # REQ-7: a zsh that fails 'zsh -i -c exit' never becomes the login shell.
        self.clone()
        self.shell = "/bin/bash"
        rc, out, _ = self.main("--no-packages", "--no-gui",
                               runner=FakeRunner(self.git_env, zsh_rc=1))
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.chsh_calls, [])
        by_phase = self.phases()
        self.assertEqual(by_phase["smoke"]["status"], "FAIL")
        self.assertEqual(by_phase["login-shell"]["status"], "SKIPPED")
        self.assertIn("smoke-failed", by_phase["login-shell"]["reasons"][0])
        self.assertIn("zsh is not your login shell yet", out)

    def packages_seam(self, names):
        """A packages phase that 'installs' ``names`` and requests their links."""

        from installer import packages
        real = install.load_seam
        bin_dir = self.target.data_home / "personal-dotfiles" / "bin"
        link_names = {"neovim": "nvim", "fzf": "fzf"}

        def run(target, platform, runner, *, dry_run=False, only=None):
            chosen = list(only or names)
            return {"phase": "packages", "status": "PASS", "reasons": [],
                    "details": {"links": [
                        {"id": f"tool-link-{link_names[n]}",
                         "dest": str(self.home / ".local/bin" / link_names[n]),
                         "kind": "symlink", "link_text": str(bin_dir / link_names[n])}
                        for n in chosen]}}

        def seam(module, name):
            if (module, name) == ("packages", "run_packages_phase"):
                return run
            if (module, name) == ("packages", "link_requests"):
                return packages.link_requests
            return real(module, name)
        return seam

    def test_no_packages_rerun_keeps_tool_links(self):
        # REQ-5: --no-packages must not retire ~/.local/bin/{nvim,fzf,...}.
        self.clone()
        nvim = self.home / ".local" / "bin" / "nvim"
        with mock.patch.object(install, "load_seam",
                               side_effect=self.packages_seam(["neovim"])):
            rc, out, _ = self.main("--no-gui", "--no-shell-change")
        self.assertEqual(rc, 0, out)
        self.assertTrue(nvim.is_symlink())
        for argv in (FLAGS, ["repair", *FLAGS]):
            rc, out, _ = self.main(*argv)
            self.assertEqual(rc, 0, out)
            self.assertTrue(nvim.is_symlink(), argv)
            self.assertNotIn("no longer managed", out)
            self.assertIn("tool-link-nvim", transaction.load_status(self.target)["entries"])
        self.assert_installed()

    def test_install_one_tool_creates_its_link(self):
        # REQ-9: 'dotfiles install fzf' -> 'install.py packages --only fzf'.
        self.clone()
        self.assertEqual(self.install()[0], 0)
        before = set(transaction.load_status(self.target)["entries"])
        with mock.patch.object(install, "load_seam",
                               side_effect=self.packages_seam(["fzf"])):
            rc, out, _ = self.main("packages", "--only", "fzf")
        self.assertEqual(rc, 0, out)
        fzf = self.home / ".local" / "bin" / "fzf"
        self.assertEqual(os.readlink(fzf), str(self.target.data_home
                                              / "personal-dotfiles" / "bin" / "fzf"))
        after = set(transaction.load_status(self.target)["entries"])
        self.assertEqual(after, before | {"tool-link-fzf"})
        self.assertNotIn("no longer managed", out)
        self.assert_installed()
        self.assertEqual(self.phases()["transaction"]["details"]["result"]["retired"], [])

    def test_dry_run_changes_nothing(self):
        self.clone()
        (self.home / ".zshrc").write_text("mine")
        before = _tree_digest(self.home)
        from installer.runner import DryRunRunner

        class IsolatedDry(DryRunRunner):
            def run(inner, argv, **kw):
                kw["env"] = self.git_env
                return super().run(argv, **kw)

        rc, out, _ = self.main("--dry-run", "--no-packages", runner=IsolatedDry())
        self.assertEqual(rc, 0, out)
        self.assertIn(f"replace {self.home / '.zshrc'}", out)
        self.assertIn("dry run: nothing was changed", out)
        self.assertEqual(_tree_digest(self.home), before)

    def test_without_systemd_units_are_skipped(self):
        self.clone()
        rc, out, runner = self.install(runner=FakeRunner(self.git_env, systemctl=False))
        self.assertEqual(rc, 0, out)
        self.assert_installed(systemd=False)
        self.assertFalse(os.path.lexists(self.target.config_home / "systemd"))
        tx = self.phases()["transaction"]
        self.assertIn("systemd-tmux-service", tx["details"]["skipped_ids"])
        self.assertIn("systemd-user entries skipped: no systemd user manager", tx["reasons"])
        self.assertIn("skipped (no systemd user manager)", out)
        self.assertFalse(runner.ran("systemctl"))

    def test_checkout_elsewhere_is_refused(self):
        other = self.clone(Path(self._home_tmp.name) / "elsewhere")
        rc, out, runner = self.main(*FLAGS, here=other)
        self.assertEqual(rc, 2)
        self.assertIn("git clone --recursive", out)
        self.assertEqual(runner.calls, [])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])

    def test_gui_autostart_entry_is_part_of_the_transaction(self):
        self.clone()
        from installer.transaction import DesiredEntry
        real = install.load_seam
        dest = self.target.config_home / "autostart" / "fixture-gui-apply.desktop"

        def seam(module, name):
            if (module, name) == ("gui", "autostart_desired_entry"):
                return lambda target: DesiredEntry("gui-autostart", dest, "file",
                                                   content=b"[Desktop Entry]\n", mode=0o644)
            if (module, name) == ("gui", "apply_or_defer"):
                return lambda target, runner, env: {
                    "phase": "gui", "status": "PENDING_GUI",
                    "reasons": ["no graphical session"], "details": {}}
            return real(module, name)

        with mock.patch.object(install, "load_seam", side_effect=seam):
            rc, out, _ = self.main("--no-packages", "--no-shell-change")
        self.assertEqual(rc, 0, out)
        self.assertEqual(dest.read_bytes(), b"[Desktop Entry]\n")
        self.assertIn("gui-autostart", transaction.load_status(self.target)["entries"])
        self.assertEqual(self.phases()["gui"]["status"], "PENDING_GUI")
        self.assertIn("no graphical session", out)
        rc, out, _ = self.main("restore", "--baseline")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(dest))


if __name__ == "__main__":
    unittest.main()
