# cuabot-setup

Backup of Henry's desktop-control game agent ("cuabot") built on Hermes Agent
+ a hand-rolled MCP server that drives mGBA's GBA input from INSIDE the emulator.

## The core idea (and why it's robust)
To play a GAME in an emulator, you don't need OS-level input injection at all.
mGBA 0.10.x ships **Lua scripting** (liblua + LuaSocket). A small Lua script
(`mgba_agent.lua`) runs INSIDE mGBA, opens a TCP socket on `127.0.0.1:8930`, and
drives the GBA buttons via mGBA's own `emu:setKeys(bitmask)` API.

The MCP server's `press_key(name="mGBA")` connects to that socket and sends the
button. This is:
- **Not** xdotool (synthetic X keys are REJECTED by mGBA's SDL/Qt input grab).
- **Not** ydotool (needs a kernel uinput bridge to X that wasn't wired up).
- **Not** dependent on window focus. Input goes straight into the emulator.
=> It just works, every time.

## Why the previous approaches failed (lessons learned)
- cua-driver routes input by **pid**; flatpak's bwrap sandbox reports pid=2, so
  keystrokes are silently dropped on mGBA. Dead end.
- xdotool synthetic keys: PROVEN rejected by mGBA even with correct focus and
  correct key bindings (Down/x/Return/raw keycode all did nothing). Dead end.
- ydotool (kernel uinput): rc=0 but X11 never attached the virtual device
  (no xinput/evdev seat bridge), so no app received events. Blocked without
  sudo + X plumbing. Dead end.
- Emulator-internal Lua socket: inputs the emulator's own button state. The
  only approach that can't be rejected. THIS is the one.

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
