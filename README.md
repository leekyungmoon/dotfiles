
# Dotfiles

🏠 Personal dotfiles for Ubuntu, derived from [wookayin/dotfiles][upstream].


## Installation

### 👉 One-liner (if you trust me):

```bash
curl -fsSL https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash
```

<details>
<summary>
💡 (Tip) No <code>curl</code>? (Click to expand)
</summary>
<p>

```bash
wget -qO- https://raw.githubusercontent.com/leekyungmoon/dotfiles/HEAD/etc/install | bash
```

</p>
</details>

<details>
<summary>
🤔 Want to manually clone and install? (Click to expand)
</summary>

<p>

```bash
$ git clone --recursive https://github.com/leekyungmoon/dotfiles.git ~/.dotfiles
$ cd ~/.dotfiles && python3 install.py
```

</p>
</details>

<br>


The installation script will clone the repository into `~/.dotfiles` and create symbolic links (e.g., `~/.vimrc`) for you,
installing what the configs need along the way.
If target files already exist (e.g. `~/.vim`, `~/.vimrc`), they are **backed up and replaced** —
`python3 ~/.dotfiles/install.py restore --baseline` puts them back ([details](docs/RECOVERY.md)).


## `$ dotfiles`

**To update dotfiles** (pull changes from upstream and run [`install.py`][install.py] again):

```bash
$ dotfiles update
$ dotfiles update --fast          # fast update mode: skip updating {vim,zsh} plugins
```

You can install some pinned tools locally (into `$HOME/.local`) *without sudo*:

```bash
$ dotfiles install                # list available packages
$ dotfiles install neovim         # -> ~/.local/bin/nvim
```


## ✨ Highlights

- 📋 `pbcopy` / `pbpaste` that just work on Linux — Wayland, X11, and OSC52 over SSH.
- 🪟 tmux with a `Ctrl+A` prefix, `s` / `v` splits, `S` session picker, smart `Ctrl+V` paste,
  and `tl` `tn` `ta` `tk` `td` `trn` aliases.
- 💾 tmux sessions saved every minute and restored at login, Codex / Claude conversations included
  ([docs](docs/tmux-auto-restore.ko.md)).
- 🧭 GNOME window / workspace navigation and `Ctrl+Super+←/→` tab switching
  ([all shortcuts](docs/SHORTCUTS.md)).


## 🆘 Troubleshooting

*Please read carefully warning messages during installation !!*

- If something goes wrong, please run **`dotfiles update`** (or `python3 ~/.dotfiles/install.py`) to make everything up-to-date.
    - Please carefully READ the error/warning message printed by the installation script.
    - Your own `~/.zshrc`, `~/.vimrc`, etc. were backed up, not deleted — see [docs/RECOVERY.md](docs/RECOVERY.md).

- Q: I see some weird icons like `⍰` in (neo)vim statusline, or in tmux statusbar.
  - A: Use a [Nerd font](https://github.com/ryanoasis/nerd-fonts) in your terminal, e.g., `JetBrainsMono Nerd Font Mono`
    (`dotfiles install nerd-font`).

- If [**neovim**][neovim] emits any startup errors:
    - Try `:checkhealth`.
    - Try `:Lazy update`, or `$ dotfiles update` (in zsh).
    - If neovim + treesitter emits an error like `query: invalid node type`, run `:TSUpdate`.

- GNOME shortcuts not applied yet? Installed without a desktop session, they are applied at your next login
  (or run `python3 ~/.dotfiles/install.py gui-apply` in the session).

- If you are still lost, or you've found a bug, please [raise an issue](https://github.com/leekyungmoon/dotfiles/issues).


[neovim]: https://github.com/neovim/neovim
[install.py]: install.py


## License

[The MIT License (MIT)](LICENSE)

Copyright (c) 2012-2026 Jongwook Choi (@wookayin)

Modifications in this repository are released under the same MIT license.


## Acknowledgements

This repository is derived from [wookayin/dotfiles][upstream] by [Jongwook Choi (@wookayin)][wookayin],
and keeps its full commit history and MIT license.
The vim/neovim, zsh and tmux setup, this README, the one-liner, `install.py` and `dotfiles` all follow his work;
the Ubuntu installer, desktop integration and clipboard helpers are built on top.

It is kept as an independent repository rather than a GitHub fork.

[upstream]: https://github.com/wookayin/dotfiles
[wookayin]: https://github.com/wookayin
