"""GUI apply + restore against a real dconf on a PRIVATE session bus.

The child runs under ``dbus-run-session`` with HOME, XDG_CONFIG_HOME,
XDG_DATA_HOME, XDG_STATE_HOME, XDG_CACHE_HOME and XDG_RUNTIME_DIR (0700) all
inside a temp dir and an environment built from scratch (no inherited D-Bus
address, display or dconf profile), so dconf-service writes only the temp
``XDG_CONFIG_HOME/dconf/user``. It uses the host's installed schemas. The real
``~/.config/dconf/user`` is only stat()ed and hashed, before and after, and
must be byte-identical (or still absent).

Skipped (not passed) when dbus-run-session, dconf, gsettings or gdbus is
missing, or the host is not a supported Ubuntu release. The input-remapper
component is disabled: it would talk to the system daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
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
    out["before"] = before
    out["after"] = sh("dconf", "dump", "/")
    print(json.dumps(out))
    """
)


def _real_dconf_db() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "dconf" / "user"


def _fingerprint(path: Path):
    """Read-only: (size, sha256) or None when absent."""

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return None
    return len(data), hashlib.sha256(data).hexdigest()


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
        real_db = _real_dconf_db()
        before_real = _fingerprint(real_db)
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

        self.assertEqual(_fingerprint(real_db), before_real, "real dconf database changed")
        self.assertTrue(out["bus"].startswith("unix:"), out["bus"])
        self.assertTrue(temp_db_exists, "dconf writes did not land in the temp XDG_CONFIG_HOME")
        self.assertEqual(out["apply"]["status"], "PASS", out["apply"]["reasons"])
        self.assertEqual(out["mismatches"], [])
        self.assertEqual(out["sets_first"], out["available"])
        self.assertEqual(out["again"], "PASS")
        self.assertEqual(out["sets_again"], 0)
        self.assertEqual(out["restore"]["status"], "PASS", out["restore"]["reasons"])
        self.assertEqual(out["after"], out["before"])
        self.assertIn("clock-format='12h'", out["before"])


if __name__ == "__main__":
    unittest.main()
