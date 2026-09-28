"""Isolation bootstrap: import an EXTRACTED input-remapper with HOME redirected.

input-remapper derives its config root from pwd (not $HOME), so the module
attribute is patched before any path-dependent module is imported. evdev
device/uinput access and subprocess calls are disabled so nothing touches
/dev/input, /dev/uinput or the display.
"""
import os
import subprocess
import sys


def boot(extract_root):
    home = os.environ["HOME"]
    assert home.startswith("/tmp/"), home
    sys.path.insert(0, extract_root)

    import evdev

    def _deny(*a, **k):
        raise RuntimeError("spike isolation: device access denied")

    evdev.list_devices = _deny
    evdev.InputDevice.__init__ = _deny
    evdev.UInput.__init__ = _deny

    def _no_subprocess(*a, **k):
        raise FileNotFoundError("spike isolation: subprocess denied")

    subprocess.check_output = _no_subprocess
    subprocess.run = _no_subprocess
    subprocess.Popen = _no_subprocess
    os.system = _no_subprocess

    import inputremapper
    import inputremapper.user as user

    assert inputremapper.__file__.startswith(extract_root), inputremapper.__file__
    user.HOME = home
    if hasattr(user, "CONFIG_PATH"):
        user.CONFIG_PATH = os.path.join(home, ".config/input-remapper")
    return inputremapper
