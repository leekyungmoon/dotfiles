"""Real sources/plugins and zsh over an SSH transport double, offline receiver.

Run with SSH_ZSH_TEST_PLUGINS pointing at prepared local Antidote checkouts.
Linux seccomp denies creation of IPv4/IPv6 sockets in every receiver child.
No real account, SSH daemon, user config or system package is changed.
"""

import ctypes
import ctypes.util
import os
import errno
import pty
import select
import signal
import shlex
import time
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def deny_internet():
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]

    class Compare(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                    ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_int, ctypes.c_uint,
                                               ctypes.POINTER(Compare)]
    ctx = library.seccomp_init(0x7fff0000)  # allow except explicit rules
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        for family in (socket.AF_INET, socket.AF_INET6):
            compare = Compare(0, 4, family, 0)  # arg0 == family
            result = library.seccomp_rule_add_array(
                ctx, 0x00050000 | 101,  # errno ENETUNREACH
                library.seccomp_syscall_resolve_name(b"socket"),
                1, ctypes.byref(compare))
            if result != 0:
                raise RuntimeError(f"seccomp rule failed: {result}")
        if library.seccomp_load(ctx) != 0:
            raise RuntimeError("seccomp_load failed")
    finally:
        library.seccomp_release(ctx)


@unittest.skipUnless(os.environ.get("SSH_ZSH_TEST_PLUGINS"),
                     "set SSH_ZSH_TEST_PLUGINS for real offline plugin integration")
class OfflineIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssh offline home ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "remote account"
        self.home.mkdir()
        self.client_home = self.root / "client"
        self.client_home.mkdir()
        self.fake = self.root / "transport"
        self.fake.mkdir()
        # Execute the actual final OpenSSH command string with the remote
        # home and a kernel network filter. Preserve stdin (the real archive).
        script = f'''#!{sys.executable}
import os, sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_ssh_dotfiles import deny_internet
deny_internet()
env = dict(os.environ)
env['HOME'] = env['SSH_TEST_HOME']
os.chdir(env['HOME'])
os.execve('/bin/sh', ['/bin/sh', '-c', sys.argv[-1]], env)
'''
        (self.fake / "ssh").write_text(script)
        (self.fake / "ssh").chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.client_home),
                        SSH_TEST_HOME=str(self.home), TERM="xterm-256color",
                        PATH=str(self.fake) + ":" + os.environ["PATH"])
        self.tmux_tmp = self.root / "tmux"
        self.tmux_tmp.mkdir(mode=0o700)
        self.env["TMUX_TMPDIR"] = str(self.tmux_tmp)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        # Shared startup scripts detect accidental reads; they must not run
        # during either isolated receiver preparation or personal zsh startup.
        for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zshrc.local",
                     ".bashrc", ".gitconfig", ".tmux.conf"):
            (self.home / name).write_text("# shared sentinel\n")
        self.before = {p.name: p.read_bytes() for p in self.home.iterdir()}

    def call(self, action, input=None):
        cmd = [str(ROOT / "bin/ssh-dotfiles"), action, "fixture-host",
               "--profile", "alice"]
        if action == "install":
            cmd += ["--plugins", os.environ["SSH_ZSH_TEST_PLUGINS"]]
        result = subprocess.run(cmd, env=self.env, input=input, capture_output=True,
                                text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("can't change option", result.stderr)
        return result

    def terminal(self, commands):
        cmd = [str(ROOT / "bin/ssh-dotfiles"), "connect", "fixture-host", "--profile", "alice"]
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(cmd[0], cmd, self.env)
        output = bytearray()
        try:
            os.write(fd, commands.encode())
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        data = os.read(fd, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    output.extend(data)
            else:
                self.fail("personal terminal did not finish within 40 seconds")
            _, status = os.waitpid(pid, 0)
            pid = 0
            text = output.decode(errors="replace")
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, text)
            self.assertNotIn("gitstatus failed", text)
            self.assertNotIn("can't change option", text)
            return text
        finally:
            os.close(fd)
            if pid:
                os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_network_filter_is_inherited(self):
        result = subprocess.run([sys.executable, "-c", "import socket; socket.socket()"],
                                preexec_fn=deny_internet, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Network is unreachable", result.stderr)

    def test_real_offline_install_connect_reinstall_and_shared_home(self):
        self.call("install")
        profile = self.home / ".local/share/ssh-dotfiles/profiles/alice"
        first = (profile / "current").resolve()
        self.assertTrue((first / "antidote.bundled.zsh").is_file())
        result = self.terminal(
            'print -r -- "READY=$_ssh_zsh_ready HOME=$HOME PROFILE=$SSH_ZSH_HOME"\n'
            'print -r -- "HIST=$HISTFILE"\n'
            'whence p10k antidote fzf-cd-widget\n'
            'alias gs\n'
            'exit\n')
        self.assertIn(f"READY=1 HOME={self.home} PROFILE={profile}", result)
        self.assertIn(f"HIST={profile}/state/zsh/history", result)
        self.assertIn("fzf-cd-widget", result)
        self.assertIn("gs='git status'", result)
        terminal = self.terminal(
            'print HISTORY_PRESERVED_917\n'
            'print -r -- "HF=$HISTFILE EXPORTED=${(t)HISTFILE}"\n'
            'print -r -- "GIT_FALLBACK=$POWERLEVEL9K_DISABLE_GITSTATUS"\n'
            'sleep 3\nexit\n')
        self.assertIn("GIT_FALLBACK=true", terminal)
        self.assertIn(f"HF={profile}/state/zsh/history EXPORTED=scalar", terminal)
        history = profile / "state/zsh/history"
        self.assertIn("HISTORY_PRESERVED_917", history.read_text())
        self.call("install")
        self.assertNotEqual(first, (profile / "current").resolve())
        self.assertTrue(first.is_dir())
        self.assertIn("HISTORY_PRESERVED_917", history.read_text())
        terminal = self.terminal('fc -l -100\nexit\n')
        self.assertIn("HISTORY_PRESERVED_917", terminal)
        for name, content in self.before.items():
            self.assertEqual((self.home / name).read_bytes(), content)
        # All added home content must be inside our explicit profile tree.
        self.assertEqual(set(p.name for p in self.home.iterdir()), set(self.before) | {".local"})
        # No private Git configuration is present in the transferred tree.
        self.assertFalse(any(p.name == ".git" for p in first.rglob("*")))
        # An incomplete/corrupt installation must not silently leave a basic
        # interactive shell that looks like a successful personal connection.
        (profile / "current/antidote.bundled.zsh").unlink()
        failed = subprocess.run([str(ROOT / "bin/ssh-dotfiles"), "connect", "fixture-host",
                                 "--profile", "alice"], env=self.env,
                                input="print SHOULD_NOT_EXECUTE\nexit\n",
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("profile startup failed", failed.stderr)
        self.assertNotIn("SHOULD_NOT_EXECUTE", failed.stdout)

    def test_connect_without_install_does_not_write(self):
        result = subprocess.run([str(ROOT / "bin/ssh-dotfiles"), "connect", "fixture-host",
                                 "--profile", "missing"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not installed", result.stderr)
        self.assertEqual(set(p.name for p in self.home.iterdir()), set(self.before))

    @unittest.skipUnless(shutil.which("tmux"), "tmux required for socket isolation test")
    def test_personal_tmux_does_not_seed_the_shared_server(self):
        self.call("install")
        real_tmux = shutil.which("tmux")
        clean = {"HOME": str(self.home), "PATH": os.environ["PATH"],
                 "TERM": "xterm-256color", "TMUX_TMPDIR": str(self.tmux_tmp)}

        def tmux(socket_name, *args):
            return subprocess.run([real_tmux, "-L", socket_name, *args], env=clean,
                                  capture_output=True, text=True, timeout=10)

        try:
            self.terminal('tmux new-session -d -s personal\nexit\n')
            private = tmux("ssh-dotfiles-alice", "show-environment", "-g", "SSH_ZSH_HOME")
            self.assertEqual(private.returncode, 0, private.stderr)
            self.assertIn("profiles/alice", private.stdout)
            self.assertNotEqual(tmux("default", "list-sessions").returncode, 0)
            started = tmux("default", "-f", "/dev/null", "new-session", "-d", "-s", "common")
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertNotEqual(tmux("default", "show-environment", "-g", "SSH_ZSH_HOME").returncode, 0)
            output = self.root / "public-pane-env"
            probe = tmux("default", "new-window", "-d", "env > " + shlex.quote(str(output)) + "; sleep 10")
            self.assertEqual(probe.returncode, 0, probe.stderr)
            for _ in range(100):
                if output.exists() and output.stat().st_size:
                    break
                time.sleep(.05)
            self.assertTrue(output.exists())
            self.assertNotIn("SSH_ZSH_HOME=", output.read_text())
            self.assertNotIn("ZDOTDIR=", output.read_text())
        finally:
            for name in ("ssh-dotfiles-alice", "default"):
                tmux(name, "kill-server")


if __name__ == "__main__":
    unittest.main()
