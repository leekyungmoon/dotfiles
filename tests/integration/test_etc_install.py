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

A fake ``zsh`` on PATH records how the final 'exec zsh -l' started it (a fake
``getent`` stands in for the passwd login shell when zsh is not on PATH). The
piped runs have no controlling terminal (like a harness or cron), so they must
never start it; the pty runs give the script a real terminal.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
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
# FAKE_INSTALL_PY_RC: make the install.py run fail with that status.
if [[ -n "${FAKE_INSTALL_PY_RC:-}" ]]; then
  printf '%s\0' "$@" > "@LOG@.argv"
  exit "$FAKE_INSTALL_PY_RC"
fi
tty=no; [[ -t 0 ]] && tty=yes
printf '%s\n' "$tty" > "@LOG@.tty"
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

# Fake zsh: records its arguments and whether stdin/stdout are terminals.
FAKE_ZSH = r'''#!/bin/sh
in=no; out=no
[ -t 0 ] && in=yes
[ -t 1 ] && out=yes
echo "zsh $* stdin-tty=$in stdout-tty=$out" >> "@LOG@"
echo "FAKE-ZSH-STARTED"
exit 0
'''

# Fake ssh for GIT_SSH_COMMAND (GIT_SSH_VARIANT=simple: "<host> <command>"):
# serves every git-upload-pack request from the local mirror.
FAKE_SSH = r'''#!/bin/sh
echo "ssh $*" >> "@LOG@"
for last; do :; done
case "$last" in
  git-upload-pack*) exec git upload-pack "@MIRROR@" ;;
esac
exit 1
'''

DEFAULT_URL = "https://github.com/leekyungmoon/dotfiles.git"


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
        for rel in ["install.py", "install", "etc/install"] + [
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
        self.zsh_log = case / "zsh.log"
        zsh = self.bin / "zsh"
        zsh.write_text(FAKE_ZSH.replace("@LOG@", str(self.zsh_log)))
        zsh.chmod(0o755)
        self.case = case

    def tearDown(self):
        self._case.cleanup()

    def run_piped(self, script: bytes | None = None, *, args=(), path=None, **extra):
        """Run etc/install; an ``extra`` value of None removes that variable."""
        env = self.env_for(path, **extra)
        body = ETC_INSTALL.read_bytes() if script is None else script
        argv = ["/bin/bash"] + (["-s", "--", *args] if args else [])
        return subprocess.run(argv, input=body, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120,
                              start_new_session=True)  # no controlling terminal

    def run_wrapper(self, install: Path, *, args=(), **extra):
        """Run a clone's ./install (clone & install) without a terminal."""
        return subprocess.run([str(install), *args], env=self.env_for(**extra),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120, start_new_session=True, cwd=str(self.case))

    def env_for(self, path=None, **extra) -> dict[str, str]:
        env = {"HOME": str(self.home), "PATH": path or f"{self.bin}:{os.environ['PATH']}",
               "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "TERM": "dumb",
               "DOTFILES_REPO_URL": str(self.mirror), **extra}
        return {k: v for k, v in env.items() if v is not None}

    def run_in_terminal(self, *, args=(), stdout_file=None, command=None, **extra):
        """Run 'bash -s' with the script on stdin (as 'curl | bash' does) and a
        pseudo-terminal as the controlling terminal and stdout/stderr.

        Returns (exit status, terminal output). With ``stdout_file``, stdout
        goes to that file instead (the terminal stays the controlling one).
        With ``command`` (an argv, e.g. ~/.dotfiles/install), that runs
        instead, with the terminal as stdin too.
        """
        script = self.case / "install.sh"
        script.write_bytes(ETC_INSTALL.read_bytes())
        env = self.env_for(**extra)
        if command is not None:
            argv = [str(a) for a in command] + list(args)
        else:
            argv = ["/bin/bash", "-s", "--", *args] if args else ["/bin/bash", "-s"]
        pid, fd = pty.fork()
        if pid == 0:  # child: the pty is its controlling terminal
            try:
                if command is None:
                    stdin = os.open(script, os.O_RDONLY)
                    os.dup2(stdin, 0)
                if stdout_file is not None:
                    out = os.open(stdout_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    os.dup2(out, 1)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        chunks = []
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                try:
                    data = os.read(fd, 65536)
                except OSError:  # EIO: every writer of the terminal is gone
                    break
                if not data:
                    break
                chunks.append(data)
            else:
                os.kill(pid, 9)
                self.fail("etc/install did not finish in a terminal")
        finally:
            _, status = os.waitpid(pid, 0)
            os.close(fd)
        return os.waitstatus_to_exitcode(status), b"".join(chunks).decode(errors="replace")

    def run_on_terminal_without_ctty(self):
        """stdout/stderr on a pseudo-terminal, but in a new session that has
        no controlling terminal: /dev/tty cannot be opened."""
        master, slave = os.openpty()
        try:
            proc = subprocess.Popen(["/bin/bash", "-s"], stdin=subprocess.PIPE,
                                    stdout=slave, stderr=slave, env=self.env_for(),
                                    start_new_session=True)
        finally:
            os.close(slave)
        proc.stdin.write(ETC_INSTALL.read_bytes())
        proc.stdin.close()
        chunks = []
        while True:
            ready, _, _ = select.select([master], [], [], 1.0)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                chunks.append(data)
            elif proc.poll() is not None:
                break
        os.close(master)
        return proc.wait(timeout=120), b"".join(chunks).decode(errors="replace")

    def zsh_runs(self) -> list[str]:
        return self.zsh_log.read_text().splitlines() if self.zsh_log.exists() else []

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

    def leftovers(self) -> list[str]:
        """Temporary sibling clones left in HOME (there must never be any)."""
        return sorted(p.name for p in self.home.iterdir() if ".new-" in p.name)

    def assert_fresh_clone(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        dotfiles = self.home / ".dotfiles"
        self.assertTrue(dotfiles.is_dir() and not dotfiles.is_symlink())
        self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"),
                         self.git("-C", str(self.source), "rev-parse", "HEAD"))
        self.assertEqual(self.git("-C", str(dotfiles), "remote", "get-url", "origin"),
                         str(self.mirror))
        self.assertEqual(self.git("-C", str(dotfiles), "status", "--porcelain"), "")
        self.assertEqual(len(self.installer_runs()), 1)
        self.assertEqual(self.leftovers(), [])
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        moved = backups[0] / "dotfiles"
        self.assertIn(f"Moved the existing {dotfiles} to {moved}", completed.stdout.decode())
        return moved

    def push_to_mirror(self, name="NEWS") -> str:
        """Add an upstream commit to the class mirror (undone by addCleanup)."""
        work = self.case / "work"
        self.git("clone", "--quiet", str(self.mirror), str(work))
        (work / name).write_text("new upstream commit\n")
        self.git("-C", str(work), "add", name)
        self.git("-C", str(work), "commit", "--quiet", "-m", "news")
        self.git("-C", str(work), "push", "--quiet", "origin", "HEAD:main")
        self.addCleanup(self.git, "-C", str(self.mirror), "update-ref", "refs/heads/main",
                        self.git("-C", str(self.source), "rev-parse", "HEAD"))
        return self.git("-C", str(work), "rev-parse", "HEAD")

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
        # No terminal (a harness, cron, CI): the script ends, no shell starts.
        self.assertEqual(self.zsh_runs(), [])
        self.assertNotIn("Starting a new zsh", out)

    # -- clone & install (./install) ----------------------------------------------

    FLAGS = ("--no-packages", "--no-gui", "--no-shell-change")

    def checkout(self, path=None) -> Path:
        """~/.dotfiles (or ``path``) as a clone of the mirror."""
        path = path or self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.mirror), str(path))
        return path

    def local_work(self, checkout: Path) -> str:
        """A local commit, an uncommitted edit and an untracked file; returns
        the new HEAD."""
        (checkout / "LOCAL").write_text("committed\n")
        self.git("-C", str(checkout), "add", "LOCAL")
        self.git("-C", str(checkout), "commit", "--quiet", "-m", "local work")
        (checkout / "LOCAL").write_text("committed\nand edited\n")
        (checkout / "UNTRACKED").write_text("mine\n")
        return self.git("-C", str(checkout), "rev-parse", "HEAD")

    def assert_in_place(self, completed, checkout: Path, head: str):
        out, err = completed.stdout.decode(), completed.stderr.decode()
        self.assertEqual(completed.returncode, 0, err)
        self.assertIn("All Done!", out)
        self.assertEqual(len(self.installer_runs()), 1)
        self.assertEqual(Path(self.py_log.with_suffix(".log.cwd").read_text().strip()),
                         self.home / ".dotfiles")
        # Nothing cloned, fetched, pulled, checked out or moved.
        for command in ("git clone", "pull --ff-only", "fetch origin", "checkout", "mv "):
            self.assertNotIn(command, err)
        self.assertEqual(self.git("-C", str(checkout), "rev-parse", "HEAD"), head)
        self.assertEqual((checkout / "LOCAL").read_text(), "committed\nand edited\n")
        self.assertTrue((checkout / "UNTRACKED").exists())
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.leftovers(), [])

    def test_install_in_dotfiles_installs_that_checkout_as_it_is(self):
        dotfiles = self.checkout()
        head = self.local_work(dotfiles)
        completed = self.run_wrapper(dotfiles / "install", args=self.FLAGS)
        self.assert_in_place(completed, dotfiles, head)
        self.assertIn("@leekyungmoon's", completed.stdout.decode())
        opts = self.installer_runs()[0]["opts"]
        self.assertTrue(opts["no_packages"] and opts["no_gui"] and opts["no_shell_change"])
        self.assertEqual(self.zsh_runs(), [])  # no terminal: no new shell

    def test_install_started_by_sh_zsh_or_bash_or_from_anywhere_is_in_place(self):
        # 'sh install' (dash) and 'zsh install' run it with bash; the cwd
        # never decides which checkout it is.
        zsh = shutil.which("zsh")
        starts = [("cwd", ["sh", "install"]), ("cwd", ["bash", "install"]),
                  ("case", ["sh", "{dotfiles}/install"]), ("case", ["{dotfiles}/install"])]
        if zsh:
            starts += [("cwd", [zsh, "install"]), ("case", [zsh, "{dotfiles}/install"])]
        for where, argv in starts:
            with self.subTest(argv=argv, cwd=where):
                shutil.rmtree(self.home / ".dotfiles", ignore_errors=True)
                if self.py_log.exists():
                    self.py_log.unlink()
                dotfiles = self.checkout()
                head = self.local_work(dotfiles)
                cwd = dotfiles if where == "cwd" else self.case
                command = [a.format(dotfiles=dotfiles) for a in argv] + list(self.FLAGS)
                completed = subprocess.run(command, env=self.env_for(), cwd=str(cwd),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=120, start_new_session=True)
                self.assert_in_place(completed, dotfiles, head)

    def test_install_through_symlinks_is_in_place(self):
        # ~/.dotfiles a symlink to the checkout, and a link to ./install itself
        real = self.checkout(self.case / "real-checkout")
        head = self.local_work(real)
        (self.home / ".dotfiles").symlink_to(real)
        link = self.case / "dotfiles-install"
        link.symlink_to(self.home / ".dotfiles" / "install")
        completed = self.run_wrapper(link, args=self.FLAGS)
        self.assert_in_place(completed, real, head)
        self.assertTrue((self.home / ".dotfiles").is_symlink())

    def test_install_from_a_clone_elsewhere_changes_nothing(self):
        # It says what to run for what ~/.dotfiles is, and touches nothing.
        dotfiles = self.checkout()
        head = self.local_work(dotfiles)
        elsewhere = self.checkout(self.case / "src" / "dotfiles")
        completed = self.run_wrapper(elsewhere / "install", args=self.FLAGS)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout.decode(), "")
        self.assertIn("Install the checkout at ~/.dotfiles:\n  ~/.dotfiles/install\n",
                      completed.stderr.decode())
        self.assertEqual(self.installer_runs(), [])
        self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"), head)
        self.assertEqual((dotfiles / "LOCAL").read_text(), "committed\nand edited\n")
        self.assertEqual(self.backups(), [])
        # Another checkout there (no ./install): the one-liner, which moves it aside.
        (dotfiles / "install").unlink()
        completed = self.run_wrapper(elsewhere / "install")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("moves it aside (nothing is deleted)", completed.stderr.decode())
        self.assertIn("/HEAD/etc/install | bash", completed.stderr.decode())
        self.assertTrue((dotfiles / "UNTRACKED").exists())
        # Without any ~/.dotfiles: the clone & install line; nothing is created.
        shutil.rmtree(dotfiles)
        completed = self.run_wrapper(elsewhere / "install")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("git clone --recursive https://github.com/leekyungmoon/dotfiles.git "
                      "~/.dotfiles && ~/.dotfiles/install", completed.stderr.decode())
        self.assertFalse(os.path.lexists(self.home / ".dotfiles"))
        self.assertEqual(self.installer_runs(), [])

    def test_etc_install_run_from_dotfiles_is_in_place(self):
        # The file itself under any spelling or shell, not only through
        # ./install; only the resolved file counts.
        link = self.case / "links" / "dotfiles-setup"
        link.parent.mkdir()
        zsh = shutil.which("zsh")
        starts = [("case", ["bash", "{dotfiles}/etc/install"]),
                  ("case", ["{dotfiles}/etc/install"]),
                  ("etc", ["./install"]), ("etc", ["bash", "install"]),
                  ("case", ["bash", "{dotfiles}/etc/./install"]),
                  ("case", [str(link)])]
        if zsh:
            starts.append(("case", [zsh, "{dotfiles}/etc/install"]))
        for where, argv in starts:
            with self.subTest(argv=argv, cwd=where):
                shutil.rmtree(self.home / ".dotfiles", ignore_errors=True)
                if self.py_log.exists():
                    self.py_log.unlink()
                dotfiles = self.checkout()
                head = self.local_work(dotfiles)
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(dotfiles / "etc" / "install")
                cwd = dotfiles / "etc" if where == "etc" else self.case
                command = [a.format(dotfiles=dotfiles) for a in argv] + list(self.FLAGS)
                completed = subprocess.run(command, env=self.env_for(), cwd=str(cwd),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=120, start_new_session=True)
                self.assert_in_place(completed, dotfiles, head)

    def test_bash_source_from_the_environment_is_refused(self):
        # An exported BASH_SOURCE would replace bash's own: it must not
        # decide in place, in either direction.
        dotfiles = self.checkout()
        head = self.local_work(dotfiles)
        spoof = str(dotfiles / "etc" / "install")
        for run in (lambda: self.run_piped(args=list(self.FLAGS), BASH_SOURCE=spoof),
                    lambda: self.run_wrapper(dotfiles / "install", args=self.FLAGS,
                                             BASH_SOURCE="/x")):
            completed = run()
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("BASH_SOURCE is set in the environment", completed.stderr.decode())
        self.assertEqual(self.installer_runs(), [])
        self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"), head)
        self.assertEqual(self.backups(), [])

    def test_install_in_dotfiles_in_a_terminal_ends_in_a_login_zsh(self):
        dotfiles = self.checkout()
        status, out = self.run_in_terminal(args=self.FLAGS, command=[dotfiles / "install"])
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.installer_runs()), 1)
        # install.py read the terminal; the new shell runs on it.
        self.assertEqual(self.py_log.with_suffix(".log.tty").read_text().strip(), "yes")
        self.assertEqual(self.zsh_runs(), ["zsh -l stdin-tty=yes stdout-tty=yes"])
        self.assertIn("FAKE-ZSH-STARTED", out)

    def test_help_dry_run_and_status_install_nothing_and_start_no_shell(self):
        dotfiles = self.checkout()
        # install.py's own parser decides: abbreviations and flag groups too
        for args in (["--help"], ["-fh"], ["--he"], ["--dry-run", *self.FLAGS],
                     ["--dry", *self.FLAGS], ["repair", "--dry"], ["status"]):
            with self.subTest(args=args):
                if self.zsh_log.exists():
                    self.zsh_log.unlink()
                status, out = self.run_in_terminal(args=args, command=[dotfiles / "install"])
                self.assertEqual(status, 0, out)
                self.assertNotIn("All Done!", out)
                self.assertEqual(self.zsh_runs(), [])
                if "h" in args[0]:
                    self.assertIn("usage:", out)  # install.py's own help
        # (the fake records parsed runs; help exits while parsing)
        runs = self.installer_runs()
        self.assertEqual([r["command"] for r in runs], ["install", "install", "repair", "status"])
        self.assertTrue(all(r["opts"].get("dry_run") for r in runs[:3]))

    def test_install_in_place_refuses_root(self):
        dotfiles = self.checkout()
        completed = self.run_wrapper(dotfiles / "install", args=self.FLAGS,
                                     _DOTFILES_INSTALL_SIMULATE_ROOT="1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("do not run this as root", completed.stderr.decode())
        self.assertEqual(self.installer_runs(), [])

    def test_plain_clone_gets_its_missing_submodules(self):
        # 'git clone' without --recursive: the submodules it left out (one
        # with a space in its path) are fetched before install.py, which
        # then finds them all checked out.
        env = self.git_env(self.case)
        file_ok = ["-c", "protocol.file.allow=always"]
        mirror = self.case / "with-subs.git"
        work = self.case / "with-subs"
        subprocess.run(["git", "clone", "--quiet", str(self.mirror), str(work)],
                       env=env, check=True)
        for name in ("one", "my plugin"):
            sub = self.case / f"sub-{name.replace(' ', '-')}"
            subprocess.run(["git", "init", "--quiet", str(sub)], env=env, check=True)
            (sub / "f").write_text(f"{name}\n")
            subprocess.run(["git", "-C", str(sub), "add", "f"], env=env, check=True)
            subprocess.run(["git", "-C", str(sub), "commit", "--quiet", "-m", name],
                           env=env, check=True)
            subprocess.run(["git", *file_ok, "-C", str(work), "submodule", "--quiet", "add",
                            str(sub), f"plugins/{name}"], env=env, check=True)
        subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m", "subs"],
                       env=env, check=True)
        subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(mirror)],
                       env=env, check=True)
        dotfiles = self.home / ".dotfiles"
        subprocess.run(["git", "clone", "--quiet", str(mirror), str(dotfiles)], env=env,
                       check=True)
        completed = self.run_wrapper(dotfiles / "install", args=self.FLAGS,
                                     GIT_CONFIG_COUNT="1",
                                     GIT_CONFIG_KEY_0="protocol.file.allow",
                                     GIT_CONFIG_VALUE_0="always")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual((dotfiles / "plugins" / "one" / "f").read_text(), "one\n")
        self.assertEqual((dotfiles / "plugins" / "my plugin" / "f").read_text(), "my plugin\n")
        status = self.git("-C", str(dotfiles), "submodule", "status")
        self.assertFalse([l for l in status.splitlines() if l[:1] in ("-", "+", "U")], status)
        self.assertEqual(len(self.installer_runs()), 1)

    # -- the final 'exec zsh -l' -------------------------------------------------

    def test_terminal_run_ends_in_a_login_zsh_on_the_terminal(self):
        status, out = self.run_in_terminal(args=["--no-packages", "--no-gui"])
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.installer_runs()), 1)
        self.assertEqual(self.zsh_runs(), ["zsh -l stdin-tty=yes stdout-tty=yes"])
        # "All Done!" comes first, then the new shell takes over the terminal.
        self.assertIn("All Done!", out)
        self.assertIn("FAKE-ZSH-STARTED", out)
        self.assertLess(out.index("All Done!"), out.index("FAKE-ZSH-STARTED"))
        self.assertIn("Starting a new zsh with the new setup.", out)
        self.assertNotIn("WARNING", out)
        self.assertNotRegex(out, r"exec zsh|predates|old setup|previous shell")

    def test_failed_install_never_starts_the_shell(self):
        status, out = self.run_in_terminal(FAKE_INSTALL_PY_RC="3")
        self.assertEqual(status, 3, out)
        self.assertTrue(self.py_log.with_suffix(".log.argv").exists())  # it did run
        self.assertNotIn("All Done!", out)
        self.assertEqual(self.zsh_runs(), [])

    def test_failed_clone_never_starts_the_shell(self):
        status, out = self.run_in_terminal(DOTFILES_REPO_URL=str(self.case / "missing.git"))
        self.assertNotEqual(status, 0, out)
        self.assertIn("nothing was installed", out)
        self.assertEqual(self.zsh_runs(), [])

    def test_opt_out_help_and_redirected_output_never_start_the_shell(self):
        cases = {
            "DOTFILES_EXEC_SHELL=0": {"extra": {"DOTFILES_EXEC_SHELL": "0"}},
            "--help": {"args": ["--help"]},
            "stdout redirected": {"stdout_file": self.case / "out.log"},
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.zsh_log.unlink(missing_ok=True)
                status, out = self.run_in_terminal(args=case.get("args", ()),
                                                   stdout_file=case.get("stdout_file"),
                                                   **case.get("extra", {}))
                self.assertEqual(status, 0, out)
                self.assertEqual(self.zsh_runs(), [])
        self.assertIn("All Done!", (self.case / "out.log").read_text())
        with self.subTest("terminal output but no controlling terminal"):
            self.zsh_log.unlink(missing_ok=True)
            status, out = self.run_on_terminal_without_ctty()
            self.assertEqual(status, 0, out)
            self.assertIn("All Done!", out)
            self.assertNotIn("Starting a new zsh", out)
            self.assertEqual(self.zsh_runs(), [])

    def path_without_zsh(self) -> str:
        """PATH without any zsh: the fakes, then the system's bin directories
        minus zsh. A fake getent in the fakes answers the passwd lookup."""
        (self.bin / "zsh").unlink()
        tools = self.case / "tools"
        tools.mkdir()
        for directory in ("/usr/bin", "/bin"):
            for entry in os.scandir(directory):
                if entry.name != "zsh" and not (tools / entry.name).exists():
                    os.symlink(entry.path, tools / entry.name)
        return f"{self.bin}:{tools}"

    def fake_getent(self, shell: str | None, rc: int = 0):
        """getent passwd <uid> prints a passwd line with this login shell."""
        log = self.case / "getent.log"
        line = "" if shell is None else (
            f"fixture:x:{os.getuid()}:{os.getgid()}::{self.home}:{shell}")
        getent = self.bin / "getent"
        getent.write_text(f"#!/bin/sh\necho \"getent $*\" >> '{log}'\n"
                          f"[ -n '{line}' ] && echo '{line}'\nexit {rc}\n")
        getent.chmod(0o755)
        return log

    def test_missing_zsh_falls_back_to_the_passwd_login_shell(self):
        path = self.path_without_zsh()
        login_log = self.case / "login.log"
        login = self.case / "login-shell" / "fixture-sh"
        login.parent.mkdir()
        login.write_text(FAKE_ZSH.replace("@LOG@", str(login_log)).replace("zsh $*",
                                                                           "fixture-sh $*"))
        login.chmod(0o755)
        getent_log = self.fake_getent(str(login))
        status, out = self.run_in_terminal(PATH=path)
        self.assertEqual(status, 0, out)
        self.assertIn("All Done!", out)
        self.assertEqual(getent_log.read_text().split(),
                         ["getent", "passwd", str(os.getuid())])
        self.assertEqual(login_log.read_text().splitlines(),
                         ["fixture-sh -l stdin-tty=yes stdout-tty=yes"])
        self.assertIn("Starting a new fixture-sh with the new setup.", out)
        self.assertLess(out.index("All Done!"), out.index("FAKE-ZSH-STARTED"))
        self.assertNotIn("WARNING", out)
        self.assertNotRegex(out, r"exec zsh|predates|old setup|previous shell")

    def test_missing_zsh_and_no_usable_login_shell_ends_normally(self):
        path = self.path_without_zsh()
        cases = {
            "nologin": dict(shell="/usr/sbin/nologin"),
            "missing file": dict(shell=str(self.case / "no-such-shell")),
            "relative": dict(shell="zsh"),
            "lookup fails": dict(shell=None, rc=2),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.fake_getent(**case)
                status, out = self.run_in_terminal(PATH=path)
                self.assertEqual(status, 0, out)
                self.assertIn("All Done!", out)
                self.assertNotIn("Starting a new", out)
                self.assertNotIn("WARNING", out)

    def test_failed_exec_keeps_the_exit_status(self):
        # A zsh that cannot be executed: the script warns and still exits 0.
        (self.bin / "zsh").write_bytes(b"\x7fELF\x00\x00\x00 not a binary")
        status, out = self.run_in_terminal()
        self.assertEqual(status, 0, out)
        self.assertIn("Starting a new zsh", out)
        self.assertIn(f"could not start {self.bin / 'zsh'}", out)
        # neutral: no hint at an old state or a manual step
        self.assertNotRegex(out, r"(?i)new terminal|old setup|WARNING")

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

    def test_clone_with_other_origin_is_moved_aside(self):
        other = self.case / "other.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", str(self.mirror), str(other)],
                       env=self.git_env(self.case), check=True)
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(other), str(dotfiles))
        moved = self.assert_fresh_clone(self.run_piped())
        self.assertEqual(self.git("-C", str(moved), "remote", "get-url", "origin"), str(other))

    def test_dirty_clone_of_this_repository_is_moved_aside(self):
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
        (dotfiles / "install.py").write_text("# local edit\n")
        moved = self.assert_fresh_clone(self.run_piped())
        self.assertEqual((moved / "install.py").read_text(), "# local edit\n")
        self.assertEqual(self.git("-C", str(moved), "remote", "get-url", "origin"),
                         str(self.mirror))
        # a second run finds a clean clone and moves nothing more
        completed = self.run_piped()
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(len(self.backups()), 1)

    def test_regular_file_is_moved_aside(self):
        (self.home / ".dotfiles").write_text("not a directory\n")
        moved = self.assert_fresh_clone(self.run_piped())
        self.assertTrue(moved.is_file() and not moved.is_symlink())
        self.assertEqual(moved.read_text(), "not a directory\n")

    def test_symlink_to_a_checkout_is_moved_aside_and_target_kept(self):
        target = self.case / "elsewhere"
        self.git("clone", "--quiet", str(self.mirror), str(target))
        target_head = self.git("-C", str(target), "rev-parse", "HEAD")
        (self.home / ".dotfiles").symlink_to(target)
        moved = self.assert_fresh_clone(self.run_piped())
        # the link itself was moved; the checkout it pointed to is untouched
        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(target))
        self.assertEqual(self.git("-C", str(target), "rev-parse", "HEAD"), target_head)
        self.assertEqual(self.git("-C", str(target), "status", "--porcelain"), "")

    def test_broken_symlink_is_moved_aside(self):
        missing = self.case / "gone"
        (self.home / ".dotfiles").symlink_to(missing)
        moved = self.assert_fresh_clone(self.run_piped())
        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(missing))
        self.assertFalse(moved.exists())

    def test_failed_clone_leaves_the_existing_checkout_in_place(self):
        # The live checkout with a tracked edit (a 'move' case) and a managed
        # link into it: a clone that fails must not take the checkout away.
        dotfiles = self.home / ".dotfiles"
        zshrc = self.home / ".zshrc"
        cases = {
            "missing branch": {"DOTFILES_REF": "no-such-branch"},
            "missing commit": {"DOTFILES_REF": "0123456789abcdef0123456789abcdef01234567"},
            "unreachable url": {"DOTFILES_REPO_URL": str(self.case / "missing.git")},
        }
        for name, extra in cases.items():
            with self.subTest(name):
                if not dotfiles.exists():
                    self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
                    (dotfiles / "install.py").write_text("# local edit\n")
                    zshrc.symlink_to(".dotfiles/install.py")
                completed = self.run_piped(**extra)
                err = completed.stderr.decode()
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("nothing was installed", err)
                self.assertIn(f"{dotfiles} was left as it was.", err)
                self.assertNotIn("Moved the existing", completed.stdout.decode())
                self.assertNotIn(b"All Done!", completed.stdout)
                self.assertTrue(dotfiles.is_dir())
                self.assertEqual((dotfiles / "install.py").read_text(), "# local edit\n")
                self.assertEqual(zshrc.read_text(), "# local edit\n")  # link still resolves
                self.assertEqual(self.backups(), [])
                self.assertEqual(self.leftovers(), [])
                self.assertEqual(self.installer_runs(), [])

    def test_clone_with_submodules_is_complete_after_the_rename(self):
        env = self.git_env(self.case)
        sub = self.case / "subsrc"
        work = self.case / "with-sub"
        mirror = self.case / "with-sub.git"
        git_file = ["-c", "protocol.file.allow=always"]
        subprocess.run(["git", "init", "--quiet", str(sub)], env=env, check=True)
        (sub / "f").write_text("sub\n")
        subprocess.run(["git", "-C", str(sub), "add", "f"], env=env, check=True)
        subprocess.run(["git", "-C", str(sub), "commit", "--quiet", "-m", "sub"],
                       env=env, check=True)
        subprocess.run(["git", "clone", "--quiet", str(self.mirror), str(work)],
                       env=env, check=True)
        subprocess.run(["git", *git_file, "-C", str(work), "submodule", "--quiet", "add",
                        str(sub), "plugins/sub"], env=env, check=True)
        subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m", "sub"],
                       env=env, check=True)
        subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(mirror)],
                       env=env, check=True)
        (self.home / ".dotfiles").write_text("in the way\n")
        completed = self.run_piped(DOTFILES_REPO_URL=str(mirror), GIT_CONFIG_COUNT="1",
                                   GIT_CONFIG_KEY_0="protocol.file.allow",
                                   GIT_CONFIG_VALUE_0="always")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        dotfiles = self.home / ".dotfiles"
        self.assertEqual((dotfiles / "plugins" / "sub" / "f").read_text(), "sub\n")
        self.assertEqual(Path(self.git("-C", str(dotfiles / "plugins" / "sub"), "rev-parse",
                                       "--show-toplevel")), dotfiles / "plugins" / "sub")
        self.assertEqual(self.git("-C", str(dotfiles), "status", "--porcelain"), "")
        self.assertEqual(self.leftovers(), [])

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

    def ssh_env(self) -> dict[str, str]:
        """Serve ssh URLs from the mirror; route the default https URL there too,
        so a wrongly classified checkout is re-cloned locally (and detected by
        its backup) instead of reaching the network."""
        ssh_log = self.case / "ssh.log"
        fake_ssh = self.bin / "fake-ssh"
        fake_ssh.write_text(FAKE_SSH.replace("@LOG@", str(ssh_log))
                            .replace("@MIRROR@", str(self.mirror)))
        fake_ssh.chmod(0o755)
        return {"DOTFILES_REPO_URL": None, "GIT_SSH_COMMAND": str(fake_ssh),
                "GIT_SSH_VARIANT": "simple", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{self.mirror}.insteadOf",
                "GIT_CONFIG_VALUE_0": DEFAULT_URL}

    def test_ssh_spelling_of_origin_counts_as_this_repository(self):
        dotfiles = self.home / ".dotfiles"
        new = self.push_to_mirror()
        env = self.ssh_env()
        ssh_log = self.case / "ssh.log"
        for origin in ("git@github.com:leekyungmoon/dotfiles.git",
                       "git@github.com:leekyungmoon/dotfiles",
                       "ssh://git@github.com/leekyungmoon/dotfiles.git"):
            with self.subTest(origin):
                if dotfiles.exists():
                    shutil.rmtree(dotfiles)
                self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
                self.git("-C", str(dotfiles), "reset", "--quiet", "--hard", "HEAD~1")
                self.git("-C", str(dotfiles), "remote", "set-url", "origin", origin)
                ssh_log.unlink(missing_ok=True)
                completed = self.run_piped(**env)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                self.assertEqual(self.backups(), [])
                self.assertIn("pull --ff-only", completed.stderr.decode())
                # fast-forwarded over the ssh URL, which stays the origin
                self.assertEqual(self.git("-C", str(dotfiles), "rev-parse", "HEAD"), new)
                self.assertEqual(self.git("-C", str(dotfiles), "config", "remote.origin.url"),
                                 origin)
                self.assertIn("git-upload-pack", ssh_log.read_text())
                self.assertIn("github.com", ssh_log.read_text())

    def test_ssh_origin_of_another_repository_is_moved_aside(self):
        dotfiles = self.home / ".dotfiles"
        self.git("clone", "--quiet", str(self.mirror), str(dotfiles))
        self.git("-C", str(dotfiles), "remote", "set-url", "origin",
                 "git@github.com:someone-else/dotfiles.git")
        completed = self.run_piped(**self.ssh_env())
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.git("-C", str(backups[0] / "dotfiles"), "remote", "get-url",
                                  "origin"), "git@github.com:someone-else/dotfiles.git")
        self.assertEqual(self.git("-C", str(dotfiles), "config", "remote.origin.url"),
                         DEFAULT_URL)
        self.assertEqual(self.leftovers(), [])

    def test_bad_url_stops_before_install_py(self):
        completed = self.run_piped(DOTFILES_REPO_URL=str(self.case / "missing.git"))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"git clone of", completed.stderr)
        self.assertIn(b"nothing was installed", completed.stderr)
        self.assertEqual(self.installer_runs(), [])
        self.assertFalse(self.py_log.with_suffix(".log.argv").exists())
        self.assertFalse(os.path.lexists(self.home / ".dotfiles"))
        self.assertEqual(self.leftovers(), [])
        self.assertNotIn(b"was left as it was", completed.stderr)
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
