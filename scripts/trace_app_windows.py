"""
Diagnostic, run by hand: trace an app's windows over time while the assistant opens and closes it
through the REAL pipeline (safety gate, launch, Verifier, close request).

    .\\venv\\Scripts\\python.exe scripts\\trace_app_windows.py notepad calculator

For each app: open_app -> watch -> close_app -> watch -> close anything still open that the run
created. A background thread polls every 20 ms and records every window that could belong to the
app - top-level windows whose title matches the app's pattern or whose process already owns a
matching window, plus the child windows of Store-app frames - and prints each change: appeared,
destroyed, shown/hidden, cloaked, reparented, retitled, enabled/disabled. Windows that existed
before the run are watched too (marked PRE-EXISTING), because a Store app may reuse a copy Windows
started in the background, but they are never closed.
"[verifier]" marks the moments a window is one the Verifier counts (top-level, visible, title matches).

Confirmation for close_app is answered "yes" automatically (the owner runs this deliberately).
Makes no Claude API calls, and never touches a window that existed before the run started.
"""
import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor import logic as executor  # noqa: E402
from app.executor.models import CLOSE_APP, OPEN_APP, ExecutorAction  # noqa: E402
from app.verifier import logic as verifier  # noqa: E402

POLL_SECONDS = 0.02
FRAME_CLASS = "ApplicationFrameWindow"
GA_PARENT, GW_OWNER, GW_CHILD, GW_HWNDNEXT = 1, 4, 5, 2
WM_CLOSE = 0x0010
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
for name, args, result in [
    ("EnumWindows", [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
    ("IsWindow", [wintypes.HWND], wintypes.BOOL),
    ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
    ("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
    ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
    ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ("GetDesktopWindow", [], wintypes.HWND),
    ("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
]:
    getattr(user32, name).argtypes, getattr(user32, name).restype = args, result
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class State:
    top: bool
    visible: bool
    cloaked: bool
    enabled: bool
    title: str
    cls: str
    pid: int
    parent: int
    owner: int


def enum_toplevel() -> list[int]:
    handles = []
    callback = WNDENUMPROC(lambda hwnd, _: handles.append(int(hwnd)) or True)
    user32.EnumWindows(callback, 0)
    return handles


def children(hwnd: int) -> list[int]:
    found, child = [], user32.GetWindow(hwnd, GW_CHILD)
    while child:
        found.append(int(child))
        child = user32.GetWindow(child, GW_HWNDNEXT)
    return found


def state(hwnd: int) -> State | None:
    if not user32.IsWindow(hwnd):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cloaked = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    parent = int(user32.GetAncestor(hwnd, GA_PARENT) or 0)
    return State(top=parent == int(user32.GetDesktopWindow()), visible=bool(user32.IsWindowVisible(hwnd)),
                 cloaked=bool(cloaked.value), enabled=bool(user32.IsWindowEnabled(hwnd)), title=title.value,
                 cls=cls.value, pid=pid.value, parent=parent, owner=int(user32.GetWindow(hwnd, GW_OWNER) or 0))


_exe_names: dict[int, str] = {}


def exe_name(pid: int) -> str:
    if pid not in _exe_names:
        name = "?"
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if process:
            buffer, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                name = Path(buffer.value).name
            kernel32.CloseHandle(process)
        _exe_names[pid] = name
    return _exe_names[pid]


def process_alive(pid: int) -> bool:
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    code = wintypes.DWORD()
    alive = bool(kernel32.GetExitCodeProcess(process, ctypes.byref(code))) and code.value == STILL_ACTIVE
    kernel32.CloseHandle(process)
    return alive


class Tracer(threading.Thread):
    def __init__(self, pattern, baseline: set[int]):
        super().__init__(daemon=True)
        self.pattern, self.baseline = pattern, baseline
        self.tracked: dict[int, State | None] = {}
        self.first_seen: dict[int, float] = {}
        self.pids: set[int] = set()
        self.lines: list[str] = []
        self.lock = threading.RLock()  # the main thread reads `tracked` while this thread updates it
        self.done = threading.Event()
        self.t0 = time.monotonic()
        for h in baseline:  # watch (never close) matching windows that were already there
            s = state(h)
            if s and s.title and pattern.search(s.title):
                self.track(h)

    def counted(self, s: State | None) -> bool:
        """What the Verifier counts: top-level, visible, titled, title matches."""
        return bool(s and s.top and s.visible and s.title and self.pattern.search(s.title))

    def describe(self, h: int, s: State) -> str:
        flags = [("top-level" if s.top else f"child of {s.parent:#x}"), "visible" if s.visible else "HIDDEN"]
        flags += ["CLOAKED"] if s.cloaked else []
        flags += [] if s.enabled else ["DISABLED"]
        flags += [f"owner {s.owner:#x}"] if s.owner else []
        flags += ["PRE-EXISTING"] if h in self.baseline else []
        return (f"{h:#010x} class={s.cls!r} title={s.title!r} exe={exe_name(s.pid)}(pid {s.pid}) "
                f"{' '.join(flags)}{'  [verifier]' if self.counted(s) else ''}")

    def log(self, text: str):
        with self.lock:
            self.lines.append(f"{time.monotonic() - self.t0:7.3f}s  {text}")
            print(self.lines[-1], flush=True)

    def mark(self, text: str):
        self.log(f"---- {text}")

    def poll(self):
        for h in enum_toplevel():
            if h in self.tracked:
                continue
            s = state(h)
            if s and ((s.title and self.pattern.search(s.title))
                      or (s.pid in self.pids and (s.visible or s.cls == "Windows.UI.Core.CoreWindow"))):
                self.track(h)
        for h, s in list(self.tracked.items()):
            if s and s.cls == FRAME_CLASS:
                for child in children(h):
                    if child not in self.tracked:
                        self.track(child)
        for h, old in list(self.tracked.items()):
            new = state(h)
            if new != old:
                self.tracked[h] = new
                if new is None:
                    self.log(f"GONE     {h:#010x}")
                else:
                    changed = "" if old is None else "  (was: " + ", ".join(
                        f"{field}={getattr(old, field)!r}" for field in State.__dataclass_fields__
                        if getattr(old, field) != getattr(new, field)) + ")"
                    self.log(f"CHANGED  {self.describe(h, new)}{changed}")
            if new and new.title and self.pattern.search(new.title):
                self.pids.add(new.pid)

    def track(self, h: int):
        s = state(h)
        if s is None:
            return
        self.tracked[h] = s
        self.first_seen[h] = time.monotonic() - self.t0
        if s.title and self.pattern.search(s.title):
            self.pids.add(s.pid)
        self.log(f"APPEARED {self.describe(h, s)}")

    def run(self):
        while not self.done.is_set():
            with self.lock:
                self.poll()
            time.sleep(POLL_SECONDS)
        with self.lock:
            self.poll()


def trace(app_name: str, watch_after_open: float, watch_after_close: float):
    print(f"\n==================== {app_name} ====================")
    expectation = verifier.expect_window(app_name)
    tracer = Tracer(expectation.pattern, set(enum_toplevel()))
    before = verifier.snapshot_windows(expectation)
    tracer.start()
    tracer.mark(f"open_app called (matching windows already open: {len(before)})")
    opened = executor.execute_with_recovery(ExecutorAction(OPEN_APP, app_name))
    tracer.mark(f"open_app returned ok={opened.ok}: {opened.message}")
    recorded = [sorted(f"{h:#010x}" for h in group) for group in executor._session_windows.get(app_name, [])]
    tracer.mark(f"open_app recorded window group(s): {recorded}")
    time.sleep(watch_after_open)
    tracer.mark("close_app called (confirmation auto-answered yes)")
    closed = executor.execute_with_recovery(ExecutorAction(CLOSE_APP, app_name), confirm=lambda *args: True)
    tracer.mark(f"close_app returned outcome={closed.outcome.value}: {closed.message}")
    time.sleep(watch_after_close)
    with tracer.lock:
        leftovers = [h for h, s in tracer.tracked.items() if tracer.counted(s) and h not in tracer.baseline]
    tracer.mark(f"windows created by this run that the Verifier would still count: {[f'{h:#010x}' for h in leftovers]}")
    for h in leftovers:
        tracer.mark(f"cleanup: WM_CLOSE -> {h:#010x}")
        user32.PostMessageW(h, WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with tracer.lock:
            if not any(tracer.counted(tracer.tracked.get(h)) for h in leftovers):
                break
        time.sleep(0.1)
    time.sleep(1)
    tracer.done.set()
    tracer.join()
    tracer.mark("trace finished")
    alive = {pid: exe_name(pid) for pid in tracer.pids if process_alive(pid)}
    print(f"processes that owned matching windows, still running: {alive or 'none'}")
    print(f"windows still existing: {[tracer.describe(h, s) for h, s in tracer.tracked.items() if s] or 'none'}")


def main() -> int:
    if sys.platform != "win32":
        print("This diagnostic only runs on Windows.")
        return 1
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("apps", nargs="+", help="configured app names, e.g. notepad calculator")
    parser.add_argument("--watch-after-open", type=float, default=3.0, help="seconds to watch before closing")
    parser.add_argument("--watch-after-close", type=float, default=4.0, help="seconds to watch after close_app")
    args = parser.parse_args()
    for app_name in args.apps:
        trace(app_name, args.watch_after_open, args.watch_after_close)
    return 0


if __name__ == "__main__":
    sys.exit(main())
