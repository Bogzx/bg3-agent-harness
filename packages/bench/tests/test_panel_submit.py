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

    def validate(self, bundle_dir, *, literals, submission):
        self.calls.append(("validate", bundle_dir.parent.name, submission))
        return {"accepted": self.accepted, "flags": [], "checks": [{"code": "F01", "ok": self.accepted}]}

    def add_render(self, render_dir, out_dir, *, literals):
        self.calls.append(("add_render", render_dir.name, out_dir.name, list(literals)))
        if (render_dir / "refuse").exists():
            raise panel.PanelError("render.json was rendered for another run_hash")
        b = out_dir / "bundle"
        (b / "render.json").write_text("{}")
        names = [{"name": "result.json"}, {"name": "render.json"}]
        (b / "bundle.json").write_text(json.dumps({"files": names}))
        return "ef" * 32, b

    def add_video(self, render_dir, out_dir, *, youtube):
        """Takes youtu.be/<id> or watch?v=<id> URLs, like pack.youtube_id (tested in the bench)."""
        self.calls.append(("add_video", render_dir.name, out_dir.name, youtube))
        youtube_id = youtube.rsplit("/", 1)[-1].rsplit("=", 1)[-1]
        if "youtu" not in youtube:
            raise panel.PanelError(f"{youtube!r} is not a YouTube video id or a youtube.com/watch?v=, youtu.be/ URL")
        if (render_dir / video.VIDEO).read_text() == "refuse":
            raise panel.PanelError("render.json was rendered for another run_hash")
        b = out_dir / "bundle"
        env = json.loads((b / "bundle.json").read_text())
        (b / "bundle.json").write_text(json.dumps({**env, "video": {"youtube_id": youtube_id}}))
        return "0a" * 32, b


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

    # epoch seconds (panel.results): d's 10.0 sorts after c's 9.5 as a number, not as text
    return [r("a", 1.0), r("b", 2.0, valid=False), r("c", 9.5), r("d", 10.0), r("x", 0.0, model="other")]


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


def final(root: Path, run: str = "s1", video: bool = True) -> None:
    """The prepared bundle as add_render (its render in, validated as a submission) and, with `video`, add_video leave
    it: Upload takes it."""
    f = root / run / submit.PREPARED
    prepared, bdir = json.loads(f.read_text()), Path(json.loads(f.read_text())["bundle_dir"])
    if video:
        env = bdir / "bundle.json"
        env.write_text(json.dumps({**json.loads(env.read_text()), "video": {"youtube_id": VID}}))
    f.write_text(json.dumps({**prepared, "final": True, "files": submit.bundle_listing(bdir)}))


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
    assert b.calls[1] == ("validate", "1", False) and view["final"] is False  # the record only, before the render
    assert submit.prepare(run_dir, root, b, attempt_n=2, unsubmitted_valid=1, literals=[])["bundle_dir"].endswith(
        "/2/bundle"
    )
    final(root)
    doc = submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)
    assert doc["id"] == "sub1" and doc["status"] == "received" and doc["youtube_id"] == VID
    assert site.calls == [("post", "tok", ["bundle.json", "result.json"], f"https://www.youtube.com/watch?v={VID}")]
    assert submit.submitted(root, "s1")
    with pytest.raises(panel.PanelError, match="submitted already"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)
    with pytest.raises(panel.PanelError, match="submitted already"):
        submit.prepare(run_dir, root, b, attempt_n=2, unsubmitted_valid=1, literals=[])
    site.get = (200, {"status": "accepted", "flags": ["quarantined"], "run_url": "https://b/runs/1", "secret": "x"})
    doc = submit.poll(root, "s1", token="tok", site=site)
    assert doc["status"] == "accepted" and doc["flags"] == ["quarantined"] and "secret" not in doc
    assert submit.state(root, "s1")["submission"]["status"] == "accepted"


def test_upload_refusals(run_dir, tmp_path):
    root, site = tmp_path / "submissions", FakeSite()
    with pytest.raises(panel.PanelError, match="validate it first"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)
    with pytest.raises(panel.PanelError, match="bad run"):
        submit.upload(root, "../x", bundle_sha256=SHA, token="tok", site=site)
    submit.prepare(run_dir, root, FakeBundler(accepted=False), attempt_n=1, unsubmitted_valid=0, literals=[])
    final(root)
    with pytest.raises(panel.PanelError, match="rejects this bundle"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)
    submit.prepare(run_dir, root, FakeBundler(), attempt_n=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match="render the evidence video first"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)  # accepted, no render
    final(root, video=False)
    with pytest.raises(panel.PanelError, match="add the YouTube link of its evidence video first"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)  # rendered, bundle.json has no video
    final(root)
    with pytest.raises(panel.PanelError, match="changed since you saw it"):
        submit.upload(root, "s1", bundle_sha256="cd" * 32, token="tok", site=site)
    with pytest.raises(panel.PanelError, match="no site token"):
        submit.upload(root, "s1", bundle_sha256=SHA, token=None, site=site)
    (root / "s1" / "2" / "bundle" / "result.json").write_text('{"valid": true, "more": 1}')
    with pytest.raises(panel.PanelError, match="files changed since validation"):
        submit.upload(root, "s1", bundle_sha256=SHA, token="tok", site=site)
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
    final(root)
    with pytest.raises(panel.PanelError, match=message):
        submit.upload(root, "s1", bundle_sha256=SHA, token="t", site=FakeSite(answer))
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
    out = run_dir / "render-1"
    job = video.job_for(run_dir, man, res, sc, source=bundle, out=out, run_hash=SHA, site="https://bench.example")
    assert video.command("py", job) == [
        "py", "-m", "bg3harness.video", str(bundle), "--index", str(run_dir / "recording" / "index.json"),
        "--out", str(out), "--run-id", "s1-r001", "--run-hash", SHA, "--cost-kind", "billed",
        "--site", "bench.example", "--title", "Get the item", "--task", "Find it.", "--cap-min", "45",
        "--t-pass-s", "351",
    ]  # fmt: skip
    bare = video.job_for(run_dir, {}, {}, None, source=bundle, out=out, run_hash=SHA, site=None)
    assert video.command("py", bare)[-4:] == ["--run-hash", SHA, "--cost-kind", "billed"] and bare.run_id == "s1"


@pytest.mark.parametrize(
    ("res", "flags"),
    [
        ({"passed": True, "outcome": "reached", "deaths": {"wipes": 1}}, []),  # a pass has no ending
        ({"passed": False, "outcome": "timeout"}, ["--ending", "TIME CAP"]),
        ({"passed": None, "outcome": "timeout"}, ["--ending", "TIME CAP"]),  # a maximise run ends on its cap
        ({"passed": False, "outcome": "over_budget"}, ["--ending", "COST CAP"]),
        ({"passed": False, "outcome": "failed", "deaths": {"fights": 2, "wipes": 1}}, ["--ending", "WIPED"]),
        ({"passed": False, "outcome": "failed", "deaths": {"wipes": 0}}, []),  # a fired never: no ending to show
        ({"passed": False, "outcome": "timeout", "pilot": ["memory"]}, ["--pilot", "--ending", "TIME CAP"]),
        ({"passed": True, "outcome": "reached", "pilot": ["memory"]}, ["--pilot"]),
    ],
)
def test_video_command_passes_pilot_and_ending_from_the_result(run_dir, res, flags):
    job = video.job_for(run_dir, {}, res, None, source=run_dir / "b", out=run_dir / "r", run_hash=SHA, site=None)
    argv = video.command("py", job)
    assert argv[argv.index("billed") + 1 :] == flags


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
    out = run_dir / "render-1"
    job = video.job_for(run_dir, {}, {}, None, source=run_dir / "bundle", out=out, run_hash=SHA, site=None)
    assert vp.state() is None
    with pytest.raises(panel.PanelError, match="no recording"):
        vp.start(job)
    (run_dir / "recording").mkdir()
    (run_dir / "recording" / "index.json").write_text('{"v": 1, "segments": []}')
    vp.start(job)
    assert calls[0][0][:3] == ["py", "-m", "bg3harness.video"] and calls[0][1]["stderr"] == subprocess.STDOUT
    assert calls[0][0][calls[0][0].index("--out") + 1] == str(out) and out.is_dir()
    assert vp.state() == {"run": "s1", "source": str(run_dir / "bundle"), "out": str(out), "running": True,
                          "returncode": None, "started": 5.0, "log": str(out / "render.log"), "video": None,
                          "video_bytes": None}  # fmt: skip
    with pytest.raises(panel.PanelError, match="one render at a time"):
        vp.start(job)
    (out / "evidence.mp4").write_bytes(b"x" * 10)
    procs[0].rc = 0
    st = vp.state()
    assert st is not None and st["video"] == str(out / "evidence.mp4") and st["video_bytes"] == 10 and not st["running"]


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

    def validate(bundle_dir, release, *, where, submission, literals):
        seen["validate"] = (bundle_dir, release, where, submission, list(literals))
        return Verdict()

    def add_render(render_dir, out_dir, literals):
        seen["add_render"] = (render_dir, out_dir, list(literals))
        if render_dir.name == "other":
            raise PackError("render.json was rendered for another run_hash")
        return SimpleNamespace(bundle_sha256="ef" * 32, bundle_dir=out_dir / "bundle")

    def add_video(render_dir, out_dir, youtube):  # pack.add_video(render_dir, out_dir, youtube) -> Packed
        seen["add_video"] = (render_dir, out_dir, youtube)
        if youtube == "x" * 11:
            raise PackError("render.json was rendered for another run_hash")
        return SimpleNamespace(bundle_sha256="0a" * 32, bundle_dir=out_dir / "bundle")

    mods = {
        "pack": SimpleNamespace(
            pack=pack, add_render=add_render, add_video=add_video, Attempt=lambda n, u: (n, u), PackError=PackError
        ),
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
        bs.validate(tmp_path, literals=[], submission=False)
    release.write_text('{"suite": "bench-v1"}')
    assert bs.validate(tmp_path / "b", literals=["k"], submission=True) == {"accepted": True}
    assert seen["validate"] == (tmp_path / "b", "bench-v1", "panel", True, ["k"])
    assert bs.add_render(tmp_path / "render-1", tmp_path / "o", literals=["k"]) == (
        "ef" * 32,
        tmp_path / "o" / "bundle",
    )
    assert seen["add_render"] == (tmp_path / "render-1", tmp_path / "o", ["k"])
    with pytest.raises(panel.PanelError, match="another run_hash"):
        bs.add_render(tmp_path / "other", tmp_path / "o", literals=[])
    assert bs.add_video(tmp_path / "render-1", tmp_path / "o", youtube=VID) == ("0a" * 32, tmp_path / "o" / "bundle")
    assert seen["add_video"] == (tmp_path / "render-1", tmp_path / "o", VID)
    with pytest.raises(panel.PanelError, match="another run_hash"):
        bs.add_video(tmp_path / "render-1", tmp_path / "o", youtube="x" * 11)


def test_add_render_updates_the_prepared_bundle(run_dir, tmp_path):
    root, b = tmp_path / "submissions", FakeBundler()
    view = submit.prepare(run_dir, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])
    bdir = Path(view["bundle_dir"])
    render = bdir.parent / "render-1"
    render.mkdir()
    got = submit.add_render(root, "s1", b, bundle_dir=bdir, render_dir=render, literals=["k"], now=9.0)
    assert b.calls[-2] == ("add_render", "render-1", "1", ["k"])
    assert (
        got["bundle_sha256"] == "ef" * 32 and got["verdict"] == view["verdict"]
    )  # the run_hash, so the verdict, stays
    assert [f["name"] for f in got["files"]] == ["bundle.json", "result.json", "render.json"]
    assert got["render"] == {"dir": str(render), "added": 9.0} and submit.state(root, "s1")["prepared"] == got
    assert b.calls[-1] == ("validate", "1", True) and got["final"] is True  # validated again with its render


def test_add_render_refusals(run_dir, tmp_path):
    root, b = tmp_path / "submissions", FakeBundler()
    view = submit.prepare(run_dir, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])
    bdir = Path(view["bundle_dir"])
    render = bdir.parent / "render-1"
    render.mkdir()
    (render / "refuse").write_text("")
    got = submit.add_render(root, "s1", b, bundle_dir=bdir, render_dir=render, literals=[], now=9.0)
    assert got["render"] == {"dir": str(render), "error": "render.json was rendered for another run_hash"}
    assert got["bundle_sha256"] == SHA  # the bundle is unchanged
    with pytest.raises(panel.PanelError, match="no longer the prepared one"):
        submit.add_render(
            root, "s1", b, bundle_dir=tmp_path / "old" / "bundle", render_dir=render, literals=[], now=9.0
        )
    with pytest.raises(panel.PanelError, match="no longer the prepared one"):
        submit.add_render(root, "s2", b, bundle_dir=bdir, render_dir=render, literals=[], now=9.0)
    final(root)
    submit.upload(root, "s1", bundle_sha256=SHA, token="t", site=FakeSite())
    with pytest.raises(panel.PanelError, match="submitted already"):
        submit.add_render(root, "s1", b, bundle_dir=bdir, render_dir=render, literals=[], now=9.0)


def rendered(run_dir: Path, root: Path, b: FakeBundler, mp4: str = "mp4") -> tuple[dict, Path]:
    """A prepared bundle with its render added, the render's evidence.mp4 beside it: (the prepared view, render dir)."""
    bdir = Path(submit.prepare(run_dir, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])["bundle_dir"])
    render = bdir.parent / "render-1"
    render.mkdir()
    (render / video.VIDEO).write_text(mp4)
    return submit.add_render(root, "s1", b, bundle_dir=bdir, render_dir=render, literals=[], now=9.0), render


def test_add_video_names_the_upload_in_bundle_json_and_upload_sends_it(run_dir, tmp_path):
    root, b, site = tmp_path / "submissions", FakeBundler(), FakeSite()
    view, _ = rendered(run_dir, root, b)
    assert "video" not in view and submit.bundle_video(Path(view["bundle_dir"])) is None
    with pytest.raises(panel.PanelError, match="add the YouTube link"):
        submit.upload(root, "s1", bundle_sha256=view["bundle_sha256"], token="tok", site=site)
    got = submit.add_video(
        root, "s1", b, bundle_sha256="ef" * 32, youtube_url=f"https://www.youtube.com/watch?v={VID}", literals=["k"]
    )
    assert b.calls[-2:] == [
        ("add_video", "render-1", "1", f"https://www.youtube.com/watch?v={VID}"),
        ("validate", "1", True),
    ]  # validated again
    assert got["bundle_sha256"] == "0a" * 32 and got["video"] == {"youtube_id": VID} and got["final"] is True
    assert submit.state(root, "s1")["prepared"] == got and submit.bundle_video(Path(got["bundle_dir"])) == VID
    with pytest.raises(panel.PanelError, match="changed since you saw it"):
        submit.upload(root, "s1", bundle_sha256="ef" * 32, token="tok", site=site)
    doc = submit.upload(root, "s1", bundle_sha256="0a" * 32, token="tok", site=site)
    assert doc["youtube_id"] == VID
    assert site.calls == [("post", "tok", ["bundle.json", "result.json", "render.json"],
                           f"https://www.youtube.com/watch?v={VID}")]  # fmt: skip


def test_add_video_refusals(run_dir, tmp_path):
    root, b = tmp_path / "submissions", FakeBundler()
    yt = f"https://youtu.be/{VID}"
    with pytest.raises(panel.PanelError, match="validate it first"):
        submit.add_video(root, "s1", b, bundle_sha256=SHA, youtube_url=yt, literals=[])
    submit.prepare(run_dir, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])
    with pytest.raises(panel.PanelError, match="render the evidence video first"):
        submit.add_video(root, "s1", b, bundle_sha256=SHA, youtube_url=yt, literals=[])
    view, render = rendered(run_dir, root, b, mp4="refuse")
    sha = view["bundle_sha256"]
    with pytest.raises(panel.PanelError, match="changed since you saw it"):
        submit.add_video(root, "s1", b, bundle_sha256=SHA, youtube_url=yt, literals=[])
    with pytest.raises(panel.PanelError, match="youtube_url"):
        submit.add_video(root, "s1", b, bundle_sha256=sha, youtube_url=None, literals=[])
    with pytest.raises(panel.PanelError, match="not a YouTube video id"):  # the pack's check
        submit.add_video(root, "s1", b, bundle_sha256=sha, youtube_url="https://vimeo.com/1", literals=[])
    with pytest.raises(panel.PanelError, match="another run_hash"):
        submit.add_video(root, "s1", b, bundle_sha256=sha, youtube_url=yt, literals=[])  # the pack's refusal
    (render / video.VIDEO).unlink()
    with pytest.raises(panel.PanelError, match="evidence.mp4 is gone: render the video again"):
        submit.add_video(root, "s1", b, bundle_sha256=sha, youtube_url=yt, literals=[])
    assert submit.state(root, "s1")["prepared"] == view  # nothing changed


def test_video_out_dir_is_new_each_time(tmp_path):
    b = tmp_path / "1" / "bundle"
    b.mkdir(parents=True)
    assert video.out_dir(b) == tmp_path / "1" / "render-1"
    (tmp_path / "1" / "render-1").mkdir()
    (tmp_path / "1" / "render-x").mkdir()
    assert video.out_dir(b) == tmp_path / "1" / "render-2"


def test_poll_refusals(run_dir, tmp_path):
    root = tmp_path / "submissions"
    with pytest.raises(panel.PanelError, match="not submitted"):
        submit.poll(root, "s1", token="t", site=FakeSite())
    submit.prepare(run_dir, root, FakeBundler(), attempt_n=1, unsubmitted_valid=0, literals=[])
    final(root)
    submit.upload(root, "s1", bundle_sha256=SHA, token="t", site=FakeSite())
    with pytest.raises(panel.PanelError, match="no site token"):
        submit.poll(root, "s1", token=None, site=FakeSite())
    gone = FakeSite(get=(404, {"error": {"code": "not_found"}}))
    with pytest.raises(panel.PanelError, match="HTTP 404.*not_found"):
        submit.poll(root, "s1", token="t", site=gone)
    assert submit.state(root, "s1")["submission"]["status"] == "received"


def test_bundle_listing_needs_an_envelope(tmp_path):
    with pytest.raises(panel.PanelError, match="no readable bundle.json"):
        submit.bundle_listing(tmp_path)


class PackOnly(submit.BenchSubmission):
    """The real pack, add_render and add_video; a fixed verdict (validating needs a release file, which this test isn't about)."""

    def validate(self, bundle_dir, *, literals, submission):
        return {"accepted": submission, "flags": [], "checks": []}


def test_add_render_with_the_real_pack(tmp_path):
    """Real integration: bench's pack.pack makes the bundle from its fixture run, the panel reads the run_hash the
    renderer gets, bench's pack.add_render puts the render into it through submit.add_render, and pack.add_video
    names the YouTube upload of its evidence.mp4 in bundle.json through submit.add_video."""
    import hashlib
    import tarfile

    from bg3bench.submission import bundle

    fixture = Path(__file__).parent / "fixtures" / "submission" / "fight_act2_balthazar.bundle.tar.gz"
    run = tmp_path / "sessions" / "balthazar"
    run.mkdir(parents=True)
    with tarfile.open(fixture) as tar:
        for m in tar.getmembers():
            f = tar.extractfile(m)
            if f is not None and m.name != bundle.ENVELOPE:
                (run / m.name).write_bytes(f.read())
    root, bs = tmp_path / "submissions", PackOnly(None, "0.1.0")
    view = submit.prepare(run, root, bs, attempt_n=1, unsubmitted_valid=0, literals=[])
    bdir = Path(view["bundle_dir"])
    run_hash = video.run_hash(bdir)  # what Render passes as --run-hash
    render = video.out_dir(bdir)
    (render / "frames").mkdir(parents=True)
    (render / "render.json").write_text(
        json.dumps({"schema": 1, "run_hash": run_hash, "video_start": 0, "video_end": 1, "fps": 15, "events": []})
    )
    for i in range(2):
        (render / "frames" / f"{i}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    got = submit.add_render(root, "balthazar", bs, bundle_dir=bdir, render_dir=render, literals=[], now=5.0)
    assert "error" not in got["render"], got["render"]
    env_bytes = (bdir / bundle.ENVELOPE).read_bytes()
    assert got["bundle_sha256"] == hashlib.sha256(env_bytes).hexdigest() != view["bundle_sha256"]
    assert json.loads(env_bytes)["run_hash"] == run_hash  # the render doesn't change the run hash
    names = {f["name"] for f in got["files"]}
    assert {"render.json", bundle.FRAMES_TAR} <= names
    assert submit.state(root, "balthazar")["prepared"] == got and got["final"] is True
    # the user uploaded evidence.mp4 (a 12 s movie header, as ffmpeg writes it) and pastes its link
    mvhd = bytes(12) + (1000).to_bytes(4, "big") + (12000).to_bytes(4, "big") + bytes(80)
    mp4 = b"\0\0\0\x10ftypisom\0\0\2\0" + (8 + 8 + 8 + len(mvhd)).to_bytes(4, "big") + b"moov"
    mp4 += b"\0\0\0\x08trak" + (8 + len(mvhd)).to_bytes(4, "big") + b"mvhd" + mvhd
    (render / video.VIDEO).write_bytes(mp4)
    with pytest.raises(panel.PanelError, match="not a YouTube video id"):
        submit.add_video(root, "balthazar", bs, bundle_sha256=got["bundle_sha256"], youtube_url="x", literals=[])
    yt = f"https://youtu.be/{VID}"
    done = submit.add_video(root, "balthazar", bs, bundle_sha256=got["bundle_sha256"], youtube_url=yt, literals=[])
    env = json.loads((bdir / bundle.ENVELOPE).read_bytes())
    assert env["video"] == {"youtube_id": VID, "duration_s": 12.0, "mp4_sha256": hashlib.sha256(mp4).hexdigest()}
    assert env["run_hash"] == run_hash and done["video"] == {"youtube_id": VID}
    assert done["bundle_sha256"] == hashlib.sha256((bdir / bundle.ENVELOPE).read_bytes()).hexdigest()
    assert done["verdict"]["accepted"] is True and submit.state(root, "balthazar")["prepared"] == done
    # a second render of the same bundle is refused by pack and kept in prepared.json, the bundle unchanged
    again = submit.add_render(root, "balthazar", bs, bundle_dir=bdir, render_dir=render, literals=[], now=6.0)
    assert "already holds a render" in again["render"]["error"] and again["bundle_sha256"] == done["bundle_sha256"]


def test_add_render_keeps_a_failed_final_validation(run_dir, tmp_path):
    class NoFinal(FakeBundler):
        def validate(self, bundle_dir, *, literals, submission):
            if submission:
                raise panel.PanelError("no release file")
            return super().validate(bundle_dir, literals=literals, submission=submission)

    root, b = tmp_path / "submissions", NoFinal()
    bdir = Path(submit.prepare(run_dir, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])["bundle_dir"])
    render = bdir.parent / "render-1"
    render.mkdir()
    got = submit.add_render(root, "s1", b, bundle_dir=bdir, render_dir=render, literals=[], now=9.0)
    assert got["bundle_sha256"] == "ef" * 32 and got["final"] is False and got["render"]["added"] == 9.0
    assert "couldn't be validated: no release file" in got["render"]["error"]


def test_videos_todo_lists_each_rendered_run_without_a_link_and_add_videos_links_many(run_dir, tmp_path):
    root, b = tmp_path / "submissions", FakeBundler()
    assert submit.videos_todo(root) == []
    view, render = rendered(run_dir, root, b, mp4="mp4mp4")  # s1: rendered; its mp4 is 6 bytes
    env = Path(view["bundle_dir"]) / "bundle.json"
    env.write_text(json.dumps({**json.loads(env.read_text()), "run_id": "s1-r001", "scenario": "act1_gold_max",
                               "model": {"slug": "deepseek-flash"}}))  # fmt: skip
    d2 = tmp_path / "sessions" / "s2"
    d2.mkdir()
    submit.prepare(d2, root, b, attempt_n=1, unsubmitted_valid=0, literals=[])  # s2: not rendered yet
    (todo,) = submit.videos_todo(root)
    assert todo["run"] == "s1" and todo["run_id"] == "s1-r001" and todo["bundle_sha256"] == "ef" * 32
    assert todo["path"] == str(render / video.VIDEO) and todo["bytes"] == 6
    assert "s1-r001" in todo["title"] and "s1-r001" in todo["description"]  # E01 wants the run id in the title
    ok = submit.add_videos(
        root, [{"run": "s1", "bundle_sha256": "ef" * 32, "youtube_url": f"https://youtu.be/{VID}"}], b, literals=[]
    )
    assert ok == [{"run": "s1", "ok": True, "video": {"youtube_id": VID}}]
    assert submit.videos_todo(root) == []  # it has its link now
    bad = submit.add_videos(
        root,
        [{"run": "s2", "bundle_sha256": SHA, "youtube_url": "x"}, {"run": "../x"}, {"bundle_sha256": SHA}, 3],
        b, literals=[],
    )  # fmt: skip
    assert [r["ok"] for r in bad] == [False] * 4
    assert "render the evidence video first" in bad[0]["error"] and "bad run" in bad[1]["error"]
    with pytest.raises(panel.PanelError, match="items must be a list"):
        submit.add_videos(root, [], b, literals=[])
