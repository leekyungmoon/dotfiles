"""Unit tests for installer.platform: platform gates and owner/root anchoring."""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import platform as plat  # noqa: E402


def fake_passwd(uid: int, home: Path, name: str = "fixture-user"):
    entry = pwd.struct_passwd((name, "x", uid, uid, "", str(home), "/bin/zsh"))

    def getpwuid(requested: int):
        if requested != uid:
            raise KeyError(requested)
        return entry

    return getpwuid


class OsReleaseTests(unittest.TestCase):
    def test_quoted_and_unquoted_values(self):
        text = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n'
        self.assertEqual(plat.parse_os_release(text), ("ubuntu", "24.04"))

    def test_single_quotes_comments_and_blank_lines(self):
        text = "# comment\n\nID='Ubuntu'\nVERSION_ID='22.04'\n"
        self.assertEqual(plat.parse_os_release(text), ("ubuntu", "22.04"))

    def test_missing_fields_are_rejected(self):
        with self.assertRaises(plat.PlatformError):
            plat.parse_os_release("NAME=Ubuntu\n")
        with self.assertRaises(plat.PlatformError):
            plat.parse_os_release("ID=ubuntu\n")


class ArchitectureTests(unittest.TestCase):
    def test_known_aliases(self):
        self.assertEqual(plat.normalize_architecture("x86_64"), "amd64")
        self.assertEqual(plat.normalize_architecture("amd64\n"), "amd64")
        self.assertEqual(plat.normalize_architecture("aarch64"), "arm64")
        self.assertEqual(plat.normalize_architecture("ARM64"), "arm64")

    def test_unsupported_architectures(self):
        for raw in ("i386", "i686", "armhf", "armv7l", "riscv64", "s390x", ""):
            with self.subTest(raw=raw), self.assertRaises(plat.PlatformError):
                plat.normalize_architecture(raw)


class SupportedPlatformTests(unittest.TestCase):
    def test_supported_matrix(self):
        for release in ("22.04", "24.04"):
            for arch in ("amd64", "arm64"):
                platform = plat.Platform("ubuntu", release, arch)
                with self.subTest(release=release, arch=arch):
                    self.assertTrue(platform.is_supported)
                    plat.require_supported_platform(platform)

    def test_rejections(self):
        cases = [
            plat.Platform("ubuntu", "20.04", "amd64"),
            plat.Platform("ubuntu", "26.04", "amd64"),
            plat.Platform("debian", "12", "amd64"),
            plat.Platform("ubuntu", "24.04", "i386"),
        ]
        for platform in cases:
            with self.subTest(platform=platform):
                self.assertFalse(platform.is_supported)
                with self.assertRaises(plat.PlatformError):
                    plat.require_supported_platform(platform)

    def test_detect_platform_reads_given_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "os-release"
            path.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
            detected = plat.detect_platform(path)
        self.assertEqual((detected.distribution, detected.release),
                         ("ubuntu", "22.04"))
        self.assertIn(detected.architecture, plat.SUPPORTED_ARCHITECTURES)

    def test_detect_platform_missing_file(self):
        with self.assertRaises(plat.PlatformError):
            plat.detect_platform(Path("/nonexistent/os-release"))

    def test_linux_platforms_keep_their_shape(self):
        platform = plat.Platform("ubuntu", "24.04", "arm64")
        self.assertEqual(platform.system, "linux")
        self.assertTrue(platform.is_linux)
        self.assertFalse(platform.is_darwin)
        self.assertFalse(platform.best_effort)
        self.assertEqual(platform.artifact_key, "arm64")
        self.assertEqual(platform.describe(), "ubuntu 24.04 (arm64)")
        self.assertIsNone(plat.homebrew_prefix(platform, {}))


def _version_plist(directory: Path, version: str) -> Path:
    import plistlib

    path = directory / "SystemVersion.plist"
    path.write_bytes(plistlib.dumps({"ProductName": "macOS",
                                     "ProductVersion": version}))
    return path


class MacosPlatformTests(unittest.TestCase):
    def setUp(self):
        # These tests hand detect_macos a plist. A SYSTEM_VERSION_COMPAT=1
        # exported on the Mac that runs them would send it to the real
        # sw_vers instead (MacosVersionCompatTests has that path).
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SYSTEM_VERSION_COMPAT", None)

    def test_supported_matrix(self):
        cases = [(r, "arm64") for r in ("15", "26", "27")]
        cases += [(r, "amd64") for r in ("15", "26")]
        for release, arch in cases:
            platform = plat.Platform("macos", release, arch, "darwin")
            with self.subTest(release=release, arch=arch):
                self.assertTrue(platform.is_supported)
                plat.require_supported_platform(platform)
                self.assertEqual(platform.best_effort, arch == "amd64")

    def test_rejections(self):
        cases = [
            plat.Platform("macos", "14", "arm64", "darwin"),
            plat.Platform("macos", "13", "amd64", "darwin"),
            plat.Platform("macos", "28", "arm64", "darwin"),
            plat.Platform("macos", "27", "amd64", "darwin"),
            plat.Platform("darwin", "26", "arm64", "darwin"),
        ]
        for platform in cases:
            with self.subTest(platform=platform):
                self.assertFalse(platform.is_supported)
                with self.assertRaises(plat.PlatformError) as caught:
                    plat.require_supported_platform(platform)
                self.assertNotIn("Ubuntu", str(caught.exception))

    def test_rejection_message_names_the_supported_set(self):
        with self.assertRaises(plat.PlatformError) as caught:
            plat.require_supported_platform(plat.Platform("macos", "14", "arm64", "darwin"))
        message = str(caught.exception)
        self.assertIn("'14'", message)
        self.assertIn("15 (Sequoia), 26 (Tahoe), 27 on Apple silicon", message)
        self.assertIn("15, 26 on Intel (best effort)", message)

    def test_ubuntu_messages_are_unchanged(self):
        with self.assertRaises(plat.PlatformError) as caught:
            plat.require_supported_platform(plat.Platform("ubuntu", "26.04", "amd64"))
        self.assertEqual(str(caught.exception),
                         "unsupported Ubuntu release '26.04'; supported: 22.04, 24.04")

    def test_artifact_key_and_description(self):
        platform = plat.Platform("macos", "26", "arm64", "darwin")
        self.assertEqual(platform.artifact_key, "darwin-arm64")
        self.assertEqual(platform.describe(), "macos 26 Tahoe (arm64)")
        self.assertEqual(plat.Platform("macos", "27", "arm64", "darwin").describe(),
                         "macos 27 (arm64)")
        self.assertEqual(plat.Platform("macos", "15", "amd64", "darwin").artifact_key,
                         "darwin-amd64")

    def test_parse_macos_version(self):
        for raw, major in (("15.5", "15"), ("26.0.1", "26"), (" 27 \n", "27"),
                           ("26.5.2", "26")):
            with self.subTest(raw=raw):
                self.assertEqual(plat.parse_macos_version(raw), major)
        for raw in ("", "10.16-beta", "macOS 15", "15."):
            with self.subTest(raw=raw), self.assertRaises(plat.PlatformError):
                plat.parse_macos_version(raw)

    def test_detect_macos_reads_the_version_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = _version_plist(Path(tmp), "26.5.2")
            detected = plat.detect_macos(version_plist=plist,
                                         sysctl=lambda name: "0", machine="arm64")
        self.assertEqual(detected, plat.Platform("macos", "26", "arm64", "darwin"))

    def test_detect_macos_intel(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = _version_plist(Path(tmp), "15.6")
            detected = plat.detect_macos(version_plist=plist,
                                         sysctl=lambda name: None, machine="x86_64")
        self.assertEqual(detected, plat.Platform("macos", "15", "amd64", "darwin"))
        self.assertTrue(detected.best_effort)

    def test_detect_macos_refuses_rosetta(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = _version_plist(Path(tmp), "26.1")
            with self.assertRaises(plat.PlatformError) as caught:
                plat.detect_macos(
                    version_plist=plist, machine="x86_64",
                    sysctl=lambda name: "1" if name == "sysctl.proc_translated" else None)
        self.assertIn("Rosetta", str(caught.exception))
        self.assertIn("arch -arm64", str(caught.exception))

    def test_homebrew_prefix(self):
        arm = plat.Platform("macos", "26", "arm64", "darwin")
        intel = plat.Platform("macos", "15", "amd64", "darwin")
        self.assertEqual(plat.homebrew_prefix(arm, {}), Path("/opt/homebrew"))
        self.assertEqual(plat.homebrew_prefix(intel, {}), Path("/usr/local"))
        self.assertEqual(plat.homebrew_prefix(arm, {"HOMEBREW_PREFIX": "/x/brew/"}),
                         Path("/x/brew"))
        # A relative or empty value is not a prefix.
        self.assertEqual(plat.homebrew_prefix(arm, {"HOMEBREW_PREFIX": "brew"}),
                         Path("/opt/homebrew"))
        self.assertEqual(plat.homebrew_prefix(arm, {"HOMEBREW_PREFIX": " "}),
                         Path("/opt/homebrew"))

    def test_explicit_os_release_path_is_always_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "os-release"
            path.write_text("ID=ubuntu\nVERSION_ID=24.04\n", encoding="utf-8")
            detected = plat.detect_platform(path)
        self.assertEqual(detected.system, "linux")


class MacosVersionCompatTests(unittest.TestCase):
    """SYSTEM_VERSION_COMPAT=1 (or 2) makes libSystem hand every open() of
    SystemVersion.plist in the process another plist: SystemVersionCompat's
    10.16 on macOS 11 to 15, 16.0 on 26. etc/install's sw_vers runs with the
    variable at 0 and accepts the Mac; install.py, which inherits it, must
    not refuse the same Mac. sw_vers is a fake here: none runs for real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.sw_vers_envs: list[dict[str, str]] = []

    def detect(self, plist_version: str | None, env: dict[str, str], answer=None):
        """detect_macos on arm64 with a plist saying ``plist_version`` (None:
        no plist) and a sw_vers that answers ``answer`` (None: it fails)."""

        def sw_vers(child_env):
            self.sw_vers_envs.append(dict(child_env))
            return answer

        plist = (self.dir / "missing.plist" if plist_version is None
                 else _version_plist(self.dir, plist_version))
        return plat.detect_macos(version_plist=plist, sysctl=lambda name: None,
                                 machine="arm64", env=env, sw_vers=sw_vers)

    def test_compat_on_asks_sw_vers_and_ignores_the_compat_plist(self):
        # libSystem's rule: strtol(value, NULL, 0) is 1 (macOS) or 2 (iOS).
        for compat_plist, real in (("10.16", "15.5"), ("16.0", "26.0.1")):
            for value in ("1", "2", " 1", "+1", "01", "0x1", "0X02", "1.5"):
                with self.subTest(plist=compat_plist, value=value):
                    self.sw_vers_envs.clear()
                    env = {"SYSTEM_VERSION_COMPAT": value, "PATH": "/usr/bin:/bin"}
                    detected = self.detect(compat_plist, env, answer=real)
                    self.assertEqual(detected, plat.Platform("macos", real.split(".")[0],
                                                             "arm64", "darwin"))
                    plat.require_supported_platform(detected)
                    # Asked once, with the variable at 0 and the rest kept.
                    self.assertEqual(self.sw_vers_envs, [{"SYSTEM_VERSION_COMPAT": "0",
                                                          "PATH": "/usr/bin:/bin"}])
                    self.assertEqual(env["SYSTEM_VERSION_COMPAT"], value)

    def test_compat_on_with_a_failing_sw_vers_falls_back_to_the_plist(self):
        for answer in (None, ""):
            with self.subTest(answer=answer):
                self.sw_vers_envs.clear()
                detected = self.detect("26.1", {"SYSTEM_VERSION_COMPAT": "1"}, answer=answer)
                self.assertEqual(detected.release, "26")
                self.assertEqual(len(self.sw_vers_envs), 1)
        # Neither answers: an error, and sw_vers was not asked twice.
        self.sw_vers_envs.clear()
        with self.assertRaises(plat.PlatformError) as caught:
            self.detect(None, {"SYSTEM_VERSION_COMPAT": "1"})
        self.assertIn("cannot read the macOS version", str(caught.exception))
        self.assertEqual(len(self.sw_vers_envs), 1)

    def test_compat_off_reads_the_plist_without_sw_vers(self):
        values = ("0", "", "3", "10", "-1", "true", "0x", "08", "0x10")
        for env in [{}] + [{"SYSTEM_VERSION_COMPAT": value} for value in values]:
            with self.subTest(env=env):
                self.sw_vers_envs.clear()
                detected = self.detect("15.6", env, answer="26.0")
                self.assertEqual(detected.release, "15")
                self.assertEqual(self.sw_vers_envs, [])

    def test_a_compat_release_without_the_variable_asks_sw_vers(self):
        # libSystem's SDK rule can hand a python built with an older SDK the
        # compat plist with the variable unset; no Mac runs 10.16 or 16.x.
        for compat_plist, real in (("10.16", "15.5"), ("16.0", "26.0.1"), ("16", "26.1")):
            with self.subTest(plist=compat_plist):
                self.sw_vers_envs.clear()
                detected = self.detect(compat_plist, {}, answer=real)
                self.assertEqual(detected.release, real.split(".")[0])
                self.assertEqual(self.sw_vers_envs, [{"SYSTEM_VERSION_COMPAT": "0"}])
        # A failing sw_vers leaves the plist's answer, which is refused.
        detected = self.detect("16.0", {}, answer=None)
        self.assertEqual(detected.release, "16")
        with self.assertRaises(plat.PlatformError):
            plat.require_supported_platform(detected)
        # Real releases never ask.
        self.sw_vers_envs.clear()
        for version in ("15.6", "26.0", "10.15.7", "27.0"):
            self.detect(version, {}, answer="99.0")
        self.assertEqual(self.sw_vers_envs, [])

    def test_compat_off_without_a_plist_still_asks_sw_vers(self):
        detected = self.detect(None, {"PATH": "/usr/bin"}, answer="15.5")
        self.assertEqual(detected.release, "15")
        self.assertEqual(self.sw_vers_envs, [{"PATH": "/usr/bin", "SYSTEM_VERSION_COMPAT": "0"}])
        self.sw_vers_envs.clear()
        with self.assertRaises(plat.PlatformError):
            self.detect(None, {})
        self.assertEqual(len(self.sw_vers_envs), 1)

    def test_the_environment_defaults_to_this_process(self):
        plist = _version_plist(self.dir, "16.0")
        envs = []
        with mock.patch.dict(os.environ, {"SYSTEM_VERSION_COMPAT": "1"}):
            detected = plat.detect_macos(version_plist=plist, sysctl=lambda name: None,
                                         machine="arm64",
                                         sw_vers=lambda env: envs.append(env) or "26.0.1")
        self.assertEqual(detected.release, "26")
        self.assertEqual([env["SYSTEM_VERSION_COMPAT"] for env in envs], ["0"])

    def test_sw_vers_runs_the_system_binary_with_the_given_environment(self):
        done = subprocess.CompletedProcess([], 0, stdout="26.0.1\n", stderr="")
        with mock.patch.object(plat.subprocess, "run", return_value=done) as run:
            self.assertEqual(plat._sw_vers_product_version({"SYSTEM_VERSION_COMPAT": "0"}),
                             "26.0.1")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/sw_vers", "-productVersion"])
        self.assertEqual(run.call_args.kwargs["env"], {"SYSTEM_VERSION_COMPAT": "0"})
        for outcome in (subprocess.CompletedProcess([], 1, stdout="", stderr="no"),
                        subprocess.CompletedProcess([], 0, stdout="\n", stderr=""),
                        FileNotFoundError(2, "No such file or directory"),
                        subprocess.TimeoutExpired(["/usr/bin/sw_vers"], 10)):
            with self.subTest(outcome=outcome):
                effect = ({"side_effect": outcome} if isinstance(outcome, BaseException)
                          else {"return_value": outcome})
                with mock.patch.object(plat.subprocess, "run", **effect):
                    self.assertIsNone(plat._sw_vers_product_version({}))


class ResolveTargetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home with space"
        self.home.mkdir()
        self.uid = os.geteuid()
        self.getpwuid = fake_passwd(self.uid, self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, env):
        return plat.resolve_target(env, euid=self.uid, getpwuid=self.getpwuid)

    def test_defaults_are_under_passwd_home(self):
        target = self.resolve({"HOME": str(self.home)})
        self.assertEqual(target.home, self.home)
        self.assertEqual(target.data_home, self.home / ".local" / "share")
        self.assertEqual(target.state_home, self.home / ".local" / "state")
        self.assertEqual(target.config_home, self.home / ".config")
        self.assertEqual(target.cache_home, self.home / ".cache")
        self.assertEqual(target.repo_root, self.home / ".dotfiles")
        self.assertEqual(target.compat_link, target.repo_root)
        self.assertEqual(target.staging_root,
                         self.home / ".local" / "share" / "personal-dotfiles" / "staging")
        self.assertEqual(target.state_root,
                         self.home / ".local" / "state" / "personal-dotfiles")

    def test_unset_home_falls_back_to_passwd(self):
        self.assertEqual(self.resolve({}).home, self.home)

    def test_root_is_refused_even_with_sudo_user(self):
        with self.assertRaises(plat.PlatformError) as ctx:
            plat.resolve_target(
                {"HOME": "/root", "SUDO_USER": "fixture-user"},
                euid=0,
                getpwuid=self.getpwuid,
            )
        self.assertIn("root", str(ctx.exception))

    def test_sudo_user_never_changes_ownership(self):
        target = self.resolve({"HOME": str(self.home), "SUDO_USER": "someone"})
        self.assertEqual(target.username, "fixture-user")
        self.assertEqual(target.uid, self.uid)

    def test_mismatched_home_is_refused(self):
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(plat.PlatformError):
            self.resolve({"HOME": str(other)})

    def test_missing_passwd_entry(self):
        with self.assertRaises(plat.PlatformError):
            plat.resolve_target({}, euid=self.uid + 12345,
                                getpwuid=self.getpwuid)

    def test_home_owned_by_someone_else_is_refused(self):
        # /  is owned by root, which is never the test's euid.
        if os.geteuid() == 0:
            self.skipTest("cannot model a foreign owner as root")
        getpwuid = fake_passwd(self.uid, Path("/"))
        with self.assertRaises(plat.PlatformError):
            plat.resolve_target({}, euid=self.uid, getpwuid=getpwuid)

    def test_custom_xdg_under_home_with_spaces(self):
        custom = self.home / "my data"
        target = self.resolve({"HOME": str(self.home),
                               "XDG_DATA_HOME": str(custom)})
        self.assertEqual(target.data_home, custom)
        # The checkout stays at ~/.dotfiles; only scratch clones follow XDG.
        self.assertEqual(target.repo_root, self.home / ".dotfiles")
        self.assertEqual(target.staging_root,
                         custom / "personal-dotfiles" / "staging")

    def test_relative_xdg_is_refused(self):
        with self.assertRaises(plat.PlatformError):
            self.resolve({"XDG_STATE_HOME": "relative/state"})

    def test_xdg_outside_home_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaises(plat.PlatformError):
            self.resolve({"XDG_CONFIG_HOME": str(outside)})

    def test_lexically_outside_link_back_into_home_is_refused(self):
        inside = self.home / ".local" / "share"
        inside.mkdir(parents=True)
        link = self.root / "link-into-home"
        link.symlink_to(inside)
        with self.assertRaises(plat.PlatformError):
            self.resolve({"XDG_DATA_HOME": str(link)})

    def test_symlinked_parent_escape_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".local").symlink_to(outside)
        with self.assertRaises(plat.PlatformError):
            self.resolve({"XDG_DATA_HOME": str(self.home / ".local" / "share")})

    def test_dangling_symlinked_parent_escape_is_refused(self):
        (self.home / ".local").symlink_to(self.root / "not-created-yet")
        with self.assertRaises(plat.PlatformError):
            self.resolve({"XDG_STATE_HOME":
                          str(self.home / ".local" / "state")})

    def test_symlink_inside_home_is_accepted(self):
        real = self.home / "real-cache"
        real.mkdir()
        (self.home / "cache-link").symlink_to(real)
        target = self.resolve({"XDG_CACHE_HOME": str(self.home / "cache-link")})
        self.assertEqual(target.cache_home, self.home / "cache-link")

    def test_to_json_has_no_foreign_fields(self):
        payload = self.resolve({}).to_json()
        self.assertIn('"username": "fixture-user"', payload)
        self.assertNotIn("SUDO_USER", payload)


class SessionKindTests(unittest.TestCase):
    def test_declared_type_wins(self):
        self.assertEqual(plat.session_kind({"XDG_SESSION_TYPE": "wayland"}),
                         "wayland")
        self.assertEqual(plat.session_kind({"XDG_SESSION_TYPE": "X11"}), "x11")

    def test_display_variables(self):
        self.assertEqual(plat.session_kind({"WAYLAND_DISPLAY": "wayland-0"}),
                         "wayland")
        self.assertEqual(plat.session_kind({"DISPLAY": ":0"}), "x11")

    def test_headless(self):
        self.assertEqual(plat.session_kind({}), "none")
        self.assertEqual(plat.session_kind({"XDG_SESSION_TYPE": "tty"}), "none")


class GenerationIdTests(unittest.TestCase):
    def test_format(self):
        self.assertTrue(plat.is_generation_id("20260928T010203Z-0123abcd"))
        self.assertFalse(plat.is_generation_id("20260928T010203Z"))
        self.assertFalse(plat.is_generation_id("../20260928T010203Z-0123abcd"))


if __name__ == "__main__":
    unittest.main()
