"""Unit tests for installer.packages with a fake runner and a fake fetch.

Nothing here touches apt, sudo, the network or the real home: apt/dpkg/sudo/
gpgv/npm and the Claude vendor installer are scripted by :class:`FakeRunner`,
and every download is served from in-memory fixtures by :class:`FakeFetch`.
Only fixture shell scripts extracted into the temporary prefix are executed.
"""

from __future__ import annotations

import base64
import hashlib
import io
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

from installer import downloads as dl  # noqa: E402
from installer import packages as pk  # noqa: E402
from installer.platform import Platform, Target  # noqa: E402

BASE = "https://example.invalid"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def script(text: str) -> bytes:
    return f"#!/bin/sh\n{text}\n".encode()


def make_tar(entries, kind: str = "gz") -> bytes:
    buf = io.BytesIO()
    items = entries.items() if isinstance(entries, dict) else entries
    with tarfile.open(fileobj=buf, mode=f"w:{kind}") as tar:
        for name, value in items:
            info = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                info.type = tarfile.SYMTYPE if value[0] == "sym" else tarfile.LNKTYPE
                info.linkname = value[1]
                tar.addfile(info)
            else:
                info.size = len(value)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(value))
    return buf.getvalue()


def key_material(seed: bytes) -> tuple[bytes, str]:
    body = b"\x04\x5f\x00\x00\x00\x16" + hashlib.sha256(seed).digest()
    packet = bytes([0xC6, len(body)]) + body
    b64 = base64.b64encode(packet).decode()
    crc = base64.b64encode(dl.crc24(packet).to_bytes(3, "big")).decode()
    armored = (f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n{b64}\n={crc}\n"
               "-----END PGP PUBLIC KEY BLOCK-----\n").encode()
    return armored, dl.key_fingerprint(body)


NODE_KEY, NODE_FPR = key_material(b"node-releaser")
CLAUDE_KEY, CLAUDE_FPR = key_material(b"claude")
CHROME_KEY, CHROME_FPR = key_material(b"chrome")


class FakeFetch:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.calls: list[str] = []

    def __call__(self, url, dest, *, timeout):
        self.calls.append(url)
        if url not in self.files:
            raise dl.DownloadError(f"{url}: HTTP 404")
        Path(dest).write_bytes(self.files[url])
        return dest


class FakeRunner:
    def __init__(self, sandbox: Path):
        self.sandbox = str(sandbox)
        self.calls: list[tuple[list[str], bool]] = []
        self.installed: dict[str, str] = {}
        self.sudo_ok = True
        self.sudo_prompt_ok = False
        self.sim_extra: list[str] = []
        self.sim_remove: list[str] = []
        self.gpgv_rc = 0
        self.gpgv_signer: str | None = None  # default: the keyring's own primary
        self.locales = "C.utf8\nen_US.utf8\n"
        self.which_map: dict[str, str | None] = {}
        self.claude_payload: bytes | None = None
        self.pip_done = False

    def which(self, name):
        if name in self.which_map:
            return self.which_map[name]
        return f"/usr/bin/{name}"

    def names(self):
        return [argv for argv, _ in self.calls]

    def ran(self, *needle) -> bool:
        return any(all(n in argv for n in needle) for argv in self.names())

    @staticmethod
    def ok(argv, out=b"", rc=0, err=b""):
        return subprocess.CompletedProcess(argv, rc, out, err)

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None, read_only=False):
        argv = [str(a) for a in argv]
        self.calls.append((argv, read_only))
        head = argv[0]
        if head == "dpkg-query":
            lines = [f"{n}\tii \t{self.installed[n]}" for n in argv[3:] if n in self.installed]
            return self.ok(argv, ("\n".join(lines) + "\n").encode(), 0 if len(lines) == len(argv) - 3 else 1)
        if argv[:2] == ["locale", "-a"]:
            return self.ok(argv, self.locales.encode())
        if argv[:3] == ["sudo", "-n", "true"]:
            return self.ok(argv, rc=0 if self.sudo_ok else 1, err=b"sudo: a password is required")
        if argv[:2] == ["sudo", "-v"]:
            if self.sudo_prompt_ok:
                self.sudo_ok = True
            return self.ok(argv, rc=0 if self.sudo_prompt_ok else 1, err=b"sudo: 3 incorrect password attempts")
        if head == "apt-get" and "--simulate" in argv:
            pkgs = [a for a in argv[4:] if not a.startswith("-")]
            out = "".join(f"Inst {p} (1.0 Ubuntu:24.04/noble [amd64])\n" for p in pkgs + self.sim_extra)
            out += "".join(f"Remv {p} [1.0]\n" for p in self.sim_remove)
            return self.ok(argv, out.encode())
        if argv[:3] == ["sudo", "apt-get", "update"]:
            return self.ok(argv)
        if argv[:2] == ["sudo", "env"] and "install" in argv:
            for p in argv[argv.index("--no-install-recommends") + 1:]:
                if not p.startswith("-") and not p.startswith("Dpkg"):
                    self.installed[p] = "9.9-test"
            return self.ok(argv)
        if argv[:2] == ["sudo", "install"]:
            shutil.copyfile(argv[-2], argv[-1])
            return self.ok(argv)
        if argv[:2] == ["sudo", "locale-gen"]:
            self.locales += "en_US.utf8\n"
            return self.ok(argv)
        if head.endswith("gpgv"):
            keyring = Path(argv[argv.index("--keyring") + 1]).read_bytes()
            signer = self.gpgv_signer or dl.primary_fingerprints(keyring)[0]
            status = (f"[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG 0000 Fixture\n"
                      f"[GNUPG:] VALIDSIG {signer} 2026-01-01 0 0 4 0 22 10 00 {signer}\n")
            if self.gpgv_rc:
                return self.ok(argv, b"[GNUPG:] BADSIG 0000 Fixture\n", 1, b"gpgv: BAD signature")
            return self.ok(argv, status.encode())
        if head.endswith("/npm") and "install" in argv:
            prefix = Path(argv[argv.index("--prefix") + 1])
            assert env["PATH"].split(":")[0].endswith("personal-dotfiles/bin"), env["PATH"]
            (prefix / "bin").mkdir(parents=True, exist_ok=True)
            omx = prefix / "bin" / "omx"
            omx.write_bytes(script("echo 'oh-my-codex v0.21.6'"))
            omx.chmod(0o755)
            return self.ok(argv)
        if len(argv) == 3 and argv[1] == "install" and Path(head).name == "claude":
            home = Path(env["HOME"])
            versions = home / ".local/share/claude/versions"
            versions.mkdir(parents=True, exist_ok=True)
            binary = versions / argv[2]
            binary.write_bytes(self.claude_payload if self.claude_payload is not None
                               else Path(head).read_bytes())
            binary.chmod(0o755)
            launcher = home / ".local/bin/claude"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(launcher):
                launcher.unlink()
            launcher.symlink_to(binary)
            return self.ok(argv)
        if len(argv) >= 3 and argv[1:3] == ["-m", "venv"]:
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_bytes(b"")
            return self.ok(argv)
        if head.endswith("/bin/python") and argv[1:4] == ["-m", "pip", "install"]:
            self.pip_done = True
            return self.ok(argv)
        if head.endswith("/bin/python") and argv[1] == "-c":
            return self.ok(argv, b"0.6.0\n") if self.pip_done else self.ok(argv, rc=1)
        if head.startswith(self.sandbox) and os.path.isfile(head):
            return subprocess.run(argv, capture_output=True, env=env, timeout=timeout, check=False)
        raise AssertionError(f"unexpected command {argv}")


class Fixture:
    """Temporary HOME, manifests and artifact server for one test."""

    def __init__(self, test: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp(prefix="pkgtest-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        home = self.root / "home"
        home.mkdir()
        self.target = Target(os.getuid(), os.getgid(), "fixture", home, home / ".local/share",
                             home / ".local/state", home / ".config", home / ".cache")
        self.system = self.root / "system"
        self.system.mkdir()
        self.manifests = self.root / "manifests"
        self.manifests.mkdir()
        self.runner = FakeRunner(self.root)
        self.files: dict[str, bytes] = {}
        self._build()
        self.fetch = FakeFetch(self.files)

    def _build(self):
        node = {}
        tools: dict = {}
        for arch, tag in (("amd64", "x64"), ("arm64", "arm64")):
            top = f"node-v24.21.0-linux-{tag}"
            data = make_tar({
                f"{top}/bin/node": script(f"echo v24.21.0; echo {arch} >&2"),
                f"{top}/lib/node_modules/npm/bin/npm-cli.js": script("echo 11.19.0"),
                f"{top}/bin/npm": ("sym", "../lib/node_modules/npm/bin/npm-cli.js"),
                f"{top}/bin/npx": ("sym", "../lib/node_modules/npm/bin/npm-cli.js"),
            }, "xz")
            url = f"{BASE}/node/{top}.tar.xz"
            self.files[url] = data
            node[arch] = {"url": url, "sha256": sha(data), "archive": "tar.xz", "strip_components": 1}
        sums = "".join(f"{v['sha256']}  {Path(v['url']).name}\n" for v in node.values()).encode()
        self.files[f"{BASE}/node/SHASUMS256.txt"] = sums
        self.files[f"{BASE}/node/SHASUMS256.txt.sig"] = b"detached-signature"
        self.files[f"{BASE}/keys/node.asc"] = NODE_KEY
        tools["node"] = {
            "version": "24.21.0", "artifacts": node,
            "integrity": {"checksum_source": {"type": "signed-checksum-file",
                                              "url": f"{BASE}/node/SHASUMS256.txt", "sha256": sha(sums),
                                              "signature_url": f"{BASE}/node/SHASUMS256.txt.sig"},
                          "signer_fingerprints": [NODE_FPR], "signer_key_url": f"{BASE}/keys/node.asc"},
            "install": {"method": "extract"}, "prefix": "{prefix_root}/node/{version}",
            "links": {"node": "bin/node", "npm": "bin/npm", "npx": "bin/npx"},
            "verify": [{"argv": ["{bin_dir}/node", "--version"], "stdout": "^v24\\.21\\.0$"},
                       {"argv": ["{bin_dir}/npm", "--version"], "stdout": "^11\\.19\\.0$"}],
        }
        fzf = {}
        for arch in ("amd64", "arm64"):
            data = make_tar({"fzf": script("echo '0.74.4 (fixture)'")})
            url = f"{BASE}/fzf/fzf-0.74.4-linux_{arch}.tar.gz"
            self.files[url] = data
            fzf[arch] = {"url": url, "sha256": sha(data), "archive": "tar.gz", "strip_components": 0}
        fsums = "".join(f"{v['sha256']}  {Path(v['url']).name}\n" for v in fzf.values()).encode()
        self.files[f"{BASE}/fzf/checksums.txt"] = fsums
        preview = script("echo preview")
        self.files[f"{BASE}/fzf/fzf-preview.sh"] = preview
        tools["fzf"] = {
            "version": "0.74.4", "artifacts": fzf,
            "extra_files": {"fzf-preview.sh": {"url": f"{BASE}/fzf/fzf-preview.sh", "sha256": sha(preview),
                                               "dest": "bin/fzf-preview.sh", "mode": "0755"}},
            "integrity": {"checksum_source": {"type": "checksum-file", "url": f"{BASE}/fzf/checksums.txt",
                                              "sha256": sha(fsums)}},
            "install": {"method": "extract"}, "prefix": "{prefix_root}/fzf/{version}",
            "links": {"fzf": "fzf", "fzf-preview.sh": "bin/fzf-preview.sh"},
            "verify": [{"argv": ["{bin_dir}/fzf", "--version"], "stdout": "^0\\.74\\.4 "}],
        }
        nv = make_tar({"nvim-linux/bin/nvim": script("echo 'NVIM v0.12.5'")})
        self.files[f"{BASE}/nvim.tar.gz"] = nv
        tools["neovim"] = {
            "version": "0.12.5",
            "artifacts": {"amd64": {"url": f"{BASE}/nvim.tar.gz", "sha256": sha(nv), "archive": "tar.gz",
                                    "strip_components": 1}},
            "integrity": {"checksum_source": {"type": "github-release-asset-digest"}},
            "install": {"method": "extract"}, "prefix": "{prefix_root}/neovim/{version}",
            "links": {"nvim": "bin/nvim"},
            "verify": [{"argv": ["{bin_dir}/nvim", "--version"], "stdout": "^NVIM v0\\.12\\.5"}],
        }
        claude_bin = script("echo '2.1.274 (Claude Code)'")
        self.claude_bin = claude_bin
        cmanifest = json.dumps({"version": "2.1.274", "platforms": {
            "linux-x64": {"checksum": sha(claude_bin), "size": len(claude_bin)}}}).encode()
        self.files[f"{BASE}/claude/manifest.json"] = cmanifest
        self.files[f"{BASE}/claude/manifest.json.sig"] = b"sig"
        self.files[f"{BASE}/claude/key.asc"] = CLAUDE_KEY
        self.files[f"{BASE}/claude/linux-x64/claude"] = claude_bin
        tools["claude-code"] = {
            "version": "2.1.274",
            "artifacts": {"amd64": {"url": f"{BASE}/claude/linux-x64/claude", "sha256": sha(claude_bin),
                                    "size": len(claude_bin), "archive": "none", "platform": "linux-x64"}},
            "integrity": {"checksum_source": {"type": "signed-manifest", "url": f"{BASE}/claude/manifest.json",
                                              "sha256": sha(cmanifest),
                                              "signature_url": f"{BASE}/claude/manifest.json.sig"},
                          "signer_fingerprints": [CLAUDE_FPR], "signer_key_url": f"{BASE}/claude/key.asc"},
            "install": {"method": "vendor-self-install", "argv": ["{download}", "install", "{version}"]},
            "vendor_layout": {"binary": "{home}/.local/share/claude/versions/{version}",
                              "launcher": "{home}/.local/bin/claude"},
            "verify": [{"argv": ["{home}/.local/bin/claude", "--version"],
                        "stdout": "^2\\.1\\.274 \\(Claude Code\\)$"}],
        }
        tgz = b"npm-tarball-bytes"
        self.files[f"{BASE}/omx.tgz"] = tgz
        tools["oh-my-codex"] = {
            "version": "0.21.6",
            "artifacts": {"any": {"url": f"{BASE}/omx.tgz", "sha256": sha(tgz),
                                  "sha512": "sha512-" + base64.b64encode(hashlib.sha512(tgz).digest()).decode(),
                                  "archive": "npm-tarball"}},
            "integrity": {"checksum_source": {"type": "npm-registry-integrity"}},
            "install": {"method": "npm-global-prefix", "requires_tool": "node",
                        "argv": ["{bin_dir}/npm", "install", "--global", "--prefix", "{prefix}", "{download}"],
                        "env": {"npm_config_update_notifier": "false"}},
            "prefix": "{prefix_root}/oh-my-codex/{version}", "links": {"omx": "bin/omx"},
            "verify": [{"argv": ["{bin_dir}/omx", "--version"], "stdout": "^oh-my-codex v0\\.21\\.6$"}],
        }
        font = make_tar({"JetBrainsMonoNerdFont-Regular.ttf": b"ttf"}, "xz")
        self.files[f"{BASE}/font.tar.xz"] = font
        tools["nerd-font"] = {
            "version": "3.5.1", "group": "gui",
            "artifacts": {"any": {"url": f"{BASE}/font.tar.xz", "sha256": sha(font), "archive": "tar.xz"}},
            "integrity": {"checksum_source": {"type": "github-release-asset-digest"}},
            "install": {"method": "extract", "post": ["fc-cache", "-f", "{prefix}"]},
            "prefix": "{data_home}/fonts/personal-dotfiles/JBM-{version}", "links": {},
            "verify": [{"argv": ["fc-list", ":family=JetBrainsMono Nerd Font Mono", "family"],
                        "stdout": "JetBrainsMono"}],
        }
        self.tools = {
            "schema": 1, "prefix_root": "{data_home}/personal-dotfiles/tools",
            "bin_dir": "{data_home}/personal-dotfiles/bin",
            "home_links": ["node", "npm", "npx", "nvim", "fzf", "codex", "omx"],
            "python_venvs": {"nvim": {"path": "{data_home}/personal-dotfiles/venvs/nvim",
                                      "requirements": ["pynvim"], "only_binary": ["greenlet", "msgpack"]}},
            "tools": tools,
        }
        self.files[f"{BASE}/chrome.pub"] = CHROME_KEY
        self.fdfind = self.system / "fdfind"
        self.fdfind.write_bytes(script("echo fd"))
        self.packages = {
            "schema": 1,
            "apt_policy": {"install_recommends": False,
                           "install_command": ["apt-get", "install", "--yes", "--no-install-recommends"],
                           "simulate_command": ["apt-get", "--simulate", "install", "--no-install-recommends"]},
            "forbidden": {"patterns": ["^(gcc|g\\+\\+|cpp|clang)(-[0-9]+)?$", "^build-essential$",
                                       "^(nodejs|npm)$"]},
            "groups": [
                {"id": "shell-core", "packages": [{"name": "zsh"}, {"name": "locales"}, {"name": "fd-find"}]},
                {"id": "gui", "packages": [{"name": "gnome-terminal"},
                                           {"name": "google-chrome-stable", "repository": "google-chrome"}]},
            ],
            "repositories": {"google-chrome": {
                "key_url": f"{BASE}/chrome.pub", "key_primary_fingerprint": CHROME_FPR,
                "keyring_path": str(self.system / "google-chrome.gpg"),
                "sources_path": str(self.system / "google-chrome.sources"),
                "sources_deb822": {"Types": "deb", "URIs": "https://dl.google.com/linux/chrome-stable/deb/",
                                   "Suites": "stable", "Components": "main",
                                   "Architectures": "{architecture}",
                                   "Signed-By": str(self.system / "google-chrome.gpg")},
                "package_architectures": ["amd64", "arm64"]}},
            "shims": [{"name": "fd-fixture-shim", "target": str(self.fdfind), "only_if_missing": True}],
            "system_steps": [{"id": "locale-en-us-utf8", "check": ["locale", "-a"],
                              "present_if_output_matches": "^en_US\\.utf8$",
                              "command": ["locale-gen", "en_US.UTF-8"]}],
        }
        self.write_manifests()

    def write_manifests(self):
        (self.manifests / "tools.json").write_text(json.dumps(self.tools))
        (self.manifests / "packages.json").write_text(json.dumps(self.packages))

    @property
    def bin_dir(self) -> Path:
        return self.target.data_home / "personal-dotfiles" / "bin"

    @property
    def tools_root(self) -> Path:
        return self.target.data_home / "personal-dotfiles" / "tools"

    def run(self, arch="amd64", **kw):
        kw.setdefault("fetch", self.fetch)
        return pk.run_packages_phase(self.target, Platform("ubuntu", "24.04", arch), self.runner,
                                     manifest_dir=self.manifests, log=lambda m: None, **kw)


class AptTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.runner = self.fx.runner

    def install_calls(self):
        return [a for a in self.runner.names() if a[:2] == ["sudo", "env"] and "install" in a]

    def test_happy_path_simulates_then_installs_and_records_versions(self):
        self.runner.installed = {"zsh": "5.9-6"}
        self.runner.locales = "C.utf8\n"
        result = self.fx.run(groups=("shell-core", "gui"), only=["shell-core", "gui"])
        apt = result["details"]["apt"]
        self.assertEqual(apt["status"], "PASS", result["reasons"])
        self.assertIs(apt["transactional"], False)
        self.assertEqual(apt["missing"], ["locales", "fd-find", "gnome-terminal", "google-chrome-stable"])
        names = self.runner.names()
        order = [next(i for i, a in enumerate(names) if cond(a)) for cond in (
            lambda a: a[:2] == ["sudo", "install"],
            lambda a: a[:3] == ["sudo", "apt-get", "update"],
            lambda a: "--simulate" in a,
            lambda a: a[:2] == ["sudo", "env"])]
        self.assertEqual(order, sorted(order))
        sim = next(c for c in self.runner.calls if "--simulate" in c[0])
        self.assertTrue(sim[1], "simulation must be read-only")
        self.assertIn("--no-install-recommends", sim[0])
        self.assertIn("--no-install-recommends", self.install_calls()[0])
        self.assertEqual(apt["versions"]["google-chrome-stable"], "9.9-test")
        self.assertEqual(apt["locale"], "generated")
        keyring = Path(self.fx.packages["repositories"]["google-chrome"]["keyring_path"])
        self.assertEqual(dl.primary_fingerprints(keyring.read_bytes()), [CHROME_FPR])
        sources = Path(self.fx.packages["repositories"]["google-chrome"]["sources_path"]).read_text()
        self.assertIn("Architectures: amd64\n", sources)
        self.assertIn(f"Signed-By: {keyring}\n", sources)
        self.assertEqual(os.readlink(self.fx.bin_dir / "fd-fixture-shim"), str(self.fx.fdfind))

    def test_forbidden_pattern_aborts_before_any_install(self):
        self.runner.sim_extra = ["gcc-13", "cpp"]
        result = self.fx.run(only=["shell-core"])
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("gcc-13" in r and "cpp" in r for r in result["reasons"]), result["reasons"])
        self.assertEqual(self.install_calls(), [])
        self.assertEqual(self.runner.installed, {})

    def test_removal_in_simulation_aborts(self):
        self.runner.sim_remove = ["ubuntu-desktop"]
        result = self.fx.run(only=["shell-core"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.install_calls(), [])

    def test_chrome_key_fingerprint_mismatch_aborts_before_apt(self):
        self.fx.packages["repositories"]["google-chrome"]["key_primary_fingerprint"] = "A" * 40
        self.fx.write_manifests()
        result = self.fx.run(only=["gui"])
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(self.runner.ran("sudo", "install"))
        self.assertFalse(self.runner.ran("update"))
        self.assertEqual(self.install_calls(), [])

    def test_sudo_refusal_fails_with_actionable_reason(self):
        self.runner.sudo_ok = False
        result = self.fx.run(only=["shell-core"])
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn("sudo", reason)
        self.assertIn("apt-get install --yes --no-install-recommends zsh", reason)
        self.assertTrue(self.runner.ran("sudo", "-v"))
        self.assertFalse(self.runner.ran("update"))
        self.assertEqual(self.install_calls(), [])

    def test_sudo_missing(self):
        self.runner.which_map["sudo"] = None
        result = self.fx.run(only=["shell-core"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("sudo is not installed", " ".join(result["reasons"]))

    def test_nothing_missing_needs_no_sudo(self):
        self.runner.installed = {"zsh": "1", "locales": "1", "fd-find": "1"}
        self.runner.sudo_ok = False
        result = self.fx.run(only=["shell-core"])
        self.assertEqual(result["details"]["apt"]["status"], "PASS")
        self.assertFalse(self.runner.ran("sudo", "-n", "true"))

    def test_dry_run_only_runs_read_only_commands(self):
        self.runner.locales = "C.utf8\n"
        result = self.fx.run(dry_run=True)
        self.assertEqual(result["status"], "SKIPPED", result["reasons"])
        mutating = [a for a, ro in self.runner.calls if not ro]
        self.assertEqual(mutating, [])
        apt = result["details"]["apt"]
        self.assertIn("google-chrome-stable", apt["unsimulated"])
        self.assertEqual(apt["closure"]["forbidden"], [])
        self.assertEqual(self.fx.fetch.calls, [])
        self.assertFalse(self.fx.tools_root.exists())
        self.assertFalse(self.fx.bin_dir.exists())

    def test_dry_run_still_reports_forbidden_closure(self):
        self.runner.sim_extra = ["build-essential"]
        result = self.fx.run(dry_run=True, only=["shell-core"])
        self.assertEqual(result["status"], "FAIL")

    def test_unsupported_architecture_is_rejected_before_anything(self):
        result = self.fx.run(arch="riscv64")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("riscv64", result["reasons"][0])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.fx.fetch.calls, [])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.runner = self.fx.runner

    def test_node_fzf_install_verify_and_link(self):
        result = self.fx.run(only=["node", "fzf"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        node_prefix = self.fx.tools_root / "node" / "24.21.0"
        self.assertEqual(os.readlink(self.fx.bin_dir / "node"), str(node_prefix / "bin/node"))
        self.assertTrue((node_prefix / pk.MARKER_NAME).is_file())
        self.assertEqual(result["details"]["tools"]["node"]["verify"], ["v24.21.0", "11.19.0"])
        preview = self.fx.bin_dir / "fzf-preview.sh"
        self.assertTrue(os.access(preview, os.X_OK))
        links = {e["id"]: e for e in result["details"]["links"]}
        self.assertEqual(sorted(links), ["tool-link-fzf", "tool-link-node", "tool-link-npm", "tool-link-npx"])
        self.assertEqual(links["tool-link-node"]["dest"], str(self.fx.target.home / ".local/bin/node"))
        self.assertEqual(links["tool-link-node"]["link_text"], str(self.fx.bin_dir / "node"))
        self.assertFalse((self.fx.target.home / ".local/bin").exists(), "transaction owns ~/.local/bin")
        entries = pk.link_requests(result["details"])
        self.assertEqual({e.kind for e in entries}, {"symlink"})
        gpgv = next(a for a in self.runner.names() if a[0].endswith("gpgv"))
        self.assertIn("--keyring", gpgv)
        self.assertIn("--homedir", gpgv)

    def test_idempotent_rerun_downloads_nothing(self):
        first = self.fx.run(only=["node", "fzf", "neovim", "oh-my-codex", "claude-code"])
        self.assertEqual(first["status"], "PASS", first["reasons"])
        downloaded = len(self.fx.fetch.calls)
        self.assertGreater(downloaded, 0)
        second = self.fx.run(only=["node", "fzf", "neovim", "oh-my-codex", "claude-code"])
        self.assertEqual(second["status"], "PASS", second["reasons"])
        self.assertEqual(len(self.fx.fetch.calls), downloaded)
        self.assertTrue(all(not t["downloaded"] for t in second["details"]["tools"].values()))
        self.assertEqual(os.readlink(self.fx.bin_dir / "omx"),
                         str(self.fx.tools_root / "oh-my-codex/0.21.6/bin/omx"))

    def test_checksum_mismatch_fails_closed(self):
        url = self.fx.tools["tools"]["node"]["artifacts"]["amd64"]["url"]
        self.fx.fetch.files[url] = self.fx.fetch.files[url] + b"tampered"
        result = self.fx.run(only=["oh-my-codex"])
        self.assertEqual(result["status"], "FAIL")
        tools = result["details"]["tools"]
        self.assertIn("sha256", tools["node"]["reason"])
        self.assertIn("requires node", tools["oh-my-codex"]["reason"])
        self.assertFalse((self.fx.tools_root / "node" / "24.21.0").exists())
        self.assertFalse(os.path.lexists(self.fx.bin_dir / "node"))
        self.assertEqual(result["details"]["links"], [])
        self.assertFalse(any((self.fx.tools_root / "node").glob("*")))

    def test_checksum_file_entry_mismatch_fails_before_archive_download(self):
        self.fx.fetch.files[f"{BASE}/fzf/checksums.txt"] = b"0" * 64 + b"  other\n"
        result = self.fx.run(only=["fzf"])
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn(self.fx.tools["tools"]["fzf"]["artifacts"]["amd64"]["url"], self.fx.fetch.calls)

    def test_signature_failure_fails_closed(self):
        self.runner.gpgv_rc = 1
        result = self.fx.run(only=["node"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("gpgv", result["details"]["tools"]["node"]["reason"])
        self.assertNotIn(self.fx.tools["tools"]["node"]["artifacts"]["amd64"]["url"], self.fx.fetch.calls)
        self.assertFalse(os.path.lexists(self.fx.bin_dir / "node"))

    def test_signature_from_unpinned_key_fails(self):
        self.runner.gpgv_signer = "B" * 40
        result = self.fx.run(only=["node"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("not pinned", result["details"]["tools"]["node"]["reason"])

    def test_signer_key_fingerprint_mismatch_fails_before_gpgv(self):
        self.fx.tools["tools"]["node"]["integrity"]["signer_fingerprints"] = ["C" * 40]
        self.fx.write_manifests()
        result = self.fx.run(only=["node"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fingerprint", result["details"]["tools"]["node"]["reason"])
        self.assertFalse(any(a[0].endswith("gpgv") for a in self.runner.names()))

    def test_per_architecture_artifact_selection(self):
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                fx = Fixture(self)
                result = fx.run(arch=arch, only=["node", "fzf"])
                self.assertEqual(result["status"], "PASS", result["reasons"])
                self.assertIn(fx.tools["tools"]["node"]["artifacts"][arch]["url"], fx.fetch.calls)
                other = "arm64" if arch == "amd64" else "amd64"
                self.assertNotIn(fx.tools["tools"]["node"]["artifacts"][other]["url"], fx.fetch.calls)
                self.assertIn(fx.tools["tools"]["fzf"]["artifacts"][arch]["url"], fx.fetch.calls)
                self.assertNotIn(fx.tools["tools"]["fzf"]["artifacts"][other]["url"], fx.fetch.calls)

    def test_missing_artifact_for_architecture_fails(self):
        result = self.fx.run(arch="arm64", only=["neovim"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("arm64", result["details"]["tools"]["neovim"]["reason"])

    def test_verify_failure_removes_fresh_prefix(self):
        nv = make_tar({"nvim-linux/bin/nvim": script("echo 'NVIM v0.11.0'")})
        self.fx.fetch.files[f"{BASE}/nvim.tar.gz"] = nv
        self.fx.tools["tools"]["neovim"]["artifacts"]["amd64"]["sha256"] = sha(nv)
        self.fx.write_manifests()
        result = self.fx.run(only=["neovim"])
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse((self.fx.tools_root / "neovim" / "0.12.5").exists())
        self.assertFalse(os.path.lexists(self.fx.bin_dir / "nvim"))

    def test_claude_vendor_install_is_checked_against_pin(self):
        result = self.fx.run(only=["claude-code"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        claude = result["details"]["tools"]["claude-code"]
        self.assertEqual(claude["verify"], ["2.1.274 (Claude Code)"])
        self.assertTrue(any(a[0].endswith("gpgv") for a in self.runner.names()))

    def test_claude_vendor_install_with_wrong_binary_fails(self):
        self.runner.claude_payload = b"something else"
        result = self.fx.run(only=["claude-code"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("does not match", result["details"]["tools"]["claude-code"]["reason"])

    def test_claude_signed_manifest_checksum_mismatch(self):
        data = json.dumps({"platforms": {"linux-x64": {"checksum": "0" * 64}}}).encode()
        self.fx.fetch.files[f"{BASE}/claude/manifest.json"] = data
        self.fx.tools["tools"]["claude-code"]["integrity"]["checksum_source"]["sha256"] = sha(data)
        self.fx.write_manifests()
        result = self.fx.run(only=["claude-code"])
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn(f"{BASE}/claude/linux-x64/claude", self.fx.fetch.calls)

    def test_font_needs_fc_cache(self):
        self.runner.which_map["fc-cache"] = None
        result = self.fx.run(only=["nerd-font"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fc-cache", result["details"]["tools"]["nerd-font"]["reason"])

    def test_nvim_venv_created_with_pip_inside_only(self):
        result = self.fx.run(only=["neovim"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        python = result["details"]["python"]
        venv = self.fx.target.data_home / "personal-dotfiles/venvs/nvim"
        self.assertEqual(python["python3_host_prog"], str(venv / "bin/python"))
        self.assertEqual(python["pynvim"], "0.6.0")
        pip = next(a for a in self.runner.names() if "pip" in a)
        self.assertEqual(pip[0], str(venv / "bin/python"))
        self.assertNotIn("--break-system-packages", pip)
        self.assertNotIn("--user", pip)
        again = self.fx.run(only=["neovim"])
        self.assertFalse(again["details"]["python"]["created"])

    def test_real_manifests_cover_both_architectures(self):
        _, tools = pk.load_manifests()
        for tool_id, spec in tools["tools"].items():
            arts = spec["artifacts"]
            self.assertTrue("any" in arts or {"amd64", "arm64"} <= set(arts), tool_id)
        self.assertEqual(tools["tools"]["oh-my-codex"]["prefix"], "{prefix_root}/oh-my-codex/{version}")
        packages, _ = pk.load_manifests()
        self.assertFalse(packages["apt_policy"]["install_recommends"])


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def extract(self, entries):
        archive = self.tmp / "a.tar.gz"
        archive.write_bytes(make_tar(entries))
        dest = self.tmp / "out"
        pk.safe_extract(archive, dest, kind="tar.gz", strip=0)
        return dest

    def test_rejects_parent_traversal(self):
        with self.assertRaises(dl.VerificationError):
            self.extract({"../evil": b"x"})

    def test_rejects_escaping_symlink(self):
        with self.assertRaises(dl.VerificationError):
            self.extract({"a/link": ("sym", "../../etc/passwd")})
        shutil.rmtree(self.tmp / "out", ignore_errors=True)
        with self.assertRaises(dl.VerificationError):
            self.extract({"link": ("sym", "/etc/passwd")})

    def test_rejects_duplicate_member_overwrite(self):
        with self.assertRaises(dl.VerificationError):
            self.extract([("f", b"1"), ("f", b"2")])
        shutil.rmtree(self.tmp / "out", ignore_errors=True)
        with self.assertRaises(dl.VerificationError):
            self.extract([("l", ("sym", "real")), ("l", b"through")])

    def test_keeps_internal_symlinks(self):
        dest = self.extract({"lib/real": b"x", "bin/l": ("sym", "../lib/real")})
        self.assertEqual((dest / "bin/l").read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
