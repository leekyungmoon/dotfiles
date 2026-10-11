"""installer/launchd.py: plists, launchctl status mapping, registration.

Runnable on any host. launchd is simulated in memory (:class:`LaunchdSim`,
also used by tests/unit/test_install_cli.py and the macOS install flow in
tests/integration/test_install_flow.py), and the bounded waits run on a fake
clock. Nothing here runs a real launchctl, tmux or locale, except the
read-only checks in RealMacTests, which run only on macOS.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import launchd  # noqa: E402
from installer.phases import LAUNCHD_GUI_DOMAIN_UNAVAILABLE  # noqa: E402
from installer.platform import Target  # noqa: E402
from installer.runner import Runner  # noqa: E402

LABELS = (launchd.LABEL_TMUX, launchd.LABEL_AUTOSAVE)


def make_target(home: Path, **roots) -> Target:
    return Target(uid=os.getuid(), gid=os.getgid(), username="fixture", home=home,
                  data_home=roots.get("data_home", home / ".local/share"),
                  state_home=roots.get("state_home", home / ".local/state"),
                  config_home=roots.get("config_home", home / ".config"),
                  cache_home=roots.get("cache_home", home / ".cache"))


def completed(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, out.encode(), err.encode())


class LaunchdSim:
    """launchd as launchctl shows it, in memory.

    Enough of print, print-disabled, bootstrap, bootout, enable, disable and
    kickstart for the installer: bootstrap reads the plist from disk (so a
    test sees exactly what the transaction wrote), a RunAtLoad job "runs"
    at once and ends with ``exit_codes[label]`` (None: still running).
    ``codes[(verb, label)]`` forces return codes (a list, used in order);
    ``linger[label]`` keeps a booted-out service listed for that many more
    prints (launchd finishes a bootout asynchronously; -1: forever).
    ``busy[label]``: a run is still going, so a bootout answers 36
    (Operation now in progress) and the service stays listed for that many
    more prints while launchd stops the run (-1: forever).
    """

    def __init__(self, uid: int, *, gui: bool = True, legacy_disabled: bool = False):
        self.uid = uid
        self.gui = gui
        self.legacy_disabled = legacy_disabled
        self.loaded: dict[str, dict] = {}
        self.disabled: dict[str, bool] = {}
        self.codes: dict[tuple, list[int]] = {}
        self.linger: dict[str, int] = {}
        self.lingering: dict[str, dict] = {}
        self.busy: dict[str, int] = {}
        self.exit_codes: dict[str, int | None] = {}
        # label -> exit code reached after that many prints while running
        self.finish_after: dict[str, tuple[int, int]] = {}

    # -- helpers for tests -----------------------------------------------------

    def load(self, label: str, **fields) -> None:
        """Pretend ``label`` is loaded (as at login), with these print fields."""

        self.loaded[label] = {"state": "not running", "pid": None, "last_exit_code": 0,
                              "run_interval": None, "path": None, "program": None,
                              **fields}

    def force(self, verb: str, label: str, *codes: int) -> None:
        self.codes[(verb, label)] = list(codes)

    # -- launchctl ---------------------------------------------------------------

    def handle(self, argv: list[str]) -> subprocess.CompletedProcess:
        verb = argv[1]
        target = argv[-1] if verb != "bootstrap" else argv[2]
        label = target.split("/", 2)[2] if target.count("/") >= 2 else None
        forced = self.codes.get((verb, label or (Path(argv[3]).stem
                                                 if verb == "bootstrap" else None)))
        if forced:
            code = forced.pop(0)
            if code != 0 or verb != "bootstrap":
                return completed(argv, code, err=f"{verb} failed: {code}\n")
        domain = f"gui/{self.uid}"
        if verb == "print" and target == domain:
            return completed(argv, 0, out=f"{domain} = {{\n\ttype = login\n}}\n") \
                if self.gui else completed(argv, 112, err="Could not find domain\n")
        if not self.gui:
            return completed(argv, 112 if verb != "bootstrap" else 125,
                             err="Could not find specified domain\n")
        if verb == "print":
            return self._print(argv, label)
        if verb == "print-disabled":
            yes, no = ("true", "false") if self.legacy_disabled else ("disabled", "enabled")
            body = "".join(f'\t"{name}" => {yes if off else no}\n'
                           for name, off in sorted(self.disabled.items()))
            return completed(argv, 0, out=f"disabled services = {{\n{body}}}\n"
                                          "login item associations = {\n}\n")
        if verb == "bootstrap":
            return self._bootstrap(argv)
        if verb == "bootout":
            if label not in self.loaded:
                return completed(argv, 3, err="Boot-out failed: 3: No such process\n")
            fields = self.loaded.pop(label)
            if label in self.busy:
                self.linger[label] = self.busy.pop(label)
                self.lingering[label] = fields
                return completed(argv, 36,
                                 err="Boot-out failed: 36: Operation now in progress\n")
            if self.linger.get(label):
                self.lingering[label] = fields
            return completed(argv, 0)
        if verb in ("enable", "disable"):
            self.disabled[label] = verb == "disable"
            return completed(argv, 0)
        if verb == "kickstart":
            return completed(argv, 0 if label in self.loaded else 113)
        raise AssertionError(f"unexpected launchctl call {argv}")

    def _bootstrap(self, argv):
        plist = Path(argv[3])
        with open(plist, "rb") as handle:
            data = plistlib.load(handle)
        label = data["Label"]
        if self.disabled.get(label) or label in self.loaded or label in self.lingering:
            return completed(argv, 5, err="Bootstrap failed: 5: Input/output error\n")
        fields = {"state": "not running", "pid": None, "last_exit_code": None,
                  "run_interval": data.get("StartInterval"), "path": str(plist),
                  "program": data["ProgramArguments"][0],
                  "arguments": list(data["ProgramArguments"])}
        if data.get("RunAtLoad"):
            code = self.exit_codes.get(label, 0)
            if code is None:
                fields.update(state="running", pid=4242)
            else:
                fields["last_exit_code"] = code
        self.loaded[label] = fields
        return completed(argv, 0)

    def _print(self, argv, label):
        fields = self.loaded.get(label)
        if fields is None and label in self.lingering:
            left = self.linger[label]
            if left == 0:
                del self.lingering[label]
            else:
                self.linger[label] = left - 1 if left > 0 else left
                fields = self.lingering[label]
        if fields is None:
            return completed(argv, 113, err=f'Could not find service "{label}" in domain '
                                            f'for user gui: {self.uid}\n')
        if label in self.finish_after and fields.get("state") == "running":
            prints, code = self.finish_after[label]
            if prints <= 0:
                fields.update(state="not running", pid=None, last_exit_code=code)
            else:
                self.finish_after[label] = (prints - 1, code)
        return completed(argv, 0, out=service_text(self.uid, label, fields))


def service_text(uid: int, label: str, fields: dict) -> str:
    """``launchctl print gui/<uid>/<label>`` as macOS 15 prints it, with
    nested blocks that must not be mistaken for the service's own fields."""

    lines = [f"gui/{uid}/{label} = {{", "\tactive count = 0"]
    if fields.get("path"):
        lines.append(f"\tpath = {fields['path']}")
    lines += ["\ttype = LaunchAgent", f"\tstate = {fields.get('state') or 'not running'}", ""]
    if fields.get("program"):
        lines.append(f"\tprogram = {fields['program']}")
        lines += ["\targuments = {",
                  *[f"\t\t{arg}" for arg in fields.get("arguments") or [fields["program"]]],
                  "\t}", ""]
    lines += ["\tenvironment = {", "\t\tPATH => /usr/bin:/bin",
              f"\t\tXPC_SERVICE_NAME => {label}", "\t}", "",
              f"\tdomain = gui/{uid} [100008]", "\tminimum runtime = 10",
              "\texit timeout = 5", "\truns = 1"]
    if fields.get("pid") is not None:
        lines.append(f"\tpid = {fields['pid']}")
    code = fields.get("last_exit_code")
    if fields.get("last_signal"):
        lines.append(f"\tlast terminating signal = {fields['last_signal']}")
    else:
        lines.append("\tlast exit code = "
                     + ("(never exited)" if code is None else
                        "78: EX_CONFIG" if code == 78 else str(code)))
    if fields.get("run_interval") is not None:
        lines += ["", f"\trun interval = {fields['run_interval']} seconds"]
    lines += ["", "\tevent triggers = {", f"\t\t{label}.268435459 => {{",
              "\t\t\tkeepalive = 0", "\t\t\tstate = 1", "\t\t\tdescriptor = {",
              '\t\t\t\t"interval" => 60', "\t\t\t}", "\t\t}", "\t}", "",
              "\tendpoints = {", '\t\t"com.example.endpoint" = {', "\t\t\tport = 0x2b03",
              "\t\t\tactive = 0", "\t\t}", "\t}", "",
              "\tspawn type = daemon (3)", "\tjetsam memory limit (active) = (unlimited)",
              "\tproperties = runatload | inferred program", "}"]
    return "\n".join(lines) + "\n"


class SimRunner(Runner):
    """Runner whose launchctl is :class:`LaunchdSim`; records every call with
    its read_only flag and env; ``locale -a`` and ``tmux has-session`` are
    scripted; anything else is a test failure."""

    def __init__(self, sim: LaunchdSim, *, locales: str | None = "C\nen_US.UTF-8\n",
                 server: bool = False, tmux: str | None = "/opt/homebrew/bin/tmux"):
        self.sim = sim
        self.locales = locales
        self.server = server
        self.tmux = tmux
        self.calls: list[list[str]] = []
        self.read_only: list[bool] = []
        self.envs: list[dict | None] = []
        self.which_paths: list[str | None] = []

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None,
            read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.read_only.append(read_only)
        self.envs.append(env)
        if argv[0] == launchd.LAUNCHCTL:
            return self.sim.handle(argv)
        if argv[:2] == [launchd.LOCALE, "-a"]:
            if self.locales is None:
                return completed(argv, 1, err="locale: failed\n")
            return completed(argv, 0, out=self.locales)
        if argv[1:] == ["has-session"]:
            return completed(argv, 0 if self.server else 1)
        raise AssertionError(f"unexpected command {argv}")

    def which(self, name, path=None):
        self.which_paths.append(path)
        return self.tmux if name == "tmux" else None

    def launchctl(self, verb: str | None = None) -> list[list[str]]:
        return [c for c in self.calls if c[0] == launchd.LAUNCHCTL
                and (verb is None or c[1] == verb)]


@contextmanager
def fake_clock():
    """launchd's polling sleeps on a clock that only moves when it sleeps."""

    now = [1000.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    with mock.patch.object(launchd, "_sleep", side_effect=sleep), \
            mock.patch.object(launchd, "_monotonic", side_effect=lambda: now[0]):
        yield slept


def install_plists(target, brew_prefix="/opt/homebrew", lang="en_US.UTF-8") -> None:
    """What the transaction does with launchd.desired_entries."""

    for entry in launchd.desired_entries(target, brew_prefix=brew_prefix, lang=lang):
        entry.dest.parent.mkdir(parents=True, exist_ok=True)
        entry.dest.write_bytes(entry.content)
        os.chmod(entry.dest, entry.mode)


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf-launchd-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        self.target = make_target(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def sim(self, **kw) -> LaunchdSim:
        return LaunchdSim(self.target.uid, **kw)


# --- plists ---------------------------------------------------------------------

class RenderTests(TempHome):
    def render(self, agent, target=None, brew="/opt/homebrew", lang="en_US.UTF-8"):
        return launchd.render(agent, target or self.target, brew_prefix=brew, lang=lang)

    def test_render_is_deterministic_and_exact(self):
        tmux = self.render(launchd.TMUX)
        self.assertEqual(tmux, self.render(launchd.TMUX))
        self.assertTrue(tmux.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'))
        data = plistlib.loads(tmux)
        log = str(self.target.state_home / "personal-dotfiles/launchd"
                  / f"{launchd.LABEL_TMUX}.log")
        self.assertEqual(data, {
            "Label": launchd.LABEL_TMUX,
            "ProgramArguments": [str(self.home / ".dotfiles/tmux/tmux-launchd-start")],
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "AbandonProcessGroup": True,
            "LimitLoadToSessionType": "Aqua",
            "WorkingDirectory": str(self.home),
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "EnvironmentVariables": {
                "PATH": launchd.launch_path(self.target, "/opt/homebrew"),
                "LANG": "en_US.UTF-8",
                "PERSONAL_DOTFILES_LAUNCHD_LOG": log,
            },
        })
        # Keys sorted, so the bytes do not depend on dict order.
        keys = [line.strip() for line in tmux.decode().splitlines()
                if line.strip().startswith("<key>")]
        top = [k for k in keys if k[5:-6] in data]
        self.assertEqual(top, sorted(top))

    def test_autosave_agent(self):
        data = plistlib.loads(self.render(launchd.AUTOSAVE))
        log = str(self.target.state_home / "personal-dotfiles/launchd"
                  / f"{launchd.LABEL_AUTOSAVE}.log")
        self.assertEqual(data["Label"], launchd.LABEL_AUTOSAVE)
        self.assertEqual(data["ProgramArguments"],
                         [str(self.home / ".dotfiles/tmux/resurrect-autosave")])
        for key, value in (("StartInterval", 60), ("RunAtLoad", False),
                           ("ProcessType", "Background"), ("LowPriorityIO", True),
                           ("Nice", 10), ("Umask", 0o077), ("LimitLoadToSessionType", "Aqua"),
                           ("WorkingDirectory", str(self.home)), ("StandardOutPath", log),
                           ("StandardErrorPath", log)):
            self.assertEqual(data[key], value, key)
        self.assertEqual(data["EnvironmentVariables"]["PERSONAL_DOTFILES_LAUNCHD_LOG"], log)
        self.assertNotIn("AbandonProcessGroup", data)

    def test_never_keepalive_tmux_or_test_seams(self):
        for agent in launchd.AGENTS:
            data = plistlib.loads(self.render(agent))
            self.assertNotIn("KeepAlive", data)
            env = data["EnvironmentVariables"]
            for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR", "TMUX_LAUNCHD_TMUX",
                         "TMUX_LAUNCHD_START_WAIT", "TMUX_LAUNCHD_FINISH_WAIT",
                         "TMUX_AUTOSAVE_TIMEOUT"):
                self.assertNotIn(name, env)
            self.assertNotIn(b"Tmux.Start", self.render(agent))

    def test_paths_are_absolute(self):
        for agent in launchd.AGENTS:
            text = self.render(agent).decode()
            data = plistlib.loads(text.encode())
            for path in (data["ProgramArguments"][0], data["StandardOutPath"],
                         data["StandardErrorPath"], data["WorkingDirectory"],
                         *data["EnvironmentVariables"]["PATH"].split(":")):
                self.assertTrue(path.startswith("/"), path)
            # launchd expands neither of these.
            self.assertNotIn("~", text)
            self.assertNotIn("$HOME", text)
            self.assertNotIn("${", text)

    def test_xml_escaping_for_any_home(self):
        for name in ("a & b", "x<y>z", "two words", "한글 café", "it's \"q\""):
            with self.subTest(home=name):
                home = self.root / name
                home.mkdir()
                target = make_target(home)
                content = launchd.render(launchd.TMUX, target, brew_prefix="/opt/homebrew",
                                         lang="en_US.UTF-8")
                if "&" in name:
                    self.assertIn(b"a &amp; b", content)
                if "<" in name:
                    self.assertIn(b"x&lt;y&gt;z", content)
                self.assertNotIn(b"<y>", content)
                data = plistlib.loads(content)
                self.assertEqual(data["WorkingDirectory"], str(home))
                self.assertEqual(data["ProgramArguments"],
                                 [str(home / ".dotfiles/tmux/tmux-launchd-start")])
                if not set(name) & set("&<>"):
                    self.assertIn(name.encode("utf-8"), content)  # UTF-8, not escaped

    def test_launch_path_order_and_dedupe(self):
        data = str(self.target.data_home / "personal-dotfiles/bin")
        local = str(self.home / ".local/bin")
        self.assertEqual(launchd.launch_path(self.target, "/opt/homebrew").split(":"),
                         [data, local, "/opt/homebrew/bin", "/opt/homebrew/sbin",
                          "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        # Intel: Homebrew is /usr/local, listed once where Homebrew goes.
        self.assertEqual(launchd.launch_path(self.target, Path("/usr/local")).split(":"),
                         [data, local, "/usr/local/bin", "/usr/local/sbin", "/usr/bin",
                          "/bin", "/usr/sbin", "/sbin"])
        self.assertEqual(launchd.launch_path(self.target, None).split(":"),
                         [data, local, "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
                          "/sbin"])
        # A ':' cannot be part of a PATH element: such a directory is left out.
        odd = self.root / "a:b"
        odd.mkdir()
        self.assertEqual(launchd.launch_path(make_target(odd), "/opt/homebrew").split(":"),
                         ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                          "/usr/bin", "/bin", "/usr/sbin", "/sbin"])

    def test_xdg_only_when_not_default(self):
        env = launchd.environment(self.target, launchd.TMUX, brew_prefix="/opt/homebrew",
                                  lang="ko_KR.UTF-8")
        self.assertEqual(sorted(env), ["LANG", "PATH", "PERSONAL_DOTFILES_LAUNCHD_LOG"])
        self.assertEqual(env["LANG"], "ko_KR.UTF-8")
        custom = make_target(self.home, data_home=self.home / "data",
                             state_home=self.home / "state")
        env = launchd.environment(custom, launchd.AUTOSAVE, brew_prefix="/opt/homebrew",
                                  lang="en_US.UTF-8")
        self.assertEqual(env["XDG_DATA_HOME"], str(self.home / "data"))
        self.assertEqual(env["XDG_STATE_HOME"], str(self.home / "state"))
        self.assertNotIn("XDG_CONFIG_HOME", env)
        self.assertNotIn("XDG_CACHE_HOME", env)
        self.assertEqual(env["PERSONAL_DOTFILES_LAUNCHD_LOG"],
                         str(self.home / "state/personal-dotfiles/launchd"
                             / f"{launchd.LABEL_AUTOSAVE}.log"))
        self.assertTrue(env["PATH"].startswith(str(self.home / "data/personal-dotfiles/bin:")))

    def test_desired_entries(self):
        entries = launchd.desired_entries(self.target, brew_prefix="/opt/homebrew",
                                          lang="en_US.UTF-8")
        self.assertEqual([e.id for e in entries], ["launchd-tmux",
                                                   "launchd-tmux-resurrect-autosave"])
        self.assertEqual(launchd.ENTRY_IDS, tuple(e.id for e in entries))
        for entry, agent in zip(entries, launchd.AGENTS):
            self.assertEqual(entry.dest, self.home / "Library/LaunchAgents"
                             / f"{agent.label}.plist")
            self.assertEqual((entry.kind, entry.mode), ("file", 0o644))
            self.assertEqual(entry.content, self.render(agent))

    def test_agent_names(self):
        self.assertEqual([a.name for a in launchd.AGENTS], ["tmux", "autosave"])
        self.assertIs(launchd.agent_named("autosave"), launchd.AUTOSAVE)
        with self.assertRaises(KeyError):
            launchd.agent_named("Tmux.Start")


class PickLangTests(TempHome):
    def pick(self, env, locales="C\nPOSIX\nen_US.UTF-8\nko_KR.UTF-8\nfr_FR.ISO8859-1\n"):
        runner = SimRunner(self.sim(), locales=locales)
        return launchd.pick_lang(env, runner), runner

    def test_first_listed_utf8_value(self):
        self.assertEqual(self.pick({"LANG": "ko_KR.UTF-8"})[0], "ko_KR.UTF-8")
        # LC_ALL first, then LC_CTYPE, then LANG; non-UTF-8 values are skipped.
        self.assertEqual(self.pick({"LC_ALL": "ko_KR.UTF-8", "LANG": "en_US.UTF-8"})[0],
                         "ko_KR.UTF-8")
        self.assertEqual(self.pick({"LC_ALL": "C", "LC_CTYPE": "fr_FR.ISO8859-1",
                                    "LANG": "ko_KR.UTF-8"})[0], "ko_KR.UTF-8")
        self.assertEqual(self.pick({"LC_CTYPE": "ko_KR.UTF-8", "LANG": "en_US.UTF-8"})[0],
                         "ko_KR.UTF-8")
        # Not listed: the next one, else the default.
        self.assertEqual(self.pick({"LC_ALL": "xx_YY.UTF-8", "LANG": "ko_KR.UTF-8"})[0],
                         "ko_KR.UTF-8")
        self.assertEqual(self.pick({"LANG": "de_DE.UTF-8"})[0], "en_US.UTF-8")

    def test_spelling_is_the_listed_one(self):
        # Linux spells it en_US.utf8; macOS lists en_US.UTF-8.
        self.assertEqual(self.pick({"LANG": "ko_KR.utf8"})[0], "ko_KR.UTF-8")
        self.assertEqual(self.pick({"LANG": "KO_kr.Utf-8"}, locales="ko_KR.utf8\n")[0],
                         "ko_KR.utf8")

    def test_default_without_a_usable_value(self):
        for env in ({}, {"LANG": "C"}, {"LC_CTYPE": "UTF-8"}, {"LANG": "en_US"},
                    {"LANG": "en_US.ISO8859-1"}):
            lang, runner = self.pick(env)
            self.assertEqual(lang, "en_US.UTF-8", env)
            self.assertEqual(runner.calls, [], env)  # no locale -a without a candidate

    def test_locale_failure_means_default(self):
        lang, runner = self.pick({"LANG": "ko_KR.UTF-8"}, locales=None)
        self.assertEqual(lang, "en_US.UTF-8")
        self.assertEqual(runner.calls, [[launchd.LOCALE, "-a"]])
        self.assertEqual(runner.read_only, [True])

        class Broken(SimRunner):
            def run(self, argv, **kw):
                raise OSError("no locale")
        self.assertEqual(launchd.pick_lang({"LANG": "ko_KR.UTF-8"}, Broken(self.sim())),
                         "en_US.UTF-8")


class AgentEnvTests(TempHome):
    def test_from_the_installed_plist(self):
        install_plists(self.target, lang="ko_KR.UTF-8")
        env = launchd.agent_env(self.target, launchd.TMUX,
                                {"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%1",
                                 "TMUX_TMPDIR": "/private", "TMPDIR": "/var/t",
                                 "SHELL": "/bin/zsh", "HOME": "/elsewhere", "PATH": "/x"})
        self.assertEqual(env, {
            "HOME": str(self.home), "USER": "fixture", "LOGNAME": "fixture",
            "SHELL": "/bin/zsh", "TMPDIR": "/var/t", "LANG": "ko_KR.UTF-8",
            "PATH": launchd.launch_path(self.target, "/opt/homebrew")})

    def test_without_a_plist(self):
        env = launchd.agent_env(self.target, launchd.TMUX, {})
        self.assertEqual(env["PATH"], launchd.launch_path(self.target, None))
        self.assertNotIn("LANG", env)

    def test_with_a_plist_edited_into_something_else(self):
        plist = launchd.plist_path(self.target, launchd.TMUX)
        plist.parent.mkdir(parents=True)
        for content in (b"<plist><dict><key>x</key>", b"not xml at all",
                        plistlib.dumps(["a", "list"])):
            plist.write_bytes(content)
            env = launchd.agent_env(self.target, launchd.TMUX, {})
            self.assertEqual(env["PATH"], launchd.launch_path(self.target, None))


# --- launchctl output -----------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_service_fields(self):
        text = service_text(501, launchd.LABEL_AUTOSAVE, {
            "path": "/Volumes/Home/x/Library/LaunchAgents/a.plist", "state": "not running",
            "program": "/Volumes/Home/x/.dotfiles/tmux/resurrect-autosave",
            "last_exit_code": 0, "run_interval": 60})
        info = launchd.parse_service(text)
        self.assertEqual(info.state, "not running")  # not the nested "state = 1"
        self.assertIsNone(info.pid)
        self.assertEqual(info.last_exit_code, 0)
        self.assertEqual(info.run_interval, 60)
        self.assertEqual(info.path, "/Volumes/Home/x/Library/LaunchAgents/a.plist")
        self.assertEqual(info.program, "/Volumes/Home/x/.dotfiles/tmux/resurrect-autosave")
        self.assertEqual(info.arguments, [info.program])
        self.assertFalse(info.running)
        self.assertTrue(info.exited)

    def test_running_never_exited_described_and_signalled(self):
        running = launchd.parse_service(service_text(501, "x", {
            "state": "running", "pid": 812, "last_exit_code": None}))
        self.assertEqual((running.state, running.pid, running.last_exit_code),
                         ("running", 812, None))
        self.assertTrue(running.running)
        self.assertFalse(running.exited)
        fresh = launchd.parse_service(service_text(501, "x", {"last_exit_code": None}))
        self.assertFalse(fresh.running)
        self.assertFalse(fresh.exited)  # loaded, never ran yet
        described = launchd.parse_service(service_text(501, "x", {"last_exit_code": 78}))
        self.assertEqual(described.last_exit_code, 78)  # "78: EX_CONFIG"
        killed = launchd.parse_service(service_text(501, "x", {
            "last_signal": "Killed: 9"}))
        self.assertEqual((killed.last_exit_code, killed.last_signal), (None, "Killed: 9"))
        self.assertTrue(killed.exited)
        self.assertEqual(launchd.parse_service("not what launchctl prints\n"),
                         launchd.ServiceInfo())

    def test_disabled_both_formats(self):
        modern = 'disabled services = {\n\t"a" => disabled\n\t"b" => enabled\n}\n' \
                 'login item associations = {\n\t"c" => "com.example.app"\n}\n'
        self.assertEqual(launchd.parse_disabled(modern), {"a": True, "b": False})
        legacy = 'disabled services = {\n\t"a" => true\n\t"b" => false\n}\n'
        self.assertEqual(launchd.parse_disabled(legacy), {"a": True, "b": False})

    def test_error_text(self):
        self.assertEqual(launchd.error_text(5), "5: Input/output error")
        self.assertEqual(launchd.error_text(119), "119: Service is disabled")
        self.assertEqual(launchd.error_text(9999), "9999; see: launchctl error 9999")


class LaunchctlCallTests(TempHome):
    def test_argv_and_read_only(self):
        sim = self.sim()
        runner = SimRunner(sim)
        install_plists(self.target)
        uid = self.target.uid
        plist = launchd.plist_path(self.target, launchd.AUTOSAVE)
        launchd.gui_domain_available(runner, uid)
        launchd.disabled_labels(runner, uid)
        launchd.service_info(runner, uid, launchd.LABEL_AUTOSAVE)
        launchd.bootstrap(runner, uid, plist)
        launchd.kickstart(runner, uid, launchd.LABEL_AUTOSAVE, kill=True)
        launchd.disable(runner, uid, launchd.LABEL_AUTOSAVE)
        launchd.enable(runner, uid, launchd.LABEL_AUTOSAVE)
        launchd.bootout(runner, uid, launchd.LABEL_AUTOSAVE)
        service = f"gui/{uid}/{launchd.LABEL_AUTOSAVE}"
        self.assertEqual([c[1:] for c in runner.calls], [
            ["print", f"gui/{uid}"], ["print-disabled", f"gui/{uid}"], ["print", service],
            ["bootstrap", f"gui/{uid}", str(plist)], ["kickstart", "-k", service],
            ["disable", service], ["enable", service], ["bootout", service]])
        self.assertTrue(all(c[0] == "/bin/launchctl" for c in runner.calls))
        # Only the queries run under --dry-run.
        self.assertEqual(runner.read_only, [True, True, True, False, False, False, False,
                                            False])

    def test_queries_that_cannot_answer(self):
        class Broken(SimRunner):
            def run(self, argv, **kw):
                raise OSError("launchctl missing")
        runner = Broken(self.sim())
        self.assertFalse(launchd.gui_domain_available(runner, 501))
        self.assertIsNone(launchd.disabled_labels(runner, 501))
        self.assertIsNone(launchd.service_info(runner, 501, "x"))
        self.assertIsNone(launchd.disabled_labels(SimRunner(self.sim(gui=False)), 501))


# --- registration ------------------------------------------------------------------

class RegisterTests(TempHome):
    def setUp(self):
        super().setUp()
        install_plists(self.target)
        self.plist = launchd.plist_path(self.target, launchd.AUTOSAVE)

    def register(self, sim, agent=launchd.AUTOSAVE, changed=False, **kw):
        runner = SimRunner(sim, **kw)
        with fake_clock() as slept:
            result = launchd.register(self.target, runner, agent, changed=changed, env={})
        self.slept = slept
        return result, runner

    def loaded_as_installed(self, sim, agent=launchd.AUTOSAVE, **fields):
        plist = launchd.plist_path(self.target, agent)
        program = str(launchd.program_path(self.target, agent))
        defaults = {"path": str(plist), "program": program,
                    "run_interval": 60 if agent is launchd.AUTOSAVE else None}
        sim.load(agent.label, **{**defaults, **fields})

    def test_fresh_bootstrap_is_verified(self):
        sim = self.sim()
        result, runner = self.register(sim)
        self.assertEqual(result.status, "PASS", result.reasons)
        self.assertEqual(result.reasons, [])
        self.assertEqual([c[1] for c in runner.launchctl()],
                         ["print", "print-disabled", "print", "bootstrap", "print"])
        self.assertEqual(result.details["action"], "bootstrapped")
        self.assertEqual(result.details["run_interval"], 60)
        self.assertEqual(result.details["plist"], str(self.plist))
        self.assertEqual(result.details["log"], str(launchd.log_path(self.target,
                                                                     launchd.AUTOSAVE)))
        self.assertIn(launchd.LABEL_AUTOSAVE, sim.loaded)

    def test_unchanged_and_loaded_is_left_alone(self):
        # The idempotent repair: no bootout, no bootstrap, no wait.
        sim = self.sim()
        self.loaded_as_installed(sim)
        result, runner = self.register(sim)
        self.assertEqual((result.status, result.details["action"]), ("PASS", "unchanged"))
        self.assertEqual(runner.launchctl("bootout") + runner.launchctl("bootstrap"), [])
        self.assertEqual(self.slept, [])
        self.assertTrue(all(runner.read_only))  # only queries

    def test_kept_plist_counts_as_unchanged(self):
        # install.py passes changed=False for a plist the user edited (kept).
        sim = self.sim()
        self.loaded_as_installed(sim)
        result, runner = self.register(sim, changed=False)
        self.assertEqual(result.details["action"], "unchanged")
        self.assertEqual(runner.launchctl("bootstrap"), [])

    def test_unchanged_but_not_loaded_is_bootstrapped(self):
        result, runner = self.register(self.sim())
        self.assertEqual(result.details["action"], "bootstrapped")
        self.assertEqual(runner.launchctl("bootout"), [])
        self.assertEqual(len(runner.launchctl("bootstrap")), 1)

    def test_changed_and_loaded_is_reloaded_after_it_is_gone(self):
        sim = self.sim()
        self.loaded_as_installed(sim)
        sim.linger[launchd.LABEL_AUTOSAVE] = 2  # listed for two more prints
        result, runner = self.register(sim, changed=True)
        self.assertEqual(result.status, "PASS", result.reasons)
        self.assertEqual((result.details["action"], result.details["reloaded"]),
                         ("reloaded", "changed"))
        verbs = [c[1] for c in runner.launchctl()]
        self.assertEqual(verbs, ["print", "print-disabled", "print", "bootout", "print",
                                 "print", "print", "bootstrap", "print"])
        self.assertEqual(self.slept, [launchd.POLL_INTERVAL] * 2)

    def test_a_reload_waits_out_a_run_in_progress(self):
        # A running job's bootout answers 36: launchd stops the run, then
        # lets the job go; the reload waits for that before the bootstrap.
        sim = self.sim()
        self.loaded_as_installed(sim, state="running", pid=99, last_exit_code=None)
        sim.busy[launchd.LABEL_AUTOSAVE] = 2
        result, runner = self.register(sim, changed=True)
        self.assertEqual(result.status, "PASS", result.reasons)
        self.assertEqual(result.details["action"], "reloaded")
        verbs = [c[1] for c in runner.launchctl()]
        self.assertEqual(verbs, ["print", "print-disabled", "print", "bootout", "print",
                                 "print", "print", "bootstrap", "print"])
        self.assertEqual(self.slept, [launchd.POLL_INTERVAL] * 2)

    def test_bootout_that_never_finishes_fails(self):
        sim = self.sim()
        self.loaded_as_installed(sim)
        sim.linger[launchd.LABEL_AUTOSAVE] = -1
        result, runner = self.register(sim, changed=True)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("still loaded 10 s after launchctl bootout", result.reasons[0])
        self.assertIn(f"run: launchctl bootout gui/$(id -u)/{launchd.LABEL_AUTOSAVE}",
                      result.reasons[0])
        self.assertEqual(runner.launchctl("bootstrap"), [])
        self.assertAlmostEqual(sum(self.slept), launchd.BOOTOUT_WAIT, delta=0.5)

    def test_bootout_error_fails_without_bootstrap(self):
        sim = self.sim()
        self.loaded_as_installed(sim)
        sim.force("bootout", launchd.LABEL_AUTOSAVE, 1)
        result, runner = self.register(sim, changed=True)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("(1: Operation not permitted)", result.reasons[0])
        self.assertEqual(runner.launchctl("bootstrap"), [])
        # Gone meanwhile ("No such process", "Could not find service"): fine.
        for code in (3, 113):
            sim = self.sim()
            self.loaded_as_installed(sim)
            original = sim.handle

            def handle(argv, code=code, sim=sim, original=original):
                if argv[1] == "bootout":
                    sim.loaded.pop(launchd.LABEL_AUTOSAVE, None)
                    return completed(argv, code)
                return original(argv)
            sim.handle = handle
            result, runner = self.register(sim, changed=True)
            self.assertEqual(result.status, "PASS", (code, result.reasons))
            self.assertEqual(len(runner.launchctl("bootstrap")), 1)

    def test_a_loaded_definition_that_is_not_this_plist_is_reloaded(self):
        for fields in ({"path": "/Volumes/Old/a.plist"}, {"program": "/opt/old/autosave"},
                       {"run_interval": 300}):
            with self.subTest(fields=fields):
                sim = self.sim()
                self.loaded_as_installed(sim, **fields)
                result, runner = self.register(sim, changed=False)
                self.assertEqual(result.details["action"], "reloaded")
                self.assertEqual(len(runner.launchctl("bootout")), 1)
                self.assertEqual(len(runner.launchctl("bootstrap")), 1)
        # What print does not say is no difference.
        sim = self.sim()
        self.loaded_as_installed(sim, path=None, program=None, run_interval=None)
        result, runner = self.register(sim, changed=False)
        self.assertEqual(result.details["action"], "unchanged")

    def test_disabled_stays_off(self):
        for legacy in (False, True):
            sim = self.sim(legacy_disabled=legacy)
            sim.disabled[launchd.LABEL_AUTOSAVE] = True
            sim.disabled[launchd.LABEL_TMUX] = False
            result, runner = self.register(sim, changed=True)
            self.assertEqual(result.status, "SKIPPED")
            self.assertEqual(result.reasons, ["autosave: disabled (enable: dotfiles services "
                                              "enable autosave)"])
            self.assertEqual(result.details["action"], "disabled")
            self.assertEqual(runner.launchctl("bootstrap") + runner.launchctl("bootout"), [])

    def test_no_gui_domain_is_relogin(self):
        result, runner = self.register(self.sim(gui=False), changed=True)
        self.assertEqual((result.status, result.reasons),
                         ("RELOGIN_REQUIRED", [LAUNCHD_GUI_DOMAIN_UNAVAILABLE]))
        self.assertEqual(result.details["command"],
                         f"launchctl bootstrap gui/$(id -u) {self.plist}")
        self.assertEqual([c[1:] for c in runner.calls], [["print", f"gui/{self.target.uid}"]])

    def test_commands_quote_odd_paths(self):
        home = self.root / "two words"
        home.mkdir()
        target = make_target(home)
        install_plists(target)
        runner = SimRunner(self.sim(gui=False))
        result = launchd.register(target, runner, launchd.TMUX, changed=False)
        plist = launchd.plist_path(target, launchd.TMUX)
        self.assertEqual(result.details["command"],
                         f"launchctl bootstrap gui/$(id -u) '{plist}'")

    def test_bootstrap_status_mapping(self):
        background = "'Allow in the Background'"
        cases = (
            # (forced bootstrap code, loaded afterwards, status, words in the reason)
            (5, True, "PASS", None),       # already loaded, or a race
            (5, False, "FAIL", ("5: Input/output error", background, "not over SSH",
                                "launchctl bootstrap gui/$(id -u)")),
            (37, True, "PASS", None),
            (37, False, "FAIL", ("37: Operation already in progress", background)),
            (17, True, "PASS", None),
            (112, False, "RELOGIN_REQUIRED", (LAUNCHD_GUI_DOMAIN_UNAVAILABLE,)),
            (125, False, "RELOGIN_REQUIRED", (LAUNCHD_GUI_DOMAIN_UNAVAILABLE,)),
            (119, False, "SKIPPED", ("disabled (enable: dotfiles services enable autosave)",)),
            (122, False, "FAIL", ("122: Path had bad ownership/permissions", "chmod 644")),
            (134, False, "FAIL", ("134: Service cannot load in requested session",
                                  "desktop session")),
            (150, False, "FAIL", ("150: Operation not permitted while System Integrity",)),
            (201, False, "FAIL", ("201; see: launchctl error 201",)),
        )
        for code, loaded, status, words in cases:
            with self.subTest(code=code, loaded=loaded):
                sim = self.sim()
                sim.force("bootstrap", launchd.LABEL_AUTOSAVE, code)
                if loaded:
                    self.loaded_as_installed(sim)
                runner = SimRunner(sim)
                # Not loaded at the first probe: a bootstrap is attempted.
                original = sim.handle
                probes = []

                def handle(argv, original=original, probes=probes):
                    if argv[1] == "print" and argv[-1].endswith(launchd.LABEL_AUTOSAVE) \
                            and not probes:
                        probes.append(argv)
                        return completed(argv, 113)
                    return original(argv)
                sim.handle = handle
                with fake_clock():
                    result = launchd.register(self.target, runner, launchd.AUTOSAVE,
                                              changed=False)
                self.assertEqual(result.status, status, result.reasons)
                self.assertEqual(len(runner.launchctl("bootstrap")), 1)
                for word in words or ():
                    self.assertIn(word, " ".join(result.reasons))
                if status == "PASS":
                    self.assertEqual(result.reasons, [])
                if code in (112, 125):
                    self.assertIn("command", result.details)

    def test_bootstrap_success_without_print_evidence_fails(self):
        sim = self.sim()
        original = sim.handle

        def handle(argv):
            if argv[1] == "print" and argv[-1].endswith(launchd.LABEL_AUTOSAVE):
                return completed(argv, 113)  # never shows up
            return original(argv)
        sim.handle = handle
        result, _ = self.register(sim)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("launchctl print does not find", result.reasons[0])

    def test_a_launchctl_that_hangs_is_a_failure_not_an_exception(self):
        from installer.runner import RunnerError
        for verb in ("bootstrap", "bootout"):
            sim = self.sim()
            self.loaded_as_installed(sim)
            original = sim.handle

            def handle(argv, verb=verb, original=original):
                if argv[1] == verb:
                    raise RunnerError(argv, None, "", "timed out after 60s")
                return original(argv)
            sim.handle = handle
            result, _ = self.register(sim, changed=True)
            self.assertEqual(result.status, "FAIL", verb)
            self.assertIn("timed out after 60s", result.reasons[0])
            self.assertIn("run: launchctl bootstrap gui/$(id -u)", result.reasons[0])
            self.assertEqual(result.details["action"], "failed")

    def test_missing_plist_fails(self):
        self.plist.unlink()
        result, runner = self.register(self.sim())
        self.assertEqual(result.status, "FAIL")
        self.assertIn("is missing; run: dotfiles repair", result.reasons[0])
        self.assertEqual(runner.calls, [])

    def test_autosave_interval_is_verified(self):
        sim = self.sim()
        original = sim._bootstrap

        def bootstrap(argv):
            done = original(argv)
            sim.loaded[launchd.LABEL_AUTOSAVE]["run_interval"] = 30
            return done
        sim._bootstrap = bootstrap
        result, _ = self.register(sim)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("every 30 s, not 60 s", result.reasons[0])


class TmuxAgentTests(TempHome):
    """How the tmux agent's run ended (tmux/tmux-launchd-start)."""

    def setUp(self):
        super().setUp()
        install_plists(self.target, lang="ko_KR.UTF-8")
        self.log = str(launchd.log_path(self.target, launchd.TMUX))

    def register(self, exit_code, *, server=False, finish_after=None, changed=True):
        sim = LaunchdSim(self.target.uid)
        sim.exit_codes[launchd.LABEL_TMUX] = exit_code
        if finish_after is not None:
            sim.finish_after[launchd.LABEL_TMUX] = finish_after
        runner = SimRunner(sim, server=server)
        with fake_clock() as slept:
            result = launchd.register(self.target, runner, launchd.TMUX, changed=changed,
                                      env={"TMUX": "/tmp/tmux-1/default,1,0",
                                           "TMUX_TMPDIR": "/private/x"})
        return result, runner, slept

    def test_exit_0_is_started(self):
        result, runner, slept = self.register(0)
        self.assertEqual((result.status, result.reasons), ("PASS", []))
        self.assertEqual(result.details["outcome"], "started")
        self.assertEqual(result.details["last_exit_code"], 0)
        self.assertEqual(slept, [])
        self.assertEqual([c for c in runner.calls if "has-session" in c], [])

    def test_exit_3_is_a_warning_with_the_way_back(self):
        result, _, _ = self.register(3)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["outcome"], "not-restored")
        self.assertEqual(len(result.reasons), 1)
        self.assertIn(self.log, result.reasons[0])
        self.assertIn("prefix + C-r", result.reasons[0])

    def test_failures_name_the_log(self):
        for code, words in ((1, "tmux not found"), (78, "exit status 78"),
                            (2, "exit status 2")):
            with self.subTest(code=code):
                result, _, _ = self.register(code)
                self.assertEqual(result.status, "FAIL")
                self.assertIn(words, result.reasons[0])
                self.assertIn(self.log, result.reasons[0])

    def test_exit_1_names_every_cause(self):
        # tmux exits 1 on every error, so a failed new-session ends the
        # script with 1 as well, not only a missing tmux or HOME.
        result, _, _ = self.register(1)
        self.assertEqual(result.reasons, [
            "the tmux LaunchAgent could not start tmux (exit status 1: tmux not found, "
            f"HOME unusable, or `tmux new-session` failed); see {self.log}"])

    def test_killed_is_a_failure(self):
        sim = LaunchdSim(self.target.uid)
        original = sim._bootstrap

        def bootstrap(argv):
            done = original(argv)
            sim.loaded[launchd.LABEL_TMUX].update(last_exit_code=None,
                                                  last_signal="Killed: 9")
            return done
        sim._bootstrap = bootstrap
        with fake_clock():
            result = launchd.register(self.target, SimRunner(sim), launchd.TMUX,
                                      changed=True)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("stopped (Killed: 9)", result.reasons[0])

    def test_still_running_is_not_a_failure(self):
        # Restoring: the server answers, the job keeps running.
        result, runner, slept = self.register(None, server=True)
        self.assertEqual((result.status, result.reasons), ("PASS", []))
        self.assertEqual(result.details["outcome"], "running")
        self.assertEqual(result.details["pid"], 4242)
        self.assertAlmostEqual(sum(slept), launchd.TMUX_START_WAIT, delta=0.5)
        # tmux is looked up and asked as the agent would: its PATH, no TMUX.
        agent_path = launchd.launch_path(self.target, "/opt/homebrew")
        self.assertIn(agent_path, runner.which_paths)
        index = runner.calls.index(["/opt/homebrew/bin/tmux", "has-session"])
        env = runner.envs[index]
        self.assertEqual((env["PATH"], env["LANG"], env["HOME"]),
                         (agent_path, "ko_KR.UTF-8", str(self.home)))
        for name in ("TMUX", "TMUX_TMPDIR", "TMUX_PANE"):
            self.assertNotIn(name, env)
        self.assertTrue(runner.read_only[index])

    def test_still_starting_without_a_server(self):
        result, _, _ = self.register(None, server=False)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["outcome"], "starting")
        self.assertIn("still starting", result.reasons[0])
        self.assertIn(self.log, result.reasons[0])

    def test_a_run_that_ends_while_polled(self):
        result, runner, slept = self.register(None, finish_after=(3, 0))
        self.assertEqual(result.details["outcome"], "started")
        self.assertEqual(result.status, "PASS")
        self.assertLess(sum(slept), launchd.TMUX_START_WAIT)

    def test_unchanged_and_loaded_tmux_agent_is_not_started_again(self):
        sim = LaunchdSim(self.target.uid)
        sim.load(launchd.LABEL_TMUX, path=str(launchd.plist_path(self.target, launchd.TMUX)),
                 program=str(launchd.program_path(self.target, launchd.TMUX)),
                 last_exit_code=3)
        runner = SimRunner(sim)
        with fake_clock() as slept:
            result = launchd.register(self.target, runner, launchd.TMUX, changed=False)
        self.assertEqual((result.status, result.details["action"]), ("PASS", "unchanged"))
        self.assertEqual(result.reasons, [])  # its login run is not this run's news
        self.assertEqual(runner.launchctl("bootstrap") + runner.launchctl("kickstart"), [])
        self.assertEqual(slept, [])


class UnloadAndStatusTests(TempHome):
    def test_unload_outcomes(self):
        sim = self.sim()
        sim.load("a")
        sim.force("bootout", "c", 113)
        sim.force("bootout", "d", 1)
        outcomes = {}
        problems = launchd.unload(self.target, SimRunner(sim), ["a", "b", "c", "d"],
                                  outcomes=outcomes)
        self.assertEqual(outcomes, {"a": "unloaded", "b": "not-loaded", "c": "not-loaded",
                                    "d": "failed"})
        self.assertEqual(len(problems), 1)
        self.assertIn("(1: Operation not permitted)", problems[0])
        self.assertIn("run: launchctl bootout gui/$(id -u)/d", problems[0])
        outcomes = {}
        self.assertEqual(launchd.unload(self.target, SimRunner(self.sim(gui=False)), ["a"],
                                        outcomes=outcomes), [])
        self.assertEqual(outcomes, {"a": "no-gui-domain"})

    def test_unload_waits_out_a_run_in_progress(self):
        # The autosave job mid-save: bootout answers 36 (in progress), and
        # launchd lets the job go once the run has stopped. Unloaded.
        sim = self.sim()
        sim.load(launchd.LABEL_AUTOSAVE, state="running", pid=99, last_exit_code=None)
        sim.busy[launchd.LABEL_AUTOSAVE] = 2
        runner = SimRunner(sim)
        outcomes = {}
        with fake_clock() as slept:
            problems = launchd.unload(self.target, runner, [launchd.LABEL_AUTOSAVE],
                                      outcomes=outcomes)
        self.assertEqual((problems, outcomes), ([], {launchd.LABEL_AUTOSAVE: "unloaded"}))
        self.assertEqual([c[1] for c in runner.launchctl()],
                         ["bootout", "print", "print", "print"])
        self.assertEqual(runner.read_only, [False, True, True, True])  # the wait only asks
        self.assertEqual(slept, [launchd.POLL_INTERVAL] * 2)

    def test_unload_of_a_job_that_never_leaves_fails(self):
        sim = self.sim()
        sim.load(launchd.LABEL_AUTOSAVE, state="running", pid=99, last_exit_code=None)
        sim.busy[launchd.LABEL_AUTOSAVE] = -1
        outcomes = {}
        with fake_clock() as slept:
            problems = launchd.unload(self.target, SimRunner(sim), [launchd.LABEL_AUTOSAVE],
                                      outcomes=outcomes)
        self.assertEqual(outcomes, {launchd.LABEL_AUTOSAVE: "failed"})
        self.assertEqual(problems, [
            f"{launchd.LABEL_AUTOSAVE} was still loaded 10 s after launchctl bootout; "
            f"run: launchctl bootout gui/$(id -u)/{launchd.LABEL_AUTOSAVE}"])
        self.assertAlmostEqual(sum(slept), launchd.BOOTOUT_WAIT, delta=0.5)

    def test_status(self):
        install_plists(self.target)
        sim = self.sim()
        sim.load(launchd.LABEL_TMUX, last_exit_code=0)
        sim.load(launchd.LABEL_AUTOSAVE, run_interval=60, state="running", pid=99,
                 last_exit_code=None)
        sim.disabled[launchd.LABEL_AUTOSAVE] = False
        saved = launchd.last_save_path(self.target)
        saved.parent.mkdir(parents=True)
        saved.touch()
        os.utime(saved, (5000, 5000))
        runner = SimRunner(sim)
        view = launchd.status(self.target, runner, now=5042)
        self.assertTrue(all(runner.read_only))
        self.assertEqual(view["manager"], "launchd")
        self.assertEqual(view["domain"], f"gui/{self.target.uid}")
        self.assertTrue(view["gui_domain"])
        self.assertEqual(view["last_successful_save"],
                         {"path": str(saved), "age_seconds": 42})
        tmux, autosave = view["agents"]["tmux"], view["agents"]["autosave"]
        self.assertEqual((tmux["loaded"], tmux["disabled"], tmux["last_exit_code"],
                          tmux["plist_present"]), (True, False, 0, True))
        self.assertEqual((autosave["pid"], autosave["run_interval"], autosave["state"]),
                         (99, 60, "running"))
        self.assertEqual(autosave["log"], str(launchd.log_path(self.target,
                                                               launchd.AUTOSAVE)))
        lines = launchd.describe_status(view)
        self.assertEqual(lines[0], f"services:  launchd gui/{self.target.uid}")
        self.assertIn("  tmux       loaded, not running, last exit 0", lines)
        self.assertIn("  autosave   loaded, running (pid 99), every 60 s", lines)
        self.assertEqual(lines[-1], "  last save: 42 s ago")

    def test_status_when_nothing_is_there(self):
        sim = self.sim(gui=False)
        view = launchd.status(self.target, SimRunner(sim))
        self.assertFalse(view["gui_domain"])
        for entry in view["agents"].values():
            self.assertEqual((entry["plist_present"], entry["loaded"], entry["disabled"]),
                             (False, None, None))
        self.assertIsNone(view["last_successful_save"]["age_seconds"])
        text = "\n".join(launchd.describe_status(view))
        self.assertIn("no GUI login session here", text)
        self.assertIn("not installed (no plist; run: dotfiles repair)", text)
        self.assertIn("last save: never", text)
        install_plists(self.target)
        sim = self.sim()
        sim.disabled[launchd.LABEL_TMUX] = True
        text = "\n".join(launchd.describe_status(launchd.status(self.target,
                                                                SimRunner(sim))))
        self.assertIn("tmux       disabled (enable: dotfiles services enable tmux)", text)
        self.assertIn("autosave   not loaded (load: dotfiles services enable autosave)", text)


class ContinuumLauncherTests(TempHome):
    def test_nothing_to_do(self):
        runner = SimRunner(self.sim())
        self.assertIsNone(launchd.retire_continuum_launcher(self.target, runner, "run1"))
        self.assertEqual(runner.calls, [])

    def test_unloaded_and_set_aside(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "Tmux.Start.plist").write_text("<plist/>")
        install_plists(self.target)
        ours = {p.name: p.read_bytes() for p in agents.glob("com.*.plist")}
        sim = self.sim()
        sim.load("Tmux.Start.plist")
        runner = SimRunner(sim)
        report = launchd.retire_continuum_launcher(self.target, runner, "run1")
        self.assertEqual(runner.calls, [["/bin/launchctl", "bootout",
                                         f"gui/{self.target.uid}/Tmux.Start.plist"]])
        backup = self.target.state_root / "backups/launchd/run1/Tmux.Start.plist"
        self.assertEqual(report, {"plist": str(agents / "Tmux.Start.plist"),
                                  "bootout": "unloaded", "moved_to": str(backup)})
        self.assertEqual(backup.read_text(), "<plist/>")
        self.assertFalse(os.path.lexists(agents / "Tmux.Start.plist"))
        for folder in (backup.parent, backup.parent.parent):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        # Never one of ours.
        self.assertEqual({p.name: p.read_bytes() for p in agents.glob("com.*.plist")}, ours)

    def test_a_failed_bootout_still_sets_it_aside(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "Tmux.Start.plist").write_text("x")
        sim = self.sim()
        sim.force("bootout", "Tmux.Start.plist", 1)
        report = launchd.retire_continuum_launcher(self.target, SimRunner(sim), "run2")
        self.assertEqual(report["bootout"], "failed")
        self.assertIn("Operation not permitted", report["bootout_error"])
        self.assertTrue(Path(report["moved_to"]).is_file())


class LogDirTests(TempHome):
    def test_private_log_dir(self):
        path = launchd.prepare_log_dir(self.target)
        self.assertEqual(path, self.target.state_root / "launchd")
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        os.chmod(path, 0o755)
        launchd.prepare_log_dir(self.target)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)


@unittest.skipUnless(sys.platform == "darwin", "needs macOS's plutil and launchctl")
class RealMacTests(TempHome):
    """Read-only checks against the real tools; nothing is loaded."""

    def test_plutil_accepts_the_plists(self):
        install_plists(self.target)
        for agent in launchd.AGENTS:
            done = subprocess.run(["/usr/bin/plutil", "-lint",
                                   str(launchd.plist_path(self.target, agent))],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_queries_parse_real_output(self):
        runner = Runner()
        uid = os.getuid()
        if not launchd.gui_domain_available(runner, uid):
            self.skipTest("no GUI login domain for this user here")
        states = launchd.disabled_labels(runner, uid)
        self.assertIsInstance(states, dict)
        self.assertTrue(all(isinstance(v, bool) for v in states.values()))
        # A service every GUI login has; only its own fields are read.
        info = launchd.service_info(runner, uid, "com.apple.Finder")
        if info is not None:
            self.assertTrue(info.state)


if __name__ == "__main__":
    unittest.main()
