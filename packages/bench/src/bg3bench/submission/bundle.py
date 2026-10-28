"""The bundle envelope (schema/bundle.v1.schema.json): its canonical form, its hash and the size caps.

The bundle hash is the sha256 of bundle.json in canonical form: keys sorted, no whitespace, UTF-8 with non-ASCII kept.
It covers every other file through files[].sha256, names the stored bundle and goes on the evidence video's id line.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = 1
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "bundle.v1.schema.json"
ENVELOPE = "bundle.json"

MAX_TOTAL_BYTES = 20 * 1024 * 1024  # every file, bundle.json included
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FRAMES_BYTES = 3 * 1024 * 1024  # frames/*.jpg, the contact sheet
FRAMES_DIR = "frames/"


def canonical_bytes(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def bundle_hash(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())
