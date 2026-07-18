---
name: desktop-control-xdotool
description: "Use when an agent must control the desktop (click, type, keypress, drag, scroll) on an X11 Linux machine — especially sandboxed apps (flatpak/snap: mGBA, Discord, etc.) where Hermes computer_use's built-in input silently fails. Gives the model DIRECT xdotool tools via an MCP server; computer_use is used ONLY for screenshots. Reliable, app-agnostic input — no per-flatpak reconfiguration."
---

# Desktop control via xdotool MCP

## The rule
- **INPUT (mouse/keyboard): use the `xdotool` MCP tools.** These send real X11
  input to a window by its `window_id` and work through flatpak/snap sandboxes.
- **SCREENSHOTS / window info: use `computer_use` capture.** cua-driver's capture
  works fine; only its input delivery is unreliable for sandboxed apps.

Do NOT use computer_use `click` / `key` / `type_text` / `drag` / `scroll` for
actual input — on sandboxed apps those keystrokes are silently dropped (the app
reports pid=2 under bwrap, which owns no X window). Use the xdotool MCP tools
instead. See also `computer-use-cua-driver-flatpak-fix` for the root-cause diagnosis.

## Tools available (xdotool MCP)
- `mcp_xdotool_list_windows()` → visible windows with `title` + `window_id`.
- `mcp_xdotool_focus_window(name)` → raise + focus a window by title substring; returns `window_id`.
- `mcp_xdotool_press_key(key, window_id= or name=)` → one key. Names: `return`, `x`, `y`, `up`, `down`, `left`, `right`, `backspace`, `escape`, `tab`. Combos: `ctrl+c`.
- `mcp_xdotool_type_text(text, window_id= or name=)` → type text.
- `mcp_xdotool_click(x, y, button="left", count=1, window_id= or name=)` → move REAL cursor + click.
- `mcp_xdotool_drag(x1, y1, x2, y2, button="left", window_id= or name=)`.
- `mcp_xdotool_scroll(x, y, direction="up", amount=3, window_id= or name=)`.
- `mcp_xdotool_mouse_move(x, y, window_id= or name=)`.

Coordinates are absolute screen pixels (3 monitors = one ~6400×1440 space).
Every tool auto-focuses the target window first: `focus_window` raises it AND
does a real `mousemove`+`click` at its center (see "keyboard needs REAL focus"
below — the click is what grants keyboard focus to SDL/Qt apps).

## Loop (e.g. playing a game)
1. CALL `mcp_xdotool_focus_window(name="mGBA")` to raise the game FIRST.
2. CALL `computer_use capture(mode="vision", app="mGBA")` to SEE the screen.
   (KEYWORD args — see "Screenshots" below for the positional-swap trap.)
3. Decide ONE action. CALL `mcp_xdotool_press_key(key="return", name="mGBA")`
   (or click/drag/scroll). One action per step.
4. `capture` again to confirm the screen changed.

## CRITICAL: keyboard needs REAL focus (the #1 failure mode)
mGBA (SDL/Qt) GRABS the keyboard and IGNORES synthetic keys sent to a specific
window (`xdotool key --window WID` → silently dropped, 0% screen change). Mouse
events (`click --window`) DO arrive. Fix that works:
1. focus_window(name=...) must RAISE the window AND do a real `mousemove`+`click`
   at its center — that gives the app true X keyboard focus the grab honors.
2. press_key / type_text send GLOBALLY (`xdotool key X`, NO `--window`) so the key
   routes through the normal focus path to the frontmost window.
This was the bug that made "press enter/x does nothing" — sending to a window_id
instead of the OS focus. Global key + real-click-focus = 77% screen change.

## Screenshots (computer_use)
`computer_use capture(mode="vision", app="mGBA")` — KEYWORD args only.
`capture("mGBA", "vision")` positionally SWAPS to mode="mGBA", app="vision"
and fails ("no on-screen window matched app='vision'"). mGBA's app_name is
literally "mGBA" so the filter works; the failure mode is almost always the
swapped positional call, not a missing window.

## MCP server wiring (PITFALLS — learned the hard way)
The server is `xdotool_mcp.py` at the skill root:
`~/.hermes/skills/desktop-control-xdotool/xdotool_mcp.py` (NOT under `scripts/`).
Register in the profile `config.yaml`:
```yaml
toolsets:
  - hermes-cli
  - computer_use
  - mcp:xdotool
mcp_servers:
  xdotool:
    command: python3
    args: ["/home/henry/.hermes/skills/desktop-control-xdotool/xdotool_mcp.py"]
    env:
      DISPLAY: ":0"
```
- **`env: {DISPLAY: ":0"}` is MANDATORY.** Hermes spawns the MCP server in an
  environment where `$DISPLAY` is STRIPPED. Without it, `xdotool` sees 0 windows
  and `list_windows` returns `[]` — the agent reports "no mGBA window" and gives
  up, even though the window is clearly open. The server also does
  `os.environ.setdefault("DISPLAY", ":0")` as a belt-and-suspenders, but set the
  config env too so it's explicit. Verified: with DISPLAY stripped, 0 windows;
  with DISPLAY=:0, 55 windows.
- **DO NOT pass `--toolsets mcp:xdotool` to `cuabot -z`.** The CLI `--toolsets`
  flag only accepts BUILT-IN toolsets; it rejects `mcp:xdotool` ("ignoring
  unknown --toolsets entries"). MCP servers load from `mcp_servers` in config +
  the `toolsets: [mcp:xdotool]` line — never from the `-z` flag.
- **The `mcp` Python SDK's low-level `Server.run(read, write, ...)` HANGS on
  `tools/list`** (the agent never sees the tools). If you use the SDK, prefer
  `FastMCP`, or — bulletproof — hand-roll the JSON-RPC stdio server (no SDK
  dependency). The shipped `xdotool_mcp.py` is hand-rolled: it reads
  newline-delimited JSON from stdin, dispatches `initialize` / `tools/list` /
  `tools/call`, and writes responses to stdout. This is the version that WORKS.
- Verify the server before trusting it: `python3 test_mcp.py` (in the skill dir)
  should print the 8 tool names + a real `list_windows` result. If Hermes reports
  "no MCP servers connected", the server process is crashing on spawn — check it
  starts standalone first.

## Model-size warning
A ~2B model (qwen3.5-2b-mtp) CANNOT reliably orchestrate 8 MCP tools + screenshots:
it types tool names as literal text ("_xdotool_list_windows") and emits malformed
`<function=computer_use>` calls with no args. Use a >=9B local model (e.g.
gemma-4-12b-qat) for actual gameplay. The xdotool MCP + capture pipeline is proven;
the bottleneck is model tool-calling competence, not the plumbing.

## mGBA launch (software GL so it's capturable)
`flatpak run --env=QT_OPENGL=software --env=LIBGL_ALWAYS_SOFTWARE=1 io.mgba.mGBA <rom>`

## Support files
- `xdotool_mcp.py` — the working hand-rolled MCP server (proven). At skill root,
  NOT under `scripts/`.
- `references/setup-and-gotchas.md` — full reproduction recipe, param-order trap,
  MCP registration gotcha, DISPLAY-strip pitfall, model-size note, pixel-diff probe.
- Backup repo (rollback): `gurkebaui/cuabot-setup` on GitHub — full
  `cua_backend.py` patch, cuabot profile (config+SOUL.md), and this server + README.
