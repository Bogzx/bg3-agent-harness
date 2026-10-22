"""Is web/dist the build of web/'s sources? The e2e tests drive whatever dist is there, so a build older than a
source change tests the old app: a Launch.svelte fix "failed" its own e2e test until `npm run build` (app r16)."""

from pathlib import Path

SOURCES = ("src", "index.html", "package.json", "package-lock.json", "vite.config.ts", "svelte.config.js",
           "tsconfig.json")  # fmt: skip
FIX = "run `npm ci && npm run build` in packages/playthrough/web"


def build_problem(web: Path) -> str | None:
    """Why web/dist can't be tested (None: it is built from the current sources)."""
    built = web / "dist" / "index.html"
    if not built.is_file():
        return f"no built web app at {built.parent}: {FIX}"
    files = [p for name in SOURCES for p in ([web / name] if (web / name).is_file() else (web / name).rglob("*"))]
    newest = max((p for p in files if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
    if newest is not None and newest.stat().st_mtime > built.stat().st_mtime:
        return f"the built web app is older than {newest.relative_to(web)}: {FIX}"
    return None
