# pyright: basic
"""Which save a playthrough plays on (moved from runner/play.py): HANDOFF.md's save line, the agent's Sxxx- labels
behind $BG3AI_SAVE_PREFIX, the playthrough's play windows, and the candidates and pick (pick_save, pick_save_why).
Pure functions over Save lists and the playthrough's history; the runner's Runner.choose_save calls them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .io import Save

SAVE_PREFIX = os.environ.get("BG3AI_SAVE_PREFIX", "")


def is_autosave(label: str) -> bool:
    return re.match(r"(?i)^(auto|quick)save_\d+$", label) is not None


HANDOFF_SAVE_RE = re.compile(r"^Save to load:\s*(.+)$", re.M)


def parse_handoff_save(text: str) -> str | None:
    """The save label HANDOFF.md asks for ('Save to load: "S042-end" — ...'), or None."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m = HANDOFF_SAVE_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    q = re.match(r'["“`\']([^"”`\']+)["”`\']', value)
    label = q.group(1) if q else re.split(r"\s+[—–-]\s+|\s+\(", value)[0]  # noqa: RUF001 - the dashes HANDOFF uses
    label = label.strip().strip("`")
    if not label or label.lower().startswith(("(none", "none", "n/a")):
        return None
    return label


def is_ai_label(label: str, prefix: str = SAVE_PREFIX) -> bool:
    """Saves the agent makes are labelled Sxxx-<slug>, optionally behind the playthrough's prefix."""
    return re.match(rf"^{re.escape(prefix)}S\d{{3}}-", label) is not None


def is_ai_save(s: Save, prefix: str = SAVE_PREFIX) -> bool:
    """An `Sxxx-` save of this playthrough. $BG3AI_SAVE_PREFIX is used two ways: as a label prefix
    (`pt1-S010-end`) and, by the MCP server and docs/SETUP.md (`BG3AI_SAVE_PREFIX=Claude-`), as a folder
    prefix: the character's name plus the dash the game adds (`Claude-123__S001-end`). The agent's
    save_game never prefixes the label, so a folder-prefix match takes a plain `Sxxx-` label."""
    if is_ai_label(s.label, prefix):
        return True
    return bool(prefix) and s.folder.startswith(prefix) and is_ai_label(s.label, "")


def label_matches(label: str, wanted: str, prefix: str = SAVE_PREFIX) -> bool:
    return label == wanted or label == prefix + wanted


def saved_key(s: Save) -> float:
    """A save's sort time: when the game wrote it (Save.saved, its meta.lsf SaveTime), never its mtime: Steam's local
    cloud restores a moved-aside or deleted save at a launch with mtime = the launch (bench B3, run_host.own_save).
    An unread time sorts oldest (save_candidates never takes such a save)."""
    return s.saved if s.saved is not None else float("-inf")


SAVE_WINDOW_SLACK_S = 120.0  # a save this close to one of the playthrough's play windows still counts as written in it


def _first_last_t(path: Path) -> tuple[float, float] | None:
    """The first and last "t" of a jsonl file (a Sxxx.turns.jsonl: the session's first and last stream event), reading
    only its head and tail; None when unreadable or without times."""
    try:
        with open(path, "rb") as f:
            head = f.readline()
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().splitlines()
    except OSError:
        return None
    ts = []
    for ln in [head, *reversed(tail)]:
        try:
            t = json.loads(ln).get("t")
        except (ValueError, AttributeError):
            continue
        if isinstance(t, (int, float)):
            ts.append(float(t))
            if len(ts) == 2:
                break
    return (min(ts), max(ts)) if ts else None


def play_windows(
    history: list[dict] | None, state_dir: Path | None, running: tuple[float, float] | None = None
) -> list[tuple[float, float]]:
    """When this playthrough was being played: its play sessions' [started, ended] (runner.json history), each
    Sxxx*.turns.jsonl's first..last event (a session the runner never recorded: it crashed mid-session) and `running`,
    the session still playing (its start..now: the app writes no turns.jsonl, and a mid-session recovery must take
    the saves it made; live9 S002 loaded S001's save over its own S002-passage). pick_save
    takes an `Sxxx-` save of the owner only when it was written in one of these: the owner (the character's name) is
    shared by other playthroughs (replay-hoh saved S027-end / S028-lastlight-prep as "Claude" on 10-15, after
    claude-run-2's S065-end), and a worker's savetest (S999-savetest) is no playthrough's."""
    out = [
        (float(h["started"]), float(h["ended"]))
        for h in history or []
        if h.get("kind") == "play"
        and isinstance(h.get("started"), (int, float))
        and isinstance(h.get("ended"), (int, float))
    ]
    if state_dir is not None:
        try:
            files = sorted(Path(state_dir).glob("S*.turns.jsonl"))
        except OSError:
            files = []
        out += [w for w in (_first_last_t(f) for f in files) if w]
    if running is not None:
        out.append(running)
    return sorted(out)


def in_windows(t: float, windows: list[tuple[float, float]], slack: float = SAVE_WINDOW_SLACK_S) -> bool:
    return any(a - slack <= t <= b + slack for a, b in windows)


def played_elsewhere(t: float, windows: list[tuple[float, float]], slack: float = SAVE_WINDOW_SLACK_S) -> bool:
    """A save time after this playthrough began (its first play window) that falls in none of its windows: written
    while another playthrough (or a worker) had the game. Older saves are judged as before (the newest wins anyway)."""
    return bool(windows) and t > windows[0][0] - slack and not in_windows(t, windows, slack)


def save_candidates(
    saves: list[Save],
    handoff_label: str | None,
    owner: str | None,
    runner_saves: set[str],
    prefix: str = SAVE_PREFIX,
    windows: list[tuple[float, float]] | None = None,
) -> list[Save]:
    """The saves of this playthrough.

    Candidates: the folder HANDOFF names, any `Sxxx-` save of the playthrough's owner, and quicksaves the
    runner itself made for this playthrough (recorded by folder). Never anything else. With `windows`
    (play_windows: when this playthrough was played; empty for a new one), a labelled save counts only when it was
    written in one of them or before the first: another playthrough with the same name, or a worker, wrote the rest.
    Written is the save's own time (Save.saved), never its mtime; a save whose time can't be read is never one.
    """
    cands = []
    for s in saves:
        if s.saved is None:
            continue  # when it was written, so whose it is, is unknown (run_host.own_save's rule)
        if s.folder in runner_saves:
            cands.append(s)
        elif is_autosave(s.label):
            continue  # AutoSave_N/QuickSave_N: the game's or a worker's test state (se 18:14 AutoSave_59), never ours
        elif owner is not None and s.owner != owner:
            continue
        elif played_elsewhere(s.saved, windows or []):
            continue  # same owner, written while another playthrough (or a worker) played
        elif (handoff_label and label_matches(s.label, handoff_label, prefix)) or (
            owner is not None and is_ai_save(s, prefix)
        ):
            cands.append(s)
    return cands


def pick_save(
    saves: list[Save],
    handoff_label: str | None,
    owner: str | None,
    runner_saves: set[str],
    prefix: str = SAVE_PREFIX,
    windows: list[tuple[float, float]] | None = None,
) -> Save | None:
    """The newest save of this playthrough (save_candidates)."""
    return max(
        save_candidates(saves, handoff_label, owner, runner_saves, prefix, windows), key=saved_key, default=None
    )


HANDOFF_SAVE_SLACK_S = 5.0  # a save this soon after the HANDOFF's session ended was still that session's


def session_ends(history: list[dict] | None, state_dir: Path | None) -> dict[int, float]:
    """When each play session ended: runner.json history "ended", or its Sxxx.turns.jsonl's last event (a session the
    runner never recorded: it crashed mid-session), whichever is later."""
    out: dict[int, float] = {}

    def put(sid: str, t: float) -> None:
        m = re.match(r"S(\d+)", sid or "")
        if m:
            n = int(m.group(1))
            out[n] = max(out.get(n, t), t)

    for h in history or []:
        if h.get("kind") == "play" and isinstance(h.get("ended"), (int, float)):
            put(str(h.get("session") or ""), float(h["ended"]))
    if state_dir is not None:
        try:
            files = sorted(Path(state_dir).glob("S*.turns.jsonl"))
        except OSError:
            files = []
        for f in files:
            w = _first_last_t(f)
            if w:
                put(f.name, w[1])
    return out


def pick_save_why(
    saves: list[Save],
    handoff_label: str | None,
    owner: str | None,
    runner_saves: set[str],
    prefix: str = SAVE_PREFIX,
    windows: list[tuple[float, float]] | None = None,
    handoff_session: int | None = None,
    ends: dict[int, float] | None = None,
) -> tuple[Save | None, str]:
    """The save a session starts from, and why. HANDOFF's "Save to load" is the agent's decision: it wins over newer
    saves unless one was written after the session that wrote HANDOFF (`handoff_session`, its heading) ended (`ends`,
    session_ends): later play HANDOFF doesn't know about (a session that crashed before its wrap-up, a runner
    quicksave), or HANDOFF's session is unknown. S076 (L8): the newest was S075-fight, saved mid-fight on try 6,
    HANDOFF (S075's) named S075-end; S076's HANDOFF named S075-end again, its S076-end being a losing fight."""
    cands = save_candidates(saves, handoff_label, owner, runner_saves, prefix, windows)
    newest = max(cands, key=saved_key, default=None)
    if newest is None:
        return None, "no save of this playthrough"
    if not handoff_label:
        return newest, "the newest save (HANDOFF names no save)"
    named = max(
        (s for s in cands if label_matches(s.label, handoff_label, prefix)), key=saved_key, default=None
    )
    if named is None:
        return newest, f'the newest save (HANDOFF names "{handoff_label}": no such save of this playthrough)'
    if named is newest:
        return named, 'HANDOFF\'s "Save to load", also the newest save'
    sid = f"S{handoff_session:03d}" if handoff_session is not None else None
    end = (ends or {}).get(handoff_session) if handoff_session is not None else None
    if end is None:
        return newest, (
            f'the newest save (HANDOFF names "{handoff_label}" but '
            + (f"{sid} never played here" if sid else "has no '# Handoff Sxxx' heading")
            + ": can't tell whether it knew the newer saves)"
        )
    if saved_key(newest) > end + HANDOFF_SAVE_SLACK_S:
        return newest, (
            f'the newest save (HANDOFF names "{handoff_label}", but "{newest.label}" was saved '
            f"after {sid}, which wrote HANDOFF, ended)"
        )
    return named, (f'HANDOFF\'s "Save to load" ({sid}\'s choice), not the newer "{newest.label}" from the same session')


def foreign_saves(
    saves: list[Save],
    owner: str | None,
    runner_saves: set[str],
    windows: list[tuple[float, float]],
    prefix: str = SAVE_PREFIX,
) -> list[str]:
    """The owner's `Sxxx-` saves written outside this playthrough's play windows: another playthrough with the same
    character name (replay-hoh's S027-end / S028-lastlight-prep as "Claude"), a worker's S999-savetest. The MCP
    server's list_saves / load_game("latest") must not offer them (BG3AI_SAVES_EXCLUDE): before claude-run-2's first
    save after a resume, "latest" was replay-hoh's Act 2 save. [] for a new playthrough (no windows)."""
    if not windows or not owner:
        return []
    return sorted(
        s.folder
        for s in saves
        if s.owner == owner
        and s.folder not in runner_saves
        and is_ai_save(s, prefix)
        and played_elsewhere(s.mtime, windows)
    )


def saves_since(saves: list[Save], t: float, owner: str | None, prefix: str = SAVE_PREFIX) -> list[Save]:
    """AI-labelled saves written after t (by this playthrough's owner, when known)."""
    return sorted(
        (s for s in saves if s.mtime >= t and is_ai_save(s, prefix) and (owner is None or s.owner == owner)),
        key=lambda s: s.mtime,
    )


SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")  # bg3bench.scenario's too


def save_lsv(save_dir: Path, folder: str) -> Path | None:
    """A save folder's .lsv: the game names it after the label, <owner>-<id>__<label>/<label>.lsv (+ <label>.WebP);
    else the folder's only .lsv (a label the game rewrote). None when there's none, or several."""
    d = save_dir / folder
    m = SAVE_FOLDER_RE.match(folder or "")
    if m and (d / f"{m.group('label')}.lsv").is_file():
        return d / f"{m.group('label')}.lsv"
    try:
        found = [p for p in d.iterdir() if p.suffix == ".lsv" and p.is_file()]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None
