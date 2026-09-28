"""Terminal output in the style of the upstream dotfiles installer.

Colors, the boxed section headers (``log_boxed``) and the per-target lines
(``"{:60s} : {}"``) follow wookayin/dotfiles' install.py. Colors are off when
stdout is not a terminal or ``NO_COLOR`` is set, so logs and tests see plain
text.
"""

from __future__ import annotations

import os
import sys
import unicodedata

LOGO = '''
   @leekyungmoon's          ███████╗██╗██╗     ███████╗███████╗
   ██████╗  █████╗ ████████╗██╔════╝██║██║     ██╔════╝██╔════╝
   ██╔══██╗██╔══██╗╚══██╔══╝█████╗  ██║██║     █████╗  ███████╗
   ██║  ██║██║  ██║   ██║   ██╔══╝  ██║██║     ██╔══╝  ╚════██║
   ██████╔╝╚█████╔╝   ██║   ██║     ██║███████╗███████╗███████║
   ╚═════╝  ╚════╝    ╚═╝   ╚═╝     ╚═╝╚══════╝╚══════╝╚══════╝

   https://github.com/leekyungmoon/dotfiles
'''

_CODES = {
    "GRAY": "\033[0;37m",
    "WHITE": "\033[1;37m",
    "RED": "\033[0;31m",
    "GREEN": "\033[0;32m",
    "YELLOW": "\033[0;33m",
    "CYAN": "\033[0;36m",
    "BLUE": "\033[0;34m",
}
RESET = "\033[0m"

_state = {"enabled": None}


def colors_wanted(stream=None, env=None) -> bool:
    """Colors only on a terminal and only without ``NO_COLOR``."""

    env = os.environ if env is None else env
    if "NO_COLOR" in env:
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def configure(*, enabled: bool | None = None, stream=None, env=None) -> bool:
    """Decide once whether to color; ``enabled`` forces the answer."""

    _state["enabled"] = colors_wanted(stream, env) if enabled is None else bool(enabled)
    return _state["enabled"]


def enabled() -> bool:
    if _state["enabled"] is None:
        configure()
    return bool(_state["enabled"])


def _wrap_colors(name: str):
    code = _CODES[name]

    def color(msg) -> str:
        if not enabled():
            return str(msg)
        return code + str(msg) + RESET

    color.__name__ = name
    return color


GRAY = _wrap_colors("GRAY")
WHITE = _wrap_colors("WHITE")
RED = _wrap_colors("RED")
GREEN = _wrap_colors("GREEN")
YELLOW = _wrap_colors("YELLOW")
CYAN = _wrap_colors("CYAN")
BLUE = _wrap_colors("BLUE")


def log(msg: str = "", *, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    stream.write(str(msg) + "\n")
    stream.flush()


def boxed(msg: str, color_fn=WHITE, use_bold: bool = False, len_adjust: int = 0) -> str:
    pad_msg = " " + msg + "  "
    width = sum(not unicodedata.combining(ch) for ch in pad_msg) + len_adjust
    if use_bold:
        text = ("┏" + "━" * width + "┓\n"
                + "┃" + pad_msg + "┃\n"
                + "┗" + "━" * width + "┛")
    else:
        text = ("┌" + "─" * width + "┐\n"
                + "│" + pad_msg + "│\n"
                + "└" + "─" * width + "┘")
    return color_fn(text)


def log_boxed(msg: str, color_fn=WHITE, use_bold: bool = False, len_adjust: int = 0,
              *, stream=None) -> None:
    log(boxed(msg, color_fn, use_bold, len_adjust), stream=stream)


def section(title: str, *, stream=None) -> None:
    """A CYAN bold box, as used for every installer section."""

    log("", stream=stream)
    log_boxed(title, color_fn=CYAN, use_bold=True, stream=stream)


def target_line(target, message: str) -> str:
    return "{:60s} : {}".format(BLUE(str(target)), message)


def log_target(target, message: str, *, stream=None) -> None:
    log(target_line(target, message), stream=stream)


__all__ = [
    "BLUE", "CYAN", "GRAY", "GREEN", "LOGO", "RED", "WHITE", "YELLOW",
    "boxed", "colors_wanted", "configure", "enabled", "log", "log_boxed",
    "log_target", "section", "target_line",
]
