#!/usr/bin/env python3
"""Shared utilities for cpython-review-toolkit scripts. Stdlib-only."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


# Matches /* ... */ block comments (possibly multi-line) and // line comments.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


_SAFETY_KEYWORDS = frozenset({
    "safety:", "safe because", "intentional", "by design", "nolint",
    "checked:", "correct because", "this is safe", "not a bug",
    "deliberately", "expected", "gil held", "gil-held", "already locked",
    "already protected", "refcount safe", "borrowed ok",
})


def extract_nearby_comments(source: str, line: int, radius: int = 5) -> list[str]:
    """Return C-style comments within +/- `radius` lines of 1-based `line`.

    Scans /* ... */ and // ... comments.  Each returned item is the comment
    text with the markers stripped.  Handles multi-line block comments.
    """
    lines = source.splitlines()
    if not lines:
        return []
    min_line = max(1, line - radius)
    max_line = min(len(lines), line + radius)
    # Compute byte offsets for each line start.
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln) + 1)  # +1 for \n

    def line_of(offset: int) -> int:
        # Binary search.
        lo, hi = 0, len(offsets) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if offsets[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1  # 1-based

    comments: list[str] = []
    for m in _BLOCK_COMMENT_RE.finditer(source):
        start_line = line_of(m.start())
        end_line = line_of(m.end() - 1)
        if start_line <= max_line and end_line >= min_line:
            text = m.group(0)[2:-2].strip()
            comments.append(text)
    for m in _LINE_COMMENT_RE.finditer(source):
        ln = line_of(m.start())
        if min_line <= ln <= max_line:
            comments.append(m.group(0)[2:].strip())
    return comments


def has_safety_annotation(comments: Iterable[str]) -> bool:
    """True if any comment contains a safety-annotation keyword."""
    for c in comments:
        lower = c.lower()
        if any(kw in lower for kw in _SAFETY_KEYWORDS):
            return True
    return False


def make_finding(
    finding_type: str,
    *,
    file: str = "",
    line: int = 0,
    function: str = "",
    classification: str,
    severity: str,
    confidence: str = "high",
    detail: str,
    **extra,
) -> dict:
    """Create a finding dict with a consistent shape."""
    finding: dict = {
        "type": finding_type,
        "file": file,
        "line": line,
        "function": function,
        "classification": classification,
        "severity": severity,
        "confidence": confidence,
        "detail": detail,
    }
    finding.update(extra)
    return finding


def load_json_data(path: Path) -> dict:
    """Load a JSON data file.  Emit stderr warning on failure; return {}.

    Unlike a bare `json.load`, this never silently returns empty: callers
    can still proceed (returning 0 findings) but the warning flags the
    degraded state to the user.
    """
    import json
    import sys
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: Failed to load {path}: {e}", file=sys.stderr)
        return {}
