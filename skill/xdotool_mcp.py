#!/usr/bin/env python3
"""xdotool-mcp: a minimal, dependency-free MCP server exposing X11 input.

Hand-rolled JSON-RPC over stdio (no `mcp` SDK needed) implementing just
enough of the MCP protocol for Hermes to connect:
  - initialize
  - tools/list
  - tools/call
Each request is a JSON object on its own line; responses go to stdout.

INPUT is done by xdotool (XSendEvent to a real window_id). This works through
flatpak/snap bwrap sandboxes that cua-driver's pid-based input CANNOT reach.
Screenshots/window-info stay with Hermes computer_use (cua-driver capture).

Tools:
  list_windows() -> visible windows (title, window_id)
  focus_window(name) -> activate window by title substring; returns window_id
  press_key(key, window_id?/name?) -> one key (cua name -> xdotool keysym)
  type_text(text, window_id?/name?) -> type text
  click(x, y, button, count, window_id?/name?) -> move real cursor + click
  drag(x1,y1,x2,y2, button, window_id?/name?) -> drag
  scroll(x, y, direction, amount, window_id?/name?) -> scroll
  mouse_move(x, y, window_id?/name?) -> move cursor
"""
import json
import os
import re
import shutil
import subprocess
import sys

# xdotool talks to the X server via $DISPLAY. The MCP server may be spawned by
# Hermes in an environment where DISPLAY is stripped (bare subprocess -> xdotool
# sees 0 windows). Force it so input always reaches the display.
os.environ.setdefault("DISPLAY", ":0")

# ---------------------------------------------------------------------------
# xdotool helpers
# ---------------------------------------------------------------------------
def _has_xdotool() -> bool:
    return shutil.which("xdotool") is not None


# ---------------------------------------------------------------------------
# mGBA socket control (emulator-internal input, no OS injection)
# ---------------------------------------------------------------------------
# mGBA 0.10.x ships Lua scripting (liblua + LuaSocket). mgba_agent.lua runs
# INSIDE mGBA, opens a TCP socket on 127.0.0.1:8930, and drives the GBA input
# via emu:setKeys(). This is the ONLY input path that reliably works for mGBA:
# xdotool/ydotool synthetic keys are rejected by mGBA's SDL/Qt input grab, and
# OS focus hacks are fragile. The socket talks to the emulator directly.
_MGBA_SOCKET_HOST = "127.0.0.1"
_MGBA_SOCKET_PORT = 8930

# GBA button -> bit (matches mGBA KEY_NAMES order)
_MGBA_BITS = {
    "A": 1 << 0, "B": 1 << 1, "SELECT": 1 << 2, "START": 1 << 3,
    "LEFT": 1 << 4, "RIGHT": 1 << 5, "UP": 1 << 6, "DOWN": 1 << 7,
    "R": 1 << 8, "L": 1 << 9,
}

def _mgba_socket_send(cmd, timeout=3.0):
    """Send one command line to the running mgba_agent.lua socket.
    Returns the server's text reply, or None if the socket isn't up."""
    import socket as _sock
    try:
        s = _sock.create_connection((_MGBA_SOCKET_HOST, _MGBA_SOCKET_PORT), timeout=timeout)
        s.sendall((cmd + "\n").encode("utf-8"))
        s.settimeout(timeout)
        buf = b""
        # read until newline or short timeout
        try:
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except _sock.timeout:
            pass
        s.close()
        return buf.decode("utf-8", "replace").strip()
    except Exception as e:
        return f"ERR socket: {e}"


def mgba_press(button, action="PRESS"):
    """Press a GBA button inside mGBA via the Lua socket.
    action: PRESS (tap ~80ms) | HOLD | REL (release)."""
    b = str(button).upper()
    b = {"S": "START", "SSELECT": "SELECT"}.get(b, b)
    if b not in _MGBA_BITS:
        return {"ok": False, "error": f"unknown GBA button '{button}'"}
    resp = _mgba_socket_send(f"{action} {b}")
    return {"ok": True, "button": b, "action": action, "reply": resp}


def mgba_type(text):
    """Type a string into mGBA by tapping A/B/dpad? Not generally meaningful for
    a game; provided for completeness (types via repeated HOLD/REL of keys)."""
    return {"ok": False, "error": "mgba_type not supported (use mgba_press per button)"}


def _mgba_socket_up():
    """Quick liveness check of the mGBA control socket."""
    r = _mgba_socket_send("STATUS", timeout=1.0)
    return r is not None and not str(r).startswith("ERR")


# ---------------------------------------------------------------------------
# ydotool input (real kernel-level input; works on ANY X11 window incl. flatpak)
# ---------------------------------------------------------------------------
# ydotool injects via /dev/uinput, so SDL/Qt keyboard grabs CANNOT reject it
# (unlike xdotool). Keys are RAW Linux keycodes (linux/input-event-codes.h);
# the daemon (ydotoold) holds device state across calls, so a drag can press
# in one call and release in a later call.
_LINUX_KEYCODE = {
    "escape": 1, "esc": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "-": 12, "=": 13, "minus": 12, "equal": 13,
    "backspace": 14,
    "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25,
    "[": 26, "]": 27, "bracketleft": 26, "bracketright": 27,
    "enter": 28, "return": 28, "ret": 28,
    "ctrl": 29, "lctrl": 29, "leftctrl": 29, "control": 29, "rctrl": 97,
    "rightctrl": 97,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    ";": 39, "semicolon": 39, "'": 40, "apostrophe": 40, "`": 41, "grave": 41,
    "shift": 42, "lshift": 42, "leftshift": 42, "rshift": 54, "rightshift": 54,
    "\\": 43, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    ",": 51, "comma": 51, ".": 52, "dot": 52, "/": 53, "slash": 53,
    "alt": 56, "lalt": 56, "leftalt": 56, "ralt": 100, "rightalt": 100,
    "space": 57, " ": 57,
    "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "home": 102, "up": 103, "pageup": 104, "pgup": 104,
    "left": 105, "right": 106, "end": 107, "down": 108, "pagedown": 109,
    "pgdn": 109, "insert": 110, "ins": 110, "delete": 111, "del": 111,
    "win": 125, "super": 125, "meta": 125,
}


def _has_ydotool() -> bool:
    return shutil.which("ydotool") is not None


def _ydotool_key_seq(parts):
    """parts: list of key names; emit press+release for each (combined = held
    chord). Returns (ok, err)."""
    seq = []
    for name in parts:
        code = _LINUX_KEYCODE.get(str(name).lower())
        if code is None:
            return False, f"unknown key '{name}'"
        seq.append(f"{code}:1")
        seq.append(f"{code}:0")
    if not seq:
        return False, "empty key sequence"
    ok, err = _run(["ydotool", "key"] + seq)
    return ok, err


def ydotool_press(key):
    """Press ONE key (or a chord like 'ctrl+c') via ydotool real input."""
    k = str(key).strip()
    if "+" in k:
        parts = [p.strip() for p in k.split("+")]
        ok, err = _ydotool_key_seq(parts)
        return {"ok": ok, "key": key, "method": "ydotool", "error": err}
    if k.lower() in _LINUX_KEYCODE:
        ok, err = _ydotool_key_seq([k])
        return {"ok": ok, "key": key, "method": "ydotool", "error": err}
    # single character -> let ydotool type handle shift/caps
    if len(k) == 1:
        ok, err = _run(["ydotool", "type", k])
        return {"ok": ok, "key": key, "method": "ydotool", "error": err}
    return {"ok": False, "error": f"unknown key '{key}'"}


def ydotool_type(text):
    """Type a string via ydotool (handles shift/caps internally)."""
    ok, err = _run(["ydotool", "type", str(text)])
    return {"ok": ok, "text": text, "method": "ydotool", "error": err}


_BUTTON_HEX = {"left": "0x00", "right": "0x01", "middle": "0x02",
               "1": "0x00", "2": "0x01", "3": "0x02"}


def ydotool_click(x, y, button="left"):
    btn = _BUTTON_HEX.get(str(button).lower(), "0x00")
    ok1, err1 = _run(["ydotool", "mousemove", "-a", "-x", str(x), "-y", str(y)])
    ok2, err2 = _run(["ydotool", "click", btn])
    return {"ok": ok1 and ok2, "x": x, "y": y, "button": button,
            "method": "ydotool", "error": err1 or err2}


def ydotool_move(x, y):
    ok, err = _run(["ydotool", "mousemove", "-a", "-x", str(x), "-y", str(y)])
    return {"ok": ok, "x": x, "y": y, "method": "ydotool", "error": err}


def ydotool_scroll(x, y, direction="up", amount=3):
    # move to position, then wheel: down = positive y, up = negative y
    dy = amount if direction.lower() in ("down", "wheeldown") else -amount
    ok1, err1 = _run(["ydotool", "mousemove", "-a", "-x", str(x), "-y", str(y)])
    ok2, err2 = _run(["ydotool", "mousemove", "-w", "-y", str(dy)])
    return {"ok": ok1 and ok2, "x": x, "y": y, "direction": direction,
            "amount": amount, "method": "ydotool", "error": err1 or err2}


def ydotool_drag(x1, y1, x2, y2, button="left"):
    """Drag: move to start, press (hold), move to end, release.
    ydotoold holds button state across calls, so we omit the UP on press
    (mask 0x40) and omit the DOWN on release (mask 0x80)."""
    b = _BUTTON_HEX.get(str(button).lower(), "0x00")
    down = f"0x{(int(b,16) | 0x40):02x}"   # omit UP -> hold
    up = f"0x{(int(b,16) | 0x80):02x}"     # omit DOWN -> release
    ok1, err1 = _run(["ydotool", "mousemove", "-a", "-x", str(x1), "-y", str(y1)])
    ok2, err2 = _run(["ydotool", "click", down])
    ok3, err3 = _run(["ydotool", "mousemove", "-a", "-x", str(x2), "-y", str(y2)])
    ok4, err4 = _run(["ydotool", "click", up])
    return {"ok": all([ok1, ok2, ok3, ok4]), "from": (x1, y1), "to": (x2, y2),
            "method": "ydotool", "error": err1 or err2 or err3 or err4}


def _keysym(name: str) -> str:
    v = (name or "").lower()
    if v in ("up", "down", "left", "right"):
        return v.capitalize()
    return {
        "return": "Return", "enter": "Return", "backspace": "BackSpace",
        "escape": "Escape", "tab": "Tab", "space": "space",
        "cmd": "Super", "command": "Super", "option": "Alt", "alt": "Alt",
        "ctrl": "Control", "control": "Control", "shift": "Shift", "fn": "Fn",
    }.get(v, name or "")


def _btn(button: str) -> str:
    return {"left": "1", "middle": "2", "right": "3"}.get((button or "left").lower(), "1")


def _activate(wid: int) -> None:
    if not wid:
        return
    # Raise the window AND give it true X keyboard focus by clicking its
    # center. `windowactivate` alone is ignored by SDL/Qt apps that grab the
    # keyboard; a real (synthetic) click establishes focus the grab honors.
    # xdotool's `key`/`type` are then sent globally (no --window) so they
    # route through the normal focus path to the frontmost window.
    subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)],
                   capture_output=True, text=True, timeout=10)
    # Fallback if windowactivate didn't take.
    subprocess.run(["xdotool", "windowfocus", str(wid)],
                   capture_output=True, text=True, timeout=10)
    # Find the on-screen center to click. Some windows (e.g. mGBA's flatpak
    # container) report a 1x1 geometry at (-100,-100); in that case use the
    # largest child window's geometry instead.
    geom = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(wid)],
                          capture_output=True, text=True, timeout=10).stdout
    cx = cy = w = h = 0
    for line in geom.splitlines():
        if line.startswith("X="):
            cx = int(line.split("=")[1])
        elif line.startswith("Y="):
            cy = int(line.split("=")[1])
        elif line.startswith("WIDTH="):
            w = int(line.split("=")[1])
        elif line.startswith("HEIGHT="):
            h = int(line.split("=")[1])
    if w <= 2 or h <= 2 or cx < 0 or cy < 0:
        # 1x1 / off-screen container -> use the largest visible child.
        kids = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid",
                               str(_pid_of(wid))], capture_output=True, text=True, timeout=10).stdout.split()
        best = (0, 0, 0, 0, 0)
        for k in kids:
            if not k.strip().isdigit() or int(k) == wid:
                continue
            g2 = subprocess.run(["xdotool", "getwindowgeometry", "--shell", k],
                                capture_output=True, text=True, timeout=5).stdout
            x2 = y2 = w2 = h2 = 0
            for gl in g2.splitlines():
                if gl.startswith("X="): x2 = int(gl.split("=")[1])
                elif gl.startswith("Y="): y2 = int(gl.split("=")[1])
                elif gl.startswith("WIDTH="): w2 = int(gl.split("=")[1])
                elif gl.startswith("HEIGHT="): h2 = int(gl.split("=")[1])
            if w2 * h2 > best[3] * best[4]:
                best = (int(k), x2, y2, w2, h2)
        if best[3] > 2:
            wid, cx, cy, w, h = best[0], best[1], best[2], best[3], best[4]
    ccx, ccy = cx + w // 2, cy + h // 2
    if ccx > 0 and ccy > 0:
        subprocess.run(["xdotool", "mousemove", str(ccx), str(ccy)],
                       capture_output=True, text=True, timeout=10)
        subprocess.run(["xdotool", "click", "1"],
                       capture_output=True, text=True, timeout=10)


def _pid_of(wid: int):
    try:
        return subprocess.run(["xdotool", "getwindowpid", str(wid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _find_window(name: str):
    """Forgiving window lookup: case-insensitive CONTAINS match across all
    visible windows (not exact). Returns (window_id, title) or (None, None).
    Used only as an OPTIONAL hint — input works globally without a match."""
    if not name:
        return None, None
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""],
                             capture_output=True, text=True, timeout=10)
        wids = [w for w in out.stdout.split() if w.strip().isdigit()]
        rows = []
        for wid in wids:
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            if t:
                rows.append((int(wid), t))
        # prefer a contains-match on the name
        needle = name.lower()
        for wid, t in rows:
            if needle in t.lower():
                return wid, t
        # fallback: any window whose title shares a token with the query
        for wid, t in rows:
            if any(tok in t.lower() for tok in needle.split() if len(tok) > 2):
                return wid, t
    except Exception:
        pass
    return None, None


def _available_titles():
    """List visible window titles — used to build helpful 'no match' errors."""
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""],
                             capture_output=True, text=True, timeout=10)
        wids = [w for w in out.stdout.split() if w.strip().isdigit()]
        titles = []
        for wid in wids:
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            if t:
                titles.append(t)
        return titles
    except Exception:
        return []


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return r.returncode == 0, r.stderr.strip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def list_windows():
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""],
                              capture_output=True, text=True, timeout=10)
        wids = [w for w in out.stdout.split() if w.strip().isdigit()]
        rows = []
        for wid in wids:
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            if t:
                rows.append({"window_id": int(wid), "title": t})
        return rows
    except Exception as e:
        return {"error": str(e)}


def focus_window(name):
    """OPTIONAL helper: raise a window and click it to give it true X keyboard
    focus (needed only for apps that grab the keyboard, e.g. mGBA's SDL).
    Input works GLOBALLY without this — but focusing mGBA first makes its
    game keys register reliably. Forgiving contains-match on the name."""
    wid, title = _find_window(name)
    if wid is None:
        titles = _available_titles()
        return {"ok": False,
                "error": f"no window matching '{name}'.",
                "available_windows": titles}
    _activate(wid)
    return {"ok": True, "window_id": wid, "title": title}


def _resolve_wid(window_id=None, name="mGBA"):
    """Resolve a window id: explicit id wins, else forgiving name match,
    else None (caller decides: focus+act atomically, or act globally)."""
    if window_id:
        return int(window_id)
    if name:
        wid, _ = _find_window(name)
        return wid
    return None


def _to_global(x, y, name, window_id):
    """Translate WINDOW-LOCAL screenshot coords (what the model sees) to GLOBAL
    X screen coords. xdotool mousemove is global, but the screenshot tool
    returns window-local pixels, so we must add the window's top-left offset."""
    wid = _resolve_wid(window_id, name)
    if wid is None:
        return x, y
    try:
        geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(wid)],
                             capture_output=True, text=True, timeout=10).stdout
        gx = gy = 0
        for ln in geo.splitlines():
            if ln.startswith("X="):
                gx = int(ln.split("=")[1])
            elif ln.startswith("Y="):
                gy = int(ln.split("=")[1])
        return gx + x, gy + y
    except Exception:
        return x, y


# Longest-side cap for screenshots (keeps the image tiny in context = fast).
SHOT_MAX_SIDE = 854

_PRIMARY = None  # cached (x, y, w, h) of the primary monitor in global space


def _primary_monitor():
    """Return the primary monitor's global rect (x, y, w, h), cached.
    Parsed from `xrandr` (the monitor tagged 'primary')."""
    global _PRIMARY
    if _PRIMARY:
        return _PRIMARY
    try:
        out = subprocess.run(["xrandr", "--query"],
                             capture_output=True, text=True, timeout=10).stdout
        for ln in out.splitlines():
            if "primary" in ln:
                # e.g. "HDMI-1 connected primary 2560x1440+1920+0 (...)"
                m = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", ln)
                if m:
                    w, h, x, y = (int(g) for g in m.groups())
                    _PRIMARY = (x, y, w, h)
                    return _PRIMARY
    except Exception:
        pass
    # fallback: whole screen
    _PRIMARY = (0, 0, 1920, 1080)
    return _PRIMARY


def _from_primary(x, y):
    """Translate coords the model READ from the downscaled primary-monitor
    screenshot (what it sees) into GLOBAL X screen coords.

    Flow: model sees a screenshot downscaled so its longest side = SHOT_MAX_SIDE.
    Upscale by the same ratio to recover original primary-monitor pixels, then
    add the primary monitor's global offset."""
    px, py, pw, ph = _primary_monitor()
    s = max(pw, ph) / float(SHOT_MAX_SIDE)
    return int(px + x * s), int(py + y * s)


def press_key(key, window_id=None, name="mGBA"):
    """Send ONE key. For mGBA this goes through the emulator-internal Lua socket
    (emu:setKeys) which is the only reliable path (xdotool/ydotool synthetic keys
    are rejected by mGBA's input grab). For other windows it falls back to
    ydotool/xdotool (global or focused)."""
    if name and name.lower() in ("mgba", "mGBA", "pokemon"):
        # emulator-internal input — no OS injection, no focus needed
        btn = _map_to_gba_button(key)
        if btn:
            return mgba_press(btn, "PRESS")
        # not a GBA button name; fall through to ydotool for mGBA's own menus
    # non-mGBA (or unknown GBA button): use ydotool real kernel input.
    # ydotool works on ANY window (incl. flatpak) because it injects at the
    # kernel uinput layer — SDL/Qt grabs can't reject it (unlike xdotool).
    if _has_ydotool():
        return ydotool_press(key)
    # last-resort fallback: xdotool
    wid = _resolve_wid(window_id, name)
    if wid:
        ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                        "key", _keysym(key)])
    else:
        ok, err = _run(["xdotool", "key", _keysym(key)])
    return {"ok": ok, "key": key, "method": "xdotool", "error": err}


def _map_to_gba_button(key):
    """Map a key name to a GBA button for the mGBA socket path."""
    k = str(key).lower()
    return {
        "a": "A", "b": "B", "x": "A", "y": "B",
        "start": "START", "return": "START", "enter": "START",
        "select": "SELECT", "backspace": "SELECT",
        "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
        "^": "UP", "v": "DOWN", "<": "LEFT", ">": "RIGHT",
    }.get(k)


def type_text(text, window_id=None, name="mGBA"):
    """Type text. For mGBA, typing is not meaningful (game uses buttons). For
    other windows, use ydotool real input (works on any window incl. flatpak)."""
    if name and name.lower() in ("mgba", "mGBA", "pokemon"):
        return {"ok": False, "error": "typing is not meaningful for mGBA (use buttons)"}
    if _has_ydotool():
        return ydotool_type(text)
    # fallback: xdotool
    wid = _resolve_wid(window_id, name)
    if wid:
        ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                        "type", "--delay", "0", text])
    else:
        ok, err = _run(["xdotool", "type", "--delay", "0", text])
    return {"ok": ok, "chars": len(text), "error": err}


def click(x, y, button="left", count=1, window_id=None, name="mGBA"):
    """Click at coords from a screenshot; translated to global coords.
    - name="primary": coords are from the downscaled primary-monitor screenshot
      (screenshot(window_name="primary")); auto upscaled + offset to global.
    - other name: window-local coords (window geometry offset added).
    - no name: coords are already global.
    Mouse uses xdotool (correct global multi-monitor coords)."""
    if name and name.lower() == "primary":
        gx, gy = _from_primary(x, y)
    else:
        gx, gy = _to_global(x, y, name, window_id)
    btn = _btn(button)
    wid = _resolve_wid(window_id, name)
    if wid:
        cmd = ["xdotool", "windowactivate", "--sync", str(wid),
               "mousemove", str(gx), str(gy)]
    else:
        cmd = ["xdotool", "mousemove", str(gx), str(gy)]
    for _ in range(max(1, count)):
        ok, err = _run(cmd + ["click", btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "global": [gx, gy], "button": button}


def drag(x1, y1, x2, y2, button="left", window_id=None, name="mGBA"):
    """Drag between coords from a screenshot; translated to global.
    name="primary" uses the downscaled primary-monitor screenshot coords."""
    if name and name.lower() == "primary":
        gx1, gy1 = _from_primary(x1, y1)
        gx2, gy2 = _from_primary(x2, y2)
    else:
        gx1, gy1 = _to_global(x1, y1, name, window_id)
        gx2, gy2 = _to_global(x2, y2, name, window_id)
    btn = _btn(button)
    wid = _resolve_wid(window_id, name)
    steps = max(2, min(20, abs(gx2 - gx1) + abs(gy2 - gy1) // 20))
    cmd = ["xdotool"]
    if wid:
        cmd += ["windowactivate", "--sync", str(wid)]
    cmd += ["mousemove", str(gx1), str(gy1), "mousedown", btn]
    for i in range(1, steps + 1):
        ix = gx1 + (gx2 - gx1) * i // steps
        iy = gy1 + (gy2 - gy1) * i // steps
        cmd += ["mousemove", str(ix), str(iy)]
    cmd += ["mouseup", btn]
    ok, err = _run(cmd)
    return {"ok": ok, "from": [x1, y1], "to": [x2, y2],
            "global_from": [gx1, gy1], "global_to": [gx2, gy2], "error": err}


def scroll(x, y, direction="up", amount=3, window_id=None, name="mGBA"):
    """Scroll at coords from a screenshot; translated to global.
    name="primary" uses the downscaled primary-monitor screenshot coords."""
    if name and name.lower() == "primary":
        gx, gy = _from_primary(x, y)
    else:
        gx, gy = _to_global(x, y, name, window_id)
    btn = "4" if (direction or "").lower() in ("up", "left") else "5"
    wid = _resolve_wid(window_id, name)
    for _ in range(max(1, min(50, amount))):
        if wid:
            ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                            "mousemove", str(gx), str(gy), "click", btn])
        else:
            ok, err = _run(["xdotool", "mousemove", str(gx), str(gy), "click", btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "global": [gx, gy], "direction": direction, "amount": amount}


def mouse_move(x, y, window_id=None, name="mGBA"):
    """Move the real cursor to coords from a screenshot; translated to global.
    name="primary" uses the downscaled primary-monitor screenshot coords."""
    if name and name.lower() == "primary":
        gx, gy = _from_primary(x, y)
    else:
        gx, gy = _to_global(x, y, name, window_id)
    wid = _resolve_wid(window_id, name)
    if wid:
        ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                        "mousemove", str(gx), str(gy)])
    else:
        ok, err = _run(["xdotool", "mousemove", str(gx), str(gy)])
    return {"ok": ok, "x": x, "y": y, "global": [gx, gy], "error": err}


def screenshot(window_name=None):
    """Capture to a PNG and return it as an MCP image block.

    Returns ONLY the image (no SOM/AX summary text) so the model pays ~0 text
    tokens per screenshot — the whole point of moving capture off cua-driver,
    whose capture attaches a ~1.5k-token 'SOM index + summary' text block.

    Modes:
      - window_name="primary": capture the PRIMARY monitor only (cropped from a
        full-screen shot), downscaled to SHOT_MAX_SIDE. Returns image + a `meta`
        block with the coordinate `scale` and `monitor` rect so the model can
        map the pixels it sees back to real screen coordinates for clicking.
        This is the FAST, single-monitor path (no multi-monitor confusion).
      - window_name=<other>: capture that window (window-local coords).
      - no window_name: capture the focused window.
    """
    import base64
    px, py, pw, ph = _primary_monitor()
    try:
        import tempfile, os
        fd, tmppath = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        os.remove(tmppath)  # scrot won't overwrite; let it create fresh

        if window_name and window_name.lower() == "primary":
            # full-screen, then crop to the primary monitor rect
            full = tmppath + ".full.png"
            png = subprocess.run(["scrot", full],
                                 capture_output=True, text=True, timeout=15)
            if png.returncode != 0 or not os.path.getsize(full):
                try: os.remove(full)
                except Exception: pass
                return {"isError": True, "content": [{"type": "text",
                        "text": json.dumps({"error": "scrot failed",
                                             "stderr": png.stderr[:200]})}]}
            from io import BytesIO
            from PIL import Image
            img = Image.open(full)
            os.remove(full)
            img = img.crop((px, py, px + pw, py + ph))
            scale = max(pw, ph) / float(SHOT_MAX_SIDE)
            if max(img.size) > SHOT_MAX_SIDE:
                sc = SHOT_MAX_SIDE / float(max(img.size))
                img = img.resize((max(1, int(img.size[0] * sc)),
                                  max(1, int(img.size[1] * sc))), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="PNG")
            raw = buf.getvalue()
            b64 = base64.b64encode(raw).decode("ascii")
            meta = {"scale": round(scale, 4),
                    "monitor": {"x": px, "y": py, "w": pw, "h": ph},
                    "note": "coords you read from this image are in the "
                            "downscaled space; click(name='primary', x, y) "
                            "maps them back automatically"}
            return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"},
                                {"type": "text", "text": json.dumps(meta)}]}

        # window (or focused-window) capture
        wid = None
        if window_name:
            wid, _ = _find_window(window_name)
        if wid is None:
            try:
                wid = subprocess.run(["xdotool", "getwindowfocus"],
                                     capture_output=True, text=True, timeout=10).stdout.strip()
            except Exception:
                wid = None
        if not wid or (isinstance(wid, str) and not wid.isdigit()):
            return {"isError": True, "content": [{"type": "text",
                    "text": json.dumps({"error": "no window to capture"})}]}
        wid = int(wid)
        png = subprocess.run(["scrot", "-w", str(wid), tmppath],
                             capture_output=True, text=True, timeout=15)
        if png.returncode != 0 or not os.path.getsize(tmppath):
            try: os.remove(tmppath)
            except Exception: pass
            return {"isError": True, "content": [{"type": "text",
                    "text": json.dumps({"error": "scrot failed", "stderr": png.stderr[:200]})}]}
        with open(tmppath, "rb") as f:
            raw = f.read()
        os.remove(tmppath)
        # Downscale before encoding — a 1440p window PNG is enormous in base64
        # and blows the model context. Cap the longest side (default 854 ~
        # "480p is plenty" for a game) so the image is tiny in context.
        try:
            from io import BytesIO
            from PIL import Image
            img = Image.open(BytesIO(raw))
            if max(img.size) > SHOT_MAX_SIDE:
                sc = SHOT_MAX_SIDE / float(max(img.size))
                new_size = (max(1, int(img.size[0] * sc)),
                            max(1, int(img.size[1] * sc)))
                img = img.resize(new_size, Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="PNG")
                raw = buf.getvalue()
        except Exception:
            pass  # fall back to the full-res PNG if PIL/resizing fails
        b64 = base64.b64encode(raw).decode("ascii")
        # Return ONLY the image block — minimal token cost.
        return {"content": [{"type": "image", "data": b64, "mimeType": "image/png"}]}
    except Exception as e:
        return {"isError": True, "content": [{"type": "text",
                "text": json.dumps({"error": str(e)})}]}


# ---------------------------------------------------------------------------
# MCP protocol (hand-rolled JSON-RPC over stdio)
# ---------------------------------------------------------------------------
TOOLS = [
    {"name": "list_windows", "description": "List visible X11 windows (title + window_id).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "focus_window", "description": "Raise + focus a window by title substring; returns window_id.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "screenshot", "description": "Capture an image. window_name='primary' captures the PRIMARY monitor only (downscaled, fast, single-monitor — returns image + a meta block with coordinate scale). Other window_name captures that window (window-local coords). No arg = focused window. Returns image only (cheap, no text). Use 'primary' for general desktop control.",
     "inputSchema": {"type": "object", "properties": {"window_name": {"type": "string"}}}},
    {"name": "press_key", "description": "Send ONE key (or chord like 'ctrl+c'). For mGBA this drives the emulator's GBA buttons via its internal Lua socket (reliable). For ALL other windows it uses ydotool REAL kernel input (works on any app, incl. flatpak/sandboxed). Keys: letters a-z, digits, return/enter, backspace, tab, space, up/down/left/right, escape, ctrl+..., shift+..., alt+....",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "window_id": {"type": "integer"}, "name": {"type": "string", "default": "mGBA"}}}},
    {"name": "mgba_press", "description": "Press a GBA button INSIDE mGBA via the emulator's Lua socket (emu:setKeys). Most reliable input for mGBA. button: A/B/START/SELECT/UP/DOWN/LEFT/RIGHT. action: PRESS (tap) | HOLD | REL.",
     "inputSchema": {"type": "object", "properties": {
         "button": {"type": "string"}, "action": {"type": "string", "default": "PRESS"}}}},
    {"name": "type_text", "description": "Type a string into a window via ydotool real input (works on any window incl. flatpak). Not meaningful for mGBA (game uses buttons).",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "click", "description": "Move real cursor to SCREEN (x,y) and click via xdotool (correct global multi-monitor coords; mouse is not the rejected-by-mGBA case). button: left/right/middle. Keyboard uses ydotool.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "default": "left"}, "count": {"type": "integer", "default": 1},
         "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "drag", "description": "Drag from SCREEN (x1,y1) to (x2,y2) via xdotool (correct global multi-monitor coords).",
     "inputSchema": {"type": "object", "properties": {
         "x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"},
         "button": {"type": "string", "default": "left"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "scroll", "description": "Scroll at SCREEN (x,y) via xdotool. direction: up/down/left/right.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}, "direction": {"type": "string", "default": "up"},
         "amount": {"type": "integer", "default": 3}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "mouse_move", "description": "Move the real cursor to coords from a screenshot; translated to global. name=\"primary\" uses the downscaled primary-monitor screenshot coords.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
]


def _dispatch(method, params, req_id):
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "xdotool-mcp", "version": "1.0.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        try:
            if name == "list_windows":
                res = list_windows()
            elif name == "focus_window":
                res = focus_window(args.get("name", ""))
            elif name == "press_key":
                res = press_key(args.get("key", ""), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "mgba_press":
                res = mgba_press(args.get("button", ""), args.get("action", "PRESS"))
            elif name == "type_text":
                res = type_text(args.get("text", ""), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "click":
                res = click(args.get("x", 0), args.get("y", 0), args.get("button", "left"),
                            args.get("count", 1), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "drag":
                res = drag(args.get("x1", 0), args.get("y1", 0), args.get("x2", 0), args.get("y2", 0),
                           args.get("button", "left"), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "scroll":
                res = scroll(args.get("x", 0), args.get("y", 0), args.get("direction", "up"),
                             args.get("amount", 3), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "mouse_move":
                res = mouse_move(args.get("x", 0), args.get("y", 0), args.get("window_id"), args.get("name", "mGBA"))
            elif name == "screenshot":
                res = screenshot(args.get("window_name"))
            else:
                res = {"error": f"unknown tool {name}"}
        except Exception as e:
            res = {"error": str(e)}
        # screenshot() returns an already-formed MCP content block
        # ({"content":[{"type":"image",...}]}); pass it through verbatim so the
        # image reaches the model. All other tools return a plain dict that we
        # serialize as a text block.
        if isinstance(res, dict) and "content" in res:
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(res)}]}}
    # notifications (initialized, etc.) -> no response
    return None


def main():
    if not _has_xdotool():
        print("ERROR: xdotool not found on PATH", file=sys.stderr)
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {}) or {}
        # notifications have no id -> no response
        if req_id is None:
            continue
        resp = _dispatch(method, params, req_id)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
