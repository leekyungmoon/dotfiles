"""Exercise the pinned upstream tmux-resurrect/continuum with this repo's files.

Isolation: every tmux server here is private (-S <socket in a temp dir>), the
inherited TMUX/TMUX_PANE are dropped, and the environment is rebuilt from
scratch with a temporary HOME/XDG tree.  Upstream scripts and the hooks call a
bare `tmux`; they reach only the private server through the TMUX variable set
below (TMUX_TMPDIR also points into the temp dir).  `claude`, `codex`,
`systemctl` and continuum's view of `ps -u` are fakes on PATH, so no real AI
session, user manager or live tmux server is touched or counted.  The pinned
plugins are cloned read-only from GitHub; the tests skip when that is
impossible.

TMUX_TEST_BINARY selects the tmux to test (for example an extracted Ubuntu
22.04 tmux 3.2a); the default is the first tmux on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TMUX_DIR = REPO_ROOT / "tmux"
PLUGINS = ("tpm", "tmux-resurrect", "tmux-continuum")
ID_A = "11111111-2222-4333-8444-555555555555"
ID_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SHELL = "bash --noprofile --norc -i"
BOOTSTRAP = "__continuum_startup"
RESTORE_STATE = "@tmux-restore-complete"
SKIPPED = 75

FAKE_AI = """#!/bin/bash
printf '%s\\n' "$(basename "$0") $*" >> "$HOME/ai-trace"
exec -a "$0" python3 -c 'import time; time.sleep(600)' "$@"
"""
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOME/systemctl.log"
case "$*" in
    *is-enabled*) [ -e "$HOME/systemctl-enabled" ] ;;
    *) exit 0 ;;
esac
"""
# continuum skips auto-restore when `ps -u <uid>` shows another tmux server;
# show it only this test's processes (those naming the private socket).
FAKE_PS = """#!/bin/sh
case " $* " in
    *" -u "*) {real} "$@" | awk -v s={socket} 'NR == 1 || index($0, s)'; exit 0 ;;
esac
exec {real} "$@"
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def tmux_version(binary: str) -> tuple[int, int]:
    output = subprocess.run(
        [binary, "-V"], capture_output=True, text=True, check=True
    ).stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    assert match is not None, output
    return int(match.group(1)), int(match.group(2))


def unit_path_value(home: Path) -> str:
    """Environment=PATH of tmux.service with %h expanded."""
    text = (REPO_ROOT / "systemd/user/tmux.service").read_text(encoding="utf-8")
    match = re.search(r"^Environment=PATH=(\S+)$", text, re.MULTILINE)
    assert match is not None
    return match.group(1).replace("%h", str(home))


class PinnedPlugins(unittest.TestCase):
    plugin_dir: Path
    tmux_binary: str
    _class_tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        tmux = os.environ.get("TMUX_TEST_BINARY") or shutil.which("tmux")
        missing = [
            name for name in ("git", "bash", "flock", "python3", "ps")
            if shutil.which(name) is None
        ]
        if tmux is None or missing:
            raise unittest.SkipTest(f"missing tools: tmux={tmux} {missing}")
        cls.tmux_binary = tmux
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="tmux-restore-plugins-")
        cls.plugin_dir = Path(cls._class_tmp.name) / "plugins"
        manifest = json.loads(
            (REPO_ROOT / "manifests/tmux-plugins.json").read_text(encoding="utf-8")
        )
        pins = {plugin["name"]: plugin for plugin in manifest["plugins"]}
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": cls._class_tmp.name,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        for name in PLUGINS:
            dest = cls.plugin_dir / name
            try:
                subprocess.run(
                    ["git", "clone", "--quiet", "--no-checkout",
                     pins[name]["url"], str(dest)],
                    env=git_env, check=True, capture_output=True, timeout=180,
                )
                subprocess.run(
                    ["git", "-C", str(dest), "checkout", "--quiet",
                     pins[name]["commit"]],
                    env=git_env, check=True, capture_output=True, timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                cls._class_tmp.cleanup()
                raise unittest.SkipTest(f"cannot clone pinned {name}: {error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_tmp.cleanup()

    # shared helpers ----------------------------------------------------------

    def make_tree(self, data: Path) -> None:
        root = Path(self.tmp.name)
        self.root = root
        self.socket = str(root / "socket")
        self.data = data
        self.config = self.home / ".config"
        self.bin = root / "bin"
        self.work = root / "work"
        for directory in (self.home, self.config, self.bin, self.data / "tmux"):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("alpha", "beta", "gamma", "delta"):
            (self.work / name).mkdir(parents=True)
        (self.home / ".dotfiles").symlink_to(REPO_ROOT)
        (self.home / ".tmux").symlink_to(TMUX_DIR)
        (self.data / "tmux/plugins").symlink_to(self.plugin_dir)
        self.state = self.data / "tmux/resurrect"
        (self.bin / "tmux").symlink_to(self.tmux_binary)
        write_executable(self.bin / "claude", FAKE_AI)
        write_executable(self.bin / "codex", FAKE_AI)
        write_executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        write_executable(self.bin / "ps", FAKE_PS.format(
            real=shlex.quote(shutil.which("ps") or "/bin/ps"),
            socket=shlex.quote(self.socket),
        ))

    def run_command(self, argv, check=True, timeout=60, env=None, cwd=None):
        return subprocess.run(
            [str(arg) for arg in argv], env=env or self.env, check=check,
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )

    def tmux(self, *args, check=True):
        return self.run_command(
            [self.tmux_binary, "-S", self.socket, *args], check=check
        ).stdout.strip()

    def option(self, name):
        return self.tmux("show-option", "-gqv", name)

    def plugin_script(self, plugin, relative):
        return self.data / "tmux/plugins" / plugin / relative

    def save(self, check=True):
        return self.run_command([TMUX_DIR / "resurrect-save"], check=check)

    def restore(self, env=None):
        return self.run_command(
            [self.plugin_script("tmux-resurrect", "scripts/restore.sh")], env=env
        )

    def sessions(self):
        return sorted(self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def panes(self):
        return sorted(self.tmux(
            "list-panes", "-a", "-F",
            "#{session_name}|#{window_index}|#{pane_index}|#{pane_current_path}",
        ).splitlines())

    def window_names(self):
        return sorted(self.tmux(
            "list-windows", "-a", "-F",
            "#{session_name}|#{window_index}|#{window_name}",
        ).splitlines())

    def wait_until(self, predicate, what, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def snapshot_target(self):
        return os.readlink(self.state / "last")


class ResurrectIntegration(PinnedPlugins):
    """resurrect.conf alone in a server with a fixed test shell."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tmux-restore-test-")
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.make_tree(root / "data")
        # Built from scratch: nothing from the live session (TMUX, TMUX_PANE,
        # DISPLAY, CODEX_HOME, ...) leaks into the private server.
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "SHELL": "/bin/bash",
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TMUX_TMPDIR": str(root),
            "TMUX": f"{self.socket},0,0",
        }
        self.bootstrap()

    def tearDown(self) -> None:
        self.tmux("kill-server", check=False)
        time.sleep(0.1)
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------

    def bootstrap(self):
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "bootstrap", SHELL)
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        for name, value in {
            "base-index": "1",
            "pane-base-index": "1",
            "default-shell": "/bin/bash",
            "default-command": SHELL,
        }.items():
            self.tmux("set-option", "-g", name, value)

    def geometry(self):
        return sorted(self.tmux(
            "list-panes", "-a", "-F",
            "#{session_name}|#{window_index}|#{pane_index}|#{pane_left}|"
            "#{pane_top}|#{pane_width}|#{pane_height}",
        ).splitlines())

    def trace(self):
        path = self.home / "ai-trace"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def wait_for_trace(self, expected, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.trace()) >= expected:
                time.sleep(0.5)  # let the processes exec; catch extra launches
                return self.trace()
            time.sleep(0.1)
        self.fail(f"AI launches not observed: {self.trace()!r}")

    def fixture(self, with_ai=False):
        self.tmux("new-session", "-d", "-s", "shell", "-c", self.work / "alpha")
        self.tmux("split-window", "-d", "-t", "shell:1", "-c", self.work / "beta")
        self.tmux("new-session", "-d", "-s", "weekly", "-c", self.work / "gamma")
        self.tmux("new-window", "-d", "-t", "weekly:2", "-c", self.work / "delta")
        if with_ai:
            self.tmux("new-session", "-d", "-s", "ai", "-n", "claude-a",
                      "-c", self.work / "alpha")
            self.tmux("new-window", "-d", "-t", "ai:2", "-n", "codex-b",
                      "-c", self.work / "beta")
            self.tmux("new-window", "-d", "-t", "ai:3", "-n", "claude-unknown",
                      "-c", self.work / "gamma")
            self.tmux("send-keys", "-t", "ai:1.1", f"claude --resume {ID_A}", "Enter")
            self.tmux("send-keys", "-t", "ai:2.1", f"codex resume {ID_B}", "Enter")
            self.tmux("send-keys", "-t", "ai:3.1", "claude", "Enter")
            self.wait_for_trace(3)
        self.tmux("kill-session", "-t", "bootstrap")

    def restart_server(self):
        self.tmux("kill-server")
        time.sleep(0.2)
        self.bootstrap()

    # tests -----------------------------------------------------------------

    def test_config_resolves_portable_paths_and_keeps_wrapper_binding(self):
        self.assertEqual(self.option("@resurrect-dir"), str(self.state))
        self.assertEqual(self.option("@continuum-save-interval"), "0")
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(self.option("@continuum-restore"), "on")
        for hook, expected in (
            ("@resurrect-hook-post-save-layout",
             str(self.home / ".dotfiles/tmux/resurrect-ai-session.py")),
            ("@resurrect-hook-pre-restore-all",
             f"{self.home / '.tmux/resurrect-save'} --pre-restore"),
            ("@resurrect-hook-post-restore-all",
             f"{self.home / '.tmux/resurrect-save'} --post-restore"),
        ):
            with self.subTest(hook=hook):
                self.assertEqual(
                    self.run_command(
                        ["bash", "-c", 'eval "printf \'%s \' $1"', "-",
                         self.option(hook)]
                    ).stdout.strip(),
                    expected,
                )
        # Plugin load (what TPM runs) must not replace the wrapper binding.
        self.run_command([self.plugin_script("tmux-resurrect", "resurrect.tmux")])
        save_key = self.tmux("list-keys", "-T", "prefix", "C-s")
        self.assertIn("resurrect-save", save_key)
        self.assertNotIn("save.sh", save_key)
        self.assertIn("restore.sh", self.tmux("list-keys", "-T", "prefix", "C-r"))

    def test_default_command_survives_resurrect_exec_prefix(self):
        # SEC-1: resurrect starts a pane with saved contents as
        # "cat <file>; exec <default-command>" through the default shell.
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        command = self.option("default-command")
        if shutil.which("zsh", path=self.env["PATH"]):
            self.assertEqual(command, "zsh -il")
        else:
            self.assertEqual(command, "")
        for shell in ("sh", "bash"):
            with self.subTest(shell=shell):
                self.run_command([shell, "-n", "-c", f"cat '/x'; exec {command or 'sh'}"])

    def test_continuum_boot_never_writes_or_toggles_the_unit(self):
        pid1 = subprocess.run(["ps", "-o", "comm=", "-p", "1"],
                              capture_output=True, text=True).stdout.strip()
        if pid1 != "systemd":
            self.skipTest("continuum's boot handling only runs under systemd")
        handler = self.plugin_script(
            "tmux-continuum", "scripts/handle_tmux_automatic_start.sh"
        )
        unit = self.config / "systemd/user/tmux.service"
        log = self.home / "systemctl.log"

        def continuum_calls():
            log.unlink(missing_ok=True)
            self.run_command([handler])
            return log.read_text().splitlines() if log.exists() else []

        # No managed unit: "off", continuum's disable is a no-op, nothing written.
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(continuum_calls(), ["--user disable tmux.service"])
        self.assertFalse(unit.exists())

        # Managed and enabled: "on", continuum only checks is-enabled.
        unit.parent.mkdir(parents=True)
        unit_bytes = (REPO_ROOT / "systemd/user/tmux.service").read_bytes()
        unit.write_bytes(unit_bytes)
        (self.home / "systemctl-enabled").touch()
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        self.assertEqual(self.option("@continuum-boot"), "on")
        self.assertEqual(continuum_calls(), ["--user is-enabled tmux.service"])
        self.assertEqual(unit.read_bytes(), unit_bytes)

        # Disabled by the user: stays disabled (only a no-op disable).
        (self.home / "systemctl-enabled").unlink()
        self.tmux("source-file", TMUX_DIR / "resurrect.conf")
        self.assertEqual(self.option("@continuum-boot"), "off")
        self.assertEqual(continuum_calls(), ["--user disable tmux.service"])
        self.assertEqual(unit.read_bytes(), unit_bytes)

    def test_headless_roundtrip_restores_layout_paths_and_proven_ai(self):
        self.fixture(with_ai=True)
        expected = self.panes()
        geometry = self.geometry()
        names = self.window_names()
        self.assertEqual(len(expected), 7)
        self.assertEqual(self.tmux("list-clients"), "")
        self.save()

        last = self.state / "last"
        self.assertTrue(last.is_file())
        self.assertTrue((self.state / "pane_contents.tar.gz").is_file())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(last.stat().st_mode), 0o600)
        commands = {
            (fields[1], fields[2]): fields[10]
            for fields in (line.split("\t") for line in last.read_text().splitlines())
            if fields[0] == "pane"
        }
        self.assertEqual(commands[("ai", "1")], f":{self.bin}/claude --resume {ID_A}")
        self.assertEqual(commands[("ai", "2")], f":codex resume {ID_B}")
        self.assertEqual(commands[("ai", "3")], ":")

        self.restart_server()
        (self.home / "ai-trace").unlink()
        self.restore()
        self.tmux("kill-session", "-t", "bootstrap")
        self.assertEqual(self.panes(), expected)
        self.assertEqual(self.geometry(), geometry)
        self.assertEqual(self.window_names(), names)
        self.assertEqual(self.tmux("list-clients"), "")
        # Only proven IDs are resumed; the unresolved Claude pane stays a shell.
        self.assertEqual(sorted(self.wait_for_trace(2)), sorted([
            f"claude --resume {ID_A}", f"codex resume {ID_B}",
        ]))

        before = self.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        self.restore()
        self.assertEqual(
            self.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"), before
        )
        time.sleep(0.5)
        self.assertEqual(len(self.trace()), 2)

    def test_failed_hook_keeps_previous_snapshot(self):
        self.fixture()
        self.tmux("set-environment", "-g", "TMUX_PLUGIN_MANAGER_PATH",
                  f"{self.data}/tmux/plugins/")
        self.save()
        previous = os.readlink(self.state / "last")
        previous_data = (self.state / "last").read_bytes()
        self.tmux("new-window", "-d", "-t", "weekly:3")
        self.tmux("set-option", "-g", "@resurrect-hook-post-save-layout", "/bin/false")
        time.sleep(1.1)  # a new one-second snapshot name
        failed = self.save(check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("previous snapshot preserved", failed.stderr)
        self.assertEqual(os.readlink(self.state / "last"), previous)
        self.assertEqual((self.state / "last").read_bytes(), previous_data)

    def test_login_bootstrap_is_removed_only_after_restore(self):
        self.fixture()
        expected = self.panes()
        self.save()
        self.restart_server()
        self.tmux("rename-session", "-t", "bootstrap", BOOTSTRAP)
        hook = self.option("@resurrect-hook-post-restore-all")
        self.assertIn("resurrect-save", hook)
        # With nothing restored yet, cleanup must keep the only live session.
        self.run_command(["/bin/bash", "-c", hook])
        self.assertEqual(self.sessions(), [BOOTSTRAP])
        self.restore()  # runs the same hooks via resurrect around the restore
        self.assertEqual(self.panes(), expected)
        self.assertEqual(self.tmux("list-clients"), "")

    def test_restore_keeps_saved_windows_of_the_login_session(self):
        # REQ-1: work done in __continuum_startup itself is part of the
        # snapshot; the login cleanup must not delete it after the restore.
        self.tmux("rename-session", "-t", "bootstrap", BOOTSTRAP)
        self.tmux("new-window", "-d", "-t", f"{BOOTSTRAP}:2", "-n", "important",
                  "-c", self.work / "beta")
        self.tmux("new-session", "-d", "-s", "work", "-c", self.work / "alpha")
        expected = self.panes()
        names = self.window_names()
        self.save()
        self.restart_server()
        self.tmux("rename-session", "-t", "bootstrap", BOOTSTRAP)
        self.restore()
        self.assertEqual(self.window_names(), names)
        self.assertEqual(self.panes(), expected)
        self.assertEqual(self.sessions(), [BOOTSTRAP, "work"])

    def test_manual_restore_later_never_deletes_the_login_session(self):
        # The cleanup belongs to the login restore only: a later prefix + C-r
        # leaves a (possibly used) __continuum_startup alone.
        self.fixture()
        self.save()
        self.restart_server()
        self.tmux("rename-session", "-t", "bootstrap", BOOTSTRAP)
        # A clock far past the server start: this is not the login restore.
        (self.root / "later").mkdir()
        write_executable(self.root / "later/date", "#!/bin/sh\necho 4102444800\n")
        later = dict(self.env, PATH=f"{self.root / 'later'}:{self.env['PATH']}")
        self.restore(env=later)
        self.assertEqual(self.sessions(), [BOOTSTRAP, "shell", "weekly"])
        self.assertEqual(self.option(RESTORE_STATE), "on")

    def test_save_waits_until_this_server_restored_last(self):
        # DS-5: a fresh server that has not restored must not replace "last".
        self.fixture()
        self.save()
        target = self.snapshot_target()
        data = (self.state / "last").read_bytes()
        self.assertEqual(self.option(RESTORE_STATE), "on")
        self.restart_server()
        self.tmux("rename-session", "-t", "bootstrap", BOOTSTRAP)
        time.sleep(1.1)
        skipped = self.save(check=False)
        self.assertEqual(skipped.returncode, SKIPPED, skipped.stderr)
        self.assertIn("not restored", skipped.stderr)
        self.assertEqual(self.snapshot_target(), target)
        self.assertEqual((self.state / "last").read_bytes(), data)

        # After the restore (post-restore hook) saves go through again.
        self.restore()
        self.assertEqual(self.option(RESTORE_STATE), "on")
        self.tmux("new-window", "-d", "-t", "weekly:5")
        time.sleep(1.1)
        self.save()
        self.assertNotEqual(self.snapshot_target(), target)

    def test_explicit_opt_in_allows_saving_without_restore(self):
        self.fixture()
        self.save()
        target = self.snapshot_target()
        self.restart_server()
        self.tmux("set-option", "-g", RESTORE_STATE, "on")
        self.tmux("new-window", "-d", "-t", "bootstrap:5")
        time.sleep(1.1)
        self.save()
        self.assertNotEqual(self.snapshot_target(), target)

    def test_timestamp_alone_never_unlocks_saving(self):
        # DS-5: without a restore state, no save may replace "last" -- not even
        # when @continuum-save-last-timestamp looks recent, because continuum
        # writes that option at plugin load too (e.g. after a config reload on
        # a server that skipped the login restore).
        self.fixture()
        self.save()
        target = self.snapshot_target()
        self.tmux("set-option", "-gu", RESTORE_STATE)
        start = int(self.tmux("display-message", "-p", "#{start_time}"))
        for stamp in (start, start + 3600):
            self.tmux("set-option", "-g", "@continuum-save-last-timestamp", str(stamp))
            self.tmux("new-window", "-d", "-t", f"weekly:{5 if stamp == start else 6}")
            time.sleep(1.1)
            self.assertEqual(self.save(check=False).returncode, SKIPPED)
            self.assertEqual(self.snapshot_target(), target)
        # The documented escape hatch: mark this server's state as the one to keep.
        self.tmux("set-option", "-g", RESTORE_STATE, "on")
        time.sleep(1.1)
        self.save()
        self.assertNotEqual(self.snapshot_target(), target)

    def test_restored_ordinary_command_is_replayed_as_quoted_argv(self):
        # SEC-3: resurrect types the saved command into the pane shell.
        name = "a;touch PWNED;#"
        (self.work / "alpha" / name).write_text("line\n")
        self.tmux("new-session", "-d", "-s", "logs", "-c", self.work / "alpha")
        self.tmux("send-keys", "-t", "logs:1.1", f"tail -f '{name}'", "Enter")
        self.wait_until(
            lambda: self.tmux("display-message", "-p", "-t", "logs:1.1",
                              "#{pane_current_command}") == "tail",
            "tail to start",
        )
        self.tmux("kill-session", "-t", "bootstrap")
        self.save()
        rows = [
            line.split("\t")
            for line in (self.state / "last").read_text().splitlines()
            if line.startswith("pane\tlogs\t")
        ]
        self.assertEqual(rows[0][10], ":tail -f 'a;touch PWNED;#'")

        self.restart_server()
        self.restore()
        self.wait_until(
            lambda: self.tmux("display-message", "-p", "-t", "logs:1.1",
                              "#{pane_current_command}") == "tail",
            "restored tail",
        )
        time.sleep(0.5)
        self.assertFalse((self.work / "alpha/PWNED").exists())


class LoginRestoreEndToEnd(PinnedPlugins):
    """The full tmux.conf with TPM, started the way tmux.service starts it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tmux-login-test-")
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.make_tree(self.home / ".local/share")
        (self.home / ".tmux.conf").symlink_to(TMUX_DIR / "tmux.conf")
        # zsh -il in the panes: no new-user wizard, no global compinit.
        (self.home / ".zshenv").write_text("skip_global_compinit=1\n")
        (self.home / ".zshrc").write_text("PS1='%# '\n")
        self.tools = self.data / "personal-dotfiles/bin"
        self.tools.mkdir(parents=True)
        write_executable(self.tools / "fd", "#!/bin/sh\nexit 0\n")
        write_executable(self.bin / "fzf", '#!/bin/sh\npwd > "$HOME/fzf-cwd"\nexit 1\n')
        # What the user manager hands tmux.service: HOME, the unit's PATH
        # (behind the fakes), a login SHELL; no TMUX, no XDG_DATA_HOME.
        self.unit_env = {
            "PATH": f"{self.bin}:{unit_path_value(self.home)}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "SHELL": shutil.which("zsh") or "/bin/bash",
            "TMUX_TMPDIR": str(root),
        }
        # Scripts run from the test reach only the private server.
        self.env = dict(self.unit_env, TMUX=f"{self.socket},0,0")

    def tearDown(self) -> None:
        self.tmux("kill-server", check=False)
        time.sleep(0.1)
        self.tmp.cleanup()

    def start_like_unit(self):
        """ExecStart=/usr/bin/env tmux new-session -d -s __continuum_startup"""
        self.run_command(
            [self.tmux_binary, "-S", self.socket, "new-session", "-d", "-s", BOOTSTRAP],
            env=self.unit_env, cwd=self.home,
        )
        self.wait_until(
            lambda: self.option("@resurrect-restore-script-path") != "",
            "TPM to load tmux-resurrect",
        )
        # continuum restores from a background job (after a 1 s sleep) that
        # reaches the server by socket path; let it finish so a quick restart
        # in a test never receives the previous server's restore.
        self.wait_until(
            lambda: not self.continuum_jobs(), "continuum's restore job", timeout=60
        )

    def continuum_jobs(self):
        script = str(self.data / "tmux/plugins/tmux-continuum")
        jobs = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
                except OSError:
                    continue
                if script.encode() in cmdline and b"continuum_restore" in cmdline:
                    jobs.append(entry)
        return jobs

    def stop_server(self):
        self.tmux("kill-server")
        self.wait_until(
            lambda: self.run_command(
                [self.tmux_binary, "-S", self.socket, "has-session"], check=False,
            ).returncode != 0,
            "server exit",
        )

    def contents(self, target):
        # (-J keeps trailing spaces on tmux 3.2; the patterns allow them)
        return self.tmux("capture-pane", "-p", "-J", "-S", "-", "-t", target)

    def type_marker(self, target):
        marker = "MARK_" + re.sub(r"\W", "_", target)
        self.tmux("send-keys", "-t", target, f"echo {marker}", "Enter")
        self.wait_until(
            lambda: re.search(rf"^{marker}\s*$", self.contents(target), re.MULTILINE),
            f"marker in {target}",
        )
        return marker

    def workspace(self):
        """Saved work in the login session itself plus two other sessions."""
        self.tmux("new-window", "-d", "-t", f"{BOOTSTRAP}:2", "-n", "important",
                  "-c", self.work / "delta")
        self.tmux("new-session", "-d", "-s", "shell", "-n", "edit",
                  "-c", self.work / "alpha")
        self.tmux("split-window", "-d", "-t", "shell:1", "-c", self.work / "beta")
        self.tmux("new-session", "-d", "-s", "weekly", "-c", self.work / "gamma")
        markers = {
            target: self.type_marker(target)
            for target in (f"{BOOTSTRAP}:2.1", "shell:1.1", "shell:1.2", "weekly:1.1")
        }
        return markers

    def test_login_start_auto_restores_windows_paths_and_contents(self):
        # SEC-1 end to end: pane contents are captured, so every restored
        # pane is started through "cat <contents>; exec <default-command>".
        self.start_like_unit()
        self.assertEqual(self.option("@resurrect-capture-pane-contents"), "on")
        markers = self.workspace()
        panes = self.panes()
        names = self.window_names()
        self.save()
        self.assertTrue((self.state / "pane_contents.tar.gz").is_file())

        self.stop_server()
        self.start_like_unit()
        self.wait_until(
            lambda: self.option(RESTORE_STATE) == "on", "continuum auto-restore"
        )
        self.assertEqual(self.sessions(), [BOOTSTRAP, "shell", "weekly"])
        self.assertEqual(self.window_names(), names)
        self.assertEqual(self.panes(), panes)
        for target, marker in markers.items():
            with self.subTest(target=target):
                self.assertRegex(self.contents(target), rf"(?m)^{marker}\s*$")
        # The restored panes are live shells, not exited placeholders.
        time.sleep(1.0)
        self.assertEqual(self.panes(), panes)
        # Restored: automatic saves may now replace "last".
        target = self.snapshot_target()
        self.tmux("new-window", "-d", "-t", "weekly:5")
        time.sleep(1.1)
        self.save()
        self.assertNotEqual(self.snapshot_target(), target)

    def test_skipped_auto_restore_never_overwrites_last(self):
        # DS-5: halt file, then the 1-minute timer and tmux.service's stop.
        self.start_like_unit()
        self.workspace()
        self.save()
        target = self.snapshot_target()
        data = (self.state / "last").read_bytes()
        self.stop_server()
        (self.home / "tmux_no_auto_restore").touch()
        self.start_like_unit()
        time.sleep(3)
        self.assertEqual(self.sessions(), [BOOTSTRAP])
        time.sleep(1.1)
        for _ in range(2):  # timer run, then ExecStop
            result = self.save(check=False)
            self.assertEqual(result.returncode, SKIPPED, result.stderr)
        self.assertEqual(self.snapshot_target(), target)
        self.assertEqual((self.state / "last").read_bytes(), data)
        # The documented manual restore still has the full workspace.
        self.restore()
        self.assertEqual(self.sessions(), [BOOTSTRAP, "shell", "weekly"])
        self.assertIn(f"{BOOTSTRAP}|2|important", self.window_names())

    def test_unit_path_reaches_run_shell_tools(self):
        # PLAT-1: prefix-@ runs fd/fzf-preview.sh from the tools directory.
        self.start_like_unit()
        out = self.root / "which"
        self.tmux("run-shell", f"command -v fd > {out}")
        self.assertEqual(out.read_text().strip(), str(self.tools / "fd"))

    def test_file_picker_never_runs_the_directory_name(self):
        # SEC-6: a quote in the pane directory must not break out.
        if tmux_version(self.tmux_binary) < (3, 3):
            self.skipTest("tmux < 3.3 uses display-popup -d (no shell)")
        self.start_like_unit()
        evil = self.root / "x'; touch PWNED; echo '"
        evil.mkdir()
        self.tmux("new-session", "-d", "-s", "evil", "-c", evil)
        pane = self.tmux("display-message", "-p", "-t", "evil:1.1", "#{pane_id}")
        # The tmux >= 3.3 binding exactly as tmux.conf writes it, run for
        # that pane (what pressing prefix + @ there does).
        source = (TMUX_DIR / "tmux.conf").read_text(encoding="utf-8")
        match = re.search(
            r'^bind-key @ run-shell (".*?\n")$', source, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(match)
        conf = self.root / "picker.conf"
        conf.write_text(f"run-shell -t {pane} {match.group(1)}")
        self.tmux("source-file", conf)
        self.wait_until(lambda: (self.home / "fzf-cwd").exists(), "the picker")
        self.assertEqual((self.home / "fzf-cwd").read_text().strip(), str(evil))
        self.assertFalse((self.root / "PWNED").exists())
        self.assertFalse((self.home / "PWNED").exists())


if __name__ == "__main__":
    unittest.main()
