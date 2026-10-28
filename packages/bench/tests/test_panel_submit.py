# pyright: basic
import email.message
import io
import json
import subprocess
from pathlib import Path
from urllib import error as urlerror

import pytest

from bg3bench.serve import panel, submit, video

SHA = "ab" * 32
VID = "dQw4w9WgXcQ"


class FakeBundler:
    """Packs a run as bundle.json + result.json; the verdict is set by the test."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted, self.calls = accepted, []

    def pack(self, run_dir, out_dir, *, attempt, unsubmitted_valid, literals):
        self.calls.append(("pack", run_dir.name, out_dir.name, attempt, unsubmitted_valid, list(literals)))
        b = out_dir / "bundle"
        b.mkdir(parents=True)
        (b / "result.json").write_text('{"valid": true}')
        (b / "bundle.json").write_text(json.dumps({"files": [{"name": "result.json"}]}))
        return SHA, b

    def validate(self, bundle_dir, *, literals):
        self.calls.append(("validate", bundle_dir.parent.name))
        return {"accepted": self.accepted, "flags": [], "checks": [{"code": "F01", "ok": self.accepted}]}


class FakeSite:
    def __init__(self, post=(202, {"id": "sub1", "status": "received", "bundle_sha256": SHA}), get=None) -> None:
        self.post, self.get, self.calls = post, get or (200, {"status": "validating", "checks": []}), []

    def post_submission(self, token, files, youtube_url):
        self.calls.append(("post", token, [n for n, _ in files], youtube_url))
        return self.post

    def get_submission(self, token, submission_id):
        self.calls.append(("get", token, submission_id))
        return self.get


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=30",
        f"https://youtu.be/{VID}",
        f"https://m.youtube.com/shorts/{VID}",
        f" https://www.youtube.com/watch?v={VID} ",
    ],
)
def test_youtube_id(url):
    assert submit.youtube_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [None, "", VID, f"ftp://youtu.be/{VID}", f"https://evil.example/watch?v={VID}", "https://youtu.be/short",
     f"https://youtube.com/embed/{VID}", f"https://youtu.be/{VID}x", "https://youtube.com/watch?v=../../etc/pass"],
)  # fmt: skip
def test_youtube_id_refusals(url):
    with pytest.raises(panel.PanelError):
        submit.youtube_id(url)


@pytest.mark.parametrize(
    ("url", "base"),
    [("https://bench.example", "https://bench.example"), ("https://bench.example/", "https://bench.example"),
     ("http://127.0.0.1:8787", "http://127.0.0.1:8787")],
)  # fmt: skip
def test_site_url(url, base):
    assert submit.site_url(url) == base


@pytest.mark.parametrize("url", [None, "", "http://bench.example", "https://bench.example/x", "https://b.example?a=1"])
def test_site_url_refusals(url):
    with pytest.raises(panel.PanelError):
        submit.site_url(url)


def rows():
    def r(d, created, valid=True, scenario="s", model="m"):
        return {
            "dir": d,
            "created": created,
            "valid": valid,
            "scenario": scenario,
            "model": model,
            "condition": "rules",
        }

    return [r("a", "1"), r("b", "2", valid=False), r("c", "3"), r("d", "4"), r("x", "0", model="other")]


def test_attempt_counts_the_cell_and_earlier_unsubmitted_valid_runs():
    assert submit.attempt(rows(), "a", lambda d: False) == (1, 0)
    assert submit.attempt(rows(), "d", lambda d: False) == (4, 2)  # a and c; b is invalid
    assert submit.attempt(rows(), "d", lambda d: d == "a") == (4, 1)
    assert submit.attempt(rows(), "x", lambda d: False) == (1, 0)
    with pytest.raises(panel.PanelError, match="no finished run"):
        submit.attempt(rows(), "zz", lambda d: False)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions" / "s1"
    d.mkdir(parents=True)
    return d


def test_prepare_upload_poll(run_dir, tmp_path):
    root, b, site = tmp_path / "submissions", FakeBundler(), FakeSite()
    view = submit.prepare(run_dir, root, b, attempt_n=2, unsubmitted_valid=1, literals=["secret-value"])
    assert (
        view["bundle_sha256"] == SHA
        and view["verdict"]["accepted"]
        and view["attempt"] == {"n": 2, "unsubmitted_valid": 1}
    )
    assert view["files"] == [{"name": "bundle.json", "bytes": 36}, {"name": "result.json", "bytes": 15}]
    assert b.calls[0] == ("pack", "s1", "1", 2, 1, ["secret-value"])
    assert submit.prepare(run_dir, root, b, attempt_n=2, unsubmitted_valid=1, literals=[])["bundle_dir"].endswith(
        "/2/bundle"
    )
    doc = submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=f"https://youtu.be/{VID}", token="tok", site=site)
    assert doc["id"] == "sub1" and doc["status"] == "received" and doc["youtube_id"] == VID
    assert site.calls == [("post", "tok", ["bundle.json", "result.json"], f"https://www.youtube.com/watch?v={VID}")]
    assert submit.submitted(root, "s1")
    with pytest.raises(panel.PanelError, match="submitted already"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=f"https://youtu.be/{VID}", token="tok", site=site)
    with pytest.raises(panel.PanelError, match="submitted already"):
        submit.prepare(run_dir, root, b, attempt_n=2, unsubmitted_valid=1, literals=[])
    site.get = (200, {"status": "accepted", "flags": ["quarantined"], "run_url": "https://b/runs/1", "secret": "x"})
    doc = submit.poll(root, "s1", token="tok", site=site)
    assert doc["status"] == "accepted" and doc["flags"] == ["quarantined"] and "secret" not in doc
    assert submit.state(root, "s1")["submission"]["status"] == "accepted"


def test_upload_refusals(run_dir, tmp_path):
    root, site = tmp_path / "submissions", FakeSite()
    yt = f"https://youtu.be/{VID}"
    with pytest.raises(panel.PanelError, match="validate it first"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=yt, token="tok", site=site)
    with pytest.raises(panel.PanelError, match="bad run"):
        submit.upload(root, "../x", bundle_sha256=SHA, youtube_url=yt, token="tok", site=site)
    submit.prepare(run_dir, root, FakeBundler(accepted=False), attempt_n=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match="rejects this bundle"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=yt, token="tok", site=site)
    submit.prepare(run_dir, root, FakeBundler(), attempt_n=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match="changed since you saw it"):
        submit.upload(root, "s1", bundle_sha256="cd" * 32, youtube_url=yt, token="tok", site=site)
    with pytest.raises(panel.PanelError, match="no site token"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=yt, token=None, site=site)
    with pytest.raises(panel.PanelError, match="youtube_url"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url="https://vimeo.com/1", token="tok", site=site)
    (root / "s1" / "2" / "bundle" / "result.json").write_text('{"valid": true, "more": 1}')
    with pytest.raises(panel.PanelError, match="files changed since validation"):
        submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=yt, token="tok", site=site)
    assert site.calls == [] and not submit.submitted(root, "s1")


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ((409, {"error": {"code": "duplicate", "message": "submitted before"}}), "HTTP 409.*submitted before"),
        ((422, {"error": {"code": "checks", "checks": [{"code": "S01", "ok": False}, {"code": "F01", "ok": True}]}}),
         "failed checks: S01$"),
        ((429, {"error": {"code": "rate"}, "retry_after": "120"}), "try again in 120 s"),
        ((202, {"status": "received"}), "HTTP 202"),
    ],
)  # fmt: skip
def test_site_refusals_are_shown_and_nothing_is_recorded(run_dir, tmp_path, answer, message):
    root = tmp_path / "submissions"
    submit.prepare(run_dir, root, FakeBundler(), attempt_n=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match=message):
        submit.upload(
            root, "s1", bundle_sha256=SHA, youtube_url=f"https://youtu.be/{VID}", token="t", site=FakeSite(answer)
        )
    assert not submit.submitted(root, "s1")


def test_bundle_listing_refuses_names_outside_the_bundle(tmp_path):
    (tmp_path / "bundle.json").write_text(json.dumps({"files": [{"name": "../manifest.json"}]}))
    with pytest.raises(panel.PanelError, match="not a bare name"):
        submit.bundle_listing(tmp_path)


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers=None) -> None:
        super().__init__(body)
        self.status, self.headers = status, headers or {}


def test_http_site_posts_multipart_with_the_bearer_token():
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return FakeResponse(202, b'{"id": "sub1", "status": "received"}')

    site = submit.HttpSite("https://bench.example", urlopen=urlopen, new_boundary=lambda: "BOUND")
    status, body = site.post_submission("bgb_tok", [("bundle.json", b"{}")], "https://www.youtube.com/watch?v=x")
    assert status == 202 and body["id"] == "sub1"
    (req,) = seen
    assert req.full_url == "https://bench.example/api/submissions" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer bgb_tok"
    assert req.get_header("Content-type") == "multipart/form-data; boundary=BOUND"
    assert req.data == (
        b'--BOUND\r\nContent-Disposition: form-data; name="bundle.json"; filename="bundle.json"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n{}\r\n"
        b'--BOUND\r\nContent-Disposition: form-data; name="youtube_url"\r\n'
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nhttps://www.youtube.com/watch?v=x\r\n--BOUND--\r\n"
    )


def test_http_site_errors_and_status():
    def refuse(req, timeout):
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "30"
        raise urlerror.HTTPError(req.full_url, 429, "", hdrs, io.BytesIO(b'{"error": {"code": "rate"}}'))

    site = submit.HttpSite("https://bench.example", urlopen=refuse)
    assert site.get_submission("t", "sub1") == (429, {"error": {"code": "rate"}, "retry_after": "30"})
    with pytest.raises(panel.PanelError, match="bad submission id"):
        site.get_submission("t", "../admin")

    def down(req, timeout):
        raise urlerror.URLError("connection refused")

    with pytest.raises(panel.PanelError, match="didn't answer: connection refused"):
        submit.HttpSite("https://bench.example", urlopen=down).get_submission("t", "sub1")
    html = submit.HttpSite("https://bench.example", urlopen=lambda req, timeout: FakeResponse(502, b"<html>"))
    assert html.get_submission("t", "sub1")[1]["error"]["code"] == "not_json"


def test_bench_submission_without_the_submission_package(tmp_path, monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(submit.importlib, "import_module", missing)
    with pytest.raises(panel.PanelError, match="no bg3bench.submission"):
        submit.BenchSubmission(None, "0").pack(tmp_path, tmp_path, attempt=1, unsubmitted_valid=0, literals=[])


# ---------------------------------------------------------------- the renderer


class FakeProc:
    pid = 7

    def __init__(self) -> None:
        self.rc: int | None = None

    def poll(self):
        return self.rc

    def send_signal(self, sig):  # pragma: no cover - a render is never signalled
        raise AssertionError


def test_video_job_and_command(run_dir):
    man = {"run_id": "s1-r001", "scenario": {"name": "act1_get_item"}}
    res = {"run_id": "s1-r001", "t_pass_s": 351.0, "cost_kind": "billed"}
    sc = {"title": "Get the item", "task": "Find it.", "caps": {"run_hours": 0.75}}
    bundle = run_dir / "bundle"
    job = video.job_for(run_dir, man, res, sc, source=bundle, run_hash=SHA, site="https://bench.example")
    assert video.command("py", job) == [
        "py", "-m", "bg3harness.video", str(bundle), "--index", str(run_dir / "recording" / "index.json"),
        "--out", str(run_dir), "--run-id", "s1-r001", "--run-hash", SHA, "--cost-kind", "billed",
        "--site", "bench.example", "--title", "Get the item", "--task", "Find it.", "--cap-min", "45",
        "--t-pass-s", "351",
    ]  # fmt: skip
    bare = video.job_for(run_dir, {}, {}, None, source=bundle, run_hash=SHA, site=None)
    assert video.command("py", bare)[-4:] == ["--run-hash", SHA, "--cost-kind", "billed"] and bare.run_id == "s1"


def test_video_run_hash_reads_bundle_json(tmp_path):
    (tmp_path / "bundle.json").write_text(json.dumps({"run_hash": SHA}))
    assert video.run_hash(tmp_path) == SHA
    for doc in ({"files": []}, {"run_hash": "AB" * 32}, {"run_hash": 5}, []):
        (tmp_path / "bundle.json").write_text(json.dumps(doc))
        with pytest.raises(panel.PanelError, match="no run_hash"):
            video.run_hash(tmp_path)
    (tmp_path / "bundle.json").unlink()
    with pytest.raises(panel.PanelError, match="no run_hash"):
        video.run_hash(tmp_path)


def test_video_process(run_dir):
    calls, procs = [], []

    def popen(argv, **kw):
        calls.append((argv, kw))
        procs.append(FakeProc())
        return procs[-1]

    vp = video.VideoProcess(popen=popen, python="py", now=lambda: 5.0)
    job = video.job_for(run_dir, {}, {}, None, source=run_dir / "bundle", run_hash=SHA, site=None)
    assert vp.state() is None
    with pytest.raises(panel.PanelError, match="no recording"):
        vp.start(job)
    (run_dir / "recording").mkdir()
    (run_dir / "recording" / "index.json").write_text('{"v": 1, "segments": []}')
    vp.start(job)
    assert calls[0][0][:3] == ["py", "-m", "bg3harness.video"] and calls[0][1]["stderr"] == subprocess.STDOUT
    assert vp.state() == {"run": "s1", "running": True, "returncode": None, "started": 5.0,
                          "log": str(run_dir / "render.log"), "video": None, "video_bytes": None}  # fmt: skip
    with pytest.raises(panel.PanelError, match="one render at a time"):
        vp.start(job)
    (run_dir / "evidence.mp4").write_bytes(b"x" * 10)
    procs[0].rc = 0
    st = vp.state()
    assert (
        st is not None
        and st["video"] == str(run_dir / "evidence.mp4")
        and st["video_bytes"] == 10
        and not st["running"]
    )


def test_bench_submission_calls_pack_and_validate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    class PackError(ValueError):
        pass

    seen = {}

    def pack(run_dir, out_dir, *, attempt, literals, panel_version):
        seen["pack"] = (run_dir, out_dir, attempt, list(literals), panel_version)
        if run_dir.name == "pilot":
            raise PackError("pilot: refused, a pilot run")
        return SimpleNamespace(bundle_sha256=SHA, bundle_dir=out_dir / "bundle")

    class Verdict:
        def as_json(self):
            return {"accepted": True}

    def validate(bundle_dir, release, *, where, literals):
        seen["validate"] = (bundle_dir, release, where, list(literals))
        return Verdict()

    mods = {
        "pack": SimpleNamespace(pack=pack, Attempt=lambda n, u: (n, u), PackError=PackError),
        "validate": SimpleNamespace(validate=validate, Release=SimpleNamespace(from_json=lambda d: d["suite"])),
    }
    monkeypatch.setattr(submit.importlib, "import_module", lambda name: mods[name.rsplit(".", 1)[1]])
    release = tmp_path / "release.json"
    bs = submit.BenchSubmission(release, "0.1.0")
    assert bs.pack(tmp_path / "r1", tmp_path / "o", attempt=3, unsubmitted_valid=1, literals=["k"]) == (
        SHA,
        tmp_path / "o" / "bundle",
    )
    assert seen["pack"] == (tmp_path / "r1", tmp_path / "o", (3, 1), ["k"], "0.1.0")
    with pytest.raises(panel.PanelError, match="a pilot run"):
        bs.pack(tmp_path / "pilot", tmp_path / "o", attempt=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match="no release file"):
        bs.validate(tmp_path, literals=[])
    release.write_text('{"suite": "bench-v1"}')
    assert bs.validate(tmp_path / "b", literals=["k"]) == {"accepted": True}
    assert seen["validate"] == (tmp_path / "b", "bench-v1", "panel", ["k"])


def test_poll_refusals(run_dir, tmp_path):
    root = tmp_path / "submissions"
    with pytest.raises(panel.PanelError, match="not submitted"):
        submit.poll(root, "s1", token="t", site=FakeSite())
    submit.prepare(run_dir, root, FakeBundler(), attempt_n=1, unsubmitted_valid=0, literals=[])
    submit.upload(root, "s1", bundle_sha256=SHA, youtube_url=f"https://youtu.be/{VID}", token="t", site=FakeSite())
    with pytest.raises(panel.PanelError, match="no site token"):
        submit.poll(root, "s1", token=None, site=FakeSite())
    gone = FakeSite(get=(404, {"error": {"code": "not_found"}}))
    with pytest.raises(panel.PanelError, match="HTTP 404.*not_found"):
        submit.poll(root, "s1", token="t", site=gone)
    assert submit.state(root, "s1")["submission"]["status"] == "received"


def test_bundle_listing_needs_an_envelope(tmp_path):
    with pytest.raises(panel.PanelError, match="no readable bundle.json"):
        submit.bundle_listing(tmp_path)
