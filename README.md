# cuabot-setup

Backup of Henry's desktop-control game agent ("cuabot") built on Hermes Agent
+ a hand-rolled MCP server that drives mGBA's GBA input from INSIDE the emulator.

## The core idea (and why it's robust)
Three input paths, each chosen for what it does best:

1. **mGBA game buttons → emulator-internal Lua socket.** `mgba_agent.lua`
   runs INSIDE mGBA, opens a TCP socket on `127.0.0.1:8930`, and drives the GBA
   buttons via `emu:setKeys(bitmask)`. No OS input at all — can't be rejected.
   This is the only path that reliably controls mGBA (xdotool/ydotool synthetic
   keys are rejected by mGBA's SDL/Qt input grab).

2. **Keyboard on ANY other window → ydotool (kernel uinput).** ydotool injects
   real input events at the kernel level, so SDL/Qt grabs CANNOT reject them
   (unlike xdotool). Works on flatpak/sandboxed apps too. Requires `ydotoold`
   running and `/dev/uinput` accessible (henry in `input` group). After
   installing, `sudo udevadm trigger` makes X bind the virtual device.

3. **Mouse on ANY window → xdotool (global coords).** xdotool `mousemove` uses
   correct GLOBAL multi-monitor coordinates. The screenshot tool returns
   WINDOW-LOCAL pixels, so the MCP server translates local→global via the
   window geometry before moving. (ydotool absolute coords are pinned to one
   monitor with an offset, so xdotool is used for the pointer; ydotool is
   reserved for keyboard.)

## Why the previous approaches failed (lessons learned)
- cua-driver routes input by **pid**; flatpak's bwrap sandbox reports pid=2, so
  keystrokes are silently dropped on mGBA. Dead end.
- xdotool synthetic keys: PROVEN rejected by mGBA even with correct focus and
  correct key bindings. Dead end for the game.
- ydotool (kernel uinput) for the GAME: needs X to bind the virtual device;
  works for keyboard once `udevadm trigger` is done, but its absolute mouse
  coords are monitor-pinned — so mouse uses xdotool instead.

## Known issues
- **mGBA d-pad LEFT/RIGHT are inverted** in `mgba_agent.lua` — swap bits 4/5
  (LEFT/RIGHT) in the button mapping to fix. Low priority (game mostly needs
  A/START/dpad-up/down).

## Files
- `skill/mgba_agent.lua`           The Lua control server. Load ONCE into mGBA
                                    (Tools -> Scripting -> Load). It then
                                    auto-loads on every future launch
                                    (mGBA `autoload=1`).
- `skill/xdotool_mcp.py`           The MCP server. `press_key(name="mGBA")`
                                    routes to the socket; other windows fall
                                    back to xdotool/ydotool. Also has a
                                    `screenshot(window_name=)` tool (image-only,
                                    ~0 tokens). Copy to
                                    `~/.hermes/skills/desktop-control-xdotool/xdotool_mcp.py`
- `profile/config.yaml`            cuabot profile. toolsets = [hermes-cli,
                                    mcp:xdotool]; computer_use removed.
                                    Copy to `~/.hermes/profiles/cuabot/config.yaml`
- `profile/SOUL.md`                cuabot system prompt.
- `hermes_patch/cua_backend.py`    Patched cua-driver backend (secondary safety
                                    net; foreground visibility). Optional.
- `skill/SKILL.md`                 Skill doc.

## Setup (one-time)
1. Copy files: `skill/xdotool_mcp.py` -> `~/.hermes/skills/desktop-control-xdotool/`,
   `profile/config.yaml` + `profile/SOUL.md` -> `~/.hermes/profiles/cuabot/`,
   `skill/mgba_agent.lua` -> somewhere mGBA's file dialog can reach (e.g.
   `/home/henry/Documents/agent/mgba_agent.lua`).
2. Install xdotool (for screenshots + non-game input):
   `sudo dnf install -y xdotool scrot`
3. Load a local model in LM Studio (gemma-4-12b-qat proven; qwen3.5-2b-mtp also
   works but is less reliable at multi-tool orchestration). Config -> `gemma-4-12b-qat`.
4. Launch mGBA with software GL (so it's capturable) AND load the script:
   ```
   flatpak run --env=QT_OPENGL=software --env=LIBGL_ALWAYS_SOFTWARE=1 \
     io.mgba.mGBA "<rom>"
   ```
   Then in mGBA: **Tools -> Scripting -> Load**, navigate to `mgba_agent.lua`,
   open it. The scripting console should print:
   `mgba_agent: listening on 127.0.0.1:8930`
   => Done ONCE. `autoload=1` reloads it on every future mGBA launch.
5. Run the agent: `cd <agent dir> && cuabot`

## Verifying the socket (sanity check, no model needed)
```
python3 -c "import socket;s=socket.socket();s.connect(('127.0.0.1',8930));s.sendall(b'STATUS\n');import time;time.sleep(0.3);print(s.recv(256))"
```
Should print `OK held=... frame=...`. If connection refused, the script isn't
loaded in mGBA (repeat step 4).

## Game Boy button mapping (sent to the emulator, not the OS)
A=A button, B=B button, START=Start, SELECT=Select, UP/DOWN/LEFT/RIGHT=d-pad.
In mGBA's key config these are A=x, B=y, START=Return, SELECT=Backspace — but
you send BUTTONS, so the OS keymap is irrelevant.

## How the model drives the game
1. `mcp_xdotool_screenshot(window_name="mGBA")` -> look at the screen.
2. `mcp_xdotool_press_key(key="a", name="mGBA")` -> press A (no focus needed).
3. Wait ~1s, screenshot again, confirm change.
4. Repeat, one button at a time.

## Rollback
Everything is in this repo. If Hermes updates and breaks something, restore the
specific file. The MCP server is independent of Hermes internals.
