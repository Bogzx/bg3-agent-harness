/*
 * Test program for libbg3sdl.so: an SDL2 event loop that prints every event as one JSON line.
 *
 * Linked against the game's own bin/libSDL2.so and run with SDL_VIDEODRIVER=dummy, so it opens no window on
 * the desktop. Calls go through the PLT exactly like the game's, so LD_PRELOAD=libbg3sdl.so interposes them.
 *
 * Probes of the other hooked functions, triggered by injected keys:
 *   F12  print SDL_GetGlobalMouseState, SDL_GetMouseFocus == window, SDL_GetWindowFlags focus bits
 *   F11  SDL_WarpMouseInWindow(window, 100, 50), then the same probe
 *   F10  exit
 *
 * usage: sdltest [seconds]   (default 30)
 */
#include <SDL.h>
#include <stdio.h>
#include <stdlib.h>

static SDL_Window *win;

static void probe(const char *why) {
    int gx = -1, gy = -1;
    Uint32 mask = SDL_GetGlobalMouseState(&gx, &gy);
    Uint32 flags = SDL_GetWindowFlags(win);
    printf("{\"probe\":\"%s\",\"global\":[%d,%d],\"mask\":%u,\"focus_is_window\":%s,"
           "\"input_focus\":%s,\"mouse_focus\":%s}\n",
           why, gx, gy, mask, SDL_GetMouseFocus() == win ? "true" : "false",
           (flags & SDL_WINDOW_INPUT_FOCUS) ? "true" : "false",
           (flags & SDL_WINDOW_MOUSE_FOCUS) ? "true" : "false");
}

int main(int argc, char **argv) {
    int seconds = argc > 1 ? atoi(argv[1]) : 30;
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) != 0) {
        fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
        return 1;
    }
    win = SDL_CreateWindow("sdltest", 0, 0, 1280, 720, 0);
    if (!win) {
        fprintf(stderr, "SDL_CreateWindow: %s\n", SDL_GetError());
        return 1;
    }
    SDL_version v;
    SDL_GetVersion(&v);
    printf("{\"ready\":true,\"sdl\":\"%d.%d.%d\",\"window\":%u}\n", v.major, v.minor, v.patch, SDL_GetWindowID(win));

    Uint32 end = SDL_GetTicks() + (Uint32)seconds * 1000;
    int running = 1;
    while (running && SDL_GetTicks() < end) {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            switch (e.type) {
            case SDL_MOUSEMOTION:
                printf("{\"ev\":\"motion\",\"win\":%u,\"x\":%d,\"y\":%d,\"xrel\":%d,\"yrel\":%d,\"state\":%u}\n",
                       e.motion.windowID, e.motion.x, e.motion.y, e.motion.xrel, e.motion.yrel, e.motion.state);
                break;
            case SDL_MOUSEBUTTONDOWN:
            case SDL_MOUSEBUTTONUP:
                printf("{\"ev\":\"%s\",\"win\":%u,\"button\":%u,\"clicks\":%u,\"x\":%d,\"y\":%d,\"t\":%u}\n",
                       e.type == SDL_MOUSEBUTTONDOWN ? "mdown" : "mup", e.button.windowID, e.button.button,
                       e.button.clicks, e.button.x, e.button.y, e.button.timestamp);
                break;
            case SDL_MOUSEWHEEL:
                printf("{\"ev\":\"wheel\",\"y\":%d,\"mx\":%d,\"my\":%d}\n", e.wheel.y, e.wheel.mouseX, e.wheel.mouseY);
                break;
            case SDL_KEYDOWN:
            case SDL_KEYUP:
                printf("{\"ev\":\"%s\",\"win\":%u,\"scancode\":%d,\"name\":\"%s\",\"sym\":%d,\"mod\":%u,\"t\":%u}\n",
                       e.type == SDL_KEYDOWN ? "kdown" : "kup", e.key.windowID, e.key.keysym.scancode,
                       SDL_GetScancodeName(e.key.keysym.scancode), e.key.keysym.sym, e.key.keysym.mod,
                       e.key.timestamp);
                if (e.type == SDL_KEYUP && e.key.keysym.scancode == SDL_SCANCODE_F12) probe("f12");
                if (e.type == SDL_KEYUP && e.key.keysym.scancode == SDL_SCANCODE_F11) {
                    SDL_WarpMouseInWindow(win, 100, 50);
                    probe("warp");
                }
                if (e.type == SDL_KEYUP && e.key.keysym.scancode == SDL_SCANCODE_F10) running = 0;
                break;
            case SDL_TEXTINPUT:
                printf("{\"ev\":\"text\",\"text\":\"%s\"}\n", e.text.text);
                break;
            default:
                break;
            }
        }
        SDL_Delay(16); /* ~60 polls per second, like a frame loop */
    }
    printf("{\"exit\":true}\n");
    SDL_DestroyWindow(win);
    SDL_Quit();
    return 0;
}
