-- World -> screen projection and hover checks for click-on-entity flows
-- (client context; owner: worker "act").
--
-- ops:
--   ui.screen_pos  {uuid, aim?="auto"|"feet"|"low"|"mid"|"chest", dy?, ox?, toward?, dist?} | {pos=[x,y,z]}
--       -> {x, y, on_screen, ndc=[x,y], depth, size=[w,h], aim, side, world=[x,y,z]}
--       pos: a world point (a ground spot: a two-step cast's landing); world: the point projected.
--       ox: metres along the entity's own X axis (a door's origin is its
--       hinge; +-0.5 aims at the leaf). side: "left"|"right" = where the
--       point is relative to the view direction (also when behind).
--       x,y are window pixels (Noesis root size, which matches the SDL
--       window size the input shim uses); ndc is [-1,1] with +y = down.
--   ui.hover_check {uuid}
--       -> {hovered, hover?=uuid, cursor=[x,y], world?=[x,y,z]}
--       What the picking helper says is under the cursor right now
--       (PlayerPickingHelper.Inner.Inner[1|2].GameObject).
--   ui.ground_check {pos?, tol?=1.0, avoid?=[uuid], no_chars?}
--       -> {world?, d_m?, near?, hover?, on_avoid, cursor?, overlay?, warning?}
--       A ground click's proof before it is made (two-step casts, act 2026-10-16): the cursor's world point
--       (picking helper WorldPosition) and its flat distance to pos, whether a uuid in `avoid` (with no_chars:
--       any character; both spells' second pick is "not Character()") is under it,
--       and the cursor the player sees (ecl::CursorControl RequestedCursor: "Cast" on a spot the spell takes,
--       "CastWarning" on one it doesn't; GUESS T1, acceptance act "Dimension Door 10 m via the hotbar" reads it).
--
-- Camera: the active ls::CameraComponent's Controller.Camera View and
-- Projection matrices (glm, column-major).

BG3AI = BG3AI or {}
local U = BG3AI.Util
local RPC = BG3AI.RPC
local P = {}
BG3AI.Pick = P

local function guid(s)
    if type(s) ~= "string" then return nil end
    local g = s:match("(%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x)$")
    return g and g:lower() or nil
end

local function entity_uuid(e)
    local ok, u = pcall(function() return e.Uuid.EntityUuid end)
    return ok and u and guid(tostring(u)) or nil
end

-- Active game camera controller (prefers Active, then any with a controller).
function P.camera()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "Camera")
    if not ok or not list then return nil end
    local fallback, fallback_e
    for _, e in ipairs(list) do
        local okc, c = pcall(function() return e.Camera end)
        if okc and c then
            local okk, ctrl = pcall(function() return c.Controller end)
            if okk and ctrl then
                local okA, active = pcall(function() return c.Active end)
                if okA and active then return ctrl, e end
                if not fallback then fallback, fallback_e = ctrl, e end
            end
        end
    end
    return fallback, fallback_e
end

function P.screen_size()
    local ok, w, h = pcall(function()
        local root = Ext.UI.GetRoot()
        return root.ActualWidth, root.ActualHeight
    end)
    if ok and type(w) == "number" and w > 0 then return w, h end
    return nil
end

-- Projection model (verified live 2026-10-09 against the picking helper's
-- per-pixel cursor rays on a 5120x1440 window):
--   camera-local l = conj(q) * (p - C), with C/q the active camera entity's
--   ls::TransformComponent; the camera looks along local +Z, +Y up, +X right.
--   ndc_x = fx * l.x / l.z,  ndc_y(down) = -fy * l.y / l.z,  fx = fy / aspect.
-- bg3le reads rf::CameraController with a field misalignment: the fields
-- named FOV/NearPlane hold the far plane / aspect, and ProjectionMatrix[2]
-- holds the real P[1][1] (= fy). Those are accepted only when plausible,
-- then the correctly-aligned names, then the calibrated constant (vFOV ~55 deg).
P.FY_DEFAULT = 1.921

local function plausible(x, lo, hi) return type(x) == "number" and x >= lo and x <= hi end

function P.intrinsics(ctrl)
    local fy, aspect
    pcall(function()
        local m = ctrl.Camera.ProjectionMatrix
        if plausible(m[2], 0.3, 6) then fy = m[2] elseif plausible(m[6], 0.3, 6) then fy = m[6] end
    end)
    pcall(function()
        if plausible(ctrl.NearPlane, 0.8, 6) then aspect = ctrl.NearPlane
        elseif plausible(ctrl.AspectRatio, 0.8, 6) then aspect = ctrl.AspectRatio end
    end)
    local w, h = P.screen_size()
    if not aspect and w and h and h > 0 then aspect = w / h end
    fy = fy or P.FY_DEFAULT
    aspect = aspect or (16 / 9)
    return fy / aspect, fy, aspect
end

local function rotate_inv(q, v)
    -- v rotated by the conjugate of the unit quaternion q = (x, y, z, w)
    local x, y, z, w = -q[1], -q[2], -q[3], q[4]
    local tx = 2 * (y * v[3] - z * v[2])
    local ty = 2 * (z * v[1] - x * v[3])
    local tz = 2 * (x * v[2] - y * v[1])
    return { v[1] + w * tx + (y * tz - z * ty), v[2] + w * ty + (z * tx - x * tz), v[3] + w * tz + (x * ty - y * tx) }
end

-- Project world point p -> ndc (x right, y down), depth, on_screen.
function P.project(p)
    local ctrl, cam = P.camera()
    if not cam then return nil, "no active camera" end
    local ok, C, q = pcall(function()
        local t = cam.Transform.Transform
        return t.Translate, t.RotationQuat
    end)
    if not ok or not C or not q then return nil, "camera transform unavailable" end
    local l = rotate_inv(q, { p[1] - C[1], p[2] - C[2], p[3] - C[3] })
    local side = l[1] < 0 and "left" or "right"
    if l[3] <= 0.05 then return { behind = true, side = side } end
    local fx, fy = P.intrinsics(ctrl)
    local nx = fx * l[1] / l[3]
    local ny = -fy * l[2] / l[3]
    return { ndc = { nx, ny }, depth = l[3], side = side,
        on_screen = nx >= -1 and nx <= 1 and ny >= -1 and ny <= 1 }
end

-- Where to aim on an entity: characters at chest height, corpses low, items
-- slightly above their origin.
-- S070 21:08:39: click_on a Dragon Head Torch (Wyrmway) missed 89 aims, every one on the wall behind it (a no-id
-- object 9.6 m from the torch's origin), after both camera turns: the sconces hang ~1.5-2 m up the wall (the
-- screenshot) and an item's aims stopped at 1.1 m over its origin. "high"/"top" reach a wall mount (GUESS: the
-- torch's origin is at the wall's foot); a character's are its chest (deduplicated by the search).
P.AIM_UP = { feet = 0.05, low = 0.3, mid = 0.9, chest = 1.1, high = 1.6, top = 2.1 }
function P.aim_point(uuid, aim, dy, ox)
    local e = Ext.Entity.Get(uuid)
    if not e then return nil, "unknown entity" end
    local ok, t = pcall(function() return e.Transform.Transform.Translate end)
    if not ok or not t then return nil, "no transform" end
    local p = { t[1], t[2], t[3] }
    local is_char = U.comp(e, "ClientCharacter") ~= nil
    if aim == nil or aim == "auto" then
        local dead = U.comp(e, "Death") ~= nil
        if is_char then aim = dead and "low" or "chest" else aim = "low" end
    end
    if is_char and (aim == "high" or aim == "top") then aim = "chest" end
    local off = P.AIM_UP[aim] or 0.3
    p[2] = p[2] + off + (tonumber(dy) or 0)
    ox = tonumber(ox)
    if ox and ox ~= 0 then
        pcall(function()
            local q = e.Transform.Transform.RotationQuat
            local d = rotate_inv({ -q[1], -q[2], -q[3], q[4] }, { ox, 0, 0 }) -- rotate by q
            p[1], p[3] = p[1] + d[1], p[3] + d[3]
        end)
    end
    return p, aim
end

local function vec3(t)
    if type(t) ~= "table" then return nil end
    local x, y, z = tonumber(t[1]), tonumber(t[2]), tonumber(t[3])
    if not (x and y and z) then return nil end
    return { x, y, z }
end

RPC.register("ui.screen_pos", function(args)
    local p, aim
    local uuid = guid(args.uuid)
    if not uuid and args.pos ~= nil then
        p, aim = vec3(args.pos), "point"
        if not p then error("bad pos") end
    else
        if not uuid then error("bad uuid") end
        p, aim = P.aim_point(uuid, args.aim, args.dy, args.ox)
        if not p then error(aim) end
    end
    -- toward + dist: a ground spot beside the entity, dist metres toward another (Revivify's second click:
    -- the spot the body stands up on, which must not be a character)
    local tw = guid(args.toward)
    if tw and tonumber(args.dist) then
        local te = Ext.Entity.Get(tw)
        local okt, t = pcall(function() return te.Transform.Transform.Translate end)
        if okt and t then
            local dx, dz = t[1] - p[1], t[3] - p[3]
            local d = math.sqrt(dx * dx + dz * dz)
            if d > 0.01 then
                local k = math.min(tonumber(args.dist), d) / d
                p[1], p[3] = p[1] + dx * k, p[3] + dz * k
            end
        end
    end
    local r, err = P.project(p)
    if not r then error(err) end
    local world = { U.round(p[1], 2), U.round(p[2], 2), U.round(p[3], 2) }
    if r.behind then return { on_screen = false, behind = true, side = r.side, world = world } end
    local w, h = P.screen_size()
    local out = { ndc = { U.round(r.ndc[1], 4), U.round(r.ndc[2], 4) }, on_screen = r.on_screen,
        depth = U.round(r.depth, 2), aim = aim, side = r.side, world = world }
    if w then
        out.size = { w, h }
        out.x = math.floor((r.ndc[1] * 0.5 + 0.5) * w + 0.5)
        out.y = math.floor((r.ndc[2] * 0.5 + 0.5) * h + 0.5)
    end
    return out
end)

-- Entities currently under the cursor according to the picking helper; also
-- the raw GameObjects (an object with no Uuid component still counts when it
-- is the target entity itself).
-- L7 22:09 (Dimension Door's first pick, the caster himself, spell in hand): 1.5 s of aims and the helper's GameObject
-- never named him; no hover with a spell in hand was ever seen live. The entry's own Character / DeadCharacter / Item
-- picks and the Selection (the in-range pick the cursor targets) are read too, after the GameObjects, and `via` says
-- which field named each uuid (GUESS: SE field names never read live before).
P.PICK_FIELDS = { "Character", "DeadCharacter", "Item" }
function P.hovered()
    local ok, ph = pcall(Ext.UI.GetPickingHelper, 1)
    if not ok or not ph then return {}, nil, {}, {} end
    local out, objs, via = {}, {}, {}
    local function add(e, field)
        local u = e and entity_uuid(e)
        if u and not via[u] then
            out[#out + 1], via[u] = u, field
        end
    end
    local inner
    pcall(function()
        inner = ph.Inner.Inner
        for i = 1, #inner do
            local go = inner[i].GameObject
            if go then objs[#objs + 1] = go end
            add(go, "GameObject")
        end
    end)
    pcall(function()
        for i = 1, #inner do
            for _, f in ipairs(P.PICK_FIELDS) do
                pcall(function() add(inner[i][f], f) end)
            end
        end
    end)
    pcall(function() add(ph.Selection.field_0_Entity, "Selection") end)
    return out, ph, objs, via
end

-- A picked object without a Uuid component is the target when it is the same entity (by handle: bg3le's entity
-- __eq only fires between two entity proxies, and the helper's GameObject may be a raw handle) or, failing that,
-- an object at the target's own origin (within SAME_ORIGIN_M). S050 04:11-04:12 + S051 04:47-04:56: lockpick on
-- the Steel Watch Foundry / Security Office doors gave up 8x "hover_miss hovered_instead=[(an object with no id)]"
-- (23-33 aims, 8 s each) while the `go == te` test never held.
P.SAME_ORIGIN_M = 0.3

local function handle_bits(x)
    local ok, b = pcall(Ext.Utils.HandleToInteger, x)
    return ok and b or nil
end

local function origin(x)
    if type(x) == "number" then
        local ok, e = pcall(Ext.Utils.IntegerToHandle, x)
        x = ok and e or nil
    end
    local ok, t = pcall(function() return x.Transform.Transform.Translate end)
    return ok and t and { t[1], t[2], t[3] } or nil
end

-- (same, how, metres) for a picked no-Uuid object vs the target entity te.
function P.same_entity(go, te)
    local okq, eq = pcall(function() return go == te end)
    if okq and eq then return true, "eq", 0 end
    local a, b = handle_bits(go), handle_bits(te)
    if a and b and a == b then return true, "handle", 0 end
    local pa, pb = origin(go), origin(te)
    if pa and pb then
        local d = math.sqrt((pa[1] - pb[1]) ^ 2 + (pa[2] - pb[2]) ^ 2 + (pa[3] - pb[3]) ^ 2)
        if d <= P.SAME_ORIGIN_M then return true, "origin", d end
        return false, nil, d
    end
    return false
end

RPC.register("ui.hover_check", function(args)
    local want = guid(args.uuid)
    local list, ph, objs, via = P.hovered()
    local res = { hovered = false }
    for _, u in ipairs(list) do
        if u == want then res.hovered = true end
    end
    if res.hovered and via[want] ~= "GameObject" then res.via_field = via[want] end
    local anon = false
    for _, go in ipairs(objs) do if not entity_uuid(go) then anon = true end end
    if not res.hovered and anon then
        -- S032 17:42: 90 aims at a Vault Door and the helper never named anything: an object without a Uuid
        -- component (its client entity) is compared to the target's entity itself.
        res.hover_nouuid = true
        local okE, te = pcall(Ext.Entity.Get, want)
        if okE and te then
            local seen = {}
            for _, go in ipairs(objs) do
                if not entity_uuid(go) then
                    local same, how, d = P.same_entity(go, te)
                    if same then
                        res.hovered, res.via = true, how
                        break
                    end
                    if #seen < 3 then
                        seen[#seen + 1] = { m = d and U.round(d, 2) or nil, h = handle_bits(go) and true or nil,
                            t = type(go) }
                    end
                end
            end
            if not res.hovered and #seen > 0 then res.nouuid = seen end  -- why it isn't the target (next failure)
        else
            res.nouuid_why = "the target has no client entity to compare with"
        end
    end
    res.hover = list[1]
    if ph then
        pcall(function() res.cursor = { ph.WindowCursorPos[1], ph.WindowCursorPos[2] } end)
        pcall(function() res.world = U.vec(ph.Inner.WorldPosition, 1) end)
    end
    return res
end)

-- The cursor the player sees (bg3se Enumerations CursorType): every "...Warning" cursor is the game's "can't do
-- that here" (CastWarning on a spot a spell won't take). bg3le may hand the enum as its name or its number.
P.WARNING_CURSORS = { [3] = true, [5] = true, [8] = true, [0xB] = true, [0xD] = true, [0xF] = true, [0x15] = true,
    [0x17] = true, [0x19] = true, [0x1B] = true, [0x1D] = true, [0x20] = true, [0x23] = true, [0x25] = true,
    [0x29] = true, [0x2B] = true, [0x2D] = true, [0x30] = true }
P.CURSOR_NAMES = { [0xC] = "Cast", [0xD] = "CastWarning", [0x2C] = "Walk", [0x2D] = "WalkWarning", [2] = "Arrow" }

local function cursor_name(v)
    if v == nil then return nil end
    if type(v) == "number" then return P.CURSOR_NAMES[v] or tostring(v), P.WARNING_CURSORS[v] == true end
    local s = tostring(v)
    return s, s:find("Warning", 1, true) ~= nil
end

-- {cursor, overlay, warning} from an ecl::CursorControl (nil fields when unreadable). GUESS T1: RequestedCursor is
-- the shown cursor while a spell is in hand.
function P.cursor_info(cc)
    local out = {}
    if cc == nil then return out end
    local okr, req = pcall(function() return cc.RequestedCursor end)
    local name, warn = cursor_name(okr and req or nil)
    if name then out.cursor, out.warning = name, warn end
    local oko, ov = pcall(function() return cc.CurrentOverlay end)
    local oname, owarn = cursor_name(oko and ov or nil)
    if oname and oname ~= "None" and oname ~= "0" then
        out.overlay = oname
        if owarn then out.warning = true end
    end
    return out
end

-- Flat (x/z) distance: a landing's height is the floor's, the cursor's world point sits on it.
function P.flat_dist(a, b)
    return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2)
end

-- The ground_check verdict from its reads (pure; tested offline).
function P.ground_verdict(want, tol, world, hovered, avoid, cur, is_char)
    local res = { on_avoid = false }
    for k, v in pairs(cur or {}) do res[k] = v end
    if world then res.world = { U.round(world[1], 2), U.round(world[2], 2), U.round(world[3], 2) } end
    for _, u in ipairs(hovered or {}) do
        res.hover = res.hover or u
        for _, a in ipairs(avoid or {}) do
            if u == a then res.on_avoid = true end
        end
        if is_char and is_char(u) then res.on_avoid = true end
    end
    if want and world then
        res.d_m = U.round(P.flat_dist(world, want), 2)
        res.near = P.flat_dist(world, want) <= (tonumber(tol) or 1.0)
    end
    return res
end

-- The cursor's point on the ground (pure; tested offline). L7 22:10 (Misty Step scroll, Bhaal temple): every spot read
-- "the cursor's ground point is 11.56 m off": WorldPosition is the cursor ray's near-plane point (the projection's
-- calibration, impl-act "Projection"), about the camera's spot, not a hit. The hit fields are SE names not read live
-- before, so one counts only when it lies on the cursor ray (camera C through the near point W) past W; else the
-- ray meets the landing's height (no occluder check: the post-click state and the move say if it went elsewhere).
-- hits: { {name, point}, ... } in preference order. -> point, source
P.RAY_TOL = 0.02  -- off-ray distance per metre along it (~1.1 deg)
function P.ground_point(C, W, hits, want_y)
    if not (C and W) then return nil end
    local d = { W[1] - C[1], W[2] - C[2], W[3] - C[3] }
    local len = math.sqrt(d[1] ^ 2 + d[2] ^ 2 + d[3] ^ 2)
    if len < 1e-6 then return nil end
    d = { d[1] / len, d[2] / len, d[3] / len }
    if len > 0.5 then
        -- W is no near-plane point (> 0.5 m from the camera): it is the hit itself
        hits = { { "WorldPosition", W }, table.unpack(hits or {}) }
    end
    for _, h in ipairs(hits or {}) do
        local p = h[2]
        if p and not (p[1] == 0 and p[2] == 0 and p[3] == 0) then
            local v = { p[1] - C[1], p[2] - C[2], p[3] - C[3] }
            local t = v[1] * d[1] + v[2] * d[2] + v[3] * d[3]
            if t > 0.3 then
                local off = math.sqrt((v[1] - t * d[1]) ^ 2 + (v[2] - t * d[2]) ^ 2 + (v[3] - t * d[3]) ^ 2)
                if off <= math.max(0.05, t * P.RAY_TOL) then return p, h[1] end
            end
        end
    end
    if want_y and math.abs(d[2]) > 1e-3 then
        local t = (want_y - C[2]) / d[2]
        if t > 0 then return { C[1] + t * d[1], want_y, C[3] + t * d[3] }, "ray_plane" end
    end
    return nil
end

P.GROUND_HIT_FIELDS = { "Position", "SceneryPosition", "PlaceablePosition" }

RPC.register("ui.ground_check", function(args)
    local want = args.pos ~= nil and vec3(args.pos) or nil
    if args.pos ~= nil and not want then error("bad pos") end
    local list, ph = P.hovered()
    local world, src
    if ph then
        local W, hits = nil, {}
        pcall(function()
            local wp = ph.Inner.WorldPosition
            W = vec3({ wp[1], wp[2], wp[3] })
        end)
        for _, f in ipairs(P.GROUND_HIT_FIELDS) do
            pcall(function()
                local hp = ph.Inner[f]
                hits[#hits + 1] = { f, vec3({ hp[1], hp[2], hp[3] }) }
            end)
        end
        local C
        local _, cam = P.camera()
        if cam then pcall(function()
            local t = cam.Transform.Transform.Translate
            C = vec3({ t[1], t[2], t[3] })
        end) end
        world, src = P.ground_point(C, W, hits, want and want[2])
    end
    local avoid = {}
    for _, a in ipairs(args.avoid or {}) do avoid[#avoid + 1] = guid(a) end
    local cc
    pcall(function() cc = Ext.UI.GetCursorControl() end)
    local is_char = args.no_chars and function(u)
        local ok, e = pcall(Ext.Entity.Get, u)
        return ok and e ~= nil and U.comp(e, "ClientCharacter") ~= nil
    end or nil
    local res = P.ground_verdict(want, args.tol, world, list, avoid, P.cursor_info(cc), is_char)
    res.world_src = src
    if ph then pcall(function() res.at = { ph.WindowCursorPos[1], ph.WindowCursorPos[2] } end) end
    return res
end)

return P
