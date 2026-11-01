"""The evidence video's images (Pillow), with bundled fonts so every machine draws the same pixels. Layout (1920x1080,
dark, fixed):

    y 0-80     header: the brand and the scenario's title; the pilot badge, the verdict chip, the clock against the
               cap and the cost so far
    x 24, y 96     the game, 1408x792 (compose.py overlays it)       x 1456, y 96   Agent says (440x280)
    x 24, y 904    the timeline card, 1408x96: five lanes            x 1456, y 392  Party (440x184)
    x 24, y 1016   the id line                                       x 1456, y 592  Decisions (440x464)

frame_image() draws everything but the game, Agent says and Decisions, once per second of run clock; column_image()
draws those two cards (RGBA, transparent where the party card shows through), once per panel change.
Fonts: Space Grotesk 600, Inter 400/500 and Roboto Mono 400/500/600 (SIL OFL); a character a font lacks is drawn with
DejaVu Sans (Mono).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from bg3ai.tool_classes import READ_TOOLS
from PIL import Image, ImageDraw, ImageFont

from .timeline import Gap, Item, Lanes, Member, Meta, Strip

WIDTH, HEIGHT = 1920, 1080
GAME_X, GAME_Y, GAME_W, GAME_H = 24, 96, 1408, 792
COL_X, COL_Y, COL_W, COL_H = 1456, 96, 440, 960
SAYS_H, PARTY_Y, PARTY_H, DEC_Y, DEC_H = 280, 392, 184, 592, 464  # PARTY_Y and DEC_Y are frame y
TL_X, TL_Y, TL_W, TL_H = 24, 904, 1408, 96
ID_Y = 1016
FONTS = Path(__file__).parent / "fonts"
PAD = 20
RADIUS = 12
ENDING_S = 5  # a run that didn't pass shows its ending over its last seconds (a wipe: from the wipe on)

PAPER = (10, 10, 26)
CARD = (19, 19, 42)
BORDER = (44, 44, 66)
CARD_2 = (25, 25, 54)
INK = (243, 241, 236)
INK_2 = (212, 210, 204)
INK_3 = (161, 166, 180)
TEAL = (62, 230, 218)
AMBER = (255, 179, 64)
RUST = (255, 138, 128)
TEAL_CHIP = (16, 37, 55)  # TEAL at .12 over the frame
RUST_CHIP = (35, 23, 36)  # RUST at .10 over the frame
FIGHT = (118, 89, 59)  # AMBER at .40 over CARD_2

FACES = {
    "title": "SpaceGrotesk-SemiBold.ttf",
    "sans": "Inter-Regular.ttf",
    "sans-medium": "Inter-Medium.ttf",
    "mono": "RobotoMono-Regular.ttf",
    "mono-medium": "RobotoMono-Medium.ttf",
    "mono-bold": "RobotoMono-SemiBold.ttf",
}
LANE_NAMES = ("FIGHT", "KILLS", "DOWNS", "TOOL FAILS", "AGENT TEXT")
LANE_PITCH, LANE_H = 17, 13  # five lanes in the 96 px card


@cache
def font(face: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / FACES[face]), size)


@cache
def _fallback(face: str, size: int) -> ImageFont.FreeTypeFont:
    name = "DejaVuSansMono.ttf" if face.startswith("mono") else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONTS / name), size)


@cache
def _has(f: ImageFont.FreeTypeFont, ch: str) -> bool:
    """Whether f has a glyph for ch (what it draws differs from its missing-glyph box)."""
    if ch.isspace():
        return True
    return _glyph(f, ch) != _glyph(f, "\U000f0000")


@cache
def _glyph(f: ImageFont.FreeTypeFont, ch: str) -> bytes:
    img = Image.new("L", (int(f.size) * 2, int(f.size) * 2))
    ImageDraw.Draw(img).text((0, 0), ch, font=f, fill=255)
    return img.tobytes()


def _runs(text: str, face: str, size: int) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """text cut where the font changes: the face's, or DejaVu for the characters it lacks."""
    f, fb = font(face, size), _fallback(face, size)
    out: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for ch in text:
        use = f if _has(f, ch) else fb
        if out and out[-1][1] is use:
            out[-1] = (out[-1][0] + ch, use)
        else:
            out.append((ch, use))
    return out


def length(text: str, face: str, size: int, track: float = 0.0) -> float:
    return sum(f.getlength(run) for run, f in _runs(text, face, size)) + track * len(text)


def text(
    d: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    s: str,
    face: str,
    size: int,
    fill: tuple[int, ...],
    anchor: str = "ls",
    track: float = 0.0,
) -> float:
    """s drawn from xy (anchor's first letter: l or r; second: Pillow's vertical anchor); track adds that many px
    after every character. Returns the right edge."""
    w = length(s, face, size, track)
    x, y = xy
    if anchor[0] == "r":
        x -= w
    a = "l" + anchor[1]
    for run, f in _runs(s, face, size):
        if track:
            for ch in run:
                d.text((x, y), ch, font=f, fill=fill, anchor=a)
                x += f.getlength(ch) + track
        else:
            d.text((x, y), run, font=f, fill=fill, anchor=a)
            x += f.getlength(run)
    return x


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def minutes(seconds: float) -> str:
    """mm:ss with the minutes past 59 (the header's clock and cap: `75:02 / 90:00`)."""
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def short_clock(seconds: float) -> str:
    """m:ss (h:mm:ss past an hour), the verdict's and the party's time."""
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def wrap(s: str, face: str, size: int, width: float, most: int) -> list[str]:
    """s in lines no wider than width (words split only when one alone is too wide), at most `most` lines, the
    last one ending in an ellipsis when s was cut."""
    lines: list[str] = []
    for para in s.splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}" if line else word
            if length(cand, face, size) <= width:
                line = cand
                continue
            if line:
                lines.append(line)
            while length(word, face, size) > width:  # a word wider than the line: cut it
                k = max(1, int(len(word) * width / length(word, face, size)))
                lines.append(word[:k])
                word = word[k:]
            line = word
        lines.append(line)
    lines = [ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1])]  # no runs of blank lines
    if len(lines) > most:
        lines = lines[:most]
        last = lines[-1]
        while last and length(last + " …", face, size) > width:
            last = last[:-1]
        lines[-1] = last + " …"
    return lines


def clip(s: str, face: str, size: int, width: float) -> str:
    """s on one line, cut with an ellipsis when wider than width."""
    s = " ".join(s.split())
    if length(s, face, size) <= width:
        return s
    while s and length(s + "…", face, size) > width:
        s = s[:-1]
    return s + "…"


def card(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    d.rounded_rectangle(box, RADIUS, fill=CARD, outline=BORDER, width=1)


def label(d: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, fill: tuple[int, ...]) -> None:
    """A card's label: Roboto Mono 500 16 caps, +0.08 em."""
    text(d, xy, s.upper(), "mono-medium", 16, fill, "la", track=1.3)


def hp_colour(frac: float) -> tuple[int, int, int]:
    return TEAL if frac > 0.5 else AMBER if frac >= 0.25 else RUST


def verdict(s: Strip, meta: Meta, duration_s: float) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """The verdict chip at this second: (text, ink, chip fill)."""
    if s.passed_s is not None and s.clock_s >= s.passed_s:
        return f"PASSED {short_clock(s.passed_s)}", TEAL, TEAL_CHIP
    wipe = s.wiped_s if meta.ending.upper() == "WIPED" and s.wiped_s is not None else None
    if meta.ending and s.clock_s >= (duration_s - ENDING_S if wipe is None else wipe):
        return meta.ending.upper(), RUST, RUST_CHIP
    return "GOAL: NOT YET", INK_3, CARD_2


def _header(d: ImageDraw.ImageDraw, s: Strip, meta: Meta, duration_s: float) -> None:
    d.rectangle((24, 34, 35, 45), outline=TEAL, width=2)  # the brand node
    x = text(d, (46, 40), "bg3 bench", "title", 26, INK, "lm")
    right = WIDTH - 24
    cap = f" / {minutes(meta.cap_s)}" if meta.cap_s else ""
    cap_x = text(d, (right, 50), cap, "mono", 26, INK_3, "rs") - length(cap, "mono", 26) if cap else right
    clock_x = text(d, (cap_x, 50), clock(s.clock_s), "mono-bold", 48, INK, "rs") - length(
        clock(s.clock_s), "mono-bold", 48
    )
    if s.cost_usd is not None:
        text(d, (right, 58), f"${s.cost_usd:.2f} {meta.cost_kind}", "mono", 20, INK_3, "ra")
    word, ink, fill = verdict(s, meta, duration_s)
    chip_w = length(word, "mono-bold", 30) + 32
    chip_x = clock_x - 24 - chip_w
    d.rounded_rectangle((chip_x, 16, chip_x + chip_w, 64), 8, fill=fill)
    text(d, (chip_x + 16, 40), word, "mono-bold", 30, ink, "lm")
    left = chip_x
    if meta.pilot:
        pill_w = length("pilot", "mono-medium", 22) + 56
        px = chip_x - 16 - pill_w
        d.rounded_rectangle((px, 22, px + pill_w, 58), 18, outline=AMBER, width=2)
        cx, cy = px + 22, 40
        d.polygon(((cx, cy - 7), (cx + 7, cy), (cx, cy + 7), (cx - 7, cy)), outline=AMBER, width=2)
        text(d, (px + 38, 40), "pilot", "mono-medium", 22, AMBER, "lm")
        left = px
    if meta.title:
        text(d, (x + 16, 40), clip(meta.title, "title", 30, left - 24 - x - 16), "title", 30, INK_2, "lm")


def _party(d: ImageDraw.ImageDraw, s: Strip) -> None:
    x0, y0 = COL_X, PARTY_Y
    card(d, (x0, y0, x0 + COL_W - 1, y0 + PARTY_H - 1))
    at = f" · as of {short_clock(s.party_at_s)}" if s.party_at_s is not None else ""
    label(d, (x0 + PAD, y0 + 18), "Party" + at, INK_3)
    members: Sequence[Member] = s.party[:4]
    for i, m in enumerate(members):
        y = y0 + 64 + i * 30
        name = clip(m.name.split(" ")[0] + (" down" if m.down else ""), "title", 22, 150)
        text(d, (x0 + PAD, y), name, "title", 22, RUST if m.down else INK, "lm")
        text(d, (x0 + 252, y), f"{m.hp}/{m.max_hp}", "mono", 20, INK_2, "rm")
        bx0, bx1 = x0 + 268, x0 + COL_W - PAD
        d.rounded_rectangle((bx0, y - 5, bx1, y + 5), 5, fill=CARD_2)
        frac = 0.0 if m.max_hp <= 0 else max(0.0, min(1.0, m.hp / m.max_hp))
        if frac > 0:
            d.rounded_rectangle((bx0, y - 5, bx0 + max(10, int((bx1 - bx0) * frac)), y + 5), 5, fill=hp_colour(frac))


def lanes_image(lanes: Lanes, duration_s: float) -> Image.Image:
    """The timeline card without its playhead (it is the same every second)."""
    img = Image.new("RGB", (TL_W, TL_H), PAPER)
    d = ImageDraw.Draw(img)
    card(d, (0, 0, TL_W - 1, TL_H - 1))
    x0, x1 = 16 + 130, TL_W - 16
    top = (TL_H - (4 * LANE_PITCH + LANE_H)) // 2

    def x(at: float) -> int:
        return x0 + round((x1 - x0) * max(0.0, min(1.0, at / duration_s))) if duration_s > 0 else x0

    for i, name in enumerate(LANE_NAMES):
        y = top + i * LANE_PITCH
        text(d, (16, y + LANE_H / 2), name, "mono-medium", 12, INK_3, "lm", track=1.0)
        d.rectangle((x0, y, x1, y + LANE_H - 1), fill=CARD_2)
    mid = [top + i * LANE_PITCH + LANE_H // 2 for i in range(5)]
    for a, b in lanes.fights:
        d.rectangle((x(a), top, max(x(a) + 1, x(b)), top + LANE_H - 1), fill=FIGHT)
    for at, goal in lanes.kills:
        cx, cy, r = x(at), mid[1], 6
        d.polygon(((cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)), fill=TEAL if goal else INK_2)
    for at in lanes.downs:
        cx, y = x(at), top + 2 * LANE_PITCH
        d.polygon(((cx - 6, y + LANE_H - 1), (cx + 6, y + LANE_H - 1), (cx, y)), fill=RUST)
    for at in lanes.fails:
        y = top + 3 * LANE_PITCH
        d.rectangle((x(at) - 1, y, x(at) + 1, y + LANE_H - 1), fill=RUST)
    for at in lanes.texts:
        y = top + 4 * LANE_PITCH
        d.rectangle((x(at), y, x(at) + 1, y + LANE_H - 1), fill=INK)
    return img


def pause_card(d: ImageDraw.ImageDraw, g: Gap) -> None:
    """A logged gap in the recording, where the game shows: why and how long (it holds for the gap's real length)."""
    card(d, (GAME_X, GAME_Y, GAME_X + GAME_W, GAME_Y + GAME_H))
    cx, cy = GAME_X + GAME_W / 2, GAME_Y + GAME_H / 2

    def centred(y: float, s: str, face: str, size: int, fill: tuple[int, ...]) -> None:
        text(d, (cx - length(s, face, size) / 2, y), s, face, size, fill, "lm")

    centred(cy - 60, "RECORDING PAUSED", "title", 44, AMBER)
    for k, ln in enumerate(wrap(g.reason, "mono", 26, GAME_W - 160, 2)):
        centred(cy + 4 + 36 * k, ln, "mono", 26, INK)
    centred(cy + 100, f"{g.length:.1f} s, run {clock(g.at_s)} to {clock(g.end_s)}", "mono", 22, INK_3)


def frame_image(s: Strip, meta: Meta, lanes: Image.Image, duration_s: float, pause: Gap | None = None) -> Image.Image:
    """The frame at one second of run clock, but the game and the column's two cards: the header, the party card,
    the timeline (lanes_image) with the playhead at this second, the id line; in a logged gap, its pause card where
    the game shows (the game is off over the gap)."""
    img = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    d = ImageDraw.Draw(img)
    if pause is not None:
        pause_card(d, pause)
    _header(d, s, meta, duration_s)
    _party(d, s)
    img.paste(lanes, (TL_X, TL_Y))
    x0, x1 = TL_X + 16 + 130, TL_X + TL_W - 16
    px = x0 + round((x1 - x0) * min(1.0, s.clock_s / duration_s)) if duration_s > 0 else x0
    d.rectangle((px - 1, TL_Y + 6, px, TL_Y + TL_H - 7), fill=TEAL)
    left, site = id_parts(meta, TL_W)
    text(d, (TL_X, ID_Y + 20), left, "mono", 24, INK_3, "lm")
    if site:
        text(d, (TL_X + TL_W, ID_Y + 20), site, "mono", 24, INK_3, "rm")
    return img


def _groups(items: Sequence[Item]) -> list[tuple[Item, Item | None]]:
    """The decisions' rows, oldest first: each call with its result (by the call's id) when that has come, then
    every other entry on its own."""
    calls = {i.ref for i in items if i.kind == "call" and i.ref}
    results = {i.ref: i for i in items if i.kind in ("result", "error") and i.ref in calls}
    return [
        (i, results.get(i.ref) if i.kind == "call" else None)
        for i in items
        if not (i.ref in results and i.kind != "call")
    ]


def column_image(items: Sequence[Item]) -> Image.Image:
    """The right column's Agent says and Decisions cards showing `items` (all shown so far, oldest first): the last
    agent text and its time on top, the newest decisions at the bottom. Transparent where the party card goes."""
    img = Image.new("RGBA", (COL_W, COL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    card(d, (0, 0, COL_W - 1, SAYS_H - 1))
    said = next((i for i in reversed(items) if i.kind == "text"), None)
    label(d, (PAD, 18), "Agent says" + (f" · {clock(said.at_s)}" if said else ""), TEAL)
    if said:
        for k, ln in enumerate(wrap(said.text, "sans-medium", 30, COL_W - 2 * PAD, 5)):
            text(d, (PAD, 52 + k * 38), ln, "sans-medium", 30, INK, "la")
    top = DEC_Y - COL_Y
    card(d, (0, top, COL_W - 1, top + DEC_H - 1))
    label(d, (PAD, top + 18), "Decisions · visible text only", INK_3)
    col = PAD + 60  # after the time
    width = COL_W - PAD - col
    y = top + DEC_H - 14
    floor = top + 48
    for it, res in reversed(_groups(items)):
        if it.kind == "text":
            lines = wrap(it.text, "sans", 22, width, 3)
            h = 28 * len(lines)
        else:
            lines = []
            h = 26 + (24 if res else 0) if it.kind == "call" else 24
        if y - h < floor:
            break
        y -= h
        row = y
        text(d, (PAD, row + 4), clock(it.at_s), "mono", 16, INK_3, "la")
        if it.kind == "text":
            for k, ln in enumerate(lines):
                text(d, (col, row + k * 28), ln, "sans", 22, INK, "la")
        elif it.kind == "call":
            end = text(d, (col, row + 2), it.tool, "mono-medium", 20, INK_3 if it.tool in READ_TOOLS else TEAL, "la")
            if it.text:
                text(d, (end + 10, row + 4), clip(it.text, "mono", 18, COL_W - PAD - end - 10), "mono", 18, INK_3, "la")
            if res:
                _result(d, res, col, row + 27, width)
        else:
            _result(d, it, col, row + 2, width)
        y -= 8
    return img


def _result(d: ImageDraw.ImageDraw, it: Item, x: float, y: float, width: float) -> None:
    failed = it.kind == "error"
    s = ("✕ " if failed else "") + it.text
    text(d, (x, y), clip(s, "mono", 18, width), "mono", 18, RUST if failed else INK_2, "la")


def id_line(meta: Meta) -> str:
    """The id line: the run id, the run hash's first 12 hex digits and the site."""
    return " · ".join(x for x in (f"run {meta.run_id}", f"hash {meta.run_sha256[:12]}", meta.site) if x)


ID_GAP = 32  # room kept between the run id and hash on the left and the site on the right


def middle_clip(s: str, face: str, size: int, width: float) -> str:
    """s cut in the middle with an ellipsis until it is no wider than width."""
    if length(s, face, size) <= width:
        return s
    keep = len(s)
    while keep > 1:
        keep -= 1
        head = (keep + 1) // 2
        cut = s[:head] + "…" + s[len(s) - (keep - head) :]
        if length(cut, face, size) <= width:
            return cut
    return "…"


def id_parts(meta: Meta, width: float) -> tuple[str, str]:
    """The id line as (left, site): the run id and hash on the left, the site right-aligned. The site always
    shows whole; the run id is shortened in the middle when the two would not fit in width."""
    site = meta.site
    room = width - (length(site, "mono", 24) + ID_GAP if site else 0)
    tail = f" · hash {meta.run_sha256[:12]}"
    run_id = middle_clip(meta.run_id, "mono", 24, room - length("run " + tail, "mono", 24))
    return f"run {run_id}{tail}", site
