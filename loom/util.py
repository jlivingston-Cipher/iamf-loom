"""Small shared helpers (hashing, canonical JSON).

One definition each; cache keys and spec-hashes depend on these staying
byte-for-byte stable (doc 49: the 0.6.0 spec-hash formula is pinned by test).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, unicode kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
