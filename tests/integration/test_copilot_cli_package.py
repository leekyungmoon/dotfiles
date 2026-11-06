"""Opt-in real Copilot CLI package install in a disposable HOME, without login.

Set DOTFILES_COPILOT_CLI_NETWORK_TEST=1. Optionally set
DOTFILES_COPILOT_CLI_ARCHIVE to a previously downloaded official archive;
the normal installer still validates its pinned digest before extracting.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from installer import packages, platform
from installer.runner import Runner


@unittest.skipUnless(os.environ.get("DOTFILES_COPILOT_CLI_NETWORK_TEST") == "1",
                     "opt in to the official Copilot CLI package smoke test")
class CopilotCliPackageTests(unittest.TestCase):
    def test_verified_extract_version_links_and_repeat_install(self):
        host = platform.detect_platform()
        platform.require_supported_platform(host)
        with tempfile.TemporaryDirectory(prefix="dotfiles-copilot-cli-package-") as tmp:
            home = Path(tmp)
            target = platform.Target(os.getuid(), os.getgid(), "fixture", home,
                                     home / "data", home / "state", home / "config", home / "cache")
            archive = os.environ.get("DOTFILES_COPILOT_CLI_ARCHIVE")

            def cached_fetch(url, dest, *, timeout):
                shutil.copyfile(archive, dest)
                return dest

            result = packages.run_packages_phase(
                target, host, Runner(), only=["copilot-cli"],
                fetch=cached_fetch if archive else None, log=lambda _: None)
            self.assertEqual(result["status"], "PASS", result["reasons"])
            self.assertEqual(result["details"]["tools_selected"], ["copilot-cli"])
            self.assertNotIn("apt", result["details"])
            binary = target.data_home / "personal-dotfiles/bin/copilot"
            self.assertTrue(binary.is_symlink())
            self.assertTrue(os.access(binary, os.X_OK))
            requests = packages.link_requests(result["details"])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].dest, target.home / ".local/bin/copilot")
            self.assertEqual(requests[0].link_text, str(binary))
            self.assertTrue(result["details"]["tools"]["copilot-cli"]["verify"][0]
                            .startswith("GitHub Copilot CLI 1.0.106."))

            def no_download(*args, **kwargs):
                self.fail("a verified second install should reuse the existing prefix")

            again = packages.run_packages_phase(
                target, host, Runner(), only=["copilot-cli"], fetch=no_download, log=lambda _: None)
            self.assertEqual(again["status"], "PASS", again["reasons"])
            self.assertEqual(binary.resolve(), Path(requests[0].link_text).resolve())


if __name__ == "__main__":
    unittest.main()
