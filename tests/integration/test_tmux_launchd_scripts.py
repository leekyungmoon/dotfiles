"""The macOS tmux runtime, exercised on any POSIX host.

- tmux/tmux-launchd-start and tmux/resurrect-autosave, the programs of the two
  LaunchAgents (com.github.leekyungmoon.dotfiles.tmux and ...tmux-resurrect-
  autosave), first against a recording fake tmux, then against real private
  tmux servers;
- tmux/resurrect-save's lock fallbacks (flock(1), else lockf's descriptor
  mode, else python3) and its interruption trap;
- tmux-continuum's macOS boot branch with resurrect.conf (fake `uname`), its
  process count against Apple's ps output, and TPM's load order of the
  declared plugins;
- statusbar.tmux's macOS CPU and memory components (fake `uname`, iostat,
  vm_stat, sysctl), also under bash 3.2.

Isolation: every real tmux server here is private.  Its socket is the default
one of a private TMUX_TMPDIR (the scripts find it the way they do under
launchd: TMUX unset), and the tests address it only with -S.  Environments
are rebuilt from scratch with a temporary HOME, so nothing from the live
session (TMUX, TMUX_PANE, XDG_*, ...) leaks in; `systemctl`, `launchctl`,
`ps` (continuum's process count), `uname`, `flock`, `lockf`, `python3` and
the macOS system tools are fakes or stand-ins on PATH where a test needs
them.  Pinned plugins are cloned read-only from GitHub; those tests skip
when that is impossible.

TMUX_TEST_BINARY selects the tmux (default: the first on PATH).
TMUX_TEST_BASH32 names a bash 3.2 (macOS's /bin/bash, used there by default)
to also run the scripts under; dash is used when installed.  A unix socket
path must stay short, so TMUX_LAUNCHD_TEST_SOCKET_DIR (default: the temp
directory) can name a short base directory for the private TMUX_TMPDIRs.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TMUX_DIR = REPO_ROOT / "tmux"
START = TMUX_DIR / "tmux-launchd-start"
AUTOSAVE = TMUX_DIR / "resurrect-autosave"
SAVE = TMUX_DIR / "resurrect-save"
BOOTSTRAP = "__continuum_startup"
RESTORE_STATE = "@tmux-restore-complete"
RESTORE_STATUS = "@tmux-restore-status"
SKIP_REPORTED = "@tmux-autosave-skip-reported"
SKIPPED = 75
LABELS = ("com.github.leekyungmoon.dotfiles.tmux",
          "com.github.leekyungmoon.dotfiles.tmux-resurrect-autosave")
NOT_FOUND = "tmux not found on PATH; run: brew install tmux && dotfiles repair"
RECOVERY = ("prefix + C-r", "tmux set-option -g @tmux-restore-complete on")
SKIP_TEXT = "has not restored the last snapshot yet"
MIB = 1024 * 1024
TMUX_BIN = os.environ.get("TMUX_TEST_BINARY") or shutil.which("tmux")
UTF8 = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
# sizeof(sun_path) - 1
SOCKET_MAX = 103 if sys.platform == "darwin" else 107

# Records every call; answers from a JSON state file (one call at a time).
FAKE_TMUX = """#!{python} -I
import json, os, sys
path = {state!r}
with open(path) as f:
    state = json.load(f)
argv = sys.argv[1:]
state["calls"].append({{"argv0": sys.argv[0], "argv": argv, "cwd": os.getcwd(),
                        "env": dict(os.environ)}})
status, out = 0, None
if argv[:1] == ["has-session"]:
    status = 0 if state["server"] else 1
elif argv[:1] == ["new-session"]:
    status = state["new_session_status"]
    state["server"] = status == 0
elif not state["server"]:
    status = 1
elif argv[:2] == ["show-option", "-gqv"]:
    name = argv[2]
    if name == "@tmux-restore-complete":
        if state["die_on_poll"]:
            state["server"], status = False, 1
        sequence = state["restore_sequence"]
        out = sequence.pop(0) if len(sequence) > 1 else sequence[0]
    else:
        out = state["options"].get(name, "")
elif argv[:2] == ["set-option", "-gq"]:
    state["options"][argv[2]] = argv[3]
with open(path + ".new", "w") as f:
    json.dump(state, f)
os.replace(path + ".new", path)
if out and status == 0:
    print(out)
sys.exit(status)
"""

# A tool that is not really there: what the shell reports for a missing one.
STAND_IN = "#!/bin/sh\nexit 127\n"

FAKE_UNAME = "#!/bin/sh\necho Darwin\n"

# FreeBSD/macOS lockf(1) in descriptor mode: `lockf -s -t 0 FD` takes
# flock(2) on the inherited FD without waiting (0 held, 75 busy).
FAKE_LOCKF = """#!{python} -I
import fcntl, json, sys
with open({calls!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
mode = {mode!r}
if mode == "no-fd-mode":  # macOS 14 and older: usage error
    sys.stderr.write("usage: lockf [-knsw] [-t seconds] file command [arguments]\\n")
    sys.exit(64)
if mode == "error":
    sys.stderr.write("lockf: cannot lock fd 9: Operation not supported\\n")
    sys.exit(73)
args = sys.argv[1:]
if len(args) != 4 or args[:3] != ["-s", "-t", "0"]:
    sys.exit(64)
try:
    fcntl.flock(int(args[3]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
"""

# Tells whether another open of the lock file can take it right now.
LOCK_PROBE = r"""
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("lock: free")
except BlockingIOError:
    print("lock: held")
"""

# tmux-resurrect's save.sh as resurrect-save sees it: a snapshot, "last"
# repointed at it, the AI hook's marker.  It also records the call, whether
# the save lock is held meanwhile, PATH and COPYFILE_DISABLE.
FAKE_SAVE_SH = """#!/bin/sh
dir=$(tmux show-option -gqv @resurrect-dir)
{{
  echo "called $*"
  {python} -I -c {probe} "$dir/operation.lock"
  echo "PATH=$PATH"
  echo "COPYFILE_DISABLE=${{COPYFILE_DISABLE:-}}"
}} >> "$FAKE_SAVE_LOG"
stamp=$(date +%Y%m%dT%H%M%S)
printf 'pane\\tdemo\\t%s\\n' "$stamp" > "$dir/tmux_resurrect_$stamp.txt"
ln -sf "tmux_resurrect_$stamp.txt" "$dir/last"
case ${{FAKE_SAVE_MODE:-}} in
  hang)
    sleep 300 &
    echo "$$ $!" > "$FAKE_SAVE_PIDS"
    wait
    exit 0
    ;;
  slow)
    echo "$$" > "$FAKE_SAVE_PIDS"
    sleep 2
    ;;
  fail) exit 1 ;;
esac
printf 'tmux_resurrect_%s.txt\\n' "$stamp" > "$TMUX_RESURRECT_HOOK_OK"
"""

# continuum skips the login restore when `ps -u <uid> -o "command pid"`
# shows more than one line starting with "tmux" besides the server.  Show it
# only the lines a test adds, so the live session's tmux processes never
# count; every other ps call is the real one.
FAKE_PS = """#!/bin/sh
case " $* " in
  *" -u "*)
    echo "COMMAND            PID"
    if [ -f "$HOME/ps-extra" ]; then cat "$HOME/ps-extra"; fi
    exit 0
    ;;
esac
exec {real} "$@"
"""

FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/systemctl.log"
case "$*" in
    *is-enabled*) [ -e "$HOME/systemctl-enabled" ] ;;
    *) exit 0 ;;
esac
"""

FAKE_LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/launchctl.log"
exit 0
"""


def write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def bash32() -> str | None:
    """A bash 3.2 (macOS's /bin/bash) to run the scripts under, if any."""
    candidates = [os.environ.get("TMUX_TEST_BASH32", "")]
    if sys.platform == "darwin":
        candidates.append("/bin/bash")
    for path in candidates:
        if path and os.access(path, os.X_OK):
            version = subprocess.run(
                [path, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if version == "3.2":
                return path
    return None


def posix_shells() -> list[tuple[str, list[str]]]:
    """How to run a #!/bin/sh script: its shebang, dash, bash 3.2 as sh."""
    shells = [("shebang", [])]
    dash = shutil.which("dash")
    if dash:
        shells.append(("dash", [dash]))
    old_bash = bash32()
    if old_bash:
        shells.append(("bash-3.2", [old_bash, "--posix"]))
    return shells


def bash_shells() -> list[tuple[str, list[str]]]:
    """How to run a bash script: its shebang (env bash), and bash 3.2."""
    shells = [("shebang", [])]
    old_bash = bash32()
    if old_bash:
        shells.append(("bash-3.2", [old_bash]))
    return shells


def tool_dir(path: Path, names, exclude=()) -> Path:
    """A PATH directory with links to these real tools only."""
    path.mkdir(parents=True, exist_ok=True)
    for name in names:
        real = shutil.which(name, path=os.environ.get("PATH", "/usr/bin:/bin"))
        if real and name not in exclude:
            (path / name).symlink_to(real)
    return path


BASIC_TOOLS = ("cat", "date", "dirname", "mv", "rm", "sleep", "tr", "wc", "ps",
               "awk", "mktemp", "uname", "sed", "grep", "ln", "readlink",
               "mkdir", "chmod", "touch", "hostname", "env")

_PLUGINS: dict = {}


def pinned_plugins(*names: str) -> Path:
    """Clone the pinned plugins once per run; skip the test when impossible."""
    if "error" in _PLUGINS:
        raise unittest.SkipTest(_PLUGINS["error"])
    if shutil.which("git") is None:
        _PLUGINS["error"] = "git is not installed"
        raise unittest.SkipTest(_PLUGINS["error"])
    if "tmp" not in _PLUGINS:
        _PLUGINS["tmp"] = tempfile.TemporaryDirectory(prefix="tmux-launchd-plugins-")
    root = Path(_PLUGINS["tmp"].name)
    manifest = json.loads(
        (REPO_ROOT / "manifests/tmux-plugins.json").read_text(encoding="utf-8")
    )
    pins = {plugin["name"]: plugin for plugin in manifest["plugins"]}
    git_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in names:
        dest = root / name
        if dest.exists():
            continue
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", pins[name]["url"],
                 str(dest)],
                env=git_env, check=True, capture_output=True, timeout=180,
            )
            subprocess.run(
                ["git", "-C", str(dest), "checkout", "--quiet", pins[name]["commit"]],
                env=git_env, check=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as error:
            shutil.rmtree(dest, ignore_errors=True)
            _PLUGINS["error"] = f"cannot clone pinned {name}: {error}"
            raise unittest.SkipTest(_PLUGINS["error"])
    return root


def tearDownModule() -> None:
    tmp = _PLUGINS.pop("tmp", None)
    if tmp is not None:
        tmp.cleanup()


class ScriptRun(unittest.TestCase):
    """A scratch tree, environments built from scratch, script runs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tmux-launchd-test-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        for directory in (self.home, self.bin):
            directory.mkdir()
        # A short private TMUX_TMPDIR: its default socket is the server's.
        base = os.environ.get("TMUX_LAUNCHD_TEST_SOCKET_DIR") or tempfile.gettempdir()
        self.tmux_tmpdir = Path(os.path.realpath(tempfile.mkdtemp(prefix="tl", dir=base)))
        self.addCleanup(shutil.rmtree, self.tmux_tmpdir, True)
        self.socket = self.tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
        if len(os.fsencode(self.socket)) > SOCKET_MAX:
            self.skipTest(f"socket path too long: {self.socket}")

    def base_env(self, **extra) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": UTF8,
            "TMUX_TMPDIR": str(self.tmux_tmpdir),
        }
        env.update(extra)
        return {name: value for name, value in env.items() if value is not None}

    def run_script(self, script, env, shell=(), timeout=90, cwd=None, **kwargs):
        start = time.monotonic()
        result = subprocess.run(
            [*shell, str(script)], env=env, cwd=cwd or self.root,
            capture_output="stdout" not in kwargs, text=True, timeout=timeout,
            **kwargs,
        )
        result.elapsed = time.monotonic() - start
        return result


class PrivateServer(ScriptRun):
    """Plus a real tmux server on the private TMUX_TMPDIR's default socket."""

    def setUp(self) -> None:
        super().setUp()
        if TMUX_BIN is None:
            self.skipTest("tmux is not installed")
        (self.bin / "tmux").symlink_to(TMUX_BIN)
        self.addCleanup(self.kill_server)

    def tmux(self, *args, check=True, env=None):
        result = subprocess.run(
            [TMUX_BIN, "-S", str(self.socket), *[str(arg) for arg in args]],
            env=env or self.base_env(), capture_output=True, text=True, timeout=60,
        )
        if check:
            self.assertEqual(result.returncode, 0, (args, result.stderr))
        return result.stdout.strip()

    def option(self, name):
        return self.tmux("show-option", "-gqv", name)

    def server_running(self):
        return subprocess.run(
            [TMUX_BIN, "-S", str(self.socket), "has-session"],
            env=self.base_env(), capture_output=True, timeout=30,
        ).returncode == 0

    def start_plain_server(self, env=None):
        """A server without any config (-f /dev/null)."""
        self.socket.parent.mkdir(mode=0o700, exist_ok=True)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", env=env)

    def kill_server(self):
        if not self.socket.exists() or not self.server_running():
            return
        self.tmux("kill-server", check=False)
        self.wait_until(lambda: not self.server_running(), "server exit", timeout=10)

    def wait_until(self, predicate, what, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")


class LaunchdStartWithFakeTmux(ScriptRun):
    """tmux-launchd-start's decisions, one recorded tmux call at a time."""

    def setUp(self) -> None:
        super().setUp()
        self.state_file = self.root / "tmux-state.json"
        self.fake = write_executable(self.bin / "tmux", FAKE_TMUX.format(
            python=sys.executable, state=str(self.state_file)))
        self.snapshots = self.root / "data/tmux/resurrect"
        self.snapshots.mkdir(parents=True)
        self.log = self.root / "launchd.log"

    def set_state(self, server=False, snapshot=True, new_session_status=0,
                  sequence=("", "restoring", "on"), die_on_poll=False, options=None):
        """The fake server's state; `options` overrides (None: unset) what
        the config would have set."""
        if snapshot:
            (self.snapshots / "tmux_resurrect_x.txt").write_text("pane\n")
            (self.snapshots / "last").symlink_to("tmux_resurrect_x.txt")
        values = {
            "@resurrect-dir": str(self.snapshots),
            "@resurrect-restore-script-path": "/plugins/tmux-resurrect/scripts/restore.sh",
            "@continuum-restore": "on",
        }
        values.update(options or {})
        self.state_file.write_text(json.dumps({
            "server": server, "new_session_status": new_session_status,
            "restore_sequence": list(sequence), "die_on_poll": die_on_poll,
            "options": {name: value for name, value in values.items()
                        if value is not None},
            "calls": [],
        }))

    def state(self):
        return json.loads(self.state_file.read_text())

    def calls(self):
        return [call["argv"] for call in self.state()["calls"]]

    def env(self, **extra):
        values = {
            # Must not reach the server: another server's address, launchd's
            # job identity, this script's own settings.
            "TMUX": "/elsewhere/tmux-1000/default,1,0",
            "TMUX_PANE": "%9",
            "XPC_SERVICE_NAME": LABELS[0],
            "XPC_FLAGS": "0x0",
            "TMUX_LAUNCHD_START_WAIT": "1",
            "TMUX_LAUNCHD_FINISH_WAIT": "1",
            "PERSONAL_DOTFILES_LAUNCHD_LOG": str(self.log),
        }
        values.update(extra)
        return self.base_env(**values)

    def start(self, shell=(), **extra):
        return self.run_script(START, self.env(**extra), shell=shell)

    def test_script_is_posix_sh(self):
        self.assertTrue(os.access(START, os.X_OK))
        self.assertEqual(START.read_text().splitlines()[0], "#!/bin/sh")
        for name, shell in posix_shells()[1:]:
            with self.subTest(name):
                subprocess.run([*shell, "-n", str(START)], check=True, timeout=30)

    def test_running_server_is_left_untouched(self):
        for name, shell in posix_shells():
            with self.subTest(name):
                self.set_state(server=True, snapshot=False)
                result = self.start(shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("already running; left untouched", result.stderr)
                self.assertEqual(self.calls(), [["has-session"]])
                self.assertEqual(self.state()["options"].get(RESTORE_STATUS), None)

    def test_bootstrap_runs_absolute_tmux_in_home_with_a_clean_environment(self):
        for name, shell in posix_shells():
            for how, extra in (("PATH", {}),
                               ("override", {"TMUX_LAUNCHD_TMUX": str(self.fake)})):
                with self.subTest(name, tmux=how):
                    shutil.rmtree(self.snapshots)
                    self.snapshots.mkdir()
                    self.set_state(snapshot=False)
                    result = self.start(shell, **extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.state()["calls"]
                    self.assertEqual([call["argv"] for call in calls[:2]],
                                     [["has-session"], ["new-session", "-d", "-s", BOOTSTRAP]])
                    for call in calls:
                        self.assertEqual(call["argv0"], str(self.fake))  # absolute
                    bootstrap = calls[1]
                    self.assertEqual(bootstrap["cwd"], str(self.home))
                    for variable in ("TMUX", "TMUX_PANE", "XPC_SERVICE_NAME", "XPC_FLAGS",
                                     "TMUX_LAUNCHD_TMUX", "TMUX_LAUNCHD_START_WAIT",
                                     "TMUX_LAUNCHD_FINISH_WAIT",
                                     "PERSONAL_DOTFILES_LAUNCHD_LOG"):
                        self.assertNotIn(variable, bootstrap["env"])
                    self.assertEqual(bootstrap["env"]["LANG"], UTF8)
                    self.assertEqual(bootstrap["env"]["TMUX_TMPDIR"], str(self.tmux_tmpdir))
                    # Nothing to restore yet: reported, success, no waiting.
                    self.assertEqual(self.state()["options"][RESTORE_STATUS], "no snapshot")
                    self.assertIn("no snapshot to restore", result.stderr)
                    self.assertLess(result.elapsed, 10)

    def test_missing_or_relative_tmux_is_refused(self):
        tools = tool_dir(self.root / "tools", BASIC_TOOLS, exclude=("tmux",))
        self.set_state()
        result = self.start(PATH=str(tools))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(NOT_FOUND, result.stderr)
        for value in ("tmux", "bin/tmux", str(self.root / "missing/tmux")):
            with self.subTest(override=value):
                result = self.start(TMUX_LAUNCHD_TMUX=value)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("TMUX_LAUNCHD_TMUX is not an absolute path to tmux",
                              result.stderr)
        self.assertEqual(self.calls(), [])

    def test_utf8_locale_is_kept_or_provided(self):
        cases = (
            ("no locale", {"LANG": None}, "en_US.UTF-8"),
            ("C", {"LANG": "C"}, "en_US.UTF-8"),
            ("korean", {"LANG": "ko_KR.UTF-8"}, "ko_KR.UTF-8"),
            ("lowercase", {"LANG": "en_US.utf8"}, "en_US.utf8"),
            ("LC_CTYPE", {"LANG": "C", "LC_CTYPE": "en_US.UTF-8"}, "C"),
        )
        for name, extra, expected in cases:
            with self.subTest(name):
                self.set_state(server=False, snapshot=False)
                shutil.rmtree(self.snapshots)
                self.snapshots.mkdir()
                result = self.start(**extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.state()["calls"][1]["env"].get("LANG"), expected)

    def test_restore_outcomes_are_recorded_and_logged(self):
        cases = (
            # name, state, exit status, @tmux-restore-status, log text
            ("restored", {}, 0, "restored ", "restored the last snapshot"),
            ("no continuum restore", {"sequence": ("",)}, 3,
             "not restored: continuum did not start it within 1s",
             "did not restore the snapshot within 1 s"),
            ("restore never finishes", {"sequence": ("restoring",)}, 3,
             "not restored: still restoring after 1s", "did not finish within 1 s"),
            ("resurrect not loaded", {"options": {"@resurrect-restore-script-path": None}},
             3, "not restored: tmux-resurrect is not loaded", "dotfiles repair"),
            ("continuum restore off", {"options": {"@continuum-restore": "off"}}, 3,
             "not restored: @continuum-restore is off", "@continuum-restore is not on"),
        )
        for name, state, status, recorded, text in cases:
            for shell_name, shell in posix_shells():
                with self.subTest(name, shell=shell_name):
                    shutil.rmtree(self.snapshots)
                    self.snapshots.mkdir()
                    self.set_state(**state)
                    result = self.start(shell)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertTrue(
                        self.state()["options"][RESTORE_STATUS].startswith(recorded),
                        self.state()["options"][RESTORE_STATUS])
                    self.assertIn(text, result.stderr)
                    if status:
                        for step in RECOVERY:
                            self.assertIn(step, result.stderr)
                    self.assertLess(result.elapsed, 15)

    def test_halt_file_is_success_with_recovery_steps(self):
        (self.home / "tmux_no_auto_restore").touch()
        self.set_state(sequence=("",))
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["options"][RESTORE_STATUS],
                         "not restored: ~/tmux_no_auto_restore exists")
        self.assertIn("remove it to restore at the next start", result.stderr)
        for step in RECOVERY:
            self.assertIn(step, result.stderr)
        # Decided at once: no wait for a restore that cannot come.
        self.assertNotIn(["show-option", "-gqv", RESTORE_STATE], self.calls())

    def test_server_gone_while_waiting(self):
        # The wait ends as soon as the server stops answering.
        self.set_state(sequence=("",), die_on_poll=True)
        result = self.start(TMUX_LAUNCHD_START_WAIT="60")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("the tmux server exited before the snapshot was restored",
                      result.stderr)
        self.assertLess(result.elapsed, 20)

    def test_failed_start_returns_its_status(self):
        self.set_state(new_session_status=1)
        result = self.start()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("tmux new-session failed (exit status 1)", result.stderr)
        self.assertEqual(self.calls(), [["has-session"],
                                        ["new-session", "-d", "-s", BOOTSTRAP]])

    def test_log_rotates_past_one_mebibyte(self):
        rotated = self.log.with_name(self.log.name + ".1")
        old = b"x" * (MIB + 1)
        cases = (
            # name, prefilled size, rotated?
            ("big", old, True),
            ("exactly 1 MiB", b"y" * MIB, False),
        )
        for name, content, rotates in cases:
            for shell_name, shell in posix_shells():
                with self.subTest(name, shell=shell_name):
                    for path in (self.log, rotated):
                        path.unlink(missing_ok=True)
                    self.log.write_bytes(content)
                    self.set_state(server=True, snapshot=False)
                    # As launchd runs it: stdout and stderr append to the log.
                    with open(self.log, "ab") as log:
                        result = self.run_script(START, self.env(), shell=shell,
                                                 stdout=log, stderr=log)
                    self.assertEqual(result.returncode, 0)
                    data = self.log.read_bytes()
                    if rotates:
                        self.assertEqual(rotated.read_bytes(), content)
                        self.assertNotIn(b"x", data.split(b" ")[0])
                        self.assertIn(b"already running; left untouched", data)
                        self.assertLess(len(data), 1000)
                    else:
                        self.assertFalse(rotated.exists())
                        self.assertTrue(data.startswith(content))
                        self.assertIn(b"already running; left untouched", data)

    def test_rotation_keeps_terminal_output_where_it_was(self):
        self.log.write_bytes(b"z" * (MIB + 10))
        self.set_state(server=True, snapshot=False)
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("left untouched", result.stderr)  # still on the caller's stderr
        self.assertFalse(self.log.exists())
        self.assertEqual(len(self.log.with_name(self.log.name + ".1").read_bytes()),
                         MIB + 10)


class SnapshotServer(PrivateServer):
    """A private server whose saves go through the real resurrect-save into a
    snapshot directory, with a fake tmux-resurrect save.sh."""

    def setUp(self) -> None:
        super().setUp()
        self.snapshots = self.root / "snapshots"
        self.plugins = self.root / "plugins"
        save_sh = self.plugins / "tmux-resurrect/scripts/save.sh"
        save_sh.parent.mkdir(parents=True)
        write_executable(save_sh, FAKE_SAVE_SH.format(
            python=sys.executable, probe=shlex_quote(LOCK_PROBE)))
        self.save_log = self.root / "save.log"
        self.pids = self.root / "save.pids"

    def start_snapshot_server(self, restored=True):
        self.start_plain_server()
        self.tmux("set-environment", "-g", "TMUX_PLUGIN_MANAGER_PATH", f"{self.plugins}/")
        self.tmux("set-option", "-g", "@resurrect-dir", self.snapshots)
        if restored:
            self.tmux("set-option", "-g", RESTORE_STATE, "on")

    def save_env(self, **extra):
        return self.base_env(**{
            "TMUX": f"{self.socket},0,0",
            "FAKE_SAVE_LOG": str(self.save_log),
            "FAKE_SAVE_PIDS": str(self.pids),
            **extra,
        })

    def save_calls(self):
        if not self.save_log.exists():
            return []
        return [line for line in self.save_log.read_text().splitlines()
                if line.startswith("called")]

    def save_log_lines(self):
        return self.save_log.read_text().splitlines() if self.save_log.exists() else []

    def last(self):
        return os.readlink(self.snapshots / "last")

    def make_previous_snapshot(self):
        result = self.run_script(SAVE, self.save_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.save_log.unlink()
        time.sleep(1.1)  # the next snapshot gets a new one-second name
        return self.last()

    def wait_for_pids(self):
        self.wait_until(lambda: self.pids.exists() and self.pids.read_text().strip(),
                        "the save to start")
        return [int(pid) for pid in self.pids.read_text().split()]

    def assert_gone(self, pids):
        for pid in pids:
            self.wait_until(lambda: not pid_alive(pid), f"process {pid} to end",
                            timeout=10)


def shlex_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not yet reaped by its parent) counts as gone.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class ResurrectSaveLocking(SnapshotServer):
    """resurrect-save's lock on every path, and its interruption trap."""

    def setUp(self) -> None:
        super().setUp()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self.lockf_calls = self.root / "lockf.calls"
        self.start_snapshot_server()

    def without(self, *names):
        """Stand-ins that act as missing tools (exit 127)."""
        for name in names:
            write_executable(self.shims / name, STAND_IN)

    def fake_lockf(self, mode="ok"):
        write_executable(self.shims / "lockf", FAKE_LOCKF.format(
            python=sys.executable, calls=str(self.lockf_calls), mode=mode))

    def reset_shims(self):
        for entry in self.shims.iterdir():
            entry.unlink()
        self.lockf_calls.unlink(missing_ok=True)

    def save(self, shell=(), **extra):
        env = self.save_env(PATH=f"{self.shims}:{self.bin}:/usr/bin:/bin", **extra)
        return self.run_script(SAVE, env, shell=shell)

    def lockf_used(self):
        if not self.lockf_calls.exists():
            return []
        return [json.loads(line) for line in self.lockf_calls.read_text().splitlines()]

    def paths(self):
        """(name, setup) for each way of taking the lock."""
        paths = [
            ("lockf", lambda: (self.without("flock"), self.fake_lockf())),
            ("lockf-without-fd-mode-then-python3",
             lambda: (self.without("flock"), self.fake_lockf("no-fd-mode"))),
            ("python3", lambda: self.without("flock", "lockf")),
        ]
        if shutil.which("flock", path=f"{self.bin}:/usr/bin:/bin"):
            paths.insert(0, ("flock", lambda: None))
        if shutil.which("lockf", path="/usr/bin"):  # macOS: the real one
            paths.append(("system-lockf", lambda: self.without("flock")))
        return paths

    def test_every_lock_path_holds_the_lock_during_the_save(self):
        for shell_name, shell in bash_shells():
            for name, setup in self.paths():
                with self.subTest(name, shell=shell_name):
                    self.reset_shims()
                    self.save_log.unlink(missing_ok=True)
                    setup()
                    before = self.last() if (self.snapshots / "last").exists() else None
                    time.sleep(1.1)
                    result = self.save(shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(self.save_calls(), ["called quiet"])
                    self.assertIn("lock: held", self.save_log_lines())
                    self.assertNotEqual(self.last(), before)
                    expected_lockf = ([["-s", "-t", "0", "9"]] if name.startswith("lockf")
                                      else [])
                    self.assertEqual(self.lockf_used(), expected_lockf)

    def test_busy_lock_skips_quietly_on_every_path(self):
        previous = self.make_previous_snapshot()
        lock = os.open(self.snapshots / "operation.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)  # another save is running
        for shell_name, shell in bash_shells():
            for name, setup in self.paths():
                with self.subTest(name, shell=shell_name):
                    self.reset_shims()
                    setup()
                    result = self.save(shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(self.save_calls(), [])
                    self.assertEqual(self.last(), previous)

    def test_lock_errors_fail_loudly(self):
        previous = self.make_previous_snapshot()
        cases = (
            ("lockf error", lambda: (self.without("flock"), self.fake_lockf("error")),
             ["cannot lock", "operation.lock (lockf exit status 73)",
              "Operation not supported"]),
            ("no lock tool", lambda: self.without("flock", "lockf", "python3"),
             ["no flock, lockf or python3 found; nothing saved."]),
        )
        for shell_name, shell in bash_shells():
            for name, setup, texts in cases:
                with self.subTest(name, shell=shell_name):
                    self.reset_shims()
                    setup()
                    result = self.save(shell)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    for text in texts:
                        self.assertIn(text, result.stderr)
                    self.assertEqual(self.save_calls(), [])
                    self.assertEqual(self.last(), previous)

    def test_relative_snapshot_directory_is_refused(self):
        # bash 3.2 does not stop at a failing [[ ]] under set -e.
        self.tmux("set-option", "-g", "@resurrect-dir", "relative/snapshots")
        work = self.root / "cwd"
        work.mkdir()
        for shell_name, shell in bash_shells():
            with self.subTest(shell_name):
                env = self.save_env(PATH=f"{self.shims}:{self.bin}:/usr/bin:/bin")
                result = self.run_script(SAVE, env, shell=shell, cwd=work)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("@resurrect-dir is not an absolute path: relative/snapshots",
                              result.stderr)
                self.assertEqual(os.listdir(work), [])
                self.assertEqual(self.save_calls(), [])

    def test_darwin_adds_homebrew_and_keeps_tarballs_free_of_appledouble(self):
        write_executable(self.shims / "uname", FAKE_UNAME)
        result = self.save()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.save_log_lines()
        (path,) = [line for line in lines if line.startswith("PATH=")]
        self.assertTrue(path.endswith(":/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin"),
                        path)
        self.assertIn("COPYFILE_DISABLE=1", lines)

    @unittest.skipIf(sys.platform == "darwin", "the host itself is macOS")
    def test_linux_path_is_unchanged(self):
        result = self.save()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.save_log_lines()
        (path,) = [line for line in lines if line.startswith("PATH=")]
        self.assertEqual(
            path, f"PATH={self.shims}:{self.bin}:/usr/bin:/bin:{self.home}/.local/bin:"
                  "/usr/local/bin:/usr/bin:/bin")
        self.assertIn("COPYFILE_DISABLE=", lines)

    def test_stop_during_the_save_keeps_the_previous_snapshot(self):
        previous = self.make_previous_snapshot()
        # group: TERM to the whole process group (launchd, systemd, the
        # watchdog), which ends save.sh at once; script: TERM to resurrect-save
        # alone, handled once save.sh has finished on its own.
        cases = [(shell_name, shell, name, mode)
                 for shell_name, shell in bash_shells()
                 for name, mode in (("group", "hang"), ("script", "slow"))]
        for shell_name, shell, name, mode in cases:
            with self.subTest(name, shell=shell_name):
                self.pids.unlink(missing_ok=True)
                proc = subprocess.Popen(
                    [*shell, str(SAVE)], env=self.save_env(FAKE_SAVE_MODE=mode),
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
                try:
                    pids = self.wait_for_pids()
                    self.assertNotEqual(self.last(), previous)  # repointed meanwhile
                    if name == "group":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        os.kill(proc.pid, signal.SIGTERM)
                    _out, err = proc.communicate(timeout=30)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()
                self.assertEqual(proc.returncode, 1, err)
                self.assertIn("save interrupted; previous snapshot preserved.", err)
                self.assertEqual(self.last(), previous)
                self.assert_gone(pids)
                self.assertEqual(
                    [path.name for path in self.snapshots.glob(".hook-ok.*")], [])


@unittest.skipUnless(sys.platform == "darwin", "macOS's own lockf(1)")
class MacOSLockf(unittest.TestCase):
    """What resurrect-save relies on: /usr/bin/lockf's descriptor mode (macOS
    15 and later) locks the open file of the caller's fd 9 for as long as
    the caller keeps it open, and answers 75 while another holds it."""

    def lockf_fd9(self, path):
        script = ('exec 9>"$1"; /usr/bin/lockf -s -t 0 9; echo "lockf=$?"; '
                  '"$2" -I -c "$3" "$1"')
        result = subprocess.run(
            ["/bin/sh", "-c", script, "-", str(path), sys.executable, LOCK_PROBE],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        if lines[0] == "lockf=64":
            self.skipTest("this lockf has no descriptor mode (macOS 14 or older)")
        return lines

    def test_descriptor_mode_holds_and_reports_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operation.lock"
            # Held by this shell's fd 9 after lockf itself has exited.
            self.assertEqual(self.lockf_fd9(path), ["lockf=0", "lock: held"])
            other = os.open(path, os.O_WRONLY)
            try:
                fcntl.flock(other, fcntl.LOCK_EX)  # another save is running
                self.assertEqual(self.lockf_fd9(path)[0], "lockf=75")
            finally:
                os.close(other)


class AutosaveAgent(SnapshotServer):
    """resurrect-autosave with the real resurrect-save behind it."""

    def setUp(self) -> None:
        super().setUp()
        self.log = self.root / "autosave.log"

    def autosave_env(self, **extra):
        return self.base_env(**{
            "FAKE_SAVE_LOG": str(self.save_log),
            "FAKE_SAVE_PIDS": str(self.pids),
            "PERSONAL_DOTFILES_LAUNCHD_LOG": str(self.log),
            # Never used: the agent saves the default server.
            "TMUX": "/elsewhere/tmux-1000/default,1,0",
            **extra,
        })

    def autosave(self, shell=(), **extra):
        return self.run_script(AUTOSAVE, self.autosave_env(**extra), shell=shell)

    def test_script_is_posix_sh(self):
        self.assertTrue(os.access(AUTOSAVE, os.X_OK))
        self.assertEqual(AUTOSAVE.read_text().splitlines()[0], "#!/bin/sh")
        for name, shell in posix_shells()[1:]:
            with self.subTest(name):
                subprocess.run([*shell, "-n", str(AUTOSAVE)], check=True, timeout=30)

    def test_no_server_is_success_without_a_save(self):
        for name, shell in posix_shells():
            with self.subTest(name):
                result = self.autosave(shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.save_calls(), [])

    def test_missing_tmux_fails_with_a_hint(self):
        tools = tool_dir(self.root / "tools", BASIC_TOOLS, exclude=("tmux",))
        result = self.autosave(PATH=str(tools))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(NOT_FOUND, result.stderr)

    def test_saves_quietly(self):
        self.start_snapshot_server()
        for name, shell in posix_shells():
            with self.subTest(name):
                before = (self.last() if (self.snapshots / "last").exists() else None)
                time.sleep(1.1)
                result = self.autosave(shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertNotEqual(self.last(), before)
                self.assertIn("lock: held", self.save_log_lines())

    def test_not_restored_counts_as_success_and_is_reported_once_per_server(self):
        self.start_snapshot_server()
        previous = self.make_previous_snapshot()
        for name, shell in posix_shells():
            with self.subTest(name):
                self.kill_server()
                time.sleep(1.1)  # a new server: a new #{start_time}
                self.start_snapshot_server(restored=False)
                started = self.tmux("display-message", "-p", "#{start_time}")
                first = self.autosave(shell)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertIn(SKIP_TEXT, first.stderr)
                self.assertIn("tmux set-option -g @tmux-restore-complete on", first.stderr)
                self.assertEqual(self.option(SKIP_REPORTED), started)
                second = self.autosave(shell)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stderr, "")
                self.assertEqual(self.last(), previous)
                self.assertEqual(self.save_calls(), [])

    def test_failures_pass_through(self):
        self.start_snapshot_server()
        self.make_previous_snapshot()
        for name, shell in posix_shells():
            with self.subTest(name):
                result = self.autosave(shell, FAKE_SAVE_MODE="fail")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("previous snapshot preserved", result.stderr)

    def test_watchdog_stops_a_save_that_hangs(self):
        self.start_snapshot_server()
        previous = self.make_previous_snapshot()
        for name, shell in posix_shells():
            with self.subTest(name):
                self.pids.unlink(missing_ok=True)
                result = self.autosave(shell, FAKE_SAVE_MODE="hang",
                                       TMUX_AUTOSAVE_TIMEOUT="2")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.returncode, SKIPPED)
                self.assertIn("resurrect-save still running after 2 s; stopping it",
                              result.stderr)
                self.assertIn("save interrupted; previous snapshot preserved.",
                              result.stderr)
                self.assertLess(result.elapsed, 20)
                self.assertEqual(self.last(), previous)
                self.assert_gone([int(pid) for pid in self.pids.read_text().split()])

    def test_stopping_the_agent_stops_the_save(self):
        self.start_snapshot_server()
        previous = self.make_previous_snapshot()
        for name, shell in posix_shells():
            with self.subTest(name):
                self.pids.unlink(missing_ok=True)
                proc = subprocess.Popen(
                    [*shell, str(AUTOSAVE)], env=self.autosave_env(FAKE_SAVE_MODE="hang"),
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
                try:
                    pids = self.wait_for_pids()
                    os.kill(proc.pid, signal.SIGTERM)  # the agent only
                    _out, err = proc.communicate(timeout=30)
                finally:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()
                self.assertEqual(proc.returncode, 1, err)
                self.assertIn("save interrupted; previous snapshot preserved.", err)
                self.assertEqual(self.last(), previous)
                self.assert_gone(pids)

    def test_log_rotates_past_one_mebibyte(self):
        rotated = self.log.with_name(self.log.name + ".1")
        content = b"o" * (MIB + 5)
        tools = tool_dir(self.root / "tools", BASIC_TOOLS, exclude=("tmux",))
        for name, shell in posix_shells():
            with self.subTest(name):
                rotated.unlink(missing_ok=True)
                self.log.write_bytes(content)
                # As launchd runs it: stdout and stderr append to the log.
                with open(self.log, "ab") as log:
                    result = self.run_script(
                        AUTOSAVE, self.autosave_env(PATH=str(tools)),
                        shell=shell, stdout=log, stderr=log)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(rotated.read_bytes(), content)
                data = self.log.read_bytes()
                self.assertIn(NOT_FOUND.encode(), data)
                self.assertLess(len(data), 1000)


class LaunchdStartEndToEnd(PrivateServer):
    """tmux-launchd-start with the full tmux.conf, TPM and the pinned plugins:
    what the tmux LaunchAgent does at login."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_dir = pinned_plugins("tpm", "tmux-resurrect", "tmux-continuum")

    def setUp(self) -> None:
        super().setUp()
        data = self.home / ".local/share"
        (data / "tmux").mkdir(parents=True)
        (data / "tmux/plugins").symlink_to(self.plugin_dir)
        self.snapshots = data / "tmux/resurrect"
        (self.home / ".dotfiles").symlink_to(REPO_ROOT)
        (self.home / ".tmux").symlink_to(TMUX_DIR)
        (self.home / ".tmux.conf").symlink_to(TMUX_DIR / "tmux.conf")
        # zsh -il in the panes: no new-user wizard, no global compinit.
        (self.home / ".zshenv").write_text("skip_global_compinit=1\n")
        (self.home / ".zshrc").write_text("PS1='%# '\n")
        write_executable(self.bin / "ps", FAKE_PS.format(
            real=shutil.which("ps") or "/bin/ps"))
        write_executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        write_executable(self.bin / "launchctl", FAKE_LAUNCHCTL)
        self.work = self.root / "work"
        for name in ("alpha", "beta"):
            (self.work / name).mkdir(parents=True)
        self.log = self.root / "state/personal-dotfiles/launchd" / f"{LABELS[0]}.log"
        self.log.parent.mkdir(parents=True)

    def agent_env(self, **extra):
        """What the agent's plist gives the script (no TMUX)."""
        return self.base_env(**{
            "SHELL": shutil.which("zsh") or "/bin/bash",
            "TMUX_LAUNCHD_TMUX": str(self.bin / "tmux"),
            "PERSONAL_DOTFILES_LAUNCHD_LOG": str(self.log),
            **extra,
        })

    def login(self, **extra):
        self.socket.parent.mkdir(mode=0o700, exist_ok=True)
        with open(self.log, "a") as log:
            result = self.run_script(START, self.agent_env(**extra), stdout=log,
                                     stderr=subprocess.STDOUT, timeout=300)
        self.wait_for_continuum_job()
        return result

    def logged(self):
        return self.log.read_text()

    def continuum_jobs(self):
        # The job runs as `bash <plugin dir>/scripts/continuum_restore.sh`,
        # by the path TPM found the plugin under: this test's own HOME
        # (~/.local/share/tmux/plugins, a link to the clones).  The command
        # column is the last one, so ps prints it whole.
        listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "command="],
                                 capture_output=True, text=True).stdout
        return [line for line in listing.splitlines()
                if str(self.home) in line and "continuum_restore" in line]

    def wait_for_continuum_job(self):
        # continuum restores from a background job that reaches the server by
        # socket; let it end, so a quick restart never gets the old restore.
        self.wait_until(lambda: not self.continuum_jobs(), "continuum's restore job",
                        timeout=60)

    def sessions(self):
        return sorted(self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def windows(self):
        return sorted(self.tmux("list-windows", "-a", "-F",
                                "#{session_name}|#{window_index}|#{window_name}").splitlines())

    def save_workspace(self):
        self.tmux("new-session", "-d", "-s", "work", "-n", "edit", "-c", self.work / "alpha")
        self.tmux("new-window", "-d", "-t", "work:2", "-n", "logs", "-c", self.work / "beta")
        windows = self.windows()
        result = self.run_script(SAVE, self.base_env(TMUX=f"{self.socket},0,0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.snapshots / "last").is_file())
        return windows

    def logout(self):
        self.kill_server()

    def test_first_login_starts_tmux_with_nothing_to_restore(self):
        result = self.login()
        self.assertEqual(result.returncode, 0, self.logged())
        self.assertEqual(self.sessions(), [BOOTSTRAP])
        self.assertEqual(self.tmux("display-message", "-p", "-t", f"={BOOTSTRAP}:",
                                   "#{pane_current_path}"), str(self.home))
        self.assertEqual(self.option(RESTORE_STATUS), "no snapshot")
        self.assertIn("no snapshot to restore", self.logged())
        # Never this script's settings, launchd's identity or the log path.
        environment = self.tmux("show-environment", "-g")
        for name in ("TMUX_LAUNCHD_TMUX", "PERSONAL_DOTFILES_LAUNCHD_LOG",
                     "XPC_SERVICE_NAME"):
            self.assertNotIn(f"{name}=", environment)

    def test_login_restores_the_last_snapshot_and_reports_it(self):
        self.assertEqual(self.login().returncode, 0, self.logged())
        windows = self.save_workspace()
        self.logout()
        result = self.login()
        self.assertEqual(result.returncode, 0, self.logged())
        self.assertEqual(self.option(RESTORE_STATE), "on")
        self.assertTrue(self.option(RESTORE_STATUS).startswith("restored "),
                        self.option(RESTORE_STATUS))
        self.assertIn("started tmux and restored the last snapshot", self.logged())
        self.assertEqual(self.sessions(), [BOOTSTRAP, "work"])
        self.assertEqual(self.windows(), windows)

    def test_running_server_is_left_untouched(self):
        self.assertEqual(self.login().returncode, 0, self.logged())
        pid = self.tmux("display-message", "-p", "#{pid}")
        self.tmux("set-option", "-g", RESTORE_STATUS, "kept")
        result = self.login()
        self.assertEqual(result.returncode, 0, self.logged())
        self.assertIn("already running; left untouched", self.logged())
        self.assertEqual(self.tmux("display-message", "-p", "#{pid}"), pid)
        self.assertEqual(self.option(RESTORE_STATUS), "kept")

    def test_skipped_restore_exits_3_with_recovery_steps(self):
        self.assertEqual(self.login().returncode, 0, self.logged())
        self.save_workspace()
        last = os.readlink(self.snapshots / "last")
        self.logout()
        # Two other tmux processes at startup: continuum skips the restore.
        (self.home / "ps-extra").write_text(
            "tmux attach-session -t x   99998\ntmux new-session -s y      99999\n")
        result = self.login(TMUX_LAUNCHD_START_WAIT="3")
        self.assertEqual(result.returncode, 3, self.logged())
        self.assertEqual(self.sessions(), [BOOTSTRAP])
        self.assertTrue(self.option(RESTORE_STATUS).startswith(
            "not restored: continuum did not start it within 3s"), self.option(RESTORE_STATUS))
        for step in RECOVERY:
            self.assertIn(step, self.logged())
        self.assertEqual(os.readlink(self.snapshots / "last"), last)

    def test_halt_file_starts_tmux_without_restoring(self):
        self.assertEqual(self.login().returncode, 0, self.logged())
        self.save_workspace()
        self.logout()
        (self.home / "tmux_no_auto_restore").touch()
        result = self.login()
        self.assertEqual(result.returncode, 0, self.logged())
        self.assertEqual(self.sessions(), [BOOTSTRAP])
        self.assertEqual(self.option(RESTORE_STATUS),
                         "not restored: ~/tmux_no_auto_restore exists")
        self.assertLess(result.elapsed, 60)


class ContinuumOnMacOS(PrivateServer):
    """continuum's boot handling where `uname` says Darwin, with resurrect.conf."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_dir = pinned_plugins("tmux-continuum")

    def setUp(self) -> None:
        super().setUp()
        self.fakes = self.root / "fakes"
        self.fakes.mkdir()
        write_executable(self.fakes / "systemctl", FAKE_SYSTEMCTL)
        write_executable(self.fakes / "launchctl", FAKE_LAUNCHCTL)
        self.config = self.home / ".config"
        # A managed, enabled tmux.service: the Linux line alone would say "on".
        unit = self.config / "systemd/user/tmux.service"
        unit.parent.mkdir(parents=True)
        unit.write_bytes((REPO_ROOT / "systemd/user/tmux.service").read_bytes())
        (self.home / "systemctl-enabled").touch()
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)

    def server_env(self, darwin):
        if darwin:
            write_executable(self.fakes / "uname", FAKE_UNAME)
        else:
            (self.fakes / "uname").unlink(missing_ok=True)
        return self.base_env(PATH=f"{self.fakes}:{self.bin}:/usr/bin:/bin",
                             XDG_CONFIG_HOME=str(self.config))

    def boot_option(self, darwin):
        env = self.server_env(darwin)
        self.kill_server()
        self.start_plain_server(env=env)
        self.tmux("source-file", TMUX_DIR / "resurrect.conf", env=env)
        return self.option("@continuum-boot")

    def test_boot_is_off_on_macos_even_with_an_enabled_unit(self):
        self.assertEqual(self.boot_option(darwin=True), "off")
        if sys.platform != "darwin":
            # Control: the same setup on Linux follows the enabled unit.
            self.assertEqual(self.boot_option(darwin=False), "on")

    def test_continuum_only_removes_its_own_launch_agent(self):
        ours = {}
        for label in LABELS:
            path = self.agents / f"{label}.plist"
            path.write_text(f"<plist><!-- {label} --></plist>\n")
            ours[path] = (path.read_bytes(), path.stat().st_mtime_ns)
        (self.agents / "Tmux.Start.plist").write_text("<plist>continuum</plist>\n")
        self.assertEqual(self.boot_option(darwin=True), "off")
        env = dict(self.server_env(darwin=True), TMUX=f"{self.socket},0,0")
        handler = self.plugin_dir / "tmux-continuum/scripts/handle_tmux_automatic_start.sh"
        result = self.run_script(handler, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.agents / "Tmux.Start.plist").exists())
        for path, (data, mtime) in ours.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(sorted(entry.name for entry in self.agents.iterdir()),
                         sorted(f"{label}.plist" for label in LABELS))
        self.assertFalse((self.home / "launchctl.log").exists())
        # resurrect.conf asked systemctl once (its Linux line); continuum's macOS
        # branch never touches the unit.
        self.assertEqual((self.home / "systemctl.log").read_text().splitlines(),
                         ["--user is-enabled tmux.service"])


# Records how it was called: "$0 $*", so the path the caller used is kept.
ARGV0_TMUX = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$HOME/tmux-argv"
case $1 in
  has-session) exit 1 ;;
esac
exit 0
"""

# Apple's ps(1) as continuum calls it, `ps -u <uid> -o "command pid"`: the
# lines of $HOME/apple-ps.
APPLE_PS = """#!/bin/sh
cat "$HOME/apple-ps"
"""


def apple_ps_line(command: str, pid: int) -> str:
    """A `ps -o "command pid"` line as macOS prints it: a command column that
    is not the last one is cut to 16 characters (adv_cmds ps)."""
    return f"{command[:16]:<16} {pid:>5}"


class ContinuumProcessCountOnMacOS(ScriptRun):
    """Why tmux-launchd-start runs tmux by its absolute path: continuum's
    "another tmux server" check with Apple's ps output."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_dir = pinned_plugins("tmux-continuum")

    def verdict(self, lines, server_pid, bash):
        """continuum_restore.sh's decision for this process listing."""
        (self.home / "apple-ps").write_text("\n".join(lines) + "\n")
        write_executable(self.bin / "ps", APPLE_PS)
        helpers = self.plugin_dir / "tmux-continuum/scripts/helpers.sh"
        result = subprocess.run(
            [bash, "-c", 'source "$1"; if another_tmux_server_running_on_startup; '
             'then echo skip; else echo restore; fi', "-", str(helpers)],
            env=self.base_env(TMUX=f"/private/tmp/tmux-501/default,{server_pid},0"),
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_launchd_bootstrap_is_not_counted_and_a_bare_one_is(self):
        write_executable(self.bin / "tmux", ARGV0_TMUX)
        result = self.run_script(START, self.base_env())
        self.assertEqual(result.returncode, 0, result.stderr)  # no snapshot
        (bootstrap,) = [line for line in
                        (self.home / "tmux-argv").read_text().splitlines()
                        if " new-session " in line]
        self.assertEqual(bootstrap, f"{self.bin}/tmux new-session -d -s {BOOTSTRAP}")
        # While the config loads: the server (on macOS it keeps the argv of
        # the client that started it), that client, and one transient client
        # such as continuum.tmux's own `tmux show-option`.
        transient = "tmux show-option -gqv status-right"
        cases = (
            ("tmux-launchd-start", bootstrap, "restore"),
            ("bare tmux", f"tmux new-session -d -s {BOOTSTRAP}", "skip"),
        )
        bashes = [("bash", "bash")]
        if bash32():
            bashes.append(("bash-3.2", bash32()))
        for bash_name, bash in bashes:
            for name, client, expected in cases:
                with self.subTest(name, shell=bash_name):
                    lines = ["COMMAND            PID",
                             apple_ps_line(client, 4242),
                             apple_ps_line(client, 4243),
                             apple_ps_line(transient, 4250)]
                    self.assertEqual(self.verdict(lines, 4242, bash), expected)


# macOS tools for statusbar.tmux's CPU and memory components.
FAKE_IOSTAT = """#!/bin/sh
# `iostat -c 60 -w 2`: headers, the since-boot sample, then each interval.
cat <<'EOF'
              disk0       cpu    load average
    KB/t  tps  MB/s  us sy id   1m   5m   15m
   21.21   10  0.21   5  3 92  1.20 1.10 1.00
   21.21   10  0.21  70 35  0  1.20 1.10 1.00
EOF
"""

FAKE_VM_STAT = """#!/bin/sh
cat <<'EOF'
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               20000.
Pages active:                            400000.
Pages inactive:                          300000.
Pages speculative:                        10000.
Pages throttled:                              0.
Pages wired down:                        200000.
EOF
"""

FAKE_SYSCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/sysctl.log"
case "$*" in
  "-n hw.memsize") echo 17179869184 ;;
  *) echo "sysctl: unknown oid '$*'" >&2; exit 1 ;;
esac
"""

FAKE_SYSTEM_PROFILER = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/system_profiler.log"
echo "      Memory: 16 GB"
"""

MEM_BYTES = "17179869184"  # 16 GiB


class StatusbarOnMacOS(PrivateServer):
    """statusbar.tmux where `uname` says Darwin, under bash 3.2 as well
    (macOS's /bin/bash; the script's `#!/usr/bin/env bash` finds it unless
    Homebrew's bash comes first)."""

    STATUSBAR = TMUX_DIR / "statusbar.tmux"

    def setUp(self) -> None:
        super().setUp()
        self.fakes = self.root / "fakes"
        self.fakes.mkdir()
        for name, text in (("uname", FAKE_UNAME), ("iostat", FAKE_IOSTAT),
                           ("vm_stat", FAKE_VM_STAT), ("sysctl", FAKE_SYSCTL),
                           ("system_profiler", FAKE_SYSTEM_PROFILER),
                           ("pagesize", "#!/bin/sh\necho 16384\n")):
            write_executable(self.fakes / name, text)

    def darwin_env(self, **extra):
        return self.base_env(PATH=f"{self.fakes}:{self.bin}:/usr/bin:/bin", **extra)

    def statusbar(self, shell, *args, env=None, **extra):
        """statusbar.tmux [component], as a #() job or the config runs it."""
        return subprocess.run(
            [*shell, str(self.STATUSBAR), *args], env=env or self.darwin_env(**extra),
            cwd=self.root, capture_output=True, text=True, timeout=60)

    def logged(self, name):
        path = self.home / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_cpu_at_100_percent_or_more_takes_the_last_color(self):
        self.start_plain_server()
        for name, shell in bash_shells():
            with self.subTest(name):
                result = self.statusbar(shell, "component-cpu", "-S", "main")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")  # no "bad array subscript"
                self.assertEqual(result.stdout.splitlines(), [
                    "#[bg=#1c1c1c,fg=#8e2424,nobold,nounderscore,noitalics]\ue0b2"
                    "#[bg=#8e2424,fg=white] \U000f0ee0 105 % #[default]"])

    def test_ram_uses_hw_memsize_never_system_profiler(self):
        self.start_plain_server()
        expected = "#[bg=#755515,fg=white] \U000f035b 9.2/16 GB #[default]"
        for name, shell in bash_shells():
            with self.subTest(name):
                for path in (self.home / "sysctl.log", self.home / "system_profiler.log"):
                    path.unlink(missing_ok=True)
                # Every refresh: the size main() handed over, no tool at all.
                result = self.statusbar(shell, "component-ram", "-S", "main",
                                        STATUSBAR_MEM_BYTES=MEM_BYTES)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(self.logged("sysctl"), [])
                # Without it (an older status line): one cheap sysctl.
                result = self.statusbar(shell, "component-ram", "-S", "main")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(self.logged("sysctl"), ["-n hw.memsize"])
                self.assertEqual(self.logged("system_profiler"), [])

    def ram_job(self):
        status_right = self.option("status-right")
        jobs = re.findall(r"#\(([^)]*component-ram[^)]*)\)", status_right)
        self.assertEqual(len(jobs), 1, status_right)
        return jobs[0]

    def test_main_reads_the_memory_size_once_for_the_ram_job(self):
        self.start_plain_server()
        for name, shell in bash_shells():
            with self.subTest(name):
                (self.home / "sysctl.log").unlink(missing_ok=True)
                result = self.statusbar(shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.ram_job(),
                    f"STATUSBAR_MEM_BYTES={MEM_BYTES} {self.STATUSBAR} component-ram -S main")
                self.assertEqual(self.logged("sysctl"), ["-n hw.memsize"])
                self.assertEqual(self.logged("system_profiler"), [])

    @unittest.skipIf(sys.platform == "darwin", "the host itself is macOS")
    def test_linux_ram_job_is_unchanged(self):
        self.start_plain_server()
        result = self.statusbar([], env=self.base_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ram_job(), f"{self.STATUSBAR} component-ram -S main")


class BootLines(unittest.TestCase):
    """resurrect.conf's continuum boot choice: the Linux line as it always
    was, then the macOS override."""

    LINUX = (
        """if-shell 'c="${XDG_CONFIG_HOME:-}"; case "$c" in /*) ;; *) """
        """c="$HOME/.config" ;; esac; test -f "$c/systemd/user/tmux.service" && """
        """systemctl --user is-enabled tmux.service >/dev/null 2>&1' """
        """"set -g @continuum-boot 'on'" "set -g @continuum-boot 'off'\""""
    )
    DARWIN = """if-shell 'test "$(uname -s)" = Darwin' "set -g @continuum-boot 'off'\""""

    def test_linux_line_is_unchanged_and_macos_overrides_it(self):
        lines = (TMUX_DIR / "resurrect.conf").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count(self.LINUX), 1)
        self.assertEqual(lines.count(self.DARWIN), 1)
        self.assertLess(lines.index(self.LINUX), lines.index(self.DARWIN))
        self.assertLess(lines.index(self.DARWIN),
                        lines.index("set -g @continuum-systemd-start-cmd "
                                    "'new-session -d -s __continuum_startup'"))


if __name__ == "__main__":
    unittest.main()
