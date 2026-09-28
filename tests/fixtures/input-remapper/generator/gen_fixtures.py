"""Phase 1: build collision fixtures with each version's OWN classes.

Usage (through run.sh): gen_fixtures.py FAMILY EXTRACT_ROOT OUT_DIR
Writes OUT_DIR/<case>/<config-dir>/... exactly as the package saved it, then
loads every file back through the package's own loader and prints a report.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
from irboot import boot  # noqa: E402

fam, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
boot(root)
HOME = os.environ["HOME"]

TARGET = "fixture-keyboard"
OTHER = "other-keyboard"
HASH = "00000000000000000000000000000001"

CTRL, LMETA, RMETA, LEFT, RIGHT, UP = 29, 125, 126, 105, 106, 103
CAPS, F13 = 58, 183

report = {"family": fam, "cases": {}}


def k(*codes):
    return [(1, c) for c in codes]


if fam == "1.4":
    import inputremapper.paths as paths
    from inputremapper.config import GlobalConfig
    from inputremapper.key import Key
    from inputremapper.mapping import Mapping
    from inputremapper.groups import _Group, _Groups
    from inputremapper.logger import VERSION

    CONF_DIR = "input-remapper"
    SYM = {"esc": "Escape", "home": "Home", "end": "End", "a": "a", "b": "b",
           "macro": "key(KEY_A).key(KEY_B)"}

    def make_preset(path, entries, extra_config=None):
        m = Mapping()
        for combo, symbol in entries:
            m.change(Key(*[(t, c, 1) for t, c in combo]), "keyboard", symbol)
        for key, value in (extra_config or {}).items():
            m.set(key, value)
        m.save(path)

    def load_preset(path):
        m = Mapping()
        m.load(path)
        got = []
        for key, value in m:
            got.append({"key": [list(x) for x in key.keys], "output": list(value)})
        return {"entries": got, "config": m._config}

    def new_config():
        c = GlobalConfig()
        c.load_config()  # creates INITIAL_CONFIG under the patched HOME
        return c

    def load_config(path):
        c = GlobalConfig()
        c.load_config(path)
        return {"autoload": dict(c.iterate_autoload_presets()), "version": c._config.get("version")}

else:
    import inputremapper.configs.paths as paths
    from inputremapper.configs.global_config import GlobalConfig
    from inputremapper.configs.mapping import Mapping
    from inputremapper.configs.preset import Preset
    from inputremapper.groups import _Group, _Groups
    from inputremapper.logger import VERSION

    CONF_DIR = "input-remapper-2"
    SYM = {"esc": "KEY_ESC", "home": "KEY_HOME", "end": "KEY_END", "a": "KEY_A",
           "b": "KEY_B", "macro": "key(KEY_A).key(KEY_B)"}

    def make_preset(path, entries, extra_config=None):
        p = Preset(path)
        for combo, symbol in entries:
            combination = []
            for item in combo:
                t, c = item[0], item[1]
                cfg = {"type": t, "code": c}
                if len(item) > 2:
                    cfg["origin_hash"] = item[2]
                combination.append(cfg)
            p.add(Mapping(input_combination=combination, target_uinput="keyboard",
                          output_symbol=symbol))
        p.save()

    def load_preset(path):
        p = Preset(path)
        p.load()
        got = []
        for m in p:
            got.append({
                "input": [[ic.type, ic.code, ic.origin_hash] for ic in m.input_combination],
                "output_symbol": m.output_symbol,
                "target_uinput": m.target_uinput,
                "mapping_type": m.mapping_type,
                "release_combination_keys": m.release_combination_keys,
            })
        return {"entries": got}

    def new_config():
        c = GlobalConfig()
        c.load_config()
        return c

    def load_config(path):
        c = GlobalConfig()
        c.load_config(path)
        return {"autoload": dict(c.iterate_autoload_presets()), "version": c._config.get("version")}

assert paths.CONFIG_PATH.startswith(HOME), paths.CONFIG_PATH
report["package_version"] = VERSION
report["config_path_rel"] = os.path.relpath(paths.CONFIG_PATH, HOME)

unrelated = [
    (k(CAPS), SYM["esc"]),
    (k(LMETA, LEFT), SYM["home"]),
    (k(CTRL, LMETA, UP), SYM["end"]),
    (k(F13), SYM["macro"]),
]
c1 = (k(CTRL, LMETA, LEFT), SYM["home"])
c2_permuted = (k(LMETA, CTRL, RIGHT), SYM["end"])
if fam == "1.4":
    c3 = (k(CTRL, RMETA, LEFT), SYM["home"])
else:
    c3 = ([(1, CTRL, HASH), (1, RMETA, HASH), (1, LEFT, HASH)], SYM["home"])

cases = {
    "one-conflict": {
        "presets": {
            (TARGET, "daily"): unrelated + [c1],
            (TARGET, "spare"): [(k(CTRL, LMETA, RIGHT), SYM["a"])],
            (OTHER, "gaming"): [(k(CTRL, LMETA, LEFT), SYM["b"])],
        },
        "autoload": {TARGET: "daily", OTHER: "gaming"},
    },
    "both-conflict": {
        "presets": {
            (TARGET, "daily"): unrelated[:2] + [c1, c2_permuted, c3],
            (OTHER, "gaming"): [(k(CTRL, LMETA, LEFT), SYM["b"])],
        },
        "autoload": {TARGET: "daily", OTHER: "gaming"},
    },
    "no-selection": {
        "presets": {
            (TARGET, "spare"): [(k(CTRL, LMETA, RIGHT), SYM["a"])],
            (OTHER, "gaming"): [(k(CTRL, LMETA, LEFT), SYM["b"])],
        },
        "autoload": {OTHER: "gaming"},
    },
}
v1_extra = {"macros.keystroke_sleep_ms": 25} if fam == "1.4" else None

for case, spec in cases.items():
    if os.path.exists(paths.CONFIG_PATH):
        shutil.rmtree(paths.CONFIG_PATH)
    for (group_name, preset), entries in spec["presets"].items():
        group = _Group(paths=["/dev/input/event90"], names=[group_name],
                       types=["keyboard"], key=group_name)
        extra = v1_extra if (preset == "daily") else None
        make_preset(group.get_preset_path(preset), entries, extra)
    cfg = new_config()
    for group_key, preset in spec["autoload"].items():
        cfg.set_autoload_preset(group_key, preset)

    # load everything back through the package's own loaders
    loaded = {"config": load_config(os.path.join(paths.CONFIG_PATH, "config.json")), "presets": {}}
    for (group_name, preset) in spec["presets"]:
        group = _Group(paths=["/dev/input/event90"], names=[group_name],
                       types=["keyboard"], key=group_name)
        loaded["presets"][f"{group_name}/{preset}"] = load_preset(group.get_preset_path(preset))
    report["cases"][case] = loaded

    dest = os.path.join(out, case, CONF_DIR)
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(paths.CONFIG_PATH, "config.json"), dest)
    shutil.copytree(os.path.join(paths.CONFIG_PATH, "presets"), os.path.join(dest, "presets"),
                    dirs_exist_ok=True)

# group dumps in the package's own serialization
fixture_groups = [
    _Group(paths=["/dev/input/event90", "/dev/input/event91"],
           names=["fixture-keyboard", "fixture-keyboard Consumer Control"],
           types=["keyboard"], key="fixture-keyboard"),
    _Group(paths=["/dev/input/event92"], names=["fixture-mouse"], types=["mouse"],
           key="fixture-mouse"),
    _Group(paths=["/dev/input/event93"], names=["input-remapper keyboard"],
           types=["keyboard"], key="input-remapper keyboard"),
    _Group(paths=["/dev/input/event94"],
           names=["input-remapper fixture-keyboard forwarded"], types=["keyboard"],
           key="input-remapper fixture-keyboard forwarded"),
    _Group(paths=["/dev/input/event95"], names=["fixture-keyboard"], types=["keyboard"],
           key="fixture-keyboard 2"),
]
dump = json.dumps([g.dumps() for g in fixture_groups])
g = _Groups()
g.loads(dump)
report["groups_filter_keys"] = [x.key for x in g.filter()]
report["groups_list_names"] = g.list_group_names()
with open(os.path.join(out, "groups-dump.json"), "w") as fh:
    fh.write(dump + "\n")

print(json.dumps(report, indent=1, sort_keys=True))
