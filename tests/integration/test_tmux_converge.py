"""A tmux server that predates the install must become the new setup.

After install, repair or update, the user's running tmux server must be
configured exactly like a freshly started server with the new ~/.tmux.conf,
its sessions, windows, panes and busy programs untouched. When the
generation changed, every idle interactive zsh WITHOUT the reload hook (an
upstream-style shell) is respawned with the new shell setup; a hooked zsh
(it holds an fd on {state}/personal-dotfiles/shell-hook and reloads itself),
a bash/sh pane and anything not idle are never respawned.

Isolation: every server here lives in a private TMUX_TMPDIR under a fresh
``/tmp/pdfcv*`` directory and is addressed and killed only through its socket
there; TMUX/TMUX_PANE never reach a child, and the environment is rebuilt from
scratch with a temporary HOME/XDG tree. :class:`IsolatedRunner` refuses any
tmux call of the code under test that could reach another server. The
plugins' ``systemctl`` and continuum's ``ps -u`` view are fakes on PATH, so no
user manager and no other tmux server is touched or counted. The pinned
plugins (manifests/tmux-plugins.json) are cloned from GitHub once per class;
the tests skip when that is impossible.

Hooked shells source the repository's zsh/zsh.d/dotfiles-reload.zsh (the
working tree's) when it implements the shell-hook contract; until then they
source HOOK_STUB, which holds the fd exactly as the contract says (read-only,
close-on-exec, via zsh/system sysopen). HOOK_SOURCE says which one ran;
PD_TEST_HOOK_SOURCE=stub or module forces one.

TMUX_TEST_BINARY selects the tmux to test (default /usr/bin/tmux).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from installer import phases  # noqa: E402
from installer.platform import Target  # noqa: E402
from installer.runner import Runner  # noqa: E402

TMUX = os.environ.get("TMUX_TEST_BINARY") or shutil.which("tmux", path="/usr/bin:/bin")
ZSH = shutil.which("zsh", path="/usr/bin:/bin")
UPSTREAM_REV = "097d88d8"  # wookayin/dotfiles' tmux.conf before this repository
ZSH_SETTINGS = REPO_ROOT / "zsh" / "zsh.d" / "zsh_custom_settings.zsh"
RELOAD_MODULE = REPO_ROOT / "zsh" / "zsh.d" / "dotfiles-reload.zsh"
# zsh/zshrc's last line: the reload module puts a reloaded shell's session back.
STARTUP_CALL = "(( ! ${+functions[_pd_reload_startup]} )) || _pd_reload_startup\n"
BASH = shutil.which("bash", path="/usr/bin:/bin")

FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/systemctl.log"
exit 0
"""
# continuum counts `ps -u` lines starting with "tmux" as other servers; show
# it none, so the user's real tmux server never changes what it decides.
FAKE_PS = """#!/bin/sh
case " $* " in
    *" -u "*) echo "COMMAND PID"; exit 0 ;;
esac
exec /bin/ps "$@"
"""
# The shell-hook contract, for as long as the repository's module does not
# implement it yet: one read-only, close-on-exec fd on the static file.
HOOK_STUB = r"""
zmodload zsh/system zsh/files
() {
  local dir=${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles
  [[ -d $dir ]] || mkdir -p -m 700 -- $dir
  [[ -e $dir/shell-hook ]] || { : >| $dir/shell-hook; chmod 600 -- $dir/shell-hook }
  typeset -gi _pd_hook_fd
  sysopen -r -o cloexec -u _pd_hook_fd -- $dir/shell-hook
}
"""
try:
    _MODULE_TEXT = RELOAD_MODULE.read_text(encoding="utf-8")
except OSError:
    _MODULE_TEXT = ""
# PD_TEST_HOOK_SOURCE=stub|module forces one (module: the test then fails
# if the module does not hold the fd).
HOOK_SOURCE = (os.environ.get("PD_TEST_HOOK_SOURCE")
               or ("module" if "shell-hook" in _MODULE_TEXT else "stub"))
# Each zsh records which config it started with, in ONE write per record
# (shells starting at the same moment never interleave): pid and `alias td`.
SHELL_LOG_LINE = ('print -r -- "$$ ${$(alias td 2>/dev/null):-missing}"'
                  ' >> "$HOME/shells.log"\n')
# An upstream-style zsh: no reload hook.
OLD_ZSHRC = "PS1='old%# '\n" + SHELL_LOG_LINE + "print -r -- OLD-SCREEN-MARK\n"
TD = "td='tmux detach'"
STATUSBAR_SESSION_RE = re.compile(r"((?:statusbar\.tmux component-[a-z]+|tmux-agent-update)) -S [^)]*\)")
# Scripts waiting for an answer (round-1 and round-2 verifier reproductions).
BASH_C_COMMAND = """bash -c 'read -p "continue? " x; echo got $x; sleep 999'"""
SCRIPT_TEXT = 'read -p "deploy? [y/N] " answer\necho "answer=$answer"\nsleep 999\n'
NO_SHEBANG_TEXT = 'read -p "NS? " x\necho "ns=$x"\nsleep 999\n'
SOURCED_TEXT = 'read -p "SRC? " x\necho "src=$x"\n'
ZSH_SOURCED_TEXT = 'read "x?ZSRC? "\nprint -r -- "zsrc=$x"\n'
ROS_EXPORTS = ("export ROS_DISTRO=humble AMENT_PREFIX_PATH=/opt/ws/install "
               "COLCON_PREFIX_PATH=/opt/ws/install CMAKE_PREFIX_PATH=/opt/ws/install")


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class IsolatedRunner(Runner):
    """The real runner, refusing tmux calls that could reach another server."""

    def __init__(self, root: Path):
        self.root = str(root)
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if Path(argv[0]).name == "tmux" and argv[1:] != ["-V"]:
            if env is None or "TMUX" in env or "TMUX_PANE" in env:
                raise AssertionError(f"tmux call without an isolated env: {argv}")
            tmpdir = env.get("TMUX_TMPDIR", "")
            private = tmpdir.startswith(self.root + "/")
            probe = (os.path.basename(tmpdir).startswith("pdfs-") and "-L" in argv
                     and os.path.isdir(tmpdir) and tmpdir.startswith(tempfile.gettempdir()))
            if not (private or probe) or "-S" in argv:
                raise AssertionError(f"tmux call outside the test servers: {argv} {tmpdir}")
        return super().run(argv, timeout=timeout, check=check, env=env, input=input,
                           cwd=cwd, read_only=read_only)

    def which(self, name):
        return TMUX if name == "tmux" else shutil.which(name)


@unittest.skipUnless(TMUX and ZSH and BASH, "tmux, zsh and bash are required")
class ConvergeRunningTmuxTests(unittest.TestCase):
    plugin_dir: Path
    upstream_conf: str

    @classmethod
    def setUpClass(cls):
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="pdfcv-plugins-")
        cls.plugin_dir = Path(cls._class_tmp.name)
        git_env = {"PATH": "/usr/bin:/bin", "HOME": cls._class_tmp.name,
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_TERMINAL_PROMPT": "0"}
        try:
            cls.upstream_conf = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", f"{UPSTREAM_REV}:tmux/tmux.conf"],
                env=git_env, check=True, capture_output=True, text=True).stdout
        except subprocess.CalledProcessError as exc:
            cls._class_tmp.cleanup()
            raise unittest.SkipTest(f"upstream tmux.conf {UPSTREAM_REV} unavailable: {exc}")
        plugins = phases.load_tmux_plugins(REPO_ROOT)
        for plugin in plugins:
            dest = cls.plugin_dir / plugin["name"]
            try:
                subprocess.run(["git", "clone", "--quiet", "--no-checkout", plugin["url"],
                                str(dest)], env=git_env, check=True, capture_output=True,
                               timeout=180)
                subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--detach",
                                plugin["commit"]], env=git_env, check=True,
                               capture_output=True, timeout=60)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                cls._class_tmp.cleanup()
                raise unittest.SkipTest(f"cannot clone pinned {plugin['name']}: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls._class_tmp.cleanup()

    # -- fixture -----------------------------------------------------------------

    def setUp(self):
        # Short paths: a unix socket path must fit in 107 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="pdfcv", dir="/tmp"))
        self.sockets: list[Path] = []
        self.home = self.root / "h"
        self.data = self.root / "d"
        self.config = self.root / "c"
        self.bin = self.root / "b"
        self.work = self.root / "w"
        for directory in (self.home, self.data / "tmux", self.config, self.bin):
            directory.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma", "delta"):
            (self.work / name).mkdir(parents=True)
        # ~/.dotfiles: a private copy of the repository's tmux files (nothing
        # here may write into the repository) plus its bin/ for the bindings.
        dotfiles = self.home / ".dotfiles"
        shutil.copytree(REPO_ROOT / "tmux", dotfiles / "tmux", symlinks=True,
                        ignore=shutil.ignore_patterns("plugins"))
        (dotfiles / "bin").symlink_to(REPO_ROOT / "bin")
        (self.home / ".tmux").symlink_to(dotfiles / "tmux")
        # Plugins where upstream's TPM keeps them and where the new config does.
        for location in (dotfiles / "tmux" / "plugins", self.data / "tmux" / "plugins"):
            location.mkdir(parents=True)
            for plugin in self.plugin_dir.iterdir():
                (location / plugin.name).symlink_to(plugin)
        (self.data / "tmux" / "resurrect").mkdir(mode=0o700)
        self.upstream = self.home / "upstream" / "tmux.conf"
        self.upstream.parent.mkdir()
        self.upstream.write_text(self.upstream_conf)
        self.new_conf = dotfiles / "tmux" / "tmux.conf"
        (self.bin / "tmux").symlink_to(TMUX)
        write_executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        write_executable(self.bin / "ps", FAKE_PS)
        self.target = Target(uid=os.getuid(), gid=os.getgid(), username="fixture",
                             home=self.home, data_home=self.data,
                             state_home=self.root / "s", config_home=self.config,
                             cache_home=self.root / "k")
        # The reload hook every hooked zsh holds an fd on.
        self.hook_path = phases.shell_hook_path(self.target)
        self.hook_rc = self.root / "hook.zsh"
        self.hook_rc.write_text(f"source {RELOAD_MODULE}\n" if HOOK_SOURCE == "module"
                                else HOOK_STUB)
        # A zsh of an earlier install of this repository: hooked, without td.
        # Both end as zsh/zshrc does, with the reload module's startup call.
        self.hooked_old_zshrc = (f"source {self.hook_rc}\n"
                                 "PS1='hooked%# '\n" + SHELL_LOG_LINE + STARTUP_CALL)
        # The new setup: the repository's settings (td) and the reload hook.
        self.new_zshrc = (f"source {ZSH_SETTINGS} >/dev/null 2>&1\n"
                          f"source {self.hook_rc}\n"
                          "PS1='new%# '\n" + SHELL_LOG_LINE + STARTUP_CALL)
        # An upstream ZDOTDIR, for unhooked shells started after the install.
        self.old_zdotdir = self.root / "z"
        self.old_zdotdir.mkdir()
        (self.old_zdotdir / ".zshrc").write_text(OLD_ZSHRC)
        self.tmpdir = self.private_tmpdir("t")
        self.socket = self.tmpdir / f"tmux-{os.getuid()}" / "default"
        # Built from scratch: nothing of the live session (TMUX, DISPLAY,
        # DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR, ...) reaches a server.
        self.base_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
            "LANG": "C.UTF-8", "TERM": "xterm-256color", "SHELL": ZSH,
            "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.root / "s"), "XDG_CACHE_HOME": str(self.root / "k"),
        }
        self.env = dict(self.base_env, TMUX_TMPDIR=str(self.tmpdir))
        self.runner = IsolatedRunner(self.root)

    def tearDown(self):
        for socket in self.sockets:
            if socket.exists() and str(socket).startswith(str(self.root) + "/"):
                subprocess.run([TMUX, "-S", str(socket), "kill-server"],
                               env=self.base_env, capture_output=True, timeout=30)
        time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)

    def private_tmpdir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(mode=0o700)
        (path / f"tmux-{os.getuid()}").mkdir(mode=0o700)
        self.sockets.append(path / f"tmux-{os.getuid()}" / "default")
        return path

    def link_conf(self, path: Path):
        conf = self.home / ".tmux.conf"
        if os.path.lexists(conf):
            conf.unlink()
        conf.symlink_to(path)

    def install_new_setup(self):
        """What the install changes on disk: ~/.tmux.conf and ~/.zshrc."""
        self.link_conf(self.new_conf)
        (self.home / ".zshrc").write_text(self.new_zshrc)

    def tmux(self, *args, socket=None, check=True):
        socket = socket or self.socket
        done = subprocess.run([TMUX, "-S", str(socket), *map(str, args)], env=self.base_env,
                              capture_output=True, text=True, timeout=120)
        if check and done.returncode != 0:
            self.fail(f"tmux {args}: {done.stderr}")
        return done.stdout.rstrip("\n")

    def start(self, *args, socket=None, conf=None, tmpdir=None):
        """Start a server the way a login does (no -f) on a private socket."""
        socket = socket or self.socket
        env = dict(self.base_env, TMUX_TMPDIR=str(tmpdir or self.tmpdir))
        argv = [TMUX, "-S", str(socket)] + (["-f", str(conf)] if conf else []) + list(args)
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)

    generation = "gen-new"
    previous = "gen-old"

    def converge(self, env=None, generation="", previous=""):
        """What the post actions run: the generation just written and the
        one recorded before it (default: gen-old -> gen-new)."""
        return phases.converge_running_tmux(
            self.target, self.runner, env or self.env,
            generation=self.generation if generation == "" else generation,
            previous_generation=self.previous if previous == "" else previous)

    def write_generation(self, generation: str):
        """What install.py writes at the end of the post actions."""
        install.write_generation(self.target, generation)

    def hooked(self, pid: int):
        return phases.shell_hooked(pid, self.hook_path)

    def wait_hooked(self, pane_id) -> int:
        """The pane's zsh has started (logged) and holds the hook fd."""
        def check():
            pid = self.pane_pid(pane_id)
            return pid if pid in self.shells() and self.hooked(pid) else None
        return self.wait_until(check, f"the hooked shell of {pane_id}")

    def screen(self, pane_id) -> str:
        return self.tmux("capture-pane", "-p", "-t", pane_id)

    def wait_screen(self, pane_id, text, what=None) -> None:
        self.wait_until(lambda: text in self.screen(pane_id), what or f"{text!r} in {pane_id}")

    def wait_line(self, pane_id, line) -> None:
        """An output line exactly ``line`` (not the echoed input)."""
        self.wait_until(lambda: line in self.tmux("capture-pane", "-p", "-J", "-S", "-",
                                                  "-t", pane_id).splitlines(),
                        f"line {line!r} in {pane_id}")

    def wait_until(self, predicate, what, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def shells(self) -> dict[int, str]:
        log = self.home / "shells.log"
        out = {}
        for line in (log.read_text().splitlines() if log.exists() else []):
            pid, _, rest = line.partition(" ")
            out[int(pid)] = rest
        return out

    def pane(self, pane_id, fmt):
        return self.tmux("display-message", "-p", "-t", pane_id, fmt)

    def pane_pid(self, pane_id) -> int:
        return int(self.pane(pane_id, "#{pane_pid}"))

    def layout(self, socket=None):
        return self.tmux("list-panes", "-a", "-F",
                         "#{session_name}:#{window_index}:#{window_name}:#{pane_id}",
                         socket=socket).splitlines()

    def children(self, pid: int) -> list[int]:
        out = []
        for entry in os.listdir("/proc"):
            stat = phases._proc_stat(int(entry)) if entry.isdigit() else None
            if stat is not None and stat[1] == pid:
                out.append(int(entry))
        return out

    def config_state(self, socket=None) -> dict:
        """What 'configured exactly like a fresh server' compares.

        Runtime-only values are left out: phases.RUNTIME_USER_OPTIONS (the
        restore marker and continuum's save time) and the global environment
        (TPM's TMUX_PLUGIN_MANAGER_PATH, the session's DISPLAY, ...). One
        load-context value is normalized: tmux/statusbar.tmux writes the
        session it runs in ('display-message -p #S') into status-right, which
        is empty while a server runs its startup config and the current
        session on every later load (prefix r, or this converge).
        """
        state = {"keys": self.tmux("list-keys", socket=socket),
                 "notes": self.tmux("list-keys", "-N", socket=socket)}
        for flag in ("-g", "-gw", "-s"):
            lines = self.tmux("show-options", flag, socket=socket).splitlines()
            state[f"options {flag}"] = [
                STATUSBAR_SESSION_RE.sub(r"\1 -S )", line) for line in lines
                if line.split(" ", 1)[0] not in phases.RUNTIME_USER_OPTIONS]
        for flag in ("-g", "-gw"):
            state[f"hooks {flag}"] = self.tmux("show-hooks", flag, socket=socket)
        return state

    def assert_same_config(self, converged: dict, fresh: dict):
        differences = {}
        for key in sorted(set(converged) | set(fresh)):
            mine = converged.get(key)
            theirs = fresh.get(key)
            mine = mine.splitlines() if isinstance(mine, str) else list(mine or [])
            theirs = theirs.splitlines() if isinstance(theirs, str) else list(theirs or [])
            if mine != theirs:
                differences[key] = {
                    "only converged": [line for line in mine if line not in theirs],
                    "only fresh": [line for line in theirs if line not in mine],
                    "same lines, other order": sorted(mine) == sorted(theirs)}
        self.assertEqual(differences, {}, json.dumps(differences, indent=1))

    def new_pane(self, directory, *command, window=True) -> str:
        new = ("-d", "-P", "-F", "#{pane_id}")
        if window:
            return self.tmux("new-window", *new, "-t", "work:", "-c", directory, *command)
        return self.tmux("split-window", *new, "-t", "work:1", "-c", directory, *command)

    def wait_shell(self, pane_id) -> int:
        return self.wait_until(lambda: (self.pane_pid(pane_id) in self.shells()
                                        and self.pane_pid(pane_id)), f"the shell of {pane_id}")

    def server_with_every_kind_of_pane(self) -> dict[str, str]:
        """An upstream-configured server holding every kind of pane.

        Respawned by a converge with a changed generation: only
        upstream_idle, an unhooked zsh idle at its prompt. Everything else
        stays: busy, job, copy-mode
        and command panes; bash panes (idle, bash -c and bash x.sh at read,
        'exec ./noshebang.sh' and 'source x.sh' at read); hooked zsh shells
        idle, inside 'read', at a PS2 continuation, in a heredoc, at a
        spelling-correction query, inside a sourced script's 'read', with a
        ROS-style exported overlay; a hooked zsh that ran 'exec bash' and one
        that exec'd a script without a shebang (now sh). A grouped session
        shows every pane a second time in list-panes -a.
        """
        self.write_generation(self.previous)
        self.link_conf(self.upstream)
        (self.home / ".zshrc").write_text(OLD_ZSHRC)
        self.start("new-session", "-d", "-s", "work", "-x", "120", "-y", "30",
                   "-c", self.work / "alpha")
        self.assertIn(str(self.upstream), self.tmux("display-message", "-p",
                                                     "#{config_files}"))
        script = self.work / "deploy.sh"
        script.write_text(SCRIPT_TEXT)
        no_shebang = self.work / "noshebang.sh"
        write_executable(no_shebang, NO_SHEBANG_TEXT)
        (self.work / "sourced.sh").write_text(SOURCED_TEXT)
        (self.work / "zsourced.zsh").write_text(ZSH_SOURCED_TEXT)
        w = self.work
        panes = {"upstream_idle": self.tmux("list-panes", "-t", "work", "-F", "#{pane_id}")}
        panes["foreground"] = self.new_pane(w / "beta", window=False)
        panes["job"] = self.new_pane(w / "gamma")
        panes["mode"] = self.new_pane(w / "delta")
        panes["command"] = self.new_pane(w / "beta", "sleep 302")
        panes["bash_c"] = self.new_pane(w / "gamma", BASH_C_COMMAND)
        panes["bash_script"] = self.new_pane(w / "delta", BASH, str(script))
        for name in ("bash_idle", "bash_exec_ns", "bash_source"):
            panes[name] = self.new_pane(w, BASH, "--norc", "-i")
        for name in ("upstream_idle", "foreground", "job", "mode"):
            self.wait_shell(panes[name])
        # Shells of an earlier install of this repository: the reload hook.
        (self.home / ".zshrc").write_text(self.hooked_old_zshrc)
        hooked = ("hooked_idle", "hooked_read", "hooked_ps2", "hooked_heredoc",
                  "hooked_correct", "hooked_source", "hooked_ros", "hooked_execbash",
                  "hooked_exec_ns")
        for name in hooked:
            panes[name] = self.new_pane(w)
        for name in hooked:
            self.wait_hooked(panes[name])

        send = self.tmux
        send("send-keys", "-t", panes["foreground"], "sleep 300", "Enter")
        send("send-keys", "-t", panes["job"], "sleep 301 &", "Enter")
        send("copy-mode", "-t", panes["mode"])
        for name in ("bash_idle", "bash_exec_ns", "bash_source"):
            self.wait_screen(panes[name], "$", f"{name}'s prompt")
        send("send-keys", "-t", panes["bash_exec_ns"], "exec ./noshebang.sh", "Enter")
        send("send-keys", "-t", panes["bash_source"], "source ./sourced.sh", "Enter")
        send("send-keys", "-t", panes["hooked_read"],
             "read answer; print -r -- READ-$answer", "Enter")
        send("send-keys", "-t", panes["hooked_ps2"], "for i in 1 2; do", "Enter")
        send("send-keys", "-t", panes["hooked_heredoc"], "sed s/^/OUT-/ <<EOF", "Enter")
        send("send-keys", "-t", panes["hooked_heredoc"], "heredoc-line1", "Enter")
        send("send-keys", "-t", panes["hooked_correct"], "setopt correct", "Enter")
        send("send-keys", "-t", panes["hooked_correct"], "ehco CORRECTED", "Enter")
        send("send-keys", "-t", panes["hooked_source"], "source ./zsourced.zsh", "Enter")
        send("send-keys", "-t", panes["hooked_ros"], ROS_EXPORTS, "Enter")
        send("send-keys", "-t", panes["hooked_execbash"], "exec bash --norc -i", "Enter")
        send("send-keys", "-t", panes["hooked_exec_ns"], "exec ./noshebang.sh", "Enter")

        self.wait_until(lambda: self.pane(panes["foreground"], "#{pane_current_command}")
                        == "sleep", "the foreground command")
        self.wait_until(lambda: self.children(self.pane_pid(panes["job"])), "the job")
        waits = (("bash_c", "continue?"), ("bash_script", "deploy? [y/N]"),
                 ("bash_exec_ns", "NS?"), ("bash_source", "SRC?"),
                 ("hooked_exec_ns", "NS?"), ("hooked_ps2", "for>"),
                 ("hooked_heredoc", "heredoc>"), ("hooked_correct", "[nyae]?"),
                 ("hooked_source", "ZSRC?"))
        for name, prompt in waits:
            self.wait_screen(panes[name], prompt, f"{name}'s prompt")
        self.wait_until(lambda: phases._proc_stat(self.pane_pid(panes["hooked_execbash"]))[0]
                        == "bash", "exec bash")
        # Waiting in a builtin or the line editor itself: no child process.
        for name in ("bash_c", "bash_script", "bash_exec_ns", "bash_source",
                     "hooked_read", "hooked_ps2", "hooked_heredoc", "hooked_correct",
                     "hooked_source", "hooked_exec_ns"):
            self.assertEqual(self.children(self.pane_pid(panes[name])), [], name)
        for name in ("bash_c", "bash_script", "bash_exec_ns", "bash_source"):
            self.assertEqual(phases._proc_stat(self.pane_pid(panes[name]))[0], "bash", name)
        # The fd closes on exec: neither the bash nor the sh holds it.
        for name in ("hooked_execbash", "hooked_exec_ns"):
            self.assertIs(self.hooked(self.pane_pid(panes[name])), False, name)
        for name in hooked[:7]:
            self.assertTrue(self.hooked(self.pane_pid(panes[name])), name)
        for name in ("upstream_idle", "foreground", "job", "mode"):
            self.assertEqual(self.shells()[self.pane_pid(panes[name])], "missing")
            self.assertIs(self.hooked(self.pane_pid(panes[name])), False, name)
        self.assertEqual(self.pane(panes["upstream_idle"], "#{pane_in_mode}"), "0")
        # A grouped session: list-panes -a names every pane twice.
        self.tmux("new-session", "-d", "-t", "work", "-s", "grouped")
        listed = self.tmux("list-panes", "-a", "-F", "#{pane_id}").splitlines()
        self.assertEqual(len(listed), 2 * len(set(listed)))
        return panes

    # -- tests -------------------------------------------------------------------

    def test_running_server_becomes_a_fresh_server_and_only_unhooked_idle_zsh_respawns(self):
        panes = self.server_with_every_kind_of_pane()
        restarted = ("upstream_idle",)
        kept = [name for name in panes if name not in restarted]
        old_pid = self.pane_pid(panes["upstream_idle"])
        # Upstream: prefix s picks a session, prefix v is not a split.
        self.assertIn("tmux-attach", self.tmux("list-keys", "-T", "prefix", "s"))
        self.assertNotIn("split-window", self.tmux("list-keys", "-T", "prefix", "v",
                                                   check=False))
        # Runtime state of this server (resurrect-save's restore marker and
        # continuum's last save) survives; upstream-only options do not.
        self.tmux("set-option", "-g", "@tmux-restore-complete", "on")
        self.tmux("set-option", "-g", "@continuum-save-last-timestamp", "1234567890")
        self.assertEqual(self.tmux("show-options", "-gqv", "@copycat_next"), "N")

        # The install: new files, then (end of the post actions) the new
        # generation; a shell started after it is hooked and on it.
        self.install_new_setup()
        self.write_generation(self.generation)
        panes["hooked_current"] = self.new_pane(self.work / "alpha")
        self.wait_hooked(panes["hooked_current"])
        kept.append("hooked_current")
        kept_pids = {name: self.pane_pid(panes[name]) for name in kept}
        kept_children = {name: self.children(pid) for name, pid in kept_pids.items()}
        layout = self.layout()
        # A fresh server of the new config, started like a login starts one.
        fresh_tmpdir = self.private_tmpdir("t2")
        fresh = fresh_tmpdir / f"tmux-{os.getuid()}" / "default"
        self.start("new-session", "-d", "-s", "fresh", "-c", self.work / "alpha",
                   socket=fresh, tmpdir=fresh_tmpdir)

        reasons, details = self.converge()
        self.assertEqual((reasons, details["outcome"]), ([], "converged"))
        self.assertEqual(details["respawn"], "done")
        self.assertEqual(details["respawned_panes"], 1, details)
        # busy: foreground, job, mode. not-zsh: command, bash_c, bash_script,
        # bash_idle, bash_exec_ns, bash_source, hooked_execbash, hooked_exec_ns.
        # hooked: idle, read, ps2, heredoc, correct, source, ros, current.
        self.assertEqual(details["kept"], {"busy": 3, "hooked": 8, "not-zsh": 8}, details)
        self.assertEqual(details["busy_panes"], 19)
        self.assertEqual(phases.converge_summary(details),
                         "tmux: applied the new config; restarted 1 idle shell")
        (entry,) = details["restarted"]
        self.assertEqual(entry["pane_id"], panes["upstream_idle"])
        location = self.pane(panes["upstream_idle"], "#{window_index}.#{pane_index}")
        self.assertRegex(entry["pane"], rf"^(work|grouped):{re.escape(location)}$")
        self.assertEqual(entry["directory"], str(self.work / "alpha"))
        # Sessions, windows, panes and everything not respawned: untouched.
        self.assertEqual(self.layout(), layout)
        for name in kept:
            self.assertEqual(self.pane_pid(panes[name]), kept_pids[name], name)
            self.assertEqual(self.children(kept_pids[name]), kept_children[name], name)
        self.assertEqual(self.pane(panes["mode"], "#{pane_in_mode}"), "1")
        # The respawned shell: a new process, same directory, the new setup.
        new_pid = self.wait_hooked(panes["upstream_idle"])
        self.assertNotEqual(new_pid, old_pid)
        self.assertFalse(os.path.exists(f"/proc/{old_pid}"))
        self.assertEqual(self.shells()[new_pid], TD)
        self.assertEqual(os.readlink(f"/proc/{new_pid}/cwd"), str(self.work / "alpha"))
        self.assertEqual(self.pane(panes["upstream_idle"], "#{pane_current_path}"),
                         str(self.work / "alpha"))
        # The td alias works in the new shell (typed, not only defined).
        self.tmux("send-keys", "-t", panes["upstream_idle"], "whence -w td", "Enter")
        self.wait_screen(panes["upstream_idle"], "td: alias", "td in the new shell")
        # What the old shell showed is kept in the history.
        history = self.tmux("capture-pane", "-p", "-S", "-", "-t", panes["upstream_idle"])
        self.assertIn("OLD-SCREEN-MARK", history)
        # Configured exactly as the fresh server.
        self.assert_same_config(self.config_state(), self.config_state(fresh))
        self.assertIn("split-window -v", self.tmux("list-keys", "-T", "prefix", "s"))
        self.assertIn("split-window -h", self.tmux("list-keys", "-T", "prefix", "v"))
        self.assertEqual(self.tmux("show-options", "-gqv", "@tmux-restore-complete"), "on")
        self.assertEqual(self.tmux("show-options", "-gqv", "@continuum-save-last-timestamp"),
                         "1234567890")

        # Converging twice more with the generation unchanged respawns
        # nothing, not even an unhooked zsh started since (upstream ZDOTDIR).
        panes["late_unhooked"] = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}",
                                           "-t", "work:", "-c", self.work / "beta",
                                           "-e", f"ZDOTDIR={self.old_zdotdir}")
        late = self.wait_shell(panes["late_unhooked"])
        self.assertIs(self.hooked(late), False)
        pids = {name: self.pane_pid(pane) for name, pane in panes.items()}
        layout = self.layout()
        for _ in range(2):
            reasons, details = self.converge(previous=self.generation)
            self.assertEqual((reasons, details["outcome"]), ([], "converged"))
            self.assertEqual(details["respawn"], "generation-unchanged")
            self.assertEqual((details["respawned_panes"], details["restarted"]), (0, []))
            self.assertEqual(phases.converge_summary(details), "tmux: applied the new config")
            self.assertEqual({name: self.pane_pid(pane) for name, pane in panes.items()},
                             pids)
            self.assertEqual(self.layout(), layout)
            self.assert_same_config(self.config_state(), self.config_state(fresh))

        # Every pending input is still there, and completes.
        answers = (("bash_c", "yes", "got yes"), ("bash_script", "y", "answer=y"),
                   ("bash_exec_ns", "y", "ns=y"), ("bash_source", "y", "src=y"),
                   ("hooked_exec_ns", "z", "ns=z"),
                   ("hooked_read", "typed-answer", "READ-typed-answer"),
                   ("hooked_ps2", "print -r -- loop-$i; done", "loop-2"),
                   ("hooked_heredoc", "EOF", "OUT-heredoc-line1"),
                   ("hooked_correct", "y", "CORRECTED"),
                   ("hooked_source", "w", "zsrc=w"))
        for name, keys, line in answers:
            self.tmux("send-keys", "-t", panes[name], keys, "Enter")
        for name, _, line in answers:
            self.wait_line(panes[name], line)
        self.wait_line(panes["hooked_ps2"], "loop-1")
        # The ROS-style overlay is still exported in its shell.
        self.tmux("send-keys", "-t", panes["hooked_ros"],
                  "print -r -- ROS=$ROS_DISTRO:$AMENT_PREFIX_PATH:$COLCON_PREFIX_PATH", "Enter")
        self.wait_line(panes["hooked_ros"], "ROS=humble:/opt/ws/install:/opt/ws/install")
        for name in ("hooked_read", "hooked_ps2", "hooked_heredoc", "hooked_correct",
                     "hooked_source", "hooked_ros", "hooked_idle"):
            self.assertEqual(self.pane_pid(panes[name]), pids[name], name)
        # The plugins' systemctl calls went to the fake only.
        self.assertTrue((self.home / "systemctl.log").exists())

    def test_continuum_does_not_restore_into_the_converged_server(self):
        # A snapshot holding a session "saved", made by a server of the new config.
        self.install_new_setup()
        maker_tmpdir = self.private_tmpdir("t3")
        maker = maker_tmpdir / f"tmux-{os.getuid()}" / "default"
        self.start("new-session", "-d", "-s", "saved", "-c", self.work / "beta",
                   socket=maker, tmpdir=maker_tmpdir)
        time.sleep(phases.CONTINUUM_RESTORE_MAX_DELAY / 5)
        saved = subprocess.run([self.home / ".tmux" / "resurrect-save"],
                               env=dict(self.base_env, TMUX_TMPDIR=str(maker_tmpdir),
                                        TMUX=f"{maker},0,0"),
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.tmux("kill-server", socket=maker)
        self.assertTrue((self.data / "tmux" / "resurrect" / "last").exists())

        # Control: a fresh server of the new config does restore it.
        control_tmpdir = self.private_tmpdir("t4")
        control = control_tmpdir / f"tmux-{os.getuid()}" / "default"
        self.start("new-session", "-d", "-s", "login", socket=control,
                   tmpdir=control_tmpdir)
        self.wait_until(lambda: "saved" in self.tmux("list-sessions", "-F",
                                                     "#{session_name}", socket=control),
                        "continuum's restore into a fresh server")
        self.tmux("kill-server", socket=control)

        # The upstream server was started moments ago (inside continuum's
        # window); converging waits it out, so nothing is restored into it.
        self.link_conf(self.upstream)
        self.start("new-session", "-d", "-s", "work", "-c", self.work / "alpha")
        self.install_new_setup()
        layout = self.layout()
        reasons, details = self.converge()
        self.assertEqual(details["outcome"], "converged", reasons)
        self.assertGreater(details.get("waited_for_continuum_seconds", 0), 0)
        self.assertGreater(int(time.time()) - int(self.tmux(
            "display-message", "-p", "#{start_time}")), phases.CONTINUUM_RESTORE_MAX_DELAY)
        time.sleep(4)  # continuum's restore would start after 1s
        self.assertEqual(self.tmux("list-sessions", "-F", "#{session_name}"), "work")
        self.assertEqual(self.layout(), layout)
        self.assertEqual(self.tmux("show-options", "-gqv", "@continuum-restore"), "on")

    def test_the_wait_is_what_prevents_the_duplicate_restore(self):
        # Without the wait, sourcing continuum into a young server restores
        # the snapshot a second time: the guard above is load-bearing.
        self.install_new_setup()
        maker_tmpdir = self.private_tmpdir("t3")
        maker = maker_tmpdir / f"tmux-{os.getuid()}" / "default"
        self.start("new-session", "-d", "-s", "saved", socket=maker, tmpdir=maker_tmpdir)
        time.sleep(2)
        subprocess.run([self.home / ".tmux" / "resurrect-save"], check=True,
                       env=dict(self.base_env, TMUX_TMPDIR=str(maker_tmpdir),
                                TMUX=f"{maker},0,0"), capture_output=True, timeout=120)
        self.tmux("kill-server", socket=maker)
        self.link_conf(self.upstream)
        self.start("new-session", "-d", "-s", "work")
        self.install_new_setup()
        with mock.patch.object(phases, "_wait_out_continuum_restore", return_value=0.0):
            _, details = self.converge()
        self.assertEqual(details["outcome"], "converged")
        self.wait_until(lambda: "saved" in self.tmux("list-sessions", "-F",
                                                     "#{session_name}"),
                        "the duplicate restore without the wait")

    def test_inherited_tmux_variable_never_redirects_the_converge(self):
        self.link_conf(self.upstream)
        self.start("new-session", "-d", "-s", "work")
        self.install_new_setup()
        env = dict(self.env, TMUX="/nonexistent/socket,1,0", TMUX_PANE="%1")
        reasons, details = self.converge(env)
        self.assertEqual(details["outcome"], "converged", reasons)
        self.assertIn("split-window -h", self.tmux("list-keys", "-T", "prefix", "v"))

    def test_server_running_a_config_outside_this_home_is_left_alone(self):
        outside = self.root / "elsewhere.conf"
        outside.write_text("bind-key s choose-tree -Zs\n")
        self.start("new-session", "-d", "-s", "x", conf=outside)
        self.install_new_setup()
        before = self.config_state()
        reasons, details = self.converge()
        self.assertEqual((reasons, details["outcome"]),
                         ([], "running-server-uses-another-config"))
        self.assertEqual(self.config_state(), before)
        self.assertFalse(any("source-file" in c or "respawn-pane" in c
                             for c in self.runner.calls))

    def test_no_running_server_is_a_no_op(self):
        self.install_new_setup()
        reasons, details = self.converge()
        self.assertEqual((reasons, details["outcome"]), ([], "no-running-server"))
        self.assertFalse(self.socket.exists())
        # Only the read-only probe ran: no defaults server was started.
        self.assertEqual([c[1:] for c in self.runner.calls],
                         [["display-message", "-p",
                           "#{pid}\t#{start_time}\t#{version}\t#{config_files}"]])

    def test_broken_new_config_is_reported_not_fatal(self):
        (self.home / ".zshrc").write_text(OLD_ZSHRC)
        self.link_conf(self.upstream)
        self.start("new-session", "-d", "-s", "work", "-c", self.work / "alpha")
        idle = self.tmux("list-panes", "-t", "work", "-F", "#{pane_id}")
        self.wait_until(lambda: self.pane_pid(idle) in self.shells(), "the old shell")
        old_pid = self.pane_pid(idle)
        broken = self.home / "broken.conf"
        broken.write_text("this-is-not-a-tmux-command\n")
        self.link_conf(broken)
        reasons, details = self.converge()
        self.assertEqual(details["outcome"], "failed")
        self.assertTrue(reasons and "could not fully apply" in reasons[0], reasons)
        self.assertIn("this-is-not-a-tmux-command", reasons[0])
        self.assertEqual(self.tmux("list-sessions", "-F", "#{session_name}"), "work")
        self.assertEqual(self.pane_pid(idle), old_pid)  # shells are left alone
        self.assertEqual(details["respawned_panes"], 0)

    def idle_upstream_server(self) -> tuple[str, int]:
        (self.home / ".zshrc").write_text(OLD_ZSHRC)
        self.link_conf(self.upstream)
        self.start("new-session", "-d", "-s", "work", "-c", self.work / "alpha")
        idle = self.tmux("list-panes", "-t", "work", "-F", "#{pane_id}")
        return idle, self.wait_shell(idle)

    def test_error_reported_only_on_stderr_is_a_load_error(self):
        # tmux exits 0 when a command error precedes a later run-shell in the
        # same file, and only prints the error: still an error, no restart.
        idle, old_pid = self.idle_upstream_server()
        self.install_new_setup()
        conf = self.home / "runtime-error.conf"
        conf.write_text("set -g bogus-option-xyz 1\n" + self.new_conf.read_text())
        self.link_conf(conf)
        reasons, details = self.converge()
        self.assertEqual(details["outcome"], "failed", reasons)
        self.assertIn("bogus-option-xyz", reasons[0])
        self.assertEqual(details["respawned_panes"], 0)
        self.assertNotIn("restarted", details)
        self.assertIsNone(phases.converge_summary(details))
        self.assertEqual(self.pane_pid(idle), old_pid)
        self.assertFalse(any("respawn-pane" in c for c in self.runner.calls))

    def test_hook_is_seen_whatever_happened_to_the_state_directory(self):
        # The hook is an open fd, not a file that must stay in place: a
        # hooked shell in 'read' stays hooked after its state directory was
        # removed and recreated (as a cleanup or a logout may do), and so
        # does one that started with another XDG_STATE_HOME.
        self.write_generation(self.previous)
        self.link_conf(self.upstream)
        (self.home / ".zshrc").write_text(self.hooked_old_zshrc)
        self.start("new-session", "-d", "-s", "work", "-x", "120", "-y", "30",
                   "-c", self.work / "alpha")
        reader = self.tmux("list-panes", "-t", "work", "-F", "#{pane_id}")
        other_state = self.root / "s2"
        other = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "work:",
                          "-c", self.work / "beta", "-e", f"XDG_STATE_HOME={other_state}")
        reader_pid = self.wait_hooked(reader)
        other_pid = self.wait_until(lambda: self.pane_pid(other) in self.shells()
                                    and self.pane_pid(other), "the other shell")
        self.assertTrue((other_state / "personal-dotfiles" / "shell-hook").exists())
        self.tmux("send-keys", "-t", reader, "read answer; print -r -- READ-$answer", "Enter")
        self.tmux("send-keys", "-t", other, "read answer; print -r -- READ-$answer", "Enter")
        for pane in (reader, other):
            self.wait_screen(pane, "READ-$answer", "the read")
        time.sleep(0.5)  # zsh is inside 'read' now
        shutil.rmtree(self.hook_path.parent)
        self.install_new_setup()
        self.write_generation(self.generation)  # recreates the state directory
        self.assertTrue(self.hooked(reader_pid))
        self.assertTrue(self.hooked(other_pid))
        reasons, details = self.converge()
        self.assertEqual((reasons, details["outcome"]), ([], "converged"))
        self.assertEqual((details["kept"], details["restarted"]), ({"hooked": 2}, []))
        self.assertEqual((self.pane_pid(reader), self.pane_pid(other)),
                         (reader_pid, other_pid))
        for pane in (reader, other):
            self.tmux("send-keys", "-t", pane, "typed-answer", "Enter")
            self.wait_line(pane, "READ-typed-answer")

    def test_the_pane_running_the_installer_is_never_restarted(self):
        # The converge runs inside a pane, as 'dotfiles update' does: that
        # pane's shell (upstream-style, unhooked) has the installer as its
        # child, so it is busy and stays.
        idle, idle_pid = self.idle_upstream_server()
        installer_pane = self.new_pane(self.work / "beta")
        shell_pid = self.wait_shell(installer_pane)
        self.install_new_setup()
        self.write_generation(self.generation)
        out = self.root / "result.json"
        script = self.root / "converge_in_pane.py"
        script.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from installer import phases\n"
            "from installer.platform import Target\n"
            "from tests.integration.test_tmux_converge import IsolatedRunner\n"
            f"target = Target(uid={os.getuid()}, gid={os.getgid()}, username='fixture',\n"
            f"    home=Path({str(self.home)!r}), data_home=Path({str(self.data)!r}),\n"
            f"    state_home=Path({str(self.root / 's')!r}),\n"
            f"    config_home=Path({str(self.config)!r}),\n"
            f"    cache_home=Path({str(self.root / 'k')!r}))\n"
            "reasons, details = phases.converge_running_tmux(\n"
            f"    target, IsolatedRunner(Path({str(self.root)!r})), dict(os.environ),\n"
            f"    generation={self.generation!r}, previous_generation={self.previous!r})\n"
            f"Path({str(out) + '.tmp'!r}).write_text(json.dumps(\n"
            "    {'reasons': reasons, 'details': details, 'ppid': os.getppid(),\n"
            "     'tmux': 'TMUX' in os.environ}))\n"
            f"os.replace({str(out) + '.tmp'!r}, {str(out)!r})\n")
        self.tmux("send-keys", "-t", installer_pane,
                  f"PYTHONDONTWRITEBYTECODE=1 python3 -B {script}", "Enter")
        result = json.loads(self.wait_until(lambda: out.exists() and out.read_text(),
                                            "the converge inside the pane", timeout=120))
        self.assertTrue(result["tmux"])  # it ran inside the server's pane
        self.assertEqual(result["ppid"], shell_pid)
        details = result["details"]
        self.assertEqual((result["reasons"], details["outcome"]), ([], "converged"))
        self.assertEqual([e["pane_id"] for e in details["restarted"]], [idle])
        self.assertEqual(details["kept"], {"busy": 1})
        self.assertEqual(self.pane_pid(installer_pane), shell_pid)
        self.assertNotEqual(self.pane_pid(idle), idle_pid)

    def test_unhooked_idle_zsh_is_respawned_only_when_the_generation_changed(self):
        idle, old_pid = self.idle_upstream_server()
        bash = self.new_pane(self.work / "beta", BASH, "--norc", "-i")
        self.wait_screen(bash, "$", "the bash prompt")
        bash_pid = self.pane_pid(bash)
        self.install_new_setup()
        # A repair that changed nothing: the config is applied, no shell
        # is touched.
        self.write_generation(self.previous)
        reasons, details = self.converge(generation=self.previous, previous=self.previous)
        self.assertEqual((reasons, details["outcome"]), ([], "converged"))
        self.assertEqual(details["respawn"], "generation-unchanged")
        self.assertEqual((self.pane_pid(idle), self.pane_pid(bash)), (old_pid, bash_pid))
        self.assertIn("split-window -h", self.tmux("list-keys", "-T", "prefix", "v"))
        self.assertFalse(any("respawn-pane" in c or "list-panes" in c
                             for c in self.runner.calls))
        # No generation written (the write failed): nothing respawned either.
        reasons, details = self.converge(generation=None, previous=self.previous)
        self.assertEqual(details["respawn"], "generation-unchanged")
        reasons, details = phases.converge_running_tmux(
            self.target, self.runner, self.env, generation=None, previous_generation=None)
        self.assertEqual(details["respawn"], "generation-unchanged")
        self.assertEqual(self.pane_pid(idle), old_pid)
        # The generation changed: the unhooked zsh is respawned, the bash is not.
        self.write_generation(self.generation)
        reasons, details = self.converge()
        self.assertEqual((reasons, details["outcome"]), ([], "converged"))
        self.assertEqual([e["pane_id"] for e in details["restarted"]], [idle])
        self.assertEqual(details["kept"], {"not-zsh": 1})
        new_pid = self.wait_hooked(idle)
        self.assertNotEqual(new_pid, old_pid)
        self.assertEqual(self.pane_pid(bash), bash_pid)
        # A first install (nothing recorded before) counts as a change.
        self.assertTrue(phases.generation_changed(self.generation, None))

    def test_pane_that_turns_busy_before_the_respawn_is_left_alone(self):
        # The final checks run right before scrolling and right before
        # respawn-pane: a shell that started a command since the listing is
        # neither scrolled nor respawned.
        idle, old_pid = self.idle_upstream_server()
        self.install_new_setup()
        self.write_generation(self.generation)
        self.tmux("send-keys", "-t", idle, "print -r -- SHOWN-BEFORE", "Enter")
        self.wait_line(idle, "SHOWN-BEFORE")
        original = phases._pane_check
        calls = []

        def check(fields, hook_path):
            calls.append(fields[0])
            if len(calls) == 2:  # the re-check before the scroll
                self.tmux("send-keys", "-t", idle, "sleep 303", "Enter")
                self.wait_until(lambda: self.children(old_pid), "the new command")
            return original(fields, hook_path)

        scrolled = []
        with mock.patch.object(phases, "_pane_check", side_effect=check), \
                mock.patch.object(phases, "_scroll_into_history",
                                  side_effect=lambda *a: scrolled.append(a)):
            reasons, details = self.converge()
        self.assertEqual(details["outcome"], "converged", reasons)
        self.assertEqual((details["restarted"], details["kept"]), ([], {"busy": 1}))
        self.assertEqual(scrolled, [])
        self.assertEqual(self.pane_pid(idle), old_pid)
        self.assertIn("SHOWN-BEFORE", self.screen(idle))
        self.assertFalse(any("respawn-pane" in c for c in self.runner.calls))


@unittest.skipUnless(TMUX, "tmux is required")
class TmuxDefaultsTests(unittest.TestCase):
    """The throwaway defaults server: private, and killed afterwards."""

    def test_defaults_server_is_private_and_killed(self):
        root = Path(tempfile.mkdtemp(prefix="pdfcv", dir="/tmp"))
        try:
            runner = IsolatedRunner(root)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "SHELL": "/bin/sh",
                   "TERM": "xterm-256color"}
            defaults = phases.capture_tmux_defaults(TMUX, runner, env)
            self.assertTrue(any(" prefix " in k and "detach-client" in k
                                for k in defaults.keys))
            self.assertIn(("prefix", "d", "Detach the current client"), defaults.notes)
            self.assertEqual(defaults.options["-g"]["default-shell"],
                             ("default-shell /bin/sh",))
            self.assertIn("history-limit", defaults.options["-g"])
            self.assertIn("escape-time", defaults.options["-s"])
            names = {c[c.index("-L") + 1] for c in runner.calls if "-L" in c}
            self.assertEqual(len(names), 1)
            self.assertEqual(runner.calls[-1][-1], "kill-server")
            tmpdirs = [c for c in runner.calls if "-L" in c]
            self.assertTrue(tmpdirs)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_quoting_round_trips_through_the_config_parser(self):
        for text in ("plain", "it's", 'say "hi"', "$HOME", "a\\b", "#{x}", ";", "~"):
            with self.subTest(text=text):
                quoted = phases.tmux_quote(text)
                self.assertTrue(quoted[0] in "'\"" and quoted[-1] == quoted[0])


if __name__ == "__main__":
    unittest.main()
