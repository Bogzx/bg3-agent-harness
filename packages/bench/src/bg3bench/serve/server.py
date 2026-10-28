"""The benchmark's local panel (stdlib HTTP): a JSON API over the bench's own modules and the built web app.

    uv run --project packages/bench bg3bench-serve [--port 8790]

Binds 127.0.0.1 only (there is no --host). Every request must name the panel as `127.0.0.1:<port>` or
`localhost:<port>` in its Host (else 421); every non-GET request needs the write token in X-Panel-Token and a JSON
body (security.py). The token is new at each start, saved to <config>/panel-token (mode 600) and put into the served
index.html; no response carries CORS headers. Request bodies are never logged.

GET (JSON, Cache-Control: no-store):
    /api/v1/about        versions, the suite, the not-affiliated line
    /api/v1/preflight    bg3bench.preflight's checks + the panel's (ffmpeg, display, disk)
    /api/v1/scenarios    the suite's scenarios with their start save's status
    /api/v1/profiles     client profiles; which ones the panel offers and why not
    /api/v1/queue        runtime/matrix/*/progress.json
    /api/v1/results      finished runs (runtime/sessions/*/{manifest,result}.json)
    /api/v1/settings     key names with masked values, the site token masked, preferences
    /api/v1/run          the matrix this panel started (launch.Runner state), or null
    /api/v1/submissions  per run dir: its prepared bundle and the site's last answer (submit.py)
    /api/v1/render       the last video render (video.Renderer state), or null
POST (JSON body, token):
    /api/v1/plan                 a New run form -> the jobs Start would queue (nothing is written or launched)
    /api/v1/run/start            the same form: write its matrix file and start it (launch.py)
    /api/v1/run/stop             end the run playing now, start no new one
    /api/v1/submit/prepare       {"run"}: pack and validate a finished run; the verdict and the exact files
    /api/v1/submit/upload        {"run", "bundle_sha256", "youtube_url"}: send that bundle with the site token
    /api/v1/submit/status        {"run"}: the submission's status from the site
    /api/v1/render               {"run"}: render the run's evidence video (needs a prepared bundle and a recording)
    /api/v1/settings/key         {"name", "value"}: write a profile's API key to the repo's .env; returns it masked
    /api/v1/settings/site-token  {"value"}: the site token, to <config>/site-token
    /api/v1/settings/prefs       {"youtube_verified"?, "record_cap_gb"?}
"""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import shutil
import time
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from bg3harness import bg3le_buildinfo, steam_paths
from bg3harness.client_setup import read_dotenv

from bg3bench import __version__, preflight, suite
from bg3bench.adapters.legacy_cli import REPO
from bg3bench.serve import launch, panel, security, submit, video
from bg3bench.serve.settings import Settings, SettingsError

API_VERSION = 1
HOST = "127.0.0.1"
DEFAULT_PORT = 8790
MAX_BODY = 16 * 1024
DEFAULT_WEB = Path(__file__).resolve().parents[3] / "web" / "dist"
SCENARIOS = Path(__file__).resolve().parents[3] / "scenarios"
TOKEN_SLOT = "__BG3BENCH_PANEL_TOKEN__"  # index.html's <meta name="bg3bench-panel-token" content="...">
NOT_AFFILIATED = (
    "BG3 bench is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. "
    "Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC. Baldur's Gate 3 "
    "is made by Larian Studios. Not affiliated with Larian Studios."
)


class App:
    def __init__(
        self,
        *,
        token: str,
        repo: Path,
        runtime: Path,
        settings: Settings,
        saves: panel.SavesSource,
        checks: Callable[[], Sequence[panel.Check]],
        scenarios_dir: Path = SCENARIOS,
        web_dir: Path | None = DEFAULT_WEB,
        site: str | None = None,
        runner: launch.Runner | None = None,
        bundler: submit.Bundler | None = None,
        site_client: Callable[[str], submit.Site] = submit.HttpSite,
        renderer: video.Renderer | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.token, self.repo, self.runtime, self.settings = token, repo, runtime, settings
        self.saves, self.checks, self.scenarios_dir, self.web_dir = saves, checks, scenarios_dir, web_dir
        self.site = site  # the submissions site's URL (--site)
        self.runner, self.now = runner, now  # runner None: this panel starts no run
        self.bundler, self.site_client, self.renderer = bundler, site_client, renderer
        self.port = 0  # set once bound (serve)

    def profiles_dir(self) -> Path:
        return self.repo / "profiles"

    def key_names(self) -> list[str]:
        return [
            r["api_key_env"] for r in panel.profile_rows(self.profiles_dir(), lambda _: False) if r.get("api_key_env")
        ]

    def get(self, path: str) -> dict[str, Any] | None:
        if path == "/api/v1/about":
            return {
                "bench_version": __version__,
                "suite": suite.suite_version(),
                "not_affiliated": NOT_AFFILIATED,
                "site": self.site,
            }
        if path == "/api/v1/preflight":
            return panel.preflight_view(self.checks())
        if path == "/api/v1/scenarios":
            scs = panel.load_scenarios(self.scenarios_dir)
            return {"scenarios": [panel.scenario_row(n, sc, self.saves) for n, sc in scs.items()]}
        if path == "/api/v1/profiles":
            env = read_dotenv(self.settings.dotenv)

            def has(name: str) -> bool:
                return bool(os.environ.get(name) or env.get(name))

            return {"profiles": panel.profile_rows(self.profiles_dir(), has), "offered_clients": panel.OFFERED_CLIENTS}
        if path == "/api/v1/queue":
            return {"matrices": panel.queue(self.runtime / "matrix")}
        if path == "/api/v1/results":
            return {"runs": panel.results(self.runtime / "sessions")}
        if path == "/api/v1/settings":
            return self.settings.view()
        if path == "/api/v1/run":
            return {"run": self.runner.state() if self.runner else None, "can_start": self.runner is not None}
        if path == "/api/v1/submissions":
            root = self.submissions()
            runs = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
            return {"submissions": {r: submit.state(root, r) for r in runs}, "site": self.site}
        if path == "/api/v1/render":
            return {"render": self.renderer.state() if self.renderer else None}
        return None

    def submissions(self) -> Path:
        return self.runtime / submit.SUBMISSIONS

    def finished_run(self, body: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
        run = body.get("run")
        rows = panel.results(self.runtime / "sessions")
        if not isinstance(run, str) or not any(r["dir"] == run for r in rows):
            raise panel.PanelError(f"no finished run {run!r}")
        return self.runtime / "sessions" / run, rows

    def literals(self) -> list[str]:
        """Values the pack scrubs verbatim and the validator must not find: the .env values and the site token."""
        vals = [v for v in read_dotenv(self.settings.dotenv).values() if v]
        token = self.settings.site_token()
        return vals + ([token] if token else [])

    def need(self, what: Any, name: str) -> Any:
        if what is None:
            raise panel.PanelError(f"this panel has no {name}")
        return what

    def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        run_dir, rows = self.finished_run(body)
        root = self.submissions()
        n, unsubmitted = submit.attempt(rows, run_dir.name, lambda d: submit.submitted(root, d))
        return submit.prepare(
            run_dir,
            root,
            self.need(self.bundler, "bundler"),
            attempt_n=n,
            unsubmitted_valid=unsubmitted,
            literals=self.literals(),
        )

    def render(self, body: dict[str, Any]) -> dict[str, Any]:
        run_dir, _ = self.finished_run(body)
        renderer = self.need(self.renderer, "renderer")
        prepared = submit.state(self.submissions(), run_dir.name)["prepared"]
        if not prepared:
            raise panel.PanelError("validate the run first: the video's id line names its bundle")
        man, res = (json.loads((run_dir / f).read_text(encoding="utf-8")) for f in ("manifest.json", "result.json"))
        scenario_of: dict[str, Any] = man.get("scenario") or {}
        sc = panel.load_scenarios(self.scenarios_dir).get(str(scenario_of.get("name")))
        renderer.start(video.job_for(run_dir, man, res, sc, bundle_sha256=prepared["bundle_sha256"], site=self.site))
        return {"render": renderer.state()}

    def plan(self, body: dict[str, Any]) -> dict[str, Any]:
        return panel.plan_run(
            body,
            scenarios=panel.load_scenarios(self.scenarios_dir),
            prof_dir=self.profiles_dir(),
            budget=self.budget(),
            youtube_verified=self.settings.prefs()["youtube_verified"],
        )

    def budget(self) -> Path:
        return self.runtime / "budget.json"

    def run_control(self) -> launch.Runner:
        if self.runner is None:
            raise panel.PanelError("this panel starts no run (started without a runner)")
        return self.runner

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if path == "/api/v1/plan":
            return self.plan(body)
        if path == "/api/v1/run/start":
            runner = self.run_control()
            return launch.start(
                runner,
                self.plan(body),
                runtime=self.runtime,
                budget=self.budget(),
                allow_unverified=body.get("allow_unverified") is True,
                now=self.now(),
            )
        if path == "/api/v1/run/stop":
            runner = self.run_control()
            runner.stop()
            return {"run": runner.state()}
        if path == "/api/v1/submit/prepare":
            return self.prepare(body)
        if path == "/api/v1/submit/upload":
            return submit.upload(
                self.submissions(),
                body.get("run"),  # type: ignore[arg-type] - submit checks it
                bundle_sha256=body.get("bundle_sha256"),
                youtube_url=body.get("youtube_url"),
                token=self.settings.site_token(),
                site=self.site_client(submit.site_url(self.site)),
            )
        if path == "/api/v1/submit/status":
            return submit.poll(
                self.submissions(),
                body.get("run"),  # type: ignore[arg-type]
                token=self.settings.site_token(),
                site=self.site_client(submit.site_url(self.site)),
            )
        if path == "/api/v1/render":
            return self.render(body)
        if path == "/api/v1/settings/key":
            return self.settings.set_key(body.get("name"), body.get("value"))
        if path == "/api/v1/settings/site-token":
            return self.settings.set_site_token(body.get("value"))
        if path == "/api/v1/settings/prefs":
            return self.settings.set_prefs(body)
        return None

    def index_html(self, path: Path) -> bytes:
        return path.read_bytes().replace(TOKEN_SLOT.encode(), self.token.encode())

    def static(self, path: str) -> Path | None:
        if self.web_dir is None or not self.web_dir.is_dir():
            return None
        root = self.web_dir.resolve()
        p = (root / unquote(path).lstrip("/")).resolve()
        if p.is_relative_to(root) and p.is_file():
            return p
        index = root / "index.html"
        return index if index.is_file() and "." not in path.rsplit("/", 1)[-1] else None


def make_handler(app: App, verbose: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "bg3bench/1"
        protocol_version = "HTTP/1.1"

        def handle(self) -> None:
            with contextlib.suppress(ConnectionResetError):
                super().handle()

        def log_message(self, format: str, *args: object) -> None:
            if verbose:  # the request line and status only: never a body (it can hold a key)
                super().log_message(format, *args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: dict[str, Any], code: int = 200) -> None:
            body = json.dumps({"v": API_VERSION, **obj}, ensure_ascii=False, default=str).encode()
            self._send(code, body, "application/json")

        def _host_refused(self) -> bool:
            if security.host_ok(self.headers.get("Host"), app.port):
                return False
            self.close_connection = True
            self._json({"error": "this panel answers only 127.0.0.1 or localhost on its port"}, 421)
            return True

        def do_GET(self) -> None:
            if self._host_refused():
                return None
            url = urlparse(self.path)
            try:
                if url.path.startswith("/api/"):
                    payload = app.get(url.path)
                    if payload is None:
                        return self._json({"error": "not found"}, 404)
                    return self._json(payload)
                f = app.static(url.path)
                if f is None:
                    return self._send(
                        404, b"not found (build the web app: npm run build in packages/bench/web)", "text/plain"
                    )
                if f.name == "index.html":
                    return self._send(200, app.index_html(f), "text/html; charset=utf-8")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                cache = "public, max-age=31536000, immutable" if "/assets/" in url.path else "no-cache"
                return self._send(200, f.read_bytes(), ctype, cache)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except (OSError, ValueError) as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_POST(self) -> None:
            if self._host_refused():
                return None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True  # the body stays unread
                return self._json({"error": "bad or too large body"}, 413)
            raw = self.rfile.read(n) if n else b""
            why = security.write_refusal(self.headers, app.token, app.port)
            if why:
                return self._json({"error": why}, 403)
            try:
                body: Any = json.loads(raw) if raw else {}
            except ValueError:
                return self._json({"error": "the body is not JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "the body is not a JSON object"}, 400)
            try:
                payload = app.post(urlparse(self.path).path, cast(dict[str, Any], body))
            except (panel.PanelError, SettingsError) as e:
                return self._json({"error": str(e)}, 400)
            if payload is None:
                return self._json({"error": "not found"}, 404)
            return self._json(payload)

        def _no_method(self) -> None:
            if self._host_refused():
                return
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_OPTIONS = do_PUT = do_PATCH = do_DELETE = _no_method

    return Handler


def serve(app: App, port: int, verbose: bool = False) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((HOST, port), make_handler(app, verbose))
    srv.daemon_threads = True
    app.port = srv.server_address[1]
    return srv


def default_checks(repo: Path, runtime: Path) -> Callable[[], list[panel.Check]]:
    def run() -> list[panel.Check]:
        ctx = preflight.Ctx(repo=repo, steam=steam_paths, bg3le=bg3le_buildinfo)
        return preflight.preflight(ctx) + panel.panel_checks(
            which=shutil.which, env=os.environ, disk_free=lambda: shutil.disk_usage(runtime).free
        )

    return run


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    ap.add_argument("--repo", type=Path, default=REPO, help="the harness checkout (profiles/, .env, runtime/)")
    ap.add_argument("--runtime", type=Path, help="default <repo>/runtime")
    ap.add_argument("--config-dir", type=Path, default=Path.home() / ".config" / "bg3bench")
    ap.add_argument("--web", type=Path, default=DEFAULT_WEB, help="the built web app (packages/bench/web/dist)")
    ap.add_argument(
        "--site", default=os.environ.get("BG3BENCH_SITE"), help="the submissions site's URL ($BG3BENCH_SITE)"
    )
    ap.add_argument("--release", type=Path, help="the suite's release file, what Submit validates against")
    ap.add_argument("--verbose", action="store_true", help="log request lines (never bodies)")
    return ap


def main(argv: list[str] | None = None) -> int:
    opt = parser().parse_args(argv)
    repo = opt.repo.absolute()
    runtime = (opt.runtime or repo / "runtime").absolute()
    config_dir = opt.config_dir.absolute()
    token = security.issue_token(config_dir)
    app = App(
        token=token,
        repo=repo,
        runtime=runtime,
        settings=Settings(repo / ".env", config_dir, lambda: app.key_names()),
        saves=panel.StorySaves(steam_paths.saves_dir()),
        checks=default_checks(repo, runtime),
        web_dir=opt.web,
        site=opt.site,
        runner=launch.MatrixProcess(repo),
        bundler=submit.BenchSubmission(opt.release, __version__),
        renderer=video.VideoProcess(),
    )
    srv = serve(app, opt.port, opt.verbose)
    print(f"bg3bench panel: http://{HOST}:{app.port}/  (write token in {config_dir / security.TOKEN_FILE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
