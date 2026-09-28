"""Unit tests for desktop/input-remapper/adapters.py against 1.4 and 2.0.1 fixtures.

The fixtures under tests/fixtures/input-remapper were written by the packages'
own classes (python3-inputremapper 1.4.0-1 and 2.0.1-1), and every
``expected-owned.json`` was loaded back through that package's own loader.
These tests never start input-remapper-control or touch input devices.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_PATH = REPO_ROOT / "desktop" / "input-remapper" / "adapters.py"
INTENT_PATH = ADAPTERS_PATH.with_name("intent.json")
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "input-remapper"

_spec = importlib.util.spec_from_file_location("input_remapper_adapters", ADAPTERS_PATH)
ad = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ad
_spec.loader.exec_module(ad)

TARGET = "fixture-keyboard"
OTHER = "other-keyboard"
CASES = ("one-conflict", "both-conflict", "no-selection")
FAMILIES = {"1.4": "1.4.0", "2.0": "2.0.1"}
MANAGED_COMBOS = {(29, 125, 105), (29, 126, 105), (29, 125, 106), (29, 126, 106)}

_no_subprocess = mock.patch.object(
    ad.subprocess, "run", side_effect=AssertionError("tests must not run subprocesses")
)


def setUpModule():
    _no_subprocess.start()


def tearDownModule():
    _no_subprocess.stop()


INTENT = ad.load_intent(INTENT_PATH)


def fixture_root(family: str, case: str) -> Path:
    return FIXTURES / f"v{family}" / case / ad.CONFIG_DIRNAMES[family]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def config_doc(family: str, case: str) -> dict:
    return load(fixture_root(family, case) / "config.json")


def preset_doc(family: str, case: str, group: str, name: str):
    path = fixture_root(family, case) / "presets" / group / f"{name}.json"
    return load(path) if path.exists() else None


def prior_doc(family: str, case: str):
    selection = ad.autoload_selection(config_doc(family, case), TARGET)
    return preset_doc(family, case, TARGET, selection) if selection else None


def entries(family: str, document) -> list:
    if document is None:
        return []
    if family == "1.4":
        return [[k, v] for k, v in document["mapping"].items()]
    return list(document)


def identity(family: str, entry):
    if family == "1.4":
        return ad.v1_key_identity(entry[0])
    return ad.v2_entry_identity(entry)


def codes(family: str, entry) -> tuple:
    if family == "1.4":
        return tuple(int(chunk.split(",")[1]) for chunk in entry[0].split("+"))
    return tuple(int(c["code"]) for c in entry["input_combination"])


def unrelated(family: str, document) -> list:
    return [e for e in entries(family, document) if identity(family, e) not in INTENT.identities]


def managed(family: str, document) -> list:
    return [e for e in entries(family, document) if identity(family, e) in INTENT.identities]


def expected_output(family: str, combo: tuple):
    page_up = combo[-1] == 105
    if family == "1.4":
        return ["KEY_PAGEUP" if page_up else "KEY_PAGEDOWN", "keyboard"]
    return "hold_keys(KEY_LEFTCTRL,KEY_PAGEUP)" if page_up else "hold_keys(KEY_LEFTCTRL,KEY_PAGEDOWN)"


def output_of(family: str, entry):
    return entry[1] if family == "1.4" else entry["output_symbol"]


def perform(change):
    """Carry out planned FileOps the way the GUI phase does after its backup."""

    for op in change.ops:
        if op.content is None:
            op.path.unlink()
        else:
            op.path.parent.mkdir(parents=True, exist_ok=True)
            op.path.write_bytes(op.content)
            os.chmod(op.path, op.mode)
    return change.plan


def write_doc(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ad.dumps_json(document))


def group(key: str = TARGET) -> "ad.DeviceGroup":
    return ad.DeviceGroup(key=key, name=TARGET, names=(TARGET,), types=("keyboard",),
                          paths=(), physical=True, keyboard_codes=None)


class VersionDetectionTests(unittest.TestCase):
    STATUS = (
        "Package: python3-inputremapper\nStatus: install ok installed\nVersion: 2.0.1-1\n\n"
        "Package: input-remapper-daemon\nStatus: install ok installed\nVersion: 2.0.1-1\n\n"
        "Package: input-remapper-gtk\nStatus: deinstall ok config-files\nVersion: 1.4.0-1\n"
    )

    def test_detects_installed_family(self):
        self.assertEqual(ad.detect_family(self.STATUS), ("2.0", "2.0.1"))

    def test_ignores_removed_packages(self):
        found = ad.parse_dpkg_status(self.STATUS, ["input-remapper-gtk"])
        self.assertEqual(found, {})

    def test_version_to_family(self):
        self.assertEqual(ad.family_for_version("1.4.0-1"), "1.4")
        self.assertEqual(ad.family_for_version("2.0.1-1"), "2.0")
        self.assertEqual(ad.upstream_version("1:2.0.1-1ubuntu2"), "2.0.1")

    def test_unverified_versions_are_refused(self):
        for version in ("2.1.0-1", "1.5.0-1", "2.0.0-1"):
            with self.subTest(version=version), self.assertRaises(ad.AdapterError):
                ad.family_for_version(version)

    def test_missing_or_mismatched_packages(self):
        with self.assertRaises(ad.AdapterError):
            ad.detect_family("Package: python3-inputremapper\nStatus: install ok installed\nVersion: 2.0.1-1\n")
        mixed = self.STATUS.replace("Version: 2.0.1-1\n\nPackage: input-remapper-gtk", "Version: 2.0.1-1\n\nPackage: x")
        mixed = mixed.replace("input-remapper-daemon\nStatus: install ok installed\nVersion: 2.0.1-1",
                              "input-remapper-daemon\nStatus: install ok installed\nVersion: 1.4.0-1")
        with self.assertRaises(ad.AdapterError):
            ad.detect_family(mixed)


class IntentTests(unittest.TestCase):
    def test_two_chords_expand_to_both_super_keys(self):
        self.assertEqual([c.id for c in INTENT.chords], ["tab-previous", "tab-next"])
        self.assertEqual(set(INTENT.combos), MANAGED_COMBOS)
        self.assertEqual(len(INTENT.identities), 4)

    def test_permutation_is_same_identity(self):
        a = ad.combo_identity([(1, 29), (1, 125), (1, 105)])
        b = ad.combo_identity([(1, 125), (1, 29), (1, 105)])
        c = ad.combo_identity([(1, 29), (1, 105), (1, 125)])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_undeclared_key_is_rejected(self):
        document = load(INTENT_PATH)
        document["chords"][0]["press"] = "KEY_NOPE"
        with self.assertRaises(ad.AdapterError):
            ad.parse_intent(document)

    def test_intent_is_semantic_only(self):
        text = INTENT_PATH.read_text(encoding="utf-8")
        for needle in ("origin_hash", "/" + "home" + "/", "/dev/input"):
            self.assertNotIn(needle, text)


class SeedingTests(unittest.TestCase):
    def test_matches_loader_verified_output(self):
        for family in FAMILIES:
            for case in CASES:
                with self.subTest(family=family, case=case):
                    build = ad.build_owned_preset(family, prior_doc(family, case), INTENT)
                    expected = load(FIXTURES / f"v{family}" / case / "expected-owned.json")
                    self.assertEqual(build.document, expected)

    def test_unrelated_entries_preserved_verbatim_and_in_order(self):
        for family in FAMILIES:
            for case in CASES:
                with self.subTest(family=family, case=case):
                    prior = prior_doc(family, case)
                    build = ad.build_owned_preset(family, prior, INTENT)
                    self.assertEqual(unrelated(family, build.document), unrelated(family, prior))

    def test_managed_chords_overwritten(self):
        for family in FAMILIES:
            for case in CASES:
                with self.subTest(family=family, case=case):
                    prior = prior_doc(family, case)
                    build = ad.build_owned_preset(family, prior, INTENT)
                    got = managed(family, build.document)
                    self.assertEqual(len(got), 4)
                    self.assertEqual({codes(family, e) for e in got}, MANAGED_COMBOS)
                    for entry in got:
                        self.assertEqual(output_of(family, entry), expected_output(family, codes(family, entry)))
                    self.assertEqual(build.replaced, managed(family, prior))

    def test_collision_counts(self):
        expected = {"one-conflict": 1, "both-conflict": 3, "no-selection": 0}
        for family in FAMILIES:
            for case, count in expected.items():
                with self.subTest(family=family, case=case):
                    build = ad.build_owned_preset(family, prior_doc(family, case), INTENT)
                    self.assertEqual(len(build.replaced), count)

    def test_permuted_conflict_is_replaced(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                prior = prior_doc(family, "both-conflict")
                permuted = [e for e in entries(family, prior) if codes(family, e) == (125, 29, 106)]
                self.assertEqual(len(permuted), 1)
                build = ad.build_owned_preset(family, prior, INTENT)
                self.assertNotIn(permuted[0], entries(family, build.document))

    def test_rerun_is_idempotent_without_duplicates(self):
        for family in FAMILIES:
            for case in CASES:
                with self.subTest(family=family, case=case):
                    first = ad.build_owned_preset(family, prior_doc(family, case), INTENT)
                    second = ad.build_owned_preset(family, first.document, INTENT)
                    self.assertEqual(second.document, first.document)
                    ids = [identity(family, e) for e in entries(family, second.document)]
                    self.assertEqual(len(ids), len(set(ids)))

    def test_v1_extra_preset_config_preserved(self):
        prior = prior_doc("1.4", "one-conflict")
        build = ad.build_owned_preset("1.4", prior, INTENT)
        self.assertEqual(build.document["macros"], {"keystroke_sleep_ms": 25})

    def test_v2_origin_hash_reused_only_from_replaced_entry(self):
        build = ad.build_owned_preset("2.0", prior_doc("2.0", "both-conflict"), INTENT)
        hashed = {codes("2.0", e): [c.get("origin_hash") for c in e["input_combination"]]
                  for e in managed("2.0", build.document)}
        self.assertEqual(hashed[(29, 126, 105)], ["00000000000000000000000000000001"] * 3)
        for combo in MANAGED_COMBOS - {(29, 126, 105)}:
            self.assertEqual(hashed[combo], [None] * 3)

    def test_v2_managed_entries_release_combination_keys(self):
        build = ad.build_owned_preset("2.0", None, INTENT)
        for entry in build.managed:
            self.assertIs(entry["release_combination_keys"], True)
            self.assertEqual(entry["target_uinput"], "keyboard")

    def test_prior_document_is_not_mutated(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                prior = prior_doc(family, "both-conflict")
                snapshot = copy.deepcopy(prior)
                ad.build_owned_preset(family, prior, INTENT)
                self.assertEqual(prior, snapshot)


class AutoloadTests(unittest.TestCase):
    def test_only_target_group_changes(self):
        for family, version in FAMILIES.items():
            with self.subTest(family=family):
                before = config_doc(family, "one-conflict")
                after = ad.set_autoload(before, TARGET, INTENT.owned_preset_name, version)
                self.assertEqual(after["autoload"][TARGET], INTENT.owned_preset_name)
                expected = copy.deepcopy(before)
                expected["autoload"][TARGET] = INTENT.owned_preset_name
                self.assertEqual(after, expected)
                self.assertEqual(before, config_doc(family, "one-conflict"))

    def test_preexisting_other_device_selection_kept_when_target_absent(self):
        for family, version in FAMILIES.items():
            with self.subTest(family=family):
                before = config_doc(family, "no-selection")
                self.assertIsNone(ad.autoload_selection(before, TARGET))
                after = ad.set_autoload(before, TARGET, INTENT.owned_preset_name, version)
                self.assertEqual(after["autoload"], {OTHER: "gaming", TARGET: INTENT.owned_preset_name})

    def test_new_config_records_version(self):
        self.assertEqual(ad.set_autoload(None, TARGET, "x", "2.0.1"),
                         {"version": "2.0.1", "autoload": {TARGET: "x"}})

    def test_none_removes_only_that_key(self):
        before = config_doc("2.0", "one-conflict")
        after = ad.set_autoload(before, TARGET, None, "2.0.1")
        self.assertEqual(after["autoload"], {OTHER: "gaming"})


def fixture_loader(family: str, case: str, overrides: dict | None = None):
    overrides = overrides or {}

    def load_preset(name: str):
        if name in overrides:
            return copy.deepcopy(overrides[name])
        return preset_doc(family, case, TARGET, name)

    return load_preset


class PlanApplyTests(unittest.TestCase):
    family, version = "2.0", "2.0.1"

    def plan(self, case, config=None, overrides=None, record=None):
        config = config_doc(self.family, case) if config is None else config
        return ad.plan_apply(self.family, self.version, config, fixture_loader(self.family, case, overrides),
                             TARGET, TARGET, INTENT, record)

    def test_v1_intent_is_refused(self):
        with self.assertRaises(ad.UnsupportedIntent):
            ad.plan_apply("1.4", "1.4.0", config_doc("1.4", "one-conflict"),
                          fixture_loader("1.4", "one-conflict"), TARGET, TARGET, INTENT)

    def test_version_family_mismatch_refused(self):
        with self.assertRaises(ad.AdapterError):
            ad.plan_apply("2.0", "1.4.0", {}, lambda n: None, TARGET, TARGET, INTENT)

    def test_first_apply_records_prior(self):
        plan = self.plan("both-conflict")
        self.assertTrue(plan.changed)
        self.assertEqual(plan.seed_selection, "daily")
        self.assertEqual(plan.record["prior_selection"], "daily")
        self.assertTrue(plan.record["prior_selection_known"])
        self.assertEqual(plan.record["prior_managed_entries"], managed("2.0", prior_doc("2.0", "both-conflict")))
        self.assertEqual(plan.config["autoload"], {TARGET: INTENT.owned_preset_name, OTHER: "gaming"})

    def test_rerun_is_noop_and_keeps_record(self):
        first = self.plan("one-conflict")
        second = self.plan("one-conflict", config=first.config,
                           overrides={INTENT.owned_preset_name: first.owned_preset}, record=first.record)
        self.assertFalse(second.changed)
        self.assertEqual(second.owned_preset, first.owned_preset)
        self.assertEqual(second.record["prior_selection"], "daily")
        self.assertEqual(second.record["prior_managed_entries"], first.record["prior_managed_entries"])

    def test_rerun_preserves_unrelated_local_edit_and_resets_managed_conflict(self):
        first = self.plan("one-conflict")
        edited = copy.deepcopy(first.owned_preset)
        local = {"input_combination": [{"type": 1, "code": 88}], "target_uinput": "keyboard",
                 "output_symbol": "KEY_F12", "mapping_type": "key_macro"}
        edited.append(local)
        for entry in edited:
            if codes("2.0", entry) == (29, 125, 105):
                entry["output_symbol"] = "KEY_HOME"
        second = self.plan("one-conflict", config=first.config,
                           overrides={INTENT.owned_preset_name: edited}, record=first.record)
        self.assertIn(local, second.owned_preset)
        self.assertEqual([output_of("2.0", e) for e in managed("2.0", second.owned_preset)
                          if codes("2.0", e) == (29, 125, 105)], [expected_output("2.0", (29, 125, 105))])
        self.assertEqual(len(managed("2.0", second.owned_preset)), 4)
        self.assertEqual(second.record["prior_selection"], "daily")

    def test_selection_moved_elsewhere_becomes_new_prior(self):
        first = self.plan("one-conflict")
        moved = ad.set_autoload(first.config, TARGET, "spare", self.version)
        second = self.plan("one-conflict", config=moved, record=first.record)
        self.assertEqual(second.record["prior_selection"], "spare")
        self.assertEqual(second.record["history"][0]["prior_selection"], "daily")

    def test_lost_lineage_never_invents_prior(self):
        first = self.plan("one-conflict")
        second = self.plan("one-conflict", config=first.config,
                           overrides={INTENT.owned_preset_name: first.owned_preset}, record=None)
        self.assertIsNone(second.record["prior_selection"])
        self.assertFalse(second.record["prior_selection_known"])


def install(family: str, case: str):
    """Owned preset, config and restore record for either family (build level)."""

    config = config_doc(family, case)
    selection = ad.autoload_selection(config, TARGET)
    build = ad.build_owned_preset(family, prior_doc(family, case), INTENT)
    record = ad.new_restore_record(family, TARGET, TARGET, INTENT, selection, True, build)
    new_config = ad.set_autoload(config, TARGET, INTENT.owned_preset_name, FAMILIES[family])
    return new_config, build.document, record


def add_unrelated_edit(family: str, document):
    document = copy.deepcopy(document)
    if family == "1.4":
        document["mapping"]["1,88,1"] = ["KEY_F12", "keyboard"]
        return document, ["1,88,1", ["KEY_F12", "keyboard"]]
    edit = {"input_combination": [{"type": 1, "code": 88}], "target_uinput": "keyboard",
            "output_symbol": "KEY_F12", "mapping_type": "key_macro"}
    return document + [edit], edit


class RestoreTests(unittest.TestCase):
    def test_restore_returns_prior_selection_and_chords_keeping_later_edits(self):
        for family in FAMILIES:
            for case in ("one-conflict", "both-conflict"):
                with self.subTest(family=family, case=case):
                    config, owned, record = install(family, case)
                    owned, edit = add_unrelated_edit(family, owned)
                    plan = ad.plan_restore(family, config, owned, record, prior_doc(family, case))
                    self.assertEqual(plan.config, config_doc(family, case))
                    restored = entries(family, plan.owned_preset)
                    self.assertIn(edit, restored)
                    self.assertEqual(managed(family, plan.owned_preset), managed(family, prior_doc(family, case)))
                    self.assertFalse(plan.owned_redundant)
                    self.assertEqual(plan.notes, ())

    def test_restore_without_edits_is_redundant(self):
        for family in FAMILIES:
            for case in CASES:
                with self.subTest(family=family, case=case):
                    config, owned, record = install(family, case)
                    plan = ad.plan_restore(family, config, owned, record, prior_doc(family, case))
                    self.assertTrue(plan.owned_redundant)
                    self.assertEqual(plan.config, config_doc(family, case))

    def test_restore_leaves_later_user_selection(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                config, owned, record = install(family, "one-conflict")
                config = ad.set_autoload(config, TARGET, "spare", FAMILIES[family])
                plan = ad.plan_restore(family, config, owned, record)
                self.assertEqual(ad.autoload_selection(plan.config, TARGET), "spare")
                self.assertTrue(any("changed after install" in n for n in plan.notes))

    def test_restore_keeps_user_edited_managed_chord(self):
        config, owned, record = install("2.0", "one-conflict")
        for entry in owned:
            if codes("2.0", entry) == (29, 125, 105):
                entry["output_symbol"] = "KEY_END"
        plan = ad.plan_restore("2.0", config, owned, record)
        kept = [e for e in managed("2.0", plan.owned_preset) if codes("2.0", e) == (29, 125, 105)]
        self.assertEqual([e["output_symbol"] for e in kept], ["KEY_END"])
        self.assertTrue(any("edited after install" in n for n in plan.notes))

    def test_restore_tolerates_package_resave_dropping_defaults(self):
        # 2.0.1 Preset.save() drops release_combination_keys (a default) and
        # the GUI may add origin hashes; installed chords must still match.
        config, owned, record = install("2.0", "no-selection")
        resaved = copy.deepcopy(owned)
        for entry in resaved:
            entry.pop("release_combination_keys", None)
            for c in entry["input_combination"]:
                c["origin_hash"] = "00000000000000000000000000000002"
        plan = ad.plan_restore("2.0", config, resaved, record)
        self.assertEqual(managed("2.0", plan.owned_preset), [])
        self.assertTrue(plan.owned_redundant)

    def test_unknown_prior_keeps_owned_selection(self):
        config, owned, record = install("2.0", "one-conflict")
        record["prior_selection"], record["prior_selection_known"] = None, False
        plan = ad.plan_restore("2.0", config, owned, record)
        self.assertEqual(ad.autoload_selection(plan.config, TARGET), INTENT.owned_preset_name)


class DiskRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, family: str, case: str) -> dict:
        dst = ad.config_dir(family, self.home)
        shutil.copytree(fixture_root(family, case), dst)
        return {p: p.read_bytes() for p in dst.rglob("*.json") if p.name != "config.json"}

    def test_apply_and_restore_on_disk_v2(self):
        originals = self.stage("2.0", "both-conflict")
        change = ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)
        owned_path = ad.preset_path("2.0", self.home, TARGET, INTENT.owned_preset_name)
        self.assertFalse(owned_path.exists(), "apply_group must only plan")
        plan = perform(change)
        self.assertEqual(ad.read_json(owned_path), load(FIXTURES / "v2.0/both-conflict/expected-owned.json"))
        rerun = ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT, plan.record)
        self.assertFalse(rerun.plan.changed)
        self.assertEqual(rerun.ops, ())
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data, path)
        perform(ad.restore_group("2.0", self.home, plan.record))
        self.assertEqual(ad.read_json(ad.config_dir("2.0", self.home) / "config.json"),
                         config_doc("2.0", "both-conflict"))
        self.assertFalse(owned_path.exists())
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data, path)

    def test_restore_keeps_owned_file_with_later_edit_v1(self):
        self.stage("1.4", "one-conflict")
        config, owned, record = install("1.4", "one-conflict")
        owned, edit = add_unrelated_edit("1.4", owned)
        root = ad.config_dir("1.4", self.home)
        owned_path = ad.preset_path("1.4", self.home, TARGET, INTENT.owned_preset_name)
        write_doc(owned_path, owned)
        write_doc(root / "config.json", config)
        perform(ad.restore_group("1.4", self.home, record))
        self.assertEqual(ad.read_json(root / "config.json"), config_doc("1.4", "one-conflict"))
        restored = ad.read_json(owned_path)
        self.assertEqual(restored["mapping"][edit[0]], edit[1])

    def test_v1_apply_refused_on_disk(self):
        originals = self.stage("1.4", "one-conflict")
        with self.assertRaises(ad.UnsupportedIntent):
            ad.apply_group("1.4", "1.4.0", self.home, group(), INTENT)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_v2_waits_for_its_own_migration(self):
        self.stage("1.4", "one-conflict")
        self.assertTrue(ad.v2_migration_pending(self.home))
        with self.assertRaises(ad.AdapterError):
            ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)
        self.assertFalse(ad.config_dir("2.0", self.home).exists())

    def test_paths(self):
        self.assertEqual(ad.config_dir("1.4", self.home), self.home / ".config/input-remapper")
        self.assertEqual(ad.config_dir("2.0", self.home), self.home / ".config/input-remapper-2")
        self.assertEqual(ad.preset_dirname("2.0", 'a/b:c'), "a_b_c")
        with self.assertRaises(ad.AdapterError):
            ad.preset_dirname("1.4", "a/b")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        text = (FIXTURES / "proc-bus-input-devices.txt").read_text(encoding="utf-8")
        self.groups = ad.group_devices(ad.parse_proc_input_devices(text))

    def test_grouping_mirrors_input_remapper(self):
        by_key = {g.key: g for g in self.groups}
        self.assertEqual(set(by_key), {
            "fixture-laptop-keyboard", "fixture-keyboard", "fixture-keyboard 2", "fixture-mouse",
            "fixture-macropad", "input-remapper keyboard", "input-remapper fixture-keyboard forwarded",
            "fixture-virtual-keyboard",
        })
        self.assertEqual(by_key["fixture-keyboard"].names,
                         ("fixture-keyboard", "fixture-keyboard Consumer Control"))
        self.assertEqual(by_key["fixture-keyboard"].types, ("keyboard",))
        self.assertEqual(by_key["fixture-mouse"].types, ("mouse",))
        self.assertTrue(by_key["fixture-keyboard 2"].key_ambiguous)
        self.assertFalse(by_key["fixture-laptop-keyboard"].key_ambiguous)
        self.assertNotIn("Power Button", by_key)

    def test_eligible_physical_keyboards_only(self):
        eligible, rejected = ad.eligible_keyboards(self.groups, INTENT)
        self.assertEqual({g.key for g in eligible},
                         {"fixture-laptop-keyboard", "fixture-keyboard", "fixture-keyboard 2"})
        reasons = dict(rejected)
        self.assertEqual(reasons["fixture-mouse"], "not a keyboard")
        self.assertEqual(reasons["fixture-macropad"], "keyboard lacks the chord keys")
        self.assertEqual(reasons["input-remapper keyboard"], "input-remapper virtual device")
        self.assertEqual(reasons["input-remapper fixture-keyboard forwarded"], "input-remapper virtual device")
        self.assertEqual(reasons["fixture-virtual-keyboard"], "virtual (non-physical) device")

    def test_duplicates_removed(self):
        eligible, rejected = ad.eligible_keyboards(self.groups + self.groups[:3], INTENT)
        self.assertEqual(len({g.key for g in eligible}), len(eligible))
        self.assertTrue(any(reason == "duplicate group" for _, reason in rejected))

    def test_remapper_group_dumps(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                text = (FIXTURES / f"v{family}" / "groups-dump.json").read_text(encoding="utf-8")
                groups = ad.groups_from_remapper_dump(text)
                eligible, rejected = ad.eligible_keyboards(groups, INTENT)
                self.assertEqual(eligible, [], "unconfirmed dump groups must not be eligible")
                self.assertEqual(dict(rejected)["fixture-keyboard"],
                                 "not confirmed by /proc/bus/input/devices")
                groups = ad.cross_check_dump_groups(groups, self.groups)
                eligible, rejected = ad.eligible_keyboards(groups, INTENT)
                self.assertEqual([g.key for g in eligible], ["fixture-keyboard", "fixture-keyboard 2"])
                self.assertEqual({g.name for g in eligible}, {"fixture-keyboard"})
                self.assertEqual(dict(rejected)["fixture-mouse"], "not a keyboard")

    def test_dump_group_without_proc_match_is_ineligible(self):
        dump = [ad.DeviceGroup(key="ghost", name="ghost", names=("ghost",), types=("keyboard",),
                               paths=(), physical=None, keyboard_codes=None)]
        checked = ad.cross_check_dump_groups(dump, self.groups)
        eligible, rejected = ad.eligible_keyboards(checked, INTENT)
        self.assertEqual(eligible, [])
        self.assertEqual(rejected, [("ghost", "not confirmed by /proc/bus/input/devices")])
        renamed = [dataclass_replace(g, names=g.names + ("extra",)) for g in self.groups
                   if g.key == "fixture-keyboard"]
        checked = ad.cross_check_dump_groups(renamed, self.groups)
        self.assertIsNone(checked[0].physical)

    def test_uhid_keyboard_is_physical_uinput_is_virtual(self):
        text = UHID_AND_UINPUT
        groups = {g.key: g for g in ad.group_devices(ad.parse_proc_input_devices(text))}
        self.assertTrue(groups["fixture-ble-keyboard"].physical)
        self.assertFalse(groups["fixture-uinput-keyboard"].physical)
        eligible, rejected = ad.eligible_keyboards(groups.values(), INTENT)
        self.assertEqual([g.key for g in eligible], ["fixture-ble-keyboard"])
        self.assertEqual(dict(rejected)["fixture-uinput-keyboard"], "virtual (non-physical) device")

    def test_bitmap_words_are_64_bit(self):
        self.assertEqual(ad._bitmap("1 0", 64), frozenset({64}))
        self.assertEqual(ad._bitmap("10000000000000 0", 64), frozenset({116}))


def dataclass_replace(obj, **changes):
    import dataclasses
    return dataclasses.replace(obj, **changes)


_KEYBOARD_BITS = "B: EV=120013\nB: KEY=80000000000000 ffffffffffffffff fffffffffffffffe\n"
UHID_AND_UINPUT = (
    "I: Bus=0005 Vendor=0a0a Product=0b0b Version=0111\n"
    "N: Name=\"fixture-ble-keyboard\"\n"
    "P: Phys=00:00:00:00:00:01\n"
    "S: Sysfs=/devices/virtual/misc/uhid/0005:0A0A:0B0B.0001/input/input30\n"
    "H: Handlers=sysrq kbd leds event30\n" + _KEYBOARD_BITS + "\n"
    "I: Bus=0003 Vendor=0c0c Product=0d0d Version=0111\n"
    "N: Name=\"fixture-uinput-keyboard\"\n"
    "P: Phys=py-evdev-uinput\n"
    "S: Sysfs=/devices/virtual/input/input31\n"
    "H: Handlers=sysrq kbd event31\n" + _KEYBOARD_BITS
)


class OwnedPresetPerKeyTests(unittest.TestCase):
    """Identical keyboards share a group name but must not share one owned preset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names(self):
        self.assertEqual(ad.owned_preset_for(INTENT, TARGET, TARGET), INTENT.owned_preset_name)
        self.assertEqual(ad.owned_preset_for(INTENT, TARGET + " 2", TARGET),
                         INTENT.owned_preset_name + "-2")
        odd = ad.owned_preset_for(INTENT, "other key", TARGET)
        self.assertRegex(odd, "^" + INTENT.owned_preset_name + "-[0-9a-f]{8}$")

    def test_identical_keyboards_get_separate_files_and_seeds(self):
        root = ad.config_dir("2.0", self.home)
        shutil.copytree(fixture_root("2.0", "one-conflict"), root)
        config = ad.read_json(root / "config.json")
        config = ad.set_autoload(config, TARGET + " 2", "spare", "2.0.1")
        spare = [{"input_combination": [{"type": 1, "code": 87}], "target_uinput": "keyboard",
                  "output_symbol": "KEY_F11", "mapping_type": "key_macro"}]
        write_doc(root / "config.json", config)
        write_doc(ad.preset_path("2.0", self.home, TARGET, "spare"), spare)
        first = perform(ad.apply_group("2.0", "2.0.1", self.home, group(TARGET), INTENT))
        second = perform(ad.apply_group("2.0", "2.0.1", self.home, group(TARGET + " 2"), INTENT))
        self.assertNotEqual(first.record["owned_preset"], second.record["owned_preset"])
        final = ad.read_json(root / "config.json")["autoload"]
        self.assertEqual(final[TARGET], INTENT.owned_preset_name)
        self.assertEqual(final[TARGET + " 2"], INTENT.owned_preset_name + "-2")
        second_doc = ad.read_json(ad.preset_path("2.0", self.home, TARGET, final[TARGET + " 2"]))
        self.assertIn(spare[0], second_doc)
        first_doc = ad.read_json(ad.preset_path("2.0", self.home, TARGET, final[TARGET]))
        self.assertNotIn(spare[0], first_doc)
        self.assertEqual(second.record["prior_selection"], "spare")
        self.assertEqual(first.record["prior_selection"], "daily")


class FileOpSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, case="one-conflict"):
        root = ad.config_dir("2.0", self.home)
        shutil.copytree(fixture_root("2.0", case), root)
        return root

    def test_new_files_default_0644_existing_modes_preserved(self):
        root = self.stage()
        os.chmod(root / "config.json", 0o600)
        change = ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)
        modes = {op.path.name: op.mode for op in change.ops}
        self.assertEqual(modes["config.json"], 0o600)
        self.assertEqual(modes[INTENT.owned_preset_name + ".json"], 0o644)
        for op in change.ops:
            self.assertIsInstance(op.content, bytes)
            json.loads(op.content)

    def test_symlinked_target_is_refused(self):
        root = self.stage()
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        owned = ad.preset_path("2.0", self.home, TARGET, INTENT.owned_preset_name)
        owned.symlink_to(outside)
        with self.assertRaises(ad.AdapterError):
            ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)
        self.assertEqual(outside.read_text(encoding="utf-8"), "[]")
        self.assertTrue((root / "config.json").exists())

    def test_symlinked_parent_is_refused(self):
        self.stage()
        presets = ad.config_dir("2.0", self.home) / "presets"
        elsewhere = Path(self.tmp.name) / "elsewhere"
        shutil.move(str(presets / TARGET), str(elsewhere))
        (presets / TARGET).symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ad.AdapterError):
            ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)

    def test_symlinked_config_dir_is_refused(self):
        real = Path(self.tmp.name) / "real-config"
        shutil.copytree(fixture_root("2.0", "one-conflict"), real)
        (self.home / ".config").mkdir()
        ad.config_dir("2.0", self.home).symlink_to(real, target_is_directory=True)
        with self.assertRaises(ad.AdapterError):
            ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT)

    def test_restore_plans_delete_without_deleting(self):
        self.stage("both-conflict")
        plan = perform(ad.apply_group("2.0", "2.0.1", self.home, group(), INTENT))
        change = ad.restore_group("2.0", self.home, plan.record)
        deletes = [op for op in change.ops if op.content is None]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].path.exists())
        perform(change)
        self.assertFalse(deletes[0].path.exists())


class MalformedDataTests(unittest.TestCase):
    def test_malformed_values_raise_adapter_error(self):
        bad_presets = [
            [{"input_combination": [{"type": "x", "code": 29}]}],
            [{"input_combination": [{"type": 1, "code": None}]}],
            "not a list",
        ]
        for prior in bad_presets:
            with self.subTest(prior=prior), self.assertRaises(ad.AdapterError):
                ad.plan_apply("2.0", "2.0.1", {"version": "2.0.1", "autoload": {TARGET: "p"}},
                              lambda name: prior, TARGET, TARGET, INTENT)
        for config in ([], {"autoload": []}, {"autoload": {TARGET: 5}, "version": 1}):
            with self.subTest(config=config):
                try:
                    ad.plan_apply("2.0", "2.0.1", config, lambda name: None, TARGET, TARGET, INTENT)
                except ad.AdapterError:
                    pass
        with self.assertRaises(ad.AdapterError):
            ad.build_owned_preset("1.4", {"mapping": {"1,x,1": ["a", "keyboard"]}}, INTENT)
        with self.assertRaises(ad.AdapterError):
            ad.plan_restore("2.0", {"autoload": {}}, [{"input_combination": [{"type": [], "code": 1}]}],
                            {"group_key": TARGET, "owned_preset": "p",
                             "installed_managed_entries": [], "prior_managed_entries": []})
        with self.assertRaises(ad.AdapterError):
            ad.plan_restore("2.0", {}, [], {"owned_preset": "p"})

    def test_malformed_restore_record(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ad.AdapterError):
            ad.restore_group("2.0", Path(tmp), {"group_key": 3})


class LostLineageRestoreTests(unittest.TestCase):
    def test_restore_after_lost_lineage_keeps_selection_and_unrelated_entries(self):
        first = ad.plan_apply("2.0", "2.0.1", config_doc("2.0", "one-conflict"),
                              fixture_loader("2.0", "one-conflict"), TARGET, TARGET, INTENT)
        owned, edit = add_unrelated_edit("2.0", first.owned_preset)
        lost = ad.plan_apply("2.0", "2.0.1", first.config,
                             fixture_loader("2.0", "one-conflict", {INTENT.owned_preset_name: owned}),
                             TARGET, TARGET, INTENT, None)
        self.assertFalse(lost.record["prior_selection_known"])
        self.assertEqual(lost.record["prior_managed_entries"], [])
        plan = ad.plan_restore("2.0", lost.config, lost.owned_preset, lost.record)
        self.assertEqual(ad.autoload_selection(plan.config, TARGET), INTENT.owned_preset_name)
        self.assertEqual(ad.autoload_selection(plan.config, OTHER), "gaming")
        self.assertTrue(any("prior selection unknown" in n for n in plan.notes))
        self.assertEqual(managed("2.0", plan.owned_preset), [])
        self.assertIn(edit, plan.owned_preset)
        self.assertFalse(plan.owned_redundant)
        for entry in unrelated("2.0", prior_doc("2.0", "one-conflict")):
            self.assertIn(entry, plan.owned_preset)


class ControlTests(unittest.TestCase):
    def test_argv(self):
        self.assertEqual(ad.control_argv("autoload", TARGET),
                         ["input-remapper-control", "--command", "autoload", "--device", TARGET])
        self.assertEqual(ad.control_argv("start", TARGET, "p"),
                         ["input-remapper-control", "--command", "start", "--device", TARGET, "--preset", "p"])
        self.assertEqual(ad.control_argv("hello"), ["input-remapper-control", "--command", "hello"])
        self.assertEqual(ad.list_devices_argv(), ["input-remapper-control", "--list-devices"])

    def test_argv_validation(self):
        for args in (("start", TARGET), ("stop",), ("rm -rf",)):
            with self.subTest(args=args), self.assertRaises(ad.AdapterError):
                ad.control_argv(*args)


if __name__ == "__main__":
    unittest.main()
