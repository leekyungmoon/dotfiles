
# Dotfiles

🏠 Personal dotfiles for Ubuntu (Linux) and macOS.

macOS support is new and **pending verification** on real Macs; see the
[support matrix](docs/SUPPORT.md).


## Installation

### 👉 One-liner (if you trust me):

```bash
curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash
```

<details>
<summary>
🤔 Want to manually clone and install? (Click to expand)
</summary>

<p>

```bash
$ git clone --recursive https://github.com/leekyungmoon/dotfiles.git ~/.dotfiles && ~/.dotfiles/install
```

</p>
</details>

<br>


The installation script will clone the repository into `~/.dotfiles` and create symbolic links (e.g., `~/.vimrc`) for you.
Existing files are backed up and replaced; `dotfiles restore --baseline` brings them back ([recovery](docs/RECOVERY.md)).

### 🍎 macOS

The same one-liner (`| bash`, or `| sh`) works on Apple silicon Macs with
macOS 15 Sequoia, 26 Tahoe or 27; Intel Macs on 15 or 26 are best effort.
It needs **Homebrew**: when it is missing, the one-liner offers (on Apple
silicon, in a terminal) to run Homebrew's official installer, which asks
for an administrator's password and also installs the Xcode **Command Line
Tools**; otherwise it stops before changing anything and prints the
commands to run. A terminal running under
Rosetta is refused. Homebrew's `python3` (3.10 or newer) runs the installer,
and packages come from Homebrew, never with `sudo`.

On a Mac, two LaunchAgents start tmux at login and save it every minute
([how it works](docs/tmux-auto-restore.ko.md#macos)); `dotfiles services`
shows, disables and enables them. If Homebrew's installer suggests adding
`eval "$(brew shellenv)"` to `~/.zprofile`, skip that: `~/.zprofile` is a
link into `~/.dotfiles`, and the zsh setup already does it. What else
differs from Ubuntu: [docs/SUPPORT.md](docs/SUPPORT.md#macos-differences).


## `$ dotfiles`

**To update dotfiles** (pull changes from upstream and run [`install.py`][install.py] again):

```bash
$ dotfiles update
$ dotfiles update --fast          # fast update mode: skip updating {vim,zsh} plugins
```

You can install some common softwares locally (into `$HOME/.local/bin`) *without sudo*:

```bash
$ dotfiles install neovim         # -> ~/.local/bin/nvim
$ dotfiles install fzf            # -> ~/.local/bin/fzf
```

**tmux at login and the one-minute autosave** (macOS LaunchAgents; on Linux this prints the `systemctl` commands instead):

```bash
$ dotfiles services                     # status of both, and the last save
$ dotfiles services disable tmux        # stop starting tmux at login (autosave keeps running)
$ dotfiles services enable              # both back on
```


## ✨ Highlights

- 📋 `pbcopy` / `pbpaste` on Linux — Wayland, X11, and OSC52 over SSH (macOS: the system ones, and OSC52 over SSH)
- 🪟 tmux: `Ctrl+A` prefix, `s` / `v` splits, smart `Ctrl+V` (macOS: passed to the program; `Cmd+V` pastes text), `tl` `tn` `ta` `tk` `td` `trn`
- 💾 tmux sessions auto-saved and restored at login, Codex / Claude included (systemd on Ubuntu, LaunchAgents on macOS)
- 🧭 GNOME navigation and `Ctrl+Super+←/→` tab switching on Ubuntu ([shortcuts](docs/SHORTCUTS.md))


## 🆘 Troubleshooting

*Please read carefully warning messages during installation !!*

- If something goes wrong, please run **`dotfiles update`** to make everything up-to-date.
    - Please carefully READ the error/warning message printed by the installation script.

- Q: I see some weird icons like `⍰` in (neo)vim statusline, or in tmux statusbar.
  - A: Install [Nerd fonts](https://github.com/ryanoasis/nerd-fonts), e.g., `JetBrainsMono Nerd Font Mono` (`dotfiles install nerd-font`; on macOS `brew install --cask font-jetbrains-mono-nerd-font`).

- On macOS, if tmux does not start at login or stops saving:
    - `dotfiles services` shows both LaunchAgents; their logs are in `~/.local/state/personal-dotfiles/launchd/`.
    - `launchctl bootstrap failed (5: Input/output error)`: turn on *Allow in the Background* for it in System Settings → General → Login Items & Extensions, then run `dotfiles repair` in Terminal (not over SSH).
    - Programs in tmux panes cannot reach hosts on your local network, or ask for folder access in tmux's name: see [Local Network and Files & Folders](docs/tmux-auto-restore.ko.md#로컬-네트워크와-파일-및-폴더-접근).
    - More in [Recovery](docs/RECOVERY.md#macos-the-launchagents).

- If [**neovim**][neovim] emits any startup errors:
    - Try `:checkhealth`.
    - Try `:Lazy update`, or `$ dotfiles update` (in zsh).


[neovim]: https://github.com/neovim/neovim
[install.py]: install.py


## License

[The MIT License (MIT)](LICENSE)

Copyright (c) 2012-2026 Jongwook Choi (@wookayin)


## Acknowledgements

Built on [wookayin/dotfiles](https://github.com/wookayin/dotfiles) — thanks, [@wookayin](https://github.com/wookayin) 🙏
