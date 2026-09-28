"""Unit tests for installer.manifest: schema validation and resolution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import manifest as mf  # noqa: E402
from installer.platform import Target  # noqa: E402


def make_target(home: Path, **overrides) -> Target:
    values = dict(
        uid=os.getuid(),
        gid=os.getgid(),
        username="fixture-user",
        home=home,
        data_home=home / ".local" / "share",
        state_home=home / ".local" / "state",
        config_home=home / ".config",
        cache_home=home / ".cache",
    )
    values.update(overrides)
    return Target(**values)


def manifest_bytes(entries, **top) -> bytes:
    payload = {"schema": 1, "entries": entries}
    payload.update(top)
    return json.dumps(payload).encode()


class ManifestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-manifest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home dir"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        (self.repo / "zsh").mkdir(parents=True)
        (self.repo / "zsh" / "zshrc").write_text("# zshrc\n")
        (self.repo / "tmux").mkdir()
        (self.repo / "tmux" / "tmux.conf").write_text("set -g x\n")
        (self.repo / "systemd").mkdir()
        (self.repo / "systemd" / "tmux.service").write_text("[Unit]\n")
        (self.repo / "nvim").mkdir()
        self.target = make_target(self.home)

    def resolve(self, entries, **top):
        manifest = mf.parse_manifest(manifest_bytes(entries, **top))
        return mf.resolve(manifest, self.target, self.repo)

    def assertRejected(self, entries, fragment="", **top):
        with self.assertRaises(mf.ManifestError) as ctx:
            self.resolve(entries, **top)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))


class ValidManifestTests(ManifestTestCase):
    def test_resolves_all_kinds(self):
        resolved = self.resolve(
            [
                {"id": "zshrc", "dest": "{home}/.zshrc", "kind": "symlink", "source": "zsh/zshrc"},
                {"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"},
                {
                    "id": "tmux-service",
                    "dest": "{config}/systemd/user/tmux.service",
                    "kind": "copy",
                    "source": "systemd/tmux.service",
                    "mode": "0600",
                    "condition": "systemd-user",
                },
                {
                    "id": "tmux-service-wants",
                    "dest": "{config}/systemd/user/default.target.wants/tmux.service",
                    "kind": "link",
                    "link_to": "{config}/systemd/user/tmux.service",
                    "condition": "systemd-user",
                },
                {"id": "old-rc", "dest": "{home}/.oldrc", "kind": "remove"},
            ]
        )
        by_id = {e.id: e for e in resolved}
        self.assertEqual(self.target.repo_root, self.home / ".dotfiles")
        self.assertEqual(
            by_id["zshrc"].link_text, str(self.home / ".dotfiles" / "zsh" / "zshrc")
        )
        self.assertEqual(by_id["zshrc"].source, self.repo / "zsh" / "zshrc")
        self.assertEqual(by_id["nvim"].dest, self.home / ".config" / "nvim")
        self.assertEqual(by_id["tmux-service"].mode, 0o600)
        self.assertEqual(by_id["tmux-service"].condition, "systemd-user")
        self.assertEqual(
            by_id["tmux-service-wants"].link_text,
            str(self.home / ".config" / "systemd" / "user" / "tmux.service"),
        )
        self.assertEqual(by_id["old-rc"].kind, "remove")
        self.assertIsNone(by_id["old-rc"].source)
        self.assertEqual(by_id["zshrc"].condition, "always")

    def test_copy_default_mode(self):
        resolved = self.resolve(
            [{"id": "svc", "dest": "{config}/x/tmux.service", "kind": "copy", "source": "systemd/tmux.service"}]
        )
        self.assertEqual(resolved[0].mode, 0o644)

    def test_load_manifest_and_sha(self):
        path = self.tmp / "m.json"
        data = manifest_bytes([{"id": "zshrc", "dest": "{home}/.zshrc", "kind": "symlink", "source": "zsh/zshrc"}])
        path.write_bytes(data)
        manifest = mf.load_manifest(path)
        self.assertEqual(len(manifest.entries), 1)
        import hashlib

        self.assertEqual(manifest.sha256, hashlib.sha256(data).hexdigest())

    def test_unicode_and_space_paths(self):
        (self.repo / "zsh" / "파일 name").write_text("x")
        resolved = self.resolve(
            [{"id": "u", "dest": "{home}/설정 file", "kind": "symlink", "source": "zsh/파일 name"}]
        )
        self.assertEqual(resolved[0].dest, self.home / "설정 file")

    def test_repository_manifest_if_present(self):
        path = REPO_ROOT / "manifests" / "managed-paths.json"
        if not path.exists():
            self.skipTest("managed-paths.json not written yet")
        manifest = mf.load_manifest(path)
        submodules = [
            line.split("=", 1)[1].strip()
            for line in (REPO_ROOT / ".gitmodules").read_text().splitlines()
            if line.strip().startswith("path")
        ]
        empty = [p for p in submodules if not any((REPO_ROOT / p).iterdir())]
        try:
            mf.resolve(manifest, self.target, REPO_ROOT)
        except mf.ManifestError as exc:
            if empty and any(f"'{p}/" in str(exc) for p in empty):
                self.skipTest(f"submodules not checked out here: {exc}")
            raise

    def test_rewritten_configs_are_copies(self):
        """Configs that git or their programs rewrite must stay ``copy``.

        Only copies get the transaction's keep-local-edits rule, and a symlink
        would let ``git config --global`` or the program write into the
        checkout instead.
        """

        manifest = mf.load_manifest(REPO_ROOT / "manifests" / "managed-paths.json")
        kinds = {e.id: e.kind for e in manifest.entries}
        for entry_id in ("gitconfig", "pudb"):
            with self.subTest(entry_id=entry_id):
                self.assertEqual(kinds.get(entry_id), "copy")
        for entry in manifest.entries:
            if entry.dest.startswith("{config}/systemd/user/") and entry.source:
                with self.subTest(entry_id=entry.id):
                    self.assertEqual(entry.kind, "copy")


class RejectionTests(ManifestTestCase):
    def test_malformed_json(self):
        with self.assertRaises(mf.ManifestError):
            mf.parse_manifest(b"{not json")

    def test_wrong_schema(self):
        for schema in (2, "1", True, None):
            with self.subTest(schema=schema):
                with self.assertRaises(mf.ManifestError):
                    mf.parse_manifest(json.dumps({"schema": schema, "entries": []}).encode())

    def test_entries_not_list_or_empty(self):
        for entries in ({}, [], "x", None):
            with self.subTest(entries=entries):
                with self.assertRaises(mf.ManifestError):
                    mf.parse_manifest(json.dumps({"schema": 1, "entries": entries}).encode())

    def test_top_level_not_object_or_unknown_key(self):
        with self.assertRaises(mf.ManifestError):
            mf.parse_manifest(b"[]")
        with self.assertRaises(mf.ManifestError):
            mf.parse_manifest(manifest_bytes([{"id": "a", "dest": "{home}/.a", "kind": "remove"}], extra=1))

    def test_unknown_entry_key(self):
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "remove", "sorce": "x"}], "unknown keys"
        )

    def test_entry_not_object(self):
        self.assertRejected(["zshrc"])

    def test_bad_ids(self):
        for bad in ("", "Zsh", "-a", "a_b", "a b", 3, None):
            with self.subTest(id=bad):
                self.assertRejected([{"id": bad, "dest": "{home}/.a", "kind": "remove"}])

    def test_unknown_kind(self):
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "hardlink", "source": "zsh/zshrc"}], "unknown kind"
        )

    def test_unknown_condition(self):
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "remove", "condition": "sometimes"}]
        )

    def test_duplicate_id(self):
        self.assertRejected(
            [
                {"id": "a", "dest": "{home}/.a", "kind": "remove"},
                {"id": "a", "dest": "{home}/.b", "kind": "remove"},
            ],
            "duplicate entry id",
        )

    def test_duplicate_dest(self):
        self.assertRejected(
            [
                {"id": "a", "dest": "{home}/.a", "kind": "remove"},
                {"id": "b", "dest": "{home}/.a", "kind": "remove"},
            ],
            "duplicate dest",
        )

    def test_duplicate_dest_via_different_tokens(self):
        self.assertRejected(
            [
                {"id": "a", "dest": "{config}/nvim", "kind": "remove"},
                {"id": "b", "dest": "{home}/.config/nvim", "kind": "remove"},
            ],
            "overlapping",
        )

    def test_overlapping_dests(self):
        self.assertRejected(
            [
                {"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"},
                {"id": "nvim-init", "dest": "{config}/nvim/init.lua", "kind": "symlink", "source": "zsh/zshrc"},
            ],
            "overlapping",
        )

    def test_missing_source(self):
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": "zsh/missing"}],
            "does not exist",
        )

    def test_missing_source_field(self):
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "symlink"}])
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "link"}])

    def test_source_traversal(self):
        for source in ("../outside", "zsh/../../x", "/etc/passwd", "./zsh/zshrc", "zsh//zshrc", "zsh/"):
            with self.subTest(source=source):
                self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": source}])

    def test_dot_source_reserved(self):
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": "."}])

    def test_source_symlink_escaping_repo(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x")
        os.symlink(outside, self.repo / "zsh" / "escape")
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": "zsh/escape"}],
            "outside the repository",
        )

    def test_copy_source_must_be_regular_file(self):
        os.symlink("zshrc", self.repo / "zsh" / "alias")
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "copy", "source": "zsh/alias"}])
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "copy", "source": "nvim"}])

    def test_dest_traversal_and_shape(self):
        bad = [
            "{home}/../other/.zshrc",
            "{home}/a/../../x",
            "{home}/./x",
            "{home}/x/",
            "{home}//x",
            "{home}",
            "{home}/",
            "/abs/path",
            "~/.zshrc",
            "{nowhere}/x",
            "{home}x",
        ]
        for dest in bad:
            with self.subTest(dest=dest):
                self.assertRejected([{"id": "a", "dest": dest, "kind": "remove"}])

    def test_broad_roots_lexical(self):
        for dest in ("{home}/.config", "{home}/.local", "{home}/.local/share", "{home}/.cache", "{config}/systemd", "{config}/systemd/user"):
            with self.subTest(dest=dest):
                self.assertRejected([{"id": "a", "dest": dest, "kind": "remove"}], "broad")

    def test_broad_roots_after_resolution(self):
        for dest in ("{home}/.config/systemd", "{home}/.local/share/applications", "{home}/.ssh"):
            with self.subTest(dest=dest):
                self.assertRejected([{"id": "a", "dest": dest, "kind": "remove"}])

    def test_custom_xdg_root_cannot_be_replaced(self):
        self.target = make_target(self.home, config_home=self.home / "cfg")
        self.assertRejected([{"id": "a", "dest": "{home}/cfg", "kind": "remove"}], "broad")

    def test_installer_roots_rejected(self):
        for dest in (
            "{data}/personal-dotfiles",
            "{data}/personal-dotfiles/repo",
            "{data}/personal-dotfiles/repo/zsh",
            "{state}/personal-dotfiles/state.json",
        ):
            with self.subTest(dest=dest):
                self.assertRejected([{"id": "a", "dest": dest, "kind": "remove"}])

    def test_symlinked_parent_escaping_home(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        os.symlink(outside, self.home / ".config")
        self.assertRejected(
            [{"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"}],
            "symlinked parent",
        )
        # The escaping directory was not touched.
        self.assertEqual(list(outside.iterdir()), [])

    def test_dotfiles_itself_is_not_a_managed_entry(self):
        # ~/.dotfiles is the checkout (target.repo_root), never a manifest entry.
        self.assertRejected(
            [{"id": "dotfiles-compat", "dest": "{home}/.dotfiles", "kind": "symlink", "source": "."}],
            "installer-owned",
        )

    def test_symlinked_parent_into_repository(self):
        self.target.repo_root.mkdir(parents=True)
        (self.target.repo_root / "cfg").mkdir()
        os.symlink(self.home / ".dotfiles" / "cfg", self.home / ".config")
        self.assertRejected(
            [{"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"}],
            "symlinked parent",
        )

    def test_dangling_symlinked_parent(self):
        os.symlink(self.tmp / "not-yet", self.home / ".config")
        self.assertRejected(
            [{"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"}]
        )

    def test_symlinked_parent_inside_home_is_allowed(self):
        (self.home / "real-config").mkdir()
        os.symlink(self.home / "real-config", self.home / ".config")
        resolved = self.resolve(
            [{"id": "nvim", "dest": "{config}/nvim", "kind": "symlink", "source": "nvim"}]
        )
        self.assertEqual(resolved[0].dest, self.home / ".config" / "nvim")

    def test_mode_validation(self):
        for mode in ("644", "0644"):
            with self.subTest(mode=mode):
                resolved = self.resolve(
                    [{"id": "a", "dest": "{home}/.a", "kind": "copy", "source": "zsh/zshrc", "mode": mode}]
                )
                self.assertEqual(resolved[0].mode, 0o644)
        for mode in ("0888", "4755", 644, "rw-r--r--"):
            with self.subTest(mode=mode):
                self.assertRejected(
                    [{"id": "a", "dest": "{home}/.a", "kind": "copy", "source": "zsh/zshrc", "mode": mode}]
                )
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": "zsh/zshrc", "mode": "0644"}]
        )

    def test_kind_field_constraints(self):
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "remove", "source": "zsh/zshrc"}])
        self.assertRejected(
            [{"id": "a", "dest": "{home}/.a", "kind": "symlink", "source": "zsh/zshrc", "link_to": "{home}/.b"}]
        )
        self.assertRejected([{"id": "a", "dest": "{home}/.a", "kind": "link", "link_to": "{home}/../x"}])

    def test_compat_entry_shape(self):
        self.assertRejected([{"id": "dotfiles-compat", "dest": "{home}/.dots", "kind": "symlink", "source": "."}])
        self.assertRejected([{"id": "dotfiles-compat", "dest": "{home}/.dotfiles", "kind": "symlink", "source": "zsh"}])
        self.assertRejected([{"id": "dotfiles-compat", "dest": "{home}/.dotfiles", "kind": "remove"}])

    def test_missing_repo(self):
        manifest = mf.parse_manifest(manifest_bytes([{"id": "a", "dest": "{home}/.a", "kind": "remove"}]))
        with self.assertRaises(mf.ManifestError):
            mf.resolve(manifest, self.target, self.tmp / "no-repo")


if __name__ == "__main__":
    unittest.main()
