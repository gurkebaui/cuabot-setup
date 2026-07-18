-- mgba_agent.lua
-- Control mGBA's GBA input over a localhost TCP socket. Loaded via
-- mGBA Tools -> Scripting -> Load this file. The agent (Hermes MCP server)
-- connects to 127.0.0.1:8930 and sends button commands. No OS-level input
-- injection is used, so focus / SDL keyboard-grab / synthetic-key rejection
-- are all irrelevant.
--
-- Protocol (one command per line, newline-terminated):
--   PRESS A      -> tap A (~80ms hold then auto-release)
--   HOLD A       -> press A and keep it held
--   REL A        -> release A
--   RELEASE A    -> same as REL A
--   TAP A        -> same as PRESS A
-- Buttons: A B SELECT START LEFT RIGHT UP DOWN R L  (case-insensitive)
-- Also accepts combined holds like "HOLD A START".

-- GBA key bit positions (must match mGBA's KEY_NAMES order).
-- NOTE: LEFT/RIGHT bits are intentionally SWAPPED vs the nominal mGBA order
-- because on this setup pressing "left" moved the character right (inverted
-- d-pad). If it ever looks correct again, swap these two back.
local KEY_BITS = {
    A = 1 << 0, B = 1 << 1, SELECT = 1 << 2, START = 1 << 3,
    LEFT = 1 << 5, RIGHT = 1 << 4, UP = 1 << 6, DOWN = 1 << 7,
    R = 1 << 8, L = 1 << 9,
}
local KEY_ALIASES = { S = "START", s = "SELECT", ["^"] = "UP", v = "DOWN",
                      ["<"] = "LEFT", [">"] = "RIGHT" }

local PORT = 8930
local server = nil
local clients = {}
local nextID = 1

-- currently held mask (bits set by HOLD; taps are time-limited)
local held = 0
local taps = {}  -- btn -> frame at which to release

-- frame counter for tap timing
local frame = 0
local TAP_FRAMES = 5  -- ~5 frames @60fps ≈ 83ms

local function sendline(sock, msg)
    pcall(function() sock:send(msg .. "\n") end)
end

local function set_mask()
    -- combine held bits + active taps
    local m = held
    for btn, rel in pairs(taps) do
        if frame < rel then m = m | KEY_BITS[btn] end
    end
    -- Defensive: mGBA's Lua API is emu:setKeys(bitmask). Wrap so a wrong
    -- API name surfaces in the scripting console instead of failing silently.
    local ok, err = pcall(function() emu:setKeys(m) end)
    if not ok then
        console:error("mgba_agent: setKeys failed: " .. tostring(err)
            .. " (mask=" .. m .. ")")
    end
end

local function apply_command(text)
    text = text:match("^(.-)%s*$") or ""
    local lower = text:lower()
    local parts = {}
    for w in text:gmatch("%S+") do parts[#parts + 1] = w end
    if #parts == 0 then return "ERR empty" end
    local verb = parts[1]:upper()
    if verb == "PRESS" or verb == "TAP" then
        for i = 2, #parts do
            local b = parts[i]:upper()
            b = KEY_ALIASES[b] or b
            if KEY_BITS[b] then taps[b] = frame + TAP_FRAMES end
        end
        set_mask()
        return "OK tap " .. text:sub(6)
    elseif verb == "HOLD" then
        for i = 2, #parts do
            local b = parts[i]:upper()
            b = KEY_ALIASES[b] or b
            if KEY_BITS[b] then held = held | KEY_BITS[b] end
        end
        set_mask()
        return "OK hold"
    elseif verb == "REL" or verb == "RELEASE" then
        for i = 2, #parts do
            local b = parts[i]:upper()
            b = KEY_ALIASES[b] or b
            if KEY_BITS[b] then
                held = held & ~KEY_BITS[b]
                taps[b] = nil
            end
        end
        set_mask()
        return "OK release"
    elseif verb == "STATUS" then
        return "OK held=" .. held .. " frame=" .. frame
    else
        return "ERR unknown verb: " .. verb
    end
end

local function on_received(id)
    local sock = clients[id]
    if not sock then return end
    while true do
        local p, err = sock:receive(1024)
        if p then
            local resp = apply_command(p)
            sendline(sock, resp)
            console:log("mgba_agent: " .. p:match("^(.-)%s*$") .. " -> " .. resp)
        else
            if err ~= socket.ERRORS.AGAIN then
                console:error("mgba_agent: client " .. id .. " closed (" .. tostring(err) .. ")")
                clients[id] = nil
                sock:close()
            end
            return
        end
    end
end

local function on_accept()
    local sock, err = server:accept()
    if err then
        console:error("mgba_agent: accept error " .. tostring(err))
        return
    end
    local id = nextID
    nextID = id + 1
    clients[id] = sock
    sock:add("received", function() on_received(id) end)
    sock:add("error", function()
        clients[id] = nil
        pcall(sock.close, sock)
    end)
    console:log("mgba_agent: client " .. id .. " connected")
end

-- per-frame: advance tap releases
callbacks:add("frame", function()
    frame = frame + 1
    local dirty = false
    for btn, rel in pairs(taps) do
        if frame >= rel then taps[btn] = nil; dirty = true end
    end
    if dirty then set_mask() end
end)

-- start TCP server
server = nil
while not server do
    server, err = socket.bind(nil, PORT)
    if err then
        if err == socket.ERRORS.ADDRESS_IN_USE then
            PORT = PORT + 1
        else
            console:error("mgba_agent: bind error " .. tostring(err))
            break
        end
    else
        server:listen()
        server:add("received", on_accept)
        console:log("mgba_agent: listening on 127.0.0.1:" .. PORT)
    end
end
