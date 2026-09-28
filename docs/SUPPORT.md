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
| 24.04 | amd64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 24.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | amd64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |
| 22.04 | arm64 | pending verification | pending verification | pending verification | pending verification | pending verification | — |


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
