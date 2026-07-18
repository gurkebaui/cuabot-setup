# cuabot-setup

Backup of Henry's desktop-control game agent ("cuabot") built on Hermes Agent
+ cua-driver + a hand-rolled xdotool MCP server.

## What this is
A Hermes **profile** (`cuabot`) that drives the desktop via real X11 input
(xdotool) through an MCP server, while using cua-driver ONLY for screenshots.
Built to play Pokemon Emerald in mGBA, but general: any X11 window, sandboxed
(flatpak/snap) or not.

## Why xdotool instead of cua-driver input
cua-driver routes input by **pid**. Flatpak's `bwrap` sandbox reports pid=2
(the namespace wrapper), which owns no X window, so keystrokes are silently
dropped on sandboxed apps (mGBA, Discord, ...). xdotool sends XSendEvent to a
real `window_id` (or globally to the focused window) and always lands.

## Key fix (the #1 gotcha)
mGBA (SDL/Qt) GRABS the keyboard and ignores synthetic keys sent to a specific
window (`xdotool key --window WID` -> silently dropped). Fix:
1. `focus_window` raises the window AND does a real `mousemove`+`click` at its
   center -> gives the app TRUE X keyboard focus the grab honors.
2. `press_key` / `type_text` send GLOBALLY (`xdotool key X`, no `--window`) ->
   routes through the normal focus path to the frontmost window.
Without step 1, keys do nothing. Mouse via `--window` works regardless.

## Files
- `hermes_patch/cua_backend.py`     Patched cua-driver backend. Copy over
                                    `~/.hermes/hermes-agent/tools/computer_use/cua_backend.py`
                                    Adds: foreground mode (raise + agent-cursor
                                    overlay), xdotool fallback in key(),
                                    z-order DESC sort. (The xdotool MCP is the
                                    primary input path; this patch is a secondary
                                    safety net + enables foreground visibility.)
- `profile/config.yaml`             cuabot profile config. Copy to
                                    `~/.hermes/profiles/cuabot/config.yaml`
                                    NOTE: toolsets = [hermes-cli, mcp:xdotool]
                                    — computer_use is DELIBERATELY removed (the
                                    screenshot tool below replaces its capture).
- `profile/SOUL.md`                 cuabot system prompt (strict tool-use rules).
                                    Copy to `~/.hermes/profiles/cuabot/SOUL.md`
- `skill/xdotool_mcp.py`            The MCP server. Copy to
                                    `~/.hermes/skills/desktop-control-xdotool/xdotool_mcp.py`
- `skill/SKILL.md`                  Skill doc.

## Screenshot cost fix (why computer_use is removed)
`computer_use capture` returns a multimodal block with a ~1.5k-token SOM/AX
summary TEXT per capture — that was the slow/costly part. The MCP server has a
`screenshot(window_name=)` tool that captures the window via `scrot -w WID` and
returns ONLY an image block (no text) -> ~0 text tokens per screenshot. The
model receives the image directly. Verified: model sees + describes the mGBA
screen from the MCP screenshot tool.

Window-name resolution in `_find_window` returns an int window_id; `screenshot`
handles both int and str. `scrot` refuses to overwrite existing files, so the
temp path is removed before capture.

## Setup
1. Copy the four files above to their destinations.
2. Ensure xdotool is installed: `which xdotool` (else `sudo dnf install xdotool`).
3. Load a >=9B local model in LM Studio (gemma-4-12b-qat proven). Config points
   at `custom:lmstudio`, `gemma-4-12b-qat`, context 90000.
4. Launch mGBA with software GL so it's capturable:
   `flatpak run --env=QT_OPENGL=software --env=LIBGL_ALWAYS_SOFTWARE=1 \
    io.mgba.mGBA <rom>`
5. Run: `cd <agent dir> && cuabot`

## Game Boy keymap (mGBA)
A=x, B=y, START=return, SELECT=backspace, dpad=up/down/left/right.

## Rollback
If cua_backend.py breaks after a Hermes update, restore the original from the
hermes-agent repo (`git checkout` in `~/.hermes/hermes-agent`) — the xdotool MCP
is independent of the backend patch and keeps working.
