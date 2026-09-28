"""Unit tests for installer/gui.py with a scripted fake desktop.

No gsettings, dconf, gdbus or input-remapper-control is executed: a fake
Runner models their observable behaviour (a dconf key/value store, schema
lists, a session bus that can appear later, hanging commands) and a fake
clock drives the autostart schedule.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import gui  # noqa: E402
from installer.platform import Platform, Target  # noqa: E402
from installer.runner import RunnerError  # noqa: E402

PROC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "input-remapper" / "proc-bus-input-devices.txt"
MANIFEST = gui.load_manifest()
NOBLE = Platform("ubuntu", "24.04", "amd64")
JAMMY = Platform("ubuntu", "22.04", "amd64")


def fixed_path(schema: str) -> str:
    return "/" + schema.replace(".", "/") + "/"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.t += seconds


class FakeDesktop:
    """Runner double for gdbus/gsettings/dconf/dpkg-query/input-remapper-control."""

    def __init__(self, clock: FakeClock, release: str = "24.04") -> None:
        self.clock = clock
        self.bus_ready_at: float | None = 0.0  # None: never
        self.hang: set[str] = set()  # commands that consume their whole timeout
        self.schemas_ready_at: dict[str, float] = {}
        schemas = {e["schema"] for e in MANIFEST["settings"]}
        self.fixed = {s: fixed_path(s) for s in schemas if s != "org.gnome.Terminal.Legacy.Keybindings"}
        self.relocatable = {"org.gnome.Terminal.Legacy.Keybindings"}
        self.store: dict[str, str] = {}
        self.reject: set[tuple[str, str]] = set()
        self.not_persisting: set[str] = set()
        self.binaries = {"gdbus", "gsettings", "dconf", "input-remapper-control", "dpkg-query"}
        self.remapper_version = "2.0.1-1" if release == "24.04" else "1.4.0-1"
        self.control_rc = 0
        self.calls: list[list[str]] = []
        self.timeouts: list[tuple[float, float]] = []  # (now, timeout)

    # Runner API ----------------------------------------------------------
    def which(self, name: str):
        return f"/usr/bin/{name}" if name in self.binaries else None

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None, read_only=False):
        argv = [str(a) for a in argv]
        assert env is not None and "PERSONAL_DOTFILES_GUI_AUTOSTART" not in env
        self.calls.append(argv)
        self.timeouts.append((self.clock.t, timeout))
        assert timeout > 0
        name = argv[0]
        if name in self.hang:
            self.clock.t += timeout
            raise RunnerError(argv, None, "", f"timed out after {timeout:g}s")
        self.clock.t += 0.01
        handler = getattr(self, "_" + name.replace("-", "_"))
        rc, out, err = handler(argv[1:])
        done = subprocess.CompletedProcess(argv, rc, out.encode(), err.encode())
        if check and rc != 0:
            raise RunnerError(argv, rc, err)
        return done

    # commands ------------------------------------------------------------
    def elapsed(self) -> float:
        return self.clock.t

    def _gdbus(self, args):
        ready = self.bus_ready_at is not None and self.clock.t >= self.bus_ready_at
        return (0, "('0123',)\n", "") if ready else (1, "", "Error: Could not connect")

    def _visible(self, schema: str) -> bool:
        return self.clock.t >= self.schemas_ready_at.get(schema, 0.0)

    def _split(self, target: str):
        schema, _, path = target.partition(":")
        if path:
            return schema, path, schema in self.relocatable and self._visible(schema)
        return schema, self.fixed.get(schema), schema in self.fixed and self._visible(schema)

    def _gsettings(self, args):
        if args[:2] == ["list-schemas", "--print-paths"]:
            lines = [f"{s} {p}" for s, p in sorted(self.fixed.items()) if self._visible(s)]
            return 0, "\n".join(lines) + "\n", ""
        if args == ["list-relocatable-schemas"]:
            return 0, "\n".join(s for s in self.relocatable if self._visible(s)) + "\n", ""
        command, target, key = args[0], args[1], args[2]
        schema, path, present = self._split(target)
        if not present:
            return 1, "", f"No such schema “{schema}”\n"
        dkey = path + key
        if command == "set":
            if (schema, key) in self.reject:
                return 1, "", "0-4:unknown keyword\n"
            if dkey not in self.not_persisting:
                self.store[dkey] = args[3]
            return 0, "", ""
        if command == "reset":
            self.store.pop(dkey, None)
            return 0, "", ""
        raise AssertionError(f"unexpected gsettings {args}")

    def _dconf(self, args):
        assert args[0] == "read", args
        value = self.store.get(args[1])
        return 0, (value + "\n") if value is not None else "", ""

    def _dpkg_query(self, args):
        lines = [f"python3-inputremapper\tii \t{self.remapper_version}",
                 f"input-remapper-daemon\tii \t{self.remapper_version}"]
        return 0, "\n".join(lines) + "\n", ""

    def _input_remapper_control(self, args):
        return (self.control_rc, "", "" if self.control_rc == 0 else "daemon not running\n")

    # helpers -------------------------------------------------------------
    def sets(self):
        return [c for c in self.calls if c[:2] == ["gsettings", "set"]]


def make_target(root: Path, home_name: str = "home") -> Target:
    home = root / home_name
    for sub in ("", ".local/share", ".local/state", ".config", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return Target(uid=os.getuid(), gid=os.getgid(), username="fixture", home=home,
                  data_home=home / ".local/share", state_home=home / ".local/state",
                  config_home=home / ".config", cache_home=home / ".cache")


SESSION_ENV = {
    "PATH": "/usr/bin:/bin",
    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/fake-bus",
}


def available(release: str) -> list[dict]:
    return [e for e in MANIFEST["settings"] if e["releases"][release]["available"]]


def dkey(entry: dict) -> str:
    return (entry.get("path") or fixed_path(entry["schema"])) + entry["key"]


class GuiTestCase(unittest.TestCase):
    platform = NOBLE

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = make_target(self.root)
        self.clock = FakeClock()
        self.desk = FakeDesktop(self.clock, self.platform.release)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def apply(self, env=SESSION_ENV, components=("gnome-settings",), **kw):
        return gui.apply_or_defer(self.target, self.desk, dict(env), now=self.clock.now,
                                  sleep=self.clock.sleep, platform=self.platform,
                                  components=components, proc_devices=PROC_FIXTURE, **kw)

    def autostart(self, env=SESSION_ENV, components=("gnome-settings",)):
        return gui.run_autostart(self.target, self.desk, dict(env), now=self.clock.now,
                                 sleep=self.clock.sleep, platform=self.platform,
                                 components=components, proc_devices=PROC_FIXTURE)

    def restore(self, generation=None, components=("gnome-settings",)):
        return gui.restore_gui(self.target, self.desk, dict(SESSION_ENV), generation,
                               platform=self.platform, components=components)

    def state(self) -> dict:
        return json.loads((self.target.state_root / "gui" / "state.json").read_text())

    def assert_within_deadline(self, start: float) -> None:
        self.assertLessEqual(self.clock.t - start, gui.DEADLINE + 1e-9)
        for at, timeout in self.desk.timeouts:
            self.assertLessEqual(at + timeout - start, gui.DEADLINE + 1e-9)


class NotReadyTests(GuiTestCase):
    def test_no_session_bus_is_pending_with_state(self):
        env = {k: v for k, v in SESSION_ENV.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
        env["XDG_RUNTIME_DIR"] = str(self.root / "no-runtime")
        result = self.apply(env=env)
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertIn("gnome-settings: session-bus-unreachable", result["reasons"])
        self.assertEqual(self.desk.sets(), [])
        state = self.state()
        self.assertEqual(state["generation"], gui.generation_id("24.04"))
        self.assertEqual(state["components"]["gnome-settings"]["status"], "pending")
        gui_dir = self.target.state_root / "gui"
        self.assertEqual(stat.S_IMODE(gui_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((gui_dir / "state.json").stat().st_mode), 0o600)

    def test_not_gnome_is_pending(self):
        env = dict(SESSION_ENV, XDG_CURRENT_DESKTOP="KDE")
        result = self.apply(env=env)
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertIn("gnome-settings: no-gnome-session", result["reasons"])
        self.assertEqual(self.desk.calls, [])

    def test_unreachable_bus_is_pending(self):
        self.desk.bus_ready_at = None
        result = self.apply()
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertEqual(self.desk.sets(), [])

    def test_missing_schema_keeps_only_those_keys_pending(self):
        del self.desk.fixed["org.gnome.shell.extensions.dash-to-dock"]
        result = self.apply()
        self.assertEqual(result["status"], "PENDING_GUI")
        keys = self.state()["keys"]
        self.assertEqual(keys["dock.click-action"]["status"], "pending")
        self.assertEqual(keys["window.switch-windows"]["status"], "applied")
        self.assertIn("gnome-settings: schema-missing:org.gnome.shell.extensions.dash-to-dock",
                      result["reasons"])

    def test_dry_run_runner_is_skipped(self):
        self.desk.recorded = []
        self.assertEqual(self.apply()["status"], "SKIPPED")
        self.assertEqual(self.desk.calls, [])


class ApplyTests(GuiTestCase):
    def test_ready_session_applies_every_available_key(self):
        result = self.apply()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        for entry in available("24.04"):
            self.assertEqual(self.desk.store[dkey(entry)], entry["value"], entry["id"])
        self.assertEqual(len(self.desk.sets()), len(available("24.04")))
        backups = self.target.state_root / "gui" / "backups"
        for directory in (backups, backups / "baseline"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(stat.S_IMODE((backups / "baseline" / "gsettings.json").stat().st_mode), 0o600)

    def test_repeat_generation_does_no_duplicate_work(self):
        self.assertEqual(self.apply()["status"], "PASS")
        before = len(self.desk.calls)
        self.assertEqual(self.apply()["status"], "PASS")
        self.assertEqual(len(self.desk.calls), before, "interactive rerun repeated work")
        result = self.autostart()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["details"]["tries"], 0)
        self.assertEqual(len(self.desk.calls), before)

    def test_new_generation_applies_again_and_keeps_baseline(self):
        entry = MANIFEST["settings"][0]
        self.desk.store[dkey(entry)] = "['<Super>q']"
        self.apply()
        self.desk.calls.clear()
        with mock.patch.object(gui, "generation_id", return_value="gnext0000000000000"):
            self.assertEqual(self.apply()["status"], "PASS")
        self.assertEqual(len(self.desk.sets()), len(available("24.04")))
        backups = self.target.state_root / "gui" / "backups"
        baseline = json.loads((backups / "baseline" / "gsettings.json").read_text())
        self.assertEqual(baseline["keys"][entry["id"]]["value"], "['<Super>q']")
        newer = json.loads((backups / "gnext0000000000000" / "gsettings.json").read_text())
        self.assertEqual(newer["keys"][entry["id"]]["value"], entry["value"])

    def test_partial_key_failure_keeps_others_and_is_not_complete(self):
        self.desk.reject.add(("org.gnome.desktop.wm.keybindings", "switch-windows"))
        result = self.apply()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("gnome-settings: window.switch-windows: invalid-value", result["reasons"])
        state = self.state()
        self.assertEqual(state["keys"]["window.switch-windows"]["status"], "failed")
        self.assertEqual(state["components"]["gnome-settings"]["status"], "failed")
        applied = [k for k, v in state["keys"].items() if v["status"] == "applied"]
        self.assertEqual(len(applied), len(available("24.04")) - 1)

    def test_invalid_value_is_not_retried_by_autostart_but_by_interactive(self):
        self.desk.reject.add(("org.gnome.desktop.wm.keybindings", "switch-windows"))
        self.apply()
        self.desk.calls.clear()
        result = self.autostart()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.desk.sets(), [])
        self.desk.reject.clear()
        self.assertEqual(self.apply()["status"], "PASS")
        self.assertEqual([c[3] for c in self.desk.sets()], ["switch-windows"])

    def test_invalid_key_not_retried_while_missing_schema_is(self):
        self.desk.reject.add(("org.gnome.desktop.wm.keybindings", "switch-windows"))
        self.desk.schemas_ready_at["org.gnome.shell.extensions.dash-to-dock"] = self.clock.t + 6
        start = self.clock.t
        result = self.autostart()
        self.assertEqual(result["status"], "FAIL")
        switch_sets = [c for c in self.desk.sets() if c[3] == "switch-windows"]
        self.assertEqual(len(switch_sets), 1, "an invalid value must not be retried")
        self.assertEqual(self.state()["keys"]["dock.click-action"]["status"], "applied")
        self.assertEqual(result["details"]["tries"], 4)  # 0, 2, 5, 10
        self.assert_within_deadline(start)

    def test_value_not_in_dconf_is_never_reported_applied(self):
        entry = available("24.04")[0]
        self.desk.not_persisting.add(dkey(entry))
        result = self.apply()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.state()["keys"][entry["id"]]["reason"], "not-persisted")

    def test_unavailable_entries_are_not_applicable_on_jammy(self):
        self.platform = JAMMY
        result = self.apply()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        keys = self.state()["keys"]
        self.assertEqual(keys["application.open-new-window-application-1"]["status"],
                         "not-applicable")
        self.assertEqual(len(self.desk.sets()), len(available("22.04")))

    def test_corrupted_state_fails_without_retry(self):
        state = self.target.state_root / "gui" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{not json", encoding="utf-8")
        result = self.autostart()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.desk.calls, [])


class BackupRestoreTests(GuiTestCase):
    def test_explicit_and_unset_values_round_trip(self):
        explicit = {
            dkey(MANIFEST["settings"][0]): "['<Super>q']",
            "/org/gnome/terminal/legacy/keybindings/new-tab": "'<Control><Shift>t'",
            "/org/gnome/unmanaged/key": "'keep me'",
        }
        self.desk.store.update(explicit)
        original = dict(self.desk.store)
        self.assertEqual(self.apply()["status"], "PASS")
        backup = json.loads((self.target.state_root / "gui/backups/baseline/gsettings.json").read_text())
        first = backup["keys"][MANIFEST["settings"][0]["id"]]
        self.assertTrue(first["explicit"])
        self.assertEqual(first["value"], "['<Super>q']")
        self.assertFalse(backup["keys"]["window.minimize"]["explicit"])
        self.assertIsNone(backup["keys"]["window.minimize"]["value"])
        self.desk.calls.clear()
        result = self.restore()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(self.desk.store, original)
        commands = {c[1] for c in self.desk.calls if c[0] == "gsettings"}
        self.assertEqual(commands, {"set", "reset"})
        resets = [c for c in self.desk.calls if c[:2] == ["gsettings", "reset"]]
        self.assertEqual(len(resets), len(available("24.04")) - 2)
        # A restored generation is not re-applied at the next login.
        self.desk.calls.clear()
        self.assertEqual(self.autostart()["details"]["tries"], 0)
        self.assertEqual(self.desk.calls, [])

    def test_generation_restore_returns_values_before_that_generation(self):
        self.apply()
        with mock.patch.object(gui, "generation_id", return_value="gnext0000000000000"):
            self.apply()
            entry = MANIFEST["settings"][0]
            self.desk.store[dkey(entry)] = "['<Super>z']"
            result = self.restore("gnext0000000000000")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.desk.store[dkey(entry)], entry["value"])

    def test_restore_without_session_is_pending(self):
        self.apply()
        self.desk.bus_ready_at = None
        self.assertEqual(self.restore()["status"], "PENDING_GUI")

    def test_restore_rejects_path_like_generation(self):
        self.assertEqual(self.restore("../x")["status"], "FAIL")


class AutostartTests(GuiTestCase):
    def test_readiness_at_12s_completes_in_the_same_run(self):
        start = self.clock.t
        self.desk.bus_ready_at = start + 12
        result = self.autostart()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["tries"], 5)  # 0, 2, 5, 10, 20
        self.assertGreaterEqual(self.clock.t - start, 20)
        self.assert_within_deadline(start)

    def test_permanent_absence_expires_as_pending_within_deadline(self):
        self.desk.bus_ready_at = None
        start = self.clock.t
        result = self.autostart()
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertEqual(result["details"]["tries"], len(gui.RETRY_OFFSETS))
        reasons = self.state()["components"]["gnome-settings"]["reasons"]
        self.assertEqual(reasons, ["session-bus-unreachable", "deadline-reached"])
        self.assert_within_deadline(start)

    def test_hanging_commands_never_exceed_65_seconds(self):
        self.desk.hang.add("gdbus")
        start = self.clock.t
        result = self.autostart()
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assert_within_deadline(start)

    def test_hanging_writes_stay_pending_not_applied(self):
        self.desk.hang.add("gsettings")
        start = self.clock.t
        result = self.autostart()
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertEqual(self.state()["components"]["gnome-settings"]["status"], "pending")
        self.assertIn("deadline-reached", self.state()["components"]["gnome-settings"]["reasons"])
        self.assert_within_deadline(start)

    def test_no_gnome_session_is_not_retried(self):
        result = self.autostart(env=dict(SESSION_ENV, XDG_CURRENT_DESKTOP="ubuntu"))
        self.assertEqual(result["details"]["tries"], 1)
        self.assertEqual(result["status"], "PENDING_GUI")

    def test_autostart_env_flag_dispatches(self):
        self.desk.bus_ready_at = None
        start = self.clock.t
        result = self.apply(env=dict(SESSION_ENV, PERSONAL_DOTFILES_GUI_AUTOSTART="1"))
        self.assertEqual(result["details"]["mode"], "autostart")
        self.assertGreaterEqual(self.clock.t - start, 60)


class RemapperTests(GuiTestCase):
    def config_root(self) -> Path:
        return self.target.home / ".config" / "input-remapper-2"

    def test_jammy_is_pending_and_writes_nothing(self):
        self.platform = JAMMY
        result = self.apply(components=("input-remapper",))
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertEqual(result["reasons"], ["input-remapper: input-remapper-1.4-cannot-express-intent"])
        self.assertFalse((self.target.home / ".config" / "input-remapper").exists())
        self.assertFalse(self.config_root().exists())
        self.assertFalse(any(c[0] == "input-remapper-control" for c in self.desk.calls))
        # Permanent: the next login does not retry it.
        self.desk.calls.clear()
        self.assertEqual(self.autostart(components=("input-remapper",))["details"]["tries"], 0)

    def test_noble_writes_owned_presets_after_backup_and_autoloads(self):
        result = self.apply(components=("input-remapper",))
        self.assertEqual(result["status"], "PASS", result["reasons"])
        config = json.loads((self.config_root() / "config.json").read_text())
        self.assertEqual(config["autoload"], {
            "fixture-laptop-keyboard": "personal-dotfiles-tabs",
            "fixture-keyboard": "personal-dotfiles-tabs",
            "fixture-keyboard 2": "personal-dotfiles-tabs-2",
        })
        owned = self.config_root() / "presets" / "fixture-keyboard" / "personal-dotfiles-tabs-2.json"
        self.assertEqual(stat.S_IMODE(owned.stat().st_mode), 0o644)
        self.assertIn(["input-remapper-control", "--command", "autoload"], self.desk.calls)
        gen = gui.generation_id("24.04")
        manifest = json.loads((self.target.state_root / "gui/backups" / gen / "remapper/manifest.json").read_text())
        self.assertIn(str(owned), manifest["files"])
        self.assertFalse(manifest["files"][str(owned)]["existed"])
        # rerun: no duplicate work
        self.desk.calls.clear()
        self.assertEqual(self.apply(components=("input-remapper",))["status"], "PASS")
        self.assertEqual(self.desk.calls, [])

    def test_existing_file_is_backed_up_with_content_and_mode(self):
        self.config_root().mkdir(parents=True)
        original = b'{\n    "version": "2.0.1",\n    "autoload": {}\n}\n'
        (self.config_root() / "config.json").write_bytes(original)
        os.chmod(self.config_root() / "config.json", 0o600)
        self.apply(components=("input-remapper",))
        gen = gui.generation_id("24.04")
        root = self.target.state_root / "gui/backups" / gen / "remapper"
        record = json.loads((root / "manifest.json").read_text())["files"][str(self.config_root() / "config.json")]
        self.assertEqual((root / record["blob"]).read_bytes(), original)
        self.assertEqual(record["mode"], 0o600)
        self.assertEqual(stat.S_IMODE((self.config_root() / "config.json").stat().st_mode), 0o600)

    def test_autoload_failure_is_pending_not_fail(self):
        self.desk.control_rc = 1
        result = self.apply(components=("input-remapper",))
        self.assertEqual(result["status"], "PENDING_GUI")
        self.assertTrue(result["reasons"][0].startswith("input-remapper: autoload-failed"))
        self.assertTrue((self.config_root() / "config.json").exists())

    def test_missing_service_and_keyboards_are_pending(self):
        self.desk.binaries.discard("input-remapper-control")
        self.assertIn("input-remapper: input-remapper-control-missing",
                      self.apply(components=("input-remapper",))["reasons"])
        self.desk.binaries.add("input-remapper-control")
        empty = self.root / "devices"
        empty.write_text("", encoding="utf-8")
        result = gui.apply_or_defer(self.target, self.desk, dict(SESSION_ENV), platform=NOBLE,
                                    components=("input-remapper",), proc_devices=empty)
        self.assertEqual(result["reasons"], ["input-remapper: no-eligible-keyboard"])

    def test_symlinked_config_is_refused_and_untouched(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.config_root().symlink_to(elsewhere, target_is_directory=True)
        result = self.apply(components=("input-remapper",))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_restore_removes_owned_presets(self):
        self.apply(components=("input-remapper",))
        result = self.restore(components=("input-remapper",))
        self.assertEqual(result["status"], "PASS", result["reasons"])
        presets = list((self.config_root() / "presets").rglob("personal-dotfiles-*.json"))
        self.assertEqual(presets, [])
        config = json.loads((self.config_root() / "config.json").read_text())
        self.assertEqual(config["autoload"], {})

    def test_both_components_are_reported_separately(self):
        self.desk.control_rc = 1
        result = self.apply(components=None)
        self.assertEqual(result["status"], "PENDING_GUI")
        comps = result["details"]["components"]
        self.assertEqual(comps["gnome-settings"]["status"], "applied")
        self.assertEqual(comps["input-remapper"]["status"], "pending")


def parse_exec(value: str) -> list[str]:
    """Undo string-level escapes, then split per the Desktop Entry Exec rules."""

    unescaped = value.replace("\\\\", "\x00").replace("\\t", "\t").replace("\x00", "\\")
    args, current, quoted, i, started = [], "", False, 0, False
    while i < len(unescaped):
        ch = unescaped[i]
        if quoted:
            if ch == "\\" and i + 1 < len(unescaped) and unescaped[i + 1] in '"`$\\':
                current += unescaped[i + 1]
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                current += ch
        elif ch == '"':
            quoted, started = True, True
        elif ch == " ":
            if current or started:
                args.append(current)
            current, started = "", False
        else:
            current += ch
        i += 1
    if current or started:
        args.append(current)
    return [a.replace("%%", "%") for a in args]


class AutostartEntryTests(unittest.TestCase):
    def test_exec_escaping_with_spaces_and_specials(self):
        for home_name in ("plain", "my home", 'odd "q" $x `y` back\\slash 100%'):
            with self.subTest(home=home_name), tempfile.TemporaryDirectory() as tmp:
                target = make_target(Path(tmp), home_name)
                entry = gui.autostart_desired_entry(target)
                text = entry.content.decode("utf-8")
                exec_line = next(l for l in text.splitlines() if l.startswith("Exec="))
                argv = parse_exec(exec_line[len("Exec="):])
                self.assertEqual(argv, ["/usr/bin/python3", str(target.compat_link / "install.py"),
                                        "gui-apply", "--autostart"])
                if " " in home_name:
                    self.assertIn('"', exec_line)
                # shell-like split agrees for the space case
                if home_name == "my home":
                    self.assertEqual(shlex.split(exec_line[len("Exec="):])[1],
                                     str(target.compat_link / "install.py"))

    def test_entry_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = make_target(Path(tmp))
            entry = gui.autostart_desired_entry(target)
            self.assertEqual(entry.kind, "file")
            self.assertEqual(entry.mode, 0o644)
            self.assertEqual(entry.dest, target.config_home / "autostart" / gui.AUTOSTART_NAME)
            lines = entry.content.decode().splitlines()
            self.assertEqual(lines[0], "[Desktop Entry]")
            self.assertIn("OnlyShowIn=GNOME;", lines)
            self.assertIn("X-GNOME-Autostart-enabled=true", lines)
            self.assertIn("Type=Application", lines)

    def test_template_has_no_source_literals(self):
        text = gui.AUTOSTART_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(text.count("@EXEC@"), 1)
        self.assertNotIn("/home/", text)
        self.assertNotIn(os.environ.get("USER", "\0") or "\0", text)
        text.encode("ascii")

    def test_line_breaks_are_refused(self):
        with self.assertRaises(gui.GuiError):
            gui.desktop_exec_quote("a\nb")


if __name__ == "__main__":
    unittest.main()
