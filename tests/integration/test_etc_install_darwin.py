"""etc/install and ./install on macOS: simulated on Linux, and on a real Mac.

DarwinSimulationTests runs on any Linux host. Everything that tells macOS
from Linux is a command on PATH or Homebrew's own HOMEBREW_PREFIX, so fakes
in front of PATH make etc/install take its Darwin branch: ``uname -s`` says
Darwin (FAKE_UNAME_S; ``-m`` says FAKE_UNAME_M), ``sw_vers`` reports
FAKE_MACOS_VERSION, ``sysctl`` answers sysctl.proc_translated (Rosetta 2)
from FAKE_PROC_TRANSLATED ("none": no such name, as on Intel), and
``xcode-select -p`` names fake Command Line Tools with a git in them
(FAKE_CLT=0: none installed).

A fake Homebrew lives under HOMEBREW_PREFIX: bin/brew records its calls, and
'brew install' puts git or python3 into bin/ (FAKE_BREW_RC makes it fail);
bin/python3 records the install.py run like test_etc_install.py's fake,
answers the login shell lookup from FAKE_PW_SHELL, so no real login shell
ever starts, runs as FAKE_PYTHON_MACHINE (default: natively) and runs the
CA-certificate probe as if its ssl module linked FAKE_SSL_VERSION. python3
on PATH must never run on macOS: it is a trap. BSD-like ``mv`` (no -T, no
long options; FAKE_MV_FAIL makes renames fail, FAKE_MV_RECREATE puts
something back in the way) and ``readlink`` (only -f and -n) prove that the
GNU options are not used; ``getent`` and ``dpkg`` (macOS has neither),
``sudo`` and ``apt-get`` record any call, which must not happen. The
Homebrew installer is never downloaded: a fake ``curl`` writes a fake one,
which records how it ran and creates the fake brew in the prefix given by
--path.

The platform-neutral flows of test_etc_install.py run again on the Darwin
branch (SHARED_FLOWS). RealMacTests runs a few flows on a real Mac with the
real uname, sw_vers, sysctl, xcode-select, mv and /bin/sh, faking only
Homebrew.

Nothing reaches the network, the real HOME or a real login shell. Run with
BASH_UNDER_TEST=/path/to/bash-3.2 for macOS's bash version (see
test_etc_install.py).
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from installer import platform as plat  # noqa: E402
from tests.integration import test_etc_install as base  # noqa: E402

HOMEBREW_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
ARM64_RELEASES = ", ".join(plat.SUPPORTED_MACOS_RELEASES)
INTEL_RELEASES = ", ".join(plat.MACOS_INTEL_RELEASES)

FAKE_UNAME = r'''#!/bin/sh
case "${1:--s}" in
  -s) if [ "${FAKE_UNAME_S-}" = fail ]; then exit 1; fi; echo "${FAKE_UNAME_S:-Darwin}" ;;
  -m) echo "${FAKE_UNAME_M:-arm64}" ;;
  *) exec "@REAL_UNAME@" "$@" ;;
esac
'''

FAKE_SW_VERS = r'''#!/bin/sh
echo "sw_vers $* SYSTEM_VERSION_COMPAT=${SYSTEM_VERSION_COMPAT-unset}" >> "@LOG@"
if [ "$1" = -productVersion ]; then echo "${FAKE_MACOS_VERSION-15.5}"; fi
'''

FAKE_SYSCTL = r'''#!/bin/sh
echo "sysctl $*" >> "@LOG@"
if [ "$*" = "-n sysctl.proc_translated" ] && [ "${FAKE_PROC_TRANSLATED-0}" != none ]; then
  echo "${FAKE_PROC_TRANSLATED-0}"
  exit 0
fi
echo "sysctl: unknown oid '$2'" >&2
exit 1
'''

FAKE_XCODE_SELECT = r'''#!/bin/sh
[ "$1" = -p ] || exit 1
if [ "${FAKE_CLT:-1}" = 1 ]; then echo "@CLT@"; exit 0; fi
echo "xcode-select: error: unable to get active developer directory" >&2
exit 2
'''

# BSD mv: options only from -fhinv, no long options. FAKE_MV_FAIL as in
# test_etc_install.py's failing mv. FAKE_MV_RECREATE: once that path was
# moved away, a directory takes its place again (something in the way).
FAKE_BSD_MV = r'''#!/bin/sh
for a; do
  case "$a" in
    --) break ;;
    --*) echo "mv: illegal option -- -" >&2; exit 64 ;;
    -*) case "${a#-}" in *[!fhinv]*) echo "mv: illegal option -- ${a#-}" >&2
          echo "usage: mv [-f | -i | -n] [-hv] source target" >&2; exit 64 ;; esac ;;
    *) break ;;
  esac
done
src=""; n=0
for a; do n=$((n + 1)); if [ "$n" -eq $(($# - 1)) ]; then src=$a; fi; done
IFS=:
for word in ${FAKE_MV_FAIL:-}; do
  case "$src" in *"$word"*) echo "mv: rename $src: Permission denied" >&2; exit 1 ;; esac
done
"@REAL_MV@" "$@" || exit
if [ -n "${FAKE_MV_RECREATE:-}" ] && [ "$src" = "$FAKE_MV_RECREATE" ]; then
  mkdir "$src" && echo "in the way" > "$src/intruder"
fi
'''

# BSD readlink: only -f and -n.
FAKE_BSD_READLINK = r'''#!/bin/sh
for a; do
  case "$a" in
    --) break ;;
    -*) case "${a#-}" in *[!fn]*) echo "readlink: illegal option -- ${a#-}" >&2
          echo "usage: readlink [-fn] [file ...]" >&2; exit 1 ;; esac ;;
    *) break ;;
  esac
done
exec "@REAL_READLINK@" "$@"
'''

# Homebrew's brew, wherever its prefix is.
FAKE_BREW = r'''#!/bin/sh
echo "brew $*" >> "@LOG@"
prefix=$(cd "$(dirname "$0")/.." && pwd)
if [ "$1" = install ] && [ "${FAKE_BREW_RC:-0}" = 0 ]; then
  shift
  for formula; do
    case "$formula" in
      python3) cp "@BREW_PYTHON@" "$prefix/bin/python3"; chmod 755 "$prefix/bin/python3" ;;
      git) ln -sf "@REAL_GIT@" "$prefix/bin/git" ;;
    esac
  done
fi
exit "${FAKE_BREW_RC:-0}"
'''

# Homebrew's python3: the login shell lookup answers FAKE_PW_SHELL (never
# the real one), the machine it runs as is FAKE_PYTHON_MACHINE (default:
# what uname -m says, so native), and with FAKE_SSL_VERSION (and its major
# version, FAKE_SSL_MAJOR) the CA-certificate probe runs as if its ssl
# module linked that library; everything else goes to the recording fake
# python.
FAKE_HOMEBREW_PYTHON = r'''#!/bin/sh
if [ "$1" = -c ]; then
  case "$2" in
    *pwd.getpwuid*) echo "python3 pwd-lookup" >> "@LOG@"
      echo "${FAKE_PW_SHELL:-/nonexistent/login-shell}"; exit 0 ;;
    *os.uname*) echo "${FAKE_PYTHON_MACHINE:-$(uname -m)}"; exit 0 ;;
    *get_default_verify_paths*) if [ -n "${FAKE_SSL_VERSION:-}" ]; then
        exec "@RECORDER@" -c 'import ssl, sys
ssl.OPENSSL_VERSION = sys.argv[1]
ssl.OPENSSL_VERSION_INFO = (int(sys.argv[2]), 0, 0, 0, 0)
exec(sys.argv[3])' "$FAKE_SSL_VERSION" "$FAKE_SSL_MAJOR" "$2"
      fi ;;
  esac
fi
if [ "$1" = install.py ]; then echo "python3 install.py PATH=$PATH" >> "@LOG@"; fi
exec "@RECORDER@" "$@"
'''

# A python3 older than 3.10: every version check fails.
FAKE_OLD_PYTHON = r'''#!/bin/sh
echo "old-python3 $1" >> "@LOG@"
exit 1
'''

# python3 from PATH: never on macOS (/usr/bin/python3 is 3.9, or a stub that
# opens the Command Line Tools dialog).
PYTHON_TRAP = r'''#!/bin/sh
echo "python3-on-PATH $1" >> "@LOG@"
exit 97
'''

FAKE_CURL = r'''#!/bin/sh
echo "curl $*" >> "@LOG@"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then out=$2; shift; fi
  shift
done
if [ "${FAKE_CURL_RC:-0}" != 0 ]; then exit "$FAKE_CURL_RC"; fi
cp "@INSTALLER@" "$out"
'''

# What the fake curl "downloads": installs the fake brew into --path only.
FAKE_HOMEBREW_INSTALLER = r'''#!/bin/sh
tty=no; [ -t 0 ] && tty=yes
echo "homebrew-installer $* stdin-tty=$tty" >> "@LOG@"
prefix=""
for a; do case "$a" in --path=*) prefix=${a#--path=} ;; esac; done
if [ -z "$prefix" ]; then echo "fake Homebrew installer: no --path" >&2; exit 3; fi
if [ "${FAKE_INSTALLER_RC:-0}" != 0 ]; then exit "$FAKE_INSTALLER_RC"; fi
mkdir -p "$prefix/bin" && cp "@BREW@" "$prefix/bin/brew" && chmod 755 "$prefix/bin/brew"
'''


class FakeHomebrew:
    """A fake Homebrew under HOMEBREW_PREFIX, for EtcInstallTests subclasses."""

    def setup_homebrew(self):
        real = {}
        for name in ("uname", "mv", "readlink", "git"):
            real[name] = shutil.which(name)  # the runner's PATH: no fakes there
            if real[name] is None:
                self.skipTest(f"no {name} on PATH")
        case = self.case
        self.prefix = case / "homebrew"
        self.tmp = case / "tmp"
        self.tmp.mkdir()
        clt = case / "CommandLineTools"
        (clt / "usr" / "bin").mkdir(parents=True)
        (clt / "usr" / "bin" / "git").symlink_to(real["git"])
        recorder = case / "python3-recorder"
        os.replace(self.bin / "python3", recorder)  # test_etc_install.py's fake
        self.placeholders = {
            "@LOG@": str(self.log), "@CLT@": str(clt), "@RECORDER@": str(recorder),
            "@REAL_UNAME@": real["uname"], "@REAL_MV@": real["mv"],
            "@REAL_READLINK@": real["readlink"], "@REAL_GIT@": real["git"],
            "@BREW_PYTHON@": str(case / "python3-from-brew"),
            "@BREW@": str(case / "brew-template"),
            "@INSTALLER@": str(case / "homebrew-install.sh"),
        }
        self.put(case / "python3-from-brew", FAKE_HOMEBREW_PYTHON)
        self.put(case / "brew-template", FAKE_BREW)
        self.put(case / "homebrew-install.sh", FAKE_HOMEBREW_INSTALLER)
        self.put(self.prefix / "bin" / "brew", FAKE_BREW)
        self.put(self.prefix / "bin" / "python3", FAKE_HOMEBREW_PYTHON)
        self.put(self.bin / "python3", PYTHON_TRAP)
        self.put(self.bin / "curl", FAKE_CURL)

    def put(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in self.placeholders.items():
            text = text.replace(key, value)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def env_for(self, path=None, **extra):
        extra.setdefault("HOMEBREW_PREFIX", str(self.prefix))
        extra.setdefault("TMPDIR", str(self.tmp))
        return super().env_for(path, **extra)

    def call_lines(self) -> list[str]:
        return self.calls().splitlines()

    def brew_calls(self) -> list[str]:
        return [line for line in self.call_lines() if line.startswith("brew ")]

    def assert_no_linux_tools(self):
        """No apt, no sudo, no Linux-only tools, no python3 from PATH."""
        for tool in ("sudo", "apt-get", "getent", "dpkg", "python3-on-PATH"):
            self.assertFalse([line for line in self.call_lines()
                              if line.startswith(tool + " ")], tool)

    def assert_nothing_changed(self, completed, brew=()):
        """Stopped before any change (only the given brew calls happened)."""
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.installer_runs(), [])
        self.assertEqual(self.brew_calls(), list(brew))
        self.assert_no_linux_tools()


class DarwinSimulationTests(FakeHomebrew, base.EtcInstallTests):
    """The Darwin branch on any Linux host (the base class needs Ubuntu)."""

    __unittest_skip__ = False

    def setUp(self):
        super().setUp()
        self.setup_homebrew()
        self.put(self.bin / "uname", FAKE_UNAME)
        self.put(self.bin / "sw_vers", FAKE_SW_VERS)
        self.put(self.bin / "sysctl", FAKE_SYSCTL)
        self.put(self.bin / "xcode-select", FAKE_XCODE_SELECT)
        self.put(self.bin / "mv", FAKE_BSD_MV)
        self.put(self.bin / "readlink", FAKE_BSD_READLINK)
        for name in ("getent", "dpkg"):  # macOS has neither
            self.put(self.bin / name, base.FAKE_RECORDER.format(name=name, log=self.log,
                                                                rc=127))

    def failing_mv(self, real_mv=None):
        """The BSD-like mv fails the FAKE_MV_FAIL renames itself."""

    def no_homebrew(self) -> Path:
        return self.case / "no-homebrew"

    # -- the macOS prerequisites ---------------------------------------------------

    def test_darwin_fresh_install_runs_homebrew_python(self):
        completed = self.run_piped(args=list(self.FLAGS))
        out, err = completed.stdout.decode(), completed.stderr.decode()
        self.assertEqual(completed.returncode, 0, err)
        self.assertIn("All Done!", out)
        runs = self.installer_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["command"], "install")
        # Homebrew's python3 by its absolute path (set -x shows it).
        self.assertIn(f"+ {self.prefix}/bin/python3 install.py --no-packages --no-gui "
                      "--no-shell-change\n", err)
        calls = self.call_lines()
        self.assertIn("sw_vers -productVersion SYSTEM_VERSION_COMPAT=0", calls)
        self.assertIn("sysctl -n sysctl.proc_translated", calls)
        # install.py (and what it starts) finds Homebrew first on PATH.
        paths = [line.split("PATH=", 1)[1] for line in calls
                 if line.startswith("python3 install.py PATH=")]
        self.assertEqual(len(paths), 1, calls)
        self.assertTrue(paths[0].startswith(f"{self.prefix}/bin:{self.prefix}/sbin:"), paths[0])
        # git from the Command Line Tools, python3 already in Homebrew
        self.assertEqual(self.brew_calls(), [])
        # The CA probe's openssl formula is only for its error message.
        self.assertNotIn("openssl@", out + err)
        self.assertNotIn("WARNING", err)
        self.assert_no_linux_tools()

    def test_darwin_missing_git_and_python3_come_from_brew(self):
        (self.prefix / "bin" / "python3").unlink()
        completed = self.run_piped(args=list(self.FLAGS), FAKE_CLT="0")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn("Missing: git python3. Installing with Homebrew: brew install git python3\n",
                      completed.stdout.decode())
        self.assertEqual(self.brew_calls(), ["brew install git python3"])
        self.assertTrue((self.prefix / "bin" / "git").is_symlink())
        self.assertEqual(len(self.installer_runs()), 1)
        self.assert_no_linux_tools()

    def test_darwin_too_old_python3_comes_from_brew(self):
        self.put(self.prefix / "bin" / "python3", FAKE_OLD_PYTHON)
        completed = self.run_piped(args=list(self.FLAGS))
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(self.brew_calls(), ["brew install python3"])
        self.assertEqual(len(self.installer_runs()), 1)

    def test_darwin_failed_brew_install_stops_before_any_change(self):
        (self.prefix / "bin" / "python3").unlink()
        completed = self.run_piped(FAKE_BREW_RC="1")
        self.assertIn("ERROR: brew install python3 failed. Run 'brew doctor', fix what it "
                      "reports, then rerun.", completed.stderr.decode())
        self.assert_nothing_changed(completed, brew=["brew install python3"])

    def test_darwin_python3_still_too_old_after_brew_stops(self):
        self.put(self.prefix / "bin" / "python3", FAKE_OLD_PYTHON)
        self.put(self.case / "python3-from-brew", FAKE_OLD_PYTHON)
        completed = self.run_piped()
        self.assertIn(f"ERROR: {self.prefix}/bin/python3 is not python 3.10 or newer. "
                      "Run 'brew upgrade python3'", completed.stderr.decode())
        self.assert_nothing_changed(completed, brew=["brew install python3"])

    def test_darwin_python3_under_rosetta_is_refused(self):
        # An Intel-only python3 on Apple silicon (an Intel Homebrew that
        # HOMEBREW_PREFIX still names): install.py would refuse it, but only
        # after the checkout was replaced.
        completed = self.run_piped(FAKE_PYTHON_MACHINE="x86_64")
        self.assertIn(f"ERROR: {self.prefix}/bin/python3 runs as 'x86_64' on this arm64 Mac "
                      "(an Intel-only build under Rosetta 2?)", completed.stderr.decode())
        self.assert_nothing_changed(completed)

    def test_darwin_python3_without_ca_certificates_stops(self):
        # No CA file nor directory for python's default context: install.py's
        # downloads would fail, so nothing starts. The remedy names the
        # openssl formula that this python links (Homebrew's python3 links
        # openssl@4 now); a python without OpenSSL gets the CA bundle alone.
        missing = str(self.case / "no-such-ca")
        this_python = (f"openssl@{ssl.OPENSSL_VERSION_INFO[0]}"
                       if ssl.OPENSSL_VERSION.startswith("OpenSSL") else None)
        cases = {"this python": ({}, this_python),
                 "OpenSSL 4": ({"FAKE_SSL_VERSION": "OpenSSL 4.0.3", "FAKE_SSL_MAJOR": "4"},
                               "openssl@4"),
                 "OpenSSL 3": ({"FAKE_SSL_VERSION": "OpenSSL 3.6.5", "FAKE_SSL_MAJOR": "3"},
                               "openssl@3"),
                 "LibreSSL": ({"FAKE_SSL_VERSION": "LibreSSL 2.8.3", "FAKE_SSL_MAJOR": "2"},
                              None)}
        for name, (extra, formula) in cases.items():
            with self.subTest(name):
                completed = self.run_piped(SSL_CERT_FILE=missing, SSL_CERT_DIR=missing, **extra)
                err = completed.stderr.decode()
                reinstall = f"ca-certificates {formula}" if formula else "ca-certificates"
                self.assertIn(f"ERROR: {self.prefix}/bin/python3 finds no CA certificates, so "
                              f"its downloads would fail. Run 'brew reinstall {reinstall}' "
                              "(or, for a python.org Python, 'Install Certificates.command' "
                              "in its /Applications/Python 3.x folder), then rerun.\n", err)
                self.assert_nothing_changed(completed)

    # -- which Macs ----------------------------------------------------------------

    def test_darwin_supported_releases_pass(self):
        cases = [("arm64", release + ".1") for release in plat.SUPPORTED_MACOS_RELEASES]
        cases += [("x86_64", release) for release in plat.MACOS_INTEL_RELEASES]
        for arch, version in cases:
            with self.subTest(arch=arch, version=version):
                completed = self.run_piped(args=["--dry-run", *self.FLAGS], FAKE_UNAME_M=arch,
                                           FAKE_MACOS_VERSION=version,
                                           FAKE_PROC_TRANSLATED="0" if arch == "arm64" else "none")
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(len(self.installer_runs()), len(cases))

    def test_darwin_unsupported_release_is_refused_before_any_change(self):
        cases = [("arm64", v) for v in ("14.7.1", "10.16", "28.0", "", "abc", "1")]
        cases += [("x86_64", v) for v in ("27.0", "14.7")]
        for arch, version in cases:
            with self.subTest(arch=arch, version=version):
                completed = self.run_piped(FAKE_UNAME_M=arch, FAKE_MACOS_VERSION=version,
                                           FAKE_PROC_TRANSLATED="0" if arch == "arm64" else "none")
                self.assertIn(f"ERROR: unsupported macOS release '{version or 'unknown'}' on "
                              f"{arch}; supported: {ARM64_RELEASES} on Apple silicon; "
                              f"{INTEL_RELEASES} on Intel (best effort).\n",
                              completed.stderr.decode())
                self.assert_nothing_changed(completed)

    def test_darwin_intel_is_best_effort_with_a_warning(self):
        completed = self.run_piped(args=list(self.FLAGS), FAKE_UNAME_M="x86_64",
                                   FAKE_PROC_TRANSLATED="none", FAKE_MACOS_VERSION="15.7.1")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn("WARNING: Intel Macs are best effort only", completed.stderr.decode())
        self.assertEqual(len(self.installer_runs()), 1)

    def test_darwin_rosetta_terminal_is_refused(self):
        # Rosetta 2 says x86_64 on Apple silicon: it would pick Intel's prefix.
        completed = self.run_piped(FAKE_UNAME_M="x86_64", FAKE_PROC_TRANSLATED="1",
                                   FAKE_MACOS_VERSION="26.0")
        err = completed.stderr.decode()
        self.assertIn("ERROR: this terminal runs under Rosetta 2", err)
        self.assertIn("'arch -arm64 /bin/zsh -l'", err)
        self.assert_nothing_changed(completed)

    def test_darwin_unknown_architecture_is_refused(self):
        completed = self.run_piped(FAKE_UNAME_M="i386")
        self.assertIn("ERROR: unsupported architecture 'i386'", completed.stderr.decode())
        self.assert_nothing_changed(completed)

    def test_other_systems_are_refused_naming_both(self):
        cases = {"FreeBSD": "unsupported system 'FreeBSD'; Ubuntu 22.04/24.04, or macOS "
                            f"{ARM64_RELEASES} on Apple silicon ({INTEL_RELEASES} on Intel, "
                            "best effort), is required.",
                 "fail": "cannot tell which system this is ('uname -s' failed)."}
        for name, message in cases.items():
            with self.subTest(name):
                completed = self.run_piped(FAKE_UNAME_S=name)
                self.assertIn(f"ERROR: {message}\n", completed.stderr.decode())
                self.assert_nothing_changed(completed)
        self.assertEqual(self.calls(), "")  # not even sw_vers

    def test_darwin_root_is_refused_with_the_macos_message(self):
        completed = self.run_piped(_DOTFILES_INSTALL_SIMULATE_ROOT="1")
        self.assertIn("ERROR: do not run this as root (or with sudo). Run it as your normal "
                      "user; Homebrew refuses to run as root.\n", completed.stderr.decode())
        self.assertEqual(self.calls(), "")
        self.assert_nothing_changed(completed)

    # -- no Homebrew yet -------------------------------------------------------------

    def homebrew_steps(self, prefix: Path) -> str:
        return ("  xcode-select --install\n"
                f'  /bin/bash -c "$(curl -fsSL {HOMEBREW_INSTALL_URL})" install.sh '
                f"--path={prefix}\n")

    def test_darwin_without_homebrew_and_without_terminal_stops(self):
        empty = self.no_homebrew()
        completed = self.run_piped(HOMEBREW_PREFIX=str(empty))
        self.assertIn(f"ERROR: Homebrew is not installed in {empty}, and there is no terminal "
                      "for its installer. In a terminal, run\n" + self.homebrew_steps(empty)
                      + "then rerun this.\n", completed.stderr.decode())
        self.assertFalse([line for line in self.call_lines() if line.startswith("curl ")])
        self.assert_nothing_changed(completed)

    def test_darwin_default_prefix_gets_the_official_commands(self):
        # HOMEBREW_PREFIX unset: /opt/homebrew, and Homebrew's own commands.
        if os.path.lexists("/opt/homebrew/bin/brew"):
            self.skipTest("this host has /opt/homebrew/bin/brew")
        completed = self.run_piped(HOMEBREW_PREFIX=None)
        self.assertIn("ERROR: Homebrew is not installed in /opt/homebrew, and there is no "
                      "terminal for its installer. In a terminal, run\n"
                      "  xcode-select --install\n"
                      f'  /bin/bash -c "$(curl -fsSL {HOMEBREW_INSTALL_URL})"\n'
                      "then rerun this.\n", completed.stderr.decode())
        self.assert_nothing_changed(completed)

    def test_darwin_intel_without_homebrew_is_refused_even_on_a_terminal(self):
        empty = self.no_homebrew()
        status, out = self.run_in_terminal(typed=b"y\n", HOMEBREW_PREFIX=str(empty),
                                           FAKE_UNAME_M="x86_64", FAKE_PROC_TRANSLATED="none")
        self.assertNotEqual(status, 0, out)
        self.assertIn(f"Homebrew is not installed in {empty}. Its official installer supports "
                      "only Apple silicon now", out)
        self.assertIn("(xcode-select --install)", out)
        self.assertNotIn("[y/N]", out)
        self.assertFalse([line for line in self.call_lines() if line.startswith("curl ")])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.installer_runs(), [])

    def test_darwin_homebrew_installer_runs_after_a_yes_on_the_terminal(self):
        new = self.case / "new-homebrew"
        status, out = self.run_in_terminal(args=list(self.FLAGS), typed=b"y\n",
                                           HOMEBREW_PREFIX=str(new))
        self.assertEqual(status, 0, out)
        self.assertIn(f"Homebrew is not installed in {new};", out)
        self.assertIn("[y/N]", out)
        # Downloaded to a temporary file, run with the terminal as stdin
        # and the custom prefix, then removed.
        curl = [line.split() for line in self.call_lines() if line.startswith("curl ")]
        self.assertEqual(len(curl), 1)
        self.assertEqual(curl[0][1:3], ["-fsSL", "-o"])
        self.assertTrue(curl[0][3].startswith(f"{self.tmp}/homebrew-install."), curl[0])
        self.assertEqual(curl[0][4:], [HOMEBREW_INSTALL_URL])
        self.assertEqual([line for line in self.call_lines()
                          if line.startswith("homebrew-installer ")],
                         [f"homebrew-installer --path={new} stdin-tty=yes"])
        self.assertEqual(list(self.tmp.glob("homebrew-install.*")), [])
        # The new Homebrew has no python3 yet; then it goes on as usual.
        self.assertTrue((new / "bin" / "brew").exists())
        self.assertEqual(self.brew_calls(), ["brew install python3"])
        self.assertEqual(len(self.installer_runs()), 1)
        self.assertIn("All Done!", out)
        self.assertEqual(self.zsh_runs(), ["zsh -l stdin-tty=yes stdout-tty=yes"])

    def test_darwin_declined_homebrew_installer_changes_nothing(self):
        empty = self.no_homebrew()
        for typed in (b"\n", b"n\n", b"no\n"):
            with self.subTest(typed=typed):
                status, out = self.run_in_terminal(typed=typed, HOMEBREW_PREFIX=str(empty))
                self.assertNotEqual(status, 0, out)
                self.assertIn("[y/N]", out)
                self.assertIn("Homebrew is required on macOS. Install it, then rerun:", out)
                self.assertFalse([line for line in self.call_lines()
                                  if line.startswith("curl ")])
                self.assertFalse(os.path.lexists(empty))
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertEqual(self.installer_runs(), [])

    def test_darwin_failed_homebrew_installer_stops(self):
        new = self.case / "new-homebrew"
        cases = {"installer fails": ({"FAKE_INSTALLER_RC": "1"},
                                     "the Homebrew installer failed (status 1, see above)"),
                 "download fails": ({"FAKE_CURL_RC": "22"},
                                    "cannot download the Homebrew installer")}
        for name, (extra, message) in cases.items():
            with self.subTest(name):
                status, out = self.run_in_terminal(typed=b"y\n", HOMEBREW_PREFIX=str(new),
                                                   **extra)
                self.assertNotEqual(status, 0, out)
                self.assertIn(message, out)
                self.assertFalse(os.path.lexists(new / "bin" / "brew"))
                self.assertEqual(list(self.tmp.glob("homebrew-install.*")), [])
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertEqual(self.installer_runs(), [])

    # -- the rest of the run -------------------------------------------------------

    def test_darwin_missing_zsh_falls_back_to_the_pwd_login_shell(self):
        # macOS has no getent: python's pwd module (Directory Services).
        path = self.path_without_zsh()
        login_log = self.case / "login.log"
        login = self.case / "login-shell" / "fixture-sh"
        login.parent.mkdir()
        login.write_text(base.FAKE_ZSH.replace("@LOG@", str(login_log))
                         .replace("zsh $*", "fixture-sh $*"))
        login.chmod(0o755)
        status, out = self.run_in_terminal(PATH=path, FAKE_PW_SHELL=str(login))
        self.assertEqual(status, 0, out)
        self.assertIn("python3 pwd-lookup", self.call_lines())
        self.assertEqual(login_log.read_text().splitlines(),
                         ["fixture-sh -l stdin-tty=yes stdout-tty=yes"])
        self.assertIn("Starting a new fixture-sh with the new setup.", out)
        self.assert_no_linux_tools()  # no getent
        # No usable login shell: it just ends.
        for shell in ("/usr/sbin/nologin", "zsh", str(self.case / "no-such-shell")):
            with self.subTest(shell=shell):
                status, out = self.run_in_terminal(PATH=path, FAKE_PW_SHELL=shell)
                self.assertEqual(status, 0, out)
                self.assertIn("All Done!", out)
                self.assertNotIn("Starting a new", out)

    def test_darwin_rename_never_moves_the_clone_into_what_is_in_the_way(self):
        # BSD mv has no -T: given an existing directory, it moves the clone
        # into it. Something at ~/.dotfiles again after the old checkout was
        # moved aside: neither the clone nor the old checkout goes in there.
        old = self.old_checkout()
        completed = self.run_piped(FAKE_MV_RECREATE=str(old))
        err = completed.stderr.decode()
        self.assertNotEqual(completed.returncode, 0)
        moved = self.backups()[0] / "dotfiles"
        self.assertIn(f"cannot rename {moved} to {old}: {old} is in the way\n", err)
        self.assertIn(f"nor put the previous {old} back. It is in {moved};", err)
        self.assertEqual([p.name for p in old.iterdir()], ["intruder"])
        self.assertEqual((moved / "notes.txt").read_text(), "my old dotfiles\n")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.installer_runs(), [])

    def test_darwin_curl_pipe_sh_posix_mode_works(self):
        # macOS 'curl ... | sh': /bin/sh is bash 3.2 in POSIX mode, started
        # as sh (or, the same mode, with --posix).
        for argv in (["sh"], [base.BASH, "--posix"]):
            with self.subTest(argv=argv):
                shutil.rmtree(self.home / ".dotfiles", ignore_errors=True)
                if self.py_log.exists():
                    self.py_log.unlink()
                completed = subprocess.run([*argv, "-s", "--", *self.FLAGS],
                                           executable=base.BASH,
                                           input=base.ETC_INSTALL.read_bytes(),
                                           env=self.env_for(), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, timeout=120,
                                           start_new_session=True)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                self.assertIn("All Done!", completed.stdout.decode())
                self.assertIn("git clone --recursive -j8", completed.stderr.decode())
                self.assertEqual(len(self.installer_runs()), 1)

    def test_darwin_sh_install_is_in_place(self):
        # macOS 'sh ~/.dotfiles/install': bash in POSIX mode runs the wrapper.
        dotfiles = self.checkout()
        head = self.local_work(dotfiles)
        completed = subprocess.run([base.BASH, "--posix", str(dotfiles / "install"),
                                    *self.FLAGS], env=self.env_for(), cwd=str(self.case),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=120, start_new_session=True)
        self.assert_in_place(completed, dotfiles, head)


# The platform-neutral flows of test_etc_install.py, again on the Darwin
# branch (as test_darwin_<name>). Not here: the getent fallback and the
# apt-get prerequisites (Linux only), and the fresh install, which has its
# own Darwin test above.
SHARED_FLOWS = (
    "test_install_in_dotfiles_installs_that_checkout_as_it_is",
    "test_install_started_by_sh_zsh_or_bash_or_from_anywhere_is_in_place",
    "test_install_through_symlinks_is_in_place",
    "test_install_from_a_clone_elsewhere_changes_nothing",
    "test_etc_install_run_from_dotfiles_is_in_place",
    "test_bash_source_from_the_environment_is_refused",
    "test_install_in_dotfiles_in_a_terminal_ends_in_a_login_zsh",
    "test_help_dry_run_and_status_install_nothing_and_start_no_shell",
    "test_install_in_place_refuses_root",
    "test_plain_clone_gets_its_missing_submodules",
    "test_terminal_run_ends_in_a_login_zsh_on_the_terminal",
    "test_failed_install_never_starts_the_shell",
    "test_failed_clone_never_starts_the_shell",
    "test_opt_out_help_and_redirected_output_never_start_the_shell",
    "test_failed_exec_keeps_the_exit_status",
    "test_existing_unrelated_dotfiles_is_moved_aside",
    "test_clone_with_other_origin_is_moved_aside",
    "test_dirty_clone_of_this_repository_is_moved_aside",
    "test_regular_file_is_moved_aside",
    "test_symlink_to_a_checkout_is_moved_aside_and_target_kept",
    "test_broken_symlink_is_moved_aside",
    "test_failed_clone_leaves_the_existing_checkout_in_place",
    "test_failed_rename_into_place_puts_the_old_checkout_back",
    "test_failed_put_back_says_where_the_old_checkout_is",
    "test_clone_with_submodules_is_complete_after_the_rename",
    "test_existing_clean_clone_is_pulled_not_moved",
    "test_ssh_spelling_of_origin_counts_as_this_repository",
    "test_ssh_origin_of_another_repository_is_moved_aside",
    "test_bad_url_stops_before_install_py",
    "test_root_is_refused",
    "test_truncated_script_does_nothing",
)
for _name in SHARED_FLOWS:
    setattr(DarwinSimulationTests, "test_darwin_" + _name[len("test_"):],
            getattr(base.EtcInstallTests, _name))


@unittest.skipUnless(sys.platform == "darwin", "needs a real Mac")
class RealMacTests(FakeHomebrew, base.EtcInstallTests):
    """On a real Mac: the real uname, sw_vers, sysctl, xcode-select, BSD mv
    and /bin/sh (bash 3.2); only Homebrew is fake (HOMEBREW_PREFIX)."""

    # (the base class is skipped off Ubuntu; skipUnless alone would inherit that)
    __unittest_skip__ = sys.platform != "darwin"
    __unittest_skip_why__ = "needs a real Mac"

    def setUp(self):
        super().setUp()
        self.setup_homebrew()

    def refusal(self) -> str | None:
        """What etc/install must refuse this Mac with, by installer/platform.py's
        policy; None when it is supported."""
        env = {**os.environ, "SYSTEM_VERSION_COMPAT": "0"}
        version = subprocess.run(["/usr/bin/sw_vers", "-productVersion"], env=env, check=True,
                                 stdout=subprocess.PIPE, text=True).stdout.strip()
        translated = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True).stdout.strip()
        if translated == "1":
            return "runs under Rosetta 2"
        machine = os.uname().machine
        releases = plat.SUPPORTED_MACOS_RELEASES if machine == "arm64" else plat.MACOS_INTEL_RELEASES
        if version.split(".")[0] not in releases:
            return "unsupported macOS release"
        return None

    def require_supported(self):
        refusal = self.refusal()
        if refusal:
            self.skipTest(f"this Mac is refused ({refusal})")

    def test_real_mac_gate_follows_the_policy(self):
        refusal = self.refusal()
        completed = self.run_piped(args=list(self.FLAGS))
        if refusal:
            self.assertIn(refusal, completed.stderr.decode())
            self.assert_nothing_changed(completed)
            return
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn("All Done!", completed.stdout.decode())
        self.assertIn(f"+ {self.prefix}/bin/python3 install.py --no-packages",
                      completed.stderr.decode())
        self.assert_no_linux_tools()

    def test_real_mac_existing_checkout_is_moved_aside(self):
        self.require_supported()
        base.EtcInstallTests.test_existing_unrelated_dotfiles_is_moved_aside(self)

    def test_real_mac_install_through_symlinks_is_in_place(self):
        self.require_supported()
        base.EtcInstallTests.test_install_through_symlinks_is_in_place(self)

    def test_real_mac_curl_pipe_sh(self):
        self.require_supported()
        completed = subprocess.run(["/bin/sh", "-s", "--", *self.FLAGS],
                                   input=base.ETC_INSTALL.read_bytes(), env=self.env_for(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=120, start_new_session=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(len(self.installer_runs()), 1)


# Only the tests above, not the Ubuntu ones these classes inherit.
for _cls in (DarwinSimulationTests, RealMacTests):
    for _name in dir(base.EtcInstallTests):
        if _name.startswith("test_") and _name not in _cls.__dict__:
            setattr(_cls, _name, None)
del _cls, _name  # a class left in the module would be collected again

if __name__ == "__main__":
    unittest.main()
