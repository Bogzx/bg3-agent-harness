/* Stand-in for bg3le in the preload chain: exports SDL_PollEvent and forwards with dlsym(RTLD_NEXT), as
 * bg3le's src/sdl_forward.cpp does, and counts the events it saw pass through. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

typedef union SDL_Event SDL_Event;
static unsigned long seen;

static void report(void) { fprintf(stderr, "chainfirst: saw %lu events\n", seen); }

__attribute__((visibility("default"))) int SDL_PollEvent(SDL_Event *ev) {
    static int (*next)(SDL_Event *);
    if (!next) {
        next = (int (*)(SDL_Event *))dlsym(RTLD_NEXT, "SDL_PollEvent");
        atexit(report);
    }
    int r = next(ev);
    if (r && ev) seen++;
    return r;
}
