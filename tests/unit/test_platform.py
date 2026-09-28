"""Unit tests for installer.platform: platform gates and owner/root anchoring."""

from __future__ import annotations

import os
import pwd
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(
            target.repo_root,
            self.home / ".local" / "share" / "personal-dotfiles" / "repo",
        )
        self.assertEqual(target.compat_link, self.home / ".dotfiles")

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
        self.assertEqual(target.repo_root,
                         custom / "personal-dotfiles" / "repo")

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
