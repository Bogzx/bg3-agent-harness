"""The evidence video's panels as images (Pillow), with the bundled DejaVu fonts so every machine draws the same
pixels. Layout (1920x1080, fixed): the game at x 0-1280, y 0-720 (the recording, unscaled); the strip under it,
y 720-1040, and the id line, y 1040-1080 (one image, STRIP_W x STRIP_H); the side panel at x 1280-1920 (SIDE_W x
SIDE_H): the newest entries at the bottom, each with its run clock time.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .timeline import Item, Meta, Strip

WIDTH, HEIGHT = 1920, 1080
GAME_W, GAME_H = 1280, 720
STRIP_W, STRIP_H = GAME_W, HEIGHT - GAME_H  # the strip (320 px) and the id line (40 px)
ID_H = 40
SIDE_W, SIDE_H = WIDTH - GAME_W, HEIGHT
FONTS = Path(__file__).parent / "fonts"
PAD = 16

BG = (17, 20, 24)
FG = (232, 234, 237)
DIM = (140, 146, 156)
ACCENT = (122, 182, 255)
ERROR = (255, 120, 110)
GOOD = (110, 210, 140)
BAR_BG = (60, 64, 72)
ID_BG = (0, 0, 0)

# per kind: (font, colour, prefix, most lines)
KINDS = {
    "call": ("mono", ACCENT, "▸ ", 3),
    "result": ("mono", DIM, "↳ ", 1),
    "error": ("mono", ERROR, "↳ ", 2),
    "text": ("sans", FG, "", 8),
}


@cache
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    name = "DejaVuSansMono.ttf" if kind == "mono" else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONTS / name), size)


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def wrap(text: str, f: ImageFont.FreeTypeFont, width: float, most: int) -> list[str]:
    """text in lines no wider than width (words split only when one alone is too wide), at most `most` lines, the
    last one ending in an ellipsis when text was cut."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}" if line else word
            if f.getlength(cand) <= width:
                line = cand
                continue
            if line:
                lines.append(line)
            while f.getlength(word) > width:  # a word wider than the line: cut it
                k = max(1, int(len(word) * width / f.getlength(word)))
                lines.append(word[:k])
                word = word[k:]
            line = word
        lines.append(line)
    lines = [ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1])]  # no runs of blank lines
    if len(lines) > most:
        lines = lines[:most]
        last = lines[-1]
        while last and f.getlength(last + " …") > width:
            last = last[:-1]
        lines[-1] = last + " …"
    return lines


def side_image(items: Sequence[Item], size: int = 18) -> Image.Image:
    """The side panel showing `items` (all shown so far, oldest first): as many of the newest as fit, the newest at
    the bottom."""
    img = Image.new("RGB", (SIDE_W, SIDE_H), BG)
    d = ImageDraw.Draw(img)
    head = font("sans", 20)
    d.text((PAD, PAD), "agent: tool calls and visible text", font=head, fill=DIM)
    top = PAD + 36
    d.line((PAD, top - 8, SIDE_W - PAD, top - 8), fill=BAR_BG, width=1)
    stamp_f = font("mono", size - 4)
    stamp_w = stamp_f.getlength("00:00 ")
    width = SIDE_W - 2 * PAD - stamp_w
    y = SIDE_H - PAD
    for it in reversed(items):
        face, colour, prefix, most = KINDS[it.kind]
        f = font(face, size)
        lines = wrap(prefix + it.text, f, width, most)
        line_h = size + 6
        h = line_h * len(lines) + 8
        if y - h < top:
            break
        y -= h
        d.text((PAD, y + 3), clock(it.at_s), font=stamp_f, fill=DIM)
        for i, ln in enumerate(lines):
            d.text((PAD + stamp_w, y + i * line_h), ln, font=f, fill=colour)
    return img


def _gold(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def strip_image(s: Strip, meta: Meta) -> Image.Image:
    """The strip at one second of run clock: the scenario's title and task; the clock against the cap, the cost so
    far, the gold and the goal; the party's HP bars; under it the id line (run id, run hash, site)."""
    img = Image.new("RGB", (STRIP_W, STRIP_H), BG)
    d = ImageDraw.Draw(img)
    width = STRIP_W - 2 * PAD
    title_f, task_f, info_f = font("sans", 28), font("sans", 20), font("sans", 24)
    y = PAD
    d.text((PAD, y), wrap(meta.title or meta.run_id, title_f, width, 1)[0], font=title_f, fill=FG)
    y += 38
    for ln in wrap(meta.task, task_f, width, 2):
        d.text((PAD, y), ln, font=task_f, fill=DIM)
        y += 26
    y = PAD + 38 + 2 * 26 + 18
    cap = f" / {clock(meta.cap_s)}" if meta.cap_s else ""
    parts: list[tuple[str, tuple[int, int, int]]] = [(clock(s.clock_s) + cap, FG)]
    if s.cost_usd is not None:
        parts.append((f"${s.cost_usd:.2f} {meta.cost_kind}", FG))
    if s.gold is not None:
        start = f"{_gold(s.gold_start)} → " if s.gold_start is not None else ""
        parts.append((f"gold {start}{_gold(s.gold)}", FG))
    if s.passed_s is not None and s.clock_s >= s.passed_s:
        parts.append((f"PASSED {clock(s.passed_s)}", GOOD))
    else:
        parts.append(("goal: not yet", DIM))
    x = PAD
    for text, colour in parts:
        d.text((x, y), text, font=info_f, fill=colour)
        x += int(info_f.getlength(text)) + 36
    y += 48
    if s.party:
        name_f = font("sans", 18)
        slot = width // len(s.party)
        for i, m in enumerate(s.party):
            x0 = PAD + i * slot
            bar_w = slot - 24
            d.text((x0, y), f"{m.name} {m.hp}/{m.max_hp}", font=name_f, fill=ERROR if m.down else FG)
            d.rectangle((x0, y + 28, x0 + bar_w, y + 44), fill=BAR_BG)
            frac = 0.0 if m.max_hp <= 0 else max(0.0, min(1.0, m.hp / m.max_hp))
            if frac > 0:
                d.rectangle((x0, y + 28, x0 + int(bar_w * frac), y + 44), fill=GOOD if frac > 0.5 else ERROR)
    id_y = STRIP_H - ID_H
    d.rectangle((0, id_y, STRIP_W, STRIP_H), fill=ID_BG)
    id_f = font("mono", 28)
    d.text((PAD, id_y + 4), wrap(id_line(meta), id_f, width, 1)[0], font=id_f, fill=FG)
    return img


def id_line(meta: Meta) -> str:
    """The strip's last line: the run id, the run hash's first 12 hex digits and the site."""
    return " · ".join(x for x in (f"run {meta.run_id}", f"hash {meta.run_sha256[:12]}", meta.site) if x)
