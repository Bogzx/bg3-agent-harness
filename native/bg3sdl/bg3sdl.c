/*
 * bg3sdl: LD_PRELOAD SDL2 input shim for Baldur's Gate 3 (native Linux build).
 *
 * Injects mouse and keyboard input into the game only: no compositor, no focus, no permissions
 * (research/05-screen-input.md section 2(d)). The game imports SDL through the PLT and takes keys and
 * buttons only from events and the cursor position from SDL_GetGlobalMouseState, so it is enough to:
 *
 *   SDL_PollEvent            hand out queued synthetic events first, on the game's own polling thread
 *   SDL_GetGlobalMouseState  report window position + virtual cursor, and the virtual button mask (agent mode)
 *   SDL_GetMouseFocus        report the game window (agent mode)
 *   SDL_WarpMouseInWindow    move the virtual cursor instead of the real pointer (agent mode)
 *   SDL_GetWindowFlags       report input and mouse focus for the game window (agent mode)
 *   SDL_CreateWindow         remember the game window
 *
 * Chains with bg3le: LD_PRELOAD=libbg3le.so:libbg3sdl.so. bg3le also exports SDL_PollEvent and forwards with
 * dlsym(RTLD_NEXT), which lands here, and we forward to the real libSDL2. Injected events therefore pass
 * through bg3le too (its overlay and Ext.Events.KeyInput see them).
 *
 * Control: abstract unix socket "\0bg3ai-input" (BG3AI_INPUT_SOCKET overrides the name), one command per
 * line, one reply line per command ("ok ..." or "err ..."). Coordinates are window coordinates, the same
 * space as SDL's mouse events (equal to pixels unless the window is HiDPI-scaled; "status" reports both
 * sizes).
 *
 *   status                      ok {json: agent, window, pos, size, drawable, cursor, buttons, queue, frames, ...}
 *   agent on|off                agent mode (virtual cursor, fake focus, drop real mouse and focus-loss events)
 *   move X Y                    mouse motion to X,Y
 *   click B X Y [N]             move, then N clicks of button B (L|R|M|X1|X2), each held for HOLD frames
 *   mdown B [X Y] / mup B [X Y] press or release a mouse button (for drags)
 *   wheel N [X Y]               vertical wheel, N notches (positive = away from the user)
 *   key K[+K...]                press and release a key or chord (e.g. "key F5", "key LCTRL+S")
 *   down K / up K               press or release one key
 *   text STRING                 SDL_TEXTINPUT with the rest of the line (UTF-8)
 *   wait N                      wait N frames before the next queued step
 *   sync [MS]                   reply once everything queued so far has been delivered (default 5000 ms)
 *   clear                       drop everything still queued
 *   ping                        ok pong
 *
 * Mouse commands switch agent mode on by themselves (the game reads the cursor from SDL_GetGlobalMouseState,
 * so injected motion without it would be ignored); "agent off" hands the mouse back to the user.
 * BG3AI_INPUT_AGENT=1 starts in agent mode. BG3AI_INPUT_HOLD sets the hold in frames (default 2).
 *
 * Built without linking SDL: every SDL function is found at run time, so the shim loads into any SDL2
 * program and does nothing until that program polls for events.
 */
#define _GNU_SOURCE
#include <SDL.h>

#include <ctype.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))
#define BG3SDL_VERSION "bg3sdl 1"
#define QUEUE_MAX 4096
#define LINE_MAX_LEN 4096

/* ---- real SDL functions ---- */

static int (*real_PollEvent)(SDL_Event *);
static Uint32 (*real_GetGlobalMouseState)(int *, int *);
static SDL_Window *(*real_GetMouseFocus)(void);
static void (*real_WarpMouseInWindow)(SDL_Window *, int, int);
static Uint32 (*real_GetWindowFlags)(SDL_Window *);
static SDL_Window *(*real_CreateWindow)(const char *, int, int, int, int, Uint32);
static Uint32 (*fn_GetWindowID)(SDL_Window *);
static SDL_Window *(*fn_GetWindowFromID)(Uint32);
static void (*fn_GetWindowPosition)(SDL_Window *, int *, int *);
static void (*fn_GetWindowSize)(SDL_Window *, int *, int *);
static void (*fn_Vulkan_GetDrawableSize)(SDL_Window *, int *, int *);
static SDL_Scancode (*fn_GetScancodeFromName)(const char *);
static SDL_Keycode (*fn_GetKeyFromScancode)(SDL_Scancode);
static Uint32 (*fn_GetTicks)(void);

static void logmsg(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("bg3sdl: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

/* Past ours in the preload chain; the explicit handle covers a program that has not loaded SDL by the time
 * we are asked (same fallback bg3le uses). */
static void *resolve(const char *name) {
    void *fn = dlsym(RTLD_NEXT, name);
    if (fn) return fn;
    static const char *libs[] = {"libSDL2.so", "libSDL2-2.0.so.0"};
    for (size_t i = 0; i < sizeof libs / sizeof *libs; i++) {
        void *h = dlopen(libs[i], RTLD_LAZY | RTLD_NOLOAD);
        if (h && (fn = dlsym(h, name))) return fn;
    }
    return NULL;
}

static pthread_once_t g_resolve_once = PTHREAD_ONCE_INIT;

static void resolve_all(void) {
    real_PollEvent = resolve("SDL_PollEvent");
    real_GetGlobalMouseState = resolve("SDL_GetGlobalMouseState");
    real_GetMouseFocus = resolve("SDL_GetMouseFocus");
    real_WarpMouseInWindow = resolve("SDL_WarpMouseInWindow");
    real_GetWindowFlags = resolve("SDL_GetWindowFlags");
    real_CreateWindow = resolve("SDL_CreateWindow");
    fn_GetWindowID = resolve("SDL_GetWindowID");
    fn_GetWindowFromID = resolve("SDL_GetWindowFromID");
    fn_GetWindowPosition = resolve("SDL_GetWindowPosition");
    fn_GetWindowSize = resolve("SDL_GetWindowSize");
    fn_Vulkan_GetDrawableSize = resolve("SDL_Vulkan_GetDrawableSize");
    fn_GetScancodeFromName = resolve("SDL_GetScancodeFromName");
    fn_GetKeyFromScancode = resolve("SDL_GetKeyFromScancode");
    fn_GetTicks = resolve("SDL_GetTicks");
    if (!real_PollEvent) logmsg("SDL_PollEvent not found; shim inactive");
}

static void ensure_resolved(void) { pthread_once(&g_resolve_once, resolve_all); }

/* ---- shared state ---- */

static _Atomic(SDL_Window *) g_window;
static atomic_uint g_window_id;
static atomic_int g_agent;
static atomic_int g_cx, g_cy;            /* virtual cursor, window coordinates */
static atomic_uint g_buttons;            /* SDL_BUTTON_* mask of injected presses */
static atomic_int g_wx, g_wy, g_ww, g_wh, g_dw, g_dh; /* window cache, refreshed once per frame */
static atomic_ulong g_frames;            /* completed drains of the real event queue */
static atomic_ulong g_injected, g_dropped, g_warps;
static int g_hold = 2;

/* Main-thread only: modifier state of injected keys. */
static Uint16 g_mods;

/* ---- the queue: synthetic events and frame waits ---- */

enum step_kind { STEP_EVENT, STEP_WAIT };

typedef struct {
    int kind;
    SDL_Event ev;            /* STEP_EVENT; windowID, timestamp, state, sym and mod are filled at delivery */
    unsigned long frames;    /* STEP_WAIT: how many frames */
    unsigned long until;     /* STEP_WAIT: frame count to wait for, set when the wait reaches the head */
} step_t;

static pthread_mutex_t g_qlock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_qempty = PTHREAD_COND_INITIALIZER;
static step_t g_queue[QUEUE_MAX];
static size_t g_qhead, g_qlen;
static atomic_int g_qnonempty;
static unsigned long g_qdelivered; /* steps taken off the queue; sync waits on it, under g_qlock */
static unsigned long g_qposted;    /* steps ever added, under g_qlock */

/* Adds a whole batch atomically, so commands from two clients never interleave. */
static int queue_push(const step_t *steps, size_t n) {
    pthread_mutex_lock(&g_qlock);
    if (g_qlen + n > QUEUE_MAX) {
        pthread_mutex_unlock(&g_qlock);
        return -1;
    }
    for (size_t i = 0; i < n; i++) g_queue[(g_qhead + g_qlen + i) % QUEUE_MAX] = steps[i];
    g_qlen += n;
    g_qposted += n;
    atomic_store(&g_qnonempty, 1);
    pthread_mutex_unlock(&g_qlock);
    return 0;
}

static void queue_pop_locked(void) {
    g_qhead = (g_qhead + 1) % QUEUE_MAX;
    g_qlen--;
    g_qdelivered++;
    if (g_qlen == 0) {
        atomic_store(&g_qnonempty, 0);
        pthread_cond_broadcast(&g_qempty);
    }
}

static Uint16 mod_for(SDL_Scancode sc) {
    switch (sc) {
    case SDL_SCANCODE_LCTRL: return KMOD_LCTRL;
    case SDL_SCANCODE_RCTRL: return KMOD_RCTRL;
    case SDL_SCANCODE_LSHIFT: return KMOD_LSHIFT;
    case SDL_SCANCODE_RSHIFT: return KMOD_RSHIFT;
    case SDL_SCANCODE_LALT: return KMOD_LALT;
    case SDL_SCANCODE_RALT: return KMOD_RALT;
    case SDL_SCANCODE_LGUI: return KMOD_LGUI;
    case SDL_SCANCODE_RGUI: return KMOD_RGUI;
    default: return 0;
    }
}

/* Main thread: complete a synthetic event the way SDL would have, and apply it to the virtual state. */
static void finish_event(SDL_Event *ev) {
    Uint32 wid = atomic_load(&g_window_id);
    Uint32 now = fn_GetTicks ? fn_GetTicks() : 0;
    switch (ev->type) {
    case SDL_MOUSEMOTION: {
        int px = atomic_exchange(&g_cx, ev->motion.x);
        int py = atomic_exchange(&g_cy, ev->motion.y);
        ev->motion.timestamp = now;
        ev->motion.windowID = wid;
        ev->motion.xrel = ev->motion.x - px;
        ev->motion.yrel = ev->motion.y - py;
        ev->motion.state = atomic_load(&g_buttons);
        break;
    }
    case SDL_MOUSEBUTTONDOWN:
    case SDL_MOUSEBUTTONUP: {
        Uint32 bit = SDL_BUTTON(ev->button.button);
        if (ev->type == SDL_MOUSEBUTTONDOWN) atomic_fetch_or(&g_buttons, bit);
        else atomic_fetch_and(&g_buttons, ~bit);
        ev->button.timestamp = now;
        ev->button.windowID = wid;
        ev->button.x = atomic_load(&g_cx);
        ev->button.y = atomic_load(&g_cy);
        break;
    }
    case SDL_MOUSEWHEEL:
        ev->wheel.timestamp = now;
        ev->wheel.windowID = wid;
        ev->wheel.mouseX = atomic_load(&g_cx);
        ev->wheel.mouseY = atomic_load(&g_cy);
        break;
    case SDL_KEYDOWN:
    case SDL_KEYUP: {
        Uint16 m = mod_for(ev->key.keysym.scancode);
        if (m) {
            if (ev->type == SDL_KEYDOWN) g_mods |= m;
            else g_mods &= ~m;
        }
        ev->key.timestamp = now;
        ev->key.windowID = wid;
        ev->key.keysym.sym = fn_GetKeyFromScancode ? fn_GetKeyFromScancode(ev->key.keysym.scancode)
                                                   : SDL_SCANCODE_TO_KEYCODE(ev->key.keysym.scancode);
        ev->key.keysym.mod = g_mods;
        break;
    }
    case SDL_TEXTINPUT:
        ev->text.timestamp = now;
        ev->text.windowID = wid;
        break;
    }
}

/* Main thread: the next synthetic event, or 0 when none is due this frame. */
static int take_injected(SDL_Event *out) {
    if (!atomic_load(&g_qnonempty)) return 0;
    pthread_mutex_lock(&g_qlock);
    while (g_qlen) {
        step_t *s = &g_queue[g_qhead];
        if (s->kind == STEP_WAIT) {
            unsigned long frames = atomic_load(&g_frames);
            if (s->until == 0) s->until = frames + s->frames;
            if (frames < s->until) break;
            queue_pop_locked();
            continue;
        }
        *out = s->ev;
        queue_pop_locked();
        pthread_mutex_unlock(&g_qlock);
        finish_event(out);
        atomic_fetch_add(&g_injected, 1);
        return 1;
    }
    pthread_mutex_unlock(&g_qlock);
    return 0;
}

static void adopt_window(SDL_Window *w) {
    if (!w) return;
    atomic_store(&g_window, w);
    if (fn_GetWindowID) atomic_store(&g_window_id, fn_GetWindowID(w));
}

/* Main thread, once per frame: where the window is and how big, for the cursor hooks and "status". */
static void refresh_window(void) {
    SDL_Window *w = atomic_load(&g_window);
    if (!w) return;
    int x = 0, y = 0, cw = 0, ch = 0, dw = 0, dh = 0;
    if (fn_GetWindowPosition) fn_GetWindowPosition(w, &x, &y);
    if (fn_GetWindowSize) fn_GetWindowSize(w, &cw, &ch);
    if (fn_Vulkan_GetDrawableSize) fn_Vulkan_GetDrawableSize(w, &dw, &dh);
    atomic_store(&g_wx, x);
    atomic_store(&g_wy, y);
    atomic_store(&g_ww, cw);
    atomic_store(&g_wh, ch);
    atomic_store(&g_dw, dw);
    atomic_store(&g_dh, dh);
}

/* Agent mode: the user's mouse and the window losing focus must not reach the game. */
static int drop_real(const SDL_Event *ev) {
    switch (ev->type) {
    case SDL_MOUSEMOTION:
    case SDL_MOUSEBUTTONDOWN:
    case SDL_MOUSEBUTTONUP:
    case SDL_MOUSEWHEEL:
        return 1;
    case SDL_WINDOWEVENT:
        return ev->window.event == SDL_WINDOWEVENT_FOCUS_LOST || ev->window.event == SDL_WINDOWEVENT_LEAVE;
    default:
        return 0;
    }
}

static void start_listener(void);

/* ---- interposed SDL functions ---- */

EXPORT int SDL_PollEvent(SDL_Event *event) {
    ensure_resolved();
    if (!real_PollEvent) return 0;
    start_listener();

    /* With a NULL argument SDL only reports whether an event is pending, without removing it. */
    if (!event) return atomic_load(&g_qnonempty) || real_PollEvent(NULL);

    if (take_injected(event)) return 1;

    SDL_Event *ev = event;
    for (;;) {
        int r = real_PollEvent(ev);
        if (!r) {
            atomic_fetch_add(&g_frames, 1);
            refresh_window();
            return 0;
        }
        if (!atomic_load(&g_window) && fn_GetWindowFromID) {
            Uint32 id = 0;
            if (ev->type == SDL_WINDOWEVENT) id = ev->window.windowID;
            else if (ev->type == SDL_KEYDOWN || ev->type == SDL_KEYUP) id = ev->key.windowID;
            else if (ev->type == SDL_MOUSEMOTION) id = ev->motion.windowID;
            if (id) adopt_window(fn_GetWindowFromID(id));
        }
        if (atomic_load(&g_agent) && drop_real(ev)) {
            atomic_fetch_add(&g_dropped, 1);
            continue;
        }
        return 1;
    }
}

EXPORT SDL_Window *SDL_CreateWindow(const char *title, int x, int y, int w, int h, Uint32 flags) {
    ensure_resolved();
    SDL_Window *win = real_CreateWindow ? real_CreateWindow(title, x, y, w, h, flags) : NULL;
    if (win && !(flags & SDL_WINDOW_HIDDEN)) adopt_window(win);
    else if (win && !atomic_load(&g_window)) adopt_window(win);
    return win;
}

EXPORT Uint32 SDL_GetGlobalMouseState(int *x, int *y) {
    ensure_resolved();
    if (!atomic_load(&g_agent) || !atomic_load(&g_window)) {
        return real_GetGlobalMouseState ? real_GetGlobalMouseState(x, y) : 0;
    }
    if (x) *x = atomic_load(&g_wx) + atomic_load(&g_cx);
    if (y) *y = atomic_load(&g_wy) + atomic_load(&g_cy);
    return atomic_load(&g_buttons);
}

EXPORT SDL_Window *SDL_GetMouseFocus(void) {
    ensure_resolved();
    SDL_Window *w = atomic_load(&g_window);
    if (atomic_load(&g_agent) && w) return w;
    return real_GetMouseFocus ? real_GetMouseFocus() : NULL;
}

EXPORT void SDL_WarpMouseInWindow(SDL_Window *window, int x, int y) {
    ensure_resolved();
    if (atomic_load(&g_agent)) {
        /* The game recentres the cursor while the camera is dragged; keep that virtual so the user's
         * pointer is not yanked. */
        atomic_store(&g_cx, x);
        atomic_store(&g_cy, y);
        atomic_fetch_add(&g_warps, 1);
        return;
    }
    if (real_WarpMouseInWindow) real_WarpMouseInWindow(window, x, y);
}

EXPORT Uint32 SDL_GetWindowFlags(SDL_Window *window) {
    ensure_resolved();
    Uint32 flags = real_GetWindowFlags ? real_GetWindowFlags(window) : 0;
    if (atomic_load(&g_agent) && window && window == atomic_load(&g_window))
        flags |= SDL_WINDOW_INPUT_FOCUS | SDL_WINDOW_MOUSE_FOCUS;
    return flags;
}

/* ---- command parsing (socket threads) ---- */

static const struct { const char *alias, *name; } KEY_ALIASES[] = {
    {"CTRL", "Left Ctrl"},   {"LCTRL", "Left Ctrl"},    {"RCTRL", "Right Ctrl"},
    {"SHIFT", "Left Shift"}, {"LSHIFT", "Left Shift"},  {"RSHIFT", "Right Shift"},
    {"ALT", "Left Alt"},     {"LALT", "Left Alt"},      {"RALT", "Right Alt"},
    {"GUI", "Left GUI"},     {"LGUI", "Left GUI"},      {"RGUI", "Right GUI"},
    {"ESC", "Escape"},       {"ENTER", "Return"},       {"RETURN", "Return"},
    {"SPACE", "Space"},      {"BACKSPACE", "Backspace"},{"DEL", "Delete"},
    {"PGUP", "PageUp"},      {"PAGEUP", "PageUp"},      {"PGDN", "PageDown"},
    {"PAGEDOWN", "PageDown"},{"INS", "Insert"},         {"CAPSLOCK", "CapsLock"},
};

static SDL_Scancode scancode_of(const char *name) {
    for (size_t i = 0; i < sizeof KEY_ALIASES / sizeof *KEY_ALIASES; i++)
        if (!strcasecmp(name, KEY_ALIASES[i].alias)) name = KEY_ALIASES[i].name;
    if (!fn_GetScancodeFromName) return SDL_SCANCODE_UNKNOWN;
    return fn_GetScancodeFromName(name);
}

static int button_of(const char *s) {
    if (!strcasecmp(s, "L") || !strcasecmp(s, "LEFT") || !strcmp(s, "1")) return SDL_BUTTON_LEFT;
    if (!strcasecmp(s, "R") || !strcasecmp(s, "RIGHT") || !strcmp(s, "3")) return SDL_BUTTON_RIGHT;
    if (!strcasecmp(s, "M") || !strcasecmp(s, "MIDDLE") || !strcmp(s, "2")) return SDL_BUTTON_MIDDLE;
    if (!strcasecmp(s, "X1") || !strcmp(s, "4")) return SDL_BUTTON_X1;
    if (!strcasecmp(s, "X2") || !strcmp(s, "5")) return SDL_BUTTON_X2;
    return 0;
}

typedef struct {
    step_t steps[512];
    size_t n;
    int overflow;
} batch_t;

static step_t *batch_add(batch_t *b) {
    if (b->n >= sizeof b->steps / sizeof *b->steps) {
        b->overflow = 1;
        return &b->steps[b->n - 1];
    }
    step_t *s = &b->steps[b->n++];
    memset(s, 0, sizeof *s);
    return s;
}

static void add_wait(batch_t *b, unsigned long frames) {
    if (!frames) return;
    step_t *s = batch_add(b);
    s->kind = STEP_WAIT;
    s->frames = frames;
}

static void add_move(batch_t *b, int x, int y) {
    step_t *s = batch_add(b);
    s->kind = STEP_EVENT;
    s->ev.type = SDL_MOUSEMOTION;
    s->ev.motion.x = x;
    s->ev.motion.y = y;
}

static void add_button(batch_t *b, int down, int button, int clicks) {
    step_t *s = batch_add(b);
    s->kind = STEP_EVENT;
    s->ev.type = down ? SDL_MOUSEBUTTONDOWN : SDL_MOUSEBUTTONUP;
    s->ev.button.button = (Uint8)button;
    s->ev.button.state = down ? SDL_PRESSED : SDL_RELEASED;
    s->ev.button.clicks = (Uint8)clicks;
}

static void add_key(batch_t *b, int down, SDL_Scancode sc) {
    step_t *s = batch_add(b);
    s->kind = STEP_EVENT;
    s->ev.type = down ? SDL_KEYDOWN : SDL_KEYUP;
    s->ev.key.state = down ? SDL_PRESSED : SDL_RELEASED;
    s->ev.key.keysym.scancode = sc;
}

/* Split on spaces; returns the number of tokens. `rest` points at the text after the first token. */
static int tokenize(char *line, char **tok, int max, char **rest) {
    int n = 0;
    char *p = line;
    *rest = NULL;
    while (*p && n < max) {
        while (*p == ' ' || *p == '\t') p++;
        if (!*p) break;
        tok[n++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        if (*p) *p++ = '\0';
        if (n == 1) {
            char *r = p;
            while (*r == ' ' || *r == '\t') r++;
            *rest = r;
        }
    }
    return n;
}

static int parse_int(const char *s, int *out) {
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || end == s || *end || v < -1000000 || v > 1000000) return 0;
    *out = (int)v;
    return 1;
}

static void status_json(char *out, size_t cap) {
    pthread_mutex_lock(&g_qlock);
    size_t qlen = g_qlen;
    pthread_mutex_unlock(&g_qlock);
    snprintf(out, cap,
             "ok {\"version\":\"%s\",\"agent\":%s,\"window\":%u,\"pos\":[%d,%d],\"size\":[%d,%d],"
             "\"drawable\":[%d,%d],\"cursor\":[%d,%d],\"buttons\":%u,\"queue\":%zu,\"frames\":%lu,"
             "\"injected\":%lu,\"dropped\":%lu,\"warps\":%lu,\"hold\":%d}",
             BG3SDL_VERSION, atomic_load(&g_agent) ? "true" : "false", atomic_load(&g_window_id),
             atomic_load(&g_wx), atomic_load(&g_wy), atomic_load(&g_ww), atomic_load(&g_wh),
             atomic_load(&g_dw), atomic_load(&g_dh), atomic_load(&g_cx), atomic_load(&g_cy),
             atomic_load(&g_buttons), qlen, atomic_load(&g_frames), atomic_load(&g_injected),
             atomic_load(&g_dropped), atomic_load(&g_warps), g_hold);
}

static void do_sync(long ms, char *reply, size_t cap) {
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += ms / 1000;
    deadline.tv_nsec += (ms % 1000) * 1000000L;
    if (deadline.tv_nsec >= 1000000000L) {
        deadline.tv_sec++;
        deadline.tv_nsec -= 1000000000L;
    }
    pthread_mutex_lock(&g_qlock);
    unsigned long target = g_qposted;
    int rc = 0;
    while (g_qdelivered < target && rc != ETIMEDOUT) rc = pthread_cond_timedwait(&g_qempty, &g_qlock, &deadline);
    int done = g_qdelivered >= target;
    size_t left = g_qlen;
    pthread_mutex_unlock(&g_qlock);
    if (done) snprintf(reply, cap, "ok");
    else snprintf(reply, cap, "err timeout: %zu steps still queued (is the game polling events?)", left);
}

/* One command line -> one reply line (without the newline). */
static void handle_command(char *line, char *reply, size_t cap) {
    /* "text" keeps the rest of the line verbatim, so take it before tokenizing splits it. */
    char raw[LINE_MAX_LEN];
    snprintf(raw, sizeof raw, "%s", line);
    char *tok[8];
    char *rest;
    int n = tokenize(line, tok, 8, &rest);
    if (rest) rest = raw + (rest - line);
    if (n == 0) {
        snprintf(reply, cap, "err empty command");
        return;
    }
    const char *cmd = tok[0];
    batch_t *b = calloc(1, sizeof *b);
    if (!b) {
        snprintf(reply, cap, "err out of memory");
        return;
    }
    int x, y, count = 1, notches;

    if (!strcasecmp(cmd, "ping")) {
        snprintf(reply, cap, "ok pong");
    } else if (!strcasecmp(cmd, "status")) {
        status_json(reply, cap);
    } else if (!strcasecmp(cmd, "agent")) {
        if (n == 2 && !strcasecmp(tok[1], "on")) {
            atomic_store(&g_agent, 1);
            snprintf(reply, cap, "ok agent on");
        } else if (n == 2 && !strcasecmp(tok[1], "off")) {
            atomic_store(&g_agent, 0);
            snprintf(reply, cap, "ok agent off");
        } else {
            snprintf(reply, cap, "err usage: agent on|off");
        }
    } else if (!strcasecmp(cmd, "clear")) {
        pthread_mutex_lock(&g_qlock);
        size_t dropped = g_qlen;
        g_qdelivered += g_qlen;
        g_qhead = 0;
        g_qlen = 0;
        atomic_store(&g_qnonempty, 0);
        pthread_cond_broadcast(&g_qempty);
        pthread_mutex_unlock(&g_qlock);
        snprintf(reply, cap, "ok cleared %zu", dropped);
    } else if (!strcasecmp(cmd, "sync")) {
        int ms = 5000;
        if (n >= 2 && (!parse_int(tok[1], &ms) || ms < 0)) {
            snprintf(reply, cap, "err usage: sync [ms]");
        } else {
            do_sync(ms, reply, cap);
        }
    } else if (!strcasecmp(cmd, "move")) {
        if (n != 3 || !parse_int(tok[1], &x) || !parse_int(tok[2], &y)) {
            snprintf(reply, cap, "err usage: move X Y");
        } else {
            atomic_store(&g_agent, 1);
            add_move(b, x, y);
            add_wait(b, 1);
        }
    } else if (!strcasecmp(cmd, "click")) {
        int button = n >= 2 ? button_of(tok[1]) : 0;
        if (n < 4 || n > 5 || !button || !parse_int(tok[2], &x) || !parse_int(tok[3], &y) ||
            (n == 5 && (!parse_int(tok[4], &count) || count < 1 || count > 3))) {
            snprintf(reply, cap, "err usage: click L|R|M|X1|X2 X Y [1-3]");
        } else {
            atomic_store(&g_agent, 1);
            add_move(b, x, y);
            add_wait(b, g_hold); /* let hover and picking catch up before pressing */
            for (int i = 1; i <= count; i++) {
                add_button(b, 1, button, i);
                add_wait(b, g_hold);
                add_button(b, 0, button, i);
                add_wait(b, 1);
            }
        }
    } else if (!strcasecmp(cmd, "mdown") || !strcasecmp(cmd, "mup")) {
        int down = !strcasecmp(cmd, "mdown");
        int button = n >= 2 ? button_of(tok[1]) : 0;
        if (!button || (n != 2 && n != 4) ||
            (n == 4 && (!parse_int(tok[2], &x) || !parse_int(tok[3], &y)))) {
            snprintf(reply, cap, "err usage: %s L|R|M|X1|X2 [X Y]", cmd);
        } else {
            atomic_store(&g_agent, 1);
            if (n == 4) {
                add_move(b, x, y);
                add_wait(b, 1);
            }
            add_button(b, down, button, 1);
            add_wait(b, g_hold);
        }
    } else if (!strcasecmp(cmd, "wheel")) {
        if ((n != 2 && n != 4) || !parse_int(tok[1], &notches) || notches == 0 || notches < -100 || notches > 100 ||
            (n == 4 && (!parse_int(tok[2], &x) || !parse_int(tok[3], &y)))) {
            snprintf(reply, cap, "err usage: wheel N [X Y]");
        } else {
            atomic_store(&g_agent, 1);
            if (n == 4) {
                add_move(b, x, y);
                add_wait(b, 1);
            }
            int step = notches > 0 ? 1 : -1;
            for (int i = 0; i != notches; i += step) {
                step_t *s = batch_add(b);
                s->kind = STEP_EVENT;
                s->ev.type = SDL_MOUSEWHEEL;
                s->ev.wheel.y = step;
                s->ev.wheel.preciseY = (float)step;
                s->ev.wheel.direction = SDL_MOUSEWHEEL_NORMAL;
                add_wait(b, 1);
            }
        }
    } else if (!strcasecmp(cmd, "key") || !strcasecmp(cmd, "down") || !strcasecmp(cmd, "up")) {
        if (n != 2) {
            snprintf(reply, cap, "err usage: %s KEY%s", cmd, strcasecmp(cmd, "key") ? "" : "[+KEY...]");
        } else {
            SDL_Scancode keys[8];
            int nk = 0, bad = 0;
            char *save = NULL;
            for (char *k = strtok_r(tok[1], "+", &save); k; k = strtok_r(NULL, "+", &save)) {
                if (nk == 8) { bad = 1; break; }
                keys[nk] = scancode_of(k);
                if (keys[nk] == SDL_SCANCODE_UNKNOWN) {
                    snprintf(reply, cap, "err unknown key '%s'", k);
                    bad = 2;
                    break;
                }
                nk++;
            }
            if (bad == 2) {
                /* reply already names the unknown key */
            } else if (bad == 1 || (nk > 1 && strcasecmp(cmd, "key"))) {
                snprintf(reply, cap, "err too many keys");
            } else if (nk == 0) {
                snprintf(reply, cap, "err no key");
            } else {
                if (!strcasecmp(cmd, "down")) {
                    add_key(b, 1, keys[0]);
                    add_wait(b, 1);
                } else if (!strcasecmp(cmd, "up")) {
                    add_key(b, 0, keys[0]);
                    add_wait(b, 1);
                } else {
                    for (int i = 0; i < nk; i++) {
                        add_key(b, 1, keys[i]);
                        add_wait(b, 1);
                    }
                    add_wait(b, g_hold > 1 ? g_hold - 1 : 0);
                    for (int i = nk - 1; i >= 0; i--) {
                        add_key(b, 0, keys[i]);
                        add_wait(b, 1);
                    }
                }
            }
        }
    } else if (!strcasecmp(cmd, "text")) {
        if (!rest || !*rest) {
            snprintf(reply, cap, "err usage: text STRING");
        } else {
            /* SDL_TEXTINPUT carries at most 31 bytes; split on UTF-8 character boundaries. */
            size_t len = strlen(rest), pos = 0;
            while (pos < len) {
                size_t take = len - pos;
                if (take > SDL_TEXTINPUTEVENT_TEXT_SIZE - 1) {
                    take = SDL_TEXTINPUTEVENT_TEXT_SIZE - 1;
                    while (take > 0 && ((unsigned char)rest[pos + take] & 0xC0) == 0x80) take--;
                }
                step_t *s = batch_add(b);
                s->kind = STEP_EVENT;
                s->ev.type = SDL_TEXTINPUT;
                memcpy(s->ev.text.text, rest + pos, take);
                s->ev.text.text[take] = '\0';
                add_wait(b, 1);
                pos += take;
            }
        }
    } else if (!strcasecmp(cmd, "wait")) {
        int frames;
        if (n != 2 || !parse_int(tok[1], &frames) || frames < 1 || frames > 100000) {
            snprintf(reply, cap, "err usage: wait FRAMES");
        } else {
            add_wait(b, (unsigned long)frames);
        }
    } else {
        snprintf(reply, cap, "err unknown command '%s'", cmd);
    }

    if (b->n) {
        if (b->overflow) snprintf(reply, cap, "err command too long");
        else if (queue_push(b->steps, b->n)) snprintf(reply, cap, "err queue full");
        else snprintf(reply, cap, "ok queued %zu", b->n);
    }
    free(b);
}

/* ---- control socket ---- */

static void *client_thread(void *arg) {
    int fd = (int)(intptr_t)arg;
    char buf[LINE_MAX_LEN];
    size_t used = 0;
    for (;;) {
        ssize_t r = recv(fd, buf + used, sizeof buf - 1 - used, 0);
        if (r <= 0) break;
        used += (size_t)r;
        char *start = buf, *nl;
        while ((nl = memchr(start, '\n', used - (size_t)(start - buf)))) {
            *nl = '\0';
            if (nl > start && nl[-1] == '\r') nl[-1] = '\0';
            char reply[1024];
            handle_command(start, reply, sizeof reply - 1);
            size_t rl = strlen(reply);
            reply[rl++] = '\n';
            if (send(fd, reply, rl, MSG_NOSIGNAL) < 0) goto done;
            start = nl + 1;
        }
        used -= (size_t)(start - buf);
        memmove(buf, start, used);
        if (used == sizeof buf - 1) {
            static const char msg[] = "err line too long\n";
            send(fd, msg, sizeof msg - 1, MSG_NOSIGNAL);
            used = 0;
        }
    }
done:
    close(fd);
    return NULL;
}

static void *listener_thread(void *arg) {
    int srv = (int)(intptr_t)arg;
    for (;;) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) {
            if (errno == EINTR) continue;
            logmsg("accept failed: %s", strerror(errno));
            break;
        }
        pthread_t t;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
        if (pthread_create(&t, &attr, client_thread, (void *)(intptr_t)fd)) close(fd);
        pthread_attr_destroy(&attr);
    }
    close(srv);
    return NULL;
}

static pthread_once_t g_listener_once = PTHREAD_ONCE_INIT;

static void listener_init(void) {
    const char *env = getenv("BG3AI_INPUT_AGENT");
    if (env && env[0] == '1') atomic_store(&g_agent, 1);
    env = getenv("BG3AI_INPUT_HOLD");
    if (env) {
        int h;
        if (parse_int(env, &h) && h >= 1 && h <= 60) g_hold = h;
    }

    const char *name = getenv("BG3AI_INPUT_SOCKET");
    if (!name || !*name) name = "bg3ai-input";
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    size_t len = strlen(name);
    if (len > sizeof addr.sun_path - 2) len = sizeof addr.sun_path - 2;
    memcpy(addr.sun_path + 1, name, len); /* abstract: leading NUL, shared across the sniper container */
    socklen_t alen = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + len);

    int srv = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (srv < 0 || bind(srv, (struct sockaddr *)&addr, alen) || listen(srv, 8)) {
        logmsg("cannot listen on @%.*s: %s (another instance?)", (int)len, name, strerror(errno));
        if (srv >= 0) close(srv);
        return;
    }
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&t, &attr, listener_thread, (void *)(intptr_t)srv)) {
        logmsg("cannot start listener thread");
        close(srv);
    } else {
        logmsg("%s listening on @%.*s (pid %d, agent %s)", BG3SDL_VERSION, (int)len, name, (int)getpid(),
               atomic_load(&g_agent) ? "on" : "off");
    }
    pthread_attr_destroy(&attr);
}

/* Lazily, from the first SDL_PollEvent: only a process that actually polls SDL events gets a listener,
 * not every helper that inherits LD_PRELOAD. */
static void start_listener(void) { pthread_once(&g_listener_once, listener_init); }
