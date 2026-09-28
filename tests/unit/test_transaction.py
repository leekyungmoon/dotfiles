"""Unit tests for installer.transaction: snapshots, backups, rollback, restore.

Every test works in a throwaway home under a temp directory with a Target
built directly; nothing touches the real user environment.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import transaction as tx  # noqa: E402
from installer.manifest import ResolvedEntry  # noqa: E402
from installer.platform import Target  # noqa: E402
from installer.transaction import (  # noqa: E402
    ABSENT,
    ConcurrentRunError,
    DesiredEntry,
    ObjectState,
    Transaction,
    TransactionError,
    snapshot,
)

GEN = {"commit": "c0ffee", "manifest_sha256": "0" * 64}


def setUpModule():
    # Durability is not what these tests measure; fsync makes the failure
    # matrices slow. DurabilityTests turns it back on for one full run.
    tx.FSYNC = False


def tearDownModule():
    tx.FSYNC = True


class Injected(Exception):
    pass


class Crash(Exception):
    """A simulated process death: the transaction must not roll back."""

    skip_rollback = True


class FaultAt:
    def __init__(self, name: str, nth: int = 1, exc: type = Injected, action=None):
        self.name = name
        self.nth = nth
        self.exc = exc
        self.action = action
        self.count = 0

    def __call__(self, step: str) -> None:
        if step != self.name:
            return
        self.count += 1
        if self.count == self.nth:
            if self.action is not None:
                self.action()
                return
            raise self.exc(step)


class Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def __call__(self, step: str) -> None:
        self.seen.append(step)


def make_target(home: Path) -> Target:
    return Target(
        uid=os.getuid(),
        gid=os.getgid(),
        username="fixture-user",
        home=home,
        data_home=home / ".local" / "share",
        state_home=home / ".local" / "state",
        config_home=home / ".config",
        cache_home=home / ".cache",
    )


def home_state(target: Target) -> dict:
    """Every object under home except the installer's own state root."""

    result: dict = {}

    def walk(path: Path, rel: str) -> None:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            child = Path(entry.path)
            if child == target.state_root:
                continue
            if target.state_root.is_relative_to(child) and not os.path.islink(child):
                walk(child, rel + entry.name + "/")  # installer-created parents
                continue
            key = rel + entry.name
            st = os.lstat(child)
            if stat.S_ISDIR(st.st_mode):
                result[key] = ("dir", stat.S_IMODE(st.st_mode))
                walk(child, key + "/")
            else:
                result[key] = snapshot(child)

    walk(target.home, "")
    return result


def run_apply(target, desired, *, fault=None, generation=GEN):
    with Transaction(target, tx.new_run_id(), fault=fault) as t:
        return t.apply(desired, generation=generation)


def journal(target, run_id) -> dict:
    return json.loads((target.state_root / "journal" / f"{run_id}.json").read_text())


def only_journal(target) -> dict:
    paths = sorted((target.state_root / "journal").glob("*.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text())


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-tx-"))
        self.addCleanup(self._cleanup)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.target = make_target(self.home)
        self.repo_link = self.home / ".dotfiles"

    def _cleanup(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for name in dirnames:
                path = os.path.join(dirpath, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def link(self, rel: str) -> str:
        return str(self.repo_link / rel)

    def baseline_bytes(self) -> dict:
        root = self.target.state_root / "backups" / "baseline"
        result = {}
        for folder in sorted(root.iterdir()):
            result[folder.name] = (
                (folder / "meta.json").read_bytes(),
                snapshot(folder / "object"),
            )
        return result

    def populate(self) -> list[DesiredEntry]:
        """A realistic conflict set; returns the desired entries."""

        home = self.home
        (home / ".zshrc").write_text("old zshrc\n")
        os.chmod(home / ".zshrc", 0o640)
        nvim = home / ".config" / "nvim"
        (nvim / "lua" / "쓰기 dir").mkdir(parents=True)
        (nvim / "init.lua").write_text("-- mine\n")
        (nvim / "lua" / "쓰기 dir" / "a b.lua").write_text("x")
        os.chmod(nvim / "lua", 0o750)
        os.symlink("../init.lua", nvim / "lua" / "link")
        os.symlink("/nonexistent/place", home / ".vimrc")
        (home / ".legacyrc").write_text("legacy\n")
        (home / "keep.txt").write_text("sentinel\n")
        (home / ".config" / "keep.conf").write_text("sentinel conf\n")
        return [
            DesiredEntry("zshrc", home / ".zshrc", "symlink", link_text=self.link("zsh/zshrc")),
            DesiredEntry("nvim", nvim, "symlink", link_text=self.link("nvim")),
            DesiredEntry("tmux-conf", home / ".tmux.conf", "symlink", link_text=self.link("tmux/tmux.conf")),
            DesiredEntry(
                "tmux-service",
                home / ".config" / "systemd" / "user" / "tmux.service",
                "file",
                content=b"[Unit]\nDescription=tmux\n",
                mode=0o644,
            ),
            DesiredEntry("vimrc", home / ".vimrc", "symlink", link_text=self.link("vim/vimrc")),
            DesiredEntry("legacy", home / ".legacyrc", "absent"),
        ]


class SnapshotTests(HomeCase):
    def test_absent(self):
        self.assertEqual(snapshot(self.home / "missing"), ABSENT)
        (self.home / "file").write_text("x")
        self.assertEqual(snapshot(self.home / "file" / "child"), ABSENT)

    def test_regular_file_mode_and_hash(self):
        path = self.home / "a file ü"
        path.write_bytes(b"hello")
        os.chmod(path, 0o640)
        state = snapshot(path)
        self.assertEqual(state.kind, "file")
        self.assertEqual(state.mode, 0o640)
        self.assertEqual(
            state.sha256, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )
        os.chmod(path, 0o600)
        self.assertNotEqual(snapshot(path), state)

    def test_symlink_exact_text_not_followed(self):
        target = self.home / "real"
        target.write_text("data")
        os.symlink("./real", self.home / "link")
        state = snapshot(self.home / "link")
        self.assertEqual(state, ObjectState("symlink", link_text="./real"))
        os.symlink(self.home, self.home / "dirlink")
        self.assertEqual(snapshot(self.home / "dirlink").kind, "symlink")

    def test_dangling_symlink(self):
        os.symlink("nowhere/좋아 x", self.home / "dangling")
        self.assertEqual(
            snapshot(self.home / "dangling"), ObjectState("symlink", link_text="nowhere/좋아 x")
        )

    def test_directory_tree_hash(self):
        root = self.home / "tree"
        (root / "sub dir").mkdir(parents=True)
        (root / "sub dir" / "파일").write_text("1")
        os.symlink("sub dir/파일", root / "ln")
        first = snapshot(root)
        self.assertEqual(first.kind, "dir")
        self.assertEqual(snapshot(root), first)
        os.chmod(root / "sub dir" / "파일", 0o600)
        second = snapshot(root)
        self.assertNotEqual(second, first)
        os.unlink(root / "ln")
        os.symlink("sub dir/other", root / "ln")
        self.assertNotEqual(snapshot(root), second)
        (root / "empty").mkdir()
        self.assertNotEqual(snapshot(root), second)

    def test_special_files_refused(self):
        os.mkfifo(self.home / "fifo")
        with self.assertRaises(TransactionError):
            snapshot(self.home / "fifo")

    def test_json_round_trip(self):
        path = self.home / "f"
        path.write_text("x")
        for state in (ABSENT, snapshot(path), ObjectState("symlink", link_text="a b"), snapshot(self.home)):
            self.assertEqual(ObjectState.from_json(json.loads(json.dumps(state.to_json()))), state)
        with self.assertRaises(TransactionError):
            ObjectState.from_json({"kind": "pipe"})

    def test_run_id_format(self):
        from installer.platform import is_generation_id

        self.assertTrue(is_generation_id(tx.new_run_id()))


class InstallTests(HomeCase):
    def test_install_backups_and_permissions(self):
        desired = self.populate()
        before = home_state(self.target)
        result = run_apply(self.target, desired)
        home = self.home
        self.assertEqual(sorted(result.changed), sorted(d.id for d in desired))
        self.assertEqual(os.readlink(home / ".zshrc"), self.link("zsh/zshrc"))
        self.assertEqual(os.readlink(home / ".config" / "nvim"), self.link("nvim"))
        self.assertEqual(os.readlink(home / ".vimrc"), self.link("vim/vimrc"))
        service = home / ".config" / "systemd" / "user" / "tmux.service"
        self.assertEqual(service.read_bytes(), b"[Unit]\nDescription=tmux\n")
        self.assertEqual(stat.S_IMODE(os.lstat(service).st_mode), 0o644)
        self.assertFalse(os.path.lexists(home / ".legacyrc"))
        # Unmanaged neighbours are untouched.
        self.assertEqual((home / "keep.txt").read_text(), "sentinel\n")
        self.assertEqual((home / ".config" / "keep.conf").read_text(), "sentinel conf\n")
        # No temp siblings left behind.
        leftovers = [p for p in home.rglob(".*.pd-*")]
        self.assertEqual(leftovers, [])

        root = self.target.state_root
        for folder in (root, root / "journal", root / "backups", root / "backups" / "baseline",
                       root / "backups" / "runs", result.backup_dir):
            self.assertEqual(stat.S_IMODE(os.lstat(folder).st_mode), 0o700, folder)
        for entry in result.backup_dir.iterdir():
            self.assertEqual(stat.S_IMODE(os.lstat(entry).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.lstat(entry / "meta.json").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(root / "state.json").st_mode), 0o600)

        # Baseline and per-run backups hold the exact prior objects.
        nvim_before = before[".config/nvim"]
        baseline_nvim = root / "backups" / "baseline" / "nvim"
        self.assertTrue((baseline_nvim / "COMPLETE").is_file())
        meta = json.loads((baseline_nvim / "meta.json").read_text())
        self.assertEqual(ObjectState.from_json(meta["state"]).kind, "dir")
        self.assertEqual(snapshot(baseline_nvim / "object"), ObjectState.from_json(meta["state"]))
        self.assertEqual(
            snapshot(root / "backups" / "baseline" / "vimrc" / "object"),
            ObjectState("symlink", link_text="/nonexistent/place"),
        )
        self.assertEqual(snapshot(root / "backups" / "baseline" / "zshrc" / "object"), before[".zshrc"])
        self.assertEqual(nvim_before, ("dir", stat.S_IMODE(os.lstat(baseline_nvim / "object").st_mode)))
        # The baseline for a previously absent target records absence.
        meta = json.loads((root / "backups" / "baseline" / "tmux-conf" / "meta.json").read_text())
        self.assertEqual(meta["state"]["kind"], "absent")

        state = json.loads((root / "state.json").read_text())
        self.assertEqual(state["owner_uid"], os.getuid())
        self.assertEqual(state["generation"], GEN)
        self.assertEqual(state["run_id"], result.run_id)
        self.assertEqual(journal(self.target, result.run_id)["status"], "committed")
        # Nothing in the journal or state carries file contents.
        blob = (root / "state.json").read_text() + json.dumps(journal(self.target, result.run_id))
        self.assertNotIn("old zshrc", blob)
        self.assertNotIn("Description=tmux", blob)

    def test_rerun_is_noop(self):
        desired = self.populate()
        run_apply(self.target, desired)
        after_first = home_state(self.target)
        second = run_apply(self.target, desired)
        self.assertEqual(second.changed, [])
        self.assertEqual(sorted(second.unchanged), sorted(d.id for d in desired))
        self.assertIsNone(second.backup_dir)
        self.assertEqual(home_state(self.target), after_first)

    def test_baseline_immutable_across_three_runs(self):
        desired = self.populate()
        run_apply(self.target, desired)
        baseline = self.baseline_bytes()
        # Run 2: unchanged. Run 3: user edits a target and the desired link moves.
        run_apply(self.target, desired)
        self.assertEqual(self.baseline_bytes(), baseline)
        os.unlink(self.home / ".zshrc")
        (self.home / ".zshrc").write_text("edited after install\n")
        desired3 = [
            DesiredEntry(d.id, d.dest, d.kind, link_text=self.link("zsh/zshrc.v3"))
            if d.id == "zshrc" else d
            for d in desired
        ]
        third = run_apply(self.target, desired3)
        self.assertEqual(third.changed, ["zshrc"])
        self.assertEqual(self.baseline_bytes(), baseline)
        runs = sorted((self.target.state_root / "backups" / "runs").iterdir())
        self.assertEqual(len(runs), 2)  # run 2 changed nothing
        edited = snapshot(third.backup_dir / "zshrc" / "object")
        self.assertEqual(edited.kind, "file")
        self.assertEqual(stat.S_IMODE(os.lstat(third.backup_dir).st_mode), 0o700)

    def test_all_backups_verified_before_first_replacement(self):
        desired = self.populate()
        before = home_state(self.target)
        observed = {}

        def check():
            runs = list((self.target.state_root / "backups" / "runs").iterdir())
            self.assertEqual(len(runs), 1)
            for d in desired:
                meta = json.loads((runs[0] / d.id / "meta.json").read_text())
                state = ObjectState.from_json(meta["state"])
                if state.kind != "absent":
                    folder = "baseline" if meta["payload"] == "baseline" else None
                    payload = (
                        self.target.state_root / "backups" / "baseline" / d.id / "object"
                        if folder else runs[0] / d.id / "object"
                    )
                    self.assertEqual(snapshot(payload), state)
                observed[d.id] = state
            # Nothing replaced yet.
            self.assertEqual(home_state(self.target), before)

        run_apply(self.target, desired, fault=FaultAt("swap:" + desired[0].id, action=check))
        self.assertEqual(set(observed), {d.id for d in desired})

    def test_missing_parents_created(self):
        dest = self.home / ".config" / "deep" / "er" / "file.conf"
        run_apply(self.target, [DesiredEntry("deep", dest, "file", content=b"x", mode=0o600)])
        self.assertEqual(dest.read_bytes(), b"x")
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o600)

    def test_file_replaces_directory_and_symlink_replaces_file(self):
        (self.home / "d").mkdir()
        (self.home / "d" / "inner").write_text("i")
        (self.home / "f").write_text("f")
        run_apply(
            self.target,
            [
                DesiredEntry("d", self.home / "d", "file", content=b"now a file"),
                DesiredEntry("f", self.home / "f", "symlink", link_text="relative target"),
            ],
        )
        self.assertEqual((self.home / "d").read_bytes(), b"now a file")
        self.assertEqual(os.readlink(self.home / "f"), "relative target")

    def test_symlink_leaf_to_unrelated_file_is_not_followed(self):
        unrelated = self.tmp / "unrelated.txt"
        unrelated.write_text("do not touch")
        os.chmod(unrelated, 0o444)
        os.symlink(unrelated, self.home / ".zshrc")
        run_apply(self.target, [DesiredEntry("zshrc", self.home / ".zshrc", "file", content=b"new")])
        self.assertEqual(unrelated.read_text(), "do not touch")
        self.assertEqual((self.home / ".zshrc").read_bytes(), b"new")
        tx.restore(self.target, which="baseline")
        self.assertEqual(os.readlink(self.home / ".zshrc"), str(unrelated))
        self.assertEqual(unrelated.read_text(), "do not touch")

    def test_validation_rejects_before_mutation(self):
        cases = [
            [DesiredEntry("a", self.home / ".a", "symlink", link_text="x"),
             DesiredEntry("b", self.home / ".a" / "b", "symlink", link_text="y")],
            [DesiredEntry("a", self.home / ".a", "symlink", link_text="x"),
             DesiredEntry("a", self.home / ".b", "symlink", link_text="y")],
            [DesiredEntry("a", self.tmp / "outside", "symlink", link_text="x")],
            [DesiredEntry("a", self.home / ".config", "symlink", link_text="x")],
            [DesiredEntry("a", self.target.state_root / "x", "symlink", link_text="x")],
            [DesiredEntry("a", self.home / ".a", "weird")],
            [DesiredEntry("a", self.home / ".a", "symlink")],
            [DesiredEntry("a", self.home / ".a", "file")],
            [DesiredEntry("a", self.target.repo_root, "dir", staged_dir=self.tmp / "missing")],
            [DesiredEntry("Bad", self.home / ".a", "absent")],
        ]
        for desired in cases:
            with self.subTest(desired=desired):
                with self.assertRaises(TransactionError):
                    run_apply(self.target, desired)
                self.assertEqual(list((self.target.state_root / "journal").iterdir()), [])
        self.assertFalse(os.path.lexists(self.home / ".a"))

    def test_symlinked_parent_escape_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.home / ".config")
        with self.assertRaises(TransactionError):
            run_apply(self.target, [DesiredEntry("n", self.home / ".config" / "nvim", "symlink", link_text="x")])
        self.assertEqual(list(outside.iterdir()), [])

    def test_state_owner_mismatch_refused(self):
        run_apply(self.target, [DesiredEntry("a", self.home / ".a", "symlink", link_text="x")])
        path = self.target.state_root / "state.json"
        state = json.loads(path.read_text())
        state["owner_uid"] = os.getuid() + 1
        path.write_text(json.dumps(state))
        with self.assertRaises(TransactionError):
            with Transaction(self.target, tx.new_run_id()):
                pass

    def test_entries_from_manifest(self):
        source = self.tmp / "svc"
        source.write_bytes(b"[Unit]\n")
        resolved = [
            ResolvedEntry("a", self.home / ".a", "symlink", self.tmp, str(self.home / ".dotfiles/a"), None, "always"),
            ResolvedEntry("w", self.home / ".w", "link", None, "/x/y", None, "systemd-user"),
            ResolvedEntry("c", self.home / ".c", "copy", source, None, 0o600, "systemd-user"),
            ResolvedEntry("r", self.home / ".r", "remove", None, None, None, "always"),
        ]
        desired = tx.entries_from_manifest(resolved, self.target)
        self.assertEqual([d.kind for d in desired], ["symlink", "symlink", "file", "absent"])
        self.assertEqual(desired[2].content, b"[Unit]\n")
        self.assertEqual(desired[2].mode, 0o600)
        self.assertEqual(desired[1].link_text, "/x/y")
        without = tx.entries_from_manifest(resolved, self.target, systemd_user=False)
        self.assertEqual([d.id for d in without], ["a", "r"])
        self.assertEqual(tx.skipped_by_condition(resolved, systemd_user=False), ["w", "c"])

    def test_load_status(self):
        self.assertFalse(tx.load_status(self.target)["installed"])
        desired = self.populate()
        result = run_apply(self.target, desired)
        status = tx.load_status(self.target)
        self.assertTrue(status["installed"])
        self.assertEqual(status["run_id"], result.run_id)
        self.assertEqual(status["drifted"], [])
        self.assertTrue(all(e["matches"] for e in status["entries"].values()))
        self.assertEqual(status["last_run"]["status"], "committed")
        os.unlink(self.home / ".zshrc")
        status = tx.load_status(self.target)
        self.assertEqual(status["drifted"], ["zshrc"])
        self.assertEqual(status["entries"]["zshrc"]["current_kind"], "absent")


class ConcurrencyTests(HomeCase):
    def test_second_transaction_refused(self):
        with Transaction(self.target, tx.new_run_id()):
            with self.assertRaises(ConcurrentRunError):
                with Transaction(self.target, tx.new_run_id()):
                    pass
            with self.assertRaises(ConcurrentRunError):
                tx.restore(self.target, which="baseline")
        # Released afterwards.
        run_apply(self.target, [DesiredEntry("a", self.home / ".a", "symlink", link_text="x")])

    def test_requires_context_manager(self):
        t = Transaction(self.target, tx.new_run_id())
        with self.assertRaises(TransactionError):
            t.apply([], generation=GEN)


def _fault_points(case: HomeCase, build, prior=None) -> list[tuple[str, int]]:
    """Run once with a recorder and return every (step, occurrence)."""

    scratch = Path(tempfile.mkdtemp(prefix="pd-tx-rec-"))
    try:
        home = scratch / "home"
        home.mkdir()
        target = make_target(home)
        clone = HomeCase.__new__(HomeCase)
        clone.home, clone.target, clone.tmp = home, target, scratch
        clone.repo_link = home / ".dotfiles"
        if prior is not None:
            run_apply(target, prior(clone))
        recorder = Recorder()
        run_apply(target, build(clone), fault=recorder)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    counts: dict[str, int] = {}
    points = []
    for step in recorder.seen:
        counts[step] = counts.get(step, 0) + 1
        points.append((step, counts[step]))
    return points


class FailureInjectionTests(unittest.TestCase):
    """Every fault point of a real install rolls back to the exact prior state."""

    def _fresh(self) -> HomeCase:
        case = HomeCase("run")
        case.run = lambda *a, **k: None  # type: ignore[assignment]
        case.setUp()
        self.addCleanup(case._cleanup)
        return case

    def test_injected_failures_roll_back_exactly(self):
        points = _fault_points(self, lambda c: HomeCase.populate(c))
        names = {p[0] for p in points}
        for required in ("plan", "before-backup", "after-backup", "before-commit", "journal"):
            self.assertIn(required, names)
        self.assertTrue(any(n.startswith("swap:") for n in names))
        self.assertTrue(any(n.startswith("baseline:") for n in names))
        for name, nth in points:
            with self.subTest(fault=name, nth=nth):
                case = self._fresh()
                desired = case.populate()
                before = home_state(case.target)
                with self.assertRaises(Injected):
                    run_apply(case.target, desired, fault=FaultAt(name, nth))
                self.assertEqual(home_state(case.target), before)
                self.assertFalse((case.target.state_root / "state.json").exists())
                self.assertEqual(list((case.target.state_root / "backups" / "baseline").iterdir()), [])
                journals = list((case.target.state_root / "journal").glob("*.json"))
                if journals:  # absent only when the very first journal write failed
                    self.assertEqual(only_journal(case.target)["status"], "rolled-back")
                else:
                    self.assertEqual((name, nth), ("journal", 1))
                # The next run succeeds from the restored state.
                run_apply(case.target, desired)

    def test_crash_then_recovery_restores_exactly(self):
        points = _fault_points(self, lambda c: HomeCase.populate(c))
        for name, nth in points:
            with self.subTest(fault=name, nth=nth):
                case = self._fresh()
                desired = case.populate()
                before = home_state(case.target)
                with self.assertRaises(Crash):
                    run_apply(case.target, desired, fault=FaultAt(name, nth, exc=Crash))
                with Transaction(case.target, tx.new_run_id()) as t:
                    recovered = list(t.recovered)
                    # Nobody touched the targets after the crash, so
                    # recovery has nothing unexpected to set aside.
                    self.assertEqual(t.recovery_saved, [])
                self.assertEqual(home_state(case.target), before)
                self.assertFalse((case.target.state_root / "backups" / "recovery").exists())
                self.assertFalse((case.target.state_root / "state.json").exists())
                self.assertEqual(
                    [p.name for p in (case.target.state_root / "backups" / "baseline").iterdir()], []
                )
                journals = sorted((case.target.state_root / "journal").glob("*.json"))
                if journals:
                    data = json.loads(journals[0].read_text())
                    self.assertEqual(data["status"], "rolled-back")
                    self.assertEqual(recovered, [data["run_id"]])

    def test_v2_failures_roll_back_retire_and_add_together(self):
        def v1(c):
            h = c.home
            (h / ".old-a").write_text("orig a\n")
            (h / "keep.txt").write_text("sentinel\n")
            return [
                DesiredEntry("a", h / ".old-a", "symlink", link_text=c.link("a")),
                DesiredEntry("b", h / ".b", "symlink", link_text=c.link("b")),
                DesiredEntry("keep-me", h / ".k", "symlink", link_text=c.link("k")),
            ]

        def v2(c):
            h = c.home
            (h / ".new-a").write_text("orig new a\n") if not os.path.lexists(h / ".new-a") else None
            return [
                DesiredEntry("keep-me", h / ".k", "symlink", link_text=c.link("k2")),
                DesiredEntry("a-renamed", h / ".new-a", "symlink", link_text=c.link("a")),
            ]

        points = _fault_points(self, v2, prior=v1)
        self.assertTrue(any(n == "swap:a" for n, _ in points))  # retirement step
        for name, nth in points:
            with self.subTest(fault=name, nth=nth):
                case = self._fresh()
                run_apply(case.target, v1(case))
                desired = v2(case)
                before = home_state(case.target)
                baseline = case.baseline_bytes()
                state_bytes = (case.target.state_root / "state.json").read_bytes()
                with self.assertRaises(Injected):
                    run_apply(case.target, desired, fault=FaultAt(name, nth))
                self.assertEqual(home_state(case.target), before)
                self.assertEqual(case.baseline_bytes(), baseline)
                self.assertEqual((case.target.state_root / "state.json").read_bytes(), state_bytes)


class RecoveryTests(HomeCase):
    def test_commit_point_is_state_json(self):
        desired = self.populate()
        result = run_apply(self.target, desired)
        after = home_state(self.target)
        path = self.target.state_root / "journal" / f"{result.run_id}.json"
        data = json.loads(path.read_text())
        data["status"] = "running"  # final journal write "lost"
        path.write_text(json.dumps(data))
        with Transaction(self.target, tx.new_run_id()) as t:
            self.assertEqual(t.recovered, [])
        self.assertEqual(home_state(self.target), after)
        self.assertEqual(journal(self.target, result.run_id)["status"], "committed")

    def test_crash_during_second_run_recovers_to_first(self):
        desired = self.populate()
        run_apply(self.target, desired)
        after_first = home_state(self.target)
        baseline = self.baseline_bytes()
        changed = [
            DesiredEntry(d.id, d.dest, d.kind, link_text=d.link_text + ".v2") if d.kind == "symlink" else d
            for d in desired
        ]
        with self.assertRaises(Crash):
            run_apply(self.target, changed, fault=FaultAt("swapped:nvim", exc=Crash))
        self.assertNotEqual(home_state(self.target), after_first)
        self.assertEqual(tx.load_status(self.target)["interrupted"].__len__(), 1)
        with Transaction(self.target, tx.new_run_id()) as t:
            self.assertEqual(len(t.recovered), 1)
        self.assertEqual(home_state(self.target), after_first)
        self.assertEqual(self.baseline_bytes(), baseline)
        self.assertEqual(tx.load_status(self.target)["interrupted"], [])

    def test_stale_baseline_temp_removed(self):
        tmp = self.target.state_root / "backups" / "baseline" / ".tmp-x-20260101T000000Z-deadbeef"
        tmp.mkdir(parents=True)
        (tmp / "object").write_text("partial")
        with Transaction(self.target, tx.new_run_id()):
            pass
        self.assertFalse(tmp.exists())


class EvolutionTests(HomeCase):
    def test_v1_to_v2_add_remove_rename(self):
        h = self.home
        (h / ".a").write_text("orig a\n")
        (h / ".b").write_text("orig b\n")
        (h / "keep.txt").write_text("sentinel\n")
        v1 = [
            DesiredEntry("a", h / ".a", "symlink", link_text=self.link("a")),
            DesiredEntry("b", h / ".b", "symlink", link_text=self.link("b")),
            DesiredEntry("c", h / ".c", "symlink", link_text=self.link("c")),
            DesiredEntry("same-dest", h / ".s", "symlink", link_text=self.link("s")),
        ]
        run_apply(self.target, v1)
        baseline = self.baseline_bytes()
        (h / ".d").write_text("orig d\n")
        v2 = [
            DesiredEntry("a", h / ".a", "symlink", link_text=self.link("a")),
            DesiredEntry("d", h / ".d", "symlink", link_text=self.link("c")),  # c renamed to d
            DesiredEntry("e", h / ".e", "file", content=b"new"),
            DesiredEntry("same-dest-v2", h / ".s", "symlink", link_text=self.link("s2")),
        ]
        result = run_apply(self.target, v2)
        self.assertEqual(sorted(result.retired), ["b", "c"])
        self.assertEqual(result.drifted, [])
        self.assertEqual(sorted(result.baseline_added), ["d", "e", "same-dest-v2"])
        # Existing baselines are byte-identical; new ones were appended.
        after = self.baseline_bytes()
        for key, value in baseline.items():
            self.assertEqual(after[key], value)
        self.assertEqual((h / ".b").read_text(), "orig b\n")  # retired -> baseline
        self.assertFalse(os.path.lexists(h / ".c"))  # retired -> baseline absent
        self.assertEqual(os.readlink(h / ".d"), self.link("c"))
        self.assertEqual((h / ".e").read_bytes(), b"new")
        self.assertEqual(os.readlink(h / ".s"), self.link("s2"))
        self.assertEqual((h / "keep.txt").read_text(), "sentinel\n")
        # The id moved to a new name at the same dest adopts the old baseline
        # (absent), not the V1-installed link.
        meta = json.loads(
            (self.target.state_root / "backups" / "baseline" / "same-dest-v2" / "meta.json").read_text()
        )
        self.assertEqual(meta["state"]["kind"], "absent")
        self.assertEqual(meta["adopted_from"], "same-dest")
        state = json.loads((self.target.state_root / "state.json").read_text())
        self.assertEqual(sorted(state["entries"]), ["a", "d", "e", "same-dest-v2"])
        # Retiring back to V1-less: restoring baseline of everything returns originals.
        tx.restore(self.target, which="baseline")
        self.assertEqual((h / ".a").read_text(), "orig a\n")
        self.assertEqual((h / ".d").read_text(), "orig d\n")
        self.assertFalse(os.path.lexists(h / ".e"))
        self.assertFalse(os.path.lexists(h / ".s"))

    def test_drifted_retirement_preserved_and_reported(self):
        h = self.home
        v1 = [
            DesiredEntry("a", h / ".a", "symlink", link_text=self.link("a")),
            DesiredEntry("b", h / ".b", "symlink", link_text=self.link("b")),
        ]
        run_apply(self.target, v1)
        os.unlink(h / ".b")
        (h / ".b").write_text("user's own now\n")
        result = run_apply(self.target, v1[:1])
        self.assertEqual(result.drifted, ["b"])
        self.assertEqual(result.retired, [])
        self.assertEqual((h / ".b").read_text(), "user's own now\n")
        state = json.loads((self.target.state_root / "state.json").read_text())
        self.assertNotIn("b", state["entries"])

    def test_moved_dest_for_same_id_refused(self):
        h = self.home
        run_apply(self.target, [DesiredEntry("a", h / ".a", "symlink", link_text="x")])
        with self.assertRaises(TransactionError):
            run_apply(self.target, [DesiredEntry("a", h / ".a2", "symlink", link_text="x")])
        self.assertEqual(os.readlink(h / ".a"), "x")
        self.assertFalse(os.path.lexists(h / ".a2"))

    def test_retired_overlapping_new_dest_refused(self):
        h = self.home
        run_apply(self.target, [DesiredEntry("nvim", h / ".config" / "nvim", "symlink", link_text="x")])
        with self.assertRaises(TransactionError):
            run_apply(
                self.target,
                [DesiredEntry("nvim-init", h / ".config" / "nvim" / "init.lua", "symlink", link_text="y")],
            )
        self.assertEqual(os.readlink(h / ".config" / "nvim"), "x")


class RestoreTests(HomeCase):
    def test_restore_baseline_and_prior_run_including_absent(self):
        h = self.home
        (h / ".f").write_text("original f\n")
        os.chmod(h / ".f", 0o600)
        v1 = [
            DesiredEntry("f", h / ".f", "file", content=b"v1 content\n", mode=0o644),
            DesiredEntry("new", h / ".new", "symlink", link_text="v1-link"),
        ]
        first = run_apply(self.target, v1)
        after_v1 = home_state(self.target)
        v2 = [
            DesiredEntry("f", h / ".f", "file", content=b"v2 content\n", mode=0o640),
            DesiredEntry("new", h / ".new", "symlink", link_text="v2-link"),
        ]
        second = run_apply(self.target, v2)
        # Restoring the second run returns to what it replaced (the v1 state).
        result = tx.restore(self.target, which=second.run_id)
        self.assertEqual(sorted(result.restored), ["f", "new"])
        self.assertEqual(home_state(self.target), after_v1)
        # Restoring the first run returns to the pre-install state, including absence.
        result = tx.restore(self.target, which=first.run_id, force=False)
        self.assertEqual((h / ".f").read_text(), "original f\n")
        self.assertEqual(stat.S_IMODE(os.lstat(h / ".f").st_mode), 0o600)
        self.assertFalse(os.path.lexists(h / ".new"))
        # And reinstalling works.
        run_apply(self.target, v2)
        tx.restore(self.target, which="baseline")
        self.assertEqual((h / ".f").read_text(), "original f\n")
        self.assertFalse(os.path.lexists(h / ".new"))
        state = json.loads((self.target.state_root / "state.json").read_text())
        self.assertEqual(state["entries"], {})

    def test_restore_refuses_drift_without_force_and_snapshots_first(self):
        desired = self.populate()
        run_apply(self.target, desired)
        os.unlink(self.home / ".zshrc")
        (self.home / ".zshrc").write_text("edited by user\n")
        drifted_state = home_state(self.target)
        runs_before = sorted((self.target.state_root / "backups" / "runs").iterdir())
        with self.assertRaises(TransactionError) as ctx:
            tx.restore(self.target, which="baseline")
        self.assertIn("zshrc", str(ctx.exception))
        self.assertEqual(home_state(self.target), drifted_state)
        self.assertEqual(sorted((self.target.state_root / "backups" / "runs").iterdir()), runs_before)

        result = tx.restore(self.target, which="baseline", force=True)
        self.assertEqual(result.forced, ["zshrc"])
        self.assertEqual((self.home / ".zshrc").read_text(), "old zshrc\n")
        saved = result.backup_dir / "zshrc" / "object"
        self.assertEqual(saved.read_text(), "edited by user\n")
        # The restore run itself can be undone.
        tx.restore(self.target, which=result.run_id, ids=["zshrc"], force=True)
        self.assertEqual((self.home / ".zshrc").read_text(), "edited by user\n")

    def test_restore_selected_ids_and_sentinels(self):
        desired = self.populate()
        run_apply(self.target, desired)
        tx.restore(self.target, which="baseline", ids=["nvim"])
        self.assertTrue((self.home / ".config" / "nvim").is_dir())
        self.assertEqual(os.readlink(self.home / ".zshrc"), self.link("zsh/zshrc"))
        self.assertEqual((self.home / "keep.txt").read_text(), "sentinel\n")
        self.assertEqual((self.home / ".config" / "keep.conf").read_text(), "sentinel conf\n")

    def test_directory_at_symlink_dest_restored_exactly(self):
        desired = self.populate()
        original = home_state(self.target)
        nvim_keys = {k: v for k, v in original.items() if k.startswith(".config/nvim")}
        run_apply(self.target, desired)
        self.assertTrue(os.path.islink(self.home / ".config" / "nvim"))
        tx.restore(self.target, which="baseline")
        restored = home_state(self.target)
        self.assertEqual({k: v for k, v in restored.items() if k.startswith(".config/nvim")}, nvim_keys)
        # Everything else also returns, apart from parent dirs created for new targets.
        for key, value in original.items():
            self.assertEqual(restored[key], value, key)

    def test_restore_failure_rolls_back(self):
        desired = self.populate()
        run_apply(self.target, desired)
        installed = home_state(self.target)
        with self.assertRaises(Injected):
            tx.restore(self.target, which="baseline", fault=FaultAt("swapped:nvim"))
        self.assertEqual(home_state(self.target), installed)

    def test_restore_rejects_unknown_or_uncommitted_run(self):
        with self.assertRaises(TransactionError):
            tx.restore(self.target, which="not-a-run")
        with self.assertRaises(TransactionError):
            tx.restore(self.target, which="20260101T000000Z-deadbeef")
        desired = self.populate()
        with self.assertRaises(Injected):
            run_apply(self.target, desired, fault=FaultAt("before-commit"))
        failed = only_journal(self.target)["run_id"]
        with self.assertRaises(TransactionError):
            tx.restore(self.target, which=failed)


class DirectoryPromotionTests(HomeCase):
    def make_staged(self, name: str, marker: str) -> Path:
        staged = self.target.staging_root / name
        (staged / "zsh").mkdir(parents=True)
        (staged / "zsh" / "zshrc").write_text(marker)
        (staged / ".personal-dotfiles-owned").write_text("{}")
        os.symlink("zsh/zshrc", staged / "link")
        return staged

    def promote(self, staged: Path, **kwargs):
        return run_apply(
            self.target,
            [
                DesiredEntry("repo", self.target.repo_root, "dir", staged_dir=staged),
            ],
            **kwargs,
        )

    def test_promotion_by_rename_and_update(self):
        staged = self.make_staged("20260101T000000Z-00000001", "v1")
        expected = snapshot(staged)
        self.promote(staged)
        self.assertFalse(os.path.lexists(staged))
        self.assertEqual(snapshot(self.target.repo_root), expected)
        self.assertEqual((self.repo_link / "zsh" / "zshrc").read_text(), "v1")

        staged2 = self.make_staged("20260101T000000Z-00000002", "v2")
        v1_state = snapshot(self.target.repo_root)
        result = self.promote(staged2)
        self.assertEqual(result.changed, ["repo"])
        self.assertEqual((self.repo_link / "zsh" / "zshrc").read_text(), "v2")
        self.assertEqual(snapshot(result.backup_dir / "repo" / "object"), v1_state)

    def test_promotion_rollback_returns_staged_and_old_repo(self):
        staged = self.make_staged("s1", "v1")
        self.promote(staged)
        old_repo = snapshot(self.target.repo_root)
        staged2 = self.make_staged("s2", "v2")
        staged2_state = snapshot(staged2)
        with self.assertRaises(Injected):
            self.promote(staged2, fault=FaultAt("before-commit"))
        self.assertEqual(snapshot(self.target.repo_root), old_repo)
        self.assertEqual(snapshot(staged2), staged2_state)

    def test_first_promotion_rollback(self):
        staged = self.make_staged("s1", "v1")
        staged_state = snapshot(staged)
        with self.assertRaises(Injected):
            self.promote(staged, fault=FaultAt("swapped:repo"))
        self.assertFalse(os.path.lexists(self.target.repo_root))
        self.assertFalse(os.path.lexists(self.repo_link))
        self.assertEqual(snapshot(staged), staged_state)

    def test_cross_device_promotion_copies_and_verifies(self):
        staged = self.make_staged("s1", "v1")
        expected = snapshot(staged)
        with mock.patch.object(tx, "_same_device", return_value=False):
            self.promote(staged)
        self.assertEqual(snapshot(self.target.repo_root), expected)
        self.assertTrue(staged.is_dir())  # caller cleans staging

    def test_existing_populated_dotfiles_dir_backed_up(self):
        live = self.home / ".dotfiles"  # == target.repo_root
        self.assertEqual(live, self.target.repo_root)
        (live / "sub").mkdir(parents=True)
        (live / "sub" / "file").write_text("user's old checkout")
        original = snapshot(live)
        staged = self.make_staged("s1", "v1")
        expected = snapshot(staged)
        result = self.promote(staged)
        self.assertEqual(snapshot(live), expected)
        self.assertEqual(snapshot(self.target.state_root / "backups" / "baseline" / "repo" / "object"), original)
        self.assertIsNotNone(result.backup_dir)
        tx.restore(self.target, which="baseline")
        self.assertEqual(snapshot(live), original)


def backup_object(target, backup_dir: Path, entry_id: str) -> Path:
    """The saved object for ``entry_id`` in a run, following baseline refs."""

    meta = json.loads((backup_dir / entry_id / "meta.json").read_text())
    if meta["payload"] == "baseline":
        return target.state_root / "backups" / "baseline" / entry_id / "object"
    return backup_dir / entry_id / "object"


STUB = (REPO_ROOT / "git" / "gitconfig.stub").read_bytes()


class CopiedFileDriftTests(HomeCase):
    """DS-1: a copied config that a program or the user changed is kept."""

    def gitconfig(self, content: bytes = STUB) -> DesiredEntry:
        return DesiredEntry("gitconfig", self.home / ".gitconfig", "file",
                            content=content, mode=0o644)

    def others(self) -> list[DesiredEntry]:
        return [DesiredEntry("zshrc", self.home / ".zshrc", "symlink",
                             link_text=self.link("zsh/zshrc"))]

    def recorded(self, entry_id: str) -> ObjectState:
        state = json.loads((self.target.state_root / "state.json").read_text())
        return ObjectState.from_json(state["entries"][entry_id]["state"])

    def test_git_config_global_edit_survives_rerun(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is not installed")
        import subprocess

        (self.home / ".gitconfig").write_text("[user]\n\tname = Before Install\n")
        first = run_apply(self.target, [self.gitconfig(), *self.others()])
        self.assertIn("gitconfig", first.changed)  # first install overwrites
        self.assertEqual(backup_object(self.target, first.backup_dir, "gitconfig").read_text(),
                         "[user]\n\tname = Before Install\n")
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": str(self.home / ".config")}
        for key, value in (("core.editor", "nano"), ("credential.helper", "store")):
            subprocess.run([git, "config", "--global", key, value], env=env, check=True)
        edited = (self.home / ".gitconfig").read_bytes()
        self.assertIn(b"nano", edited)

        second = run_apply(self.target, [self.gitconfig(), *self.others()])
        self.assertEqual(second.kept, ["gitconfig"])
        self.assertEqual(second.changed, [])
        self.assertNotIn("gitconfig", second.unchanged)
        self.assertIsNone(second.backup_dir)
        self.assertEqual((self.home / ".gitconfig").read_bytes(), edited)
        out = subprocess.run([git, "config", "--global", "core.editor"], env=env,
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "nano")
        # Still reported on later runs, and status shows the local change.
        third = run_apply(self.target, [self.gitconfig(), *self.others()])
        self.assertEqual(third.kept, ["gitconfig"])
        self.assertEqual(tx.load_status(self.target)["drifted"], ["gitconfig"])

    def test_force_backs_up_and_overwrites_local_edit(self):
        run_apply(self.target, [self.gitconfig()])
        with open(self.home / ".gitconfig", "ab") as handle:
            handle.write(b"[alias]\n\tlol = log --graph --oneline\n")
        edited = snapshot(self.home / ".gitconfig")
        with Transaction(self.target, tx.new_run_id()) as t:
            result = t.apply([self.gitconfig()], generation=GEN, force=True)
        self.assertEqual(result.forced, ["gitconfig"])
        self.assertEqual(result.changed, ["gitconfig"])
        self.assertEqual(result.kept, [])
        self.assertEqual((self.home / ".gitconfig").read_bytes(), STUB)
        self.assertEqual(
            snapshot(backup_object(self.target, result.backup_dir, "gitconfig")), edited
        )
        self.assertEqual(self.recorded("gitconfig"), snapshot(self.home / ".gitconfig"))

    def test_unedited_copy_follows_repo_updates(self):
        run_apply(self.target, [self.gitconfig(b"v1\n")])
        second = run_apply(self.target, [self.gitconfig(b"v2\n")])
        self.assertEqual(second.changed, ["gitconfig"])
        self.assertEqual((self.home / ".gitconfig").read_bytes(), b"v2\n")
        # An edited copy is kept even when the repository version moves on,
        # and the recorded state stays what the installer last wrote.
        (self.home / ".gitconfig").write_bytes(b"v2\nmine\n")
        third = run_apply(self.target, [self.gitconfig(b"v3\n")])
        self.assertEqual(third.kept, ["gitconfig"])
        self.assertEqual((self.home / ".gitconfig").read_bytes(), b"v2\nmine\n")
        self.assertEqual(self.recorded("gitconfig").sha256,
                         __import__("hashlib").sha256(b"v2\n").hexdigest())
        # Undoing the edit hands the file back to the installer.
        (self.home / ".gitconfig").write_bytes(b"v2\n")
        fourth = run_apply(self.target, [self.gitconfig(b"v3\n")])
        self.assertEqual(fourth.changed, ["gitconfig"])
        self.assertEqual((self.home / ".gitconfig").read_bytes(), b"v3\n")

    def test_mode_change_and_type_change_are_local_changes(self):
        run_apply(self.target, [self.gitconfig()])
        os.chmod(self.home / ".gitconfig", 0o600)
        self.assertEqual(run_apply(self.target, [self.gitconfig()]).kept, ["gitconfig"])
        os.unlink(self.home / ".gitconfig")
        own = self.tmp / "my-gitconfig"
        own.write_text("mine")
        os.symlink(own, self.home / ".gitconfig")
        self.assertEqual(run_apply(self.target, [self.gitconfig()]).kept, ["gitconfig"])
        self.assertEqual(os.readlink(self.home / ".gitconfig"), str(own))

    def test_deleted_copy_is_written_again(self):
        run_apply(self.target, [self.gitconfig()])
        os.unlink(self.home / ".gitconfig")
        result = run_apply(self.target, [self.gitconfig()])
        self.assertEqual(result.changed, ["gitconfig"])
        self.assertEqual(result.kept, [])
        self.assertEqual((self.home / ".gitconfig").read_bytes(), STUB)

    def test_edited_symlink_target_is_still_replaced(self):
        run_apply(self.target, self.others())
        os.unlink(self.home / ".zshrc")
        (self.home / ".zshrc").write_text("mine\n")
        result = run_apply(self.target, self.others())
        self.assertEqual(result.changed, ["zshrc"])
        self.assertEqual(result.kept, [])
        self.assertTrue(os.path.islink(self.home / ".zshrc"))

    def test_renamed_id_at_same_dest_keeps_local_edit(self):
        unit = self.home / ".config" / "systemd" / "user" / "x.service"
        run_apply(self.target, [DesiredEntry("old-unit", unit, "file", content=b"a\n")])
        unit.write_bytes(b"a\nedited\n")
        result = run_apply(self.target, [DesiredEntry("new-unit", unit, "file", content=b"b\n")])
        self.assertEqual(result.kept, ["new-unit"])
        self.assertEqual(unit.read_bytes(), b"a\nedited\n")

    def test_every_manifest_copy_keeps_program_edits(self):
        """gitconfig, terminator, pudb and the systemd copies, end to end."""

        import dataclasses as dc

        from installer import manifest as manifest_mod

        loaded = manifest_mod.load_manifest(REPO_ROOT / "manifests" / "managed-paths.json")
        copies = tuple(e for e in loaded.entries if e.kind == "copy")
        self.assertGreaterEqual(len(copies), 3)
        subset = dc.replace(loaded, entries=copies)
        resolved = manifest_mod.resolve(subset, self.target, REPO_ROOT)
        desired = tx.entries_from_manifest(resolved, self.target)
        self.assertEqual(len(desired), len(copies))
        first = run_apply(self.target, desired)
        self.assertEqual(sorted(first.changed), sorted(d.id for d in desired))
        edited = {}
        for d in desired:
            with open(d.dest, "ab") as handle:
                handle.write(b"\n# saved by the program\n")
            edited[d.id] = snapshot(d.dest)
        second = run_apply(self.target, desired)
        self.assertEqual(sorted(second.kept), sorted(d.id for d in desired))
        self.assertEqual(second.changed, [])
        for d in desired:
            self.assertEqual(snapshot(d.dest), edited[d.id], d.id)


class CrashRecoveryEditTests(HomeCase):
    """DS-4: an edit made between a crash and recovery is saved, not lost."""

    def crash_then_edit(self, dest: Path, desired: DesiredEntry, edit: bytes):
        with self.assertRaises(Crash):
            run_apply(self.target, [desired], fault=FaultAt(f"swapped:{desired.id}", exc=Crash))
        self.assertEqual(dest.read_bytes(), desired.content)
        with open(dest, "ab") as handle:
            handle.write(edit)
        return snapshot(dest)

    def test_edit_after_crash_saved_before_rollback(self):
        dest = self.home / ".gitconfig"
        dest.write_bytes(b"[user]\n\tname = Orig\n")
        original = snapshot(dest)
        desired = DesiredEntry("gitconfig", dest, "file", content=STUB, mode=0o644)
        edit = b"[alias]\n\tlol = log --graph --oneline\n"
        edited = self.crash_then_edit(dest, desired, edit)

        with Transaction(self.target, tx.new_run_id()) as t:
            self.assertEqual(len(t.recovered), 1)
            saved = list(t.recovery_saved)
            result = t.apply([desired], generation=GEN)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], "gitconfig")
        self.assertEqual(saved[0]["dest"], str(dest))
        self.assertEqual(saved[0]["run_id"], t.recovered[0])
        saved_path = Path(saved[0]["saved_to"])
        self.assertTrue(saved_path.is_relative_to(self.target.state_root / "backups" / "recovery"))
        self.assertEqual(snapshot(saved_path), edited)
        self.assertIn(edit, saved_path.read_bytes())
        meta = json.loads((saved_path.parent / "meta.json").read_text())
        self.assertEqual(ObjectState.from_json(meta["state"]), edited)
        self.assertEqual(stat.S_IMODE(os.lstat(saved_path.parent).st_mode), 0o700)
        self.assertEqual(result.recovery_saved, saved)
        # Rollback still returned the pre-crash object, which the rerun
        # then backed up and replaced as on a first install.
        self.assertEqual(
            snapshot(backup_object(self.target, result.backup_dir, "gitconfig")), original
        )
        self.assertEqual(dest.read_bytes(), STUB)
        entry = journal(self.target, t.recovered[0])["steps"][0]
        self.assertEqual(entry["unexpected_saved"], str(saved_path))

    def test_new_file_edited_after_crash_saved_before_removal(self):
        dest = self.home / ".config" / "pudb" / "pudb.cfg"
        desired = DesiredEntry("pudb", dest, "file", content=b"[pudb]\n", mode=0o644)
        edited = self.crash_then_edit(dest, desired, b"theme = dark\n")
        with Transaction(self.target, tx.new_run_id()) as t:
            saved = list(t.recovery_saved)
        self.assertFalse(os.path.lexists(dest))  # the crashed run created it
        self.assertEqual(len(saved), 1)
        self.assertEqual(snapshot(Path(saved[0]["saved_to"])), edited)

    def test_unsaveable_object_left_in_place(self):
        dest = self.home / ".gitconfig"
        dest.write_bytes(b"orig\n")
        desired = DesiredEntry("gitconfig", dest, "file", content=STUB, mode=0o644)
        edited = self.crash_then_edit(dest, desired, b"mine\n")
        with mock.patch.object(tx, "_copy_object", side_effect=OSError("disk full")):
            with self.assertRaises(TransactionError):
                with Transaction(self.target, tx.new_run_id()):
                    pass
        self.assertEqual(snapshot(dest), edited)


class DurabilityTests(HomeCase):
    def test_full_run_with_fsync(self):
        with mock.patch.object(tx, "FSYNC", True):
            desired = self.populate()
            run_apply(self.target, desired)
            tx.restore(self.target, which="baseline")
        self.assertEqual((self.home / ".zshrc").read_text(), "old zshrc\n")


class ReadOnlyTreeTests(HomeCase):
    def test_read_only_directories_backed_up_and_removed(self):
        d = self.home / ".config" / "ro"
        (d / "inner").mkdir(parents=True)
        (d / "inner" / "f").write_text("x")
        os.chmod(d / "inner" / "f", 0o400)
        os.chmod(d / "inner", 0o500)
        original = snapshot(d)
        run_apply(self.target, [DesiredEntry("ro", d, "symlink", link_text="elsewhere")])
        self.assertTrue(os.path.islink(d))
        tx.restore(self.target, which="baseline")
        self.assertEqual(snapshot(d), original)


if __name__ == "__main__":
    unittest.main()
