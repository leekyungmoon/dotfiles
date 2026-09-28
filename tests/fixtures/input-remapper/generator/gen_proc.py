"""Generate a synthetic /proc/bus/input/devices fixture from evdev constants.

Only generic placeholder names/ids; bitmaps are printed the way the kernel's
input_print_bitmap does (64-bit words, most significant first, unpadded).
"""
import sys
from evdev import ecodes as e


def bitmap(bits):
    if not bits:
        return "0"
    words = [0] * (max(bits) // 64 + 1)
    for b in bits:
        words[b // 64] |= 1 << (b % 64)
    return " ".join(f"{w:x}" for w in reversed(words))


KEYBOARD_KEYS = set(range(1, 128)) | {e.KEY_F13}
CONSUMER_KEYS = {e.KEY_VOLUMEUP, e.KEY_VOLUMEDOWN, e.KEY_MUTE, e.KEY_PLAYPAUSE}
MACROPAD_KEYS = set(range(e.KEY_1, e.KEY_0 + 1)) | {e.KEY_A, e.KEY_LEFTCTRL, e.KEY_LEFTMETA}
MOUSE_KEYS = {e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE}


def dev(bus, vendor, product, name, phys, sysfs, handlers, ev, key=(), rel=(), abs_=()):
    lines = [
        f"I: Bus={bus:04x} Vendor={vendor:04x} Product={product:04x} Version=0111",
        f'N: Name="{name}"',
        f"P: Phys={phys}",
        f"S: Sysfs={sysfs}",
        "U: Uniq=",
        f"H: Handlers={handlers}",
        "B: PROP=0",
        f"B: EV={bitmap(ev)}",
    ]
    if key:
        lines.append(f"B: KEY={bitmap(key)}")
    if rel:
        lines.append(f"B: REL={bitmap(rel)}")
    if abs_:
        lines.append(f"B: ABS={bitmap(abs_)}")
    return "\n".join(lines) + "\n"


USB = "/devices/pci0000:00/0000:00:14.0/usb1"
KBD_EV = {e.EV_SYN, e.EV_KEY, e.EV_MSC, e.EV_LED, e.EV_REP}
blocks = [
    dev(0x19, 0, 1, "Power Button", "PNP0C0C/button/input0",
        "/devices/LNXSYSTM:00/LNXPWRBN:00/input/input0", "kbd event0",
        {e.EV_SYN, e.EV_KEY}, {e.KEY_POWER}),
    dev(0x11, 1, 1, "fixture-laptop-keyboard", "isa0060/serio0/input0",
        "/devices/platform/i8042/serio0/input/input1", "sysrq kbd leds event1",
        KBD_EV, KEYBOARD_KEYS),
    dev(0x3, 0x1111, 0x2222, "fixture-keyboard", "usb-0000:00:14.0-1/input0",
        f"{USB}/1-1/1-1:1.0/0003:1111:2222.0001/input/input2", "sysrq kbd leds event2",
        KBD_EV, KEYBOARD_KEYS),
    dev(0x3, 0x1111, 0x2222, "fixture-keyboard Consumer Control", "usb-0000:00:14.0-1/input1",
        f"{USB}/1-1/1-1:1.1/0003:1111:2222.0002/input/input3", "kbd event3",
        {e.EV_SYN, e.EV_KEY}, CONSUMER_KEYS),
    dev(0x3, 0x1111, 0x2222, "fixture-keyboard", "usb-0000:00:14.0-2/input0",
        f"{USB}/1-2/1-2:1.0/0003:1111:2222.0003/input/input4", "sysrq kbd leds event4",
        KBD_EV, KEYBOARD_KEYS),
    dev(0x3, 0x3333, 0x4444, "fixture-mouse", "usb-0000:00:14.0-3/input0",
        f"{USB}/1-3/1-3:1.0/0003:3333:4444.0004/input/input5", "mouse0 event5",
        {e.EV_SYN, e.EV_KEY, e.EV_REL, e.EV_MSC}, MOUSE_KEYS,
        {e.REL_X, e.REL_Y, e.REL_WHEEL}),
    dev(0x3, 0x5555, 0x6666, "fixture-macropad", "usb-0000:00:14.0-4/input0",
        f"{USB}/1-4/1-4:1.0/0003:5555:6666.0005/input/input6", "kbd event6",
        KBD_EV, MACROPAD_KEYS),
    dev(0x3, 0x1, 0x1, "input-remapper keyboard", "input-remapper",
        "/devices/virtual/input/input7", "sysrq kbd event7",
        {e.EV_SYN, e.EV_KEY}, KEYBOARD_KEYS),
    dev(0x3, 0x1111, 0x2222, "input-remapper fixture-keyboard forwarded", "",
        "/devices/virtual/input/input8", "sysrq kbd leds event8",
        KBD_EV, KEYBOARD_KEYS),
    dev(0x6, 0x2345, 0x6789, "fixture-virtual-keyboard", "",
        "/devices/virtual/input/input9", "sysrq kbd event9",
        {e.EV_SYN, e.EV_KEY}, KEYBOARD_KEYS),
]
sys.stdout.write("\n".join(blocks))
