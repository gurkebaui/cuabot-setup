---
name: desktop-control-xdotool
description: "Use when an agent must control the desktop (click, type, keypress, drag, scroll, AND cheap screenshots) on an X11 Linux machine — especially sandboxed apps (flatpak/snap: mGBA, Discord, etc.) where Hermes computer_use's built-in input silently fails. Gives the model DIRECT xdotool tools via a hand-rolled MCP server (input + screenshot). computer_use is NOT used — its capture attaches a ~1.5k-token SOM/AX text block per shot, which is the costly path this replaces. Reliable, app-agnostic input — no per-flatpak reconfiguration."
---

# Desktop control via xdotool MCP

## The rule
- **INPUT (mouse/keyboard) AND SCREENSHOTS: use the `xdotool` MCP tools.** They
  send real X11 input to a window and capture windows as images — all working
  through flatpak/snap sandboxes.
- **Do NOT use `computer_use` at all for this agent.** Its `capture` attaches a
  ~1.5k-token SOM/AX summary TEXT block per screenshot (the slow/costly part).
  The MCP `screenshot` tool returns ONLY an image (~0 text tokens). Input via
  cua-driver is also unreliable for sandboxed apps (pid=2 bwrap drop). See
  `computer-use-cua-driver-flatpak-fix` for the root-cause diagnosis.

## PITFALL: WRONG-TARGET CLICKS (the "clicked top-right" bug)
If the model says "I told it to click bottom-middle but it clicked top-right":
- ROOT CAUSE (2026-07-19): the mouse functions (`click`/`drag`/`scroll`/
  `mouse_move`) defaulted `name="mGBA"`. So `click(x, y)` with NO name (after the
  model looked at `screenshot(window_name="primary")`) defaulted to `name="mGBA"`
  and mapped the coords via the mGBA WINDOW geometry, not the screenshot it just
  saw → click landed in the wrong place. This is NOT a multi-monitor math error;
  it's the wrong coordinate FRAME being selected by the default.
- FIX (shipped): mouse functions now default `name=None`. The server CACHES the
  coordinate map from the most recent screenshot into `STATE["map"]`
  (`{"ox","oy","sx","sy"}`, `global = ox + x*sx, oy + y*sy`). `_map_click` priority:
  (1) `name=="primary"` → `_from_primary`; (2) name resolves to a real window →
  `_to_global`; (3) else → `STATE["map"]` (the last screenshot's frame). So after
  ANY screenshot (primary / window / crop-around-cursor), `click(x, y)` with the
  coords the model READ from the last image auto-maps correctly — NO name, NO math.
- CRITICAL: `STATE["map"]` persists only WITHIN one persistent MCP process.
  Hermes keeps the MCP server alive across turns, so it works in a real run. But
  if you spawn a FRESH `python3 xdotool_mcp.py` per call in a test, the cache is
  lost and `click` returns RAW coords (looks broken, isn't). Test cached mapping
  by making screenshot + click in the SAME subprocess (see `scripts/test_persist_click.py`).
- For mGBA KEYBOARD: `press_key` keeps `name="mGBA"` default (game buttons); that
  is intentional and correct. But for DESKTOP TYPING the model MUST name the app
  (e.g. `name="kwrite"`) or keys go to the game. Do NOT tell the model "just pass
  name='<window>'" for CLICKS — that reintroduces the bug; clicks use the cached
  last-screenshot map automatically.
- VERIFY (persistent process): primary screenshot → `click(427,460)` → global
  (3200,1378) = bottom-middle of primary. MATCH. Crop → `click(200,200)` → lands
  exactly on cursor. MATCH.

## PITFALL: "keys do nothing" on mGBA vs general apps — do NOT conflate
- mGBA KEYBOARD: synthetic keys REJECTED by mGBA regardless of focus (see CRITICAL
  section). PRIMARY fix = emulator-internal Lua socket (`emu:setKeys`). Defaulting
  `press_key` name to mGBA routes there. The "77% screen change" earlier was a
  FALSE POSITIVE (mGBA auto-advanced a cutscene).
- GENERAL desktop apps: ydotool keyboard works (kernel uinput). For TYPING the
  model must pass `name="<app>"` (default is mGBA). For CLICKS, no name needed
  (cached last-screenshot map, see WRONG-TARGET CLICKS above).
VERIFY (deterministic probe): `press_key(key="x", name="kwrite")` with PIXEL-DIFF
before/after; for mGBA use `press_key(key="a", name="mGBA")` (socket) instead.
Lesson for SOUL/prompt: do NOT tell the model "input is global, focus is optional"
— but ALSO do not claim "focus makes keys work on mGBA" — it does not.

## PITFALL: `focus_window` no-match must self-correct, not stall
If the model passes a non-exact title (e.g. `"~ : hermes — Konsole"`), an exact
`xdotool search --name` fails and the old code returned `"no window matching '...'"`
with no way forward — the model got STUCK. FIX (shipped): `_find_window` does a
**case-insensitive CONTAINS match** across all visible windows (so `"mGBA"` matches
`"mGBA - Pokemon - Emerald..."`), and `focus_window` on miss returns
`{"ok": false, "error": "...", "available_windows": [<titles>]}` so the model picks
the closest title and retries. Always pass a short substring, never the full title.

## Tools available (xdotool MCP)
- `mcp_xdotool_list_windows()` → visible windows with `title` + `window_id`.
- `mcp_xdotool_focus_window(name)` → raise + REAL-CLICK-focus a window by title
  substring; returns `window_id`. The click is what grants keyboard focus.
- `mcp_xdotool_press_key(key, window_id= or name=)` → one key. For mGBA, routes
  to the emulator-internal Lua socket (`emu:setKeys`) — RELIABLE, no OS focus.
  For general X11 apps, uses xdotool (works). Names: `return`, `x`, `y`, `up`,
  `down`, `left`, `right`, `backspace`, `escape`, `tab`. Combos: `ctrl+c`.
- `mcp_xdotool_mgba_press(button, action="PRESS")` → press a GBA BUTTON directly
  inside mGBA via the Lua socket. button: A/B/START/SELECT/UP/DOWN/LEFT/RIGHT.
  action: PRESS (tap) | HOLD | REL. Use this (or press_key with name="mGBA") to
  drive the game — it is the primary, reliable input path.
- `mcp_xdotool_click(x, y, button="left", count=1, window_id= or name=)` → move
  REAL cursor + click.
- `mcp_xdotool_drag(x1, y1, x2, y2, button="left", window_id= or name=)`.
- `mcp_xdotool_scroll(x, y, direction="up", amount=3, window_id= or name=)`.
- `mcp_xdotool_mouse_move(x, y, window_id= or name=)`.
- `mcp_xdotool_mouse_location()` → returns the real cursor's current global (x, y).
- `mcp_xdotool_screenshot(window_name=)` → capture the window to an IMAGE and
  return ONLY the image block (~0 text tokens). If `window_name` is omitted,
  captures the focused window.
- `mcp_xdotool_screenshot_around_cursor(radius=200, max_side=512)` → a SMALL
  high-res box centered on the real cursor (great for clicking small/tiny UI).
  Caches its coordinate map so `click(x, y)` inside the crop maps back exactly.
- `mcp_xdotool_screenshot(window_name="primary")` → PRIMARY monitor only
  (downscaled, single-monitor, accurate — see PRIMARY-MONITOR section).

**COORDINATES: read them from the screenshot, click them, NO name, NO math.**
After any screenshot (primary / window / crop), the server CACHES that image's
coordinate map (`STATE["map"]`). `click(x, y)` with the coords you READ from the
last image auto-maps to real screen pixels. Do NOT pass a window name for clicks
(passing `name="mGBA"` is the classic wrong-target bug). `name="primary"` only
forces the primary mapping if you switched monitors between shot and click.
**The screenshot meta returns `image_size: {w, h}` — the EXACT pixel size of the
image you received. READ coords ONLY within [0..w, 0..h]; do NOT use full-screen
pixel numbers (e.g. 1760,975 on an 854x480 image is invalid and will clamp to a
wrong spot). See the "8436 bug" pitfall below.**

## Loop (e.g. playing a game)
1. CALL `mcp_xdotool_focus_window(name="mGBA")` to raise the game FIRST.
2. CALL `mcp_xdotool_screenshot(window_name="mGBA")` to SEE the screen (image-only,
   cheap).
3. Decide ONE action. CALL `mcp_xdotool_press_key(key="return", name="mGBA")`
   (or click/drag/scroll). One action per step.
4. CALL `mcp_xdotool_screenshot` again to confirm the screen changed.
Repeat. One button at a time — do not spam.

## FINAL ARCHITECTURE — three input paths, each for what it does best
This is the working split (verified 2026-07-19), NOT "xdotool for everything":

1. **mGBA game buttons → emulator-internal Lua socket** (`emu:setKeys`). RELIABLE,
   no OS input, can't be rejected. PRIMARY path for gameplay. See
   `references/emulator-internal-input.md`.
2. **Keyboard on ANY other window → ydotool (kernel uinput).** ydotool injects
   REAL input events at the kernel level, so SDL/Qt grabs CANNOT reject them
   (unlike xdotool). Works on flatpak/sandboxed apps too. ydotool uses RAW Linux
   keycodes (e.g. `28:1 28:0` = Enter) — NOT xdotool key names. The MCP server
   translates key names → keycodes in `_LINUX_KEYCODE`. See
   `references/ydotool-fix.md` for the keycode map + setup.
3. **Mouse on ANY window → xdotool (global coords).** xdotool `mousemove` uses
   correct GLOBAL multi-monitor coordinates. The screenshot tool returns
   WINDOW-LOCAL pixels, so the MCP server translates local→global via the window
   geometry (`_to_global`) before moving. WHY not ydotool for mouse: ydotool
   absolute coords are PINNED to one monitor with a y-offset (observed:
   `ydotool mousemove -a 500 500` → global 4980,860 on a 3-monitor 6400×1440
   setup), so clicks miss monitors 1/2. xdotool handles global coords correctly.
   Also: `ydotool click` uses the YDOTOOL device's OWN pointer position (not the
   real cursor), so mixing xdotool-move + ydotool-click lands wrong. Keep mouse
   entirely on xdotool, keyboard entirely on ydotool.

## PITFALL — ydotool "rc=0 but no app receives" is FIXABLE (not a dead end)
Earlier sessions concluded ydotool was "often unreliable / rc=0 but no app
receives." That was a STALLED X (X started before ydotoold/evdev existed, so its
udev hotplug missed the virtual device — `Xorg.0.log` had zero mention of
ydotool). The FIX (verified 2026-07-19):
- Install the evdev driver: `sudo dnf install -y xorg-x11-drv-evdev` (or
  `xorg-x11-drv-libinput`).
- Make X rebind the device: `sudo udevadm trigger` (re-fires uevents so X picks
  up ydotool now that evdev exists). If that alone doesn't bind it, a logout/login
  (restart X) definitely does.
- After this, `ydotool type`/key land in a focused window (proven: terminal
  received `YDTOOL_MCP_OKx` from the MCP server). So ydotool IS the primary
  general-keyboard path — do NOT treat it as a dead end anymore.
- The device node `/dev/uinput` must be group-writable: `sudo chown root:input
  /dev/uinput && sudo chmod 660 /dev/uinput` (henry in `input` group), and
  `ydotoold` must be running (socket at `/run/user/1000/.ydotool_socket`).

## CRITICAL — xdotool KEYBOARD does NOT work on mGBA (PROVEN, 2026-07-18)
This is the single most important fact in this skill and it corrects an earlier
wrong belief ("a real click gives mGBA keyboard focus and then xdotool keys
work"). It does NOT. mGBA's input layer REJECTS xdotool synthetic key events
entirely, regardless of focus.

PROVEN by direct probe (focus confirmed + correct bindings + pixel diff):
- `xdotool windowactivate --sync WID key x` (atomic focus+key) -> NO screen change
- real `mousemove`+`click` on mGBA center (focus VERIFIED on mGBA via
  `getwindowfocus`) then global `xdotool key x` -> NO screen change
- `xdotool key Down` (highlight should move) -> NO change
- `xdotool key 88` (raw XK_x keycode) -> NO change
- `xdotool key Return`, `F11` -> NO change
Bindings were correct (`keyA=88` in `~/.var/app/io.mgba.mGBA/config/mgba/config.ini`
`[gba.input.QT_K]`). mGBA was NOT paused (title showed "60 fps"). So the keys
are simply dropped by mGBA's SDL/Qt input layer — synthetic XTEST events are
ignored even with focus.

WHY THE OLD "it worked" BELIEF WAS A FALSE POSITIVE: what looked like progress
(advancing past the title, reaching Professor Birch's dialogue) was mGBA's OWN
auto-advance — boot/attract/cutscene sequences that play with NO input. Same
trap as the earlier "Oak's intro" false positive. NEVER trust "the screen
changed" as proof input worked — verify with a pixel diff (see below).

THE FIX (PRIMARY, no sudo, no root): drive the emulator from INSIDE, not via OS
input. mGBA 0.10.x ships **Lua scripting** (liblua + LuaSocket, bundled in the
flatpak). A small Lua script (`mgba_agent.lua`) runs INSIDE mGBA, opens a TCP
socket on `127.0.0.1:8930`, and calls mGBA's own `emu:setKeys(bitmask)` API to
press GBA buttons. The MCP server's `press_key(name="mGBA")` connects to that
socket and sends the button — NO OS input, NO focus, NO synthetic-key rejection.
This is the ONLY approach that cannot be rejected by mGBA, and it needs no root.
The shipped `xdotool_mcp.py` already routes `press_key`/`mgba_press` for mGBA to
this socket (via `_mgba_socket_send`); non-mGBA windows fall back to
xdotool/ydotool as before. See `references/emulator-internal-input.md` for the
script + one-time load recipe (Tools → Scripting → Load; mGBA `autoload=1`
reloads it every launch) + the GBA key bitmask.

THE FIX (GENERAL KEYBOARD — primary, needs sudo once): use `ydotool` for OS-level
keyboard on ANY window (incl. flatpak). It injects REAL input events via the
kernel (`/dev/uinput`) which SDL/Qt cannot distinguish from a physical keyboard.
- Requires: `ydotool` installed, `ydotoold` running, `/dev/uinput` group-writable
  (henry in `input` group), and X bound to the virtual device.
- One-time setup (needs sudo): `sudo dnf install -y ydotool xorg-x11-drv-evdev &&
  sudo usermod -aG input henry`, relogin (or `newgrp input`), `sudo chown
  root:input /dev/uinput && sudo chmod 660 /dev/uinput`, then start `ydotoold`
  (socket at `/run/user/1000/.ydotool_socket`).
- CRITICAL: after installing evdev, run `sudo udevadm trigger` so X rebinds the
  device (verified 2026-07-19 — without it, `ydotool key x` returned rc=0 but NO
  app received the event because X never attached the virtual device). A
  logout/login also works (restarts X).
- ydotool uses RAW keycodes, not xdotool key names: `ydotool key 28:1 28:0` =
  Enter (press+release). The MCP server maps names→codes in `_LINUX_KEYCODE`
  (`press_key`/`type_text` route non-mGBA keys here). Mouse is NOT ydotool — see
  FINAL ARCHITECTURE note 3 (ydotool absolute coords are monitor-pinned; mouse
  uses xdotool with local→global translation).
- Until ydotool is fully set up, OS keyboard on non-mGBA apps falls back to
  xdotool (works on most apps; only mGBA's grab rejects it). Do NOT keep patching
  focus logic for mGBA keyboard — the Lua socket is the answer for the game.

MOUSE: xdotool MOUSE (click/drag/scroll) may still work on mGBA (pointer events
don't need keyboard focus), but verify with a pixel diff per action — do not
assume. The title/menu screen can't be navigated by mouse alone (it's dpad+A),
so keyboard is essential for gameplay.

## ROOT-CAUSE ISOLATION RULE (avoid hours of blind patching)
When "input does nothing," ISOLATE THE LAYER before patching:
1. Prove the server works: call the tool directly (standalone python invoking
   the MCP server over stdio) and check it returns ok. (Server was always fine.)
2. Prove the INJECTION works: run the raw xdotool/ydotool command BY HAND in a
   terminal, with focus confirmed via `xdotool getwindowfocus`, and PIXEL-DIFF
   the screenshot before/after (+1.2s wait). If the diff is 0, the injection
   method is rejected by the target app — fix the METHOD (e.g. switch to
   ydotool), NOT the server's focus code.
3. Only if both above pass but the model still fails -> it's a prompt/SOUL issue.
The "it advanced 5h ago" observation is NOT evidence input worked — mGBA
auto-advances cutscenes. Always confirm with a pixel diff, never by eye.

## SCREENSHOTS — use mcp_xdotool_screenshot, NOT computer_use (cheap, image-only)
- CALL `mcp_xdotool_screenshot(window_name="mGBA")` to SEE the screen. It returns
  ONLY an image block — no text summary → almost no tokens per capture.
- WHY this replaced computer_use: `computer_use capture` returns a multimodal
  block = `[{text: "SOM index + summary"}, {image}]`. The IMAGE is cheap; the
  ~1.5k-token SOM/AX summary TEXT is the slow/costly part (the model burns
  ~1.5k tokens GENERATING per capture). Moving capture into the MCP server
  (which returns just the image) removes that cost. Verified: qwen3.5-9b-mtp saw
  + described the mGBA screen from the MCP screenshot tool.
- `computer_use` is DELIBERATELY absent from the cuabot toolsets so the model
  cannot fall back to the expensive capture path.
- Capture before every decision and after every action. Never screenshot Firefox.

## SCREENSHOT FRESHNESS — the capture is ALWAYS fresh; "stale" = model timing
- The MCP `screenshot` returns a NEW capture every call (scrot grabs the live
  window frame). Verified: 3 rapid captures of a static screen = 1 identical
  hash; after a key press + 1s wait the hash DIFFERS. The server does NOT cache
  or reuse images.
- If the agent reports "same / outdated screenshot, no progress", the cause is
  almost NEVER staleness — it is one of:
  1. **No wait after keypress:** mGBA renders at 60fps; screenshot taken
     instantly after `press_key` captures the PRE-press frame. ALWAYS wait
     ~1s after a key before screenshotting. (This was the actual "outdated
     screenshot" symptom this session — fixed by mandating the wait in SOUL.md.)
  2. **Key didn't register:** focus_window wasn't called first, so SDL ignored
     the synthetic key → screen never changed. Re-focus + retry.
- Debugging recipe: capture twice with a `time.sleep(1.2)` between a known-good
  keypress and the second capture; diff the two PNGs (Pillow resize->gray->mean
  abs diff, threshold ~3/255). If they differ, the pipeline is fine and the
  model's loop discipline is the issue, not the server.

## SCREENSHOT DOWNSIZE (token control)
- `screenshot` resizes the captured PNG with Pillow (LANCZOS) to `max_side=854`
  px longest side BEFORE base64-encoding. A 1440p window is ~180KB+ raw;
  downscaled ~11KB. This is the real context-cost lever — tune `max_side` up
  for sharper dialogue text, down if context is tight. Falls back to full-res
  PNG if PIL import/resize fails (wrapped in try/except).

## FAST VISION — `image_input_mode: native` + `model.supports_vision: true` (BOTH required)
There are TWO independent gates in Hermes before your model actually SEES pixels:
1. `agent.image_input_mode: native` (skips the auxiliary-vision roundtrip).
2. **`model.supports_vision: true`** — WITHOUT THIS the model gets a FILE PATH
   string, not the image. Source: `run_agent.py::_tool_result_content_for_active_model`
   (line ~5416) replaces any multimodal tool result with `_multimodal_text_summary`
   (saves PNG to `cache/images/`, gives the model the path text) WHEN
   `self._model_supports_vision()` is False. `_model_supports_vision()`
   (`run_agent.py:5211`) returns True only if `_lookup_supports_vision()` resolves
   True, which for a CUSTOM LM Studio model (not in models.dev) is False UNLESS you
   declare it. The override (`agent/image_routing.py::_supports_vision_override`,
   lines 202-207) reads the top-level `model.supports_vision` shortcut first.
   SYMPTOM of the missing flag: the model says "the image is saved at
   cache/images/...png, I can't see it" or "coordinate scale is 2.2482x" but
   describes nothing. FIX: add `supports_vision: true` under `model:` in the
   profile config. VERIFIED 2026-07-19: with the flag,
   `_lookup_supports_vision('custom:lmstudio','qwen3.5-9b-mtp')` returns True.
- `image_input_mode: native` detail: in `auto` mode, if the model is NOT
  recognized as vision-capable, Hermes routes EVERY screenshot through a SEPARATE
  vision model (`auxiliary.vision`) to "describe" it — a full extra model call per
  shot. `native` forces embedding directly into the main VLM. Source authority:
  `agent/image_routing.py::decide_image_mode` returns `"native"` unconditionally
  when `agent.image_input_mode == "native"` (lines 438-439) — does NOT consult
  `_lookup_supports_vision`, so it works even if Hermes mis-detects the model.
- REQUIRES the main model to actually be a VLM (qwen3.5-9b-mtp / qwen3-vl-2b are).
  If pointed at a TEXT-ONLY model, native sends images it can't parse -> errors;
  flip to `auto` in that case.
- `compression.enabled: true` (top-level `compression:` block) lets context
  auto-compress when long (gemma-4-12b-qat summarizes per `auxiliary.compression.model`).
  Verified gate at `agent/agent_init.py:1647`.

OPERATIONAL — config changes need a FRESH launch: a running agent loads
`config.yaml` at startup, so changing the vision flags, `CUABOT_MONITOR`, etc.
only takes effect on a NEW launch of the profile (e.g. `cuabot`). A blind run
against the OLD config is NOT evidence the fix failed — relaunch before judging.
See `references/hermes-vision-gating.md` for the full trace + verify command.

## PRIMARY-MONITOR SCREENSHOT + AUTO COORD MAPPING (single-monitor, accurate)
For general desktop control, multi-monitor coords are a headache. The MCP
`screenshot(window_name="primary")` path solves it:
- Captures ONLY the PRIMARY monitor (crops the full `scrot` shot to the primary
  rect via `_primary_monitor()`, which parses `xrandr --query`). Returns the image PLUS a
  `meta` block `{scale, monitor}`.
- **THE TARGET MONITOR IS CONFIGURABLE** via env `CUABOT_MONITOR` (set in the
  profile `mcp_servers.xdotool.env`). It is either an xrandr connector name
  (e.g. `DP-2`) or `"primary"` (xrandr's primary flag). **The xrandr `primary`
  flag is NOT always the monitor the user looks at** — on this rig xrandr's
  primary is HDMI-1 but Henry's real main screen is **DP-2 (1920x1080 @
  +4480+360)**, so `CUABOT_MONITOR=DP-2` is set. If the agent screenshots the
  WRONG monitor, change `CUABOT_MONITOR` to the correct connector (see
  `xrandr --query` for names/geometry). Default if unset = `"primary"`.
- The model reads pixel coords FROM that downscaled image, then calls
  `click(x, y)` (NO name). The server uses the cached map from this screenshot:
  original = downscaled × `scale`; global = primary_offset + original. The model
  does NO arithmetic. (`name="primary"` also works but is redundant with the
  cached map.) `_from_primary()` implements it.
- Verified 2026-07-19: primary screenshot → `click(427,460)` → global
  (3200,1378) = bottom-middle. MATCH. (That test used HDMI-1; with
  `CUABOT_MONITOR=DP-2` the same logic targets DP-2 instead — same math,
  different offset.)
- IMPORTANT GOTCHA: `_primary_monitor()` uses `re.search` — the server MUST
  `import re` at top, or the NameError hits the fallback `(0,0,1920,1080)` and
  ALL primary clicks land on the wrong monitor (this bug cost a debug cycle this
  session). If primary clicks are off by the monitor offset, check `import re`
  is present.
- For window-specific screenshots (e.g. `window_name="mGBA"`), coords are
  WINDOW-LOCAL and `_to_global()` adds the window geometry offset instead. Both
  paths keep the model coordinate-free; only the server translates.

## PITFALL — model reads COORDS OUTSIDE the image it was shown (the 8436 bug)
This is the SINGLE most common click failure and it LOOKS like the
"wrong monitor"/"wrong-target" bugs above, but the ROOT CAUSE is different:
- SYMPTOM (2026-07-19): agent told to "click the settings icon on the desktop",
  took `screenshot(window_name="primary")`, read coords `(1760, 975)`, and the
  click landed on the far-right monitor's lower-right quarter. The click result
  even reported `global (8436, 2552)` — off the entire 6400x1440 virtual desktop.
- ROOT CAUSE: the primary screenshot is DOWNSCALED to `max_side=854` (so for a
  1920x1080 monitor the image is only **854x480**). The model had NO idea of the
  image's real pixel size, so it ASSUMED full-resolution and read `(1760, 975)` —
  coordinates that do not even exist in an 854x480 image. Fed through the
  (correct) scale mapping, that exploded to `(8436, 2552)`.
  The old `meta` reported `scale` + `monitor` (useful for humans debugging, not
  for the model's coordinate READ). It never told the model the image bounds.
- FIX (shipped): the screenshot `meta` now returns `image_size: {w, h}` (the
  ACTUAL downscaled image dimensions) plus an explicit note: "IMAGE is WxH px.
  READ coords in that range [0..W, 0..H]. Do not use full-screen pixel numbers."
  `_map_click` also CLAMPS the mapped global coord to the virtual desktop bounds
  (`_virtual_bounds()` from xrandr) and returns a `warning` ("re-read coords from
  the image (range in its meta)") when the requested coord is off-desktop — so a
  bad read lands on a real monitor edge instead of flying off into the void.
- VERIFY (persistent process): primary screenshot → check `meta.image_size`
  equals the real downscaled dims (e.g. 854x480). Feed a deliberately out-of-range
  click (1760,975) → result must contain `"warning": "CLAMPED..."` and `global`
  within [0..vw, 0..vh]. Feed an in-range click (427,460) → global matches the
  monitor math (no warning).
- LESSON (for ANY vision+click agent, and for verification discipline): **verify
  against how the model ACTUALLY reads coords, not just against synthetically-
  correct inputs.** The earlier "MATCH" tests passed because they used hand-picked
  in-range numbers; they never exercised the model's real (full-res-assuming)
  read behavior. Always (a) include the image-size bound in the screenshot meta,
  and (b) clamp out-of-range clicks. When the model reports a click "mapped to
  global (8436, 2552)", do NOT chase a monitor-math bug — check whether the
  coords it read were inside the image.

## PITFALL — missing `import` for a stdlib used only in one helper
When a helper (e.g. `_primary_monitor`) uses `re.search` but the module only
imported `json/os/shutil/subprocess`, the NameError is swallowed by the
function's `try/except` and silently returns a WRONG fallback value — not a crash.
Always `import re` (and any other stdlib a helper touches) at the top of the
module, even if only one function uses it. The symptom is "everything works but
coords/values are subtly wrong," which is far harder to debug than a hard crash.

## MCP server wiring (PITFALLS — learned the hard way)
The server is `xdotool_mcp.py` at the skill root:
`~/.hermes/skills/desktop-control-xdotool/xdotool_mcp.py` (NOT under `scripts/`).
Register in the profile `config.yaml`:
```yaml
toolsets:
  - hermes-cli
  - mcp:xdotool
# NOTE: computer_use is intentionally ABSENT. Screenshots come from the MCP
# screenshot tool (cheap, image-only); input from the MCP input tools.
# Removing computer_use stops the model falling back to the ~1.5k-token
# SOM/AX-text capture path.
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
- **scrot temp-file bug:** scrot refuses to overwrite an existing file (it
  tries `<path>_000.png` instead). The `screenshot` tool must `mkstemp` then
  `os.remove` the placeholder before calling scrot, and pass
  `capture_output=True, text=True` (else `png.stderr` is bytes and crashes
  json.dumps). Also `_find_window` returns an INT window_id, so `screenshot`
  must handle both int and str (`isdigit()` only on str).
- **IMAGE BLOCK MUST PASS THROUGH VERBATIM (the silent image-killer):** the
  hand-rolled `_dispatch` wraps tool results as
  `{"content": [{"type": "text", "text": json.dumps(res)}]}` for the JSON-status
  tools. But `screenshot` returns an already-formed image block
  `{"content": [{"type": "image", "data": <b64>, "mimeType": "image/png"}]}`.
  If you route that through `json.dumps(res)`, the image block gets
  STRINGIFIED INTO A TEXT BLOCK and the model receives a JSON string, NOT an
  image (it "sees" nothing / describes a blob of text). FIX: in `_dispatch`,
  if `isinstance(res, dict) and "content" in res`, return
  `{"result": res}` verbatim (no json.dumps). All other tools still get
  serialized as text. This bug cost a full debug cycle this session.
- Verify the server before trusting it: `python3 test_mcp.py` (in the skill dir)
  should print the 12 tool names (incl. `screenshot`, `screenshot_around_cursor`,
  `mouse_location`) + a real `list_windows`
  result. If Hermes reports "no MCP servers connected", the server process is
  crashing on spawn — check it starts standalone first.

## Model-size warning
A ~2B model (qwen3.5-2b-mtp) CANNOT reliably orchestrate 8 MCP tools + screenshots:
it types tool names as literal text ("_xdotool_list_windows") and emits malformed
`<function=computer_use>` calls with no args. Use a >=9B local model (e.g.
gemma-4-12b-qat, qwen3.5-9b-mtp) for actual gameplay. The xdotool MCP + capture
pipeline is proven; the bottleneck is model tool-calling competence, not plumbing.

## CRITICAL KNOWN ISSUE — pixel-coordinate reading FAILS (the unsolved one)
As of 2026-07-19 the full pipeline is VERIFIED CORRECT end-to-end:
- Model receives the screenshot at exactly 854x480 (confirmed by inspecting
  `~/.hermes/profiles/cuabot/cache/images/*.png` -> all 854x480; Hermes does NOT
  re-resize because `_RESIZE_TARGET_BYTES`=5MB >> our ~140KB image).
- `screenshot` meta returns `image_size {w:854,h:480}` so the model KNOWS the
  valid coordinate range.
- A valid in-range click (427,460) maps to global (3200,1378) = correct
  bottom-middle of DP-2; cursor confirmed. Out-of-range reads are CLAMPED+warned.
YET the agent still mis-clicks. ROOT CAUSE: the (9B) model cannot reliably
READ pixel coordinates OUT of the screenshot and emit them in-range. Observed:
it guessed (1760,975) on an 854x480 image (coordinates that don't exist in the
frame). The image_size note + clamp mitigate but do NOT cure a model that can't
localize pixels. THIS IS NOT A PLUMBING BUG — stop patching the coordinate math.
THE FIX: switch from pixel-reading to ELEMENT-INDEXED clicking (what computer_use
`mode='som'` does and why it works):
1. A new tool `screenshot_som(window_name='primary')` returns the image PLUS a
   numbered overlay of detected interactive elements (buttons, icons, text
   fields) — like cua-driver's SOM, but we render it ourselves with pyautogui/
   OpenCV (or reuse cua-driver's grounding). Each element gets index N + its
  center (x,y) in the 854x480 space.
2. `click_element(index)` looks up that element's center and clicks via the same
   cached-map mapping. The model never outputs raw pixels — it outputs an
   integer element number. This removes the failure mode entirely.
Until that exists, the pixel path is a best-effort; for precise UI the model
should use `screenshot_around_cursor` (high-res crop) + careful reading, and
treat the clamp warning as "re-read, you were off-image".

## KNOWN ISSUES
- **mGBA d-pad LEFT/RIGHT inverted** (observed in-game 2026-07-19): `mgba_agent.lua`
  ships with `LEFT`/`RIGHT` bits SWAPPED in `KEY_BITS` to correct it. If a future
  mGBA/config flips it back, swap bits 4/5 in `KEY_BITS` (LEFT=1<<4, RIGHT=1<<5).
  Low priority — gameplay mostly needs A/START/UP/DOWN.
- **ydotool `click` bit-mask for true drag is version-dependent.** The
  hold/release mask (`0x40`/`0x80`) is inferred; verify empirically if drag
  misbehaves.
- **Pixel-coordinate reading is unreliable on ~9B models (see CRITICAL above).**
  Build element-indexed clicking; do not keep tuning the pixel mapping.

## mGBA launch (software GL so it's capturable)
`flatpak run --env=QT_OPENGL=software --env=LIBGL_ALWAYS_SOFTWARE=1 io.mgba.mGBA <rom>`

## Support files
- `xdotool_mcp.py` — the working hand-rolled MCP server (12 tools incl.
  `screenshot`), at skill ROOT, NOT under `scripts/`. `scripts/xdotool_mcp.py`
  is a mirror copy for reference.
- `references/emulator-internal-input.md` — THE primary mGBA input fix: drive
  the emulator from INSIDE via mGBA Lua scripting (`emu:setKeys`) over a TCP
  socket. Script + one-time GUI load recipe (autoload=1 reloads it) + GBA key
  bitmask + socket verify command. This is the reliable path; xdotool/ydotool
  are only for general (non-emulator) desktop apps.
- `references/ydotool-keycodes.md` — GENERAL KEYBOARD via ydotool: raw Linux
  keycode map (names→codes), mouse syntax, the multi-monitor PINNING gotcha
  (why mouse uses xdotool not ydotool), and the `udevadm trigger`+evdev fix that
  makes X actually bind the ydotool device. NEW 2026-07-19.
- `references/ydotool-fix.md` — SECONDARY mGBA/OS keyboard fallback background
  (superseded by ydotool-keycodes.md for the working recipe; kept for history).
- `references/setup-and-gotchas.md` — full reproduction recipe, DISPLAY-strip
  pitfall, MCP registration gotcha, global-key + real-click-focus nuance, the
  cheap-MCP-screenshot rationale, scrot temp-file bug, model-size note, pixel-diff
  probe.
- `references/screenshot_freshness.md` — the "stale/outdated screenshot"
  debugging path: verify the server returns a fresh frame every call, the two
  real causes (no wait after keypress / key didn't register), and a hash + pixel-diff
  probe recipe to confirm input actually changed the screen.
- `references/fast-vision-and-primary-monitor.md` — the 2026-07-19 wins: (1)
  `agent.image_input_mode: native` to skip Hermes's auxiliary vision roundtrip
  (with `image_routing.py` source authority + VLM-only caveat); (2) the
  `screenshot(window_name="primary")` single-monitor path with auto coordinate
  mapping; (3) the CACHED coordinate map fix (why bare `click(x,y)` now works via
  `STATE["map"]`, and the persistent-process caveat for testing); (4) the new
  screenshot_around_cursor` high-res precision tool.
- `references/hermes-vision-gating.md` — WHY the agent "can't see" screenshots:
  the two-gate Hermes vision mechanism (`image_input_mode: native` AND
  `model.supports_vision: true`), exact source locations in `run_agent.py` /
  `image_routing.py`, and the verify command. Read this FIRST if the model ever
  reports "image saved at cache/images/... I can't see it".
- Backup repo (rollback): `gurkebaui/cuabot-setup` on GitHub — full
  `cua_backend.py` patch, cuabot profile (config+SOUL.md), this server, the
  mgba_agent.lua socket script, and README.
