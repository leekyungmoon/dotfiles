"""Phase 2: run the repository adapters on the fixtures inside a temp HOME, then
load the results back through the package's OWN loaders, and cross-check the
adapter's device grouping against the package's own _FindGroups.

Usage (through run.sh): verify_adapters.py FAMILY EXTRACT_ROOT FIXTURE_DIR ADAPTERS_PY PROC_TXT OUT_DIR
"""
import importlib.util
import json
import multiprocessing
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from irboot import boot  # noqa: E402

fam, root, fixtures, adapters_py, proc_txt, out = sys.argv[1:7]
boot(root)
HOME = Path(os.environ["HOME"])

spec = importlib.util.spec_from_file_location("ir_adapters", adapters_py)
A = importlib.util.module_from_spec(spec)
sys.modules["ir_adapters"] = A
spec.loader.exec_module(A)
intent = A.load_intent(Path(adapters_py).with_name("intent.json"))

report = {"family": fam, "cases": {}, "checks": []}


def check(name, ok, detail=""):
    report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})


def perform(change):
    """The adapters only plan FileOps; carry them out like the caller would."""
    for op in change.ops:
        if op.content is None:
            op.path.unlink()
        else:
            op.path.parent.mkdir(parents=True, exist_ok=True)
            op.path.write_bytes(op.content)
            os.chmod(op.path, op.mode)
    return change.plan


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(A.dumps_json(document))


if fam == "1.4":
    import inputremapper.paths as paths
    from inputremapper.config import GlobalConfig
    from inputremapper.mapping import Mapping
    from inputremapper.injection.macros.parse import parse, is_this_a_macro
    import inputremapper.groups as rgroups
    from inputremapper.logger import VERSION

    def load_owned(path):
        m = Mapping()
        m.load(str(path))
        return [{"key": [list(x) for x in key.keys], "output": list(v)} for key, v in m], m._config
else:
    import inputremapper.configs.paths as paths
    from inputremapper.configs.global_config import GlobalConfig
    from inputremapper.configs.preset import Preset
    import inputremapper.groups as rgroups
    from inputremapper.logger import VERSION

    def load_owned(path):
        p = Preset(str(path))
        p.load()
        return [{
            "input": [[ic.type, ic.code, ic.origin_hash] for ic in m.input_combination],
            "output_symbol": m.output_symbol, "target_uinput": m.target_uinput,
            "release_combination_keys": m.release_combination_keys, "name": m.name,
        } for m in p], None

assert paths.CONFIG_PATH.startswith(str(HOME))
conf_dirname = A.CONFIG_DIRNAMES[fam]
group = A.DeviceGroup(key="fixture-keyboard", name="fixture-keyboard", names=("fixture-keyboard",),
                      types=("keyboard",), paths=(), physical=True, keyboard_codes=None)
managed_inputs = sorted(sorted(c) for c in intent.combos)

for case in sorted(os.listdir(os.path.join(fixtures))):
    src = os.path.join(fixtures, case, conf_dirname)
    if not os.path.isdir(src):
        continue
    dst = HOME / ".config" / conf_dirname
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    before_other = (dst / "presets/other-keyboard/gaming.json").read_bytes()
    prior_files = {p: p.read_bytes() for p in dst.rglob("*.json") if p.name != "config.json"}

    if fam == "2.0":
        plan = perform(A.apply_group(fam, VERSION, HOME, group, intent))
        rerun = perform(A.apply_group(fam, VERSION, HOME, group, intent, plan.record))
        check(f"{case}: rerun unchanged", not rerun.changed)
    else:
        try:
            A.apply_group(fam, VERSION, HOME, group, intent)
            check(f"{case}: 1.4 apply refused", False)
        except A.UnsupportedIntent:
            check(f"{case}: 1.4 apply refused (UnsupportedIntent)", True)
        cfg = A.read_json(dst / "config.json")
        seed_name = A.autoload_selection(cfg, group.key)
        seed = A.read_json(A.preset_path(fam, HOME, group.name, seed_name)) if seed_name else None
        build = A.build_owned_preset(fam, seed, intent)
        write_json(A.preset_path(fam, HOME, group.name, intent.owned_preset_name), build.document)
        write_json(dst / "config.json", A.set_autoload(cfg, group.key, intent.owned_preset_name, VERSION))

    owned_path = A.preset_path(fam, HOME, group.name, intent.owned_preset_name)
    entries, extra = load_owned(owned_path)
    c = GlobalConfig()
    c.load_config(str(dst / "config.json"))
    autoload = dict(c.iterate_autoload_presets())
    check(f"{case}: own loader autoload -> owned preset", autoload.get(group.key) == intent.owned_preset_name, str(autoload))
    check(f"{case}: other device selection untouched", autoload.get("other-keyboard") == "gaming")
    check(f"{case}: other device preset bytes untouched",
          (dst / "presets/other-keyboard/gaming.json").read_bytes() == before_other)
    check(f"{case}: original preset files untouched",
          all(p.read_bytes() == b for p, b in prior_files.items()))

    if fam == "2.0":
        managed = [e for e in entries if sorted(i[1] for i in e["input"]) in managed_inputs
                   and e["input"][-1][1] in (105, 106) and len(e["input"]) == 3
                   and {e["input"][0][1], e["input"][1][1]} <= {29, 125, 126}]
        ok = all(e["output_symbol"] in ("hold_keys(KEY_LEFTCTRL,KEY_PAGEUP)", "hold_keys(KEY_LEFTCTRL,KEY_PAGEDOWN)")
                 and e["target_uinput"] == "keyboard" and e["release_combination_keys"] is True
                 and ((e["input"][-1][1] == 105) == ("PAGEUP" in e["output_symbol"]))
                 for e in managed)
        check(f"{case}: own loader parses 4 managed hold_keys mappings", len(managed) == 4 and ok,
              json.dumps(managed))
    else:
        managed = [e for e in entries if len(e["key"]) == 3 and e["key"][-1][1] in (105, 106)
                   and {e["key"][0][1], e["key"][1][1]} in ({29, 125}, {29, 126})]
        ok = all(e["output"] == (["KEY_PAGEUP", "keyboard"] if e["key"][-1][1] == 105 else ["KEY_PAGEDOWN", "keyboard"])
                 for e in managed)
        check(f"{case}: own loader parses 4 managed mappings", len(managed) == 4 and ok, json.dumps(managed))
        check(f"{case}: extra preset config preserved", seed is None or extra == {k: v for k, v in seed.items() if k != "mapping"}, str(extra))
        for e in entries:
            sym = e["output"][0]
            if is_this_a_macro(sym):
                check(f"{case}: 1.4 macro parses: {sym}", parse(sym) is not None)
    # the package's own permutation-aware lookup must find the managed binding
    seed_doc = None
    if fam == "2.0":
        from inputremapper.configs.input_config import InputCombination
        p = Preset(str(owned_path)); p.load()
        written = A.read_json(owned_path)
        for entry in written:
            if A.v2_entry_identity(entry) not in intent.identities:
                continue
            cfgs = entry["input_combination"]
            for order in (cfgs, [cfgs[1], cfgs[0], cfgs[2]]):
                m = p.get_mapping(InputCombination(order))
                codes = tuple(c["code"] for c in order)
                check(f"{case}: package get_mapping{codes} -> managed",
                      m is not None and m.output_symbol.startswith("hold_keys(KEY_LEFTCTRL,"),
                      m.output_symbol if m else "None")
        seed_name = A.autoload_selection(A.read_json(dst / "config.json.orig") if (dst / "config.json.orig").exists() else None, group.key)
    else:
        from inputremapper.key import Key
        m = Mapping(); m.load(str(owned_path))
        for combo in intent.combos:
            for order in (combo, (combo[1], combo[0], combo[2])):
                got = m.get_mapping(Key(*[(1, c, 1) for c in order]))
                check(f"{case}: package get_mapping{order} -> managed",
                      got is not None and got[0] in ("KEY_PAGEUP", "KEY_PAGEDOWN"), str(got))
        from inputremapper.system_mapping import system_mapping
        check("1.4 system_mapping knows KEY_PAGEUP/KEY_PAGEDOWN",
              system_mapping.get("KEY_PAGEUP") == 104 and system_mapping.get("KEY_PAGEDOWN") == 109)
    src_cfg = json.load(open(os.path.join(src, "config.json")))
    sel = src_cfg.get("autoload", {}).get(group.key)
    seed_doc = json.load(open(os.path.join(src, "presets", group.name, sel + ".json"))) if sel else None
    seed_entries = (seed_doc or []) if fam == "2.0" else list((seed_doc or {}).get("mapping", {}).items())
    unrelated = [e for e in seed_entries if (A.v2_entry_identity(e) if fam == "2.0" else A.v1_key_identity(e[0])) not in intent.identities]
    check(f"{case}: unrelated entries preserved ({len(unrelated)}) + 4 managed", len(entries) == len(unrelated) + 4,
          f"{len(entries)} entries")
    os.makedirs(os.path.join(out, case), exist_ok=True)
    shutil.copy2(owned_path, os.path.join(out, case, "expected-owned.json"))
    # later user edit through the package's own classes, then restore
    if fam == "2.0":
        from inputremapper.configs.mapping import Mapping as M2
        p = Preset(str(owned_path)); p.load()
        p.add(M2(input_combination=[{"type": 1, "code": 88}], target_uinput="keyboard", output_symbol="KEY_F12"))
        p.save()
        record = plan.record
    else:
        from inputremapper.key import Key
        m = Mapping(); m.load(str(owned_path))
        m.change(Key(1, 88, 1), "keyboard", "KEY_F12"); m.save(str(owned_path))
        record = A.new_restore_record(fam, group.key, group.name, intent, sel, True, build)
    rplan = perform(A.restore_group(fam, HOME, record))
    c = GlobalConfig(); c.load_config(str(dst / "config.json"))
    restored = dict(c.iterate_autoload_presets())
    check(f"{case}: restore -> own loader sees prior selection", restored.get(group.key) == sel and restored.get("other-keyboard") == "gaming", str(restored))
    if owned_path.exists():
        after, _ = load_owned(owned_path)
        if fam == "2.0":
            has_edit = any(e["input"] == [[1, 88, None]] and e["output_symbol"] == "KEY_F12" for e in after)
            leftover = [e for e in after if e["output_symbol"].startswith("hold_keys(KEY_LEFTCTRL")]
        else:
            has_edit = any(e["key"] == [[1, 88, 1]] and e["output"] == ["KEY_F12", "keyboard"] for e in after)
            leftover = [e for e in after if e["output"][0] in ("KEY_PAGEUP", "KEY_PAGEDOWN")]
        prior_managed = len(record["prior_managed_entries"])
        check(f"{case}: restore keeps later edit, drops installed chords, returns {prior_managed} prior chord entries",
              has_edit and not leftover and len(after) == len(unrelated) + 1 + prior_managed, f"{len(after)} entries; notes={rplan.notes}")
    else:
        check(f"{case}: restore kept owned preset with later edit", False, "owned preset deleted")
    check(f"{case}: original preset files untouched after restore",
          all(p.read_bytes() == b for p, b in prior_files.items()))
    report["cases"][case] = {"entries": entries, "autoload": autoload}

# --- device grouping cross-check against the package's own _FindGroups ---
devices = A.parse_proc_input_devices(Path(proc_txt).read_text())


class _Info:
    def __init__(self, d):
        self.bustype, self.vendor, self.product = d.bustype, d.vendor, d.product


class FakeDevice:
    by_path = {}

    def __init__(self, path):
        d = FakeDevice.by_path[path]
        self.path, self.name, self.phys, self.info, self._d = path, d.name, d.phys, _Info(d), d

    def capabilities(self, absinfo=False):
        caps = {}
        for ev_type, bits in ((1, self._d.key), (2, self._d.rel), (3, self._d.abs)):
            if ev_type in self._d.ev and bits:
                caps[ev_type] = sorted(bits)
        return caps


for d in devices:
    if d.event:
        FakeDevice.by_path[f"/dev/input/{d.event}"] = d
rgroups.evdev.list_devices = lambda: list(FakeDevice.by_path)
rgroups.evdev.InputDevice = FakeDevice
pipe = multiprocessing.Pipe()
rgroups._FindGroups(pipe[1]).run()
theirs = [json.loads(g) for g in json.loads(pipe[0].recv())]
ours = A.group_devices(devices)
their_view = sorted((g["key"], tuple(sorted(g["names"])), tuple(g["types"])) for g in theirs)
our_view = sorted((g.key, tuple(sorted(g.names)), g.types) for g in ours)
check("grouping matches package _FindGroups (keys, names, types)", their_view == our_view,
      json.dumps({"theirs": their_view, "ours": our_view}))
theirs_filtered = sorted(g["key"] for g in theirs if not sorted(g["names"], key=len)[0].startswith("input-remapper"))
eligible, rejected = A.eligible_keyboards(ours, intent)
report["eligible"] = [g.key for g in eligible]
report["rejected"] = rejected
report["package_filter_keys"] = theirs_filtered

print(json.dumps(report, indent=1, sort_keys=True))
sys.exit(0 if all(c["ok"] for c in report["checks"]) else 1)
