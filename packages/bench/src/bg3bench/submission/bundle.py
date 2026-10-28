"""The bundle envelope (schema/bundle.v1.schema.json): its canonical form, its hash and the size caps.

bundle.json is written in canonical form (keys sorted, no whitespace, UTF-8 with non-ASCII kept) and the bundle hash
is the sha256 of its bytes as stored: a reader hashes the file, never a re-serialisation (JSON number formatting
differs between languages). It covers every other file through files[].sha256, names the stored bundle and goes on the
evidence video's id line.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = 1
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "bundle.v1.schema.json"
ENVELOPE = "bundle.json"

MAX_TOTAL_BYTES = 20 * 1024 * 1024  # every file, bundle.json included
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 32  # bundle.json not counted
# The contact sheet: one uncompressed tar member whose entries are only frames/<n>.jpg (regular files).
FRAMES_TAR = "frames.tar"
FRAMES_MEMBER = re.compile(r"frames/\d{1,5}\.jpg")
MAX_FRAMES_BYTES = 3 * 1024 * 1024  # frames.tar as a whole


def canonical_bytes(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def bundle_hash(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def is_canonical(data: bytes) -> bool:
    """Whether bundle.json's bytes are the canonical form of what they parse to."""
    try:
        return canonical_bytes(json.loads(data)) == data
    except (ValueError, UnicodeDecodeError):
        return False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())
