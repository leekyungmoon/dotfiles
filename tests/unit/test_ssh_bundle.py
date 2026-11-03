"""Tests for the source-only offline ssh-dotfiles payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer.ssh_bundle import BundleError, build_bundle  # noqa: E402


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def repository(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ssh bundle space "))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "source repo"
        repository(self.repo, {
            "zsh/plugins.zsh": (
                "plugin 'owner/plugin'\n"
                "if [[ `uname` == Darwin ]]; then\n"
                "  plugin 'owner/darwin-plugin'\n"
                "fi\n"
            ),
            "zsh/zshrc": "# fixture\n",
            "zsh/machine.local": "do not deploy\n",
            "zsh/zsh.d/alias.local.zsh": "do not deploy\n",
            "zsh/tests/unit.zsh": "do not deploy\n",
            "bin/helper": "#!/bin/sh\n",
            "bin/ssh-dotfiles": "#!/usr/bin/env python3\n",
            "config/f-sy-h/theme.ini": "x=1\n",
            "secret.txt": "outside allowed roots\n",
        })
        for name in ("antidote", "fasd"):
            sub = self.tmp / f"{name} source"
            repository(sub, {"tracked": name, "nested/kept": "yes"})
            subprocess.run([
                "git", "-c", "protocol.file.allow=always", "-C", str(self.repo),
                "submodule", "add", "-q", str(sub), f"zsh/{name}",
            ], check=True)
        git(self.repo, "commit", "-qam", "submodules")
        self.plugins = self.tmp / "plugin root"
        self.plugin = self.plugins / "owner" / "plugin"
        repository(self.plugin, {"plugin.zsh": "# plugin\n", "dir/nested": "tracked\n"})
        self.output = self.tmp / "payload.tar.gz"

    def names(self) -> set[str]:
        with tarfile.open(self.output, "r:gz") as archive:
            return set(archive.getnames())

    def test_contains_tracked_sources_plugins_and_nested_submodule_files(self):
        (self.repo / "zsh" / "private-token").write_text("never include")
        (self.plugin / "private-token").write_text("never include")
        nested_source = self.tmp / "nested plugin source"
        repository(nested_source, {"nested-plugin.zsh": "# nested plugin\n"})
        subprocess.run([
            "git", "-c", "protocol.file.allow=always", "-C", str(self.plugin),
            "submodule", "add", "-q", str(nested_source), "vendor/nested",
        ], check=True)
        git(self.plugin, "commit", "-qam", "nested plugin")

        build_bundle(self.repo, self.plugins, self.output)

        names = self.names()
        self.assertIn("src/zsh/zshrc", names)
        self.assertIn("src/zsh/antidote/nested/kept", names)
        self.assertIn("src/zsh/fasd/tracked", names)
        self.assertIn("src/bin/helper", names)
        self.assertNotIn("src/bin/ssh-dotfiles", names)
        self.assertIn("plugins/owner/plugin/dir/nested", names)
        self.assertIn(
            "plugins/owner/plugin/vendor/nested/nested-plugin.zsh", names
        )
        self.assertIn("bundle.json", names)
        self.assertNotIn("src/secret.txt", names)
        self.assertNotIn("src/zsh/machine.local", names)
        self.assertNotIn("src/zsh/zsh.d/alias.local.zsh", names)
        self.assertNotIn("src/zsh/tests/unit.zsh", names)
        self.assertFalse(any("private-token" in name for name in names))
        self.assertFalse(any(".git" in Path(name).parts for name in names))
        with tarfile.open(self.output, "r:gz") as archive:
            metadata = json.load(archive.extractfile("bundle.json"))
        self.assertEqual(metadata, {
            "schema": 1,
            "plugins": ["owner/plugin"],
            "required_plugins": ["owner/plugin"],
            "platform_plugins": {"Darwin": ["owner/darwin-plugin"]},
        })

    def test_available_platform_plugin_is_included(self):
        darwin = self.plugins / "owner" / "darwin-plugin"
        repository(darwin, {"anybar.plugin.zsh": "# optional platform plugin\n"})
        build_bundle(self.repo, self.plugins, self.output)
        self.assertIn(
            "plugins/owner/darwin-plugin/anybar.plugin.zsh", self.names()
        )
        with tarfile.open(self.output, "r:gz") as archive:
            metadata = json.load(archive.extractfile("bundle.json"))
        self.assertEqual(
            metadata["plugins"], ["owner/plugin", "owner/darwin-plugin"]
        )

    def test_missing_required_plugin_fails_without_replacing_output(self):
        self.output.write_bytes(b"previous")
        shutil.rmtree(self.plugin)
        with self.assertRaisesRegex(BundleError, "required offline plugin"):
            build_bundle(self.repo, self.plugins, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_missing_required_submodule_fails(self):
        shutil.rmtree(self.repo / "zsh" / "fasd")
        with self.assertRaisesRegex(BundleError, "not initialized"):
            build_bundle(self.repo, self.plugins, self.output)

    def test_safe_relative_symlink_is_preserved(self):
        link = self.plugin / "linked.zsh"
        link.symlink_to("plugin.zsh")
        git(self.plugin, "add", "linked.zsh")
        git(self.plugin, "commit", "-qm", "link")
        build_bundle(self.repo, self.plugins, self.output)
        with tarfile.open(self.output, "r:gz") as archive:
            member = archive.getmember("plugins/owner/plugin/linked.zsh")
        self.assertTrue(member.issym())
        self.assertEqual(member.linkname, "plugin.zsh")

    def test_uninitialized_nested_plugin_submodule_is_rejected(self):
        nested_source = self.tmp / "nested dependency source"
        repository(nested_source, {"dependency.zsh": "# dependency\n"})
        subprocess.run([
            "git", "-c", "protocol.file.allow=always", "-C", str(self.plugin),
            "submodule", "add", "-q", str(nested_source), "vendor/dependency",
        ], check=True)
        git(self.plugin, "commit", "-qam", "dependency")
        shutil.rmtree(self.plugin / "vendor" / "dependency")

        with self.assertRaisesRegex(BundleError, "tracked file is missing"):
            build_bundle(self.repo, self.plugins, self.output)

    def test_native_executables_are_rejected_instead_of_copied(self):
        native = self.repo / "bin" / "native-helper"
        fixtures = {
            "ELF": b"\x7fELFfixture",
            "Mach-O": b"\xcf\xfa\xed\xfefixture",
            "Mach-O fat": b"\xca\xfe\xba\xbffixture",
            "PE": b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0",
        }
        for label, contents in fixtures.items():
            with self.subTest(label=label):
                native.write_bytes(contents)
                git(self.repo, "add", "bin/native-helper")
                git(self.repo, "commit", "-qm", f"{label} fixture")
                with self.assertRaisesRegex(BundleError, "portable payload"):
                    build_bundle(self.repo, self.plugins, self.output)

    def test_absolute_and_escaping_symlinks_are_rejected(self):
        for target in ("/etc/passwd", "../../../../outside"):
            with self.subTest(target=target):
                link = self.plugin / "bad-link"
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(target)
                git(self.plugin, "add", "bad-link")
                git(self.plugin, "commit", "-qm", "bad link")
                with self.assertRaisesRegex(BundleError, "symlink"):
                    build_bundle(self.repo, self.plugins, self.output)


if __name__ == "__main__":
    unittest.main()
