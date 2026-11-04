
# Dotfiles

🏠 Personal dotfiles for Ubuntu (Linux) systems.


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
Existing files are backed up and replaced; `python3 ~/.dotfiles/install.py restore --baseline` brings them back.

### Neovim with GitHub Copilot

The installer already includes Neovim and Node.js. Alongside the Claude Code
and Codex choices, it now asks:

```text
Set up GitHub Copilot in Neovim (copilot.vim)? [Y/n]
```

Press Enter or answer `y` to install the official
[`github/copilot.vim`](https://github.com/github/copilot.vim) plugin through
the existing lazy.nvim configuration. The choice is remembered for future
updates. Answering `n` leaves Neovim available without this plugin; it does
not delete any existing plugin files.

If Copilot is not signed in, the installer asks `Sign in to GitHub Copilot
now? [Y/n]` and opens Neovim with `:Copilot setup`. Follow the device-code
instructions and approve in your browser using the **GitHub account with
Copilot access**. Neovim closes after setup, and the installer checks the
sign-in status again. This login is separate from GitHub CLI, Codex and
Claude Code authentication. Copilot Free or a paid/organization plan is
required for suggestions.

Enable or disable the integration later:

```sh
python3 ~/.dotfiles/install.py --copilot
python3 ~/.dotfiles/install.py --no-copilot
```

In Neovim, **Alt+Y** accepts a Copilot suggestion; **Tab** and **Ctrl+J** keep
the existing completion and snippet behavior. Use `:Copilot status` to check
availability, or `:Copilot setup` to finish a deferred login.

A fresh installation without a terminal skips Copilot unless `--copilot`
is supplied. Login is never started without a terminal; an unsigned account
is reported as `AUTH_REQUIRED` with the manual setup command. `--dry-run`
does not save the choice or log in. `--skip-vimplug` skips plugin installation
and the Copilot sign-in step; rerun without it to finish setup.


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


## Personal zsh on a shared SSH account

The normal local installation also installs `ssh-dotfiles`. Prepare the local
zsh plugins once, then send that installation's zsh sources and cached plugins
to a server, even when the **server has no Internet access**:

```sh
ssh-dotfiles install user@server
ssh-dotfiles connect user@server
```

An existing SSH Host alias works too. Only `connect` selects your profile;
ordinary SSH and the account's shared startup files stay unchanged. The server
needs Python 3.8+, zsh 5.4.2+, Git, `infocmp`, and fzf 0.48+ already installed.
No sudo, desktop setup, shell change or service registration is performed.
See [installation, offline preparation and recovery](docs/SSH-DOTFILES.md).

## ✨ Highlights

- 📋 `pbcopy` / `pbpaste` on Linux — Wayland, X11, and OSC52 over SSH
- 🪟 tmux: `Ctrl+A` prefix, `s` / `v` splits, smart `Ctrl+V`, `tl` `tn` `ta` `tk` `td` `trn`
- 💾 tmux sessions auto-saved and restored at login, Codex / Claude included
- 🧭 GNOME navigation and `Ctrl+Super+←/→` tab switching ([shortcuts](docs/SHORTCUTS.md))


## 🆘 Troubleshooting

*Please read carefully warning messages during installation !!*

- If something goes wrong, please run **`dotfiles update`** to make everything up-to-date.
    - Please carefully READ the error/warning message printed by the installation script.

- Q: I see some weird icons like `⍰` in (neo)vim statusline, or in tmux statusbar.
  - A: Install [Nerd fonts](https://github.com/ryanoasis/nerd-fonts), e.g., `JetBrainsMono Nerd Font Mono` (`dotfiles install nerd-font`).

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
