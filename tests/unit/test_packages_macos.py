"""macOS behaviour of installer.packages, runnable on any host.

A ``Platform("macos", <major>, <arch>, "darwin")`` is injected, the Homebrew
prefix is a directory in the test's sandbox (HOMEBREW_PREFIX in the injected
environment) and brew itself is scripted by :class:`BrewRunner`, which extends
test_packages.FakeRunner. The macOS system directories are replaced by an
empty sandbox directory, so a gpgv or python3 of the host never stands in for
Homebrew's. Nothing here runs a real brew, sudo or launchctl, or touches the
real home; only fixture shell scripts inside the sandbox are executed.

RealMacTests run only on macOS (CI) and only make brew's read-only queries.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (str(REPO_ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# The shared fakes; importing the module (not its classes) keeps its tests
# from being collected a second time here.
import test_packages as base  # noqa: E402
from installer import packages as pk  # noqa: E402
from installer import platform as plat  # noqa: E402
from installer.platform import Platform  # noqa: E402
from installer.runner import DryRunRunner  # noqa: E402

BASE = base.BASE
sha = base.sha
script = base.script
make_tar = base.make_tar

MAC_ARM = Platform("macos", "26", "arm64", "darwin")
MAC_INTEL = Platform("macos", "15", "amd64", "darwin")
UBUNTU = Platform("ubuntu", "24.04", "amd64")

# What the scripted Homebrew installs: executables per formula (rack) and a
# small dependency graph shaped like the real one.
PROVIDES = {"tmux": ["tmux"], "git": ["git"], "gnupg": ["gpgv", "gpg"], "python@3.15": ["python3"],
            "ripgrep": ["rg"], "fd": ["fd"], "tree": ["tree"]}
DEPS = {"tmux": ["libevent", "ncurses", "utf8proc"], "git": ["gettext", "pcre2"],
        "gnupg": ["gettext", "gnutls", "libgcrypt"], "python@3.15": ["openssl@4", "sqlite", "xz"],
        "ripgrep": ["pcre2"], "fd": [], "tree": [], "gnutls": ["nettle"], "sqlite": ["readline"]}
FORMULAE = ["tmux", "git", "gnupg", "python3", "ripgrep", "fd", "tree"]


class BrewRunner(base.FakeRunner):
    """FakeRunner plus a scripted Homebrew at <sandbox>/homebrew/bin/brew."""

    def __init__(self, sandbox: Path):
        super().__init__(sandbox)
        self.prefix = sandbox / "homebrew"
        self.brew = self.prefix / "bin" / "brew"
        self.brew.parent.mkdir(parents=True)
        (self.prefix / "sbin").mkdir()
        self.brew.write_bytes(script("exit 99"))  # never executed: run() answers for it
        self.brew.chmod(0o755)
        self.formulae: dict[str, str] = {}  # installed rack -> version
        self.aliases = {"python3": "python@3.15"}
        self.deps = {name: list(deps) for name, deps in DEPS.items()}
        self.unlinked: set[str] = set()  # racks 'brew install' leaves unlinked
        self.install_rc = 0
        self.install_err = b""
        self.brew_envs: list[dict] = []
        self.envs: list[tuple[list[str], dict]] = []  # every call's argv and env

    def preinstall(self, rack: str, version: str = "1.0", *, link: bool = True) -> None:
        self.formulae[rack] = version
        if link:
            self.link(rack)

    def link(self, rack: str) -> None:
        for exe in PROVIDES.get(rack, []):
            body = "echo 'Python 3.15.0'" if exe == "python3" else f"echo {exe}"
            self.put(exe, body)

    def put(self, exe: str, body: str) -> Path:
        path = self.prefix / "bin" / exe
        path.write_bytes(script(body))
        path.chmod(0o755)
        return path

    def set_python(self, version: str | None) -> None:
        if version is None:
            (self.prefix / "bin" / "python3").unlink(missing_ok=True)
        else:
            self.put("python3", f"echo 'Python {version}'")

    def brew_calls(self) -> list[tuple[list[str], bool]]:
        return [(argv, ro) for argv, ro in self.calls if argv[0] == str(self.brew)]

    def run(self, argv, *, timeout, check=True, env=None, input=None, cwd=None, read_only=False):
        argv = [str(a) for a in argv]
        self.envs.append((argv, dict(env or {})))
        if argv and argv[0] == str(self.brew):
            self.calls.append((argv, read_only))
            self.brew_envs.append(dict(env or {}))
            return self._brew(argv, read_only)
        return super().run(argv, timeout=timeout, check=check, env=env, input=input, cwd=cwd,
                           read_only=read_only)

    def _brew(self, argv: list[str], read_only: bool):
        args = argv[1:]

        def rack(name):
            return self.aliases.get(name, name)

        if args[:3] == ["list", "--formula", "--versions"]:
            assert read_only, argv
            lines = [f"{rack(n)} {self.formulae[rack(n)]}\n" for n in args[3:] if rack(n) in self.formulae]
            return self.ok(argv, "".join(lines).encode(), 0 if len(lines) == len(args) - 3 else 1)
        if args[:4] == ["deps", "--union", "--full-name", "--formula"]:
            assert read_only, argv
            seen: set[str] = set()
            stack = [rack(n) for n in args[4:]]
            while stack:
                for dep in self.deps.get(stack.pop(), []):
                    if dep not in seen:
                        seen.add(dep)
                        stack.append(dep)
            return self.ok(argv, "".join(f"{d}\n" for d in sorted(seen)).encode())
        if args[:2] == ["install", "--formula"]:
            assert not read_only, argv
            if self.install_rc:
                return self.ok(argv, b"", self.install_rc, self.install_err)
            for name in args[2:]:
                self.formulae[rack(name)] = "9.9-test"
                for dep in self.deps.get(rack(name), []):
                    self.formulae.setdefault(dep, "9.9-test")
                if rack(name) not in self.unlinked:
                    self.link(rack(name))
            return self.ok(argv)
        raise AssertionError(f"unexpected brew command {argv}")


class DarwinFixture(base.Fixture):
    """The Linux fixture plus darwin artifacts, the real macOS profile and a
    scripted Homebrew prefix inside the sandbox."""

    def __init__(self, test: unittest.TestCase):
        super().__init__(test)
        self.runner = BrewRunner(self.root)
        system = self.root / "darwin-system"
        system.mkdir()
        patcher = mock.patch.object(pk, "DARWIN_SYSTEM_PATH", str(system))
        patcher.start()
        test.addCleanup(patcher.stop)
        self.logs: list[str] = []
        self.env = {
            "HOMEBREW_PREFIX": str(self.runner.prefix),
            "HOMEBREW_API_DOMAIN": "https://mirror.example.invalid/api",
            "https_proxy": "http://proxy.example.invalid:3128",
            "LC_ALL": "C",
            "UNRELATED_VARIABLE": "must-not-reach-brew",
        }

    def _build(self):
        super()._build()
        tools = self.tools["tools"]
        node = tools["node"]
        for key, tag in (("darwin-arm64", "darwin-arm64"), ("darwin-amd64", "darwin-x64")):
            top = f"node-v24.21.0-{tag}"
            data = make_tar({
                f"{top}/bin/node": script(f"echo v24.21.0; echo {key} >&2"),
                f"{top}/lib/node_modules/npm/bin/npm-cli.js": script("echo 11.19.0"),
                f"{top}/bin/npm": ("sym", "../lib/node_modules/npm/bin/npm-cli.js"),
                f"{top}/bin/npx": ("sym", "../lib/node_modules/npm/bin/npm-cli.js"),
            }, "xz")
            url = f"{BASE}/node/{top}.tar.xz"
            self.files[url] = data
            node["artifacts"][key] = {"url": url, "sha256": sha(data), "size": len(data),
                                      "archive": "tar.xz", "strip_components": 1,
                                      "requires": {"macos": "13.5"}}
        sums = "".join(f"{v['sha256']}  {Path(v['url']).name}\n" for v in node["artifacts"].values())
        self.files[f"{BASE}/node/SHASUMS256.txt"] = sums.encode()
        node["integrity"]["checksum_source"]["sha256"] = sha(sums.encode())

        fzf = tools["fzf"]
        for key, tag in (("darwin-arm64", "arm64"), ("darwin-amd64", "amd64")):
            data = make_tar({"fzf": script(f"echo '0.74.4 (darwin_{tag})'")})
            url = f"{BASE}/fzf/fzf-0.74.4-darwin_{tag}.tar.gz"
            self.files[url] = data
            fzf["artifacts"][key] = {"url": url, "sha256": sha(data), "archive": "tar.gz",
                                     "strip_components": 0}
        fsums = "".join(f"{v['sha256']}  {Path(v['url']).name}\n" for v in fzf["artifacts"].values())
        self.files[f"{BASE}/fzf/checksums.txt"] = fsums.encode()
        fzf["integrity"]["checksum_source"]["sha256"] = sha(fsums.encode())

        nvim = tools["neovim"]
        for key, tag in (("darwin-arm64", "arm64"), ("darwin-amd64", "x86_64")):
            data = make_tar({f"nvim-macos-{tag}/bin/nvim": script("echo 'NVIM v0.12.5'")})
            url = f"{BASE}/nvim-macos-{tag}.tar.gz"
            self.files[url] = data
            nvim["artifacts"][key] = {"url": url, "sha256": sha(data), "archive": "tar.gz",
                                      "strip_components": 1}

        claude = tools["claude-code"]
        platforms = {"linux-x64": {"checksum": sha(self.claude_bin), "size": len(self.claude_bin)}}
        for key, name in (("darwin-arm64", "darwin-arm64"), ("darwin-amd64", "darwin-x64")):
            url = f"{BASE}/claude/{name}/claude"
            self.files[url] = self.claude_bin
            claude["artifacts"][key] = {"url": url, "sha256": sha(self.claude_bin),
                                        "size": len(self.claude_bin), "archive": "none",
                                        "platform": name}
            platforms[name] = {"checksum": sha(self.claude_bin), "size": len(self.claude_bin)}
        cmanifest = json.dumps({"version": "2.1.274", "platforms": platforms}).encode()
        self.files[f"{BASE}/claude/manifest.json"] = cmanifest
        claude["integrity"]["checksum_source"]["sha256"] = sha(cmanifest)

        native = tools["oh-my-codex"]["native_runtime"]
        for key, triple, platform_key in (("darwin-arm64", "aarch64-apple-darwin", "darwin-arm64"),
                                          ("darwin-amd64", "x86_64-apple-darwin", "darwin-x64")):
            top = f"omx-runtime-{triple}"
            data = make_tar({f"{top}/omx-runtime": self.runtime_bin, f"{top}/README.md": b"readme"}, "xz")
            url = f"{BASE}/omx/{top}.tar.xz"
            self.files[url] = data
            native["artifacts"][key] = {"url": url, "sha256": sha(data), "size": len(data),
                                        "archive": "tar.xz", "member": f"{top}/omx-runtime",
                                        "binary_sha256": sha(self.runtime_bin),
                                        "platform_key": platform_key}
        nmanifest = json.dumps({"version": "0.21.6", "assets": [
            {"product": "omx-runtime", "download_url": a["url"], "sha256": a["sha256"]}
            for a in native["artifacts"].values()]}).encode()
        self.files[f"{BASE}/omx/native-release-manifest.json"] = nmanifest
        native["manifest"]["sha256"] = sha(nmanifest)

        tools["nerd-font"]["platforms"] = {"darwin": {
            "skip": True, "reason": "fixture: Core Text does not read fontconfig directories.",
            "hint": "brew install --cask font-jetbrains-mono-nerd-font"}}
        self.macos, _ = pk.load_manifests(platform=MAC_ARM)
        self.write_manifests()

    def write_manifests(self):
        super().write_manifests()
        if hasattr(self, "macos"):
            (self.manifests / "packages-macos.json").write_text(json.dumps(self.macos))

    @property
    def tool_ids(self) -> list[str]:
        return list(self.tools["tools"])

    def run_darwin(self, platform: Platform = MAC_ARM, **kw):
        kw.setdefault("fetch", self.fetch)
        kw.setdefault("env", self.env)
        return pk.run_packages_phase(self.target, platform, self.runner,
                                     manifest_dir=self.manifests, log=self.logs.append, **kw)

    def run_brew(self, platform: Platform = MAC_ARM, **kw):
        """Only the Homebrew step: the shell-core group with every tool excluded."""

        return self.run_darwin(platform, only=["shell-core"], exclude=self.tool_ids, **kw)


def _no_sudo(test: unittest.TestCase, runner: base.FakeRunner) -> None:
    test.assertFalse([argv for argv, _ in runner.calls if "sudo" in argv],
                     "brew must never run through sudo")


class BrewStepTests(unittest.TestCase):
    def setUp(self):
        self.fx = DarwinFixture(self)
        self.runner = self.fx.runner
        self.prefix = self.runner.prefix
        self.brew = str(self.runner.brew)

    def install_everything(self):
        for rack in ("tmux", "git", "gnupg", "ripgrep", "fd", "tree"):
            self.runner.preinstall(rack, "1.0")
        self.runner.set_python("3.15.0")

    def install_calls(self):
        return [argv for argv, _ in self.runner.brew_calls() if argv[1] == "install"]

    def test_installs_only_the_missing_formulae_as_the_user(self):
        self.runner.preinstall("tmux", "3.8")
        self.runner.preinstall("git", "2.56.0")
        self.runner.set_python("3.13.5")
        result = self.fx.run_brew()
        brew = result["details"]["homebrew"]
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(brew["status"], "PASS")
        self.assertIs(brew["transactional"], False)
        self.assertEqual(brew["missing"], ["gnupg", "ripgrep", "fd", "tree"])
        self.assertEqual(brew["already_installed"],
                         {"tmux": "3.8", "git": "2.56.0", "python3": "python3 3.13"})
        calls = self.runner.brew_calls()
        self.assertEqual([argv[1] for argv, _ in calls], ["list", "deps", "install", "list"])
        self.assertEqual([ro for _, ro in calls], [True, True, False, True])
        self.assertEqual(calls[0][0][2:], ["--formula", "--versions",
                                           "tmux", "git", "gnupg", "ripgrep", "fd", "tree"])
        self.assertEqual(calls[1][0][2:], ["--union", "--full-name", "--formula",
                                           "gnupg", "ripgrep", "fd", "tree"])
        self.assertEqual(self.install_calls(),
                         [[self.brew, "install", "--formula", "gnupg", "ripgrep", "fd", "tree"]])
        self.assertIn("gnutls", brew["closure"]["formulae"])
        self.assertIn("gnupg", brew["closure"]["formulae"])
        self.assertEqual(brew["closure"]["forbidden"], [])
        self.assertEqual(brew["executables"]["gpgv"], str(self.prefix / "bin/gpgv"))
        self.assertEqual(brew["executables"]["python3"], str(self.prefix / "bin/python3"))
        self.assertEqual(brew["versions"]["gnupg"], "9.9-test")
        _no_sudo(self, self.runner)

    def test_brew_gets_its_own_environment(self):
        self.fx.run_brew()
        env = next(e for (argv, _), e in zip(self.runner.brew_calls(), self.runner.brew_envs)
                   if argv[1] == "install")
        for key, value in pk.BREW_SETTINGS.items():
            self.assertEqual(env[key], value, key)
        self.assertEqual(env["PATH"].split(":")[:2], [f"{self.prefix}/bin", f"{self.prefix}/sbin"])
        self.assertTrue(env["PATH"].endswith(pk.DARWIN_SYSTEM_PATH))
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(env["HOME"], str(self.fx.target.home))
        self.assertEqual(env["USER"], self.fx.target.username)
        # the user's Homebrew settings and proxies pass through, nothing else
        self.assertEqual(env["HOMEBREW_API_DOMAIN"], "https://mirror.example.invalid/api")
        self.assertEqual(env["HOMEBREW_PREFIX"], str(self.prefix))
        self.assertEqual(env["https_proxy"], "http://proxy.example.invalid:3128")
        self.assertNotIn("LC_ALL", env)
        self.assertNotIn("UNRELATED_VARIABLE", env)

    def test_user_settings_cannot_turn_upgrades_back_on(self):
        self.fx.env["HOMEBREW_NO_INSTALL_UPGRADE"] = ""
        self.fx.run_brew()
        install_env = self.runner.brew_envs[[a[1] for a, _ in self.runner.brew_calls()].index("install")]
        self.assertEqual(install_env["HOMEBREW_NO_INSTALL_UPGRADE"], "1")

    def test_nothing_missing_runs_only_queries(self):
        self.install_everything()
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual([argv[1] for argv, _ in self.runner.brew_calls()], ["list"])
        self.assertEqual(result["details"]["homebrew"]["missing"], [])
        self.assertNotIn("closure", result["details"]["homebrew"])

    def test_forbidden_formula_in_the_closure_aborts_before_installing(self):
        self.runner.deps["git"].append("node")
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn("forbidden formulae in the closure: node", reason)
        self.assertIn("nothing was installed", reason)
        self.assertEqual(self.install_calls(), [])
        self.assertEqual(self.runner.formulae, {})

    def test_an_already_installed_forbidden_dependency_still_counts(self):
        self.runner.preinstall("fzf", "0.70.0")  # e.g. the user's own Homebrew fzf
        self.runner.deps["tree"].append("fzf")
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["details"]["homebrew"]["closure"]["forbidden"], ["fzf"])
        self.assertEqual(self.install_calls(), [])

    def test_forbidden_tap_formula_is_matched_by_its_name(self):
        self.runner.deps["tmux"].append("someone/tools/neovim")
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["details"]["homebrew"]["closure"]["forbidden"], ["someone/tools/neovim"])
        self.assertEqual(self.install_calls(), [])

    def test_failed_closure_query_fails_without_installing(self):
        original = self.runner._brew

        def deps_fail(argv, read_only):
            if argv[1] == "deps":
                return self.runner.ok(argv, b"", 1, b"Error: No available formula with the name \"fd\"")
            return original(argv, read_only)

        self.runner._brew = deps_fail
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("brew deps failed", " ".join(result["reasons"]))
        self.assertEqual(self.install_calls(), [])

    def test_dry_run_runs_only_read_only_brew_queries(self):
        self.runner.preinstall("tmux", "3.8")
        self.runner.set_python("3.12.1")
        before = sorted(p.name for p in (self.prefix / "bin").iterdir())
        result = self.fx.run_darwin(dry_run=True)
        self.assertEqual(result["status"], "SKIPPED", result["reasons"])
        brew = result["details"]["homebrew"]
        self.assertEqual(brew["status"], "SKIPPED")
        self.assertEqual([a for a, ro in self.runner.calls if not ro], [])
        self.assertEqual(brew["plan"], [f"{self.brew} install --formula git gnupg ripgrep fd tree"])
        self.assertEqual(self.fx.fetch.calls, [])
        self.assertEqual(sorted(p.name for p in (self.prefix / "bin").iterdir()), before)
        self.assertFalse(self.fx.tools_root.exists())
        self.assertFalse(self.fx.bin_dir.exists())
        self.assertIn(f"packages (dry-run): {self.brew} install --formula git gnupg ripgrep fd tree",
                      self.fx.logs)

    def test_dry_run_still_reports_a_forbidden_closure(self):
        self.runner.deps["fd"].append("docker")
        result = self.fx.run_brew(dry_run=True)
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn("docker", reason)
        self.assertNotIn("nothing was installed", reason)

    def test_dry_run_reports_an_unlinked_formula(self):
        self.install_everything()
        (self.prefix / "bin" / "rg").unlink()
        result = self.fx.run_brew(dry_run=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("brew link ripgrep", " ".join(result["reasons"]))

    def test_failed_install_points_at_brew_doctor(self):
        self.runner.install_rc = 1
        self.runner.install_err = b"Error: Permission denied @ dir_s_mkdir - /opt/homebrew/Cellar\n"
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn("homebrew: brew install failed", reason)
        self.assertIn("Permission denied", reason)
        self.assertIn("brew doctor", reason)

    def test_installed_but_unlinked_formula_fails_with_the_link_command(self):
        self.install_everything()
        (self.prefix / "bin" / "tmux").unlink()
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn(f"not linked into {self.prefix}/bin: tmux (tmux)", reason)
        self.assertIn("'brew link tmux'", reason)
        self.assertEqual(self.install_calls(), [])

    def test_formula_left_unlinked_by_the_install_fails(self):
        self.runner.unlinked = {"ripgrep"}
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("'brew link ripgrep'", " ".join(result["reasons"]))
        self.assertNotIn("rg", result["details"]["homebrew"]["executables"])

    def test_python_is_satisfied_by_any_new_enough_python3(self):
        self.install_everything()
        self.runner.set_python("3.12.4")
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["homebrew"]["already_installed"]["python3"], "python3 3.12")
        # 'brew list --versions python3' would answer for python@3.15 only
        listed = self.runner.brew_calls()[0][0]
        self.assertNotIn("python3", listed)

    def test_too_old_python_is_installed_through_the_alias(self):
        self.install_everything()
        self.runner.set_python("3.9.6")
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["homebrew"]["missing"], ["python3"])
        self.assertEqual(self.install_calls(), [[self.brew, "install", "--formula", "python3"]])
        self.assertEqual(result["details"]["homebrew"]["versions"]["python3"], "python3 3.15")

    def test_python_still_too_old_after_install_fails(self):
        self.install_everything()
        self.runner.set_python("3.9.6")
        self.runner.unlinked = {"python@3.15"}
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn("still missing after 'brew install': python3", reason)
        self.assertIn("is 3.9, older than 3.10", reason)
        self.assertIn("brew doctor", reason)

    def test_missing_brew_fails_before_any_command(self):
        self.runner.brew.unlink()
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        reason = " ".join(result["reasons"])
        self.assertIn(f"{self.brew} not found", reason)
        self.assertIn("https://brew.sh", reason)
        self.assertEqual(self.runner.calls, [])

    def test_brew_must_be_executable(self):
        self.runner.brew.chmod(0o644)
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.runner.calls, [])

    def test_root_is_refused_before_brew_runs(self):
        with mock.patch.object(pk.os, "geteuid", return_value=0):
            result = self.fx.run_brew()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("refusing to run brew as root", " ".join(result["reasons"]))
        self.assertEqual(self.runner.calls, [])

    def test_intel_warns_about_source_builds(self):
        result = self.fx.run_brew(MAC_INTEL)
        self.assertEqual(result["status"], "PASS", result["reasons"])
        warning = next(r for r in result["reasons"] if "Tier 3" in r)
        self.assertIn("build from source", warning)
        self.assertTrue(any("Tier 3" in line for line in self.fx.logs))

    def test_intel_without_homebrew_needs_an_existing_install(self):
        self.runner.brew.unlink()
        result = self.fx.run_brew(MAC_INTEL)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("refuses Intel Macs", " ".join(result["reasons"]))

    def test_apple_silicon_has_no_tier_warning(self):
        result = self.fx.run_brew()
        self.assertFalse([r for r in result["reasons"] if "Tier 3" in r])

    def test_gui_group_needs_no_homebrew(self):
        self.runner.brew.unlink()
        result = self.fx.run_darwin(only=["gui"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["homebrew"]["status"], "PASS")
        self.assertEqual(result["details"]["homebrew"]["requested"], [])
        self.assertEqual(result["details"]["tools"]["nerd-font"]["status"], "SKIPPED")
        self.assertEqual(self.runner.brew_calls(), [])

    def test_results_are_saved_for_later_phases(self):
        result = self.fx.run_brew()
        self.assertEqual(result["status"], "PASS", result["reasons"])
        saved = json.loads((self.fx.target.state_root / "packages" / "last-run.json").read_text())
        self.assertEqual(saved["homebrew"]["prefix"], str(self.prefix))
        self.assertEqual(saved["homebrew"]["executables"]["tmux"], str(self.prefix / "bin/tmux"))
        self.assertEqual(saved["homebrew"]["versions"]["tmux"], "9.9-test")
        self.assertEqual(saved["apt_versions"], {})

    def test_no_apt_dpkg_or_locale_commands_on_macos(self):
        self.fx.run_darwin()
        heads = {Path(argv[0]).name for argv, _ in self.runner.calls}
        self.assertFalse(heads & {"apt-get", "dpkg-query", "locale", "locale-gen", "sudo", "fc-cache"},
                         heads)
        self.assertNotIn("apt", self.fx.run_brew()["details"])

    def test_linux_runs_never_touch_homebrew(self):
        fx = base.Fixture(self)
        result = fx.run(only=["shell-core"], exclude=list(fx.tools["tools"]))
        self.assertIn("apt", result["details"])
        self.assertNotIn("homebrew", result["details"])


class DarwinToolTests(unittest.TestCase):
    def setUp(self):
        self.fx = DarwinFixture(self)
        self.runner = self.fx.runner
        self.runner.preinstall("gnupg")  # gpgv for node and claude-code

    def test_darwin_artifacts_are_selected_for_each_architecture(self):
        for platform in (MAC_ARM, MAC_INTEL):
            with self.subTest(platform=platform.artifact_key):
                fx = DarwinFixture(self)
                fx.runner.preinstall("gnupg")
                result = fx.run_darwin(platform, only=["node", "fzf", "neovim", "claude-code"])
                self.assertEqual(result["status"], "PASS", result["reasons"])
                for tool_id in ("node", "fzf", "neovim", "claude-code"):
                    arts = fx.tools["tools"][tool_id]["artifacts"]
                    self.assertIn(arts[platform.artifact_key]["url"], fx.fetch.calls, tool_id)
                    for key, art in arts.items():
                        if key != platform.artifact_key:
                            self.assertNotIn(art["url"], fx.fetch.calls, (tool_id, key))
                node = fx.target.data_home / "personal-dotfiles/tools/node/24.21.0"
                self.assertEqual(os.readlink(fx.bin_dir / "node"), str(node / "bin/node"))
                self.assertEqual(result["details"]["tools"]["node"]["verify"], ["v24.21.0", "11.19.0"])

    def test_a_linux_build_never_serves_macos(self):
        arts = self.fx.tools["tools"]["neovim"]["artifacts"]
        del arts["darwin-arm64"], arts["darwin-amd64"]
        self.fx.write_manifests()
        result = self.fx.run_darwin(only=["neovim"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("no artifact for architecture 'darwin-arm64'",
                      result["details"]["tools"]["neovim"]["reason"])
        self.assertEqual(self.fx.fetch.calls, [])

    def test_the_any_artifact_serves_macos(self):
        result = self.fx.run_darwin(only=["oh-my-codex"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertIn(f"{BASE}/omx.tgz", self.fx.fetch.calls)

    def test_native_runtime_is_published_under_the_darwin_platform_key(self):
        for platform, key in ((MAC_ARM, "darwin-arm64"), (MAC_INTEL, "darwin-x64")):
            with self.subTest(key=key):
                fx = DarwinFixture(self)
                fx.runner.preinstall("gnupg")
                result = fx.run_darwin(platform, only=["oh-my-codex"])
                self.assertEqual(result["status"], "PASS", result["reasons"])
                runtime = (fx.target.cache_home / "oh-my-codex/native/0.21.6" / key
                           / "omx-runtime" / "omx-runtime")
                self.assertEqual(runtime.read_bytes(), fx.runtime_bin)
                native = fx.tools["tools"]["oh-my-codex"]["native_runtime"]["artifacts"]
                for linux in ("amd64", "arm64"):
                    self.assertNotIn(native[linux]["url"], fx.fetch.calls)

    def test_gpgv_comes_from_the_homebrew_prefix(self):
        result = self.fx.run_darwin(only=["node"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        gpgv = next(argv for argv, _ in self.runner.calls if argv[0].endswith("gpgv"))
        self.assertEqual(gpgv[0], str(self.runner.prefix / "bin" / "gpgv"))
        self.assertIn("--homedir", gpgv)

    def test_missing_gpgv_fails_closed_with_the_brew_command(self):
        (self.runner.prefix / "bin" / "gpgv").unlink()
        result = self.fx.run_darwin(only=["node", "claude-code"])
        self.assertEqual(result["status"], "FAIL")
        for tool_id in ("node", "claude-code"):
            self.assertIn("gpgv not found: brew install gnupg, then rerun",
                          result["details"]["tools"][tool_id]["reason"])
        arts = self.fx.tools["tools"]
        self.assertNotIn(arts["node"]["artifacts"]["darwin-arm64"]["url"], self.fx.fetch.calls)
        self.assertNotIn(arts["claude-code"]["artifacts"]["darwin-arm64"]["url"], self.fx.fetch.calls)
        self.assertNotIn(f"{BASE}/keys/node.asc", self.fx.fetch.calls)
        self.assertFalse(os.path.lexists(self.fx.bin_dir / "node"))

    def test_nerd_font_is_skipped_with_the_cask_hint(self):
        result = self.fx.run_darwin(only=["nerd-font"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        font = result["details"]["tools"]["nerd-font"]
        self.assertEqual(font["status"], "SKIPPED")
        self.assertEqual(font["hint"], "brew install --cask font-jetbrains-mono-nerd-font")
        self.assertIn("nerd-font: not installed on macOS; install it with "
                      "'brew install --cask font-jetbrains-mono-nerd-font'", result["reasons"])
        self.assertEqual(self.fx.fetch.calls, [])
        self.assertFalse([argv for argv, _ in self.runner.calls
                          if Path(argv[0]).name in ("fc-cache", "fc-list")])

    def test_a_skipped_tool_requests_no_home_links(self):
        self.fx.tools["tools"]["fzf"]["platforms"] = {"darwin": {"skip": True, "reason": "fixture"}}
        self.fx.write_manifests()
        result = self.fx.run_darwin(only=["fzf"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["tools"]["fzf"]["status"], "SKIPPED")
        self.assertEqual(result["details"]["links"], [])
        self.assertIn("fzf: not installed on macOS", result["reasons"])
        self.assertEqual(self.fx.fetch.calls, [])

    def test_nerd_font_skip_in_a_dry_run(self):
        result = self.fx.run_darwin(only=["nerd-font"], dry_run=True)
        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(result["details"]["tools"]["nerd-font"]["status"], "SKIPPED")

    def _probe_tool(self):
        data = make_tar({"probe/bin/probe": script("echo probe")})
        self.fx.files[f"{BASE}/probe.tar.gz"] = data
        self.fx.fetch.files[f"{BASE}/probe.tar.gz"] = data
        self.fx.tools["tools"]["probe"] = {
            "version": "1.0",
            "artifacts": {"any": {"url": f"{BASE}/probe.tar.gz", "sha256": sha(data),
                                  "archive": "tar.gz", "strip_components": 1}},
            "integrity": {"checksum_source": {"type": "github-release-asset-digest"}},
            "install": {"method": "extract", "post": ["probe-post", "{prefix}"]},
            "prefix": "{prefix_root}/probe/{version}", "links": {"probe": "bin/probe"},
            "verify": [{"argv": ["probe-check"], "stdout": "^ok$"}],
        }
        self.fx.write_manifests()

    def test_bare_commands_resolve_through_the_tool_path(self):
        self._probe_tool()
        post = self.runner.put("probe-post", "exit 0")
        check = self.runner.put("probe-check", "echo ok")
        result = self.fx.run_darwin(only=["probe"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        heads = [argv[0] for argv, _ in self.runner.calls]
        self.assertIn(str(post), heads)
        self.assertIn(str(check), heads)
        self.assertNotIn("probe-post", heads)
        self.assertNotIn("probe-check", heads)

    def test_a_missing_bare_command_fails_with_where_it_looked(self):
        self._probe_tool()
        self.runner.put("probe-check", "echo ok")
        result = self.fx.run_darwin(only=["probe"])
        self.assertEqual(result["status"], "FAIL")
        reason = result["details"]["tools"]["probe"]["reason"]
        self.assertIn("probe-post is not installed", reason)
        self.assertIn(f"{self.runner.prefix}/bin", reason)

    def test_tar_xz_needs_lzma_before_anything_is_downloaded(self):
        with mock.patch.object(pk, "_lzma_available", return_value=False):
            result = self.fx.run_darwin(only=["node", "fzf"])
        tools = result["details"]["tools"]
        self.assertEqual(tools["node"]["status"], "FAIL")
        self.assertIn("no lzma module", tools["node"]["reason"])
        self.assertIn("brew install python3", tools["node"]["reason"])
        self.assertFalse([url for url in self.fx.fetch.calls if "/node/" in url])
        self.assertEqual(tools["fzf"]["status"], "PASS", tools["fzf"])

    def test_dry_run_reports_missing_lzma(self):
        with mock.patch.object(pk, "_lzma_available", return_value=False):
            result = self.fx.run_darwin(only=["node"], dry_run=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.fx.fetch.calls, [])

    def test_lzma_check_is_macos_only(self):
        fx = base.Fixture(self)
        with mock.patch.object(pk, "_lzma_available", return_value=False):
            result = fx.run(only=["node"])
        self.assertEqual(result["status"], "PASS", result["reasons"])

    def test_child_processes_get_the_macos_path_and_locale(self):
        result = self.fx.run_darwin(only=["fzf"])
        self.assertEqual(result["status"], "PASS", result["reasons"])
        self.assertEqual(result["details"]["tools"]["fzf"]["verify"], ["0.74.4 (darwin_arm64)"])
        prefix = self.fx.tools_root / "fzf" / "0.74.4"
        argv, env = next((a, e) for a, e in self.runner.envs if a[0] == str(prefix / "fzf"))
        self.assertEqual(argv[1:], ["--version"])
        self.assertEqual(env["PATH"], f"{prefix}/bin:{self.fx.bin_dir}:{pk.DARWIN_SYSTEM_PATH}")
        self.assertEqual(env["LANG"], "en_US.UTF-8")


class ContextTests(unittest.TestCase):
    """_Ctx values per platform, with the real system directories."""

    def ctx(self, platform: Platform, environ: dict | None = None) -> pk._Ctx:
        fx = base.Fixture(self)
        return pk._Ctx(fx.target, platform, fx.runner, fx.fetch, False, fx.tools, lambda m: None,
                       environ=environ)

    def test_macos_children_get_the_sbin_dirs_and_a_real_locale(self):
        ctx = self.ctx(MAC_ARM, {"HOMEBREW_PREFIX": "/x/brew"})
        env = ctx.env()
        self.assertEqual(env["PATH"], f"{ctx.bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(ctx.brew_prefix, Path("/x/brew"))
        self.assertEqual(ctx.tool_path,
                         f"{ctx.bin_dir}:/x/brew/bin:/x/brew/sbin:/usr/bin:/bin:/usr/sbin:/sbin")

    def test_homebrew_prefix_defaults_per_architecture(self):
        self.assertEqual(self.ctx(MAC_ARM, {}).brew_prefix, Path("/opt/homebrew"))
        self.assertEqual(self.ctx(MAC_INTEL, {}).brew_prefix, Path("/usr/local"))

    def test_linux_context_is_unchanged(self):
        ctx = self.ctx(UBUNTU, {"HOMEBREW_PREFIX": "/x/brew"})
        env = ctx.env()
        self.assertEqual(env["PATH"], f"{ctx.bin_dir}:/usr/bin:/bin")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertIsNone(ctx.brew_prefix)
        self.assertIsNone(ctx.tool_path)


class RealManifestDarwinTests(unittest.TestCase):
    """The shipped tools.json: darwin coverage, and Linux selection as before."""

    @classmethod
    def setUpClass(cls):
        cls.packages, cls.tools = pk.load_manifests(platform=MAC_ARM)

    def test_every_tool_has_both_darwin_artifacts_or_any(self):
        for tool_id, spec in self.tools["tools"].items():
            with self.subTest(tool=tool_id):
                arts = spec["artifacts"]
                self.assertTrue("any" in arts or {"darwin-arm64", "darwin-amd64"} <= set(arts))
                for key in ("darwin-arm64", "darwin-amd64"):
                    if key not in arts:
                        continue
                    art = arts[key]
                    self.assertTrue(art["url"].startswith("https://"), art["url"])
                    self.assertRegex(art["sha256"], "^[0-9a-f]{64}$")
                    self.assertIsInstance(art["size"], int)
                    self.assertIn(art["archive"], ("tar.gz", "tar.xz", "none"))
                    self.assertIn(key, spec.get("verification", {}))

    def test_each_platform_selects_its_own_build(self):
        for tool_id, spec in self.tools["tools"].items():
            arts = spec["artifacts"]
            for platform in (Platform("ubuntu", "24.04", "amd64"), Platform("ubuntu", "22.04", "arm64"),
                             MAC_ARM, MAC_INTEL):
                with self.subTest(tool=tool_id, platform=platform.describe()):
                    chosen = pk._select_artifact(spec, platform.artifact_key)
                    self.assertIs(chosen, arts.get(platform.artifact_key) or arts["any"])
                    if "any" in arts:
                        continue
                    url = chosen["url"].lower()
                    if platform.is_darwin:
                        self.assertNotIn("linux", url)
                        self.assertTrue(re.search(r"darwin|macos", url), url)
                    else:
                        self.assertIs(chosen, arts[platform.architecture])
                        self.assertNotRegex(url, r"darwin|macos")

    def test_minimum_macos_fits_every_supported_release(self):
        floors = {"darwin-arm64": min(int(r) for r in plat.SUPPORTED_MACOS_RELEASES),
                  "darwin-amd64": min(int(r) for r in plat.MACOS_INTEL_RELEASES)}
        for tool_id, spec in self.tools["tools"].items():
            for key, floor in floors.items():
                needed = spec["artifacts"].get(key, {}).get("requires", {}).get("macos")
                if needed is None:
                    continue
                with self.subTest(tool=tool_id, key=key):
                    self.assertLessEqual(tuple(int(p) for p in needed.split(".")), (floor,))

    def test_claude_code_uses_the_signed_manifest_platform_names(self):
        arts = self.tools["tools"]["claude-code"]["artifacts"]
        self.assertEqual(arts["darwin-arm64"]["platform"], "darwin-arm64")
        self.assertEqual(arts["darwin-amd64"]["platform"], "darwin-x64")
        for key in ("darwin-arm64", "darwin-amd64"):
            self.assertIn(f"/{arts[key]['platform']}/claude", arts[key]["url"])

    def test_codex_darwin_targets_get_their_own_prefix(self):
        spec = self.tools["tools"]["codex"]
        targets = {key: spec["artifacts"][key]["target"] for key in ("darwin-arm64", "darwin-amd64")}
        self.assertEqual(targets, {"darwin-arm64": "aarch64-apple-darwin",
                                   "darwin-amd64": "x86_64-apple-darwin"})
        self.assertIn("{target}", spec["prefix"])
        self.assertNotIn("codex-resources/bwrap", spec["archive_layout_darwin"]["entries"])

    def test_native_runtime_darwin_entries(self):
        runtime = self.tools["tools"]["oh-my-codex"]["native_runtime"]
        for key, platform_key, triple in (("darwin-arm64", "darwin-arm64", "aarch64-apple-darwin"),
                                          ("darwin-amd64", "darwin-x64", "x86_64-apple-darwin")):
            with self.subTest(key=key):
                art = runtime["artifacts"][key]
                self.assertEqual(art["platform_key"], platform_key)
                self.assertEqual(art["member"], f"omx-runtime-{triple}/omx-runtime")
                self.assertEqual(art["archive"], "tar.xz")
                self.assertTrue(art["url"].startswith(
                    "https://github.com/Yeachan-Heo/oh-my-codex/releases/download/v0.21.6/"))
                for field in ("sha256", "binary_sha256"):
                    self.assertRegex(art[field], "^[0-9a-f]{64}$")
                self.assertIn(key, runtime["verification"])

    def test_lockfile_pins_the_darwin_lzma_natives(self):
        spec = self.tools["tools"]["oh-my-codex"]
        packages = spec["install"]["lockfile"]["packages"]
        for name in ("@napi-rs/lzma-darwin-arm64", "@napi-rs/lzma-darwin-x64"):
            with self.subTest(name=name):
                entry = packages[f"node_modules/{name}"]
                self.assertEqual(entry["os"], ["darwin"])
                self.assertEqual(spec["dependencies"]["native"][name]["integrity"], entry["integrity"])

    def test_only_the_nerd_font_is_skipped_on_macos(self):
        skipped = {t for t, spec in self.tools["tools"].items() if pk._platform_skip(spec, MAC_ARM)}
        self.assertEqual(skipped, {"nerd-font"})
        font = self.tools["tools"]["nerd-font"]
        skip = pk._platform_skip(font, MAC_INTEL)
        self.assertEqual(skip["hint"], "brew install --cask font-jetbrains-mono-nerd-font")
        self.assertTrue(skip["reason"].strip())
        self.assertIsNone(pk._platform_skip(font, UBUNTU))


class MacosProfileTests(unittest.TestCase):
    """manifests/packages-macos.json against the policy and the Ubuntu profile."""

    @classmethod
    def setUpClass(cls):
        cls.mac, _ = pk.load_manifests(platform=MAC_ARM)
        cls.ubuntu, _ = pk.load_manifests()

    def formulae(self):
        return [p for g in self.mac["groups"] for p in g["packages"]]

    def test_the_profile_is_chosen_by_platform(self):
        self.assertEqual(pk.package_manifest_name(MAC_INTEL), "packages-macos.json")
        self.assertEqual(pk.package_manifest_name(UBUNTU), "packages.json")
        self.assertEqual(pk.package_manifest_name(), "packages.json")
        self.assertIn("apt_policy", self.ubuntu)
        self.assertNotIn("apt_policy", self.mac)
        self.assertEqual(pk.load_manifests(platform=UBUNTU)[0], self.ubuntu)

    def test_targets_match_the_platform_policy(self):
        self.assertEqual(tuple(self.mac["targets"]["arm64"]), plat.SUPPORTED_MACOS_RELEASES)
        self.assertEqual(tuple(self.mac["targets"]["amd64"]), plat.MACOS_INTEL_RELEASES)
        self.assertEqual(self.mac["homebrew"]["prefixes"], plat.HOMEBREW_PREFIXES)

    def test_group_ids_match_the_ubuntu_profile(self):
        ids = [g["id"] for g in self.mac["groups"]]
        self.assertEqual(ids, [g["id"] for g in self.ubuntu["groups"]])
        self.assertLessEqual(set(pk.DEFAULT_GROUPS), set(ids))

    def test_shell_core_formulae(self):
        core = next(g for g in self.mac["groups"] if g["id"] == "shell-core")
        self.assertEqual([p["name"] for p in core["packages"]], FORMULAE)
        for pkg in core["packages"]:
            with self.subTest(formula=pkg["name"]):
                self.assertTrue(pkg["reason"].strip())
                self.assertTrue(pkg["provides"])
        by_name = {p["name"]: p for p in core["packages"]}
        self.assertEqual(by_name["gnupg"]["provides"], ["gpgv"])
        self.assertEqual(by_name["ripgrep"]["provides"], ["rg"])
        self.assertEqual(by_name["python3"]["satisfied_by"], {"executable": "python3", "min_version": "3.10"})
        gui = next(g for g in self.mac["groups"] if g["id"] == "gui")
        self.assertEqual(gui["packages"], [])

    def test_forbidden_patterns_block_what_tools_json_pins(self):
        patterns = self.mac["forbidden"]["patterns"]
        for name in ("node", "node@24", "npm", "fzf", "neovim", "codex", "claude-code", "gcc", "gcc@15",
                     "llvm", "llvm@20", "rust", "docker", "docker-compose", "colima", "lima", "podman",
                     "ros-jazzy", "gh", "glab", "nvm", "pyenv"):
            with self.subTest(name=name):
                self.assertEqual(pk.forbidden_matches([name], patterns), [name])
        self.assertEqual(pk.forbidden_matches(FORMULAE, patterns), [])
        self.assertEqual(pk.forbidden_matches(self.mac["evidence"]["closure"], patterns), [])

    def test_the_documented_brew_environment_is_what_the_code_sets(self):
        documented = self.mac["homebrew"]["environment"]["set"]
        self.assertEqual(set(documented), set(pk.BREW_SETTINGS) | {"PATH", "LANG"})
        for key, value in pk.BREW_SETTINGS.items():
            self.assertEqual(documented[key], value, key)
        self.assertEqual(documented["LANG"], pk.BREW_LANG)

    def test_every_ubuntu_package_is_accounted_for(self):
        mac_names = {p["name"] for p in self.formulae()}
        explained = {name.strip() for key in self.mac["not_from_homebrew"] for name in key.split(",")}
        for group in self.ubuntu["groups"]:
            for pkg in group["packages"]:
                name = pkg["name"]
                if name not in mac_names:
                    with self.subTest(package=name):
                        self.assertIn(name, explained)

    def test_evidence_is_offline_and_complete_for_apple_silicon(self):
        evidence = self.mac["evidence"]
        self.assertEqual(evidence["forbidden_hits"], [])
        self.assertEqual(evidence["without_bottle"]["arm64_sequoia, arm64_tahoe, arm64_golden_gate"], [])
        self.assertRegex(evidence["aliases"]["python3"], r"^python@3\.[0-9]+$")
        self.assertEqual(sorted(evidence["closure_count"]),
                         sorted(["arm64_sequoia", "arm64_tahoe", "arm64_golden_gate", "sequoia", "tahoe"]))
        self.assertTrue(evidence["not_proven"].strip())

    def test_no_pynvim_and_no_pip_on_macos(self):
        self.assertFalse([p for p in self.formulae() if "pynvim" in p["name"]])
        self.assertIn("python3-pynvim", self.mac["not_from_homebrew"])
        fx = DarwinFixture(self)
        fx.run_brew()
        self.assertFalse([argv for argv, _ in fx.runner.calls if "pip" in argv or "venv" in argv])


@unittest.skipUnless(sys.platform == "darwin", "needs a real Mac with Homebrew")
class RealMacTests(unittest.TestCase):
    """On a Mac (CI): brew's read-only queries against the real profile."""

    def test_brew_dry_run_is_read_only(self):
        platform = plat.detect_macos()
        if not platform.is_supported:
            self.skipTest(f"{platform.describe()} is not a supported macOS")
        prefix = plat.homebrew_prefix(platform)
        if not os.access(prefix / "bin" / "brew", os.X_OK):
            self.skipTest(f"no Homebrew at {prefix}")
        packages, tools = pk.load_manifests(platform=platform)
        fx = base.Fixture(self)
        runner = DryRunRunner()
        ctx = pk._Ctx(fx.target, platform, runner, fx.fetch, True, tools, lambda m: None)
        result = pk.brew_step(ctx, packages, list(pk.DEFAULT_GROUPS))
        self.assertEqual(runner.recorded, [], "a dry run must not run anything that mutates")
        self.assertEqual(result["requested"], FORMULAE)
        if result["status"] == "FAIL":
            self.assertTrue(result["reasons"])
        else:
            self.assertEqual(result["status"], "SKIPPED", result)
            self.assertEqual(result.get("closure", {}).get("forbidden", []), [])


if __name__ == "__main__":
    unittest.main()
