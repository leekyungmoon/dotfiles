#!/usr/bin/env python3
"""tmux-service-start (round 3) against fakes, plus an isolated real tmux server.

Unit tests run the script under ``env -i`` with a scratch HOME, fake
``systemctl``/``tmux`` recorders and a logging ``xauth`` wrapper (with optional
fault injection) first on PATH, and TMUX_SERVICE_TMUX pointing at the fake tmux.
The integration tests start a private server with ``tmux -S <socket> -f
/dev/null`` (never ~/.tmux.conf, whose continuum would restore real sessions) on
a socket inside a fresh temporary directory, and kill it only through that
socket.

Auth files are written and parsed here in the Xauthority wire format, with fake
cookies only.  Fixtures mimic mutter's per-login Xwayland file: a FamilyLocal
and a FamilyWild entry for the host, both *without* a display number.  No real
Xauthority file is read or printed.

Scratch trees are made with mkdtemp under TMPDIR and never deleted recursively:
point TMPDIR at a fresh ``mktemp -d`` directory and let it age out.  A unix
socket path must fit in 107 bytes, so set TMUX_SERVICE_TEST_SOCKET_DIR (or
TMPDIR) to a short directory.  TMUX_SERVICE_TEST_SLOW=1 also runs the ~20 s
test against real xauth lock timeouts.
"""
import collections
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tmux/tmux-service-start"
TMUX_CONF = SCRIPT.parent / "tmux.conf"
RESURRECT_SAVE = SCRIPT.parent / "resurrect-save"
UNIT_DIR = REPO_ROOT / "systemd/user"  # the shipped units, never the installed ones
XAUTH = shutil.which("xauth") or "/usr/bin/xauth"
TMUX_BIN = shutil.which("tmux") or "/usr/bin/tmux"  # what `command -v tmux` finds
ALLOWLIST = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE",
             "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS")
CLEANED = ("XAUTHORITY", "WAYLAND_DISPLAY", "DISPLAY")  # per-session, in order
BOOTSTRAP = ["new-session", "-d", "-s", "__continuum_startup"]
LIST_SESSIONS = ["list-sessions", "-F", "#{session_id}"]
EX_TEMPFAIL = 75  # start mode found a server it did not start
ALREADY_RUNNING = "a tmux server is already running"
DIAG = "tmux-service-start: "  # prefix of every diagnostic line


def not_ready_line(polls):
    return f"{DIAG}desktop session variables not ready after {polls} polls; continuing"


def kept_line(source):
    return f"{DIAG}X cookie not rebuilt; keeping {source}"


def unreadable_line(source, stable):
    return f"{DIAG}{source} is not readable; using {stable}"


COOKIE_A = "0123456789abcdef" * 2
COOKIE_B = "fedcba9876543210" * 2
COOKIE_C = "00112233445566778899aabbccddeeff"
COOKIE_D = "ffeeddccbbaa99887766554433221100"
COOKIES = (COOKIE_A, COOKIE_B, COOKIE_C, COOKIE_D)
BUS = "unix:path=/run/user/1000/bus"
HOST = "testhost"
OTHER_HOST = "otherhost"
FAMILY_LOCAL = 0x0100
FAMILY_WILD = 0xFFFF
MIT = b"MIT-MAGIC-COOKIE-1"
LOGS = ("systemctl.log", "tmux.log", "exec.log")
# External commands the script may run, wrapped only by the argv/env audit.
AUDITED = ("awk", "sed", "head", "grep", "find", "mv", "rm", "mktemp", "mkdir",
           "sleep", "printf", "cat")

FAKE_SYSTEMCTL = """#!{python} -I
import json, sys
state = {state!r}
with open(state + "/systemctl.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] != ["--user", "show-environment"]:
    sys.exit(2)
with open(state + "/systemctl.log") as log:
    calls = sum(1 for _ in log)
with open(state + "/dumps.json") as f:
    config = json.load(f)
if config["server_on_call"] == calls:
    # A tmux server appears while the script is still waiting.
    with open(state + "/server", "w") as f:
        f.write("1")
dumps = config["dumps"]
entry = dumps[min(calls, len(dumps)) - 1]
if config["fail"] or entry is None:
    sys.stderr.write("Failed to connect to bus\\n")
    sys.exit(1)
sys.stdout.write(entry)
"""

FAKE_TMUX = """#!{python} -I
import json, os, sys
state = {state!r}
argv = sys.argv[1:]
with open(state + "/tmux.log", "a") as log:
    log.write(json.dumps({{"argv": argv, "env": dict(os.environ)}}) + "\\n")
with open(state + "/server") as f:
    running = f.read().strip() == "1"
if argv[:1] == ["has-session"]:
    sys.exit(0 if running else 1)
if argv[:1] == ["list-sessions"]:
    with open(state + "/sessions.json") as f:
        sessions = json.load(f)
    if not running or sessions is None:
        sys.exit(1)
    sys.stdout.write("".join(line + "\\n" for line in sessions))
sys.exit(0)
"""

# Records argv and environment, then runs the real command, so tests can
# prove that cookies never reach a command line or an exported variable.
# For xauth, rules in xauth-faults.json make an `nmerge` into a matching file
# fail the way a real failure would, or ("slow") hang mid-merge with xauth's
# -c/-l lock and -n new-file siblings in place until the test signals.
FAKE_LOGGER = """#!{python} -I
import json, os, subprocess, sys, time
state = {state!r}
name = {name!r}
real = {real!r}
argv = sys.argv[1:]
with open(state + "/exec.log", "a") as log:
    log.write(json.dumps({{"cmd": name, "argv": argv,
                           "env": dict(os.environ)}}) + "\\n")
if name == "xauth" and "nmerge" in argv and "-f" in argv:
    target = argv[argv.index("-f") + 1]
    with open(state + "/xauth-faults.json") as f:
        rules = json.load(f)
    for rule in rules:
        if not target.startswith(rule["prefix"]):
            continue
        mode = rule["mode"]
        if mode == "lock-timeout":
            # Real xauth waits 10 x 2 s on a held lock unless -b breaks it.
            if "-b" not in argv and os.path.exists(target + "-c"):
                sys.stderr.write("xauth:  timeout in locking authority file "
                                 + target + "\\n")
                sys.exit(1)
        elif mode == "fail":
            sys.exit(1)
        elif mode == "garbage":
            with open(target, "wb") as f:
                f.write(bytes.fromhex("0100000c"))  # truncated entry
            sys.exit(1)
        elif mode == "partial":
            first = sys.stdin.read().splitlines(True)[:1]
            sys.exit(subprocess.run([real, *argv], input="".join(first),
                                    text=True).returncode)
        elif mode == "slow":
            for suffix in ("-c", "-l", "-n"):
                open(target + suffix, "w").close()
            open(rule["ready"], "w").close()
            time.sleep(rule["seconds"])
            sys.exit(1)
os.execv(real, [real, *argv])
"""

# Asks libXau (the matcher libX11/libxcb use) which entry of $XAUTHORITY a
# client connecting to FamilyLocal/<host>:<number> would send.
XAU_LOOKUP = r"""
import ctypes, ctypes.util, sys
lib = ctypes.CDLL(ctypes.util.find_library("Xau") or "libXau.so.6")
class Xauth(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort),
                ("address_length", ctypes.c_ushort), ("address", ctypes.c_void_p),
                ("number_length", ctypes.c_ushort), ("number", ctypes.c_void_p),
                ("name_length", ctypes.c_ushort), ("name", ctypes.c_void_p),
                ("data_length", ctypes.c_ushort), ("data", ctypes.c_void_p)]
lib.XauGetBestAuthByAddr.restype = ctypes.POINTER(Xauth)
host, number = sys.argv[1].encode(), sys.argv[2].encode()
name = b"MIT-MAGIC-COOKIE-1"
names = (ctypes.c_char_p * 1)(name)
lengths = (ctypes.c_int * 1)(len(name))
found = lib.XauGetBestAuthByAddr(0x0100, len(host), host, len(number), number,
                                 1, names, lengths)
if not found:
    print("none")
else:
    entry = found.contents
    print(ctypes.string_at(entry.data, entry.data_length).hex())
    lib.XauDisposeAuth(found)
"""

Entry = collections.namedtuple("Entry", "family address number cookie")


def encode_auth(entries):
    out = bytearray()
    for entry in entries:
        out += struct.pack(">H", entry.family)
        for field in (entry.address.encode(), entry.number.encode(), MIT,
                      bytes.fromhex(entry.cookie)):
            out += struct.pack(">H", len(field)) + field
    return bytes(out)


def decode_auth(data):
    entries, offset = [], 0

    def take():
        nonlocal offset
        (length,) = struct.unpack_from(">H", data, offset)
        field = data[offset + 2:offset + 2 + length]
        if len(field) != length:
            raise AssertionError("truncated Xauthority entry")
        offset += 2 + length
        return field

    while offset < len(data):
        (family,) = struct.unpack_from(">H", data, offset)
        offset += 2
        address, number, name, cookie = take(), take(), take(), take()
        if name != MIT:
            raise AssertionError(f"unexpected auth protocol {name!r}")
        entries.append(Entry(family, address.decode(), number.decode(), cookie.hex()))
    return entries


def nlist_line(entry):
    """The `xauth nlist` rendering of one entry."""
    fields = [f"{entry.family:04x}"]
    for value in (entry.address.encode(), entry.number.encode(), MIT,
                  bytes.fromhex(entry.cookie)):
        fields += [f"{len(value):04x}", value.hex()]
    return " ".join(fields)


def mutter_entries(cookie, host=HOST):
    """Xwayland's per-login file: FamilyLocal then FamilyWild, no number."""
    return [Entry(FAMILY_LOCAL, host, "", cookie), Entry(FAMILY_WILD, host, "", cookie)]


def bound(cookie, number="0", host=HOST):
    """The same entries rebound to one display number."""
    return [Entry(FAMILY_LOCAL, host, number, cookie),
            Entry(FAMILY_WILD, host, number, cookie)]


def dump(**values):
    """Render a `systemctl --user show-environment` block (sorted like systemd)."""
    return "".join(f"{name}={values[name]}\n" for name in sorted(values))


def set_g(name, value):
    return ["set-environment", "-g", name, value]


def unset_g(name):
    return ["set-environment", "-gu", name]


def session_cleanup(*session_ids):
    return [["set-environment", "-t", session_id, "-u", name]
            for session_id in session_ids for name in CLEANED]


def touch(path, when=None):
    command = ["/usr/bin/touch", *(["-d", when] if when else []), str(path)]
    subprocess.run(command, check=True, timeout=30)


def production_update_environment():
    for line in TMUX_CONF.read_text().splitlines():
        if line.startswith("set -g update-environment "):
            return shlex.split(line)[-1]
    raise AssertionError(f"no update-environment in {TMUX_CONF}")


def fingerprint(path):
    """Bytes plus identity of a file, to prove it was left untouched."""
    info = os.lstat(path)
    return (Path(path).read_bytes(), info.st_ino, info.st_mtime_ns,
            stat.S_IMODE(info.st_mode))


def snapshot(path):
    """fingerprint() of a file, or of a directory's mode and every entry."""
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        return fingerprint(path)
    return (stat.S_IMODE(os.lstat(path).st_mode),
            {name: snapshot(path / name) for name in sorted(os.listdir(path))})


class Harness(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tss-"))
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.run_dir = self.root / "run-user-1000"
        for path in (self.bin, self.state, self.run_dir):
            path.mkdir()
        self.use_home("home")
        self.fake_tmux = self.bin / "tmux"
        self.write_tool(self.bin / "systemctl", FAKE_SYSTEMCTL)
        self.write_tool(self.bin / "tmux", FAKE_TMUX)
        self.write_tool(self.bin / "xauth", FAKE_LOGGER, name="xauth", real=XAUTH)
        self.audit_dir = None
        for name in LOGS:
            (self.state / name).touch()
        self.set_dumps([dump()])
        self.set_sessions(["$0"])
        self.set_server_running(False)
        self.set_xauth_faults([])

    # -- fixtures --------------------------------------------------------
    def write_tool(self, path, template, **extra):
        path.write_text(template.format(python=sys.executable,
                                        state=str(self.state), **extra))
        path.chmod(0o755)

    def audit_bin(self):
        """Logging wrappers for every external command the script may use."""
        if self.audit_dir is None:
            self.audit_dir = self.root / "audit-bin"
            self.audit_dir.mkdir()
            for name in AUDITED:
                real = f"/usr/bin/{name}"
                if os.access(real, os.X_OK):
                    self.write_tool(self.audit_dir / name, FAKE_LOGGER,
                                    name=name, real=real)
        return self.audit_dir

    def use_home(self, name):
        """Switch to a fresh scratch HOME (one per subtest)."""
        self.home = self.root / name
        self.home.mkdir()
        self.home_auth = self.home / ".Xauthority"
        self.stable_dir = self.home / ".local/state/tmux"
        self.stable = self.stable_dir / "Xauthority"

    def fresh(self, name):
        """New HOME, empty logs, no server and no faults: one subtest."""
        self.use_home(name)
        self.clear_logs()
        self.set_server_running(False)
        self.set_xauth_faults([])

    def set_dumps(self, dumps, fail=False, server_on_call=None):
        (self.state / "dumps.json").write_text(json.dumps(
            {"dumps": list(dumps), "fail": fail, "server_on_call": server_on_call}))

    def set_sessions(self, session_ids):
        (self.state / "sessions.json").write_text(json.dumps(session_ids))

    def set_server_running(self, running):
        (self.state / "server").write_text("1" if running else "0")

    def set_xauth_faults(self, rules):
        (self.state / "xauth-faults.json").write_text(json.dumps(rules))

    def clear_logs(self):
        for name in LOGS:
            (self.state / name).write_text("")

    def write_auth(self, path, entries, mode=0o600):
        path.write_bytes(encode_auth(entries))
        path.chmod(mode)
        return path

    def mutter_auth(self, suffix, cookie=None, entries=None):
        path = self.run_dir / f".mutter-Xwaylandauth.{suffix}"
        return self.write_auth(path, entries if entries is not None
                               else mutter_entries(cookie))

    def make_stable(self, entries):
        self.stable_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        return self.write_auth(self.stable, entries)

    def full_dump(self, auth, **extra):
        values = {
            "DBUS_SESSION_BUS_ADDRESS": BUS,
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "XAUTHORITY": str(auth),
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "XDG_SESSION_TYPE": "wayland",
        }
        values.update(extra)
        return dump(**values)

    def restore_mode_later(self, path, mode=0o700):
        self.addCleanup(os.chmod, path, mode)

    # -- running ---------------------------------------------------------
    def script_argv(self, *args, polls=5, inherited=None, tmux=None, home=None,
                    shell=(), audit=False):
        path = f"{self.bin}:/usr/bin:/bin"
        if audit:
            path = f"{self.audit_bin()}:{path}"
        env = [
            f"PATH={path}",
            f"HOME={self.home if home is None else home}",
            # Never the live server: a fake or a private-socket wrapper always.
            f"TMUX_SERVICE_TMUX={tmux or self.fake_tmux}",
            f"TMUX_TMPDIR={self.root / 'tmux-tmp'}",
            "HISTFILE=/dev/null",
        ]
        if polls is not None:
            env.append(f"TMUX_SERVICE_ENV_POLLS={polls}")
        env += [f"{name}={value}" for name, value in (inherited or {}).items()]
        return ["/usr/bin/env", "-i", *env, *shell, str(SCRIPT), *args]

    def run_script(self, *args, umask=0o022, **kwargs):
        start = time.monotonic()
        result = subprocess.run(self.script_argv(*args, **kwargs),
                                capture_output=True, text=True, timeout=90,
                                umask=umask)
        result.elapsed = time.monotonic() - start
        self.last_result = result
        return result

    # -- observations ----------------------------------------------------
    def log_lines(self, name):
        text = (self.state / name).read_text()
        return [json.loads(line) for line in text.splitlines()]

    def systemctl_calls(self):
        return self.log_lines("systemctl.log")

    def tmux_calls(self):
        return self.log_lines("tmux.log")

    def tmux_argv(self):
        return [call["argv"] for call in self.tmux_calls()]

    def exec_records(self, cmd=None):
        return [record for record in self.log_lines("exec.log")
                if cmd is None or record["cmd"] == cmd]

    def xauth_calls(self):
        return [record["argv"] for record in self.exec_records("xauth")]

    def nmerge_targets(self):
        return [argv[argv.index("-f") + 1] for argv in self.xauth_calls()
                if "nmerge" in argv]

    def compat_merges(self):
        return [argv for argv in self.xauth_calls()
                if "nmerge" in argv and argv[argv.index("-f") + 1] == str(self.home_auth)]

    def bootstrap_env(self):
        calls = self.tmux_calls()
        self.assertEqual([call["argv"] for call in calls], [["has-session"], BOOTSTRAP])
        return calls[1]["env"]

    def read_auth(self, path):
        return decode_auth(Path(path).read_bytes())

    def xauth_nlist(self, path):
        return subprocess.run(
            ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", f"HOME={self.root}",
             XAUTH, "-f", str(path), "nlist"],
            check=True, capture_output=True, text=True, timeout=30).stdout.splitlines()

    def assert_auth(self, path, expected):
        """Exact content, as parsed here and as the real xauth reads it."""
        self.assertEqual(self.read_auth(path), list(expected))
        self.assertEqual(self.xauth_nlist(path), [nlist_line(e) for e in expected])

    def assert_auth_set(self, path, expected):
        entries = self.read_auth(path)
        self.assertEqual(sorted(entries), sorted(expected))

    def assert_no_tmp_leftovers(self):
        self.assertEqual(sorted(os.listdir(self.stable_dir)), ["Xauthority"])

    def assert_stderr(self, result, *lines):
        """Exactly these diagnostic lines, so no shell error slipped in."""
        self.assertEqual(result.stderr, "".join(f"{line}\n" for line in lines))

    def sync_calls(self, xauthority=True, session_ids=("$0",)):
        calls = [["has-session"], set_g("DISPLAY", ":0"),
                 set_g("WAYLAND_DISPLAY", "wayland-0")]
        if xauthority:
            calls.append(set_g("XAUTHORITY", str(self.stable)))
        calls += [set_g("XDG_SESSION_TYPE", "wayland"),
                  set_g("XDG_CURRENT_DESKTOP", "ubuntu:GNOME"),
                  set_g("DBUS_SESSION_BUS_ADDRESS", BUS),
                  LIST_SESSIONS, *session_cleanup(*session_ids)]
        return calls

    def xau_lookup(self, path, number, host=HOST):
        result = subprocess.run(
            ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", f"XAUTHORITY={path}",
             sys.executable, "-I", "-c", XAU_LOOKUP, host, number],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            self.skipTest(f"libXau unavailable: {result.stderr.strip()}")
        return result.stdout.strip()


class StartMode(Harness):
    def test_full_environment_exports_allowlist_and_stable_xauthority(self):
        auth = self.mutter_auth("AB12CD", COOKIE_A)
        source_before = fingerprint(auth)
        self.set_dumps([self.full_dump(
            auth, LANG="ko_KR.UTF-8", PATH="/usr/local/bin:/usr/bin",
            SSH_AUTH_SOCK="/run/user/1000/keyring/ssh",
            GNOME_SETUP_DISPLAY=":1", HOME="/home/elsewhere")])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.systemctl_calls(), [["--user", "show-environment"]])
        env = self.bootstrap_env()
        self.assertEqual({name: env.get(name) for name in ALLOWLIST}, {
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "XAUTHORITY": str(self.stable),
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "DBUS_SESSION_BUS_ADDRESS": BUS,
        })
        # Only the allowlist is imported; the service's own PATH/HOME stay.
        for name in ("LANG", "SSH_AUTH_SOCK", "GNOME_SETUP_DISPLAY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], f"{self.bin}:/usr/bin:/bin")
        self.assertEqual(env["HOME"], str(self.home))
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assert_auth(self.home_auth, bound(COOKIE_A))
        # The session's auth file is only read, never rewritten.
        self.assertEqual(fingerprint(auth), source_before)

    def test_systemd_escaped_values_are_never_exported(self):
        auth = self.mutter_auth("ESC", COOKIE_A)
        self.set_dumps([self.full_dump(
            auth,
            DBUS_SESSION_BUS_ADDRESS="$'unix:path=/run/user/1000/bus\\x3btouch /x'",
            XDG_CURRENT_DESKTOP="$'ubuntu:GNOME\\n'",
            DISPLAY="$':0\\x1b'")])
        result = self.run_script(inherited={"DISPLAY": ":0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.systemctl_calls()), 1)
        env = self.bootstrap_env()
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertNotIn("XDG_CURRENT_DESKTOP", env)
        self.assertEqual(env.get("DISPLAY"), ":0")  # inherited value kept
        self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
        for value in env.values():
            self.assertFalse(value.startswith("$'"), value)
        self.assert_auth(self.stable, bound(COOKIE_A))

    def test_escaped_wayland_variable_counts_as_missing(self):
        auth = self.mutter_auth("ESC2", COOKIE_A)
        self.set_dumps([self.full_dump(auth, WAYLAND_DISPLAY="$'wayland-0\\n'")])
        result = self.run_script(polls=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result, not_ready_line(2))
        self.assertEqual(len(self.systemctl_calls()), 2)
        env = self.bootstrap_env()
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))

    def test_server_present_at_start_is_synced_and_exits_75(self):
        # ExecCondition saw no server, but one exists by the time of the
        # re-check (e.g. a terminal started tmux first).
        auth = self.mutter_auth("RACE01", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        self.set_server_running(True)
        self.set_sessions(["$0", "$3"])
        result = self.run_script()
        self.assertEqual(result.returncode, EX_TEMPFAIL, result.stderr)
        self.assertIn(ALREADY_RUNNING, result.stderr)
        calls = self.tmux_argv()
        self.assertFalse([call for call in calls if call[:1] == ["new-session"]])
        self.assertEqual(calls, [["has-session"],
                                 *self.sync_calls(session_ids=("$0", "$3"))])
        self.assert_auth(self.stable, bound(COOKIE_A))

    def test_server_appearing_while_waiting_is_synced_and_exits_75(self):
        auth = self.mutter_auth("RACE02", COOKIE_A)
        early = dump(XDG_SESSION_TYPE="wayland", DBUS_SESSION_BUS_ADDRESS=BUS)
        half = dump(XDG_SESSION_TYPE="wayland", DBUS_SESSION_BUS_ADDRESS=BUS,
                    DISPLAY=":0", WAYLAND_DISPLAY="wayland-0")
        self.set_dumps([early, half, self.full_dump(auth)], server_on_call=2)
        result = self.run_script(polls=10)
        self.assertEqual(result.returncode, EX_TEMPFAIL, result.stderr)
        self.assertIn(ALREADY_RUNNING, result.stderr)
        self.assertEqual(len(self.systemctl_calls()), 3)
        calls = self.tmux_argv()
        self.assertNotIn(BOOTSTRAP, calls)
        # has-session is only asked after the wait, so it sees the new server.
        self.assertEqual(calls, [["has-session"], *self.sync_calls()])

    def test_server_present_with_unusable_manager_exits_75_without_sync(self):
        self.set_dumps([dump()], fail=True)
        self.set_server_running(True)
        result = self.run_script(polls=2, inherited={"DISPLAY": ":0"})
        self.assertEqual(result.returncode, EX_TEMPFAIL, result.stderr)
        self.assertIn(ALREADY_RUNNING, result.stderr)
        self.assertEqual(self.tmux_argv(), [["has-session"], ["has-session"]])

    def test_no_server_bootstraps_and_exits_with_tmux_status(self):
        auth = self.mutter_auth("BOOT", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    def test_script_parses_as_posix_sh(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(SCRIPT.read_text().splitlines()[0], "#!/bin/sh")
        for shell in ("/usr/bin/dash", "/bin/sh"):
            if Path(shell).exists():
                subprocess.run([shell, "-n", str(SCRIPT)], check=True, timeout=30)


class Readiness(Harness):
    def test_waits_for_late_wayland_variables(self):
        auth = self.mutter_auth("LATE01", COOKIE_A)
        early = dump(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="ubuntu:GNOME",
                     DBUS_SESSION_BUS_ADDRESS=BUS)
        half = dump(XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="ubuntu:GNOME",
                    DBUS_SESSION_BUS_ADDRESS=BUS, DISPLAY=":0",
                    WAYLAND_DISPLAY="wayland-0")  # XAUTHORITY still missing
        self.set_dumps([early, half, self.full_dump(auth)])
        result = self.run_script(polls=10, inherited={"DISPLAY": ":0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result)  # ready in time: no diagnostic
        self.assertEqual(len(self.systemctl_calls()), 3)
        self.assertGreaterEqual(result.elapsed, 0.9)  # two 0.5 s sleeps
        env = self.bootstrap_env()
        self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
        self.assert_auth(self.stable, bound(COOKIE_A))

    def test_never_arriving_environment_is_bounded_and_still_starts(self):
        # The incident state: the unit's DISPLAY=:0 only, GNOME never exports
        # WAYLAND_DISPLAY/XAUTHORITY.
        self.set_dumps([dump(XDG_SESSION_TYPE="wayland",
                             XDG_CURRENT_DESKTOP="ubuntu:GNOME",
                             DBUS_SESSION_BUS_ADDRESS=BUS)])
        result = self.run_script(polls=3, inherited={"DISPLAY": ":0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result, not_ready_line(3))
        self.assertEqual(len(self.systemctl_calls()), 3)
        self.assertGreaterEqual(result.elapsed, 0.9)
        self.assertLess(result.elapsed, 5.0)
        env = self.bootstrap_env()
        self.assertEqual(env.get("DISPLAY"), ":0")
        self.assertEqual(env.get("XDG_SESSION_TYPE"), "wayland")
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertNotIn("XAUTHORITY", env)
        self.assertFalse(self.stable.exists())
        self.assertFalse(self.home_auth.exists())

    def test_non_wayland_session_does_not_wait(self):
        cases = {
            "x11": (lambda: dump(XDG_SESSION_TYPE="x11", DISPLAY=":1",
                                 XAUTHORITY=str(self.mutter_auth(
                                     "X11", entries=bound(COOKIE_A, "1")))), ":1"),
            "no-session-yet": (lambda: dump(DBUS_SESSION_BUS_ADDRESS=BUS), ":0"),
        }
        for name, (make_dump, display) in cases.items():
            with self.subTest(name):
                self.fresh(f"home-{name}")
                self.set_dumps([make_dump()])
                result = self.run_script(polls=10, inherited={"DISPLAY": ":0"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result)
                self.assertEqual(len(self.systemctl_calls()), 1)
                self.assertLess(result.elapsed, 0.5)
                self.assertEqual(self.bootstrap_env().get("DISPLAY"), display)

    def test_pure_wayland_without_xwayland_is_ready_immediately(self):
        pure = dump(XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="wayland-0",
                    XDG_CURRENT_DESKTOP="ubuntu:GNOME", DBUS_SESSION_BUS_ADDRESS=BUS)
        # Readiness is judged on the dump: a unit-inherited DISPLAY=:0 must not
        # make a session without Xwayland wait for a cookie that never comes.
        for name, inherited in (("clean", {}), ("unit-display", {"DISPLAY": ":0"})):
            with self.subTest(name):
                self.fresh(f"home-{name}")
                self.set_dumps([pure])
                result = self.run_script(polls=10, inherited=inherited)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result)
                self.assertEqual(len(self.systemctl_calls()), 1)
                self.assertLess(result.elapsed, 0.5)
                env = self.bootstrap_env()
                self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
                self.assertEqual(env.get("DISPLAY"), inherited.get("DISPLAY"))
                self.assertNotIn("XAUTHORITY", env)
                self.assertFalse(self.stable.exists())
                self.assertFalse(self.home_auth.exists())
                self.assertEqual(self.xauth_calls(), [])

    def test_unreachable_or_empty_manager_retries_within_bound(self):
        cases = {"systemctl-fails": ([dump()], True), "empty-dump": ([""], False)}
        for name, (dumps, fail) in cases.items():
            with self.subTest(name):
                self.fresh(f"home-{name}")
                self.set_dumps(dumps, fail=fail)
                result = self.run_script(polls=3, inherited={"DISPLAY": ":0"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result, not_ready_line(3))
                self.assertEqual(len(self.systemctl_calls()), 3)
                self.assertGreaterEqual(result.elapsed, 0.9)
                self.assertLess(result.elapsed, 5.0)
                env = self.bootstrap_env()
                self.assertEqual(env.get("DISPLAY"), ":0")
                self.assertNotIn("WAYLAND_DISPLAY", env)
                self.assertNotIn("XAUTHORITY", env)
                self.assertFalse(self.stable.exists())

    def test_transient_manager_failure_then_ready(self):
        auth = self.mutter_auth("TRANS1", COOKIE_A)
        self.set_dumps([None, "", self.full_dump(auth)])  # fail, empty, ready
        result = self.run_script(polls=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.systemctl_calls()), 3)
        env = self.bootstrap_env()
        self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))

    def run_ready_on_second_call(self, value, name):
        """Ready on call 2: a bound of 0/1 stops after one call, the default 40
        takes two (with one 0.5 s sleep)."""
        self.fresh(name)
        auth = self.mutter_auth(name, COOKIE_A)
        early = dump(XDG_SESSION_TYPE="wayland", DBUS_SESSION_BUS_ADDRESS=BUS)
        self.set_dumps([early, self.full_dump(auth)])
        return self.run_script(polls=value)

    def test_invalid_poll_bound_falls_back_to_default(self):
        values = ("abc", "-1", "1.5", " 5", "4x", "+3", "", None,
                  # four or more digits are rejected before any arithmetic,
                  # including values beyond the shell's integer range
                  "0000", "0001", "1000", "99999999999999999999")
        for index, value in enumerate(values):
            with self.subTest(repr(value)):
                result = self.run_ready_on_second_call(value, f"home-polls-{index}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")  # no "Illegal number"
                self.assertEqual(len(self.systemctl_calls()), 2)
                self.assertGreaterEqual(result.elapsed, 0.45)
                self.assertLess(result.elapsed, 3.0)
                env = self.bootstrap_env()
                self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
                self.assertEqual(env.get("XAUTHORITY"), str(self.stable))

    def test_numeric_poll_bound_is_honoured(self):
        never = dump(XDG_SESSION_TYPE="wayland", DBUS_SESSION_BUS_ADDRESS=BUS)
        for value, calls in (("0", 1), ("1", 1), ("2", 2), ("03", 3), ("003", 3)):
            with self.subTest(value):
                self.fresh(f"home-bound-{value}")
                self.set_dumps([never])
                result = self.run_script(polls=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                # The bound was hit: the readiness line (with the real count)
                # is the only output, so no "Illegal number" either.
                self.assertNotIn("Illegal number", result.stderr)
                self.assert_stderr(result, not_ready_line(calls))
                self.assertEqual(len(self.systemctl_calls()), calls)
                self.assertGreaterEqual(result.elapsed, 0.5 * (calls - 1) - 0.05)
                self.assertEqual(self.bootstrap_env().get("XDG_SESSION_TYPE"),
                                 "wayland")

    def test_out_of_range_poll_bound_is_rejected_like_non_numeric(self):
        # Round 2 known defect (was expectedFailure): an all-digit value
        # beyond the shell's integer range made `[ -ge ]` fail on every
        # iteration.  Four or more digits now fall back to 40.
        result = self.run_ready_on_second_call("99999999999999999999", "home-huge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.systemctl_calls()), 2)
        self.assertEqual(result.stderr, "")
        # And a four-digit value is not honoured as its number: "0000" would
        # stop after the first call.
        result = self.run_ready_on_second_call("0000", "home-zeros")
        self.assertEqual(len(self.systemctl_calls()), 2)


class Rebinding(Harness):
    def run_display(self, display, source_entries, name):
        self.fresh(name)
        auth = self.mutter_auth(name, entries=source_entries)
        self.set_dumps([self.full_dump(auth, DISPLAY=display)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        return auth, self.bootstrap_env()

    def test_mutter_numberless_entries_are_rebound_to_the_display(self):
        for display, number in ((":0", "0"), (":0.0", "0"), (":10", "10"),
                                (":10.0", "10")):
            with self.subTest(display):
                auth, env = self.run_display(display, mutter_entries(COOKIE_A),
                                             f"home-{number}-{len(display)}")
                self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
                self.assert_auth(self.stable, bound(COOKIE_A, number))
                self.assert_auth(self.home_auth, bound(COOKIE_A, number))
                # The per-login file itself keeps its number-less entries.
                self.assertEqual(self.read_auth(auth), mutter_entries(COOKIE_A))

    def test_numbered_entries_for_other_displays_are_dropped(self):
        source = [Entry(FAMILY_LOCAL, HOST, "", COOKIE_A),
                  Entry(FAMILY_LOCAL, HOST, "1", COOKIE_C),
                  Entry(FAMILY_LOCAL, OTHER_HOST, "0", COOKIE_B),
                  Entry(FAMILY_LOCAL, HOST, "10", COOKIE_D),
                  Entry(FAMILY_WILD, HOST, "", COOKIE_A),
                  Entry(FAMILY_WILD, HOST, "1", COOKIE_C)]
        expected = {
            ":0": [Entry(FAMILY_LOCAL, HOST, "0", COOKIE_A),
                   Entry(FAMILY_LOCAL, OTHER_HOST, "0", COOKIE_B),
                   Entry(FAMILY_WILD, HOST, "0", COOKIE_A)],
            # "1" and "10" must not match each other.
            ":1": [Entry(FAMILY_LOCAL, HOST, "1", COOKIE_A),
                   Entry(FAMILY_LOCAL, HOST, "1", COOKIE_C),
                   Entry(FAMILY_WILD, HOST, "1", COOKIE_A),
                   Entry(FAMILY_WILD, HOST, "1", COOKIE_C)],
            ":10.0": [Entry(FAMILY_LOCAL, HOST, "10", COOKIE_A),
                      Entry(FAMILY_LOCAL, HOST, "10", COOKIE_D),
                      Entry(FAMILY_WILD, HOST, "10", COOKIE_A)],
        }
        for display, entries in expected.items():
            with self.subTest(display):
                _auth, env = self.run_display(display, source,
                                              f"home-drop-{display[1:]}")
                self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
                self.assert_auth(self.stable, entries)
                self.assert_auth(self.home_auth, entries)
                self.assert_no_tmp_leftovers()

    def test_source_without_entries_for_this_display_builds_nothing(self):
        auth, env = self.run_display(":0", bound(COOKIE_C, "1"), "home-none")
        # No stable file yet: the readable per-login path stays.
        self.assertEqual(env.get("XAUTHORITY"), str(auth))
        self.assert_stderr(self.last_result, kept_line(auth))
        self.assertFalse(self.stable.exists())
        self.assertFalse(self.home_auth.exists())
        self.assertEqual(self.nmerge_targets(), [])

    def test_remote_or_malformed_display_is_not_rebound(self):
        for index, display in enumerate(("host:10", "localhost:10.0", "unix:0",
                                         ":", ":0x", ":1a.0")):
            with self.subTest(display):
                self.fresh(f"home-remote-{index}")
                auth = self.mutter_auth(f"R{index}", COOKIE_A)
                self.set_dumps([self.full_dump(auth, DISPLAY=display)])
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stderr)
                # Nothing could be rebuilt for a remote display: not reported.
                self.assert_stderr(result)
                env = self.bootstrap_env()
                self.assertEqual(env.get("DISPLAY"), display)
                self.assertEqual(env.get("XAUTHORITY"), str(auth))
                self.assertEqual(self.xauth_calls(), [])
                self.assertEqual(sorted(os.listdir(self.home)), [])

    def test_remote_display_never_exports_an_existing_stable_file(self):
        self.make_stable(bound(COOKIE_B))
        before = fingerprint(self.stable)
        auth = self.mutter_auth("REMOTE", COOKIE_A)
        self.set_dumps([self.full_dump(auth, DISPLAY="host:10")])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(auth))
        self.assertEqual(fingerprint(self.stable), before)

    def test_source_listing_family_wild_first_is_rebuilt(self):
        # Round 3 defect, fixed: xauth (1.1.2) writes FamilyWild entries after
        # all others, so for a source that lists FamilyWild first `xauth
        # nlist` of the new file is a reordering of $entries.  The written
        # entries are now compared order-independently, so it is accepted.
        source = list(reversed(mutter_entries(COOKIE_A)))
        _auth, env = self.run_display(":0", source, "home-wild-first")
        self.assert_stderr(self.last_result)
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
        self.assert_auth_set(self.stable, bound(COOKIE_A))
        self.assert_auth_set(self.home_auth, bound(COOKIE_A))
        self.assert_no_tmp_leftovers()


class StableFile(Harness):
    def test_path_modes_and_exact_content(self):
        auth = self.mutter_auth("PRIV", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script(umask=0o000)  # permissive caller umask
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stable, self.home / ".local/state/tmux/Xauthority")
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))
        info = os.lstat(self.stable)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, oct(info.st_mode))
        info = os.lstat(self.stable_dir)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, oct(info.st_mode))
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assert_no_tmp_leftovers()  # no mktemp or xauth -c/-l/-n files
        self.assertEqual(stat.S_IMODE(self.home_auth.stat().st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.home)), [".Xauthority", ".local"])

    def test_relogin_rebuild_replaces_the_file_atomically(self):
        # Leftovers of an older login, including a number-less entry: the
        # rebuild replaces the whole file instead of merging into it.
        self.make_stable([Entry(FAMILY_LOCAL, HOST, "0", COOKIE_B),
                          Entry(FAMILY_WILD, HOST, "0", COOKIE_B),
                          Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C),
                          Entry(FAMILY_LOCAL, HOST, "", COOKIE_D)])
        old_bytes = self.stable.read_bytes()
        old_inode = os.lstat(self.stable).st_ino
        auth = self.mutter_auth("RELOG", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        with open(self.stable, "rb") as reader:  # a pane reading mid-login
            result = self.run_script()
            self.assertEqual(reader.read(), old_bytes)  # never modified in place
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(os.lstat(self.stable).st_ino, old_inode)
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assert_no_tmp_leftovers()
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    def test_failed_rebuild_keeps_previous_stable_file(self):
        # fail: nmerge exits 1 without writing; garbage: it leaves a corrupt
        # temporary file; partial: it "succeeds" with fewer entries.
        for mode in ("fail", "garbage", "partial"):
            with self.subTest(mode):
                self.fresh(f"home-fault-{mode}")
                self.make_stable(bound(COOKIE_B))
                before = fingerprint(self.stable)
                self.set_xauth_faults([{"prefix": f"{self.stable_dir}/", "mode": mode}])
                auth = self.mutter_auth(f"F{mode}", COOKIE_A)
                self.set_dumps([self.full_dump(auth)])
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result, kept_line(auth))
                (target,) = self.nmerge_targets()  # the temporary file only
                self.assertEqual(Path(target).parent, self.stable_dir)
                self.assertTrue(Path(target).name.startswith(".Xauthority."))
                for suffix in ("", "-n", "-c", "-l"):  # removed again
                    self.assertFalse(os.path.lexists(target + suffix), suffix)
                self.assertEqual(fingerprint(self.stable), before)
                self.assert_no_tmp_leftovers()
                # built=no: no compatibility merge, and panes keep the working
                # per-login path rather than the stale stable file.
                self.assertFalse(self.home_auth.exists())
                self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(auth))

    def test_unusable_state_directory_keeps_previous_state(self):
        # (An own read-only state directory is not unusable: the validation
        # chmods it back to 0700, see test_read_only_own_state_dir_is_rebuilt.)
        def read_only_parent():
            parent = self.stable_dir.parent
            parent.mkdir(parents=True)
            parent.chmod(0o500)
            self.restore_mode_later(parent)
            return parent

        def state_dir_is_a_file():
            self.stable_dir.parent.mkdir(parents=True)
            self.stable_dir.write_text("not a directory\n")
            return self.stable_dir

        def state_dir_is_a_symlink():
            elsewhere = self.root / f"elsewhere-{self.home.name}"
            elsewhere.mkdir(mode=0o755)
            # A previous file behind the link is neither rebuilt nor exported.
            self.write_auth(elsewhere / "Xauthority", bound(COOKIE_B))
            self.stable_dir.parent.mkdir(parents=True)
            self.stable_dir.symlink_to(elsewhere)
            return elsewhere

        for prepare in (read_only_parent, state_dir_is_a_file, state_dir_is_a_symlink):
            with self.subTest(prepare.__name__):
                self.fresh(f"home-{prepare.__name__}")
                witness = prepare()
                before = snapshot(witness)  # also: no chmod of an unvalidated dir
                auth = self.mutter_auth(prepare.__name__, COOKIE_A)
                self.set_dumps([self.full_dump(auth)])
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result, kept_line(auth))
                self.assertEqual(self.nmerge_targets(), [])  # nothing built
                self.assertFalse(self.home_auth.exists())
                self.assertEqual(snapshot(witness), before)
                self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(auth))

    def test_read_only_own_state_dir_is_rebuilt(self):
        # The state directory is validated once: an own 0500 directory is
        # chmod'ed to 0700 and then used, by both modes.
        for name, args, running in (("start", (), False), ("sync", ("--sync",), True)):
            with self.subTest(name):
                self.fresh(f"home-ro-{name}")
                self.make_stable(bound(COOKIE_B))
                old_inode = os.lstat(self.stable).st_ino
                self.stable_dir.chmod(0o500)
                self.restore_mode_later(self.stable_dir)
                self.set_server_running(running)
                auth = self.mutter_auth(f"RO{name}", COOKIE_A)
                self.set_dumps([self.full_dump(auth)])
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result)
                info = os.lstat(self.stable_dir)
                self.assertTrue(stat.S_ISDIR(info.st_mode))
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700, oct(info.st_mode))
                self.assertNotEqual(os.lstat(self.stable).st_ino, old_inode)
                self.assertEqual(stat.S_IMODE(os.lstat(self.stable).st_mode), 0o600)
                self.assert_auth(self.stable, bound(COOKIE_A))
                self.assert_no_tmp_leftovers()
                self.assert_auth(self.home_auth, bound(COOKIE_A))
                if running:
                    self.assertEqual(self.tmux_argv(), self.sync_calls())
                else:
                    self.assertEqual(self.bootstrap_env().get("XAUTHORITY"),
                                     str(self.stable))

    def test_symlink_at_stable_path_is_replaced_not_followed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        target = self.write_auth(elsewhere / "auth", bound(COOKIE_C))
        before = fingerprint(target)
        self.stable_dir.mkdir(parents=True, mode=0o700)
        self.stable.symlink_to(target)
        auth = self.mutter_auth("LINK01", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.stable.is_symlink())
        self.assertEqual(stat.S_IMODE(os.lstat(self.stable).st_mode), 0o600)
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assertEqual(fingerprint(target), before)  # link target untouched
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    def test_directory_at_stable_path_is_not_reported_as_built(self):
        # Round 3 defect, fixed by `mv -fT`: a directory found at the stable
        # path is never moved into, so built=no and the readable per-login
        # path is kept (a directory is never exported or published).
        self.stable_dir.mkdir(parents=True, mode=0o700)
        self.stable.mkdir()
        auth = self.mutter_auth("DIR01", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result, kept_line(auth))
        self.assertTrue(self.stable.is_dir())
        self.assertEqual(os.listdir(self.stable), [])
        self.assert_no_tmp_leftovers()
        self.assertFalse(self.home_auth.exists())
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(auth))

    def test_symlink_to_directory_at_stable_path_is_not_followed(self):
        # Round 3 defect, fixed by `mv -fT`: the link itself is replaced by
        # the new private file; nothing lands in the link's target directory
        # outside ~/.local/state/tmux.
        elsewhere = self.root / "elsewhere-dir"
        elsewhere.mkdir()
        self.stable_dir.mkdir(parents=True, mode=0o700)
        self.stable.symlink_to(elsewhere)
        auth = self.mutter_auth("DIR02", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertFalse(self.stable.is_symlink())
        info = os.lstat(self.stable)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assert_no_tmp_leftovers()
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    def test_existing_permissive_state_dir_still_gets_a_private_file(self):
        self.stable_dir.mkdir(parents=True)
        self.stable_dir.chmod(0o755)
        auth = self.mutter_auth("PERM", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script(umask=0o000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(os.lstat(self.stable).st_mode), 0o600)
        self.assert_auth(self.stable, bound(COOKIE_A))

    def test_self_referencing_source_is_not_rebuilt(self):
        # The manager's XAUTHORITY already is the stable path (e.g. imported
        # from a pane): no merge of a file into itself.
        self.make_stable(bound(COOKIE_A))
        before = fingerprint(self.stable)
        self.set_dumps([self.full_dump(self.stable)])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stderr(result)  # nothing to rebuild: not reported
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))
        self.assertEqual(fingerprint(self.stable), before)
        self.assertEqual(self.nmerge_targets(), [])
        self.assertFalse(self.home_auth.exists())

    def test_termination_during_rebuild_removes_temporary_files(self):
        # A merge into the temporary file hangs with xauth's -c/-l locks and
        # -n new file next to it.  systemd stops a unit with SIGTERM to the
        # whole control group, killing that xauth; a TERM to the script alone
        # is handled once the foreground xauth returns.  Either way the trap
        # removes the temporary file and its siblings and exits 1.
        for name, group, seconds in (("script", False, 2), ("control-group", True, 60)):
            with self.subTest(name):
                self.fresh(f"home-term-{name}")
                self.make_stable(bound(COOKIE_B))
                before = fingerprint(self.stable)
                ready = self.state / f"xauth-slow-{name}.ready"
                self.set_xauth_faults([{"prefix": f"{self.stable_dir}/", "mode": "slow",
                                        "seconds": seconds, "ready": str(ready)}])
                auth = self.mutter_auth(f"TERM-{name}", COOKIE_A)
                self.set_dumps([self.full_dump(auth)])
                proc = subprocess.Popen(self.script_argv(), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, umask=0o022, start_new_session=True)
                try:
                    deadline = time.monotonic() + 30
                    while not ready.exists():
                        self.assertIsNone(proc.poll(), "script exited before the merge")
                        self.assertLess(time.monotonic(), deadline, "merge never started")
                        time.sleep(0.02)
                    (target,) = self.nmerge_targets()
                    self.assertRegex(Path(target).name, r"^\.Xauthority\.[A-Za-z0-9]{6}$")
                    self.assertEqual(Path(target).parent, self.stable_dir)
                    leftovers = [target + suffix for suffix in ("", "-n", "-c", "-l")]
                    for path in leftovers:  # precondition: all present mid-merge
                        self.assertTrue(os.path.lexists(path), path)
                    if group:
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        os.kill(proc.pid, signal.SIGTERM)
                    stdout, stderr = proc.communicate(timeout=seconds + 30)
                finally:
                    if proc.poll() is None:  # never leave the scratch process
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()
                self.assertEqual(proc.returncode, 1, stderr)
                for path in leftovers:
                    self.assertFalse(os.path.lexists(path), path)
                self.assert_no_tmp_leftovers()
                self.assertEqual(fingerprint(self.stable), before)
                # Stopped before the compatibility merge and before tmux.
                self.assertEqual(sorted(os.listdir(self.home)), [".local"])
                self.assertEqual(self.tmux_argv(), [])
                for cookie in COOKIES:
                    self.assertNotIn(cookie, stdout + stderr)


class CompatFile(Harness):
    """~/.Xauthority, for processes started without XAUTHORITY."""

    def run_mutter(self, cookie=COOKIE_A, suffix="COMPAT", **kwargs):
        auth = self.mutter_auth(suffix, cookie)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_created_private_with_numbered_entries_only(self):
        self.run_mutter(umask=0o000)
        self.assert_auth(self.home_auth, bound(COOKIE_A))
        self.assertEqual(stat.S_IMODE(self.home_auth.stat().st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.home)), [".Xauthority", ".local"])

    def test_merge_replaces_this_display_and_preserves_unrelated_entries(self):
        unrelated = [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C),
                     Entry(FAMILY_WILD, HOST, "5", COOKIE_C),
                     Entry(FAMILY_LOCAL, OTHER_HOST, "0", COOKIE_D)]
        self.write_auth(self.home_auth, [*unrelated, *bound(COOKIE_B)])
        self.run_mutter()
        self.assert_auth_set(self.home_auth, [*unrelated, *bound(COOKIE_A)])
        self.assertEqual(self.compat_merges(), [["-q", "-f", str(self.home_auth),
                                                 "nmerge", "-"]])

    def test_merge_adds_no_numberless_entries(self):
        self.run_mutter()
        self.assertTrue(all(entry.number for entry in self.read_auth(self.home_auth)))
        self.assertTrue(all(entry.number for entry in self.read_auth(self.stable)))

    def test_existing_world_readable_file_becomes_private(self):
        self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)],
                        mode=0o644)
        self.run_mutter(umask=0o000)
        self.assertEqual(stat.S_IMODE(self.home_auth.stat().st_mode), 0o600)
        self.assert_auth_set(self.home_auth,
                             [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C), *bound(COOKIE_A)])

    def test_symlinked_file_is_replaced_not_followed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        target = self.write_auth(elsewhere / "auth",
                                 [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
        before = fingerprint(target)
        self.home_auth.symlink_to(target)
        self.run_mutter()
        self.assertFalse(self.home_auth.is_symlink())
        self.assertEqual(stat.S_IMODE(os.lstat(self.home_auth).st_mode), 0o600)
        # Merged from what the link pointed at, written as a new regular file.
        self.assert_auth_set(self.home_auth,
                             [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C), *bound(COOKIE_A)])
        self.assertEqual(fingerprint(target), before)

    def test_unusable_file_does_not_affect_the_stable_file(self):
        # xauth exits 0 without writing in both cases; the compatibility copy
        # is best effort and never decides what panes get.
        def directory():
            self.home_auth.mkdir()

        def read_only_stale():
            self.write_auth(self.home_auth, bound(COOKIE_C), mode=0o400)

        for prepare in (directory, read_only_stale):
            with self.subTest(prepare.__name__):
                self.fresh(f"home-{prepare.__name__}")
                prepare()
                self.run_mutter(suffix=prepare.__name__)
                self.assertEqual(self.bootstrap_env().get("XAUTHORITY"),
                                 str(self.stable))
                self.assert_auth(self.stable, bound(COOKIE_A))
                if prepare is directory:
                    self.assertEqual(os.listdir(self.home_auth), [])
                else:
                    self.assert_auth(self.home_auth, bound(COOKIE_C))

    def test_stale_lock_is_broken(self):
        # A crashed xauth leaves ~/.Xauthority-c (and -l); older than a minute
        # it is broken with -b.
        for name, locks in (("c", (".Xauthority-c",)),
                            ("c-and-l", (".Xauthority-c", ".Xauthority-l"))):
            with self.subTest(name):
                self.fresh(f"home-stale-{name}")
                self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
                for lock in locks:
                    touch(self.home / lock, "5 minutes ago")
                result = self.run_mutter(suffix=f"STALE{len(locks)}")
                self.assertLess(result.elapsed, 5.0)
                self.assertEqual(self.compat_merges(), [
                    ["-b", "-q", "-f", str(self.home_auth), "nmerge", "-"]])
                self.assert_auth_set(self.home_auth, [
                    Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C), *bound(COOKIE_A)])
                self.assertEqual(sorted(os.listdir(self.home)), [".Xauthority", ".local"])
                self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    def test_fresh_lock_is_not_broken(self):
        # A lock younger than a minute may belong to a running xauth: no -b.
        # (The fake emulates real xauth's 20 s lock timeout immediately.)
        for when in (None, "30 seconds ago"):
            with self.subTest(when or "now"):
                self.fresh(f"home-fresh-{bool(when)}")
                self.set_xauth_faults([{"prefix": str(self.home_auth),
                                        "mode": "lock-timeout"}])
                self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
                touch(self.home / ".Xauthority-c", when)
                before = fingerprint(self.home_auth)
                result = self.run_mutter(suffix="FRESH")
                self.assertEqual(self.compat_merges(), [
                    ["-q", "-f", str(self.home_auth), "nmerge", "-"]])
                self.assertEqual(fingerprint(self.home_auth), before)
                self.assertTrue((self.home / ".Xauthority-c").exists())
                self.assertEqual(result.stderr, "")  # xauth's complaint is muted
                self.assert_auth(self.stable, bound(COOKIE_A))
                self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))

    @unittest.skipUnless(os.environ.get("TMUX_SERVICE_TEST_SLOW") == "1",
                         "set TMUX_SERVICE_TEST_SLOW=1 (takes ~20 s)")
    def test_fresh_lock_with_real_xauth_times_out_harmlessly(self):
        self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
        touch(self.home / ".Xauthority-c")
        before = fingerprint(self.home_auth)
        result = self.run_mutter(suffix="REALLOCK")
        self.assertGreaterEqual(result.elapsed, 15.0)
        self.assertLess(result.elapsed, 40.0)
        self.assertEqual(fingerprint(self.home_auth), before)
        self.assertTrue((self.home / ".Xauthority-c").exists())
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assertEqual(self.bootstrap_env().get("XAUTHORITY"), str(self.stable))


class ExportRules(Harness):
    """Which XAUTHORITY the bootstrap server (and so every pane) gets."""

    def start(self, auth, display=":0", home=None, polls=5):
        self.set_dumps([self.full_dump(auth, DISPLAY=display)])
        result = self.run_script(home=home, polls=polls)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.bootstrap_env()

    def test_built_stable_file_is_exported(self):
        env = self.start(self.mutter_auth("BUILT", COOKIE_A))
        self.assertEqual(env.get("XAUTHORITY"), str(self.stable))

    def test_readable_source_is_kept_over_stale_stable_file(self):
        # Not rebuilt (no entry for this display): the working per-login path
        # is kept and the previous login's stable file is left alone.
        self.make_stable(bound(COOKIE_B))
        before = fingerprint(self.stable)
        auth = self.mutter_auth("NOENT", entries=bound(COOKIE_A, "1"))
        env = self.start(auth)
        self.assert_stderr(self.last_result, kept_line(auth))
        self.assertEqual(env.get("XAUTHORITY"), str(auth))
        self.assertEqual(fingerprint(self.stable), before)
        self.assertEqual(self.nmerge_targets(), [])
        self.assertFalse(self.home_auth.exists())

    def test_unreadable_source_exports_stable_path(self):
        def missing():
            return self.run_dir / f".mutter-Xwaylandauth.GONE-{self.home.name}"

        def unreadable():
            path = self.mutter_auth(f"MODE0-{self.home.name}", COOKIE_A)
            path.chmod(0o000)
            self.restore_mode_later(path, 0o600)
            return path

        for make_source in (missing, unreadable):
            for with_stable in (False, True):
                with self.subTest(make_source.__name__, stable=with_stable):
                    self.fresh(f"home-{make_source.__name__}-{with_stable}")
                    if with_stable:
                        self.make_stable(bound(COOKIE_B))
                    before = fingerprint(self.stable) if with_stable else None
                    # Never the dead per-login path; --sync refreshes the file.
                    source = make_source()
                    env = self.start(source, polls=1)
                    # Readiness waits for a readable cookie, so the bound is hit.
                    self.assert_stderr(self.last_result, not_ready_line(1),
                                       unreadable_line(source, self.stable))
                    self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
                    self.assertEqual(self.xauth_calls(), [])
                    self.assertFalse(self.home_auth.exists())
                    if with_stable:
                        self.assertEqual(fingerprint(self.stable), before)
                    else:
                        self.assertFalse(self.stable.exists())

    def test_unreadable_source_without_stable_path_is_unset(self):
        missing = self.run_dir / ".mutter-Xwaylandauth.GONE"

        def symlinked_state_dir():
            # A state directory that fails validation is no stable path.
            elsewhere = self.root / f"elsewhere-{self.home.name}"
            elsewhere.mkdir()
            self.write_auth(elsewhere / "Xauthority", bound(COOKIE_B))
            self.stable_dir.parent.mkdir(parents=True)
            self.stable_dir.symlink_to(elsewhere)
            return elsewhere

        for name, display, home, prepare in (
                ("remote", "host:10", None, None),
                ("no-home", ":0", "", None),
                ("symlinked-state-dir", ":0", None, symlinked_state_dir)):
            with self.subTest(name):
                self.fresh(f"home-unset-{name}")
                witness = prepare() if prepare else None
                before = snapshot(witness) if witness else None
                env = self.start(missing, display=display, home=home, polls=1)
                self.assert_stderr(self.last_result, not_ready_line(1))
                self.assertNotIn("XAUTHORITY", env)
                self.assertEqual(self.nmerge_targets(), [])
                if witness:
                    self.assertEqual(snapshot(witness), before)

    def test_no_source_leaves_xauthority_unset_even_with_stable_file(self):
        # No XAUTHORITY in the session at all (Xorg; or Xwayland whose cookie
        # never arrived): X clients use their default ~/.Xauthority, never a
        # previous login's stable file.  Neither mode exports or publishes it.
        cases = {
            "xorg": (dump(XDG_SESSION_TYPE="x11", DISPLAY=":0",
                          DBUS_SESSION_BUS_ADDRESS=BUS), 5, ()),
            "wayland-without-cookie": (dump(
                XDG_SESSION_TYPE="wayland", DISPLAY=":0", WAYLAND_DISPLAY="wayland-0",
                DBUS_SESSION_BUS_ADDRESS=BUS), 1, (not_ready_line(1),)),
        }
        for name, (session_dump, polls, stderr) in cases.items():
            for args, running in (((), False), (("--sync",), True)):
                with self.subTest(name, mode=args or "start"):
                    self.fresh(f"home-nosrc-{name}-{running}")
                    self.make_stable(bound(COOKIE_B))
                    before = fingerprint(self.stable)
                    self.set_server_running(running)
                    self.set_dumps([session_dump])
                    result = self.run_script(*args, polls=polls)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_stderr(result, *stderr)
                    if running:
                        calls = self.tmux_argv()
                        self.assertIn(set_g("DISPLAY", ":0"), calls)
                        self.assertEqual([call for call in calls if "XAUTHORITY" in call
                                          and call[1] in ("-g", "-gu")], [])
                    else:
                        env = self.bootstrap_env()
                        self.assertEqual(env.get("DISPLAY"), ":0")
                        self.assertNotIn("XAUTHORITY", env)
                    self.assertEqual(fingerprint(self.stable), before)
                    self.assertEqual(self.xauth_calls(), [])
                    self.assertFalse(self.home_auth.exists())

    def test_empty_home_keeps_readable_source(self):
        auth = self.mutter_auth("NOHOME", COOKIE_A)
        env = self.start(auth, home="")
        self.assert_stderr(self.last_result, kept_line(auth))
        self.assertEqual(env.get("XAUTHORITY"), str(auth))
        self.assertEqual(self.nmerge_targets(), [])
        self.assertEqual(sorted(os.listdir(self.home)), [])


class SyncMode(Harness):
    def test_relogin_sync_rebuilds_stable_file_and_publishes_it(self):
        first = self.mutter_auth("FIRST1", COOKIE_A)
        self.set_dumps([self.full_dump(first)])
        self.assertEqual(self.run_script().returncode, 0)
        self.assert_auth(self.stable, bound(COOKIE_A))
        unrelated = Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)
        self.write_auth(self.home_auth, [*self.read_auth(self.home_auth), unrelated])

        # Logout: tmux survives (linger); the next login renames the auth file.
        self.clear_logs()
        self.set_server_running(True)
        second = self.mutter_auth("SECOND", COOKIE_B)
        self.set_dumps([self.full_dump(second)])
        result = self.run_script("--sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assert_auth(self.stable, bound(COOKIE_B))
        self.assert_no_tmp_leftovers()
        self.assert_auth_set(self.home_auth, [unrelated, *bound(COOKIE_B)])
        self.assertEqual(stat.S_IMODE(self.home_auth.stat().st_mode), 0o600)
        self.assertEqual(self.tmux_argv(), self.sync_calls())

    def test_sync_without_server_still_rebuilds_and_exits_zero(self):
        auth = self.mutter_auth("NOSRV", COOKIE_B)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script("--sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tmux_argv(), [["has-session"]])
        # Refreshed for the next server and for the X11 fallback.
        self.assert_auth(self.stable, bound(COOKIE_B))
        self.assert_auth(self.home_auth, bound(COOKIE_B))

    def test_sync_diagnostics_say_tmux_xauthority_is_left_unchanged(self):
        # Sync never publishes an unbuilt XAUTHORITY, so its diagnostics must
        # not claim the per-login path is being kept or the stable one used.
        none_for_display = self.mutter_auth("SYNCNONE", COOKIE_C)
        self.write_auth(none_for_display, bound(COOKIE_C, "1"))
        missing = self.run_dir / ".mutter-Xwaylandauth.SYNCGONE"
        cases = (
            ("readable-no-entries", none_for_display, 5,
             [f"{DIAG}X cookie not rebuilt; tmux XAUTHORITY left unchanged"]),
            ("unreadable", missing, 1,
             [not_ready_line(1),
              f"{DIAG}{missing} is not readable; tmux XAUTHORITY left unchanged"]),
        )
        for label, source, polls, lines in cases:
            with self.subTest(label):
                self.fresh(f"home-syncdiag-{label}")
                self.set_server_running(True)
                self.set_dumps([self.full_dump(source)])
                result = self.run_script("--sync", polls=polls)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_stderr(result, *lines)
                self.assertEqual(self.tmux_argv(), self.sync_calls(xauthority=False))
                self.assertFalse(self.stable.exists())

    def test_sync_sets_only_allowlisted_variables(self):
        auth = self.mutter_auth("ALLOW", COOKIE_B)
        self.set_dumps([self.full_dump(
            auth, LANG="ko_KR.UTF-8", PATH="/usr/local/bin:/usr/bin",
            SSH_AUTH_SOCK="/run/user/1000/keyring/ssh", GNOME_SETUP_DISPLAY=":1",
            SESSION_MANAGER="local/host:@/tmp/.ICE-unix/1", _="/usr/bin/env")])
        self.set_sessions(["$0", "$1", "$2"])
        self.set_server_running(True)
        # Under systemd the service also inherits the manager's other variables.
        result = self.run_script("--sync", inherited={
            "LANG": "C.UTF-8", "SSH_AUTH_SOCK": "/run/user/1000/ssh",
            "GNOME_SETUP_DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(result.returncode, 0, result.stderr)
        # Globals first, then the session-level shadows of the changing ones.
        self.assertEqual(self.tmux_argv(),
                         self.sync_calls(session_ids=("$0", "$1", "$2")))

    def test_session_cleanup_addresses_ids_and_skips_other_lines(self):
        auth = self.mutter_auth("IDS", COOKIE_B)
        self.set_dumps([self.full_dump(auth)])
        self.set_server_running(True)
        # Only "$<digits>..." lines are used as targets; names, window/pane
        # ids, blanks and other text never are.
        self.set_sessions(["$0", "main", "$12", "@1", "%3", "$", "$x", "",
                           "=$1", " $2", "-t", "my work"])
        result = self.run_script("--sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tmux_argv()
        self.assertEqual(calls, self.sync_calls(session_ids=("$0", "$12")))
        targeted = [call[2] for call in calls if call[1:2] == ["-t"]]
        self.assertEqual(targeted, ["$0"] * 3 + ["$12"] * 3)

    def test_sync_never_publishes_unbuilt_xauthority(self):
        def missing_source():
            return self.full_dump(self.run_dir / ".mutter-Xwaylandauth.GONE"), 1

        def escaped_source():
            return self.full_dump("$'/run/user/1000/.mutter-Xwaylandauth.X\\n'"), 1

        def failed_rebuild():
            self.set_xauth_faults([{"prefix": f"{self.stable_dir}/", "mode": "fail"}])
            return self.full_dump(self.mutter_auth("SFAIL", COOKIE_B)), 5

        def uncreatable_state_dir():
            # (An own read-only state dir is chmod'ed to 0700 and rebuilt:
            # see StableFile.test_read_only_own_state_dir_is_rebuilt.)
            parent = self.stable_dir.parent
            parent.mkdir(parents=True)
            parent.chmod(0o500)
            self.restore_mode_later(parent)
            return self.full_dump(self.mutter_auth("SRO", COOKIE_B)), 5

        def directory_at_stable_path():
            self.stable_dir.mkdir(parents=True, mode=0o700)
            self.stable.mkdir()
            return self.full_dump(self.mutter_auth("SDIR", COOKIE_B)), 5

        def symlinked_state_dir():
            elsewhere = self.root / f"elsewhere-{self.home.name}"
            elsewhere.mkdir()
            self.stable_dir.parent.mkdir(parents=True)
            self.stable_dir.symlink_to(elsewhere)
            return self.full_dump(self.mutter_auth("SLINK", COOKIE_B)), 5

        def self_reference():
            self.make_stable(bound(COOKIE_A))
            return self.full_dump(self.stable), 5

        def no_entries_for_display():
            return self.full_dump(self.mutter_auth(
                "S1", entries=bound(COOKIE_B, "1"))), 5

        def remote_display():
            return self.full_dump(self.mutter_auth("SREM", COOKIE_B),
                                  DISPLAY="host:10"), 5

        def x11_without_xauthority():
            return dump(XDG_SESSION_TYPE="x11", DISPLAY=":1"), 5

        cases = (missing_source, escaped_source, failed_rebuild,
                 uncreatable_state_dir, directory_at_stable_path, symlinked_state_dir,
                 self_reference, no_entries_for_display, remote_display,
                 x11_without_xauthority)
        for prepare in cases:
            with self.subTest(prepare.__name__):
                self.fresh(f"home-{prepare.__name__}")
                self.set_server_running(True)
                session_dump, polls = prepare()
                self.set_dumps([session_dump])
                result = self.run_script("--sync", polls=polls)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.tmux_argv()
                published = [call for call in calls
                             if call[:2] in (["set-environment", "-g"],
                                             ["set-environment", "-gu"])
                             and call[2] == "XAUTHORITY"]
                self.assertEqual(published, [])
                # The rest of the refresh still happens.
                self.assertIn(LIST_SESSIONS, calls)
                self.assertEqual(calls[-3:], session_cleanup("$0"))
                self.assertFalse(self.home_auth.exists())

    def test_sync_unsets_absent_names_but_keeps_escaped_ones(self):
        x11_auth = self.mutter_auth("X11S", entries=bound(COOKIE_B, "1"))
        x11 = dump(XDG_SESSION_TYPE="x11", DISPLAY=":1", XAUTHORITY=str(x11_auth),
                   DBUS_SESSION_BUS_ADDRESS="$'unix:path=/x\\n'",
                   XDG_CURRENT_DESKTOP="$'GNOME\\n'")
        wayland_auth = self.mutter_auth("ESCS", COOKIE_B)
        escaped_wayland = self.full_dump(wayland_auth,
                                         WAYLAND_DISPLAY="$'wayland-0\\n'")
        cases = {
            # Xorg login after a Wayland one: WAYLAND_DISPLAY is gone.
            "x11-login": (x11, lambda: [
                ["has-session"], set_g("DISPLAY", ":1"), unset_g("WAYLAND_DISPLAY"),
                set_g("XAUTHORITY", str(self.stable)),
                set_g("XDG_SESSION_TYPE", "x11"),
                LIST_SESSIONS, *session_cleanup("$0")], bound(COOKIE_B, "1")),
            # Present but unrepresentable: leave the old global value alone.
            "escaped-wayland": (escaped_wayland, lambda: [
                ["has-session"], set_g("DISPLAY", ":0"),
                set_g("XAUTHORITY", str(self.stable)),
                set_g("XDG_SESSION_TYPE", "wayland"),
                set_g("XDG_CURRENT_DESKTOP", "ubuntu:GNOME"),
                set_g("DBUS_SESSION_BUS_ADDRESS", BUS),
                LIST_SESSIONS, *session_cleanup("$0")], bound(COOKIE_B)),
        }
        for name, (session_dump, expected, content) in cases.items():
            with self.subTest(name):
                self.fresh(f"home-{name}")
                self.set_server_running(True)
                self.set_dumps([session_dump])
                result = self.run_script("--sync", polls=1)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.tmux_argv(), expected())
                self.assert_auth(self.stable, content)

    def test_sync_with_empty_dump_publishes_nothing(self):
        cases = {"systemctl-fails": ([dump()], True), "empty-dump": ([""], False)}
        for name, (dumps, fail) in cases.items():
            with self.subTest(name):
                self.fresh(f"home-{name}")
                self.set_server_running(True)
                self.set_dumps(dumps, fail=fail)
                result = self.run_script("--sync", polls=2,
                                         inherited={"DISPLAY": ":0"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.systemctl_calls()), 2)
                self.assertEqual(self.tmux_argv(), [["has-session"]])

    def test_sync_tolerates_list_sessions_failure(self):
        auth = self.mutter_auth("LSFAIL", COOKIE_B)
        self.set_dumps([self.full_dump(auth)])
        self.set_server_running(True)
        self.set_sessions(None)  # list-sessions exits 1 with no output
        result = self.run_script("--sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tmux_argv(), self.sync_calls(session_ids=()))


class SecretHandling(Harness):
    def test_cookies_never_reach_argv_or_environment(self):
        self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
        first = self.mutter_auth("LEAK01", COOKIE_A)
        self.set_dumps([self.full_dump(first)])
        runs = [self.run_script(audit=True)]
        self.set_server_running(True)
        second = self.mutter_auth("LEAK02", COOKIE_B)
        self.set_dumps([self.full_dump(second)])
        runs.append(self.run_script("--sync", audit=True))
        for result in runs:
            self.assertEqual(result.returncode, 0, result.stderr)

        records = self.exec_records()
        commands = {record["cmd"] for record in records}
        # The wrappers really were used, and printf stays a shell builtin.
        self.assertLessEqual({"xauth", "awk", "sed", "mktemp", "mv", "find"}, commands)
        self.assertNotIn("printf", commands)
        texts = [result.stdout + result.stderr for result in runs]
        texts += [(self.state / name).read_text() for name in LOGS]
        for cookie in COOKIES:
            for text in texts:
                self.assertNotIn(cookie, text)
                self.assertNotIn(cookie.upper(), text)

        stable = str(self.stable)
        compat = str(self.home_auth)
        calls = []
        for argv in self.xauth_calls():
            index = argv.index("-f") + 1
            target = argv[index]
            if Path(target).parent == self.stable_dir and target != stable:
                self.assertRegex(Path(target).name, r"^\.Xauthority\.[A-Za-z0-9]{6}$")
                target = "<tmp>"
            calls.append([*argv[:index], target, *argv[index + 1:]])
        self.assertEqual(calls, [
            ["-f", str(first), "nlist"],
            ["-q", "-f", "<tmp>", "nmerge", "-"], ["-f", "<tmp>", "nlist"],
            ["-q", "-f", compat, "nmerge", "-"],
            ["-f", str(second), "nlist"],
            ["-q", "-f", "<tmp>", "nmerge", "-"], ["-f", "<tmp>", "nlist"],
            ["-q", "-f", compat, "nmerge", "-"],
        ])
        for record in self.exec_records("awk"):
            self.assertEqual(record["argv"][:2], ["-v", "num=0"])
        moves = [record["argv"] for record in self.exec_records("mv")]
        self.assertEqual(len(moves), 2)
        for argv in moves:
            # -T: the stable path is replaced, never moved into.
            self.assertEqual(len(argv), 3, argv)
            self.assertEqual((argv[0], argv[2]), ("-fT", stable))
            self.assertEqual(Path(argv[1]).parent, self.stable_dir)
            self.assertRegex(Path(argv[1]).name, r"^\.Xauthority\.[A-Za-z0-9]{6}$")
        # Nothing failed, and the cleanup trap was cleared after the rebuild
        # (sync mode exits normally; an EXIT trap left set would run rm).
        self.assertEqual(self.exec_records("rm"), [])
        self.assert_auth(self.stable, bound(COOKIE_B))
        self.assert_auth_set(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C),
                                              *bound(COOKIE_B)])

    def test_execve_never_carries_cookies_even_by_absolute_path(self):
        # The PATH wrappers above cannot see a command run by absolute path
        # (e.g. /usr/bin/printf); strace records every execve of the script
        # and its children with the full argv and environment.
        strace = "/usr/bin/strace"
        if not os.access(strace, os.X_OK):
            self.skipTest("strace not installed")
        probe = subprocess.run([strace, "-f", "-qq", "-o", "/dev/null", "/bin/true"],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            self.skipTest(f"ptrace not permitted: {probe.stderr.strip()}")
        self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
        self.make_stable(bound(COOKIE_D))  # a previous login's file
        traces = [self.root / "execve-start.log", self.root / "execve-sync.log"]

        def traced(path):
            return (strace, "-f", "-qq", "-v", "-s", "65536", "-e", "trace=execve",
                    "-o", str(path))

        first = self.mutter_auth("STRACE1", COOKIE_A)
        self.set_dumps([self.full_dump(first)])
        runs = [self.run_script(shell=traced(traces[0]))]
        self.set_server_running(True)
        second = self.mutter_auth("STRACE2", COOKIE_B)
        self.set_dumps([self.full_dump(second)])
        runs.append(self.run_script("--sync", shell=traced(traces[1])))
        for result in runs:
            self.assertEqual(result.returncode, 0, result.stderr)
        text = "".join(path.read_text() for path in traces)
        execs = [line for line in text.splitlines() if "execve(" in line]
        # The trace really covers the cookie-handling commands.
        self.assertTrue([line for line in execs if '"nmerge"' in line])
        self.assertTrue([line for line in execs if '"num=0"' in line])
        for cookie in COOKIES:
            self.assertNotIn(cookie, text)
            self.assertNotIn(cookie.upper(), text)
        self.assert_auth(self.stable, bound(COOKIE_B))

    def test_xtrace_never_prints_cookies_and_is_restored(self):
        shells = [shell for shell in ("/usr/bin/dash", "/bin/bash")
                  if os.access(shell, os.X_OK)]
        for shell in shells:
            with self.subTest(shell):
                self.fresh(f"home-trace-{Path(shell).name}")
                self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "7", COOKIE_C)])
                first = self.mutter_auth(f"TRACE1{Path(shell).name}", COOKIE_A)
                self.set_dumps([self.full_dump(first)])
                start = self.run_script(shell=(shell, "-x"))
                self.set_server_running(True)
                second = self.mutter_auth(f"TRACE2{Path(shell).name}", COOKIE_B)
                self.set_dumps([self.full_dump(second)])
                sync = self.run_script("--sync", shell=(shell, "-x"))
                for result in (start, sync):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("+ ", result.stderr)  # really traced
                    self.assertNotIn("nmerge", result.stderr)  # untraced section
                    for cookie in COOKIES:
                        self.assertNotIn(cookie, result.stdout + result.stderr)
                # Tracing resumes after the cookie handling.
                self.assertIn(" ".join(BOOTSTRAP), start.stderr)
                self.assertIn(f"set-environment -g XAUTHORITY {self.stable}",
                              sync.stderr)
                self.assert_auth(self.stable, bound(COOKIE_B))


class ClientLookup(Harness):
    """What a real X client would pick from the files (libXau's matcher)."""

    def test_stable_file_authorizes_only_its_display(self):
        auth = self.mutter_auth("LOOK", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.xau_lookup(self.stable, "0"), COOKIE_A)
        self.assertEqual(self.xau_lookup(self.stable, "0", host="renamed"), COOKIE_A)
        self.assertEqual(self.xau_lookup(self.stable, "1"), "none")
        # mutter's own file matches every display: why it is never exported.
        self.assertEqual(self.xau_lookup(auth, "1"), COOKIE_A)

    def test_compat_file_prefers_new_cookie_over_stale_numberless_entry(self):
        # A number-less entry left by an older merge is not removed, but the
        # new numbered entry is chosen for this display.
        self.write_auth(self.home_auth, [Entry(FAMILY_LOCAL, HOST, "", COOKIE_C),
                                         Entry(FAMILY_LOCAL, HOST, "7", COOKIE_D)])
        auth = self.mutter_auth("LOOK2", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.xau_lookup(self.home_auth, "0"), COOKIE_A)
        self.assertEqual(self.xau_lookup(self.home_auth, "7"), COOKIE_D)


class RealTmuxIntegration(Harness):
    """A private tmux server; the live default socket is never addressed."""

    def setUp(self):
        super().setUp()
        if not os.access(TMUX_BIN, os.X_OK):
            self.skipTest(f"{TMUX_BIN} not executable")
        base = os.environ.get("TMUX_SERVICE_TEST_SOCKET_DIR") or tempfile.gettempdir()
        self.sock_dir = Path(tempfile.mkdtemp(prefix="t", dir=base))
        self.socket = self.sock_dir / "s"
        if len(os.fsencode(self.socket)) > 107:  # sizeof(sun_path) - 1
            self.skipTest(f"socket path too long: {self.socket}")
        self.addCleanup(self.kill_private_server)
        self.wrapper = self.bin / "tmux-private"
        self.wrapper.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(TMUX_BIN)} -S {shlex.quote(str(self.socket))}"
            ' -f /dev/null "$@"\n')
        self.wrapper.chmod(0o755)
        self.pane_count = 0

    def client_env(self, **extra):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "SHELL": "/bin/sh",
               "LANG": "C.UTF-8", "HISTFILE": "/dev/null"}
        env.update(extra)
        return env  # no TMUX/TMUX_PANE: never inherit the live server

    def tmux(self, *args, env=None, check=True):
        result = subprocess.run(
            [TMUX_BIN, "-S", str(self.socket), "-f", "/dev/null", *args],
            env=self.client_env(**(env or {})), capture_output=True, text=True,
            timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, (args, result.stderr))
        return result.stdout

    def server_answers(self):
        probe = subprocess.run(
            [TMUX_BIN, "-S", str(self.socket), "-f", "/dev/null", "list-sessions"],
            env=self.client_env(), capture_output=True, timeout=30)
        return probe.returncode == 0

    def kill_private_server(self):
        # Only through the private socket; the socket file and its directory
        # are left for the scratch TMPDIR to age out.
        if not self.server_answers():
            return
        self.tmux("kill-server", check=False)
        deadline = time.monotonic() + 5
        while self.server_answers():
            self.assertLess(time.monotonic(), deadline,
                            f"private tmux server on {self.socket} survived")
            time.sleep(0.05)

    def pane_env(self, session):
        """Environment of a fresh pane in `session` (new window, then exit)."""
        self.pane_count += 1
        out = self.root / f"pane-env-{self.pane_count}"
        tmp = self.root / f"pane-env-{self.pane_count}.tmp"
        command = (f"/usr/bin/env > {shlex.quote(str(tmp))}"
                   f" && /bin/mv {shlex.quote(str(tmp))} {shlex.quote(str(out))}")
        self.tmux("new-window", "-d", "-t", f"={session}:", command)
        deadline = time.monotonic() + 10
        while not out.exists():
            self.assertLess(time.monotonic(), deadline, f"no pane env for {session}")
            time.sleep(0.02)
        return dict(line.split("=", 1) for line in out.read_text().splitlines()
                    if "=" in line)

    def session_env(self, session):
        return self.tmux("show-environment", "-t", f"={session}").splitlines()

    def global_value(self, name):
        return self.tmux("show-environment", "-g", name).strip()

    def session_names(self):
        return sorted(self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def server_pid(self):
        return self.tmux("display-message", "-p", "#{pid}").strip()

    def start_fixture_server(self, first="__continuum_startup"):
        # The pre-fix incident: a server whose sessions shadow the global
        # environment with removal markers or attach-copied volatile values.
        self.tmux("new-session", "-d", "-s", first, "exec sleep 600",
                  env={"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-stale",
                       "XAUTHORITY": "/run/fake-global-old",
                       "XDG_SESSION_TYPE": "wayland", "DBUS_SESSION_BUS_ADDRESS": BUS})
        self.tmux("set-option", "-g", "default-shell", "/bin/sh")
        self.tmux("set-option", "-g", "update-environment",
                  production_update_environment())
        desktop = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland",
                   "DBUS_SESSION_BUS_ADDRESS": BUS}
        # Client without WAYLAND_DISPLAY: update-environment leaves
        # "-WAYLAND_DISPLAY"; older setups also left "-XAUTHORITY"/"-DISPLAY".
        self.tmux("new-session", "-d", "-s", "marked", "exec sleep 600", env=desktop)
        self.tmux("set-environment", "-t", "=marked", "-r", "XAUTHORITY")
        self.tmux("set-environment", "-t", "=marked", "-r", "DISPLAY")
        # Attach-copied values from an earlier login on another display.
        self.tmux("new-session", "-d", "-s", "copied", "exec sleep 600",
                  env={**desktop, "DISPLAY": ":1",
                       "WAYLAND_DISPLAY": "wayland-attach-old",
                       "SSH_AUTH_SOCK": "/run/fake-agent"})
        self.tmux("set-environment", "-t", "=copied", "XAUTHORITY", "/run/fake-old")
        self.tmux("new-session", "-d", "-s", "my work", "exec sleep 600", env=desktop)
        self.tmux("set-environment", "-t", "=my work", "XAUTHORITY", "/run/fake-old")
        return sorted([first, "marked", "copied", "my work"])

    def assert_sessions_clean(self, sessions):
        for session in sessions:
            with self.subTest(session):
                for line in self.session_env(session):
                    for name in CLEANED:
                        self.assertNotEqual(line, f"-{name}")
                        self.assertFalse(line.startswith(f"{name}="), line)
                env = self.pane_env(session)
                self.assertEqual(env.get("XAUTHORITY"), str(self.stable))
                self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
                self.assertEqual(env.get("DISPLAY"), ":0")

    def test_sync_drops_session_shadows_on_real_tmux(self):
        sessions = self.start_fixture_server()
        # Preconditions: the shadows really hide the global values.
        self.assertIn("-XAUTHORITY", self.session_env("marked"))
        self.assertIn("-WAYLAND_DISPLAY", self.session_env("marked"))
        before = self.pane_env("marked")
        for name in CLEANED:
            self.assertNotIn(name, before)
        before = self.pane_env("copied")
        self.assertEqual(before.get("XAUTHORITY"), "/run/fake-old")
        self.assertEqual(before.get("WAYLAND_DISPLAY"), "wayland-attach-old")
        self.assertEqual(before.get("DISPLAY"), ":1")

        auth = self.mutter_auth("INTEG1", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script("--sync", tmux=self.wrapper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assertEqual(self.global_value("XAUTHORITY"), f"XAUTHORITY={self.stable}")
        self.assertEqual(self.global_value("WAYLAND_DISPLAY"),
                         "WAYLAND_DISPLAY=wayland-0")
        self.assertEqual(self.global_value("DISPLAY"), "DISPLAY=:0")
        self.assert_sessions_clean(sessions)
        # Only the three changing variables are dropped from sessions.
        self.assertIn("SSH_AUTH_SOCK=/run/fake-agent", self.session_env("copied"))
        self.assertEqual(self.pane_env("copied").get("SSH_AUTH_SOCK"),
                         "/run/fake-agent")
        self.assertEqual(self.session_names(), sessions)

    def test_sync_without_built_cookie_keeps_real_global_xauthority(self):
        self.start_fixture_server()
        missing = self.run_dir / ".mutter-Xwaylandauth.GONE"
        self.set_dumps([self.full_dump(missing)])
        result = self.run_script("--sync", tmux=self.wrapper, polls=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.stable.exists())
        self.assertEqual(self.global_value("XAUTHORITY"),
                         "XAUTHORITY=/run/fake-global-old")
        self.assertEqual(self.global_value("WAYLAND_DISPLAY"),
                         "WAYLAND_DISPLAY=wayland-0")
        # The session copy is dropped either way; panes fall back to the old
        # global value, never to the dead per-login path.
        env = self.pane_env("copied")
        self.assertEqual(env.get("XAUTHORITY"), "/run/fake-global-old")
        self.assertEqual(env.get("WAYLAND_DISPLAY"), "wayland-0")
        self.assertEqual(env.get("DISPLAY"), ":0")

    def test_start_mode_bootstraps_real_server_with_session_environment(self):
        self.assertFalse(self.server_answers())
        first = self.mutter_auth("BOOT01", COOKIE_A)
        self.set_dumps([self.full_dump(first, LANG="ko_KR.UTF-8",
                                       SSH_AUTH_SOCK="/run/user/1000/keyring/ssh")])
        result = self.run_script(tmux=self.wrapper, inherited={"SHELL": "/bin/sh"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.session_names(), ["__continuum_startup"])
        server_env = self.tmux("show-environment", "-g").splitlines()
        for line in (f"XAUTHORITY={self.stable}", "WAYLAND_DISPLAY=wayland-0",
                     "DISPLAY=:0", "XDG_SESSION_TYPE=wayland",
                     "XDG_CURRENT_DESKTOP=ubuntu:GNOME",
                     f"DBUS_SESSION_BUS_ADDRESS={BUS}"):
            self.assertIn(line, server_env)
        self.assertFalse([line for line in server_env
                          if line.startswith(("SSH_AUTH_SOCK=", "LANG="))])
        pane = self.pane_env("__continuum_startup")
        self.assertEqual(pane.get("XAUTHORITY"), str(self.stable))
        self.assertEqual(pane.get("DISPLAY"), ":0")
        self.assert_auth(self.stable, bound(COOKIE_A))

        # Next login: the surviving server is refreshed in place.
        pid = self.server_pid()
        second = self.mutter_auth("BOOT02", COOKIE_B)
        self.set_dumps([self.full_dump(second)])
        result = self.run_script("--sync", tmux=self.wrapper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_auth(self.stable, bound(COOKIE_B))
        self.assertEqual(self.server_pid(), pid)
        self.assertEqual(self.session_names(), ["__continuum_startup"])
        self.assertEqual(self.pane_env("__continuum_startup").get("XAUTHORITY"),
                         str(self.stable))

    def test_start_mode_with_user_server_syncs_it_and_exits_75(self):
        # A terminal started tmux before tmux.service ran: the unit must not
        # adopt it (ExecStop would save and kill it), only refresh it.
        sessions = self.start_fixture_server(first="user")
        pid = self.server_pid()
        auth = self.mutter_auth("USER01", COOKIE_A)
        self.set_dumps([self.full_dump(auth)])
        result = self.run_script(tmux=self.wrapper, inherited={"SHELL": "/bin/sh"})
        self.assertEqual(result.returncode, EX_TEMPFAIL, result.stderr)
        self.assertIn(ALREADY_RUNNING, result.stderr)
        self.assertEqual(self.server_pid(), pid)
        self.assertEqual(self.session_names(), sessions)  # no bootstrap session
        self.assertEqual(self.global_value("XAUTHORITY"), f"XAUTHORITY={self.stable}")
        self.assertEqual(self.global_value("DISPLAY"), "DISPLAY=:0")
        self.assert_auth(self.stable, bound(COOKIE_A))
        self.assert_sessions_clean(sessions)


class StaticConfig(unittest.TestCase):
    def test_update_environment_excludes_xauthority(self):
        names = production_update_environment().split()
        self.assertNotIn("XAUTHORITY", names)
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            self.assertIn(name, names)

    def unit_values(self, path):
        if not path.is_file():
            self.skipTest(f"{path} not installed")
        values = {}
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), []).append(value.strip())
        return values

    @staticmethod
    def seconds(value):
        return int(value[:-1]) if value.endswith("s") else int(value)

    def test_units_run_the_script_and_unset_test_seams(self):
        base = self.unit_values(UNIT_DIR / "tmux.service")
        gui = self.unit_values(UNIT_DIR / "tmux-gui-env.service")
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(base["ExecStart"], ["%h/.dotfiles/tmux/tmux-service-start"])
        self.assertEqual(gui["ExecStart"], ["%h/.dotfiles/tmux/tmux-service-start --sync"])
        for values in (base, gui):
            unset = " ".join(values.get("UnsetEnvironment", [])).split()
            for name in ("TMUX", "TMUX_PANE", "TMUX_SERVICE_TMUX",
                         "TMUX_SERVICE_ENV_POLLS"):
                self.assertIn(name, unset)
            # ~/.local/bin before /usr/bin, so a user-local tmux is the one started.
            (path,) = [v for v in values["Environment"] if v.startswith("PATH=")]
            dirs = path.split("=", 1)[1].split(":")
            self.assertLess(dirs.index("%h/.local/bin"), dirs.index("/usr/bin"))

    def test_start_unit_contract(self):
        base = self.unit_values(UNIT_DIR / "tmux.service")
        self.assertEqual(base["Type"], ["forking"])
        (condition,) = base["ExecCondition"]
        self.assertIn("! ", condition)
        self.assertIn("has-session", condition)
        self.assertEqual(base["ExecStop"][0].lstrip("-"), "%h/.dotfiles/tmux/resurrect-save")
        self.assertEqual(base["ExecStop"][1].split()[-1], "kill-server")
        self.assertEqual(len(base["ExecStop"]), 2)
        # Exit 75 must stay a failed start so that ExecStop never runs on a
        # server the unit did not start: no ignore prefix on ExecStart, no
        # SuccessExitStatus=75, no RemainAfterExit.
        self.assertFalse(base["ExecStart"][0].startswith(("-", "+", "!", ":")))
        statuses = " ".join(base.get("SuccessExitStatus", [])).split()
        self.assertNotIn(str(EX_TEMPFAIL), statuses)
        self.assertNotIn("RemainAfterExit", base)
        self.assertIn("graphical-session.target", " ".join(base["WantedBy"]))
        # 40 polls x 0.5 s plus one 20 s xauth lock timeout fit the start timeout.
        self.assertGreater(self.seconds(base["TimeoutStartSec"][-1]), 40)

    def test_gui_refresh_unit_contract(self):
        gui = self.unit_values(UNIT_DIR / "tmux-gui-env.service")
        self.assertEqual(gui["Type"], ["oneshot"])
        after = " ".join(gui["After"]).split()
        self.assertIn("graphical-session.target", after)
        self.assertIn("tmux.service", after)
        self.assertEqual(" ".join(gui["WantedBy"]).split(), ["graphical-session.target"])
        self.assertGreater(self.seconds(gui["TimeoutStartSec"][-1]), 40)


if __name__ == "__main__":
    unittest.main()
