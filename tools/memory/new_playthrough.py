#!/usr/bin/env python3
"""Create a new playthrough memory from memory-template/ and point play/memory at it.

  new_playthrough.py NAME                      → playthroughs/NAME/memory, play/memory → it
  new_playthrough.py NAME --memory-dir DIR     → DIR instead of playthroughs/NAME/memory
  new_playthrough.py NAME --no-link            → don't touch play/memory

The memory dir becomes its own git repository with an initial commit "S000 init NAME".
An existing play/memory symlink is replaced (the old target is printed); a real directory or file
at play/memory is never replaced.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint as L  # noqa: E402
import memlib as M  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SetupError(Exception):
    pass


def create_memory(name: str, dest: Path, template: Path = M.TEMPLATE_DIR) -> str:
    """Copy the template to dest, fill placeholders, git init + initial commit. Returns the commit hash."""
    if not template.is_dir():
        raise SetupError(f"template not found: {template}")
    if dest.exists() and any(dest.iterdir()):
        raise SetupError(f"{dest} already exists and is not empty; pick another name or --memory-dir")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # CHARACTER.md is written in the character-creation phase (its format is in the playbook): no stub.
    shutil.copytree(template, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(L.CHARACTER_FILE))
    created = M.now_local().strftime("%Y-%m-%d")
    for p in dest.rglob("*.md"):
        t = p.read_text()
        if "{{" in t:
            p.write_text(t.replace("{{PLAYTHROUGH_NAME}}", name).replace("{{CREATED}}", created))
    M.git(dest, "init", "-q")
    h = M.git_commit_all(dest, f"S000 init {name}")
    return h or ""


def link_play_memory(play_dir: Path, dest: Path) -> str | None:
    """Make play_dir/memory a relative symlink to dest. Returns the previous symlink target, if any."""
    link = play_dir / "memory"
    previous = None
    if link.is_symlink():
        previous = os.readlink(link)
        link.unlink()
    elif link.exists():
        raise SetupError(f"{link} exists and is not a symlink; move it away first (it may hold a playthrough)")
    play_dir.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(dest.resolve(), play_dir.resolve())
    link.symlink_to(rel, target_is_directory=True)
    return previous


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="playthrough name (letters, digits, . _ -)")
    ap.add_argument("--memory-dir", help="create the memory here instead of playthroughs/NAME/memory")
    ap.add_argument("--no-link", action="store_true", help="don't create/replace the play/memory symlink")
    ap.add_argument("--repo-root", default=str(M.REPO_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if not NAME_RE.match(args.name):
        print(f"error: invalid name {args.name!r} (use letters, digits, '.', '_' or '-')", file=sys.stderr)
        return 2
    root = Path(args.repo_root)
    dest = Path(args.memory_dir).expanduser() if args.memory_dir else root / "playthroughs" / args.name / "memory"
    try:
        h = create_memory(args.name, dest, root / "memory-template")
        print(f"created {dest} (git commit {h})")
        if not args.no_link:
            prev = link_play_memory(root / "play", dest)
            if prev:
                print(f"replaced play/memory symlink (was → {prev})")
            print(f"play/memory → {os.readlink(root / 'play' / 'memory')}")
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("next: runner/play.py run --playthrough <name> (it opens session S001); manually, any MCP client on the "
          "bg3 server in play/ (its first tool call or session_start opens S001)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
