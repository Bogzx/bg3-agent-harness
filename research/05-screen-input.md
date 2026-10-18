# 05: Screen capture, input injection and UI reading (KDE Plasma 6 Wayland, AMD, native Vulkan BG3)

Scope: how the harness (a) sees exactly what the human sees, (b) sends input to the game without hijacking the user's
desktop, (c) maps world positions to clickable screen positions, and (d) reads the on-screen UI as text.

Everything marked **VERIFIED** was tested on this machine on 2026-10-09. Test artifacts are in `/tmp/bg3ai-io-test/`.
The game was **not** launched. **UNVERIFIED** means it is inferred from documentation, symbols or source and still has to be
confirmed against a running BG3.

---

## 0. Local facts (verified)

| Item | Value |
|---|---|
| Session | `XDG_SESSION_TYPE=wayland`, `WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:1` (Xwayland 24.1.6, started by KWin with `-rootless`, **no** `-enable-ei-portal`) |
| Compositor | KWin 6.3.6 / Plasma 6.3.6; 3 monitors, 7680x1440 combined workspace |
| GPU / driver | RX 6900 XT (Navi 21) + Raphael iGPU, Mesa RADV 26.1.6 (bpo), Vulkan 1.4 |
| Vulkan layers present | `VK_LAYER_MESA_screenshot` (explicit), `VK_LAYER_MESA_overlay`, `VK_LAYER_MESA_device_select`, `VK_LAYER_MESA_anti_lag`, Steam overlay and fossilize layers |
| Mesa layer tools | `/usr/bin/mesa-screenshot-control.py`, `/usr/bin/mesa-overlay-control.py` (package `mesa-vulkan-drivers:amd64`) |
| Installed | `spectacle` (kde-spectacle 6.3.5), `qdbus6`, `xdg-desktop-portal` 1.20.3 + `-kde` 6.3.5 + `-gtk`, PipeWire 1.4.2 + wireplumber, `gstreamer1.0-pipewire`, `vulkaninfo`/`vkcube`, `xwininfo`/`xprop`/`xwd`/`import`, `python3-dbus`, `python3-gi`, `libXtst.so.6`, `libeis1` |
| Not installed (apt candidate) | `gamescope` (**no Debian 13 package**), `obs-studio` (30.2.3), `ydotool` (1.0.4 bpo), `xdotool` (3.20160805), `python3-evdev` (1.9.1), `tesseract-ocr` (5.5.0), `ffmpeg`, `kdotool` (none) |
| `/dev/uinput` | `crw-rw----+ root root`, ACL `user:<you>:rw-` (via `uaccess` tag from `60-steam-input.rules` / `60-antimicrox-uinput.rules`). **Opened O_WRONLY from an unprivileged process: OK** |
| Portals | `org.freedesktop.portal.Screenshot` v2, `ScreenCast` v5, `RemoteDesktop` v2 (AvailableDeviceTypes=7), `InputCapture` present |
| KWin D-Bus | `/org/kde/KWin/ScreenShot2` (restricted), `/org/kde/KWin/EIS/RemoteDesktop` (`connectToEIS(int caps) -> (fd, int cookie)`) |
| Steam runtime | BG3 native runs in **SteamLinuxRuntime_sniper** (pressure-vessel 0.20260805.0, `sniper_platform_3.0.20260805.254768`) |

### The game binary (verified, read-only)

- `bin/bg3` links `libSDL2.so` (bundled, `$ORIGIN` RUNPATH). The bundled SDL is **SDL-release-2.30.6** and has both X11 and Wayland
  backends compiled in (`libX11.so.6`, `libXi`, `libXrandr`, `libXcursor`, `libwayland-client.so.0`, …).
- **SDL video driver order.** The SDL bootstrap table in `.data.rel.ro` (found by resolving `R_X86_64_RELATIVE` relocations to the
  "SDL X11 video driver" / "SDL Wayland video driver" description strings) is `X11 (0x199880)` before `Wayland (0x199888)`.
  SDL2 tries drivers in that order, so with `DISPLAY` set **native BG3 runs as an Xwayland client by default** on this desktop.
  `bg3` itself contains no `SDL_VIDEODRIVER` string, and among the hints it sets (`SDL_ALLOW_TOPMOST`, `SDL_IME_*`,
  `SDL_JOYSTICK_THREAD`, `SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS`) none forces Wayland. `SDL_VIDEODRIVER=wayland` would switch it (the
  binary imports both `vkCreateXlibSurfaceKHR` and `vkCreateWaylandSurfaceKHR`). One CachyOS forum post claims the native build
  "uses Wayland by default". That contradicts the bootstrap order and is probably about a SteamOS/gamescope session.
  **UNVERIFIED at runtime.** Confirm with `xwininfo -root -tree | grep -i baldur` while the game runs.
- SDL functions imported by `bg3` (all via PLT, so they can be interposed with `LD_PRELOAD`): `SDL_PollEvent`,
  `SDL_GetGlobalMouseState`, `SDL_GetMouseFocus`, `SDL_WarpMouseInWindow`, `SDL_GetWindowWMInfo`, `SDL_GetWindowSize`,
  `SDL_GetWindowID`, `SDL_SetWindowGrab`, `SDL_ShowCursor`, `SDL_StartTextInput`, `SDL_IsTextInputActive`,
  `SDL_GameController*`, `SDL_GetClipboardText`, … The game does **not** import `SDL_GetKeyboardState`, `SDL_GetMouseState`
  or `SDL_GetModState`, so keyboard and mouse buttons come **only** from events. The cursor position also comes from
  `SDL_GetGlobalMouseState`.
- Symbols: 152,400 in total, but **Larian's own code (`ecl::`, `esv::`, `ls::`, `eoc::`) has no function symbols**. Only type-name
  strings and static registrators survive. Function symbols exist for **NoesisGUI (19,285)**, PhysX (5,512) and Wwise (3,416).
  So camera, picking and `WorldToScreen` functions are **not** findable by name. Noesis is fully named. Noesis version:
  **NoesisGUI 3.1.6**, built with clang 18.1.8 (`D:/Jenkins/.../NoesisSDK/3.1.6/linux-x86_64-gnu/18.1.8/...`).

### Pressure-vessel (sniper) behaviour (verified)

Test: ran `SteamLinuxRuntime_sniper/run -- <script>` with `LD_PRELOAD` and `VK_LOADER_LAYERS_ENABLE` set, and a host process
listening on an abstract socket:

- Network namespace: the **same** as the host (`net:[4026531833]` inside and outside), so **abstract Unix sockets (`\0name`) work across
  the container boundary**. The container connected to a host listener and received its message.
- `/tmp` and `$HOME` are shared (both `/tmp/bg3ai-io-test` and the repo checkout under `$HOME` were visible).
- `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libX.so` is rewritten automatically to `/run/host/usr/lib/...`. Paths under `$HOME` or `/tmp`
  are used as is. The host glibc (2.41) is newer than sniper's, so pressure-vessel uses the host glibc and a shim built on the host runs.
- Host Vulkan layers are imported: `/overrides/share/vulkan/explicit_layer.d/{0,1,2}.json` and
  `/usr/lib/pressure-vessel/overrides/lib/x86_64-linux-gnu/libVkLayer_MESA_screenshot.so`. `VK_LOADER_LAYERS_ENABLE` passes through.
- `DISPLAY=:1` and `WAYLAND_DISPLAY=wayland-0` pass through, and `SDL_VIDEODRIVER` is unset.
- **End to end:** `vkcube` 1920x1080 inside sniper with the Mesa screenshot layer, triggered from the host via the abstract socket. **It works.**

---

## 1. Screenshots of the game window

### 1(b) In-process Vulkan layer: **VK_LAYER_MESA_screenshot**, already installed (VERIFIED, recommended)

Mesa ships a screenshot layer ([commit eda55c7c2f6](https://gitlab.freedesktop.org/mesa/mesa/-/commit/eda55c7c2f6),
`src/vulkan/screenshot-layer/`). It hooks `vkQueuePresentKHR` and copies the swapchain image (optionally only a GPU-side
sub-region) to a host-visible buffer. It then encodes a PNG on a detached thread and writes it to `output_dir`. It is controlled over an
abstract Unix socket. It captures **exactly the presented frame**, including the in-game software cursor and all Noesis UI. It does not depend
on the compositor, focus, occlusion or which monitor the window is on.

Configuration for Mesa 26.1.6 (from `VK_LAYER_MESA_SCREENSHOT_CONFIG=help` and the orchestrator's check):

```
comms=0|1             enable the control socket
control=NAME          abstract socket name (default "mesa_screenshot")
output_dir=/path      where PNGs land
frames=1/5/15-4-5|all frame-number-triggered captures (not needed for on-demand use)
log_type=info|debug
(region=x0/y0/x1/y1 percentages: in upstream README and control script; NOT in 26.1.6's help text → UNVERIFIED on this build)
```

Protocol (README + `/usr/bin/mesa-screenshot-control.py`):
- On connect the layer sends `:MesaScreenshotControlVersion=1;:DeviceName=…;:MesaVersion=…;`.
- The client sends `:capture=<name>.png;` and the file appears as `<output_dir>/<name>.png`. The name must end in `.png`.
- Upstream also accepts `:region=0.25/0.25/0.75/0.75,capture=x.png;` (GPU-side crop, ~10x cheaper copy).

Measured on this machine (vkcube 1920x1080 **inside sniper**, 5 captures over one persistent socket):

| capture | file appears | PNG complete | size |
|---|---|---|---|
| lat0–lat4 | 35–45 ms | **45–55 ms** | ~1.08 MB |

Launch (Steam launch options for BG3):
```sh
VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_screenshot VK_LAYER_MESA_SCREENSHOT_CONFIG="comms;control=bg3shot;output_dir=/tmp/bg3ai/shots" %command%
```
(create `/tmp/bg3ai/shots` first; `/tmp` is shared with the container.)

Minimal client (host side, persistent connection):
```python
import socket, os, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect("\0bg3shot"); s.recv(4096)  # header
def shot(name, outdir="/tmp/bg3ai/shots", timeout=2.0):
    p = os.path.join(outdir, name); s.send(f":capture={name};".encode())
    t = time.monotonic()
    while time.monotonic() - t < timeout:                          # file is created before it is fully written:
        try:                                                       # wait for the PNG IEND trailer
            with open(p, "rb") as f:
                f.seek(-12, 2)
                if f.read(12)[4:8] == b"IEND": return p
        except (FileNotFoundError, OSError): pass
        time.sleep(0.005)
    raise TimeoutError(p)
```

Gotchas:
- Capture happens on the **next present**. If the game stops presenting (minimized, or KWin withholds frame callbacks from a fully hidden
  Xwayland window), the request waits. **UNVERIFIED** whether BG3/Xwayland keeps presenting when minimized. Don't minimize: keep the window on its own
  virtual desktop or monitor. Occlusion by other windows doesn't matter to the layer.
- The file appears before it is complete, so check for `IEND` (above) or poll until the size stops changing.
- Use a unique filename per request. Clean up old files explicitly; the layer never deletes anything.
- The PNG is full swapchain resolution RGBA. Downscale on the host (Pillow) before sending to the model.
- The layer is "explicit". Enable it only via env, so it never affects other Vulkan apps.
- The Steam overlay layer is also active. The two coexist: vkcube worked with Steam's implicit layers present.

**Custom layer (assessment).** If we later want raw RGBA in shared memory (no PNG encode/decode, no disk) or a GPU-downscaled 1280x720
copy, fork the Mesa screenshot layer (~1.5k lines C++) or [obs-vkcapture](https://github.com/nowrep/obs-vkcapture), write into a
`memfd`/`/dev/shm` ring buffer, and signal over the same socket. Effort: **2–4 days**. Handle swapchain formats (`B8G8R8A8_UNORM/SRGB`,
`A2B10G10R10` if HDR), image layouts and multi-queue synchronisation. Not needed initially: 50 ms per PNG is fine for a turn-based agent.

### 1(a) KDE: spectacle CLI, KWin ScreenShot2, Screenshot portal

**spectacle** (VERIFIED works headless):
```sh
spectacle -b -n -a -o /tmp/x.png   # active window, 0.23 s wall (incl. process start); -f full workspace 0.51 s (7680x1440)
```
- `-a` captures the **active** window only, so the game must be focused. No "capture window by id" CLI option.
- It goes through ScreenShot2, so it gets what KWin composited (including the compositor's view of the window).

**KWin ScreenShot2 D-Bus** (VERIFIED):
- Methods: `CaptureWindow(QString handle, a{sv}, fd)`, `CaptureActiveWindow`, `CaptureArea`, `CaptureScreen`, `CaptureActiveScreen`,
  `CaptureWorkspace`, `CaptureInteractive`. The image is written as **raw** pixels to the passed pipe fd. The result map gives
  `width, height, stride, format (QImage::Format, 5 = ARGB32), scale, windowId`.
- Unauthorized callers get `org.kde.KWin.ScreenShot2.Error.NoAuthorized`.
- **Authorization:** KWin resolves the caller's `/proc/<pid>/exe` to a `.desktop` file whose `Exec=` is that path and which has
  `X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2`. **Verified:** I copied python to `/tmp/bg3ai-io-test/bg3shot-py`, wrote
  `~/.local/share/applications/bg3ai-shot-test.desktop` with `Exec=/tmp/bg3ai-io-test/bg3shot-py` and that key, ran `kbuildsycoca6`,
  and `CaptureActiveWindow` then returned 2560x1412 ARGB32 in **9–15 ms**. After deleting the desktop file it failed again. (The test desktop
  file was removed; the python copy is still in `/tmp/bg3ai-io-test`.) Use a dedicated small binary rather than whitelisting `/usr/bin/python3`.
- `CaptureWindow(handle)` takes KWin's window UUID (`{ed20…}`). Find it with a KWin script (`workspace.windowList()` filtered on
  `resourceClass`) loaded via `org.kde.kwin.Scripting.loadScript`, or with `qdbus6 org.kde.KWin /KWin getWindowInfo <uuid>`.
  Capturing an **occluded** window should work because KWin renders the window item offscreen. Minimized: probably fails or is stale.
  **UNVERIFIED** for both.
- KWin also has an env override, `KWIN_WAYLAND_NO_PERMISSION_CHECKS` (string present in `libkwin.so.6`). It disables all checks for the whole
  session, so don't use it.

**xdg-desktop-portal Screenshot** (v2): `org.freedesktop.portal.Screenshot.Screenshot(parent, {interactive: false})`. It returns a
file URI via the Request/Response signal. With xdp-kde it captures the whole screen. The first use may show a permission prompt.
It can't target one window non-interactively, and it adds hundreds of ms. Not suitable. **UNVERIFIED** latency.

### 1(c) gamescope

Not packaged for Debian 13 (`apt-cache policy gamescope` → no candidate). Options are building from source (meson; pulls wlroots, libliftoff and
vkroots submodules) or the Flatpak `com.valvesoftware.Steam.Utility.gamescope`, which only works with Flatpak Steam, not our native Steam.
- Nested on KDE Wayland: `gamescope -W 1280 -H 720 -w 1280 -h 720 -- %command%`. The game gets its own Xwayland (`DISPLAY=:N`, number varies), and
  **Super+S** saves `/tmp/gamescope_$DATE.png` ([README](https://github.com/ValveSoftware/gamescope)). gamescope exposes a PipeWire
  stream node (used by Steam recording) and has a `--backend headless` mode (3.14+). The headless log shows libei input emulation being initialised
  ([Arch BBS](https://bbs.archlinux.org/viewtopic.php?id=309714)). Known headless issues: [#1984](https://github.com/ValveSoftware/gamescope/issues/1984)
  (only the cursor visible), [#1707](https://github.com/ValveSoftware/gamescope/issues/1707) (X socket collision).
- Programmatic screenshot: keybind, or the `gamescope_control` Wayland protocol / `gamescopectl`. **UNVERIFIED** exact commands.
- Verdict: attractive isolation (the game gets its own display, so input can't leak to the desktop), but it means a build-from-source dependency,
  Steam-overlay quirks, and another layer of frame pacing. The Vulkan layer + SDL shim give the same isolation without it. **Optional, not recommended now.**

### 1(d) PipeWire ScreenCast portal

`org.freedesktop.portal.ScreenCast` v5 (supports `persist_mode=2` + `restore_token`, so the window picker shows once, then is restored silently).
You get a continuous PipeWire node, which can be consumed with `gst-launch-1.0 pipewiresrc path=<node> ! videoconvert ! pngenc snapshot=true ! filesink`
or GStreamer appsink in Python (`python3-gi` + `gstreamer1.0-pipewire` installed). Latency is about one compositor frame once streaming. KWin streams window
content even when it's occluded. It needs a running pipeline and a one-time user click, and it captures the composited result. Good as a **fallback** if
the layer ever breaks (e.g. a game update changes presentation). **UNVERIFIED** on this machine (the dialog needs a human).

### Comparison

| Method | Latency | Unfocused / occluded | Exact frame | Setup | Notes |
|---|---|---|---|---|---|
| **Mesa screenshot layer** | ~50 ms @1080p (PNG) | **Yes / Yes** (needs presents) | **Yes** | env vars only | verified inside sniper |
| KWin ScreenShot2 `CaptureWindow` | 10–15 ms raw | Yes(?) / Yes(?) | composited | desktop file + window UUID lookup | needs a whitelisted binary |
| spectacle `-a` | ~230 ms | **No** (active window) | composited | none | quick manual debugging |
| Screenshot portal | 100s of ms | whole screen | composited | possible prompt | no |
| ScreenCast portal + PipeWire | ~1 frame, streaming | Yes / Yes | composited | one-time dialog, pipeline | fallback |
| gamescope | n/a | own display | yes | build from source | optional isolation |

---

## 2. Input injection

Constraints: inputs must reach **only the game** and not steal the user's desktop. Xwayland **XTest events never leave Xwayland**: KWin
starts Xwayland without `-enable-ei-portal`, so XTest is processed only by Xwayland's internal DIX.

### 2(d) In-process SDL2 shim via `LD_PRELOAD` (**recommended primary**)

Because `bg3` imports SDL through the PLT and is event-driven for keyboard and buttons, a tiny `.so` that interposes a few SDL functions targets only
the game. It's independent of compositor, focus and monitor, and needs no permissions. pressure-vessel honours `LD_PRELOAD` (verified, above), and the
abstract-socket control channel works across the container (verified).

Functions to interpose (`dlsym(RTLD_NEXT, …)`):
- `int SDL_PollEvent(SDL_Event*)`: first return queued synthetic events (`SDL_KEYDOWN/UP` with `keysym.scancode/sym/mod`, `SDL_TEXTINPUT`,
  `SDL_MOUSEMOTION` with `windowID`, `x,y,xrel,yrel,state`, `SDL_MOUSEBUTTONDOWN/UP`, `SDL_MOUSEWHEEL`), then fall through to the real one.
  Optionally drop real `SDL_WINDOWEVENT_FOCUS_LOST` / `LEAVE` so the game behaves as if focused when the user works elsewhere.
- `Uint32 SDL_GetGlobalMouseState(int*x,int*y)`: **must** be hooked. The game reads the cursor position from it. Return
  `window_pos + virtual_cursor` and the virtual button mask whenever "agent mode" is on.
- `SDL_Window* SDL_GetMouseFocus(void)`: return the game window in agent mode.
- `void SDL_WarpMouseInWindow(SDL_Window*,int,int)`: update the virtual cursor (camera rotate and drag use warps). Optionally suppress the real warp
  so the user's pointer isn't yanked.
- `SDL_GetWindowFlags`: report `SDL_WINDOW_INPUT_FOCUS | SDL_WINDOW_MOUSE_FOCUS` in agent mode.
- Keep the **window ID** from `SDL_CreateWindow`'s return value or `SDL_GetWindowID`.
- Control channel: a listener thread on abstract socket `\0bg3input` with a line protocol (`move x y`, `click L x y`, `key F5`, `down LALT`, `text …`,
  `wheel -3`, `agent on/off`). Events go into a lock-protected queue that is drained in `SDL_PollEvent` (the game's main thread, so no SDL
  thread-safety issues). Don't call `SDL_PushEvent` from the socket thread. Injecting from inside the hooked `SDL_PollEvent` avoids SDL's
  internal mouse state machine, which the game doesn't use anyway (it doesn't import `SDL_GetMouseState`).
- Timing: hold `down`/`up` across ≥2 polled frames for clicks and keys, because some game logic samples per frame. Move the cursor and wait one or two frames before
  clicking so hover/picking updates first.

Effort: ~250–400 lines of C, **1–2 days**, plus tuning. Risks (all **UNVERIFIED** until tested in game):
1. BG3 may read input another way somewhere, e.g. the controller via `SDL_GameController*` (fine), or raw X11 via `SDL_GetWindowWMInfo` (no Xlib
   imports in `bg3`, so unlikely).
2. The game may pause or throttle when its window isn't focused at the X level. Mitigate by suppressing focus-lost events and faking flags.
3. The Steam overlay's own `LD_PRELOAD` (`gameoverlayrenderer.so`) must be preserved: use `LD_PRELOAD="$LD_PRELOAD:/path/libbg3sdl.so"`.

### 2(b) XTest / xdotool on Xwayland (**recommended fallback**, zero code)

Native BG3 is an Xwayland client by default (section 0), so `apt install xdotool` and then `xdotool search --class "bg3"`,
`xdotool mousemove --window $W x y click 1`, `xdotool key --window $W F5` (or python-xlib / ctypes `XTestFakeKeyEvent`). Events
stay inside Xwayland, so the Wayland desktop isn't hijacked. **But** the X server only routes keyboard events to the X focus window. When
KWin gives focus to a Wayland-native app, Xwayland's keyboard focus is gone, so **the game must be the focused window** for keys.
Pointer events go to the X window under Xwayland's sprite. `xdotool --window` uses `XSendEvent` (flagged `send_event`, which SDL accepts for
keys but is unreliable). Expect it to work while the game is focused and the user isn't touching the machine. **UNVERIFIED** in game.
Force X11 explicitly with `SDL_VIDEODRIVER=x11` so a future SDL update can't silently switch to Wayland.

### 2(a) /dev/uinput (ydotool / python-evdev) and KWin EIS (libei)

- `/dev/uinput` is writable by this user (ACL via `uaccess`, verified). `apt install python3-evdev` (or `ydotool`, which needs the `ydotoold` daemon) gives
  a virtual keyboard and mouse. Input goes to **whatever KWin has focused** and moves the **real** pointer. It's global, so it hijacks the desktop.
  Absolute positioning needs an `EV_ABS` tablet device, and coordinates are in compositor space across 3 monitors.
- **KWin EIS (libei)**: `org.kde.KWin.EIS.RemoteDesktop.connectToEIS(int capabilities)` **returned an EIS fd to an unprivileged
  Python process with no portal prompt** (verified; disconnected immediately with the cookie). With `libei` (`libeis1` installed; client lib via
  `libei-dev`/`python-libei`?) you get absolute pointer and keyboard injection through the compositor, with the same "global, focused window" semantics as uinput.
  The portal route (`org.freedesktop.portal.RemoteDesktop` v2 + `ConnectToEIS`) is the sanctioned version.
- Use these only as a last resort. They're useful for things outside the game (e.g. Steam dialogs).

### 2(c) gamescope nested display

Ideal isolation: the game is the only client of gamescope's Xwayland `:N`, so `DISPLAY=:N xdotool …` reaches only the game with no focus issues.
**Blocked by packaging** (build from source). Revisit if the SDL shim turns out to have problems.

### 2(e) Controller mode (virtual gamepad)

A uinput gamepad (python-evdev `UInput` with a Xbox-360 capability set) or Steam Input would be picked up by SDL's GameController API (`bg3` imports
`SDL_GameControllerOpen`, etc.). The SDL shim could instead synthesize `SDL_CONTROLLERBUTTONDOWN`/`AXISMOTION` events directly. BG3 switches its whole UI
to the controller layout when it sees pad input.
- Pros for an AI: no pixel targeting. Shoulder buttons **cycle targets** among valid, visible targets. There's a radial menu for spells and items, a
  "nearby loot" list, and d-pad dialogue selection. The interaction set is discrete, closer to a menu than a canvas.
- Cons: radial menus are dense and visual, which makes them harder to read from a screenshot. Movement with an analog stick is imprecise. Some
  KB/M-only conveniences (hotbar numbers, Alt highlight, free cursor) go away, the UI layout differs from wiki and screenshot knowledge, and AoE placement
  is a stick-driven cursor.
- Recommendation: stay **keyboard/mouse**, but keep controller target cycling in mind as a fallback for "click on X" when coordinates are unknown.

### Recommendation for input

**Primary: in-process SDL2 `LD_PRELOAD` shim** with an abstract-socket control channel. It targets the game only, works unfocused, and gives
exact window coordinates (it addresses the same coordinate space as the screenshot from the layer).
**Fallback: xdotool/XTest on Xwayland `:1`**, with the game focused and `SDL_VIDEODRIVER=x11` forced.
Last resort: uinput/libei.

---

## 3. Coordinates: world ↔ screen

### Native Linux build (what we're running)

- bg3se does **not** run on the native build. It's a Windows DLL (`DWrite.dll` proxy) using Windows byte patterns. Norbyte, in
  [#539](https://github.com/Norbyte/bg3se/issues/539): "The Linux version is built with a different compiler … it is likely that the old patterns will not match code in the Linux binary."
  Also [#530](https://github.com/Norbyte/bg3se/issues/530): SE doesn't load with the native Deck build. Latest bg3se release is **v32** (2026-06-21), Windows/Proton only.
- Larian functions are stripped, so there are no `WorldToScreen`, `ScreenToWorld`, `ecl::PickingHelperManager` or camera functions by name. Only type
  names exist (`ecl::PickingHelperManager`, `eoc::PickingStateComponent`, `ls::CameraComponent` in TypeId strings). Getting the camera matrix natively
  needs reverse engineering (find `ls::CameraComponent` storage via the ECS type registry, or pattern-match the Windows bg3se offsets in Ghidra).
  That's **high effort** and gets **invalidated by game patches**.
- **Better native route: let the UI tell us screen positions.** BG3 already projects everything the player can see into Noesis:
  `ls.LSWorldTooltipsCanvas` / `ls.LSWorldTooltipPanel` (**Left-Alt highlight labels** for items and interactables, positioned in screen space),
  `ls.EntityOverheads` / `ls.OverheadTarget` / `ls.OverheadMessage` (nameplates, damage text), and `gui::DCCursorText` (hover text under the cursor).
  Each element has a screen position via the named Noesis function `Noesis::Visual::PointToScreen(Noesis::Point const&)` (or `VisualTreeHelper::GetOffset`
  + parent chain). A Noesis tree dump (section 4) taken while Alt-highlight is held therefore gives **"label text → screen x,y"** for every visible
  interactable. That respects fairness exactly (only what is rendered) and needs no camera math. **UNVERIFIED** that the Alt labels are Noesis children of
  `LSWorldTooltipsCanvas` with usable offsets, but these class names are in the binary and Noesis is how BG3 draws all of its UI.
- Hover probing: move the virtual cursor and read `DCCursorText` / the tooltip widget to identify what's under it. This is the "picking" a human does.
- Characters: party portraits and the combat initiative bar are clickable UI (fixed screen positions in the Noesis tree), and that selects or centres
  the camera. In combat, BG3's own nameplates and overheads give the positions.

### If the project runs the Windows build under Proton with bg3se (cross-reference other research files)

- Camera: `ls::CameraComponent` (`Ext.Entity`) → `Controller` (`rf::CameraController`) → `Camera` (`rf::Camera`) with `ViewMatrix`, `InvViewMatrix`,
  `ProjectionMatrix`, `InvProjectionMatrix`, plus `FOV`, `AspectRatio`, `NearPlane`, `FarPlane`, `WorldTransform`, `LookAt`
  ([Camera.h](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/GameDefinitions/Components/Camera.h)). World→screen is then
  `clip = P·V·[x,y,z,1]; ndc = clip.xyz/clip.w; sx = (ndc.x*0.5+0.5)*W; sy = (ndc.y*0.5+0.5)*H` (y may need flipping: Vulkan/DX conventions).
  Pick the active camera (`Active == true`). Also `ecl::GameCameraBehavior` (target, distance, `CameraMode`).
- Picking: `Ext.UI.GetPickingHelper(playerId)` → `EclPlayerPickingHelper` with `Selection` (entity under the cursor), `Inner.WorldPosition`
  (world hit point), `WindowCursorPos` ([API.md](https://github.com/Norbyte/bg3se/blob/main/Docs/API.md)). Also `Ext.UI.GetCursorControl()`,
  `Ext.UI.GetDragDrop(playerId)`.
- With bg3se most actions shouldn't be clicks at all. Mod APIs (Osiris) can move, attack and cast directly; see other research files.

---

## 4. UI reading: the Noesis visual tree

BG3's UI is NoesisGUI 3.1.6 XAML. The binary contains **67 `gui::DC*` DataContext classes** and many `ls.*` controls and viewmodels, among them
`gui::DCDialogue`, `ls.DialogueHistory`, `ls.VMDialogueAnswerTag`, `ls.VMDialogStartEntry`, `gui::DCCombatLog`, `ls.CombatLogEntry`,
`gui::DCHotBar`, `ls.VMHotBarSlot`, `gui::DCTrade`, `gui::DCNotifications`, `ls.DCMessageBox`, `ls.LSMessageBoxData`, `ls.LSTooltip`,
`ls.ContextMenu(Item)`, `ls.DCPartyLine`, `ls.DCJournalQuests`, `ls.JournalDialogue*`, `CurrentSubtitle`, `gui::DCCursorText`, `ls.SoftwareCursor`.

### Option A (Proton + bg3se): `Ext.UI`, ready-made

From the source ([Module.inl](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/Lua/Libs/ClientUI/Module.inl),
[PropertyMaps/UI.inl](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/GameDefinitions/PropertyMaps/UI.inl)):
- `Ext.UI.GetRoot()`, `GetStateMachine()`, `SetState(state, subState?, clearState?, playerId?)`, `Defer(fn)` (the API doc says Noesis access must go through
  `Ext.UI.Defer` from SE v33 onward, while the latest GitHub release tag is v32, so do it that way regardless), `RegisterType`, `Instantiate`, `GetPickingHelper`, `GetCursorControl`, `GetDragDrop`.
- On any Noesis object: `.Type`, `:ToString()`, `:GetProperty(name)`, `:GetAllProperties()`, `:DirectProperties()`, `:DependencyProperties()`,
  `:SetProperty()`. Visual: `.VisualParent`, `.VisualChildrenCount`, `:VisualChild(i)`, `:PointToScreen(pt)`, `:PointFromScreen(pt)`, `:HitTest(pt)`.
  FrameworkElement: `.Parent`, `.ChildrenCount`, `:Child(i)`, `:Find(name)`, `:Resource(key)`, `.DataContext`. ICommand: `:CanExecute(arg)`,
  `:Execute(arg)`. So the agent can **press UI buttons by executing their bound commands** without any synthetic input.
- A walker that emits `{type, name, text, visible, screenRect, dataContextType}` for visible elements is ~60 lines of Lua.

### Option B (native build): own in-process Noesis dumper

All the needed Noesis functions are **named in the native binary** (local `t` symbols, so resolve them from `/proc/self/exe`'s `.symtab` +
load base, not `dlsym`):
`Noesis::View::GetContent() const`, `Noesis::View::Update(double)` (2 symbols), `VisualTreeHelper::GetChildrenCount/GetChild/GetParent/
GetOffset/GetSize/GetRoot/HitTest`, `Visual::PointToScreen`, `FrameworkElement::GetName/FindName/GetDataContext/GetActualWidth`,
`UIElement::GetVisibility/GetIsVisible`, `TextBlock::GetText`, `ContentControl::GetContent`, `ItemsControl::GetItems/GetItemsSource`,
`BaseObject::GetClassType/ToString`, `TypeClass::FindProperty(Symbol)`, `TypeProperty::Get(void const*)`,
`DependencyObject::GetValueObject(DependencyProperty const*)`.
Plan: in the same `LD_PRELOAD` shim, inline-hook `Noesis::View::Update` (funchook/subhook) to capture each `View*` (the main HUD, menus,
tooltips) and service "dump" requests **on that thread**, since Noesis is not thread-safe. Walk `GetContent()` recursively, emit visible
`TextBlock` text with screen rects (`PointToScreen`), button names, and `DataContext` type plus selected reflected properties (Noesis reflection
via `GetClassType()` → `TypeClass` → properties). Effort **~1–2 weeks** including C++ ABI work (Noesis `Ptr<>`, `String`, `Symbol` layouts from the
NoesisGUI 3.1 SDK headers). Breakage risk on patches is low-moderate, because it's symbol-based rather than pattern-based. `Noesis::GUI::IsInspectorConnected()`
exists, but the Noesis Inspector remote connection is probably compiled out of release builds. **UNVERIFIED.**

Fairness: dump **what's rendered** (visible elements, `TextBlock.Text`, `IsVisible == true`). Don't dump arbitrary DataContext fields wholesale,
because viewmodels may carry data the UI doesn't show (e.g. roll internals or unrevealed dialogue checks). Whitelist per widget.

### OCR fallback

`tesseract-ocr` 5.5.0 is available via apt (not installed). BG3 uses decorative serif fonts over textured or translucent parchment, so expect mediocre
tesseract accuracy on small text without preprocessing (crop + 2–3x upscale + threshold). **UNVERIFIED** in practice. In this pipeline Claude's own vision on a
cropped full-resolution region is the better "OCR". Use tesseract only for cheap automated checks (e.g. "is the dialogue box open?").

---

## 5. Resolution and settings for AI play

**Token cost** (Claude vision, [docs](https://platform.claude.com/docs/en/build-with-claude/vision)): `ceil(w/28) × ceil(h/28)` visual tokens.
Claude 4.7+ models accept up to a 2576 px long edge / 4784 tokens; older ones 1568 px / 1568 tokens.

| Sent image | tokens |
|---|---|
| 960x540 | 700 |
| **1280x720** | **1,196** |
| 1568x882 | 1,792 |
| 1920x1080 | 2,691 |
| 2560x1440 | 4,784 (cap) |

Advice:
- Render the game at **1920x1080 windowed** (or borderless on a dedicated monitor). Don't use exclusive fullscreen, which causes mode switches on a 3-monitor setup.
  The layer captures the native swapchain. Send **1280x720 downscales** for routine "look" calls (~1.2k tokens). For reading small text, send a
  **full-resolution crop** of the relevant panel (dialogue box, tooltip, combat log) rather than the whole frame. Once the Noesis text dump exists, most
  steps need no screenshot at all.
- If the harness clicks on coordinates the model gave, keep a fixed scale factor (1920→1280 = 1.5) and convert in the harness.
- Frame cap 30–60 fps with vsync on. That saves GPU, and capture latency stays ≤ 1 frame + ~45 ms.
- **UI**: keep the default HUD scale (larger text helps vision; **UNVERIFIED** option names: "UI scale"/"Font size" under Interface). Enable
  **subtitles** and the **speaker name** in subtitles. Keep the **combat log** panel open and expanded (it's also `gui::DCCombatLog` for text). Set
  tooltip delay to minimum and keep extended tooltips on.
- **Left Alt** highlights interactables and shows labels (`LSWorldTooltipsCanvas`). Use a toggle mode if the game offers one, or have the shim hold `LALT` while
  taking the "scene" screenshot and Noesis dump.
- **Tactical camera** (default key `O`) gives a top-down view with less occlusion and more stable label positions. It's good for exploration and combat.
- Skip intro videos. Turn off camera auto-rotation and screen-shake effects, which makes consecutive screenshots more comparable.
- Use KB/M mode (section 2e).

---

## Recommendation

**Stack (native Linux build, inside SteamLinuxRuntime_sniper):**

1. **Capture: `VK_LAYER_MESA_screenshot`.** It's already installed and was verified in-container with ~50 ms per 1080p PNG over an abstract socket. It captures the exact
   presented frame, is compositor-independent, works when unfocused or occluded, and needs zero code. Later, optionally fork it into a shared-memory
   raw-RGBA variant. Fallback: KWin `ScreenShot2.CaptureWindow` from a whitelisted helper binary (verified 10–15 ms), or a ScreenCast-portal PipeWire stream.
2. **Input: our own `LD_PRELOAD` SDL2 shim (`libbg3sdl.so`).** It hooks `SDL_PollEvent`, `SDL_GetGlobalMouseState`, `SDL_GetMouseFocus`,
   `SDL_WarpMouseInWindow` and `SDL_GetWindowFlags`, with an abstract-socket control channel. It targets only the game, works unfocused, doesn't touch the
   user's desktop and needs no permissions. Fallback: `xdotool`/XTest on Xwayland `:1` with `SDL_VIDEODRIVER=x11` and the game focused. Last resort:
   uinput/libei (both verified accessible).
3. **UI text and positions: a Noesis tree dumper in the same shim.** Hook `Noesis::View::Update`, walk the tree with the named Noesis symbols, and emit
   visible text, buttons and screen rects, including Alt-highlight world labels and overheads as click targets. If the project instead goes Proton +
   bg3se, use `Ext.UI.GetRoot()` + `Ext.UI.Defer` + `ICommand:Execute`, and the `ls::CameraComponent` matrices / `Ext.UI.GetPickingHelper` for
   coordinates. Vision on cropped screenshots is the backup "OCR".

Why: in this setup, the in-process route (Vulkan layer + LD_PRELOAD) is the only one that is simultaneously exact, focus-independent,
desktop-safe and container-compatible, and all of its plumbing (layer import, LD_PRELOAD rewrite, shared `/tmp`, shared netns abstract sockets) was
verified to work inside pressure-vessel. gamescope would give similar isolation but isn't packaged for Debian 13.

**Setup steps:**
1. `mkdir -p /tmp/bg3ai/shots` (the harness should do this at start).
2. Build `libbg3sdl.so` (C, `-shared -fPIC -ldl`, SDL2 headers only: `apt install libsdl2-dev`) into `<repo>/native/`.
3. BG3 Steam launch options:
   ```
   SDL_VIDEODRIVER=x11 LD_PRELOAD="$LD_PRELOAD:$HOME/bg3-ai/native/libbg3sdl.so" VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_screenshot VK_LAYER_MESA_SCREENSHOT_CONFIG="comms;control=bg3shot;output_dir=/tmp/bg3ai/shots" %command%
   ```
4. Harness: connect `\0bg3shot` (capture) and `\0bg3input` (input + Noesis dump). Downscale captures to 1280x720 with Pillow, and crop at full resolution for text.
5. In-game: windowed 1920x1080 on one monitor, 30–60 fps cap, subtitles on, combat log open, tactical camera available, KB/M.
6. Optional fallback tools: `sudo apt install xdotool python3-evdev tesseract-ocr`.
7. First validation once the game is running: `xwininfo -root -tree | grep -i baldur` (confirms X11), `mesa-screenshot-control.py -s bg3shot capture t.png`,
   shim `move`/`click` on the main menu, then a Noesis dump of the main menu.

### Sources
- Mesa screenshot layer: [commit](https://gitlab.freedesktop.org/mesa/mesa/-/commit/eda55c7c2f6), README via [fuchsia mirror](https://fuchsia.googlesource.com/third_party/mesa/+/main/src/vulkan/screenshot-layer), local `/usr/bin/mesa-screenshot-control.py`
- bg3se: [API.md](https://github.com/Norbyte/bg3se/blob/main/Docs/API.md), [ClientUI/Module.inl](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/Lua/Libs/ClientUI/Module.inl), [PropertyMaps/UI.inl](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/GameDefinitions/PropertyMaps/UI.inl), [Components/Camera.h](https://github.com/Norbyte/bg3se/blob/main/BG3Extender/GameDefinitions/Components/Camera.h), issues [#530](https://github.com/Norbyte/bg3se/issues/530), [#539](https://github.com/Norbyte/bg3se/issues/539), [wiki: install SE (Proton only)](https://wiki.bg3.community/Tutorials/Mod-Use/How-to-install-Script-Extender)
- SDL2 X11 default: [Phoronix: SDL2 reverts Wayland default](https://www.phoronix.com/news/SDL2-Reverts-Wayland-Default); contrary anecdote: [CachyOS forum](https://discuss.cachyos.org/t/issues-with-running-awakened-poe-and-exiled-exchange-2/22423/5)
- gamescope: [README](https://github.com/ValveSoftware/gamescope), [wiki](https://github.com/ValveSoftware/gamescope/wiki), [Arch BBS headless](https://bbs.archlinux.org/viewtopic.php?id=309714), issues [#1984](https://github.com/ValveSoftware/gamescope/issues/1984), [#1707](https://github.com/ValveSoftware/gamescope/issues/1707)
- Claude vision token cost: [platform.claude.com vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)
- Local verification: `nm -C`/`readelf -r`/`strings` on `bin/bg3` and `bin/libSDL2.so`; `qdbus6`/`busctl` introspection; tests in `/tmp/bg3ai-io-test/`
