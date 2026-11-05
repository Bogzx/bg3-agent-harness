"""The API contract: every GET payload of the recorded fixture run matches the web client's types (contract.py)."""

import dataclasses
import json
from pathlib import Path

import pytest
from bg3play import config

from ..conftest import PT
from .conftest import S096_START, Site
from .contract import payloads, type_errors

pytestmark = pytest.mark.e2e


def guide_s096(site: Site) -> None:
    """The fixture's S096 as a guided session: the option on, a message delivered into one of its results, one still
    queued, and its supervisor.json row's counts (so watch, sessions, timeline and replay carry operator data)."""
    store = site["store"]
    pt_dir, sdir = store.playthroughs_dir / PT, store.session_dir(PT)
    config.save(pt_dir, dataclasses.replace(config.load(pt_dir), operator=config.Operator(messages=True)))
    sent = [
        {"id": "m1", "t": S096_START + 100, "text": "rest first"},
        {"id": "m2", "t": S096_START + 200, "text": "go"},
    ]
    (sdir / "operator.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sent))
    done = {"id": "m1", "t": S096_START + 130, "session": "S096", "tool": "look"}
    (sdir / "operator.delivered.jsonl").write_text(json.dumps(done) + "\n")
    row = {"session": "S096", "operator": {"enabled": True, "sent": 2, "delivered": 1}}
    (sdir / "supervisor.json").write_text(json.dumps({"v": 1, "history": [row]}))


def test_the_fixture_runs_payloads_match_the_clients_types(site: Site, tmp_path: Path):
    guide_s096(site)
    found = payloads(site["url"], PT)
    assert {"watch", "replay/S096", "memory/file decisions.md", "bench"} <= set(found)
    assert any(k.startswith("memory/diff ") for k in found), list(found)
    assert [m["status"] for m in found["watch"][1]["operator"]["messages"]] == ["delivered", "queued"]
    assert found["sessions"][1]["sessions"][-1]["operator"] == {"enabled": True, "sent": 2, "delivered": 1}
    assert found["timeline"][1]["sessions"][-1]["operator"] == {"enabled": True, "sent": 2, "delivered": 1}
    assert [e["status"] for e in found["replay/S096"][1]["events"] if e["kind"] == "operator"] == [
        "queued", "delivered", "queued"
    ]  # fmt: skip
    errors = type_errors(found, tmp_path)
    assert errors == "", errors
