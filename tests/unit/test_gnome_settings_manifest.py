"""Structural tests for desktop/gnome-settings.json and its validator.

None of these need GNOME, GLib or a D-Bus session: the manifest is parsed as
data, and the validator's gsettings probes run against a fake ``gsettings``
executable. The real-schema check at the bottom only runs when a compiled
release schema dir is supplied through the environment, e.g.::

    GNOME_SETTINGS_SCHEMADIR_24_04=/path/to/compiled \\
        python3 -m unittest tests.unit.test_gnome_settings_manifest
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import pwd
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import platform as plat  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "desktop" / "gnome-settings.json"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate-gnome-settings.py"
TERMINAL_KEYBINDINGS_PATH = "/org/gnome/terminal/legacy/keybindings/"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_gnome_settings", VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


V = load_validator()


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def by_id(manifest: dict) -> dict[str, dict]:
    return {entry["id"]: entry for entry in manifest["settings"]}


class ManifestStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.entries = by_id(self.manifest)

    def test_validator_reports_no_structural_errors(self) -> None:
        self.assertEqual(V.structural_errors(self.manifest), [])

    def test_ids_and_targets_are_unique(self) -> None:
        ids = [e["id"] for e in self.manifest["settings"]]
        self.assertEqual(len(ids), len(set(ids)))
        targets = [(e["schema"], e.get("path"), e["key"]) for e in self.manifest["settings"]]
        self.assertEqual(len(targets), len(set(targets)))

    def test_ids_are_semantic_slugs(self) -> None:
        for ident in self.entries:
            self.assertRegex(ident, r"^[a-z][a-z-]*\.[a-z0-9-]+$")

    def test_releases_match_supported_platforms(self) -> None:
        self.assertEqual(set(self.manifest["releases"]), set(plat.SUPPORTED_RELEASES))
        for entry in self.manifest["settings"]:
            self.assertEqual(set(entry["releases"]), set(plat.SUPPORTED_RELEASES), entry["id"])

    def test_every_value_is_well_formed_gvariant(self) -> None:
        for entry in self.manifest["settings"]:
            value = V.parse_gvariant(entry["value"], entry["type"])
            if entry["type"] == "as":
                self.assertIsInstance(value, list, entry["id"])
                if entry.get("unbinds"):
                    self.assertEqual(value, [], entry["id"])
                    self.assertEqual(entry["group"], "window-tiling", entry["id"])
                else:
                    self.assertTrue(value, f"{entry['id']}: an empty list would unbind the action")
            for accel in V.accelerators_of(entry):
                V.normalize_accelerator(accel)

    def test_global_accelerators_carry_a_modifier(self) -> None:
        for entry in self.manifest["settings"]:
            for accel in V.accelerators_of(entry):
                modifiers, _ = V.normalize_accelerator(accel)
                self.assertTrue(modifiers, f"{entry['id']}: bare key {accel!r}")

    def test_no_accelerator_is_bound_twice(self) -> None:
        self.assertEqual(V.accelerator_conflicts(self.manifest), [])

    def test_relocatable_path_only_for_terminal_keybindings(self) -> None:
        for entry in self.manifest["settings"]:
            if entry["schema"] == "org.gnome.Terminal.Legacy.Keybindings":
                self.assertEqual(entry.get("path"), TERMINAL_KEYBINDINGS_PATH, entry["id"])
            else:
                self.assertNotIn("path", entry, entry["id"])

    def test_requested_behaviour_is_present(self) -> None:
        expected = {
            "terminal.new-tab": "'<Control>t'",
            "terminal.new-window": "'<Control><Shift>t'",
            "terminal.close-tab": "'<Control>w'",
            "terminal.prev-tab": "'<Control>Page_Up'",
            "terminal.next-tab": "'<Control>Page_Down'",
            "window.switch-windows": "['<Alt>Tab']",
            "window.switch-group": "['<Super>Above_Tab', '<Alt>Above_Tab']",
            "launcher.terminal": "['<Primary><Alt>t']",
            "terminal.paste": "'<Control><Shift>v'",
            "dock.click-action": "'cycle-windows'",
            "tiling.edge-tiling": "false",
        }
        for ident, value in expected.items():
            self.assertIn(ident, self.entries)
            self.assertEqual(self.entries[ident]["value"], value, ident)

    def test_super_arrow_unbindings_form_the_window_tiling_group(self) -> None:
        tiling = {e["id"]: e for e in self.manifest["settings"] if e["group"] == "window-tiling"}
        self.assertEqual(
            {(e["schema"], e["key"], e["value"]) for e in tiling.values()},
            {
                ("org.gnome.mutter.keybindings", "toggle-tiled-left", "@as []"),
                ("org.gnome.mutter.keybindings", "toggle-tiled-right", "@as []"),
                ("org.gnome.desktop.wm.keybindings", "maximize", "@as []"),
                ("org.gnome.desktop.wm.keybindings", "unmaximize", "@as []"),
                ("org.gnome.mutter", "edge-tiling", "false"),
            },
        )
        for entry in tiling.values():
            for release in plat.SUPPORTED_RELEASES:
                self.assertTrue(entry["releases"][release]["available"], entry["id"])
                self.assertFalse(entry["releases"][release]["equals_default"], entry["id"])

    def test_dock_hotkeys_stay_unmanaged(self) -> None:
        dock = [e for e in self.manifest["settings"] if e["schema"].endswith("dash-to-dock")]
        self.assertEqual([e["key"] for e in dock], ["click-action"])
        self.assertEqual(dock[0]["type"], "enum")

    def test_dock_ctrl_super_collision_is_marked_unverified(self) -> None:
        for n in range(1, 10):
            note = self.entries[f"application.open-new-window-application-{n}"]["note"]
            self.assertIn(f"app-ctrl-hotkey-{n}", note)
            self.assertIn("UNVERIFIED", note)
        self.assertTrue(any("<Ctrl><Super>1..9" in item for item in self.manifest["unverified"]))

    def test_application_new_window_keys_are_noble_only(self) -> None:
        for n in range(1, 10):
            entry = self.entries[f"application.open-new-window-application-{n}"]
            self.assertEqual(entry["releases"]["22.04"], {"available": False})
            self.assertTrue(entry["releases"]["24.04"]["available"])
            self.assertIn("22.04", entry["note"])

    def test_equals_default_matches_recorded_default(self) -> None:
        for entry in self.manifest["settings"]:
            for release, record in entry["releases"].items():
                if record["available"]:
                    self.assertIs(
                        record["equals_default"],
                        record["default"] == entry["value"],
                        f"{entry['id']} [{release}]",
                    )

    def test_chrome_is_documented_as_not_gsettings(self) -> None:
        topics = {item["topic"]: item["reason"] for item in self.manifest["unmanaged"]}
        self.assertIn("chrome-tabs", topics)
        self.assertIn("not GSettings", topics["chrome-tabs"])
        for entry in self.manifest["settings"]:
            self.assertNotIn("chrome", entry["schema"].lower())


class NoHostDataTests(unittest.TestCase):
    FORBIDDEN = (
        (r"/home/", "absolute home path"),
        (r"/Users/", "absolute home path"),
        (r"/root/", "root home path"),
        (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "UUID"),
        (r"\b[0-9a-fA-F]{16,}\b", "long hex id or hash"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "e-mail address"),
        (r"/dev/input/", "input device node"),
        (r"(?i)\b(vendor|product|serial)[-_ ]?id\b", "device identifier field"),
        (r"(?i)custom-keybindings/", "per-user custom keybinding path"),
        (r"(?i)profiles:/", "per-user terminal profile path"),
    )
    # A short or generic login would match ordinary manifest words.
    GENERIC_NAMES = {"root", "user", "ubuntu", "gnome", "runner", "admin", "test"}

    def setUp(self) -> None:
        self.text = MANIFEST_PATH.read_text(encoding="utf-8")

    def test_no_forbidden_patterns(self) -> None:
        for pattern, label in self.FORBIDDEN:
            match = re.search(pattern, self.text)
            self.assertIsNone(match, f"manifest contains a {label}: {match and match.group(0)!r}")

    def test_no_current_login_or_hostname(self) -> None:
        names = set()
        try:
            names.add(pwd.getpwuid(os.getuid()).pw_name)
        except KeyError:
            pass
        names.add(os.environ.get("USER", ""))
        host = socket.gethostname()
        names.update({host, host.split(".")[0]})
        for name in names:
            if len(name) < 3 or name.lower() in self.GENERIC_NAMES:
                continue
            self.assertIsNone(
                re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", self.text),
                "manifest contains the current login or hostname",
            )

    def test_manifest_is_ascii(self) -> None:
        self.text.encode("ascii")


class ParserTests(unittest.TestCase):
    def test_string_lists(self) -> None:
        self.assertEqual(V.parse_gvariant("@as []", "as"), [])
        self.assertEqual(V.parse_gvariant("[]", "as"), [])
        self.assertEqual(V.parse_gvariant("['<Alt>Tab']", "as"), ["<Alt>Tab"])
        self.assertEqual(
            V.parse_gvariant("['<Super>Page_Up', '<Control><Alt>Left']", "as"),
            ["<Super>Page_Up", "<Control><Alt>Left"],
        )

    def test_rejects_malformed_values(self) -> None:
        for text, gtype in (
            ("['<Alt>Tab'", "as"),
            ("['<Alt>Tab',]", "as"),
            ("['<Alt>Tab''<Super>Tab']", "as"),
            ('["<Alt>Tab"]', "as"),
            ("'<Alt>Tab'", "as"),
            ("<Control>t", "s"),
            ("'it\\'s'", "s"),
            ("True", "b"),
            ("1", "i"),
        ):
            with self.subTest(text=text, gtype=gtype):
                with self.assertRaises(ValueError):
                    V.parse_gvariant(text, gtype)

    def test_accelerator_normalization(self) -> None:
        same = V.normalize_accelerator("<Control>t")
        for spelling in ("<Primary>t", "<Ctrl>T", "<control>t"):
            self.assertEqual(V.normalize_accelerator(spelling), same, spelling)
        self.assertEqual(
            V.normalize_accelerator("<Shift><Super>Tab"),
            V.normalize_accelerator("<Super><Shift>Tab"),
        )
        self.assertNotEqual(V.normalize_accelerator("<Control>t"), V.normalize_accelerator("<Control><Shift>t"))
        self.assertNotEqual(V.normalize_accelerator("<Alt>Tab"), V.normalize_accelerator("<Alt>Above_Tab"))

    def test_rejects_bad_accelerators(self) -> None:
        for accel in ("<Control>", "<Bogus>t", "<Control>t extra", "<Control>+t", ""):
            with self.subTest(accel=accel):
                with self.assertRaises(ValueError):
                    V.normalize_accelerator(accel)

    def test_disabled_terminal_binding_binds_nothing(self) -> None:
        entry = {"scope": "app:gnome-terminal", "type": "s", "value": "'disabled'"}
        self.assertEqual(V.accelerators_of(entry), [])


def _entry(ident: str, value: str, *, scope: str = "global", gtype: str = "as", **extra) -> dict:
    record = {"available": True, "default": value, "equals_default": True}
    entry = {
        "id": ident,
        "group": "window-navigation",
        "scope": scope,
        "schema": "org.gnome.desktop.wm.keybindings",
        "key": ident.split(".", 1)[1],
        "value": value,
        "type": gtype,
        "releases": {release: dict(record) for release in plat.SUPPORTED_RELEASES},
    }
    entry.update(extra)
    return entry


def _manifest(*entries: dict) -> dict:
    return {
        "schema_version": 1,
        "defaults_desktop": "ubuntu:GNOME",
        "releases": {release: {} for release in plat.SUPPORTED_RELEASES},
        "schema_packages": {
            schema: "fixture-package" for schema in V.ALLOWED_SCHEMAS
        },
        "settings": list(entries),
    }


class ConflictTests(unittest.TestCase):
    def test_duplicate_in_one_scope_is_reported(self) -> None:
        manifest = _manifest(
            _entry("window.a", "['<Alt>Tab']"),
            _entry("window.b", "['<Super>x', '<Alt>Tab']"),
        )
        problems = V.accelerator_conflicts(manifest)
        self.assertEqual(len(problems), 1)
        self.assertIn("window.a and window.b", problems[0])

    def test_primary_and_control_spellings_collide(self) -> None:
        manifest = _manifest(
            _entry("window.a", "['<Primary><Alt>t']"),
            _entry("window.b", "['<Control><Alt>t']"),
        )
        self.assertEqual(len(V.accelerator_conflicts(manifest)), 1)

    def test_same_value_twice_in_one_entry_is_reported(self) -> None:
        manifest = _manifest(_entry("window.a", "['<Alt>Tab', '<Alt>Tab']"))
        self.assertEqual(len(V.accelerator_conflicts(manifest)), 1)

    def test_mutual_share_is_allowed(self) -> None:
        manifest = _manifest(
            _entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.b"]),
            _entry("window.b", "['<Alt>Tab']", shares_accelerator_with=["window.a"]),
        )
        self.assertEqual(V.accelerator_conflicts(manifest), [])

    def test_one_sided_share_is_not_enough(self) -> None:
        manifest = _manifest(
            _entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.b"]),
            _entry("window.b", "['<Alt>Tab']"),
        )
        self.assertEqual(len(V.accelerator_conflicts(manifest)), 1)

    def test_application_key_shadowed_by_global_grab(self) -> None:
        manifest = _manifest(
            _entry("window.a", "['<Control>Page_Up']"),
            _entry("terminal.prev-tab", "'<Control>Page_Up'", scope="app:gnome-terminal", gtype="s"),
        )
        problems = V.accelerator_conflicts(manifest)
        self.assertEqual(len(problems), 1)
        self.assertIn("grabs first", problems[0])

    def test_same_key_in_different_app_scopes_is_fine_alone(self) -> None:
        manifest = _manifest(
            _entry("terminal.close-tab", "'<Control>w'", scope="app:gnome-terminal", gtype="s"),
        )
        self.assertEqual(V.accelerator_conflicts(manifest), [])


class GuardAndShareTests(unittest.TestCase):
    def test_non_dict_inputs_do_not_crash(self) -> None:
        self.assertEqual(V.structural_errors([]), ["manifest must be a JSON object"])
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']"))
        manifest["settings"].append("not-an-object")
        self.assertTrue(V.structural_errors(manifest))
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']"))
        manifest["schema_packages"] = ["x"]
        self.assertTrue(V.structural_errors(manifest))
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']", releases=[]))
        self.assertTrue(V.structural_errors(manifest))
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']"))
        manifest["settings"][0]["releases"][plat.SUPPORTED_RELEASES[0]] = "yes"
        self.assertTrue(V.structural_errors(manifest))
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']"))
        del manifest["settings"][0]["value"]
        self.assertTrue(V.structural_errors(manifest))
        manifest = _manifest(_entry("window.a", "['<Alt>Tab']"))
        manifest["unverified"] = [1]
        self.assertTrue(V.structural_errors(manifest))

    def test_share_must_name_existing_other_entry_with_common_accel(self) -> None:
        cases = {
            "unknown": _manifest(_entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.zz"])),
            "self": _manifest(_entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.a"])),
            "not-list": _manifest(_entry("window.a", "['<Alt>Tab']", shares_accelerator_with="window.b"),
                                  _entry("window.b", "['<Alt>Tab']")),
            "no-common": _manifest(
                _entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.b"]),
                _entry("window.b", "['<Super>Tab']", shares_accelerator_with=["window.a"]),
            ),
        }
        for name, manifest in cases.items():
            with self.subTest(name):
                self.assertTrue(V.structural_errors(manifest))
        ok = _manifest(
            _entry("window.a", "['<Alt>Tab']", shares_accelerator_with=["window.b"]),
            _entry("window.b", "['<Alt>Tab']", shares_accelerator_with=["window.a"]),
        )
        self.assertEqual(V.structural_errors(ok), [])

    def test_empty_list_needs_explicit_unbinding(self) -> None:
        plain = _manifest(_entry("window.a", "@as []"))
        self.assertTrue(V.structural_errors(plain))
        wrong_group = _manifest(_entry("window.a", "@as []", unbinds=True, note="frees a key"))
        self.assertTrue(V.structural_errors(wrong_group))
        good = _manifest(_entry("tiling.a", "@as []", unbinds=True, note="frees a key", group="window-tiling"))
        self.assertEqual(V.structural_errors(good), [])
        nonempty = _manifest(_entry("tiling.a", "['<Super>x']", unbinds=True, note="x", group="window-tiling"))
        self.assertTrue(V.structural_errors(nonempty))

    def test_enum_entries_bind_nothing_and_need_setting_scope(self) -> None:
        entry = _entry("dock.click-action", "'cycle-windows'", gtype="enum", scope="setting",
                       schema="org.gnome.shell.extensions.dash-to-dock", group="dock-behavior")
        self.assertEqual(V.structural_errors(_manifest(entry)), [])
        self.assertEqual(V.accelerators_of(entry), [])
        bad = dict(entry, scope="global")
        self.assertTrue(V.structural_errors(_manifest(bad)))


class StructuralNegativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()

    def mutate(self, ident: str) -> tuple[dict, dict]:
        manifest = copy.deepcopy(self.manifest)
        return manifest, by_id(manifest)[ident]

    def test_duplicate_id(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["settings"].append(copy.deepcopy(manifest["settings"][0]))
        errors = V.structural_errors(manifest)
        self.assertTrue(any("duplicate id" in e for e in errors), errors)

    def test_false_equals_default(self) -> None:
        manifest, entry = self.mutate("terminal.new-tab")
        entry["releases"]["24.04"]["equals_default"] = True
        self.assertTrue(V.structural_errors(manifest))

    def test_out_of_scope_schema(self) -> None:
        manifest, entry = self.mutate("window.close")
        entry["schema"] = "org.gnome.desktop.interface"
        errors = V.structural_errors(manifest)
        self.assertTrue(any("out of scope" in e for e in errors), errors)

    def test_unavailable_record_must_not_claim_default(self) -> None:
        manifest, entry = self.mutate("application.open-new-window-application-1")
        entry["releases"]["22.04"]["default"] = "['<Super><Control>1']"
        self.assertTrue(V.structural_errors(manifest))

    def test_missing_release_record(self) -> None:
        manifest, entry = self.mutate("window.close")
        del entry["releases"]["22.04"]
        self.assertTrue(V.structural_errors(manifest))

    def test_malformed_value(self) -> None:
        manifest, entry = self.mutate("window.close")
        entry["value"] = "['<Alt>F4'"
        self.assertTrue(V.structural_errors(manifest))

    def test_bad_relocatable_path(self) -> None:
        manifest, entry = self.mutate("terminal.new-tab")
        entry["path"] = "org/gnome/terminal/legacy/keybindings"
        self.assertTrue(V.structural_errors(manifest))


FAKE_GSETTINGS = """\
#!{python}
import json, sys
db = json.load(open({db!r}))
args = sys.argv[1:]
schemadir = None
if args[:1] == ["--schemadir"]:
    schemadir, args = args[1], args[2:]
cmd = args[0]
if cmd == "list-schemas":
    names = db["fixed"] if schemadir else db.get("host_leak", [])
    print("\\n".join(names))
    sys.exit(0)
if cmd == "list-relocatable-schemas":
    print("\\n".join(db["relocatable"]))
    sys.exit(0)
if cmd == "list-recursively":
    for line in db.get("recursive", dict()).get(args[1], []):
        print(line)
    sys.exit(0)
target, key = args[1], args[2]
record = db["keys"].get(target + " " + key)
if record is None:
    sys.stderr.write("No such key\\n")
    sys.exit(1)
if cmd == "range":
    print(record.get("range") or "type " + record["type"])
elif cmd == "get":
    print(record["default"])
elif cmd == "writable":
    print("true")
elif cmd == "set":
    sys.exit(0 if args[3] not in db.get("reject", []) else 1)
sys.exit(0)
"""


class ValidatorProbeTests(unittest.TestCase):
    """Drive validate_release through a fake gsettings, no GLib needed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "schemas").mkdir()
        self.manifest = _manifest(
            _entry("window.switch-windows", "['<Alt>Tab']"),
            dict(
                _entry("terminal.new-tab", "'<Control>t'", scope="app:gnome-terminal", gtype="s"),
                schema="org.gnome.Terminal.Legacy.Keybindings",
                path=TERMINAL_KEYBINDINGS_PATH,
            ),
            dict(
                _entry("window.only-new", "['<Super>n']"),
                note="missing on the older release",
                releases={
                    plat.SUPPORTED_RELEASES[0]: {"available": False},
                    plat.SUPPORTED_RELEASES[-1]: {
                        "available": True,
                        "default": "['<Super>n']",
                        "equals_default": True,
                    },
                },
            ),
        )
        self.assertEqual(V.structural_errors(self.manifest), [])
        self.db = {
            "fixed": ["org.gnome.desktop.wm.keybindings"],
            "relocatable": ["org.gnome.Terminal.Legacy.Keybindings"],
            "keys": {
                "org.gnome.desktop.wm.keybindings switch-windows": {
                    "type": "as",
                    "default": "['<Alt>Tab']",
                },
                f"org.gnome.Terminal.Legacy.Keybindings:{TERMINAL_KEYBINDINGS_PATH} new-tab": {
                    "type": "s",
                    "default": "'<Control>t'",
                },
            },
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_validator(self, release: str) -> list:
        db_path = self.root / "db.json"
        db_path.write_text(json.dumps(self.db), encoding="utf-8")
        fake = self.root / "gsettings"
        fake.write_text(FAKE_GSETTINGS.format(python=sys.executable, db=str(db_path)), encoding="utf-8")
        fake.chmod(0o755)
        env = V.isolated_env(self.root / "sandbox", "ubuntu:GNOME")
        gs = V.GSettings(self.root / "schemas", env, binary=str(fake), timeout=20)
        return V.validate_release(self.manifest, release, gs)

    def statuses(self, results) -> dict[str, str]:
        return {r.id: r.status for r in results}

    def test_all_pass_on_older_release(self) -> None:
        results = self.run_validator(plat.SUPPORTED_RELEASES[0])
        self.assertTrue(all(r.status == "PASS" for r in results), [r.to_dict() for r in results])
        checks = {r.id: r.check for r in results}
        self.assertEqual(checks["window.only-new"], "absent")

    def test_missing_key_on_release_that_declares_it(self) -> None:
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[-1]))
        self.assertEqual(results["window.only-new"], "FAIL")
        self.assertEqual(results["window.switch-windows"], "PASS")

    def test_declared_absent_but_present_fails(self) -> None:
        self.db["keys"]["org.gnome.desktop.wm.keybindings only-new"] = {
            "type": "as",
            "default": "['<Super>n']",
        }
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["window.only-new"], "FAIL")

    def test_stale_default_fails(self) -> None:
        self.db["keys"]["org.gnome.desktop.wm.keybindings switch-windows"]["default"] = "@as []"
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["window.switch-windows"], "FAIL")

    def test_type_mismatch_fails(self) -> None:
        self.db["keys"]["org.gnome.desktop.wm.keybindings switch-windows"]["type"] = "s"
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["window.switch-windows"], "FAIL")

    def test_relocatable_schema_must_be_relocatable(self) -> None:
        self.db["relocatable"] = []
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["terminal.new-tab"], "FAIL")

    def test_rejected_value_fails(self) -> None:
        self.db["reject"] = ["['<Alt>Tab']"]
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["window.switch-windows"], "FAIL")

    def test_enum_value_checked_against_nicks(self) -> None:
        entry = _entry("dock.click-action", "'cycle-windows'", gtype="enum", scope="setting",
                       schema="org.gnome.shell.extensions.dash-to-dock", group="dock-behavior")
        self.manifest["settings"].append(entry)
        self.db["fixed"].append("org.gnome.shell.extensions.dash-to-dock")
        self.db["keys"]["org.gnome.shell.extensions.dash-to-dock click-action"] = {
            "type": "s", "range": "enum\n'skip'\n'cycle-windows'", "default": "'cycle-windows'"}
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["dock.click-action"], "PASS")
        self.db["keys"]["org.gnome.shell.extensions.dash-to-dock click-action"]["range"] = "enum\n'skip'"
        results = self.statuses(self.run_validator(plat.SUPPORTED_RELEASES[0]))
        self.assertEqual(results["dock.click-action"], "FAIL")

    def test_default_collisions_are_report_only(self) -> None:
        self.db["recursive"] = {
            "org.gnome.desktop.wm.keybindings": [
                "org.gnome.desktop.wm.keybindings switch-windows ['<Alt>Tab']",
                "org.gnome.desktop.wm.keybindings panic ['<Alt>Tab', '<Super>z']",
                "org.gnome.desktop.wm.keybindings bare ['Tab']",
                "org.gnome.desktop.wm.keybindings flag true",
            ]
        }
        results = self.run_validator(plat.SUPPORTED_RELEASES[0])
        self.assertTrue(all(r.status == "PASS" for r in results))
        db_path = self.root / "db.json"
        fake = self.root / "gsettings"
        env = V.isolated_env(self.root / "sandbox", "ubuntu:GNOME")
        gs = V.GSettings(self.root / "schemas", env, binary=str(fake), timeout=20)
        notes = V.default_collisions(self.manifest, plat.SUPPORTED_RELEASES[0], gs)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("panic", notes[0])
        self.assertIn("window.switch-windows", notes[0])
        self.assertTrue(db_path.exists())

    def test_host_schema_leak_stops_validation(self) -> None:
        self.db["host_leak"] = ["org.gnome.desktop.wm.keybindings"]
        results = self.run_validator(plat.SUPPORTED_RELEASES[0])
        self.assertEqual([(r.check, r.status) for r in results], [("isolation", "FAIL")])

    def test_isolated_env_cannot_reach_a_session(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
            "GSETTINGS_SCHEMA_DIR": "/nonexistent",
            "GSETTINGS_BACKEND": "dconf",
            "XDG_CURRENT_DESKTOP": "Other",
            "DISPLAY": ":0",
        }
        env = V.isolated_env(self.root / "sandbox", "ubuntu:GNOME", base)
        self.assertEqual(env["GSETTINGS_BACKEND"], "memory")
        self.assertEqual(env["XDG_CURRENT_DESKTOP"], "ubuntu:GNOME")
        for name in ("DBUS_SESSION_BUS_ADDRESS", "GSETTINGS_SCHEMA_DIR", "DISPLAY", "WAYLAND_DISPLAY"):
            self.assertNotIn(name, env)
        sandbox = str(self.root / "sandbox")
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_RUNTIME_DIR"):
            self.assertTrue(env[name].startswith(sandbox), name)


class RealSchemaTests(unittest.TestCase):
    """Opt-in: validate against compiled release schemas when provided."""

    def test_release_schemadirs(self) -> None:
        ran = False
        for release in plat.SUPPORTED_RELEASES:
            variable = "GNOME_SETTINGS_SCHEMADIR_" + release.replace(".", "_")
            schemadir = os.environ.get(variable, "").strip()
            if not schemadir:
                continue
            ran = True
            with self.subTest(release=release):
                code = V.main(["--release", release, "--schemadir", schemadir, "--json"])
                self.assertEqual(code, 0)
        if not ran:
            self.skipTest("no GNOME_SETTINGS_SCHEMADIR_<release> set")


if __name__ == "__main__":
    unittest.main()
