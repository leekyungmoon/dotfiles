# 🧪 Support matrix

Supported targets are **Ubuntu 22.04 (jammy)** and **Ubuntu 24.04
(noble)** on **amd64** and **arm64**, and **macOS 15 Sequoia, 26 Tahoe and
27 on Apple silicon** (arm64), installed by a normal (non-root) user.
**Intel Macs** on macOS 15 and 26 are **best effort**: Homebrew supports
them at Tier 3 only (no new bottles, so formulae may build from source),
and its official installer refuses them, so an existing Homebrew in
`/usr/local` is required. Anything else — another macOS release, a terminal
running under Rosetta 2 on Apple silicon, an Intel-only `python3` there — is
refused before any change, by `etc/install` (the one-liner) first, and
again by `install.py` itself.

Status values:

- **pass** — verified on a real machine of that kind, with the date.
- **fail** — verified broken; see notes.
- **pending verification** — not yet run on a real machine. Nothing in
  this column is claimed to work.


## Results

### Ubuntu

| Ubuntu | Arch | One-liner install | `dotfiles update` | `restore --baseline` | GNOME settings | input-remapper tabs | Verified on |
| ------ | ---- | ----------------- | ----------------- | -------------------- | -------------- | ------------------- | ----------- |
| 24.04 | amd64 | pass (over an existing wookayin/dotfiles install) | pending verification | pending verification | pending verification | pending verification | 2026-09-28, 24.04.5 amd64, over SSH |
| 24.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | amd64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |

### macOS

No Mac has run any of this yet. CI runs on GitHub's macOS runners (below)
are pre-release checks, not results: a runner is a VM whose user is
logged in automatically.

| macOS | Arch | One-liner (`/bin/bash` 3.2) | `dotfiles update` | `restore --baseline` | tmux started and restored at login | Autosave every minute, no client | `dotfiles services` | Verified on |
| ----- | ---- | --------------------------- | ----------------- | -------------------- | ---------------------------------- | -------------------------------- | ------------------- | ----------- |
| 26 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 15 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 27 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 26 | x86_64 (best effort) | pending verification | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 15 | x86_64 (best effort) | pending verification | pending verification | pending verification | pending verification | pending verification | pending verification | — |


## Pre-release checks (2026-09-28)

What was checked before publishing, none of it on a fresh target machine:

| Check | 22.04 amd64 | 22.04 arm64 | 24.04 amd64 | 24.04 arm64 |
| ----- | ----------- | ----------- | ----------- | ----------- |
| apt profile resolves with `--no-install-recommends`, no forbidden package, no removal (`apt-get -s` against the release's archive) | pass | pass | pass | pass |
| Pinned tool artifacts: checksum/signature entry exists, URL answers | pass | pass | pass | pass |
| Pinned tools downloaded, verified and run (`--version`) | not run | not run | pass (node, nvim, fzf, codex) | not run |
| GNOME keys validated against the release's compiled schemas | pass | pass (same `all` packages) | pass | pass (same `all` packages) |
| tmux config and fzf pickers on the release's tmux (3.2a / 3.4 binaries, isolated server) | pass | not run | pass | not run |
| Installer, update, restore and one-liner flows in disposable homes (fake apt/sudo/systemctl), GNOME apply/restore on a private D-Bus, tmux save/kill/restore on isolated servers | — | — | pass (on a 24.04 amd64 host) | — |
| Unit tests on the release's Python | pass (3.10) | not run | pass (3.12) | not run |

Not verified yet anywhere: a real one-liner run on a fresh machine (real
sudo apt, Chrome repo, chsh, systemd user units at login), live GNOME
pickup of the settings, input-remapper with a physical keyboard, and any
execution on arm64. Those rows in the results table stay "pending
verification" until someone runs them.

### macOS (2026-10-11)

Checked before publishing, all on an Ubuntu 24.04 amd64 host, none on a Mac:

- The installer's macOS paths with a simulated Darwin platform and
  scripted Homebrew and `launchctl` answers (unit tests).
- `etc/install` and `./install` under GNU bash 3.2.57 (the version of
  macOS's `/bin/bash`) and BSD-strict stand-ins for the macOS tools; the
  LaunchAgents' scripts also under `dash` and bash 3.2's POSIX mode.
- The darwin tool artifacts: each checksum taken from the signed or pinned
  release file; the node, neovim and fzf archives downloaded and inspected
  (Mach-O architecture, minimum macOS, code signature); the Codex CLI and
  Claude Code ones checked by size, digest and a read of their headers
  only. None of them was run.
- The Homebrew formula closure per architecture, read from formulae.brew.sh:
  no forbidden formula; every one bottled for Apple silicon on 15, 26 and
  27; 20 without an Intel bottle.

### Pre-release checks on GitHub's runners

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs these. A
green run still leaves every macOS result above "pending verification";
record each run here with its URL and date.

| Check (job) | macos-15 (arm64) | macos-26 (arm64) | macos-15-intel | macos-26-intel |
| ----------- | ---------------- | ---------------- | -------------- | -------------- |
| Unit tests, file by file, Homebrew `python3` (`unit-macos`) | not run yet | not run yet | not run yet (best effort) | not run yet (best effort) |
| `darwin_proc` tests and the AI hook `--self-test` under `/usr/bin/python3` (3.9) and Homebrew `python3` (`unit-macos`) | not run yet | not run yet | not run yet (best effort) | not run yet (best effort) |
| Shell and zsh tests under `/bin/bash` 3.2 and `/bin/zsh` (`unit-macos`) | not run yet | not run yet | not run yet (best effort) | not run yet (best effort) |
| Bootstrap simulation, LaunchAgent scripts, tmux-resurrect on Homebrew tmux (`unit-macos`) | not run yet | not run yet | not run yet (best effort) | not run yet (best effort) |
| `etc/install` and `./install` refusals under the real `/bin/bash` and `/bin/sh` (`bash32-macos`) | not run yet | — | — | — |
| One-liner, both LaunchAgents, save without a client, restore after kill-server, repair, `services disable`/`enable`, `restore --baseline` (`e2e-macos`, [`tests/macos/e2e.sh`](../tests/macos/e2e.sh)) | not run yet | not run yet | — | — |

| Check (job) | ubuntu-22.04 | ubuntu-24.04 |
| ----------- | ------------ | ------------ |
| Unit tests, system `python3` (`unit-linux`) | not run yet | not run yet |
| Shell and zsh tests, zsh self-reload (`unit-linux`) | not run yet | not run yet |
| Integration tests, file by file (`integration-linux`) | not run yet | not run yet |
| Bootstrap and shell tests under GNU bash 3.2.57 (`bash32-linux`) | — | not run yet |
| ShellCheck 0.11.0, `dash -n`, `zsh -n`, Python 3.9 grammar (`lint`) | — | not run yet |

Runs recorded: none yet (add "date, commit, run URL, result" lines here).


## Known release differences

These come from the package manifests (`manifests/packages.json`,
`manifests/packages-macos.json`, `desktop/gnome-settings.json`), not from
the results above.

| | 22.04 | 24.04 |
| - | ----- | ----- |
| tmux (Ubuntu package) | 3.2a — degraded, see below | 3.4 |
| input-remapper | 1.4.0 | 2.0.1 |
| GNOME Shell | 42 | 46 |
| python3 | 3.10 | 3.12 |
| `Super+Ctrl+1..9` (open new app window) | not available, skipped as not-applicable | managed |
| `Ctrl+Super+Left/Right` tab chord (input-remapper preset) | not available: input-remapper 1.4 cannot express it, so no preset is written; `status` shows it as `PENDING_GUI` (`input-remapper-1.4-cannot-express-intent`) and it is never retried | managed |

On 22.04 the "input-remapper tabs" column therefore records whether the
installer reports the chord as unavailable, not whether the chord works.

### tmux 3.2a on 22.04

The Ubuntu 22.04 tmux is accepted as-is (tmux ships no official binaries,
and building it would need a C toolchain, which this profile never
installs). The configuration loads without errors, but:

- `allow-passthrough` does not exist before 3.3, so terminal passthrough
  sequences are dropped.
- Dragging windows on the status line to reorder them is not bound
  (needs 3.4).
- `fzf --tmux` popups need tmux 3.3, so the `prefix S` session picker, the
  `prefix @` file picker and the `prefix ?` cheatsheet run plain fzf inside
  `display-popup` instead.


## macOS differences

What a Mac gets instead of what Ubuntu gets. Like everything on macOS here,
pending verification.

| | Ubuntu | macOS |
| - | ------ | ----- |
| Packages | apt, through `sudo` | Homebrew formulae as you, never `sudo`: `tmux`, `git`, `gnupg` (for `gpgv`), `python3`, `ripgrep`, `fd`, `tree` — only the missing ones; an installed formula is not upgraded |
| Pinned tools (node, neovim, fzf, Codex CLI, Claude Code, oh-my-codex) | linux builds | the darwin builds of the same versions, verified the same way (`gpgv` from Homebrew's `gnupg`) |
| Python that runs `install.py` | the system `python3` (3.10 or newer) | Homebrew's `python3` (3.10 or newer, `brew install python3` when missing); the Command Line Tools' `/usr/bin/python3` (3.9) is refused |
| Shell scripts run by | bash 5, GNU tools | `/bin/bash` 3.2.57 and `/bin/sh`, BSD tools |
| tmux at login, autosave every minute | systemd user units (`tmux.service`, `tmux-resurrect-autosave.timer`) | two LaunchAgents in your GUI login ([details](tmux-auto-restore.ko.md#macos)); they load only in a desktop login, not over SSH, and not before FileVault is unlocked |
| Save when you log out or shut down | `tmux.service` saves as it stops | none: changes since the last minute's save are lost; nothing is saved while the Mac sleeps |
| Privacy permissions of programs in tmux panes | — | in the tmux the LaunchAgent starts at login, Terminal's Local Network and Files & Folders permissions do not apply ([details](tmux-auto-restore.ko.md#로컬-네트워크와-파일-및-폴더-접근)) |
| Codex / Claude resume commands | read from `/proc` | read with libproc and `sysctl` (`installer/darwin_proc.py`); if that does not work, saves go on without them, with one warning per save |
| `tmux-256color` terminal description | from ncurses | compiled into `~/.terminfo` when the system lacks it |
| GNOME settings, input-remapper, the gtk/X11/terminator configs | managed | not applicable: no desktop phase, files not installed |
| `pbcopy` / `pbpaste` | Wayland, X11 or OSC52 helpers | the system `/usr/bin/pbcopy` and `pbpaste` (with their own options); over SSH, `pbcopy` writes OSC52 and `pbpaste` fails with an explanation |
| `Ctrl+V` in tmux | smart paste (images to Codex/Claude, text to shells) | passed through to the program (Codex and Claude read images on `Ctrl+V`); `Cmd+V` pastes text |
| JetBrainsMono Nerd Font | installed into `~/.local/share/fonts` | not installed (Terminal and iTerm2 do not read that folder); `brew install --cask font-jetbrains-mono-nerd-font` |
| Neovim's python3 provider (`pynvim`) | apt `python3-pynvim` | not installed: Homebrew's Python is externally managed, and nothing is `pip`-installed; point `PYTHON3_HOST_PROG` at a virtualenv's python that has `pynvim` |
| git credentials over HTTPS | `credential.helper cache --timeout 60` | Apple's and Homebrew's git also use `osxkeychain`, so credentials persist in the Keychain |
| Login shell | `chsh`, through `sudo` when the account has no usable password | zsh already is the default; otherwise `chsh -s /bin/zsh` in a terminal (it asks for your password), or the command is printed |
| zsh self-reload hand-over | `XDG_RUNTIME_DIR` (memory) | `$TMPDIR/personal-dotfiles` (disk, 0700); a session that keeps no history keeps none across a reload |
| Terminal.app | — | per-window shell sessions off (`SHELL_SESSIONS_DISABLE=1`): one `~/.zsh_history` |

Bootstrap: Homebrew is looked for in `/opt/homebrew` (Apple silicon) or
`/usr/local` (Intel), or in an absolute `HOMEBREW_PREFIX`. git comes from
Homebrew or the Command Line Tools (else `brew install git`), and
`install.py` runs under Homebrew's `python3`. When Homebrew is missing on
Apple silicon, the one-liner asks (y/N, in a terminal) before running
Homebrew's official installer, which asks for an administrator's password
and installs the Command Line Tools if needed; without a terminal, or on an
Intel Mac, it stops before any change and prints what to run.

Homebrew's own setup: do not add `eval "$(brew shellenv)"` to
`~/.zprofile`, as Homebrew's installer suggests. `~/.zprofile` is a link
into `~/.dotfiles`, so that would edit the checkout, and `zsh/zshenv`
already exports what `brew shellenv` does, with Homebrew's `bin` and `sbin`
behind `~/.local/bin` and the pinned tools. Put local additions in
`~/.zprofile.local` or `~/.zshrc.local`.

Intel Macs: the installer warns first. Homebrew has no x86_64 bottles for
`tmux`, `git`, `gnupg`, `python` and `openssl` any more, so `brew install`
builds them with the Command Line Tools, which can take an hour or more and
may pull in build-only dependencies; a run that timed out continues where
it stopped on the next one. Every pinned tool has an Intel build.

If a Homebrew step fails: `brew doctor`, fix what it reports, rerun
`dotfiles repair`. A formula that is installed but not linked fails with
the `brew link <formula>` command to run.


## What is never installed

On every cell: no Docker/containers, ROS, `gh`/`glab`, nvm/conda/pyenv,
apt Node.js, C/C++ toolchain, GNOME Shell/`ubuntu-desktop` or display
manager. The apt plan is simulated first and the install stops if any of
these would be pulled in.

On macOS the same holds for Homebrew: before installing, the formulae and
all their dependencies are checked, and the install stops if any of them
is Node.js/npm/nvm, Homebrew's own fzf, neovim, Codex or Claude Code (they
would shadow the pinned ones), a compiler toolchain (gcc, llvm, make,
cmake, rust, go), Docker, colima/lima/podman, ROS, `gh`/`glab` or pyenv.
Homebrew itself and the Command Line Tools are prerequisites: the
one-liner installs Homebrew (and through it the Command Line Tools) only
after you say yes, and never on an Intel Mac. Nothing is installed with
`sudo`, and no cask is installed.


## Reporting a result

When you verify a cell, record the date, the `python3 ~/.dotfiles/install.py
status` summary and the installed commit, and update the row above. Keep
hostnames, usernames and device names out of the report.

On a Mac, run `dotfiles status` and `dotfiles services` instead (the
system `python3` there is the Command Line Tools' 3.9), and add
`sw_vers -productVersion`, `sw_vers -buildVersion`, `uname -m` and
`brew --prefix`. Write your home directory as `~`. Never include serial
numbers, hardware UUIDs or the computer's name (`system_profiler` prints
all three).
