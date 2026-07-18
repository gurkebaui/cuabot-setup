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
    if wid:
        # Raise the window AND give it true X keyboard focus by clicking its
        # center. `windowactivate` alone is ignored by SDL/Qt apps that grab the
        # keyboard; a real (synthetic) click establishes focus the grab honors.
        # xdotool's `key`/`type` are then sent globally (no --window) so they
        # route through the normal focus path to the frontmost window.
        subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)],
                       capture_output=True, text=True, timeout=10)
        geom = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(wid)],
                              capture_output=True, text=True, timeout=10).stdout
        cx = cy = 0
        for line in geom.splitlines():
            if line.startswith("X="):
                cx = int(line.split("=")[1])
            elif line.startswith("Y="):
                cy = int(line.split("=")[1])
            elif line.startswith("WIDTH="):
                cx += int(line.split("=")[1]) // 2
            elif line.startswith("HEIGHT="):
                cy += int(line.split("=")[1]) // 2
        if cx > 0 and cy > 0:
            subprocess.run(["xdotool", "mousemove", str(cx), str(cy)],
                           capture_output=True, text=True, timeout=10)
            subprocess.run(["xdotool", "click", "1"],
                           capture_output=True, text=True, timeout=10)


def _find_window(name: str):
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name],
                              capture_output=True, text=True, timeout=10)
        wids = [w for w in out.stdout.split() if w.strip().isdigit()]
        for wid in wids:
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            if name.lower() in t.lower():
                return int(wid), t
        if wids:
            wid = wids[0]
            t = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            return int(wid), t
    except Exception:
        pass
    return None, None


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
    wid, title = _find_window(name)
    if wid is None:
        return {"ok": False, "error": f"no window matching '{name}'"}
    _activate(wid)
    return {"ok": True, "window_id": wid, "title": title}


def press_key(key, window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    # Send GLOBALLY (no --window) so the key routes through the normal X focus
    # path to the frontmost window. Sending --window is ignored by SDL/Qt apps
    # that grab the keyboard (mGBA). A prior real click gave it true focus.
    ok, err = _run(["xdotool", "key", _keysym(key)])
    return {"ok": ok, "key": key, "window_id": window_id, "error": err}


def type_text(text, window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    ok, err = _run(["xdotool", "type", "--delay", "0", text])
    return {"ok": ok, "chars": len(text), "window_id": window_id, "error": err}


def click(x, y, button="left", count=1, window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    btn = _btn(button)
    _run(["xdotool", "mousemove", "--window", str(window_id), str(x), str(y)])
    for _ in range(max(1, count)):
        ok, err = _run(["xdotool", "click", "--window", str(window_id), btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "button": button, "window_id": window_id}


def drag(x1, y1, x2, y2, button="left", window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    btn = _btn(button)
    _run(["xdotool", "mousemove", "--window", str(window_id), str(x1), str(y1)])
    _run(["xdotool", "mousedown", "--window", str(window_id), btn])
    steps = max(2, min(20, abs(x2 - x1) + abs(y2 - y1) // 20))
    for i in range(1, steps + 1):
        ix = x1 + (x2 - x1) * i // steps
        iy = y1 + (y2 - y1) * i // steps
        _run(["xdotool", "mousemove", "--window", str(window_id), str(ix), str(iy)])
    _run(["xdotool", "mouseup", "--window", str(window_id), btn])
    return {"ok": True, "from": [x1, y1], "to": [x2, y2], "window_id": window_id}


def scroll(x, y, direction="up", amount=3, window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    btn = "4" if (direction or "").lower() in ("up", "left") else "5"
    _run(["xdotool", "mousemove", "--window", str(window_id), str(x), str(y)])
    for _ in range(max(1, min(50, amount))):
        ok, err = _run(["xdotool", "click", "--window", str(window_id), btn])
        if not ok:
            return {"ok": False, "error": err}
    return {"ok": True, "x": x, "y": y, "direction": direction,
            "amount": amount, "window_id": window_id}


def mouse_move(x, y, window_id=None, name=None):
    if window_id is None and name:
        window_id, _ = _find_window(name)
    if window_id is None:
        return {"ok": False, "error": "no window_id or matching name"}
    _activate(window_id)
    ok, err = _run(["xdotool", "mousemove", "--window", str(window_id), str(x), str(y)])
    return {"ok": ok, "x": x, "y": y, "window_id": window_id, "error": err}


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
    {"name": "press_key", "description": "Send ONE key via xdotool. Keys: return, x, y, up, down, left, right, backspace, escape, tab, or 'ctrl+c'.",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "window_id": {"type": "integer"}, "name": {"type": "string"}}}},
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
                res = press_key(args.get("key", ""), args.get("window_id"), args.get("name"))
            elif name == "type_text":
                res = type_text(args.get("text", ""), args.get("window_id"), args.get("name"))
            elif name == "click":
                res = click(args.get("x", 0), args.get("y", 0), args.get("button", "left"),
                            args.get("count", 1), args.get("window_id"), args.get("name"))
            elif name == "drag":
                res = drag(args.get("x1", 0), args.get("y1", 0), args.get("x2", 0), args.get("y2", 0),
                           args.get("button", "left"), args.get("window_id"), args.get("name"))
            elif name == "scroll":
                res = scroll(args.get("x", 0), args.get("y", 0), args.get("direction", "up"),
                             args.get("amount", 3), args.get("window_id"), args.get("name"))
            elif name == "mouse_move":
                res = mouse_move(args.get("x", 0), args.get("y", 0), args.get("window_id"), args.get("name"))
            elif name == "screenshot":
                res = screenshot(args.get("window_name"))
            else:
                res = {"error": f"unknown tool {name}"}
        except Exception as e:
            res = {"error": str(e)}
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
