# 🧪 Support matrix

Supported targets are **Ubuntu 22.04 (jammy)** and **Ubuntu 24.04
(noble)** on **amd64** and **arm64**, installed by a normal (non-root)
user. Anything else is refused before any change — by `etc/install`
(the one-liner) first, and again by `install.py` itself.

Status values:

- **pass** — verified on a real machine of that kind, with the date.
- **fail** — verified broken; see notes.
- **pending verification** — not yet run on a real machine. Nothing in
  this column is claimed to work.


## Results

| Ubuntu | Arch | One-liner install | `dotfiles update` | `restore --baseline` | GNOME settings | input-remapper tabs | Verified on |
| ------ | ---- | ----------------- | ----------------- | -------------------- | -------------- | ------------------- | ----------- |
| 24.04 | amd64 | pass (over an existing wookayin/dotfiles install) | pending verification | pending verification | pending verification | pending verification | 2026-09-28, 24.04.5 amd64, over SSH |
| 24.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | amd64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |


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


## Known release differences

These come from the package manifests (`manifests/packages.json`,
`desktop/gnome-settings.json`), not from the results above.

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


## What is never installed

On every cell: no Docker/containers, ROS, `gh`/`glab`, nvm/conda/pyenv,
apt Node.js, C/C++ toolchain, GNOME Shell/`ubuntu-desktop` or display
manager. The apt plan is simulated first and the install stops if any of
these would be pulled in.


## Reporting a result

When you verify a cell, record the date, the `python3 ~/.dotfiles/install.py
status` summary and the installed commit, and update the row above. Keep
hostnames, usernames and device names out of the report.
