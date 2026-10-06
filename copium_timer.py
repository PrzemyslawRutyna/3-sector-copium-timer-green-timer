"""
Copium Timer
============

Launches two windows:

  * "Green Timer"  - floating always-on-top green digits (HH:MM:SS, invisible
                     background) of a stopwatch that starts when launched; the
                     elapsed time and window position are stored in
                     green_timer.json and continue across sessions.
                     Drag to move, scroll to resize, right-click for the menu.
  * "Copium timer" - a terminal window with the COPIUM TIMER ascii banner,
                     the theoretical best lap (sum of best sectors) in big
                     digits, the sector table and a command prompt to submit
                     new sector times. Each sector shows when it was achieved
                     and what the Green Timer read at that moment.

Sector data lives in sectors.json next to this script; a sectors.json with
a "track" name defines a custom track (see README.md). After editing
parameters here, type 'restart' in the terminal (or right-click the Green
Timer -> Restart windows) to reopen both windows with the changes.

Usage:
    python copium_timer.py          # launch both windows
    python copium_timer.py --cli    # terminal only (in the current console)
    python copium_timer.py --gui    # Green Timer only

Pure standard library (tkinter), Python 3.8+.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve()
APP_DIR = SCRIPT.parent
STATE_PATH = APP_DIR / "sectors.json"
TIMER_PATH = APP_DIR / "green_timer.json"
CONTROL_PATH = APP_DIR / "green_control.json"  # terminal -> Green Timer stop/start requests
LOG_PATH = APP_DIR / "copium_timer.log"

GUI_TITLE = "Green Timer"
CLI_TITLE = "Copium timer"

# Defaults taken from sectors.png. The floor is the "physically impossible"
# boundary: a sector time below it is rejected. Sector 2's floor is a flat
# 1:00.000; the other floors use the same ratio (~84.4 % of the default),
# rounded to a whole second.
#            name        default   floor     (milliseconds)
DEFAULT_SECTORS = [
    ("Sector 1", 91_643, 77_000),   # 01:31.643  -> floor 01:17.000
    ("Sector 2", 71_069, 60_000),   # 01:11.069  -> floor 01:00.000
    ("Sector 3", 79_941, 67_000),   # 01:19.941  -> floor 01:07.000
]

HISTORY_LIMIT = 200


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def fmt_ms(ms):
    """91643 -> '01:31.643'"""
    ms = int(round(ms))
    sign = "-" if ms < 0 else ""
    m, rem = divmod(abs(ms), 60_000)
    s, frac = divmod(rem, 1000)
    return f"{sign}{m:02d}:{s:02d}.{frac:03d}"


def fmt_gain(ms):
    return f"-{ms / 1000:.3f}s" if ms > 0 else " 0.000s"




# --------------------------------------------------------------------------
# Shared state (sectors.json)
# --------------------------------------------------------------------------

def default_state():
    return {
        "sectors": [
            {"name": n, "default_ms": d, "floor_ms": f, "best_ms": d,
             "achieved_at": None, "achieved_timer_ms": None}
            for n, d, f in DEFAULT_SECTORS
        ],
        "history": [],
    }


TIME_PATTERN = r"(\d{1,2}):([0-5]\d)\.(\d{3})"  # M:SS.mmm


def parse_time_str(text):
    """'1:31.643' -> 91643"""
    m = re.fullmatch(TIME_PATTERN, str(text).strip())
    if not m:
        raise ValueError(f"'{text}' is not a M:SS.mmm time (e.g. 1:31.643)")
    minutes, seconds, frac = (int(g) for g in m.groups())
    return minutes * 60_000 + seconds * 1000 + frac


def validate_state(state):
    """Check and normalize sectors.json.

    A hand-written file may give times as strings ("default", "limit", and
    optionally "best", e.g. "1:31.643"); they're converted to the *_ms fields.
    """
    try:
        sectors = state["sectors"]
        if not isinstance(sectors, list) or not sectors:
            raise ValueError("'sectors' must be a non-empty list")
        for i, s in enumerate(sectors):
            s.setdefault("name", f"Sector {i + 1}")
            s["name"] = str(s["name"])
            for text_key, ms_key in (("default", "default_ms"), ("limit", "floor_ms"), ("best", "best_ms")):
                if text_key in s:
                    s[ms_key] = parse_time_str(s.pop(text_key))
            for key in ("default_ms", "floor_ms"):
                if key not in s:
                    raise ValueError(f"{s['name']}: missing '{key.replace('_ms', '').replace('floor', 'limit')}'")
            s.setdefault("best_ms", s["default_ms"])
            for key in ("default_ms", "floor_ms", "best_ms"):
                s[key] = int(s[key])
            if s["floor_ms"] > s["default_ms"]:
                raise ValueError(f"{s['name']}: limit {fmt_ms(s['floor_ms'])} is slower than "
                                 f"default {fmt_ms(s['default_ms'])}")
            s.setdefault("achieved_at", None)        # when the best was typed in
            s.setdefault("achieved_timer_ms", None)  # Green Timer value at that moment
        state.setdefault("history", [])
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"unexpected structure ({e})") from e
    return state


def read_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return validate_state(json.load(f))


def write_json(path, data):
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # The other window may be reading the file at this exact moment, which
    # blocks the replace on Windows - just retry briefly.
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.025)
    os.replace(tmp, path)


def write_state(state):
    state["history"] = state["history"][-HISTORY_LIMIT:]
    write_json(STATE_PATH, state)


def sync_config(state):
    """Apply DEFAULT_SECTORS (names, defaults, limits) to the stored state.

    For the built-in track the code is the source of truth, so editing
    DEFAULT_SECTORS and restarting takes effect. Achieved bests are kept;
    sectors still at their default follow the new default.
    A file with a "track" name is a custom track: it's left as it is.
    Returns True if anything changed.
    """
    if state.get("track"):
        return False
    before = json.dumps(state, sort_keys=True)
    sectors = state["sectors"]
    for i, (name, default, floor) in enumerate(DEFAULT_SECTORS):
        if i >= len(sectors):
            sectors.append({"name": name, "default_ms": default, "floor_ms": floor, "best_ms": default,
                            "achieved_at": None, "achieved_timer_ms": None})
            continue
        s = sectors[i]
        s["name"], s["floor_ms"] = name, floor
        if not s["achieved_at"]:
            s["best_ms"] = default
        s["default_ms"] = default
    del sectors[len(DEFAULT_SECTORS):]
    state["history"] = [h for h in state["history"] if h.get("sector", 0) < len(sectors)]
    return json.dumps(state, sort_keys=True) != before


load_problem = None  # why the last sectors.json couldn't be used (shown in the terminal)


def load_or_init_state():
    global load_problem
    try:
        state = read_state()
        if sync_config(state):
            write_state(state)
        return state
    except FileNotFoundError:
        pass
    except ValueError as e:
        # Invalid file: keep it as sectors.json.invalid and start from the defaults.
        backup = STATE_PATH.with_suffix(".json.invalid")
        try:
            os.replace(STATE_PATH, backup)
        except OSError:
            pass
        load_problem = f"sectors.json was invalid: {e}. Moved it to {backup.name}; using the defaults."
    state = default_state()
    write_state(state)
    return state


def total_ms(state):
    return sum(s["best_ms"] for s in state["sectors"])


# --------------------------------------------------------------------------
# Green Timer stopwatch (green_timer.json)
# --------------------------------------------------------------------------
# The Green Timer saves {"elapsed_ms", "running", "saved_at"} every second,
# so the elapsed time survives restarts and the terminal can read the
# current value when a sector is achieved.

def fmt_hms(ms):
    """2299000 -> '00:38:19'"""
    total_s = int(ms) // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def read_timer():
    try:
        with open(TIMER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        geometry = data.get("geometry")
        return {"elapsed_ms": int(data.get("elapsed_ms", 0)),
                "running": bool(data.get("running", False)),
                "saved_at": float(data.get("saved_at", 0)),
                "geometry": geometry if isinstance(geometry, str) else None}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"elapsed_ms": 0, "running": False, "saved_at": 0.0, "geometry": None}


def current_timer_ms():
    """Green Timer value right now, as seen from another process."""
    t = read_timer()
    ms = t["elapsed_ms"]
    since = time.time() - t["saved_at"]
    # Only extrapolate if the Green Timer is alive (it saves every second).
    if t["running"] and 0 <= since < 5:
        ms += int(since * 1000)
    return ms


# --------------------------------------------------------------------------
# Terminal: "Copium timer"
# --------------------------------------------------------------------------

RESET = "\x1b[0m"
WHITE = "\x1b[1;97m"
GREEN = "\x1b[1;92m"
RED = "\x1b[1;91m"
YELLOW = "\x1b[93m"
DIM = "\x1b[90m"
PURPLE = "\x1b[1;38;2;178;75;255m"      # F1 "purple sector"
PURPLE_DIM = "\x1b[38;2;135;95;185m"

CONSOLE_FONT = "Consolas"
CONSOLE_FONT_PX = 24   # max font height; shrinks automatically if the window wouldn't fit
CONSOLE_COLS = 114
CONSOLE_LINES = 41

# "ANSI Shadow" style letters.
LETTERS = {
    "C": [" ██████╗",
          "██╔════╝",
          "██║     ",
          "██║     ",
          "╚██████╗",
          " ╚═════╝"],
    "O": [" ██████╗ ",
          "██╔═══██╗",
          "██║   ██║",
          "██║   ██║",
          "╚██████╔╝",
          " ╚═════╝ "],
    "P": ["██████╗ ",
          "██╔══██╗",
          "██████╔╝",
          "██╔═══╝ ",
          "██║     ",
          "╚═╝     "],
    "I": ["██╗",
          "██║",
          "██║",
          "██║",
          "██║",
          "╚═╝"],
    "U": ["██╗   ██╗",
          "██║   ██║",
          "██║   ██║",
          "██║   ██║",
          "╚██████╔╝",
          " ╚═════╝ "],
    "M": ["███╗   ███╗",
          "████╗ ████║",
          "██╔████╔██║",
          "██║╚██╔╝██║",
          "██║ ╚═╝ ██║",
          "╚═╝     ╚═╝"],
    "T": ["████████╗",
          "╚══██╔══╝",
          "   ██║   ",
          "   ██║   ",
          "   ██║   ",
          "   ╚═╝   "],
    "E": ["███████╗",
          "██╔════╝",
          "█████╗  ",
          "██╔══╝  ",
          "███████╗",
          "╚══════╝"],
    "R": ["██████╗ ",
          "██╔══██╗",
          "██████╔╝",
          "██╔══██╗",
          "██║  ██║",
          "╚═╝  ╚═╝"],
}

# 3x5 digits, each '#' drawn as two full blocks.
DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": ["##.", ".#.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", "###", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", "..#", "..#", "..#"],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    ":": [".", "#", ".", "#", "."],
    ".": [".", ".", ".", ".", "#"],
    "-": ["...", "...", "###", "...", "..."],
}


def banner_word(word):
    rows = [""] * 6
    for i, ch in enumerate(word):
        glyph = LETTERS[ch]
        width = max(len(r) for r in glyph)
        for r in range(6):
            rows[r] += ("" if i == 0 else " ") + glyph[r].ljust(width)
    return rows


def banner_lines(columns):
    copium, timer = banner_word("COPIUM"), banner_word("TIMER")
    side_by_side = [a + "    " + b for a, b in zip(copium, timer)]
    if len(side_by_side[0]) + 2 <= columns:
        return side_by_side
    return copium + [""] + timer


def big_digits(text, px_w=2, px_h=1):
    """Render with DIGITS; each glyph pixel becomes px_w columns x px_h rows."""
    rows = [""] * (5 * px_h)
    for i, ch in enumerate(text):
        glyph = DIGITS[ch]
        for r in range(5 * px_h):
            cell = glyph[r // px_h].replace("#", "█" * px_w).replace(".", " " * px_w)
            rows[r] += ("" if i == 0 else "  ") + cell
    return rows


def center(text, columns):
    return " " * max(0, (columns - len(text)) // 2) + text


def enable_console():
    if os.name != "nt":
        sys.stdout.write(f"\x1b]0;{CLI_TITLE}\x07")
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetConsoleTitleW(CLI_TITLE)
    handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if k32.GetConsoleMode(handle, ctypes.byref(mode)):
        k32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    # Turn off QuickEdit: a stray click would otherwise put the console in
    # "Select" mode, which swallows typing until Esc is pressed.
    stdin = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    if k32.GetConsoleMode(stdin, ctypes.byref(mode)):
        k32.SetConsoleMode(stdin, (mode.value & ~0x0040) | 0x0080)  # -QUICK_EDIT, +EXTENDED_FLAGS
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass


def size_console():
    """Bigger console font, shrunk if needed so the window fits the screen."""
    import ctypes
    from ctypes import wintypes

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("nFont", ctypes.c_ulong), ("dwFontSize", COORD),
                    ("FontFamily", ctypes.c_uint), ("FontWeight", ctypes.c_uint),
                    ("FaceName", ctypes.c_wchar * 32)]

    k32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # measure the screen in real pixels
    except Exception:
        pass
    work = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
    work_w, work_h = work.right - work.left, work.bottom - work.top
    hwnd = k32.GetConsoleWindow()

    info = CONSOLE_FONT_INFOEX()
    info.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
    info.FontFamily = 54  # FF_MODERN | TMPF_TRUETYPE | TMPF_VECTOR
    info.FontWeight = 400
    info.FaceName = CONSOLE_FONT
    rect = wintypes.RECT()
    # Try the biggest font first and shrink until the whole window fits the screen.
    for px in range(CONSOLE_FONT_PX, 9, -1):
        info.dwFontSize = COORD(0, px)
        k32.SetCurrentConsoleFontEx(k32.GetStdHandle(-11), False, ctypes.byref(info))
        os.system(f"mode con: cols={CONSOLE_COLS} lines={CONSOLE_LINES}")
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left <= work_w and rect.bottom - rect.top <= work_h:
            break
    # Top of the screen, centered horizontally (SWP_NOSIZE | SWP_NOZORDER).
    x = work.left + max(0, (work_w - (rect.right - rect.left)) // 2)
    user32.SetWindowPos(hwnd, None, x, work.top, 0, 0, 0x0001 | 0x0004)


def timer_status_text():
    t = read_timer()
    alive = 0 <= time.time() - t["saved_at"] < 5
    if not alive:
        return DIM + f"Green Timer closed at {fmt_hms(t['elapsed_ms'])}" + RESET
    if t["running"]:
        return DIM + "Green Timer " + RESET + GREEN + "running" + RESET
    return DIM + "Green Timer " + RESET + RED + f"STOPPED at {fmt_hms(t['elapsed_ms'])}" + RESET


TABLE_WIDTH = 84


def fmt_achieved(s):
    if not s.get("achieved_at"):
        return DIM + "default" + RESET
    try:
        when = datetime.fromisoformat(s["achieved_at"]).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        when = str(s["achieved_at"])
    timer = s.get("achieved_timer_ms")
    timer_txt = f"  {DIM}@ Green{RESET} {GREEN}{fmt_hms(timer)}{RESET}" if timer is not None else ""
    return WHITE + when + RESET + timer_txt


def render(state, messages, highlight=None):
    cols = shutil.get_terminal_size((100, 30)).columns
    pad = " " * max(0, (cols - TABLE_WIDTH) // 2)
    out = ["\x1b[2J\x1b[3J\x1b[H"]

    for line in banner_lines(cols):
        out.append(WHITE + center(line, cols) + RESET + "\n")
    out.append("\n")

    # The theoretical best: double-size digits in "purple sector" color.
    total = total_ms(state)
    text = fmt_ms(total)
    big = big_digits(text, 3, 2)
    if len(big[0]) > cols:
        big = big_digits(text)  # narrow window: fall back to normal size
    label = "T H E O R E T I C A L   B E S T"
    if state.get("track"):
        label += f"   ·   {state['track']}"
    out.append(PURPLE_DIM + center(label, cols) + RESET + "\n")
    for line in big:
        out.append(PURPLE + center(line, cols) + RESET + "\n")
    out.append("\n")

    header = f"{'#':>3}  {'SECTOR':<10}  {'BEST':>9}   {'DEFAULT':>9}   {'GAINED':>8}   ACHIEVED"
    out.append(pad + DIM + header + RESET + "\n")
    out.append(pad + DIM + "─" * TABLE_WIDTH + RESET + "\n")
    for i, s in enumerate(state["sectors"]):
        gain = s["default_ms"] - s["best_ms"]
        row_color = GREEN if i == highlight else WHITE
        gain_color = GREEN if gain > 0 else DIM
        out.append(
            pad
            + row_color + f"{i + 1:>3}  {s['name'][:10]:<10}  {fmt_ms(s['best_ms']):>9}   " + RESET
            + DIM + f"{fmt_ms(s['default_ms']):>9}   " + RESET
            + gain_color + f"{fmt_gain(gain):>8}" + RESET
            + "   " + fmt_achieved(s) + "\n"
        )
    out.append(pad + DIM + "─" * TABLE_WIDTH + RESET + "\n")
    t_default = sum(s["default_ms"] for s in state["sectors"])
    t_gain = t_default - total
    out.append(
        pad + WHITE + f"{'':>3}  {'TOTAL':<10}  " + RESET + PURPLE + f"{fmt_ms(total):>9}   " + RESET
        + DIM + f"{fmt_ms(t_default):>9}   " + RESET
        + (GREEN if t_gain > 0 else DIM) + f"{fmt_gain(t_gain):>8}" + RESET
        + "   " + timer_status_text() + "\n\n"
    )

    for color, text in messages:
        out.append(pad + color + text + RESET + "\n")
    if messages:
        out.append("\n")
    out.append(pad + DIM + "s1 1:30.999  ·  undo  ·  reset  ·  stop  ·  start  ·  restart  ·  help  ·  quit"
               + RESET + "\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    return pad


HELP = [
    (WHITE, "Commands"),
    (WHITE, "  s<N> M:SS.mmm     submit a new best, e.g.  s1 1:30.999  /  s2 1:11.000  /  s3 1:19.600"),
    (WHITE, "  undo              revert the last accepted sector time"),
    (WHITE, "  reset             restore all sectors to the defaults"),
    (WHITE, "  stop / start      stop or start the Green Timer"),
    (WHITE, "  restart           close and reopen both windows (picks up code/parameter changes)"),
    (WHITE, "  help              show this help"),
    (WHITE, "  quit              close the Copium timer and the Green Timer"),
    (DIM,   "Rules: a new time must beat the current best and can't be impossibly fast."),
]

# The only accepted way to enter a sector time: s1 1:30.999
SET_RE = re.compile(r"^s(\d+) " + TIME_PATTERN + "$", re.IGNORECASE)
FORMAT_HINT = "Use exactly:  s1 1:30.999  /  s2 1:11.000  /  s3 1:19.600"


def apply_sector(state, number, new):
    """Returns (messages, highlight_index, improved: bool)."""
    sectors = state["sectors"]
    idx = number - 1
    if not 0 <= idx < len(sectors):
        return [(RED, f"No sector {number}. Valid sectors: s1-s{len(sectors)}.")], None, False

    s = sectors[idx]
    cur, floor = s["best_ms"], s["floor_ms"]
    if new > cur:
        return [(RED, f"Rejected: {fmt_ms(new)} is slower than your {s['name']} best of "
                      f"{fmt_ms(cur)} (+{(new - cur) / 1000:.3f}s).")], None, False
    if new == cur:
        return [(YELLOW, f"{fmt_ms(new)} is exactly your current {s['name']} best - nothing to update.")], None, False
    if new < floor:
        return [(RED, f"Impossible: {fmt_ms(new)} is below the {s['name']} limit of {fmt_ms(floor)}."),
                (DIM, "Nobody is that fast. Not even on copium.")], None, False

    old_total = total_ms(state)
    now = datetime.now().isoformat(timespec="seconds")
    timer = current_timer_ms()
    state["history"].append({
        "sector": idx, "old_ms": cur, "new_ms": new, "at": now,
        "old_achieved_at": s["achieved_at"], "old_achieved_timer_ms": s["achieved_timer_ms"],
    })
    s["best_ms"], s["achieved_at"], s["achieved_timer_ms"] = new, now, timer
    write_state(state)
    return [
        (GREEN, f"{s['name']}: {fmt_ms(cur)} -> {fmt_ms(new)}  (-{(cur - new) / 1000:.3f}s)"
                f"  at Green Timer {fmt_hms(timer)}"),
        (GREEN, f"Copium total: {fmt_ms(old_total)} -> {fmt_ms(total_ms(state))}"),
    ], idx, True


def control_timer(action):
    """Ask the Green Timer to 'stop' or 'start'; returns a message line."""
    want_running = action == "start"
    t = read_timer()
    if not 0 <= time.time() - t["saved_at"] < 5:
        return (YELLOW, "The Green Timer isn't open. Type 'restart' to open it.")
    if t["running"] == want_running:
        return (YELLOW, f"The Green Timer is already {'running' if want_running else 'stopped'}.")
    write_json(CONTROL_PATH, {"action": action, "at": time.time()})
    deadline = time.time() + 1.5
    while time.time() < deadline:  # wait for the Green Timer to confirm
        time.sleep(0.05)
        t = read_timer()
        if t["running"] == want_running:
            verb = "started" if want_running else "stopped"
            return (GREEN if want_running else YELLOW, f"Green Timer {verb} at {fmt_hms(t['elapsed_ms'])}.")
    return (RED, "The Green Timer didn't respond. Try 'restart'.")


def run_cli(resize=False):
    if resize and os.name == "nt":
        size_console()
    enable_console()
    global load_problem
    state = load_or_init_state()
    messages = [(DIM, "Type 'help' for commands.")]
    highlight = None

    while True:
        if load_problem:
            messages = [(RED, load_problem)] + messages
            load_problem = None
        pad = render(state, messages, highlight)
        highlight = None
        try:
            line = input("\n" + pad + "copium> ").strip()
        except (EOFError, KeyboardInterrupt, OSError):  # OSError: console window was closed
            return

        # Re-read in case sectors.json was edited by hand meanwhile.
        state = load_or_init_state()
        cmd = line.lower()

        if not line:
            messages = []
        elif cmd in ("q", "quit", "exit"):
            break
        elif cmd in ("h", "help", "?"):
            messages = HELP
        elif cmd in ("u", "undo"):
            if not state["history"]:
                messages = [(YELLOW, "Nothing to undo.")]
            else:
                h = state["history"].pop()
                s = state["sectors"][h["sector"]]
                s["best_ms"] = h["old_ms"]
                s["achieved_at"] = h.get("old_achieved_at")
                s["achieved_timer_ms"] = h.get("old_achieved_timer_ms")
                write_state(state)
                highlight = h["sector"]
                messages = [(YELLOW, f"Undone: {s['name']} back to {fmt_ms(h['old_ms'])} "
                                     f"(was {fmt_ms(h['new_ms'])}).")]
        elif cmd == "reset":
            try:
                answer = input(pad + "Reset ALL sectors to defaults? Type YES to confirm: ")
            except (EOFError, KeyboardInterrupt, OSError):
                answer = ""
            if answer.strip() == "YES":
                # Back to this track's own defaults (keeps names and limits).
                for s in state["sectors"]:
                    s["best_ms"], s["achieved_at"], s["achieved_timer_ms"] = s["default_ms"], None, None
                state["history"] = []
                write_state(state)
                messages = [(YELLOW, "All sectors reset to defaults.")]
            else:
                messages = [(DIM, "Reset cancelled.")]
        elif cmd in ("stop", "start"):
            messages = [control_timer(cmd)]
        elif cmd == "restart":
            gui_pid = os.environ.get("COPIUM_GUI_PID", "")
            # Wait for the Green Timer (or this process, if run standalone) to
            # exit and save, then launch both windows fresh.
            spawn_relaunch(int(gui_pid) if gui_pid.isdigit() else os.getpid())
            return  # the Green Timer sees us exit and closes itself
        else:
            m = SET_RE.match(line)
            if not m:
                looks_like_time = re.match(r"^s?\d", cmd)
                messages = [(RED, f"Not accepted: '{line}'. {FORMAT_HINT}" if looks_like_time
                                  else f"Unknown command '{line}'. Type 'help'.")]
            else:
                minutes, seconds, frac = (int(g) for g in m.groups()[1:])
                new = minutes * 60_000 + seconds * 1000 + frac
                messages, highlight, _ = apply_sector(state, int(m.group(1)), new)

    sys.stdout.write(RESET + "\n")


# --------------------------------------------------------------------------
# GUI: "Green Timer"
# --------------------------------------------------------------------------

KEY_HEX = "#010203"     # color key: pixels of exactly this color are invisible
GREEN_HEX = "#34b062"
PAUSED_HEX = "#24723f"
EDGE_HEX = "#06140b"    # dark edge around the digits, keeps them readable on any background
WINDOW_ALPHA = 1.0      # 1.0 = solid digits, lower = see-through digits
TICK_MS = 100
SAVE_EVERY_S = 1.0


def virtual_screen():
    """(left, top, width, height) of all monitors together."""
    if os.name == "nt":
        import ctypes
        m = ctypes.windll.user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)
    return None


class GreenTimer:
    """Stopwatch that starts on launch and keeps its elapsed time across sessions.

    Frameless with an invisible background: only the digits are visible.
    Drag the digits to move, scroll over them to resize, right-click for the menu.
    """

    def __init__(self, root, child=None, keep_stopped=False):
        import tkinter as tk
        import tkinter.font as tkfont

        self.root, self.child = root, child
        self.scale = root.winfo_fpixels("1i") / 96.0
        stored = read_timer()

        root.title(GUI_TITLE)
        root.overrideredirect(True)
        root.configure(bg=KEY_HEX)
        try:
            root.attributes("-transparentcolor", KEY_HEX)
        except tk.TclError:
            pass  # not supported on this platform: background stays dark
        root.attributes("-topmost", True)
        root.attributes("-alpha", WINDOW_ALPHA)
        root.geometry(self._initial_geometry(stored["geometry"]))
        root.protocol("WM_DELETE_WINDOW", self.close)

        families = set(tkfont.families(root))
        family = next((f for f in ("Bahnschrift", "Segoe UI", "Consolas") if f in families), "TkDefaultFont")
        self.font = tkfont.Font(root, family=family, size=-40, weight="bold")

        self.canvas = tk.Canvas(root, bg=KEY_HEX, highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.save())
        self.canvas.bind("<MouseWheel>", self.wheel)

        self.topmost = tk.BooleanVar(value=True)
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Stop timer", command=lambda: self.set_running(not self.running))
        self.menu.add_command(label="Reset timer...", command=self.reset)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost,
                                  command=lambda: root.attributes("-topmost", self.topmost.get()))
        self.menu.add_separator()
        self.menu.add_command(label="Restart windows", command=self.restart)
        self.menu.add_command(label="Quit", command=self.close)
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        # Resume from the stored value and start running immediately - except
        # after a restart of a stopped timer, which stays stopped.
        self.base_ms = stored["elapsed_ms"]
        self.t0 = time.perf_counter()
        self.running = True
        self.last_save = 0.0
        self.shown_text = None
        self.drag_offset = (0, 0)
        self.control_mtime = self._control_mtime()  # ignore requests from before we started
        if keep_stopped and not stored["running"]:
            self.set_running(False)
        self.save()
        self.tick()

    @staticmethod
    def _control_mtime():
        try:
            return os.stat(CONTROL_PATH).st_mtime_ns
        except OSError:
            return None

    def check_control(self):
        """Apply a stop/start request written by the terminal."""
        mtime = self._control_mtime()
        if mtime is None or mtime == self.control_mtime:
            return
        try:
            with open(CONTROL_PATH, "r", encoding="utf-8") as f:
                action = json.load(f).get("action")
        except (OSError, ValueError, AttributeError):
            return  # mid-write: retry on the next tick
        self.control_mtime = mtime
        if action in ("stop", "start"):
            self.set_running(action == "start")

    def _initial_geometry(self, saved):
        m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", saved or "")
        screen = virtual_screen()
        if m:
            w, h, x, y = (int(g) for g in m.groups())
            # Only reuse the saved position if it's still on a screen.
            if screen is None or (screen[0] <= x + w // 2 < screen[0] + screen[2]
                                  and screen[1] <= y + h // 2 < screen[1] + screen[3]):
                return saved
        w, h = int(240 * self.scale), int(70 * self.scale)
        x = (self.root.winfo_screenwidth() - w) // 2
        return f"{w}x{h}+{x}+{int(30 * self.scale)}"

    def elapsed_ms(self):
        if not self.running:
            return self.base_ms
        return self.base_ms + int((time.perf_counter() - self.t0) * 1000)

    def save(self):
        try:
            write_json(TIMER_PATH, {"elapsed_ms": self.elapsed_ms(), "running": self.running,
                                    "saved_at": time.time(), "geometry": self.root.geometry()})
        except OSError:
            pass  # try again on the next save
        self.last_save = time.perf_counter()

    def tick(self):
        self.check_control()
        text = fmt_hms(self.elapsed_ms())
        if text != self.shown_text:
            self.shown_text = text
            self.draw()
        if time.perf_counter() - self.last_save >= SAVE_EVERY_S:
            self.save()
        if self.child is not None and self.child.poll() is not None:
            self.close()  # terminal was closed -> close too
            return
        self.root.after(TICK_MS, self.tick)

    def start_drag(self, e):
        self.drag_offset = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag(self, e):
        dx, dy = self.drag_offset
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def wheel(self, e):
        factor = 1.1 if e.delta > 0 else 1 / 1.1
        r = self.root
        w, h = r.winfo_width(), r.winfo_height()
        nw = max(int(80 * self.scale), int(w * factor))
        nh = max(int(24 * self.scale), int(h * factor))
        # Resize around the center so the digits stay in place.
        r.geometry(f"{nw}x{nh}+{r.winfo_x() - (nw - w) // 2}+{r.winfo_y() - (nh - h) // 2}")
        self.save()

    def set_running(self, running):
        if running == self.running:
            return
        if running:
            self.t0, self.running = time.perf_counter(), True
        else:
            self.base_ms, self.running = self.elapsed_ms(), False
        self.menu.entryconfigure(0, label="Stop timer" if self.running else "Start timer")
        self.save()
        self.draw()

    def reset(self):
        from tkinter import messagebox
        if messagebox.askyesno(GUI_TITLE, "Reset the Green Timer to 00:00:00?", parent=self.root):
            self.base_ms, self.t0 = 0, time.perf_counter()
            self.save()
            self.shown_text = None

    def restart(self):
        spawn_relaunch(os.getpid())
        self.close()

    def draw(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < 4:
            return
        text = fmt_hms(self.elapsed_ms())

        size = max(6, int(h * 0.9))
        self.font.configure(size=-size)
        cells = self._cells(text)
        width = sum(cw for _, cw in cells)
        if width > w * 0.95:
            size = max(6, int(size * w * 0.95 / width))
            self.font.configure(size=-size)
            cells = self._cells(text)
            width = sum(cw for _, cw in cells)

        # Center the digits optically: digit height is ~0.72 of the ascent.
        ascent = self.font.metrics("ascent")
        top = h / 2 + ascent * 0.72 / 2 - ascent
        x = (w - width) / 2
        color = GREEN_HEX if self.running else PAUSED_HEX
        edge = max(1, round(size / 36))
        c.delete("all")
        for ch, cw in cells:
            cx = x + cw / 2
            # Dark outline + drop shadow, then the digit itself on top.
            for ox, oy in ((-edge, 0), (edge, 0), (0, -edge), (0, edge), (edge * 2, edge * 2)):
                c.create_text(cx + ox, top + oy, text=ch, font=self.font, fill=EDGE_HEX, anchor="n")
            c.create_text(cx, top, text=ch, font=self.font, fill=color, anchor="n")
            x += cw

    def _cells(self, text):
        # Fixed-width cells so the digits don't jitter as they change.
        digit_w = max(self.font.measure(d) for d in "0123456789")
        sep_w = int(self.font.measure(":") * 1.1)
        return [(ch, digit_w if ch.isdigit() else sep_w) for ch in text]

    def close(self):
        self.save()
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
        self.root.destroy()


def run_gui(child=None, keep_stopped=False):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    import tkinter as tk
    root = tk.Tk()
    GreenTimer(root, child, keep_stopped)
    root.mainloop()


# --------------------------------------------------------------------------
# Launcher
# --------------------------------------------------------------------------

def console_python():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def windowless_python():
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def spawn_relaunch(wait_pid):
    """Start a fresh launcher that waits for `wait_pid` to exit, then opens both windows."""
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP  # DETACHED_PROCESS
    subprocess.Popen(
        [windowless_python(), str(SCRIPT), "--wait-pid", str(wait_pid)],
        cwd=str(APP_DIR), creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_for_pid(pid, timeout_s=15):
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:  # 0 = already gone
            k32.WaitForSingleObject(handle, int(timeout_s * 1000))
            k32.CloseHandle(handle)
    else:
        time.sleep(1.5)


def launch(keep_stopped=False):
    if os.name != "nt":
        print(f"Green Timer started. Run '{SCRIPT.name} --cli' in another terminal for the Copium timer.")
        run_gui(keep_stopped=keep_stopped)
        return
    cmd = [console_python(), str(SCRIPT), "--cli", "--resize"]
    # If Windows Terminal is the default terminal, a new console would open as
    # a tab in an existing WT window. Hosting it in conhost guarantees its own
    # standalone window (and closing that window ends the process we watch).
    conhost = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "conhost.exe"
    if conhost.exists():
        cmd = [str(conhost)] + cmd
    # The terminal needs our pid so its 'restart' command can wait for us to exit.
    env = dict(os.environ, COPIUM_GUI_PID=str(os.getpid()))
    child = subprocess.Popen(cmd, cwd=str(APP_DIR), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        run_gui(child, keep_stopped)
    finally:
        if child.poll() is None:
            child.terminate()


def main():
    args = sys.argv[1:]
    try:
        if "--cli" in args:
            run_cli(resize="--resize" in args)
        elif "--gui" in args:
            run_gui()
        else:
            restarting = "--wait-pid" in args
            if restarting:  # let the old windows exit and save first
                i = args.index("--wait-pid")
                if i + 1 < len(args) and args[i + 1].isdigit():
                    wait_for_pid(int(args[i + 1]))
            launch(keep_stopped=restarting)
    except Exception:
        # Under pythonw there's no console to show errors - keep a log.
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
