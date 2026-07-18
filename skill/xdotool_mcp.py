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
        # not a GBA button name; fall back to OS input for mGBA's own menus etc.
    # non-mGBA or unknown button: OS input (ydotool if available, else xdotool)
    wid = _resolve_wid(window_id, name)
    if shutil.which("ydotool") and _mgba_socket_up() is False:
        if wid:
            ok, err = _run(["ydotool", "key", _keysym(key)])
        else:
            ok, err = _run(["ydotool", "key", _keysym(key)])
        if ok:
            return {"ok": ok, "key": key, "method": "ydotool", "error": err}
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
    """Type text. Atomic focus+type in one command (see press_key)."""
    wid = _resolve_wid(window_id, name)
    if wid:
        ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                        "type", "--delay", "0", text])
    else:
        ok, err = _run(["xdotool", "type", "--delay", "0", text])
    return {"ok": ok, "chars": len(text), "error": err}


def click(x, y, button="left", count=1, window_id=None, name="mGBA"):
    """Click at SCREEN coords. Atomic: focus + move + click in one command so the
    click always lands in the intended window. `count` repeats the click."""
    btn = _btn(button)
    wid = _resolve_wid(window_id, name)
    if wid:
        cmd = ["xdotool", "windowactivate", "--sync", str(wid),
               "mousemove", str(x), str(y)]
    else:
        cmd = ["xdotool", "mousemove", str(x), str(y)]
    for _ in range(max(1, count)):
        ok, err = _run(cmd + ["click", btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "button": button}


def drag(x1, y1, x2, y2, button="left", window_id=None, name="mGBA"):
    """Drag between SCREEN coords. Atomic focus+move+down+move+up in one command."""
    btn = _btn(button)
    wid = _resolve_wid(window_id, name)
    steps = max(2, min(20, abs(x2 - x1) + abs(y2 - y1) // 20))
    cmd = ["xdotool"]
    if wid:
        cmd += ["windowactivate", "--sync", str(wid)]
    cmd += ["mousemove", str(x1), str(y1), "mousedown", btn]
    for i in range(1, steps + 1):
        ix = x1 + (x2 - x1) * i // steps
        iy = y1 + (y2 - y1) * i // steps
        cmd += ["mousemove", str(ix), str(iy)]
    cmd += ["mouseup", btn]
    ok, err = _run(cmd)
    return {"ok": ok, "from": [x1, y1], "to": [x2, y2], "error": err}


def scroll(x, y, direction="up", amount=3, window_id=None, name="mGBA"):
    """Scroll at SCREEN coords. Atomic focus+move+click(repeat) in one command."""
    btn = "4" if (direction or "").lower() in ("up", "left") else "5"
    wid = _resolve_wid(window_id, name)
    for _ in range(max(1, min(50, amount))):
        if wid:
            ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                            "mousemove", str(x), str(y), "click", btn])
        else:
            ok, err = _run(["xdotool", "mousemove", str(x), str(y), "click", btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "direction": direction, "amount": amount}


def mouse_move(x, y, window_id=None, name="mGBA"):
    """Move the real cursor to SCREEN coords. Atomic focus+move in one command."""
    wid = _resolve_wid(window_id, name)
    if wid:
        ok, err = _run(["xdotool", "windowactivate", "--sync", str(wid),
                        "mousemove", str(x), str(y)])
    else:
        ok, err = _run(["xdotool", "mousemove", str(x), str(y)])
    return {"ok": ok, "x": x, "y": y, "error": err}


def screenshot(window_name=None):
    """Capture a window to PNG and return it as an MCP image block.

    Returns ONLY the image (no SOM/AX summary text) so the model pays ~0 text
    tokens per screenshot — the whole point of moving capture off cua-driver,
    whose capture attaches a ~1.5k-token 'SOM index + summary' text block.

    If window_name is given, capture that window; otherwise capture the
    currently-focused window.
    """
    import base64
    wid = None
    if window_name:
        wid, _ = _find_window(window_name)
    if wid is None:
        # frontmost focused window
        try:
            wid = subprocess.run(["xdotool", "getwindowfocus"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            wid = None
    if not wid or (isinstance(wid, str) and not wid.isdigit()):
        return {"isError": True, "content": [{"type": "text",
                "text": json.dumps({"error": "no window to capture"})}]}
    wid = int(wid)
    try:
        import tempfile, os
        fd, tmppath = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        # scrot refuses to overwrite an existing file, so remove the empty
        # placeholder mkstemp created and let scrot create it fresh.
        os.remove(tmppath)
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
        # and blows the model context. Cap the longest side (default 640 ~
        # "480p is plenty" for a game) so the image is tiny in context.
        try:
            from io import BytesIO
            from PIL import Image
            img = Image.open(BytesIO(raw))
            max_side = 854
            if max(img.size) > max_side:
                scale = max_side / float(max(img.size))
                new_size = (max(1, int(img.size[0] * scale)),
                            max(1, int(img.size[1] * scale)))
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
    {"name": "press_key", "description": "Send ONE key. For mGBA this drives the emulator's GBA buttons via its internal Lua socket (reliable). Keys: a/x (A button), b/y (B), return/start (START), backspace (SELECT), up/down/left/right (dpad). Also works as global xdotool/ydotool for other apps.",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "window_id": {"type": "integer"}, "name": {"type": "string", "default": "mGBA"}}}},
    {"name": "mgba_press", "description": "Press a GBA button INSIDE mGBA via the emulator's Lua socket (emu:setKeys). Most reliable input for mGBA. button: A/B/START/SELECT/UP/DOWN/LEFT/RIGHT. action: PRESS (tap) | HOLD | REL.",
     "inputSchema": {"type": "object", "properties": {
         "button": {"type": "string"}, "action": {"type": "string", "default": "PRESS"}}}},
    {"name": "type_text", "description": "Type text into a window via xdotool.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "click", "description": "Move real cursor to (x,y) and click. button: left/right/middle.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "default": "left"}, "count": {"type": "integer", "default": 1},
         "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "drag", "description": "Drag from (x1,y1) to (x2,y2).",
     "inputSchema": {"type": "object", "properties": {
         "x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"},
         "button": {"type": "string", "default": "left"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "scroll", "description": "Scroll at (x,y). direction: up/down/left/right.",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}, "direction": {"type": "string", "default": "up"},
         "amount": {"type": "integer", "default": 3}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "mouse_move", "description": "Move the real cursor to (x,y).",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
    {"name": "screenshot", "description": "Capture a window to an image (returns ONLY the image, no text — cheap). window_name: capture that window (e.g. 'mGBA'); if omitted, capture the focused window. Use this instead of computer_use capture to save tokens.",
     "inputSchema": {"type": "object", "properties": {
         "window_name": {"type": "string"}}}},
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
