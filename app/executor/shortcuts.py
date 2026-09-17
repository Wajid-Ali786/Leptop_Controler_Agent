"""
Keyboard shortcuts the assistant may press in Phase 1 - a FIXED allow-list with a risk level for
each one. Deliberately code, not configuration: a shortcut's risk can't be configured away.

    parse("ctrl + c")  -> the Shortcut for Ctrl+C, or a ShortcutRefusal saying why not

Input is key names joined by "+": case and spaces don't matter, modifiers may come in any order,
and a few aliases are accepted (control, windows, escape, del). Every shortcut is shown and logged
under one normalized name, e.g. "Ctrl+C".

Levels (the safety gate asks for confirmation at MEDIUM and above):
  LOW    Ctrl+A  only changes the selection; anything that then changes the selected content is
                 itself confirmed
         Win+D   shows the desktop - changes no data, pressing it again brings the windows back,
                 and nothing else here does harm on the desktop
  MEDIUM Ctrl+C  replaces the clipboard, and in a terminal or console it stops the running program;
                 Phase 1 can't reliably recognise every terminal, so it is always confirmed
         Ctrl+S  saves, which can overwrite an existing file - and the document may hold changes
                 made by the user or another program, not only by the assistant
         Ctrl+Z  undoes the most recent change, which may be the user's own work; some apps can't redo
         Ctrl+X  removes the selection from the document and replaces the clipboard
         Alt+Tab changes no data, but which window becomes active can't be known in advance, and
                 every following action lands there
  HIGH   Ctrl+V  puts unknown clipboard content into the active window: pasted lines can run as
                 commands in a terminal, and pasted files are copied in File Explorer
Alt+F4 is refused here: closing a window goes only through close_app or window control close, which share
one close mechanism (only windows this assistant opened, a polite close request, verified). F5, Ctrl+R, Ctrl+F5, Shift+F5 and Ctrl+Shift+R are refused here: refreshing goes only through the Refresh
action, which first identifies the app (F5 debugs in code editors, Ctrl+R replies in Outlook). Ctrl+Alt+Delete
and Win+L are reserved and never sent.
"""
from dataclasses import dataclass

from app.safety.models import RiskLevel

MODIFIERS = ("Ctrl", "Alt", "Shift", "Win")  # also the order names are written in

# What the Verifier can check afterwards (see app/executor/logic.py)
CHECK_NONE = "none"                   # nothing observable: unverified
CHECK_CLIPBOARD = "clipboard"         # the clipboard's change counter moved
CHECK_CUT = "cut"                     # clipboard changed AND the field's text got shorter
CHECK_SELECT_ALL = "select_all"       # a standard edit field reports everything selected
CHECK_ACTIVE_CHANGED = "active_changed"  # a different window is active
CHECK_DESKTOP = "desktop"             # the desktop is the active window


@dataclass(frozen=True)
class Shortcut:
    name: str                  # normalized, e.g. "Ctrl+C" - safe to log
    modifiers: tuple[str, ...]
    key: str
    risk: RiskLevel
    reason: str                # the safety rule that is logged - never a window title
    effect: str                # what the confirmation prompt says it does
    needs_active_window: bool  # False for shortcuts that act on Windows itself
    check: str


@dataclass(frozen=True)
class ShortcutRefusal:
    message: str


_ALIASES = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win",
            "escape": "Esc", "esc": "Esc", "del": "Delete", "delete": "Delete", "enter": "Enter", "tab": "Tab",
            "space": "Space", "backspace": "Backspace", "home": "Home", "end": "End", "insert": "Insert",
            "pageup": "PageUp", "pagedown": "PageDown", "up": "Up", "down": "Down", "left": "Left", "right": "Right"}
KNOWN_KEYS = ({chr(c) for c in range(ord("A"), ord("Z") + 1)} | {str(d) for d in range(10)}
              | {f"F{n}" for n in range(1, 25)} | {v for v in _ALIASES.values() if v not in MODIFIERS})


def _shortcut(name, risk, reason, effect, needs_active_window, check):
    *modifiers, key = name.split("+")
    return Shortcut(name, tuple(modifiers), key, risk, f"shortcut {name}: {reason}", effect, needs_active_window, check)


SUPPORTED: dict[str, Shortcut] = {s.name: s for s in [
    _shortcut("Ctrl+A", RiskLevel.LOW, "only changes the selection", "selects everything", True, CHECK_SELECT_ALL),
    _shortcut("Win+D", RiskLevel.LOW, "shows the desktop; changes no data and can be undone",
              "shows the desktop", False, CHECK_DESKTOP),
    _shortcut("Ctrl+C", RiskLevel.MEDIUM,
              "replaces the clipboard, and can stop a running program in a terminal (always confirmed in Phase 1)",
              "copies the selection to the clipboard, replacing what's on it; in a terminal or console it stops "
              "the running program", True, CHECK_CLIPBOARD),
    _shortcut("Ctrl+S", RiskLevel.MEDIUM, "saving can overwrite an existing file",
              "saves the document - this can overwrite an existing file", True, CHECK_NONE),
    _shortcut("Ctrl+Z", RiskLevel.MEDIUM, "undoes the last change, which some apps can't redo",
              "undoes the last change; some apps can't redo it", True, CHECK_NONE),
    _shortcut("Ctrl+X", RiskLevel.MEDIUM, "removes the selection and replaces the clipboard",
              "cuts the selection: removes it from the document and replaces what's on the clipboard", True, CHECK_CUT),
    _shortcut("Alt+Tab", RiskLevel.MEDIUM, "switches to a window that can't be known in advance",
              "switches to another window (which one can't be known in advance); following actions go to that "
              "window", False, CHECK_ACTIVE_CHANGED),
    _shortcut("Ctrl+V", RiskLevel.HIGH, "pastes clipboard content the assistant can't see",
              "pastes the clipboard ({clipboard}). I can't see what's on it; pasting into a terminal or chat can "
              "run or send it", True, CHECK_NONE),
]}

_RESERVED = {
    "Ctrl+Alt+Delete": "Ctrl+Alt+Delete is a reserved Windows shortcut. This assistant will not send it.",
    "Win+L": ("Win+L is a reserved Windows shortcut. This assistant will not send it: it's intentionally "
              "unsupported in Phase 1 because it locks the Windows session."),
}
_REFRESH_MESSAGE = ("{name} isn't available as a shortcut. Use the Refresh action instead: it checks which app is "
                    "active first, because these keys do different things in different apps.")
_DEFERRED = {name: _REFRESH_MESSAGE.format(name=name) for name in ("F5", "Ctrl+R", "Ctrl+F5", "Shift+F5", "Ctrl+Shift+R")}
_DEFERRED["Alt+F4"] = ("Alt+F4 isn't available as a shortcut. To close a window, use close_app or window control "
                       "close: they only close windows this assistant opened, and confirm they closed.")


def parse(text: str) -> Shortcut | ShortcutRefusal:
    """The supported Shortcut named by `text`, or a ShortcutRefusal with a clear message."""
    if not isinstance(text, str) or not text.strip():
        return ShortcutRefusal("Which shortcut should I press? For example: Ctrl+C.")
    parts = [part.strip() for part in text.split("+")]
    if any(not part for part in parts):
        return ShortcutRefusal(f"I can't read the shortcut '{text.strip()}': join key names with +, e.g. Ctrl+C.")
    modifiers, keys = [], []
    for part in parts:
        name = _ALIASES.get(part.lower()) or (part.upper() if part.upper() in KNOWN_KEYS else None)
        if name is None:
            return ShortcutRefusal(f"I don't know a key called '{part}'.")
        (modifiers if name in MODIFIERS else keys).append(name)
    if len(set(modifiers)) != len(modifiers):
        return ShortcutRefusal(f"'{text.strip()}' names the same modifier twice.")
    if len(keys) != 1:
        return ShortcutRefusal(f"'{text.strip()}' needs exactly one key besides Ctrl, Alt, Shift or Win.")
    name = "+".join([m for m in MODIFIERS if m in modifiers] + keys)
    if name in _RESERVED:
        return ShortcutRefusal(_RESERVED[name])
    if name in _DEFERRED:
        return ShortcutRefusal(_DEFERRED[name])
    if name not in SUPPORTED:
        return ShortcutRefusal(f"{name} isn't a supported shortcut yet. Supported: {', '.join(SUPPORTED)}.")
    return SUPPORTED[name]
