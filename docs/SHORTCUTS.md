# ⌨️ Shortcuts

Sources of truth: [`tmux/tmux.conf`](../tmux/tmux.conf),
[`tmux/resurrect.conf`](../tmux/resurrect.conf),
[`zsh/zsh.d/zsh_custom_settings.zsh`](../zsh/zsh.d/zsh_custom_settings.zsh),
[`desktop/gnome-settings.json`](../desktop/gnome-settings.json) and
[`desktop/input-remapper/intent.json`](../desktop/input-remapper/intent.json).
If this page and those files disagree, the files win.


## tmux

The prefix is **`Ctrl+A`** (not `Ctrl+B`). `prefix x` below means: press
`Ctrl+A`, release, then press `x`. `prefix ?` shows a searchable
cheatsheet of every key.

### Sessions

| Key | Action |
| --- | ------ |
| `prefix S` | Pick / switch session with fzf (`bin/tmux-attach`) |
| `prefix d` | Detach (tmux default) |
| `prefix a` | Send `Ctrl+A` to a nested session |

Shell aliases:

| Alias | Command |
| ----- | ------- |
| `tl` | `tmux ls` |
| `tn NAME` | `tmux new -s NAME` |
| `ta NAME` | `tmux attach -t NAME` |
| `tk NAME` | `tmux kill-session -t NAME` |
| `td` | `tmux detach` |
| `trn NEW` | `tmux rename-session NEW` (`trn -t OLD NEW` for another session) |

### Windows

| Key | Action |
| --- | ------ |
| `prefix c` | New window |
| `prefix Ctrl+A` | Last window |
| `prefix 0` … `prefix 9` | Select window 0–9 |
| `prefix ,` / `prefix .` | Previous / next window |
| `prefix Left` / `prefix Right` | Previous / next window |
| `prefix Space` / `prefix Backspace` | Next / previous window |
| `prefix Shift+Left` / `prefix Shift+Right` | Move window left / right |
| `prefix A` or `prefix Ctrl+T` | Rename window |
| `prefix N` | Renumber windows sequentially |
| `prefix /` or `prefix %` | Move window to index |
| Mouse drag on status line | Reorder windows (tmux ≥ 3.4) |

### Panes

| Key | Action |
| --- | ------ |
| `prefix s` or `prefix _` | Split below (same directory) |
| `prefix v` or `prefix \|` | Split beside (same directory) |
| `prefix h/j/k/l` | Select pane left / down / up / right |
| `Ctrl+H/J/K` | Select pane left / down / up (passed through to vim/nvim) |
| `Ctrl+\` | Last pane (passed through to vim/nvim) |
| `prefix q` | Show pane numbers and jump |
| `prefix H/J/K/L` | Move pane to the far left / bottom / top / right |
| `prefix T` | Break pane into a new window |
| `prefix t` | Rename pane title |
| `prefix Ctrl+O` | Rotate panes |
| `prefix =` / `prefix Alt+=` | Main-vertical / main-horizontal layout |
| `prefix >` / `prefix <` | Resize right / left by 10, then stay in resize mode |
| `prefix +` / `prefix -` | Resize down / up by 5, then stay in resize mode |
| resize mode: `h/j/k/l`, arrows, `< > + - = _` | Keep resizing |
| `prefix e` | Toggle synchronized input to all panes |

### Copy and paste

| Key | Action |
| --- | ------ |
| `prefix Escape` or `prefix Enter` | Enter copy mode (vi keys; mouse wheel also works) |
| copy mode `v` / `y` | Begin selection / yank to clipboard via `pbcopy` |
| `prefix ]` | Paste tmux buffer |
| `prefix p` | Paste the OS clipboard (`pbpaste`) |
| `Ctrl+V` | Smart paste: text as usual, and lets Codex / Claude Code attach clipboard images |
| `prefix @` | Pick a file with fzf and insert `@path` (for AI TUIs) |

On tmux 3.2 (Ubuntu 22.04) the fzf pickers behind `prefix S`, `prefix @`
and `prefix ?` open in a plain `display-popup` instead of `fzf --tmux`.

### Misc

| Key | Action |
| --- | ------ |
| `prefix r` | Reload `~/.tmux.conf` |
| `prefix :` | Command prompt |
| `Shift+Enter` | Forwarded as a real Shift+Enter to the program |

Work environment save / restore (tmux-resurrect, bound in
`tmux/resurrect.conf`): `prefix Ctrl+S` saves now, through the same
locked wrapper as the every-minute timer, and `prefix Ctrl+R` restores
(tmux-resurrect's default). Snapshots are kept in
`~/.local/share/tmux/resurrect`; see
[tmux-auto-restore.ko.md](tmux-auto-restore.ko.md).


## zsh

| Key | Action |
| --- | ------ |
| `Ctrl+E` | Fuzzy-pick a directory and `cd` into it (fzf) |
| `Ctrl+S` | Show `git status` and return to the same prompt |
| `Ctrl+F` | Forward one character (accept autosuggestion) |
| `Alt+J` | Repair a pasted command that the terminal display-wrapped |

Paste repair also runs automatically, but only when pasting into an
empty prompt: a pasted Markdown `sh`/`bash` code fence is unwrapped, and an indented,
display-wrapped absolute path is joined back when the result exists.
Other multi-line pastes are left untouched, and a paste never runs by
itself.


## GNOME

Applied by the installer on both 22.04 and 24.04 unless noted.
`Super` is the Windows key.

### Terminal

| Key | Action |
| --- | ------ |
| `Ctrl+Alt+T` | Open a terminal |
| `Ctrl+T` | New tab (GNOME Terminal) |
| `Ctrl+Shift+T` | New window (GNOME Terminal) |
| `Ctrl+W` | Close tab (GNOME Terminal) |
| `Ctrl+Page_Up` / `Ctrl+Page_Down` | Previous / next tab |
| `Ctrl+Shift+V` | Paste (GNOME Terminal) |
| `Ctrl+Super+Left` / `Ctrl+Super+Right` | Previous / next tab (input-remapper, see below) |

### Windows

| Key | Action |
| --- | ------ |
| `Alt+Tab` / `Shift+Alt+Tab` | Switch windows |
| `Super+Tab` / `Shift+Super+Tab` | Switch applications |
| `Super+Above_Tab` or `Alt+Above_Tab` (the key above Tab) | Switch windows of the same app (add `Shift` to reverse) |
| `Alt+F6` / `Shift+Alt+F6` | Cycle windows of the same app directly |
| `Alt+F4` | Close window |
| `Super+H` | Minimize |
| `Alt+F10` | Toggle maximized |
| `Super+D`, `Ctrl+Super+D`, `Ctrl+Alt+D` | Show desktop |

Window tiling is turned off: `Super+Left` / `Super+Right` / `Super+Up` /
`Super+Down` (and `Alt+F5`) are unbound so they are free for your own use,
and dragging a window to a screen edge no longer tiles it. `Alt+F10`
still maximizes and restores.

### Workspaces

Dynamic workspaces, on the primary monitor only.

| Key | Action |
| --- | ------ |
| `Super+Page_Up`, `Super+Alt+Left`, `Ctrl+Alt+Left` | Workspace left |
| `Super+Page_Down`, `Super+Alt+Right`, `Ctrl+Alt+Right` | Workspace right |
| `Ctrl+Alt+Up` / `Ctrl+Alt+Down` | Workspace up / down |
| `Super+Home` / `Super+End` | First / last workspace |
| add `Shift` to any of the above | Move the window there instead |
| `Super+Alt+Up` / `Super+Alt+Down` | Shift the overview up / down |

### Monitors and applications

| Key | Action |
| --- | ------ |
| `Super+Shift+Left/Right/Up/Down` | Move window to the monitor in that direction |
| `Super+1` … `Super+9` | Switch to dock application 1–9 |
| `Super+Ctrl+1` … `Super+Ctrl+9` | Open a new window of dock app 1–9 (24.04 only) |
| Click a running app's dock icon | Cycle through its windows |


## input-remapper tab chord

While a **physical keyboard** holds `Ctrl` and either `Super` key:

| Press | The focused app receives | Effect in Chrome / GNOME Terminal |
| ----- | ------------------------ | --------------------------------- |
| `Left` | `Ctrl+Page_Up` | Previous tab |
| `Right` | `Ctrl+Page_Down` | Next tab |

The preset is named `personal-dotfiles-tabs`. It does not act over SSH or
on virtual keyboards; see the README troubleshooting section.
