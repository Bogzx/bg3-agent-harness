"""The API contract: every GET payload of the recorded fixture run matches the web client's types (contract.py)."""

from pathlib import Path

import pytest

from ..conftest import PT
from .conftest import Site
from .contract import payloads, type_errors

pytestmark = pytest.mark.e2e


def test_the_fixture_runs_payloads_match_the_clients_types(site: Site, tmp_path: Path):
    found = payloads(site["url"], PT)
    assert {"watch", "replay/S096", "memory/file decisions.md", "bench"} <= set(found)
    assert any(k.startswith("memory/diff ") for k in found), list(found)
    errors = type_errors(found, tmp_path)
    assert errors == "", errors
