"""GUI apply + restore against a real dconf on a PRIVATE session bus.

The child runs under ``dbus-run-session`` with HOME, XDG_CONFIG_HOME,
XDG_DATA_HOME, XDG_STATE_HOME, XDG_CACHE_HOME and XDG_RUNTIME_DIR (0700) all
inside a temp dir and an environment built from scratch (no inherited D-Bus
address, display or dconf profile), so dconf-service writes only the temp
``XDG_CONFIG_HOME/dconf/user``. It uses the host's installed schemas.

The real database is only read, with ``dconf read`` (no session bus in its
environment), for every managed key and the sentinel key the child writes,
before and after: none of them may change. The whole file is not compared,
because the live desktop session writes it on its own (window state etc.).

Skipped (not passed) when dbus-run-session, dconf, gsettings or gdbus is
missing, or the host is not a supported Ubuntu release. The input-remapper
component is disabled: it would talk to the system daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import gui  # noqa: E402
from installer import platform as plat  # noqa: E402

TOOLS = ("dbus-run-session", "dconf", "gsettings", "gdbus")

CHILD = textwrap.dedent(
    """
    import json, os, subprocess, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from installer import gui
    from installer.platform import Platform, Target
    from installer.runner import Runner

    root = Path(sys.argv[2]); release = sys.argv[3]
    home = root / "home"
    target = Target(uid=os.getuid(), gid=os.getgid(), username="fixture", home=home,
                    data_home=home / ".local/share", state_home=home / ".local/state",
                    config_home=home / ".config", cache_home=home / ".cache")
    platform = Platform("ubuntu", release, "amd64")
    env = dict(os.environ, XDG_CURRENT_DESKTOP="ubuntu:GNOME")

    class Counting(Runner):
        def __init__(self):
            self.sets = 0
        def run(self, argv, **kw):
            if list(argv[:2]) == ["gsettings", "set"]:
                self.sets += 1
            return super().run(argv, **kw)

    def sh(*argv):
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30,
                              check=True).stdout

    manifest = gui.load_manifest()
    first = manifest["settings"][0]
    terminal = next(e for e in manifest["settings"] if e.get("path"))
    # Explicit prior values (one fixed-path, one relocatable) and an
    # unmanaged sentinel; everything else starts unset.
    sh("gsettings", "set", gui.gsettings_target(first), first["key"], "['<Super>q']")
    sh("gsettings", "set", gui.gsettings_target(terminal), terminal["key"], "'<Control><Alt>n'")
    sh("dconf", "write", "/org/gnome/desktop/interface/clock-format", "'12h'")
    before = sh("dconf", "dump", "/")

    runner = Counting()
    out = {"bus": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")}
    out["gui_root"] = str(target.state_root / "gui")
    applied = gui.apply_or_defer(target, runner, env, platform=platform,
                                 components=["gnome-settings"])
    out["apply"] = {"status": applied["status"], "reasons": applied["reasons"]}
    out["sets_first"] = runner.sets
    mismatches = []
    for entry in manifest["settings"]:
        if not entry["releases"][release]["available"]:
            continue
        got = sh("gsettings", "get", gui.gsettings_target(entry), entry["key"]).strip()
        if got != entry["value"]:
            mismatches.append([entry["id"], got, entry["value"]])
    out["mismatches"] = mismatches
    out["available"] = sum(1 for e in manifest["settings"] if e["releases"][release]["available"])
    runner.sets = 0
    again = gui.apply_or_defer(target, runner, env, platform=platform, components=["gnome-settings"])
    out["again"] = again["status"]
    out["sets_again"] = runner.sets
    restored = gui.restore_gui(target, runner, env, platform=platform, components=["gnome-settings"])
    out["restore"] = {"status": restored["status"], "reasons": restored["reasons"]}
    out["restore_saved"] = restored["details"].get("saved_current", {})
    baseline = json.loads((target.state_root / "gui/backups/baseline/gsettings.json").read_text())
    out["dconf_keys"] = sorted(snap["dconf_key"] for snap in baseline["keys"].values())
    out["before"] = before
    out["after"] = sh("dconf", "dump", "/")
    print(json.dumps(out))
    """
)


SENTINEL_KEY = "/org/gnome/desktop/interface/clock-format"


def _managed_dconf_keys() -> list[str]:
    """dconf paths of every managed key plus the sentinel the child writes.

    Fixed schema paths come from ``gsettings list-schemas --print-paths``
    run with the memory backend and no session bus, so nothing is read from
    or written to any dconf database.
    """

    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GSETTINGS_BACKEND": "memory"}
    listing = subprocess.run(["gsettings", "list-schemas", "--print-paths"],
                             capture_output=True, text=True, env=env, timeout=30, check=True,
                             stdin=subprocess.DEVNULL).stdout
    paths = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2:
            paths[parts[0]] = parts[1]
    keys = {SENTINEL_KEY}
    for entry in gui.load_manifest()["settings"]:
        base = entry.get("path") or paths.get(entry["schema"])
        if base:
            keys.add(base + entry["key"])
    return sorted(keys)


def _read_real(keys: list[str]) -> dict:
    """Read-only ``dconf read`` of each key in the user's real database.

    The session bus address is dropped: ``dconf read`` reads the database
    file directly and needs no bus, so this cannot reach dconf-service.
    """

    env = {k: v for k, v in os.environ.items() if not k.startswith("DBUS_")}
    env["LC_ALL"] = "C"
    values = {}
    for key in keys:
        done = subprocess.run(["dconf", "read", key], capture_output=True, text=True, env=env,
                              timeout=30, stdin=subprocess.DEVNULL)
        values[key] = (done.returncode, done.stdout.strip())
    return values


def _host_release() -> str | None:
    try:
        distribution, release = plat.parse_os_release(Path("/etc/os-release").read_text())
    except (OSError, plat.PlatformError):
        return None
    if distribution != "ubuntu" or release not in plat.SUPPORTED_RELEASES:
        return None
    return release


class PrivateBusDconfTests(unittest.TestCase):
    def setUp(self) -> None:
        missing = [tool for tool in TOOLS if shutil.which(tool) is None]
        if missing:
            self.skipTest(f"missing {', '.join(missing)}")
        self.release = _host_release()
        if self.release is None:
            self.skipTest("host is not a supported Ubuntu release")
        if not Path("/usr/share/glib-2.0/schemas/gschemas.compiled").is_file():
            self.skipTest("no installed GSettings schemas")

    def test_apply_and_restore_on_private_bus(self) -> None:
        watched = _managed_dconf_keys()
        before_real = _read_real(watched)
        with tempfile.TemporaryDirectory(prefix="gui-dconf.") as tmp:
            root = Path(tmp)
            home = root / "home"
            runtime = root / "runtime"
            for sub in (".config", ".local/share", ".local/state", ".cache"):
                (home / sub).mkdir(parents=True)
            runtime.mkdir(mode=0o700)
            os.chmod(runtime, 0o700)
            script = root / "child.py"
            script.write_text(CHILD, encoding="utf-8")
            env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_RUNTIME_DIR": str(runtime),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            done = subprocess.run(
                ["dbus-run-session", "--", sys.executable, "-B", str(script), str(REPO_ROOT),
                 str(root), self.release],
                capture_output=True, text=True, env=env, timeout=180, stdin=subprocess.DEVNULL,
            )
            self.assertEqual(done.returncode, 0, done.stderr[-3000:])
            out = json.loads(done.stdout.strip().splitlines()[-1])
            temp_db = home / ".config" / "dconf" / "user"
            temp_db_exists = temp_db.is_file()
            gui_root = Path(out["gui_root"])
            restore_backup = out["restore_saved"].get("backup", "")
            restore_saved_on_disk = (gui_root / "backups" / restore_backup
                                     / "gsettings.json").is_file() if restore_backup else False
            under_temp = gui_root.is_relative_to(root)

        after_real = _read_real(watched)
        changed = {k: (before_real[k], after_real[k]) for k in watched
                   if before_real[k] != after_real[k]}
        self.assertEqual(changed, {}, "managed keys changed in the real dconf database")
        # Every key the child wrote was watched (a missed schema path would hide it).
        self.assertEqual(sorted(set(out["dconf_keys"]) - set(watched)), [])
        self.assertEqual(len(out["dconf_keys"]), out["available"])
        self.assertTrue(out["bus"].startswith("unix:"), out["bus"])
        self.assertNotEqual(out["bus"], os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                            "the child did not get a private session bus")
        self.assertTrue(under_temp, out["gui_root"])
        self.assertTrue(temp_db_exists, "dconf writes did not land in the temp XDG_CONFIG_HOME")
        self.assertEqual(out["apply"]["status"], "PASS", out["apply"]["reasons"])
        self.assertEqual(out["mismatches"], [])
        self.assertEqual(out["sets_first"], out["available"])
        self.assertEqual(out["again"], "PASS")
        self.assertEqual(out["sets_again"], 0)
        self.assertEqual(out["restore"]["status"], "PASS", out["restore"]["reasons"])
        # The restore saved every value it replaced (all installed values).
        self.assertEqual(len(out["restore_saved"].get("keys", [])), out["available"])
        self.assertEqual(out["restore_saved"].get("changed_since_install"), [])
        self.assertTrue(restore_saved_on_disk, out["restore_saved"])
        self.assertEqual(out["after"], out["before"])
        self.assertIn("clock-format='12h'", out["before"])


if __name__ == "__main__":
    unittest.main()
