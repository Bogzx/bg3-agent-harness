"""Run tickets, the client side: the matrix process asks the site for a ticket before each run and reports while it plays.

`Ticketed(run_one)` wraps a matrix's run_one (matrix.py's loop doesn't change). Before the run it takes a ticket from
the site and writes <run_dir>/ticket.json; while the run plays a daemon thread sends a beat every `beat_s` seconds (how
far each record stream has got and the root over those lines, ticket_hash); when run_one returns it sends the end.

Nothing here can stop a run or block the game. Taking the ticket is the one step that can refuse: no token, the site
down or refusing, or a clock over MAX_CLOCK_S off give `RunResult(refused=True)`, the matrix's path for a start refused
before any play (no attempt, $0, the matrix stops and a later `bench run` resumes). From then on every failure goes to
ticket.beats.jsonl: each request has one try and a 10 s timeout, a lost beat is covered by the next (they are
cumulative), and a 401 or 410 stops the beats while the run plays on. An end that can't be delivered stays in
ticket.json, and `deliver_end` tries again (a matrix restart, Validate, Upload).

The site token comes from its file, read by this process only: it isn't put in the run's environment. What leaves the
machine is the cell, line counts, roots and a reason from REASONS: never a path, a message or a score.

ticket.json and ticket.beats.jsonl stay in the run dir and never go in a bundle (pack.bundle_files selects by name).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from bg3harness.client_setup import read_dotenv

from bg3bench import matrix, suite
from bg3bench.submission import scrub, ticket_hash

Json = dict[str, Any]
Open = Callable[..., Any]

TICKET_FILE = "ticket.json"
BEATS_FILE = "ticket.beats.jsonl"
TIMEOUT_S = 10.0
MAX_CLOCK_S = 60  # the machine's clock against the site's when the ticket is issued
JOIN_S = TIMEOUT_S + 5  # how long the end waits for a beat that is still being sent
SITE_ENV = "BG3BENCH_SITE"
CONFIG_ENV = "BG3BENCH_CONFIG_DIR"  # where the panel keeps the site token (its --config-dir)
SITE_TOKEN = "site-token"
ACCESS_ID_ENV, ACCESS_SECRET_ENV = "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HEX64 = re.compile(r"[0-9a-f]{64}")
# The site's reasons. An invalid run's free-text reason maps onto them (reason_of), so no path or message reaches it.
REASONS = (
    "harness_failure",
    "runner_error",
    "artifacts_failed",
    "error",
    "interrupted",
    "operator",
    "off_route",
    "unseeded",
    "foreign_save",
    "refused",
)
STOP_BEATS = (401, 410)  # revoked, closed or expired: no later beat can land


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """The ticket routes never redirect. A followed POST becomes a GET elsewhere that carries the token and the Access
    secret, and its 200 reads as a delivered end."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


open_url: Open = urlrequest.build_opener(_NoRedirect).open


def base_url(url: str | None) -> str:
    """The site's base URL: https, or http on this machine only (a local copy of the site). ValueError otherwise."""
    if not url:
        raise ValueError(f"no site URL: start the panel with --site or ${SITE_ENV}")
    u = urlparse(url)
    local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or local) or not u.hostname or u.path not in ("", "/") or u.query:
        raise ValueError(f"bad site URL {url!r}: https://<host> only")
    return f"{u.scheme}://{u.netloc}"


def access_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """The Access service token for a site behind Cloudflare Access (staging): both variables or neither."""
    cid, secret = environ.get(ACCESS_ID_ENV), environ.get(ACCESS_SECRET_ENV)
    return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret} if cid and secret else {}


def config_dir(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(CONFIG_ENV) or Path.home() / ".config" / "bg3bench")


def read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def literals(dotenv: Path, token: str | None) -> list[str]:
    """Values the scrub replaces verbatim, the same whenever a run's lines are scrubbed: the .env values, the token."""
    return [v for v in read_dotenv(dotenv).values() if v] + ([token] if token else [])


def reason_of(result: matrix.RunResult) -> str | None:
    """A run's reason from REASONS (None: valid). The free text after its first word never leaves the machine."""
    if result.valid:
        return None
    if result.refused:
        return "refused"
    why = result.invalid_reason or ""
    if why.startswith("artifacts failed"):
        return "artifacts_failed"
    if why.startswith("start failed"):
        return "runner_error"
    word = re.match(r"[a-z_]+", why)
    return word.group(0) if word and word.group(0) in REASONS else "error"


def outcome_of(result: matrix.RunResult) -> str:
    return "valid" if result.valid else "not_played" if result.refused else "invalid"


@dataclass(frozen=True)
class Site:
    """The site's ticket routes (standard library). Each call is one try; a site that doesn't answer raises."""

    base: str
    urlopen: Open = open_url
    headers: Mapping[str, str] = field(default_factory=dict[str, str])

    def _post(self, token: str, path: str, doc: Mapping[str, Any]) -> tuple[int, Json]:
        req = urlrequest.Request(
            f"{self.base}{path}",
            data=json.dumps(doc).encode(),
            method="POST",
            headers={
                **self.headers,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.urlopen(req, timeout=TIMEOUT_S) as r:
                status, raw = r.status, r.read()
        except urlerror.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            body: Any = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        return status, cast(Json, body) if isinstance(body, dict) else {}

    def issue(self, token: str, cell: Mapping[str, str]) -> tuple[int, Json]:
        return self._post(token, "/api/tickets", cell)

    def beat(self, token: str, ticket_id: str, doc: Mapping[str, Any]) -> int:
        return self._post(token, f"/api/tickets/{ticket_id}/beat", doc)[0]

    def end(self, token: str, ticket_id: str, doc: Mapping[str, Any]) -> int:
        return self._post(token, f"/api/tickets/{ticket_id}/end", doc)[0]


class Tail:
    """A run dir's record streams as they grow: complete new lines, scrubbed and fed to one running hash per stream. A
    stream's file may appear at any time, a partial last line waits, and a line the scrub can't read (not JSON) holds
    its stream there, because pack couldn't read it either."""

    def __init__(self, run_dir: Path, nonce: str, values: Sequence[str]) -> None:
        self.run_dir, self.nonce = run_dir, nonce
        self.scrubber = scrub.Scrubber(values)
        self.hashes: dict[str, ticket_hash.RunningHash] = {}
        self.offsets: dict[str, int] = {}

    def read(self) -> tuple[dict[str, int], str]:
        """Read what is new: ({stream: complete lines}, the root over them)."""
        for p in sorted(self.run_dir.iterdir()):
            if ticket_hash.is_stream(p.name) and p.is_file():
                self._feed(p)
        entries = [(n, h.lines, h.hexdigest()) for n, h in self.hashes.items()]
        return {n: h.lines for n, h in self.hashes.items()}, ticket_hash.root(entries)

    def _feed(self, path: Path) -> None:
        h = self.hashes.setdefault(path.name, ticket_hash.RunningHash(self.nonce, path.name))
        offset = self.offsets.get(path.name, 0)
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        for line in ticket_hash.complete_lines(data):
            try:
                scrubbed = self.scrubber.line(line.decode())
            except ValueError:
                break
            h.feed(scrubbed.encode())
            offset += len(line) + 1
        self.offsets[path.name] = offset


def _read_json(path: Path) -> Json | None:
    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(Json, doc) if isinstance(doc, dict) else None


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _log(run_dir: Path, now: float, rec: Mapping[str, Any]) -> None:
    """What was sent and the answer, for the panel to show. A failed write is dropped: it never reaches the run."""
    try:
        with (run_dir / BEATS_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(now, 3), **rec}) + "\n")
    except OSError:
        pass


def _send_end(site: Site, token: str, run_dir: Path, ticket: Json, doc: Json, now: float) -> bool:
    """Send an end and keep it in ticket.json with whether it was delivered."""
    try:
        status = site.end(token, ticket["id"], doc)
    except Exception as e:  # noqa: BLE001 - nothing in the ticket path reaches the run
        status = None
        _log(run_dir, now, {"seq": doc["seq"], "kind": "end", "error": type(e).__name__})
    else:
        _log(run_dir, now, {"seq": doc["seq"], "kind": "end", "status": status})
    delivered = status == 200
    try:
        _write_json(run_dir / TICKET_FILE, {**ticket, "end": {**doc, "delivered": delivered}})
    except OSError as e:
        _log(run_dir, now, {"kind": "ticket.json", "error": type(e).__name__})
    return delivered


class Beats:
    """One run's ticket while it plays. `tick` sends one beat and `finish` the end; neither raises."""

    def __init__(
        self,
        run_dir: Path,
        ticket: Json,
        site: Site,
        token: str,
        values: Sequence[str],
        now: Callable[[], float],
    ) -> None:
        self.run_dir, self.site, self.token, self.now = run_dir, site, token, now
        self.ticket: Json = ticket
        self.tail = Tail(run_dir, ticket["nonce"], values)
        self.seq = 0
        self.stopped = False
        self.lock = threading.Lock()  # one beat at a time; held while one is out, for the end to see

    def tick(self) -> None:
        with self.lock:
            if self.stopped:
                return
            seq = self.seq
            try:
                lines, root = self.tail.read()
                if not any(lines.values()):  # still launching: a ticket with a beat can no longer end not_played
                    return
                self.seq = seq + 1
                status = self.site.beat(self.token, self.ticket["id"], {"seq": seq, "lines": lines, "root": root})
            except Exception as e:  # noqa: BLE001 - the next beat covers this one; the run plays on
                _log(self.run_dir, self.now(), {"seq": seq, "error": type(e).__name__})
                return
            _log(self.run_dir, self.now(), {"seq": seq, "lines": lines, "root": root, "status": status})
            if status in STOP_BEATS and not self.stopped:  # stopped: the end came first, and ticket.json keeps it
                self.stopped = True
                self._note_stopped(status)

    def _note_stopped(self, status: int) -> None:
        self.ticket = {**self.ticket, "beats_stopped": status}  # the panel says this run can't be submitted
        try:
            _write_json(self.run_dir / TICKET_FILE, self.ticket)
        except OSError as e:
            _log(self.run_dir, self.now(), {"kind": "ticket.json", "error": type(e).__name__})

    def finish(self, outcome: str, reason: str | None) -> None:
        """After halt, which has waited JOIN_S: a beat that still holds the lock isn't coming back, and the end goes."""
        locked = self.lock.acquire(blocking=False)
        try:
            self.stopped = True
            try:
                lines, root = self.tail.read()
            except Exception as e:  # noqa: BLE001 - a run dir gone: deliver_end tries from the files, if any come back
                _log(self.run_dir, self.now(), {"kind": "end", "error": type(e).__name__})
                return
            doc: Json = {"seq": self.seq, "lines": lines, "root": root, "outcome": outcome, "reason": reason}
            _send_end(self.site, self.token, self.run_dir, self.ticket, doc, self.now())
        finally:
            if locked:
                self.lock.release()


def thread_beats(tick: Callable[[], None], every: float) -> Callable[[], None]:
    """Call tick every `every` seconds on a daemon thread; returns what stops it. A thread that dies leaves a long gap."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            tick()

    thread = threading.Thread(target=loop, name="ticket-beats", daemon=True)
    thread.start()

    def halt() -> None:
        stop.set()
        thread.join(JOIN_S)

    return halt


def _last_seq(run_dir: Path) -> int:
    try:
        rows = (run_dir / BEATS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return -1
    seqs: list[int] = []
    for row in rows:
        try:
            seq = json.loads(row).get("seq")
        except (ValueError, AttributeError):
            continue
        if isinstance(seq, int):
            seqs.append(seq)
    return max(seqs, default=-1)


def deliver_end(
    run_dir: Path,
    site: Site,
    token: str | None,
    values: Sequence[str],
    now: Callable[[], float] = time.time,
    *,
    close: bool = False,
) -> bool | None:
    """Try again to tell the site how a run ended, from its ticket.json: the end kept there. With `close` (a matrix
    restart, whose queue shows that the attempt never ended) a run with no end kept sends an `interrupted` one over the
    lines the files hold. True when the site has the end, False when it hasn't, None when the run has no ticket.
    Never raises."""
    ticket = _read_json(run_dir / TICKET_FILE)
    if ticket is None:
        return None
    kept = ticket.get("end")
    if isinstance(kept, dict) and cast(Json, kept).get("delivered"):
        return True
    if not token:
        return False
    if isinstance(kept, dict):
        doc = {k: v for k, v in cast(Json, kept).items() if k != "delivered"}
    elif not close:
        return False
    else:
        try:
            lines, root = Tail(run_dir, ticket["nonce"], values).read()
        except Exception as e:  # noqa: BLE001 - the files can't be read: the ticket stays "no result"
            _log(run_dir, now(), {"kind": "end", "error": type(e).__name__})
            return False
        # + 2: a crash can lose the log line of a beat that did land, and the site keeps beats in arrival order anyway
        doc = {
            "seq": _last_seq(run_dir) + 2,
            "lines": lines,
            "root": root,
            "outcome": "invalid",
            "reason": "interrupted",
        }
    return _send_end(site, token, run_dir, ticket, doc, now())


class Inner(Protocol):
    """What Ticketed wraps: a run_one that names the directory its next run plays in."""

    def __call__(self, job: dict[str, Any]) -> matrix.RunResult: ...

    def run_dir(self, job: dict[str, Any]) -> Path: ...


class Ticketed:
    """run_one with a ticket per attempt. Keeps run_dir, which the matrix's loop reads for its start record."""

    def __init__(
        self,
        run_one: Inner,
        site: Site,
        *,
        token: Callable[[], str | None],
        values: Callable[[str | None], Sequence[str]],
        suite_tag: str | None = None,
        now: Callable[[], float] = time.time,
        start_beats: Callable[[Callable[[], None], float], Callable[[], None]] = thread_beats,
    ) -> None:
        self.run_one, self.site, self.token, self.values = run_one, site, token, values
        self.suite = suite_tag or suite.suite_version()
        self.now, self.start_beats = now, start_beats

    def run_dir(self, job: dict[str, Any]) -> Path:
        return self.run_one.run_dir(job)

    def _refused(self, why: str) -> matrix.RunResult:
        return matrix.RunResult(None, False, False, 0.0, f"ticket refused: {why}", True, refused=True)

    def _issue(self, job: Mapping[str, Any], token: str) -> Json | str:
        """A usable ticket from the site, or why there is none."""
        cell = {
            "suite": self.suite,
            "scenario": job["scenario"],
            "model_slug": job["model"],
            "client": job["client"],
            "condition": job["condition"],
        }
        try:
            status, body = self.site.issue(token, cell)
        except Exception as e:  # noqa: BLE001 - offline or the site down: resume when fixed
            return f"the site didn't answer: {type(e).__name__}"
        if status != 201:
            err: Any = body.get("error")
            code = cast(Json, err).get("code") if isinstance(err, dict) else None
            return f"the site answered HTTP {status}{f' ({code})' if code else ''}"
        fields = ("issued_at", "expires_at", "beat_s")
        if (
            not UUID.fullmatch(str(body.get("id")))
            or not HEX64.fullmatch(str(body.get("nonce")))
            or not all(type(body.get(k)) is int for k in fields)
            or body["beat_s"] < 1
        ):
            return "the site's answer isn't a ticket"
        return {k: body[k] for k in ("id", "nonce", *fields)} | {"site": self.site.base, "cell": cell}

    def _not_played(self, ticket: Json, token: str, why: str) -> matrix.RunResult:
        """The ticket exists and nothing will play under it: it ends not_played and counts as no attempt."""
        doc: Json = {"seq": 0, "lines": {}, "root": ticket_hash.root([]), "outcome": "not_played", "reason": "refused"}
        try:
            self.site.end(token, ticket["id"], doc)
        except Exception:  # noqa: BLE001, S110 - the run is refused either way; the open ticket shows as "no result"
            pass
        return self._refused(why)

    def __call__(self, job: dict[str, Any]) -> matrix.RunResult:
        run_dir = self.run_dir(job)
        token = self.token()
        if not token:
            return self._refused("no site token: create one on the site and save it in the panel's Settings")
        ticket = self._issue(job, token)
        if isinstance(ticket, str):
            return self._refused(ticket)
        off = ticket["issued_at"] - self.now()
        if abs(off) > MAX_CLOCK_S:
            return self._not_played(ticket, token, f"this machine's clock is {abs(off):.0f} s off the site's")
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            _write_json(run_dir / TICKET_FILE, ticket)
            values = self.values(token)
        except OSError as e:
            return self._not_played(ticket, token, f"can't write {TICKET_FILE}: {type(e).__name__}")
        beats = Beats(run_dir, ticket, self.site, token, values, self.now)
        halt = self.start_beats(beats.tick, ticket["beat_s"])
        try:
            result = self.run_one(job)
        except Exception:
            halt()
            beats.finish("invalid", "error")
            raise
        except BaseException:  # a second signal: no result is recorded, a matrix restart sends `interrupted`
            halt()
            raise
        halt()
        beats.finish(outcome_of(result), reason_of(result))
        return result

    def close_interrupted(self, jobs: Mapping[str, Mapping[str, Any]]) -> None:
        """A matrix restart: each attempt its queue shows as started and never ended sends its late end."""
        for st in jobs.values():
            run_dir = st["attempts"][-1]["run_dir"] if st["status"] == "interrupted" else None
            if run_dir:
                token = self.token()
                deliver_end(Path(run_dir), self.site, token, self.values(token), self.now, close=True)


def view(run_dir: Path) -> Json | None:
    """A run's ticket as the panel shows it (no nonce), or None: {"id", "issued_at", "expires_at", "beats_sent",
    "beats_landed", "beats_stopped", "end": {"outcome", "reason", "delivered"} or None}."""
    ticket = _read_json(run_dir / TICKET_FILE)
    if ticket is None:
        return None
    sent = landed = 0
    try:
        rows = (run_dir / BEATS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        rows = []
    for row in rows:
        try:
            rec: Any = json.loads(row)
        except ValueError:
            continue
        if isinstance(rec, dict) and "kind" not in rec:
            sent += 1
            landed += cast(Json, rec).get("status") == 204
    end: Any = ticket.get("end")
    return {
        "id": ticket.get("id"),
        "issued_at": ticket.get("issued_at"),
        "expires_at": ticket.get("expires_at"),
        "beats_sent": sent,
        "beats_landed": landed,
        "beats_stopped": ticket.get("beats_stopped"),
        "end": {k: cast(Json, end).get(k) for k in ("outcome", "reason", "delivered")}
        if isinstance(end, dict)
        else None,
    }


def http_site(base: str, environ: Mapping[str, str], urlopen: Open | None = None) -> Site:
    return Site(base, urlopen or open_url, access_headers(environ))


def from_env(
    run_one: Inner,
    repo: Path,
    environ: Mapping[str, str],
    urlopen: Open | None = None,
    now: Callable[[], float] = time.time,
) -> Ticketed:
    """Ticketed against the site this machine was told about ($BG3BENCH_SITE), the token from its file. ValueError for
    a missing or bad URL."""
    site = http_site(base_url(environ.get(SITE_ENV)), environ, urlopen)
    path = config_dir(environ) / SITE_TOKEN
    return Ticketed(run_one, site, token=lambda: read_token(path), values=lambda t: literals(repo / ".env", t), now=now)
