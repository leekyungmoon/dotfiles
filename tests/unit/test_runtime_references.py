"""Static audit of runtime references into the dotfiles checkout.

``manifests/runtime-references.json`` is the reviewed inventory of every place
where executable/config sources reach the installed checkout through the
``~/.dotfiles`` compatibility link, use a DOTFILES-style identifier, or name
one of the old mutable roots that used to live inside the checkout (plugin
clones, generated bundles, lockfiles). It also records the runtime writers and
where they write.

The checkout is installer-owned source, so this test fails when:

- a scanned file gains a reference that the manifest does not list;
- a listed reference no longer exists (the inventory went stale);
- a reference is classified as a comment but is executable, or the reverse;
- a writer's redirection marker disappears or its old in-checkout path returns.

To accept a new reference, add an entry with its exact stripped line text, a
classification and a purpose; the failure message prints a ready-made entry.
"""

from __future__ import annotations

import collections
import fnmatch
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "manifests" / "runtime-references.json"
MANAGED_PATHS = REPO_ROOT / "manifests" / "managed-paths.json"

WRITER_STATUSES = {
    "redirected", "outside-checkout", "installer-delegation",
    "pending-owner-change", "open-question", "inert",
    # writes into the checkout on purpose (upstream layout, a user decision);
    # the entry must say how the change shows up and where local lines go
    "in-checkout-accepted",
}
# Wording in a writer's after/owner text that claims how a managed path is
# installed; such a writer must state the claim in "managed_paths" so it is
# checked against managed-paths.json.
_INSTALL_CLAIM = re.compile(
    r"'(?:copy|symlink)'|\bcopy of\b|\bcopied\b|\bsymlinks? (?:into|to)\b"
    r"|managed-paths\.json|\bstay symlinks\b")
# A pending transition written as if it were the state ("symlink -> copy").
_TRANSITION = re.compile(r"\b(?:symlink|link|copy)(?:\(dir\))?\s*->\s*(?:copy|symlink)\b")

# Each kind of reference the audit looks for. A line may match several kinds;
# each (line, kind) pair must be covered by its own manifest entry.
KIND_PATTERNS = {
    # ~/.dotfiles, $HOME/.dotfiles, ${HOME}/.dotfiles, systemd's %h/.dotfiles
    # (the compatibility link)
    "link": re.compile(r"(?:~|\$HOME|\$\{HOME\}|%h)/\.dotfiles(?![\w.-])"),
    # DOTFILES_UPDATE, DOTFILES_TMPDIR, _dotfiles_bin_dir, dotfiles_dir, ...
    # plus $DOTFILES / ${DOTFILES} / $DOTVIM style variables and the bare
    # DOTFILES / DOTVIM names (e.g. `export DOTVIM=...`, vim's $DOTVIM)
    "identifier": re.compile(
        r"(?<![\w-])_?(?:DOTFILES|dotfiles)_[A-Za-z0-9_]+"
        r"|(?<![\w-])\$\{?_?(?:DOTFILES|DOTVIM|dotfiles)\w*"
        r"|(?<![\w$-])(?:DOTFILES|DOTVIM)(?![\w-])"
    ),
    # mutable roots that used to be written inside the checkout through the
    # repository-backed ~/.vim, ~/.zsh, ~/.tmux and ~/.config/nvim links
    "legacy-mutable-root": re.compile(
        r"(?:(?:~|\$HOME|\$\{HOME\}|\$\{ZDOTDIR:-\$HOME\})/"
        r"\.(?:vim/plugged|zsh/antidote-plugins|zsh/antidote\.bundled\.zsh"
        r"|tmux/plugins|tmux/resurrect|config/nvim/lazy-lock\.json))(?![\w.-])"
        r"|\.dotfiles/vim/plugged(?![\w.-])"
    ),
}

CLASSIFICATIONS = {
    # executable/config dependency that intentionally resolves through the link
    "runtime-link",
    # executable line that only shows the path to the user (echo, notify, help)
    "guidance",
    # comment or example text in an executable/config file
    "comment",
    # prose in a non-executed document shipped inside a config directory
    "documentation",
    # an identifier that merely contains DOTFILES; not a path
    "identifier",
    # executable reference in a file another lane owns; must change there
    "pending-owner-change",
}
EXECUTABLE_CLASSES = {"runtime-link", "guidance", "identifier", "pending-owner-change"}
DOC_SUFFIXES = {".md", ".txt"}


SYSTEMD_SUFFIXES = {".service", ".timer", ".socket", ".target", ".path", ".mount"}


def comment_style(path: str):
    """Return (line prefixes, block comment support) for a repository path."""
    name = os.path.basename(path)
    suffix = os.path.splitext(name)[1]
    if suffix == ".lua":
        return ("--",), True
    if suffix == ".vim" or name in ("vimrc", "gvimrc"):
        return ('"',), False
    if suffix == ".scm":
        return (";",), False
    if suffix == ".json":
        return (), False
    if suffix in SYSTEMD_SUFFIXES or suffix == ".ini":
        return ("#", ";"), False
    return ("#",), False


def is_python(path: str, text: str) -> bool:
    if path.endswith(".py"):
        return True
    first = text.split("\n", 1)[0]
    return first.startswith("#!") and "python" in first


_LUA_BLOCK_OPEN = re.compile(r"^\s*--\[(=*)\[")
_PY_TRIPLE = re.compile(r'"""|\'\'\'')
_PY_DOCSTRING_OPEN = re.compile(r'^\s*[rRuU]?("""|\'\'\')')


def classify_lines(path: str, text: str):
    """Yield (stripped line, is_comment) for every line of a file.

    Python docstrings (a triple-quoted string that starts a line) count as
    comments; other triple-quoted string literals stay executable.
    """
    prefixes, lua_blocks = comment_style(path)
    python = is_python(path, text)
    block_close = None
    py_string = None  # (closing quote, is docstring) inside a triple-quoted string
    for raw in text.splitlines():
        line = raw.strip()
        if py_string is not None:
            yield line, py_string[1]
            if py_string[0] in raw:
                py_string = None
            continue
        if block_close is not None:
            yield line, True
            if block_close in raw:
                block_close = None
            continue
        if python:
            quote = _PY_TRIPLE.search(raw)
            if quote and "#" not in raw[:quote.start()]:
                docstring = bool(_PY_DOCSTRING_OPEN.match(raw))
                if quote.group(0) not in raw[quote.end():]:
                    py_string = (quote.group(0), docstring)
                if docstring:
                    yield line, True
                    continue
        if lua_blocks:
            match = _LUA_BLOCK_OPEN.match(raw)
            if match:
                close = "]" + match.group(1) + "]"
                if close not in raw[match.end():]:
                    block_close = close
                yield line, True
                continue
        yield line, bool(prefixes) and line.startswith(prefixes)


def submodule_paths(repo: Path):
    gitmodules = repo / ".gitmodules"
    if not gitmodules.exists():
        return set()
    return set(re.findall(r"^\s*path\s*=\s*(\S+)\s*$", gitmodules.read_text(), re.M))


def exclude_patterns(excludes):
    """Exclude entries are {"pattern", "reason"} objects (or bare patterns)."""
    return [e["pattern"] if isinstance(e, dict) else e for e in excludes]


def is_excluded(rel: str, patterns) -> bool:
    """fnmatch patterns ('*' also matches '/'); a pattern ending in '/' is a dir."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if rel.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def candidate_files(repo: Path, roots, excludes):
    """Tracked plus untracked-but-not-ignored files under the scan roots."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", *roots],
            check=True, capture_output=True,
        ).stdout.decode("utf-8", "surrogateescape")
        paths = sorted({p for p in out.split("\0") if p})
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for root in roots:
            base = repo / root
            if base.is_file():
                paths.append(root)
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for filename in filenames:
                    paths.append(os.path.relpath(os.path.join(dirpath, filename), repo))
        paths.sort()
    skipped = submodule_paths(repo)
    patterns = exclude_patterns(excludes)
    for rel in paths:
        if any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in skipped):
            continue
        if is_excluded(rel, patterns):
            continue
        full = repo / rel
        if full.is_symlink() or not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        yield rel, text


def scan(repo: Path, manifest):
    """Return Counter[(file, line, kind)] -> occurrences, plus comment flags."""
    scope = manifest["scan"]
    found = collections.Counter()
    is_comment = {}
    for rel, text in candidate_files(repo, scope["roots"], scope.get("exclude", [])):
        for line, comment in classify_lines(rel, text):
            for kind, pattern in KIND_PATTERNS.items():
                if pattern.search(line):
                    key = (rel, line, kind)
                    found[key] += 1
                    is_comment[key] = comment
    return found, is_comment


def load_manifest():
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_line(entry, found):
    """The full line an entry stands for.

    ``"match": "prefix"`` entries quote only the start of the line (used where
    the rest must not be copied into this public inventory); the prefix must
    identify exactly one scanned line of that file and kind.
    """
    if entry.get("match", "exact") == "exact":
        return entry["line"]
    lines = sorted({line for (rel, line, kind) in found
                    if rel == entry["file"] and kind == entry["kind"]
                    and line.startswith(entry["line"])})
    return lines[0] if len(lines) == 1 else None


class RuntimeReferenceInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.found, cls.is_comment = scan(REPO_ROOT, cls.manifest)
        cls.expected = collections.Counter()
        cls.entries = {}
        cls.unresolved = []
        for entry in cls.manifest["references"]:
            line = resolve_line(entry, cls.found)
            if line is None:
                cls.unresolved.append((entry["file"], entry["line"], entry["kind"]))
                continue
            key = (entry["file"], line, entry["kind"])
            cls.expected[key] += int(entry.get("count", 1))
            cls.entries[key] = entry

    def test_prefix_entries_resolve(self):
        self.assertFalse(self.unresolved, "prefix entries matching no line, or "
                                          "more than one: " + json.dumps(self.unresolved))

    def test_entries_are_well_formed(self):
        seen = set()
        for entry in self.manifest["references"]:
            key = (entry["file"], entry["line"], entry["kind"])
            with self.subTest(entry=key):
                self.assertNotIn(key, seen, "duplicate entry; use 'count' instead")
                seen.add(key)
                self.assertIn(entry["kind"], KIND_PATTERNS)
                self.assertIn(entry.get("match", "exact"), {"exact", "prefix"})
                self.assertIn(entry["classification"], CLASSIFICATIONS)
                self.assertTrue(entry.get("purpose", "").strip(), "purpose is required")
                resolved = resolve_line(entry, self.found) or entry["line"]
                self.assertRegex(resolved, KIND_PATTERNS[entry["kind"]])
                if entry["classification"] == "pending-owner-change":
                    self.assertTrue(entry.get("owner", "").strip(), "owner is required")
                if entry["kind"] == "legacy-mutable-root":
                    self.assertIn(
                        entry["classification"], {"comment", "pending-owner-change"},
                        "executable references to in-checkout mutable roots must be "
                        "redirected, or tracked as pending with an owner")

    def test_every_reference_is_listed(self):
        missing = []
        for key, count in sorted(self.found.items()):
            if self.expected.get(key, 0) < count:
                rel, line, kind = key
                missing.append(json.dumps({
                    "file": rel, "line": line, "kind": kind,
                    "classification": "comment" if self.is_comment[key] else "runtime-link",
                    "purpose": "TODO", "count": count,
                }, ensure_ascii=False))
        self.assertFalse(missing, "unlisted runtime references:\n" + "\n".join(missing))

    def test_no_stale_entries(self):
        stale = [
            key for key, count in sorted(self.expected.items())
            if self.found.get(key, 0) != count
        ]
        self.assertFalse(stale, "manifest entries no longer match the tree "
                                "(update the line text or count, or remove them): "
                                + json.dumps(stale, ensure_ascii=False, indent=1))

    def test_comment_classification_matches_syntax(self):
        for key, entry in sorted(self.entries.items()):
            if key not in self.is_comment:
                continue  # reported by test_no_stale_entries
            rel = key[0]
            with self.subTest(entry=key):
                cls = entry["classification"]
                if cls == "documentation":
                    self.assertIn(os.path.splitext(rel)[1], DOC_SUFFIXES)
                elif cls == "comment":
                    self.assertTrue(self.is_comment[key], "classified as comment but executable")
                elif cls in EXECUTABLE_CLASSES:
                    self.assertFalse(self.is_comment[key], "executable class on a comment line")

    def test_scan_scope(self):
        """Everything is scanned except a short, justified exclude list."""
        scope = self.manifest["scan"]
        self.assertEqual(scope["roots"], ["."])
        try:
            listed = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others",
                 "--exclude-standard"], check=True, capture_output=True, text=True,
            ).stdout.split("\n")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        for entry in scope["exclude"]:
            with self.subTest(exclude=entry):
                self.assertIsInstance(entry, dict)
                self.assertTrue(entry.get("reason", "").strip(), "reason is required")
                self.assertTrue(
                    any(is_excluded(rel, [entry["pattern"]]) for rel in listed if rel),
                    "exclude pattern matches no file (stale)")

    def test_manifest_is_public_safe(self):
        text = MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"/home/[^/\s\"]+|/Users/[^/\s\"]+")


class RuntimeWriterAuditTests(unittest.TestCase):
    """Each retained writer keeps its redirection out of the checkout."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()

    def test_writers_are_well_formed(self):
        ids = set()
        for writer in self.manifest["writers"]:
            with self.subTest(writer=writer.get("id")):
                self.assertNotIn(writer["id"], ids)
                ids.add(writer["id"])
                for field in ("entrypoint", "before", "after", "status"):
                    self.assertTrue(str(writer.get(field, "")).strip(), field)
                self.assertIn(writer["status"], WRITER_STATUSES)
                if writer["status"] in {"installer-delegation", "pending-owner-change"}:
                    self.assertTrue(writer.get("owner", "").strip(), "owner is required")

    def test_managed_path_claims_match_manifest(self):
        """What a writer says about how a path is installed is the truth.

        Every "managed_paths" claim ({id: kind}) must match
        manifests/managed-paths.json, a writer whose after/owner text talks
        about how paths are installed must carry such claims, the text must
        agree with them ('copy' appears exactly when a claimed kind is copy),
        and no pending transition may be written as the current state.
        """
        with MANAGED_PATHS.open(encoding="utf-8") as handle:
            kinds = {e["id"]: e["kind"] for e in json.load(handle)["entries"]}
        claimed_any = False
        for writer in self.manifest["writers"]:
            text = " ".join(str(writer.get(k, "")) for k in ("after", "owner"))
            claims = writer.get("managed_paths")
            with self.subTest(writer=writer["id"]):
                self.assertNotRegex(" ".join(str(writer.get(k, "")) for k in
                                             ("before", "after", "owner", "notes")),
                                    _TRANSITION, "state the current layout, not a "
                                    "pending change")
                if claims is None:
                    self.assertNotRegex(text, _INSTALL_CLAIM,
                                        "describes how paths are installed; add "
                                        "managed_paths so it is checked")
                    continue
                claimed_any = True
                self.assertIsInstance(claims, dict)
                self.assertTrue(claims)
                for entry_id, kind in claims.items():
                    self.assertIn(entry_id, kinds, "unknown managed id")
                    self.assertEqual(kinds[entry_id], kind,
                                     f"{entry_id} is installed as {kinds[entry_id]!r}")
                self.assertEqual("'copy'" in writer["after"],
                                 "copy" in claims.values(),
                                 "after text and managed_paths disagree about copies")
        self.assertTrue(claimed_any)

    def test_rc_files_are_documented_as_symlinks(self):
        """TQ-6: the rc files are symlinks into the checkout; the inventory
        must not claim appends to them stay machine-local copies."""
        writers = {w["id"]: w for w in self.manifest["writers"]}
        rc = writers["shell-rc-appenders"]
        self.assertEqual(rc["status"], "in-checkout-accepted")
        self.assertEqual(set(rc["managed_paths"]), {"zshrc", "zshenv", "zprofile", "bashrc"})
        self.assertEqual(set(rc["managed_paths"].values()), {"symlink"})
        copies = {i for w in self.manifest["writers"]
                  for i, k in w.get("managed_paths", {}).items() if k == "copy"}
        self.assertEqual(copies, {"gitconfig", "pudb"})

    def test_generation_file_contract(self):
        """The generation file the reload hook reads is the documented one
        (XDG state, with the default for an unset or relative value)."""
        contract = self.manifest["path_contract"]["generation"]
        self.assertTrue(contract.startswith(
            "${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles/generation "))
        text = (REPO_ROOT / "zsh/zsh.d/dotfiles-reload.zsh").read_text(encoding="utf-8")
        for needle in ("if [[ ${XDG_STATE_HOME:-} == /* ]]; then",
                       "    state=$XDG_STATE_HOME\n",
                       "    state=$HOME/.local/state\n",
                       "_pd_reload_dir=$state/personal-dotfiles\n",
                       "_pd_reload_file=$_pd_reload_dir/generation\n"):
            self.assertIn(needle, text)
        writers = {w["id"]: w for w in self.manifest["writers"]}
        self.assertEqual(writers["zsh-reload-history-flush"]["status"], "outside-checkout")

    def test_shell_hook_contract(self):
        """The hook mark every hooked zsh holds is the file bin/dotfiles looks
        for, at the documented path; the old per-shell markers are gone."""
        contract = self.manifest["path_contract"]["shell_hook"]
        self.assertTrue(contract.startswith(
            "${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles/shell-hook "))
        self.assertNotIn("shell_markers", self.manifest["path_contract"])
        zsh = (REPO_ROOT / "zsh/zsh.d/dotfiles-reload.zsh").read_text(encoding="utf-8")
        self.assertIn("_pd_reload_hook_file=$_pd_reload_dir/shell-hook\n", zsh)
        cmd = (REPO_ROOT / "bin/dotfiles").read_text(encoding="utf-8")
        self.assertIn("value = os.environ.get('XDG_STATE_HOME', '')", cmd)
        self.assertIn("value = os.path.join(os.path.expanduser('~'), '.local', 'state')", cmd)
        self.assertIn("return os.path.join(_state_home(), 'personal-dotfiles', 'shell-hook')", cmd)
        self.assertIn("HOOK_FILE_SUFFIX = '/personal-dotfiles/shell-hook'", cmd)
        for text in (zsh, cmd):
            self.assertNotIn("_DOTFILES_RUNTIME_ROOT", text)
            self.assertNotIn("/shells", text)
        writers = {w["id"]: w for w in self.manifest["writers"]}
        self.assertEqual(writers["zsh-shell-hook"]["status"], "outside-checkout")
        self.assertNotIn("zsh-shell-markers", writers)

    def test_reload_handover_contract(self):
        """The hand-over a reloading zsh writes goes where the contract says:
        a private XDG_RUNTIME_DIR, else the state directory, never a fixed
        /run/user path; it never passes through the environment."""
        contract = self.manifest["path_contract"]["reload_handover"]
        self.assertTrue(contract.startswith("$XDG_RUNTIME_DIR/personal-dotfiles (0700) "))
        self.assertIn("${XDG_STATE_HOME:-$HOME/.local/state}/personal-dotfiles", contract)
        zsh = (REPO_ROOT / "zsh/zsh.d/dotfiles-reload.zsh").read_text(encoding="utf-8")
        for needle in ("REPLY=$XDG_RUNTIME_DIR/personal-dotfiles\n",
                       "REPLY=$_pd_reload_dir\n",
                       "file=$REPLY/.handover.$$\n",
                       "hist=${_pd_reload_wfile:h}/.hist.$$\n",
                       "!(st[mode] & 8#077)"):
            self.assertIn(needle, zsh)
        self.assertNotIn("/run/user", zsh)
        # Only descriptor numbers and the pid go through the environment.
        exported = {name for line in re.findall(r"^\s*export .*$", zsh, re.M)
                    for name in re.findall(r"\b(_PD_RELOAD_[A-Z]+)=", line)}
        self.assertEqual(exported, {"_PD_RELOAD_PID", "_PD_RELOAD_FD", "_PD_RELOAD_HOOKFD"})
        rc = (REPO_ROOT / "zsh/zshrc").read_text(encoding="utf-8")
        self.assertTrue(rc.rstrip().splitlines()[-1].startswith("# vim:"))
        self.assertIn("|| _pd_reload_startup\n", rc)
        # The new process takes the hand-over first thing in zshenv, before
        # the startup can run any program.
        env = (REPO_ROOT / "zsh/zshenv").read_text(encoding="utf-8")
        early = "_pd_reload_early=1 source ${${(%):-%x}:A:h}/zsh.d/dotfiles-reload.zsh"
        self.assertIn(early, env)
        code = [l.strip() for l in env.splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(code[0], "if [[ -n ${_PD_RELOAD_PID:-} ]]; then")
        self.assertEqual(code[3], early)

    def test_writer_markers(self):
        for writer in self.manifest["writers"]:
            for check in writer.get("checks", []):
                path = REPO_ROOT / check["file"]
                with self.subTest(writer=writer["id"], file=check["file"]):
                    text = path.read_text(encoding="utf-8")
                    for needle in check.get("require", []):
                        self.assertRegex(text, needle)
                    for needle in check.get("forbid", []):
                        self.assertNotRegex(text, needle)


class CommentDetectionTests(unittest.TestCase):
    def test_hash_comments(self):
        lines = list(classify_lines("zsh/zshrc", "# see ~/.dotfiles\necho ~/.dotfiles\n"))
        self.assertEqual(lines, [("# see ~/.dotfiles", True), ("echo ~/.dotfiles", False)])

    def test_vim_comments(self):
        lines = list(classify_lines("vim/vimrc", '" see ~/.dotfiles\ncd ~/.dotfiles\n'))
        self.assertEqual([c for _, c in lines], [True, False])

    def test_lua_block_comment(self):
        text = "--[[\n  nvim -u ~/.dotfiles/x\n]]\nlocal x = '~/.dotfiles'\n"
        self.assertEqual([c for _, c in classify_lines("a.lua", text)], [True, True, True, False])

    def test_lua_single_line_block(self):
        text = "--[[ one line ]]\nprint(1)\n"
        self.assertEqual([c for _, c in classify_lines("a.lua", text)], [True, False])

    def test_python_docstrings(self):
        text = '#!/usr/bin/env python3\n"""Doc ~/.dotfiles\nmore\n"""\nx = "~/.dotfiles"\n'
        self.assertEqual([c for _, c in classify_lines("bin/tool", text)],
                         [True, True, True, True, False])
        self.assertEqual([c for _, c in classify_lines("a.sh", '"""\nx\n')], [False, False])
        text = "x = '''\n~/.dotfiles\n''' % y\nz = '~/.dotfiles'\n"
        self.assertEqual([c for _, c in classify_lines("a.py", text)], [False, False, False, False])

    def test_excludes(self):
        patterns = ["tests/fixtures/", "*.md"]
        self.assertTrue(is_excluded("tests/fixtures/a/b.txt", patterns))
        self.assertTrue(is_excluded("docs/SUPPORT.md", patterns))
        self.assertTrue(is_excluded("README.md", patterns))
        self.assertFalse(is_excluded("tests/unit/test_x.py", patterns))

    def test_patterns(self):
        link = KIND_PATTERNS["link"]
        self.assertRegex("$HOME/.dotfiles/bin", link)
        self.assertRegex("${HOME}/.dotfiles", link)
        self.assertRegex("ExecStart=%h/.dotfiles/tmux/resurrect-save", link)
        self.assertNotRegex("~/.dotfiles.bak", link)
        ident = KIND_PATTERNS["identifier"]
        self.assertRegex("DOTFILES_UPDATE=1", ident)
        self.assertNotRegex("personal-dotfiles_x", ident)
        self.assertRegex("source $DOTVIM/init.lua", ident)
        self.assertRegex("cd ${DOTFILES}/bin", ident)
        self.assertRegex("export DOTFILES=~/x", ident)
        self.assertRegex("See $DOTVIM/lua", ident)
        self.assertNotRegex("personal-dotfiles", ident)
        self.assertNotRegex("DOTFILESX", ident)
        legacy = KIND_PATTERNS["legacy-mutable-root"]
        self.assertRegex('ls "$HOME/.vim/plugged"', legacy)
        self.assertRegex("~/.tmux/plugins/tpm/tpm", legacy)
        self.assertNotRegex("$XDG_DATA_HOME/vim/plugged", legacy)
        self.assertRegex('d="$HOME/.tmux/resurrect"', legacy)
        self.assertNotRegex("source-file -q ~/.tmux/resurrect.conf", legacy)
        self.assertNotRegex("~/.tmux/resurrect-save", legacy)


if __name__ == "__main__":
    unittest.main()
