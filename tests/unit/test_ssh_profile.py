"""SSH argument boundaries and transactional receiver failures."""

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer import ssh_profile as client, ssh_remote as remote


def archive(entries):
    entries = {**entries, "bundle.json": '{"schema":1}'}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as out:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type = tarfile.SYMTYPE
                member.linkname = value[0]
                out.addfile(member)
            else:
                data = value.encode()
                member.size = len(data)
                out.addfile(member, io.BytesIO(data))
    stream.seek(0)
    return stream


class ClientTests(unittest.TestCase):
    def test_connect_passes_one_quoted_remote_command_and_no_install(self):
        with patch.object(client.subprocess, "call", return_value=7) as call, \
                patch.object(client, "build_bundle") as bundle:
            self.assertEqual(client.main(["connect", "a-host", "--profile", "alice"]), 7)
        args = call.call_args.args[0]
        self.assertEqual(args[:2], ["ssh", "-t"])
        self.assertEqual(args[-2], "a-host")
        self.assertIn('"$HOME/.local/share/ssh-dotfiles/profiles/alice/shell"', args[-1])
        self.assertIn("RemoteCommand=none", args)
        bundle.assert_not_called()

    def test_option_injection_and_unsafe_profile_are_refused(self):
        for args in (["connect", "--", "-oProxyCommand=evil"],
                     ["connect", "host", "--profile", "../escape"],
                     ["install", "host", "--profile", "a;touch /tmp/x"],
                     ["connect", "host\nother"]):
            with self.subTest(args=args), self.assertRaises(SystemExit), \
                    patch.object(client.subprocess, "call") as call:
                client.main(args)
            call.assert_not_called()

    def test_missing_bundle_never_connects(self):
        with patch.object(client, "plugin_directory", side_effect=ValueError("missing plugins")), \
                patch.object(client.subprocess, "call") as call:
            self.assertEqual(client.main(["install", "host"]), 1)
        call.assert_not_called()

    def test_plugin_lookup_handles_legacy_and_xdg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "repo/zsh/antidote-plugins"
            legacy.mkdir(parents=True)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}):
                self.assertEqual(client.plugin_directory(root / "repo"), legacy)
                current = root / "data/antidote"
                current.mkdir(parents=True)
                self.assertEqual(client.plugin_directory(root / "repo"), current)

    def test_normal_install_exposes_command(self):
        entries = json.loads((ROOT / "manifests/managed-paths.json").read_text())["entries"]
        entry = next(e for e in entries if e["id"] == "bin-ssh-dotfiles")
        self.assertEqual(entry["dest"], "{home}/.local/bin/ssh-dotfiles")
        self.assertEqual(entry["source"], "bin/ssh-dotfiles")
        self.assertTrue(os.access(ROOT / entry["source"], os.X_OK))


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssh remote space ")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = self.home / ".local/share/ssh-dotfiles/profiles/alice"
        self.sentinels = (".bashrc", ".zshrc", ".zshenv", ".gitconfig", ".tmux.conf")
        for name in self.sentinels:
            (self.home / name).write_text("unchanged\n")

    def install(self, entries=None):
        stream = archive(entries or {"src/zsh/zshrc": "payload"})
        data = stream.getvalue()
        remote.install(stream, "alice", len(data), hashlib.sha256(data).hexdigest())

    def test_publish_reinstall_and_failure_preserve_previous_release(self):
        with patch.object(remote, "prerequisites"), patch.object(remote, "prepare"):
            self.install()
            first = (self.profile / "current").resolve()
            self.install()
            second = (self.profile / "current").resolve()
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())  # shells using it keep working
        with patch.object(remote, "prerequisites"), \
                patch.object(remote, "prepare", side_effect=ValueError("failed smoke")):
            with self.assertRaisesRegex(ValueError, "failed smoke"):
                self.install()
        self.assertEqual((self.profile / "current").resolve(), second)
        self.assertEqual(len(list((self.profile / "releases").iterdir())), 2)
        for name in self.sentinels:
            self.assertEqual((self.home / name).read_text(), "unchanged\n")

    def test_missing_prerequisite_leaves_no_install_files(self):
        with patch.object(remote, "prerequisites", side_effect=ValueError("missing zsh")):
            with self.assertRaisesRegex(ValueError, "missing zsh"):
                self.install()
        self.assertFalse((self.home / ".local").exists())

    def test_concurrent_install_is_refused(self):
        with patch.object(remote, "prerequisites"), patch.object(remote, "prepare"):
            self.install()
            with (self.profile.parent / ".alice.lock").open("r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "in progress"):
                    self.install()

    def test_unmanaged_or_symlink_profile_is_preserved(self):
        self.profile.mkdir(parents=True)
        sentinel = self.profile / "user-file"
        sentinel.write_text("keep")
        with patch.object(remote, "prerequisites"):
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                self.install()
        self.assertEqual(sentinel.read_text(), "keep")
        shutil.rmtree(self.profile)
        self.profile.symlink_to(self.home)
        with patch.object(remote, "prerequisites"), self.assertRaisesRegex(ValueError, "occupied"):
            self.install()

    def test_customized_launcher_is_not_overwritten(self):
        with patch.object(remote, "prerequisites"), patch.object(remote, "prepare"):
            self.install()
            (self.profile / "shell").write_text("my launcher")
            with self.assertRaisesRegex(ValueError, "local changes"):
                self.install()
        self.assertEqual((self.profile / "shell").read_text(), "my launcher")

    def test_launcher_version_changes_do_not_block_reinstall(self):
        with patch.object(remote, "prerequisites"), patch.object(remote, "prepare"):
            self.install()
            with patch.object(remote, "LAUNCHER", "#!/bin/sh\necho new-version\n"):
                self.install()
        self.assertEqual((self.profile / "shell").read_text(), remote.ENTRYPOINT)

    def test_incomplete_or_corrupt_transfer_does_not_activate(self):
        with patch.object(remote, "prerequisites"), patch.object(remote, "prepare") as prepare:
            self.install()
            current = (self.profile / "current").resolve()
            data = archive({"src/zsh/zshrc": "complete-source"}).getvalue()
            prepare.reset_mock()
            for broken in (data[:-8], data[:30], data + b"extra", data[:-1] + b"x"):
                with self.subTest(size=len(broken)), self.assertRaises(ValueError):
                    remote.install(io.BytesIO(broken), "alice", len(data),
                                   hashlib.sha256(data).hexdigest())
            prepare.assert_not_called()
        self.assertEqual((self.profile / "current").resolve(), current)

    def test_unsafe_archive_entries_are_rejected(self):
        examples = ({"../escape": "bad"}, {"/tmp/escape": "bad"},
                    {"src/link": ("/etc/passwd",)},
                    {"src/link": ("../../escape",)},
                    {"src/link": ("../plugins/next",), "plugins/next": ("../../escape",)})
        for i, entries in enumerate(examples):
            release = self.home / str(i)
            release.mkdir()
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                remote.extract(archive(entries), release)

    def test_environment_keeps_real_home_and_isolates_state(self):
        release = self.profile / "releases/one"
        env = remote.environment(self.profile, release)
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(env["ZDOTDIR"], str(release / "zdot"))
        self.assertEqual(env["XDG_CACHE_HOME"], str(release / "cache"))
        self.assertEqual(env["XDG_DATA_HOME"], str(self.profile / "data"))


if __name__ == "__main__":
    unittest.main()
