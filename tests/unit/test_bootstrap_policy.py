"""etc/install and install.py admit exactly the same systems.

docs/SUPPORT.md promises that an unsupported system is refused before any
change: by etc/install first (the one-liner refuses before it clones
anything) and then by install.py. Each keeps its own copy of the support
policy (etc/install is plain bash and runs before any python of the
checkout exists), so the copies must agree: the macOS major releases on
Apple silicon and on Intel, Homebrew's default prefix on each, and the
Ubuntu releases and architectures.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import platform as plat  # noqa: E402

ETC_INSTALL = REPO_ROOT / "etc" / "install"


class BootstrapPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ETC_INSTALL.read_text(encoding="utf-8")

    def constant(self, name: str) -> str:
        """The value of etc/install's NAME=value, a constant: assigned once,
        at the top level, to a literal (no expansion)."""
        self.assertEqual(len(re.findall(rf"(?<![\w$]){name}=", self.text)), 1,
                         f"{name} must be assigned exactly once")
        found = re.findall(rf'^{name}=(?:"([^"$`\\]*)"|([^\s"$`\\;]+))$', self.text, re.M)
        self.assertEqual(len(found), 1, f"{name}=<literal> at the top level")
        quoted, bare = found[0]
        return quoted or bare

    def test_apple_silicon_releases_match_the_installer(self):
        self.assertEqual(tuple(self.constant("_MACOS_ARM64_RELEASES").split()),
                         plat.SUPPORTED_MACOS_RELEASES)

    def test_intel_releases_match_the_installer(self):
        self.assertEqual(tuple(self.constant("_MACOS_INTEL_RELEASES").split()),
                         plat.MACOS_INTEL_RELEASES)

    def test_homebrew_default_prefixes_match_the_installer(self):
        self.assertEqual(self.constant("_HOMEBREW_ARM64_PREFIX"), plat.HOMEBREW_PREFIXES["arm64"])
        self.assertEqual(self.constant("_HOMEBREW_INTEL_PREFIX"), plat.HOMEBREW_PREFIXES["amd64"])

    def test_the_macos_lists_decide_per_architecture(self):
        # uname -m names the architecture there: arm64 or x86_64 (amd64).
        found = re.findall(r'^\s*(arm64|x86_64)\) releases="\$(\w+)" prefix="\$(\w+)" ;;$',
                           self.text, re.M)
        arms = {arch: (releases, prefix) for arch, releases, prefix in found}
        self.assertEqual(len(found), len(arms))
        self.assertEqual(arms, {
            "arm64": ("_MACOS_ARM64_RELEASES", "_HOMEBREW_ARM64_PREFIX"),
            "x86_64": ("_MACOS_INTEL_RELEASES", "_HOMEBREW_INTEL_PREFIX"),
        })
        self.assertEqual({plat.normalize_architecture(arch) for arch in arms},
                         set(plat.SUPPORTED_MACOS_ARCHITECTURES))

    def test_ubuntu_releases_and_architectures_match_the_installer(self):
        releases = re.findall(r'case "\$os_version" in\n\s*([0-9. |]+)\) ;;', self.text)
        self.assertEqual(len(releases), 1)
        self.assertEqual(tuple(r.strip() for r in releases[0].split("|")),
                         plat.SUPPORTED_RELEASES)
        arches = re.findall(r'case "\$arch" in\n\s*([a-z0-9_ |]+)\) ;;', self.text)
        self.assertEqual(len(arches), 1)
        self.assertEqual({plat.normalize_architecture(a) for a in arches[0].split("|")},
                         set(plat.SUPPORTED_ARCHITECTURES))


if __name__ == "__main__":
    unittest.main()
