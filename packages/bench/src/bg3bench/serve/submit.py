# pyright: basic
"""Submit from the panel: pack and validate a run locally, show the result and the exact files, upload, poll.

Three steps, each a request of its own so the user sees what goes out before it goes:

1. `prepare`: the run is packed (bg3bench.submission.pack, through a `Bundler`) into
   runtime/submissions/<run_id>/<k>/ (a new k each time: pack never overwrites) and validated there (the same
   validator the site runs). prepared.json keeps the bundle hash, the verdict and the file list.
2. `upload`: only the bundle prepare made (the caller names its hash, so a re-pack in between is refused), only when
   its verdict is accepted, with the site token from Settings: bundle.json, the files it lists and the YouTube URL go
   to the site (`Site`) as one multipart POST. submission.json keeps the site's id.
3. `poll`: the submission's status from the site, saved to submission.json.

The site token and the .env values never appear in a response: the values are passed to the pack's scrub and the
validator's scrub check only.
"""

from __future__ import annotations

import importlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

from bg3bench.serve.panel import PanelError

SUBMISSIONS = "submissions"  # runtime/submissions/<run_id>/
PREPARED = "prepared.json"
SUBMISSION = "submission.json"
ENVELOPE = "bundle.json"
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TIMEOUT_S = 60.0


class Bundler(Protocol):
    def pack(
        self, run_dir: Path, out_dir: Path, *, attempt: int, unsubmitted_valid: int, literals: Sequence[str]
    ) -> tuple[str, Path]:
        """Pack run_dir into out_dir: (bundle sha256, bundle dir). PanelError when the run can't be packed."""
        ...

    def validate(self, bundle_dir: Path, *, literals: Sequence[str]) -> dict[str, Any]:
        """The validator's verdict as JSON: {"accepted", "flags", "checks": [{code, ok, severity, detail}], ...}."""
        ...


class Site(Protocol):
    def post_submission(
        self, token: str, files: Sequence[tuple[str, bytes]], youtube_url: str
    ) -> tuple[int, dict[str, Any]]:
        """POST /api/submissions: (HTTP status, JSON body)."""
        ...

    def get_submission(self, token: str, submission_id: str) -> tuple[int, dict[str, Any]]:
        """GET /api/submissions/<id>: (HTTP status, JSON body)."""
        ...


class BenchSubmission:
    """The real Bundler: bg3bench.submission's pack and validate against the suite's release file."""

    def __init__(self, release: Path | None, panel_version: str):
        self.release, self.panel_version = release, panel_version

    def _mod(self, name: str) -> Any:
        try:
            return importlib.import_module(f"bg3bench.submission.{name}")
        except ImportError:
            raise PanelError("this bench has no bg3bench.submission yet: it can't pack a run") from None

    def pack(
        self, run_dir: Path, out_dir: Path, *, attempt: int, unsubmitted_valid: int, literals: Sequence[str]
    ) -> tuple[str, Path]:
        pack = self._mod("pack")
        try:
            p = pack.pack(
                run_dir,
                out_dir,
                attempt=pack.Attempt(attempt, unsubmitted_valid),
                literals=literals,
                panel_version=self.panel_version,
            )
        except pack.PackError as e:
            raise PanelError(str(e)) from None
        return p.bundle_sha256, p.bundle_dir

    def validate(self, bundle_dir: Path, *, literals: Sequence[str]) -> dict[str, Any]:
        validate = self._mod("validate")
        if self.release is None or not self.release.is_file():
            raise PanelError(f"no release file of the suite to validate against ({self.release}): see --release")
        release = validate.Release.from_json(json.loads(self.release.read_text(encoding="utf-8")))
        return validate.validate(bundle_dir, release, where="panel", literals=literals).as_json()


def site_url(url: str | None) -> str:
    """The site's base URL: https, or http on this machine only (a local copy of the site)."""
    if not url:
        raise PanelError("no site URL: start the panel with --site or $BG3BENCH_SITE")
    u = urlparse(url)
    local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or local) or not u.hostname or u.path not in ("", "/") or u.query:
        raise PanelError(f"bad site URL {url!r}: https://<host> only")
    return f"{u.scheme}://{u.netloc}"


def multipart(fields: Sequence[tuple[str, bytes, str | None]], boundary: str) -> bytes:
    """(name, data, filename or None) parts as multipart/form-data."""
    out = bytearray()
    for name, data, filename in fields:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        ctype = "application/octet-stream" if filename else "text/plain; charset=utf-8"
        out += f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


Open = Callable[..., Any]


@dataclass(frozen=True)
class HttpSite:
    """The real Site: the site's JSON API over HTTPS (standard library)."""

    base: str
    urlopen: Open = urlrequest.urlopen
    new_boundary: Callable[[], str] = lambda: uuid.uuid4().hex

    def _call(self, req: urlrequest.Request) -> tuple[int, dict[str, Any]]:
        try:
            with self.urlopen(req, timeout=TIMEOUT_S) as r:
                status, raw, retry = r.status, r.read(), r.headers.get("Retry-After")
        except urlerror.HTTPError as e:
            status, raw, retry = e.code, e.read(), e.headers.get("Retry-After")
        except (urlerror.URLError, OSError) as e:
            raise PanelError(f"the site didn't answer: {getattr(e, 'reason', e)}") from None
        try:
            body: Any = json.loads(raw) if raw else {}
        except ValueError:
            body = {"error": {"code": "not_json", "message": f"HTTP {status}, not JSON"}}
        body = body if isinstance(body, dict) else {"error": {"code": "not_json", "message": "not a JSON object"}}
        if retry is not None:
            body = {**body, "retry_after": retry}
        return status, body

    def post_submission(
        self, token: str, files: Sequence[tuple[str, bytes]], youtube_url: str
    ) -> tuple[int, dict[str, Any]]:
        boundary = self.new_boundary()
        parts = [(name, data, name) for name, data in files] + [("youtube_url", youtube_url.encode(), None)]
        req = urlrequest.Request(
            f"{self.base}/api/submissions",
            data=multipart(parts, boundary),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        return self._call(req)

    def get_submission(self, token: str, submission_id: str) -> tuple[int, dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", submission_id):
            raise PanelError(f"bad submission id {submission_id!r}")
        req = urlrequest.Request(
            f"{self.base}/api/submissions/{submission_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return self._call(req)


def youtube_id(url: Any) -> str:
    """The 11-character video id of a youtube.com/watch?v=, youtu.be/ or youtube.com/shorts/ URL."""
    if not isinstance(url, str):
        raise PanelError("youtube_url must be a YouTube video URL")
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    vid = None
    if u.scheme in ("http", "https"):
        if host == "youtu.be":
            vid = u.path.lstrip("/")
        elif host == "youtube.com" and u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        elif host == "youtube.com" and u.path.startswith("/shorts/"):
            vid = u.path.removeprefix("/shorts/")
    if vid is None or not YOUTUBE_ID.fullmatch(vid):
        raise PanelError("youtube_url must be a youtube.com/watch?v=, youtu.be/ or youtube.com/shorts/ URL of a video")
    return vid


def attempt(rows: Sequence[Mapping[str, Any]], run_dir: str, submitted: Callable[[str], bool]) -> tuple[int, int]:
    """(n, unsubmitted_valid) of a run in its cell (scenario, model, condition) on this install: n counts the cell's
    runs up to this one by creation time; unsubmitted_valid the earlier valid ones not submitted."""
    me = next((r for r in rows if r["dir"] == run_dir), None)
    if me is None:
        raise PanelError(f"no finished run {run_dir!r}")
    cell = [
        r for r in rows if (r["scenario"], r["model"], r["condition"]) == (me["scenario"], me["model"], me["condition"])
    ]
    cell.sort(key=lambda r: (str(r["created"] or ""), r["dir"]))
    i = cell.index(me)
    return i + 1, sum(1 for r in cell[:i] if r["valid"] and not submitted(r["dir"]))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _safe_name(run_dir: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.+@~-]{1,200}", run_dir) or run_dir in (".", ".."):
        raise PanelError(f"bad run {run_dir!r}")
    return run_dir


def submitted(root: Path, run_dir: str) -> bool:
    return (root / run_dir / SUBMISSION).is_file()


def bundle_listing(bundle_dir: Path) -> list[dict[str, Any]]:
    """bundle.json and the files it lists, with their sizes: exactly what upload sends."""
    env = _read_json(bundle_dir / ENVELOPE)
    if not isinstance(env, dict) or not isinstance(env.get("files"), list):
        raise PanelError(f"{bundle_dir}: no readable {ENVELOPE}")
    names = [ENVELOPE] + [f["name"] for f in env["files"]]
    for n in names:
        if not isinstance(n, str) or "/" in n or n.startswith(".") or not (bundle_dir / n).is_file():
            raise PanelError(f"{bundle_dir}: bundle file {n!r} is missing or not a bare name")
    return [{"name": n, "bytes": (bundle_dir / n).stat().st_size} for n in names]


def prepare(
    run_dir: Path,
    root: Path,
    bundler: Bundler,
    *,
    attempt_n: int,
    unsubmitted_valid: int,
    literals: Sequence[str],
) -> dict[str, Any]:
    """Pack and validate run_dir into root/<run>/<k>/; returns what the Submit dialog shows."""
    base = root / run_dir.name
    if (base / SUBMISSION).is_file():
        raise PanelError(f"{run_dir.name} is submitted already")
    base.mkdir(parents=True, exist_ok=True)
    k = 1 + max((int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit()), default=0)
    sha, bdir = bundler.pack(
        run_dir, base / str(k), attempt=attempt_n, unsubmitted_valid=unsubmitted_valid, literals=literals
    )
    verdict = bundler.validate(bdir, literals=literals)
    view = {
        "run": run_dir.name,
        "bundle_sha256": sha,
        "bundle_dir": str(bdir),
        "files": bundle_listing(bdir),
        "verdict": verdict,
        "attempt": {"n": attempt_n, "unsubmitted_valid": unsubmitted_valid},
    }
    _write_json(base / PREPARED, view)
    return view


def _site_error(status: int, body: Mapping[str, Any]) -> str:
    raw = body.get("error")
    err: dict[str, Any] = raw if isinstance(raw, dict) else {}
    msg = f"the site refused it (HTTP {status}): {err.get('message') or err.get('code') or 'no reason given'}"
    if status == 429 and body.get("retry_after"):
        msg += f"; try again in {body['retry_after']} s"
    if failed := [c.get("code") for c in err.get("checks") or [] if isinstance(c, dict) and c.get("ok") is False]:
        msg += f"; failed checks: {', '.join(map(str, failed))}"
    return msg


def upload(
    root: Path, run: str, *, bundle_sha256: Any, youtube_url: Any, token: str | None, site: Site
) -> dict[str, Any]:
    base = root / _safe_name(run)
    prepared = _read_json(base / PREPARED)
    if not isinstance(prepared, dict):
        raise PanelError(f"{run}: validate it first (no prepared bundle)")
    if bundle_sha256 != prepared["bundle_sha256"]:
        raise PanelError(f"{run}: the bundle changed since you saw it: validate again")
    if (base / SUBMISSION).is_file():
        raise PanelError(f"{run} is submitted already")
    if not prepared["verdict"].get("accepted"):
        raise PanelError(f"{run}: the local validation rejects this bundle; the site would too")
    if not token:
        raise PanelError("no site token: create one on the site and save it in Settings")
    vid = youtube_id(youtube_url)
    bdir = Path(prepared["bundle_dir"])
    listing = bundle_listing(bdir)
    if listing != prepared["files"]:
        raise PanelError(f"{run}: the bundle's files changed since validation: validate again")
    files = [(f["name"], (bdir / f["name"]).read_bytes()) for f in listing]
    status, body = site.post_submission(token, files, f"https://www.youtube.com/watch?v={vid}")
    if status != 202 or not isinstance(body.get("id"), str):
        raise PanelError(_site_error(status, body))
    doc = {
        "id": body["id"],
        "status": body.get("status", "received"),
        "bundle_sha256": prepared["bundle_sha256"],
        "site_bundle_sha256": body.get("bundle_sha256"),
        "youtube_id": vid,
    }
    _write_json(base / SUBMISSION, doc)
    return doc


def poll(root: Path, run: str, *, token: str | None, site: Site) -> dict[str, Any]:
    base = root / _safe_name(run)
    doc = _read_json(base / SUBMISSION)
    if not isinstance(doc, dict):
        raise PanelError(f"{run} is not submitted")
    if not token:
        raise PanelError("no site token: save it in Settings")
    status, body = site.get_submission(token, doc["id"])
    if status != 200:
        raise PanelError(_site_error(status, body))
    keep = ("status", "flags", "checks", "run_url", "updated", "retry_after")
    doc = {**doc, **{k: body[k] for k in keep if k in body}}
    _write_json(base / SUBMISSION, doc)
    return doc


def state(root: Path, run: str) -> dict[str, Any]:
    """What the Results screen knows of a run's submission: the prepared bundle and the site's last answer."""
    base = root / _safe_name(run)
    return {"prepared": _read_json(base / PREPARED), "submission": _read_json(base / SUBMISSION)}
