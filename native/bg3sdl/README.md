# bg3sdl: SDL2 input shim

An `LD_PRELOAD` library that injects mouse and keyboard input into the game only, through the game's own
`SDL_PollEvent` (design: `research/05-screen-input.md` §2(d)). It needs no focus, no compositor and no
permissions, and it doesn't touch the user's real pointer. Python client: `bridge/src/bg3ai/input.py`.

## Build and test

```bash
native/bg3sdl/build.sh                              # -> build/bg3sdl/libbg3sdl.so (+ build/bg3sdl/sdltest)
python3 native/bg3sdl/test/test_shim.py             # 37 checks against an SDL2 program, no game needed
python3 native/bg3sdl/test/test_shim.py --chain --sniper   # behind a bg3le-style hook, inside the sniper container
```

* Compiled with clang (`tools/bg3le-builder` image) against the Steam Runtime sniper sysroot that
  `tools/build_bg3le.sh` unpacked, with `-Werror`. `build.sh` fails if the library needs a glibc newer than 2.31
  (it currently needs 2.17). SDL is not linked; every SDL function is resolved at run time.
* SDL headers: SDL2 **2.30.6**, the version of the game's bundled `bin/libSDL2.so`
  (`SDL-release-2.30.6-0-gba2f78a00`). The release tarball is fetched into `build/bg3sdl/` and checked against
  a pinned sha256.
* `sdltest` links against the game's own `bin/libSDL2.so` and uses `SDL_VIDEODRIVER=dummy`, so it opens no window.
  The test runs it on a private socket name (`BG3AI_INPUT_SOCKET`), so it never collides with a running game.

## In the game

`runner/launch_game.sh` preloads it after bg3le (`LD_PRELOAD=libbg3le.so:libbg3sdl.so`); `--no-input-shim`
leaves it out. bg3le exports `SDL_PollEvent` and forwards with `dlsym(RTLD_NEXT)`, which reaches the shim, so
injected events also pass through bg3le (its `Ext.Events.KeyInput` and overlay see them). The shim's
listener starts on the first `SDL_PollEvent` and logs `bg3sdl: bg3sdl 1 listening on @bg3ai-input` to the
game log.

Interposed functions: `SDL_PollEvent`, `SDL_GetGlobalMouseState`, `SDL_GetMouseFocus`, `SDL_WarpMouseInWindow`,
`SDL_GetWindowFlags`, and `SDL_CreateWindow` (to remember the game window).

## Protocol (`\0bg3ai-input`, one line in, one line out)

| Command | Effect |
|---|---|
| `status` | `ok {"version","agent","window","pos","size","drawable","cursor","buttons","queue","frames","injected","dropped","warps","hold"}` |
| `agent on\|off` | Agent mode: virtual cursor for `SDL_GetGlobalMouseState`, the game window as mouse focus, focus flags set, the user's real mouse events and focus-lost/leave events dropped, camera warps kept virtual. Mouse commands turn it on by themselves |
| `move X Y` | Mouse motion |
| `click L\|R\|M\|X1\|X2 X Y [1-3]` | Move, wait `hold` frames for hover/picking, then N clicks (each down held `hold` frames) |
| `mdown B [X Y]` / `mup B [X Y]` | Press or release (drags) |
| `wheel N [X Y]` | N wheel notches (positive = up/away) |
| `key K[+K...]` | Press and release a key or chord (`F5`, `Escape`, `LCTRL+S`). SDL scancode names, case-insensitive, plus the aliases `CTRL/LCTRL/RCTRL`, `SHIFT/…`, `ALT/…`, `ESC`, `ENTER`, `SPACE`, `PGUP/PGDN`, `DEL`, `INS` |
| `down K` / `up K` | One key |
| `text STRING` | `SDL_TEXTINPUT` (UTF-8, split into ≤31-byte events) |
| `wait N` | Wait N frames before the next queued step |
| `sync [MS]` | Reply once everything queued before it has been delivered |
| `clear` | Drop everything still queued |
| `ping` | `ok pong` |

Every queued command replies `ok queued N` at once. Commands from one line are queued atomically. A "frame" is
one drain of the real SDL queue (the poll that returns 0). Coordinates are window coordinates (SDL mouse-event
space). `status.size` versus `status.drawable` gives the scale to screenshot pixels
(`InputClient.from_screenshot`).

Environment: `BG3AI_INPUT_SOCKET` (socket name), `BG3AI_INPUT_AGENT=1` (start in agent mode),
`BG3AI_INPUT_HOLD` (frames, default 2).

## Not yet verified in the game

The shim has only been tested against an SDL2 test program (plain, chained, and inside sniper), not BG3 itself.
Open questions for the first live run: whether BG3's picking follows `SDL_GetGlobalMouseState` (the
research's assumption), whether two frames of hold are enough for its UI, how the camera behaves with virtual
warps (middle-drag rotate), and whether HiDPI scaling ever makes `size != drawable` on this desktop.
