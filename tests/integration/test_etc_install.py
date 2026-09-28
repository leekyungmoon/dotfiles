"""etc/install piped into bash, the way 'curl -fsSL .../etc/install | bash' runs it.

Every run uses a temporary HOME, a private git config, fake ``sudo`` and
``apt-get`` on PATH (they only record their arguments) and a fake
``python3`` that records how ``install.py`` was started and then checks, with
the real interpreter, that the cloned ``install.py`` accepts those arguments.
The real installer is never executed here (it would reach the real user
manager through systemctl); tests/integration/test_install_flow.py runs it
against a temporary home.

The repository is a local bare mirror of a scratch repository that carries
the working-tree ``install.py``, ``installer/`` and manifests (no
submodules), so no network access is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ETC_INSTALL = REPO_ROOT / "etc" / "install"
REAL_PYTHON = sys.executable


def _supported_host() -> bool:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return False
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    release = fields.get("VERSION_ID", "").strip('"')
    return fields.get("ID", "").strip('"') == "ubuntu" and release in ("22.04", "24.04")


FAKE_PYTHON = r'''#!/bin/bash
# Fake python3: real interpreter for -c probes; install.py runs are recorded
# and only parsed (never executed) with the real interpreter.
if [[ "$1" == -c ]]; then exec "@REAL@" "$@"; fi
tty=no; [[ -t 0 ]] && tty=yes
printf '%s\n' "$PWD" > "@LOG@.cwd"
printf '%s\0' "$@" > "@LOG@.argv"
exec "@REAL@" -B -c '
import json, os, sys
sys.path.insert(0, os.getcwd())
import install
args = install.parse_args(sys.argv[2:])
with open(sys.argv[1], "a") as log:
    log.write(json.dumps({"command": args.command, "opts": vars(args)}) + "\n")
' "@LOG@" "${@:2}"
'''

FAKE_RECORDER = '#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {rc}\n'


@unittest.skipUnless(_supported_host(), "etc/install only runs on Ubuntu 22.04/24.04")
class EtcInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pdf-etc-")
        root = Path(cls._tmp.name)
        cls.root = root
        cls.gitconfig = root / "gitconfig"
        cls.gitconfig.write_text("[user]\n\tname = Fixture\n\temail = f@example.invalid\n"
                                 "[init]\n\tdefaultBranch = main\n"
                                 "[advice]\n\tdetachedHead = false\n")
        env = cls.git_env(root)
        source = root / "source"
        subprocess.run(["git", "init", "--quiet", str(source)], env=env, check=True)
        for rel in ["install.py", "etc/install"] + [
                str(p.relative_to(REPO_ROOT)) for p in sorted(REPO_ROOT.glob("installer/*.py"))
        ] + [str(p.relative_to(REPO_ROOT)) for p in sorted(REPO_ROOT.glob("manifests/*.json"))]:
            dst = source / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, dst)
        subprocess.run(["git", "-C", str(source), "add", "-A"], env=env, check=True)
        subprocess.run(["git", "-C", str(source), "commit", "--quiet", "-m", "fixture"],
                       env=env, check=True)
        cls.mirror = root / "mirror.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", str(source), str(cls.mirror)],
                       env=env, check=True)
        cls.source = source

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def git_env(cls, root: Path) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
                "GIT_CONFIG_GLOBAL": str(cls.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "LANG": "C"}

    def setUp(self):
        self._case = tempfile.TemporaryDirectory(prefix="pdf-etc-case-")
        case = Path(self._case.name)
        self.home = case / "home"
        self.home.mkdir()
        self.bin = case / "bin"
        self.bin.mkdir()
        self.log = case / "calls.log"
        self.py_log = case / "python.log"
        for name, rc in (("sudo", 0), ("apt-get", 0)):
            script = self.bin / name
            script.write_text(FAKE_RECORDER.format(name=name, log=self.log, rc=rc))
            script.chmod(0o755)
        python = self.bin / "python3"
        python.write_text(FAKE_PYTHON.replace("@REAL@", REAL_PYTHON)
                          .replace("@LOG@", str(self.py_log)))
        python.chmod(0o755)
        self.case = case

    def tearDown(self):
        self._case.cleanup()

    def run_piped(self, script: bytes | None = None, *, args=(), path=None, **extra):
        env = {"HOME": str(self.home), "PATH": path or f"{self.bin}:{os.environ['PATH']}",
               "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "TERM": "dumb",
               "DOTFILES_REPO_URL": str(self.mirror), **extra}
        body = ETC_INSTALL.read_bytes() if script is None else script
        argv = ["/bin/bash"] + (["-s", "--", *args] if args else [])
        return subprocess.run(argv, input=body, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120,
                              start_new_session=True)  # no controlling terminal

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def installer_runs(self) -> list[dict]:
        if not self.py_log.exists():
            return []
        return [json.loads(line) for line in self.py_log.read_text().splitlines()]

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], env=self.git_env(self.case), check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE
                              ).stdout.decode().strip()

    def backups(self) -> list[Path]:
        root = self.home / ".local/state/personal-dotfiles/backups"
        return sorted(root.glob("pre-install-*")) if root.exists() else []

    # -- tests -----------------------------------------------------------------

    def test_fresh_home_clones_and_runs_install_py(self):
        completed = self.run_piped(args=["--no-packages", "--no-gui", "--no-shell-change"])
        out = completed.stdout.decode()
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn("@leekyungmoon's", out)
        self.assertIn("All Done!", out)
        dotfiles = self.home / ".dotfiles"
        self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"),
                         self.git("-C", str(self.source), "rev-parse", "HEAD"))
        self.assertEqual(self.git("-C", str(dotfiles), "remote", "get-url", "origin"),
                         str(self.mirror))
        runs = self.installer_runs()
        self.assertEqual(len(runs), 1)
        opts = runs[0]["opts"]
        self.assertEqual(runs[0]["command"], "install")
        self.assertTrue(opts["no_packages"] and opts["no_gui"] and opts["no_shell_change"])
        self.assertEqual(Path(self.py_log.with_suffix(".log.cwd").read_text().strip()),
                         dotfiles)
        argv = self.py_log.with_suffix(".log.argv").read_bytes().split(b"\0")
        self.assertEqual(argv[0], b"install.py")
        # set -x echoes the commands like upstream.
        err = completed.stderr.decode()
        self.assertIn("git clone --recursive -j8", err)
        self.assertIn("python3 install.py --no-packages --no-gui --no-shell-change", err)
        self.assertNotIn("sudo", self.calls())
        self.assertEqual(self.backups(), [])

    def test_existing_unrelated_dotfiles_is_moved_aside(self):
        old = self.home / ".dotfiles"
        (old / "sub").mkdir(parents=True)
        (old / "notes.txt").write_text("my old dotfiles\n")
        (old / "sub" / "f").write_text("keep\n")
        completed = self.run_piped()
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        moved = backups[0] / "dotfiles"
        self.assertEqual((moved / "notes.txt").read_text(), "my old dotfiles\n")
        self.assertEqual((moved / "sub" / "f").read_text(), "keep\n")
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        self.assertIn(f"Moved the existing {old} to {moved}", completed.stdout.decode())
        self.assertTrue((old / ".git").is_dir())
        self.assertEqual(len(self.installer_runs()), 1)

    def test_other_clone_or_dirty_clone_is_moved_aside(self):
        other = self.case / "other.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", str(self.mirror), str(other)],
                       env=self.git_env(self.case), check=True)
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(other), str(dotfiles))
        completed = self.run_piped()
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.git("-C", str(dotfiles), "remote", "get-url", "origin"),
                         str(self.mirror))

        # A clone of this repository with tracked edits is moved aside too.
        (dotfiles / "install.py").write_text("# local edit\n")
        completed = self.run_piped()
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        backups = self.backups()
        self.assertEqual(len(backups), 2)
        self.assertEqual((backups[-1] / "dotfiles" / "install.py").read_text(),
                         "# local edit\n")

    def test_existing_clean_clone_is_pulled_not_moved(self):
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
        (dotfiles / "untracked-note").write_text("mine\n")
        work = self.case / "work"
        self.git("clone", "--quiet", str(self.mirror), str(work))
        (work / "NEWS").write_text("new upstream commit\n")
        self.git("-C", str(work), "add", "NEWS")
        self.git("-C", str(work), "commit", "--quiet", "-m", "news")
        self.git("-C", str(work), "push", "--quiet", "origin", "HEAD:main")
        new = self.git("-C", str(work), "rev-parse", "HEAD")
        try:
            completed = self.run_piped()
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"), new)
            self.assertEqual((dotfiles / "untracked-note").read_text(), "mine\n")
            self.assertEqual(self.backups(), [])
            self.assertIn("pull --ff-only", completed.stderr.decode())
            self.assertEqual(len(self.installer_runs()), 1)
        finally:
            self.git("-C", str(work), "push", "--quiet", "--force", "origin",
                     f"{self.git('-C', str(self.source), 'rev-parse', 'HEAD')}:main")

    def test_ssh_spelling_of_origin_counts_as_this_repository(self):
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
        completed = self.run_piped(DOTFILES_REPO_URL=str(self.mirror) + "/")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self.backups(), [])

    def test_bad_url_stops_before_install_py(self):
        completed = self.run_piped(DOTFILES_REPO_URL=str(self.case / "missing.git"))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"git clone of", completed.stderr)
        self.assertIn(b"nothing was installed", completed.stderr)
        self.assertEqual(self.installer_runs(), [])
        self.assertFalse(self.py_log.with_suffix(".log.argv").exists())
        self.assertFalse(os.path.lexists(self.home / ".dotfiles"))
        self.assertNotIn(b"All Done!", completed.stdout)

    def test_root_is_refused(self):
        completed = self.run_piped(_DOTFILES_INSTALL_SIMULATE_ROOT="1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"root", completed.stderr)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.calls(), "")
        self.assertEqual(self.installer_runs(), [])

    def test_truncated_script_does_nothing(self):
        body = ETC_INSTALL.read_bytes()
        for cut in (len(body) // 2, len(body) - len(b'main "$@"\n'), len(body) // 3):
            completed = self.run_piped(body[:cut])
            self.assertEqual(list(self.home.iterdir()), [], cut)
            self.assertEqual(self.calls(), "", cut)
            self.assertEqual(self.installer_runs(), [], cut)
            self.assertNotIn("███".encode(), completed.stdout, cut)  # not even the banner

    def test_missing_prerequisites_use_sudo_apt_get(self):
        # PATH without git: etc/install must ask sudo apt-get for all four.
        tools = self.case / "tools"
        tools.mkdir()
        for name in ("id", "dpkg", "uname", "cat", "mkdir", "date", "mv"):
            real = shutil.which(name)
            if real:
                os.symlink(real, tools / name)
        completed = self.run_piped(path=f"{self.bin}:{tools}")
        calls = self.calls()
        self.assertIn("sudo apt-get update", calls)
        self.assertIn("sudo apt-get install -y git curl ca-certificates python3", calls)
        # The fake apt-get installs nothing, so git is still missing afterwards.
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.installer_runs(), [])

    def test_refused_sudo_fails_clearly(self):
        (self.bin / "sudo").write_text(FAKE_RECORDER.format(name="sudo", log=self.log, rc=1))
        tools = self.case / "tools"
        tools.mkdir()
        for name in ("id", "dpkg", "uname", "cat"):
            real = shutil.which(name)
            if real:
                os.symlink(real, tools / name)
        completed = self.run_piped(path=f"{self.bin}:{tools}")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"sudo apt-get install failed", completed.stderr)
        self.assertNotIn("apt-get install", self.calls().replace("sudo apt-get update", ""))
        self.assertEqual(self.installer_runs(), [])
        self.assertFalse(os.path.lexists(self.home / ".dotfiles"))


if __name__ == "__main__":
    unittest.main()
