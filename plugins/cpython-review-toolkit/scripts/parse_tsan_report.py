#!/usr/bin/env python3
"""Parse ThreadSanitizer (TSan) reports over CPython's own source into findings.

Parses TSan text output, groups races by their source-location pair,
deduplicates, and separates **CPython-source races** (the target — report them)
from **framework / scaffolding noise** (filter them out).

Key inversion vs. ft-review-toolkit's analyzer
-----------------------------------------------
The ft toolkit reviews *extensions*, so its analyzer treats a race whose frames
live in CPython internals (``Objects/`` / ``Python/`` / ``Modules/``) as
not-the-extension's-problem and filters it out. For cpython-review-toolkit the
target **is** CPython, so that logic **inverts**: a race in CPython's own
runtime source *is* the finding. Only pure thread-scaffolding / test-harness
frames (thread bootstrap, ``pthread_create``, the ``_testcapi`` family) and
genuinely third-party library frames (libc, libssl, the sanitizer runtime) are
noise.

Confirmed races get recorded in the local findings repo ``cpython-tsan-findings``;
a race's signature there is the *unordered pair* of racing ``file:func`` sites,
which is exactly the ``signature`` field each finding carries here.

Unlike the tree-sitter scanners in this toolkit, this uses regex (not
Tree-sitter) because TSan output is plain text, not C source — so it needs
neither the tree-sitter chassis nor CPython-root detection. Stdlib only.

Usage:
    python parse_tsan_report.py /path/to/tsan_report.txt
"""

import json
import os
import re
import sys
from pathlib import Path

# Patterns for parsing TSan output.
_WARNING_RE = re.compile(r"WARNING: ThreadSanitizer: (.+?)(?:\s+\(pid=\d+\))?\s*$")
_ACCESS_RE = re.compile(
    r"^\s+((?:Previous )?(?:[Ww]rite|[Rr]ead)) of size (\d+) "
    r"at (0x[0-9a-f]+) by (.*?):\s*$"
)
_FRAME_RE = re.compile(r"^\s+#(\d+)\s+(\S+)\s+(.+?)(?:\s+\((.+?)\))?\s*$")
_LOCATION_RE = re.compile(
    # Not end-anchored: heap-location lines carry a trailing
    # "allocated by thread T1:" that global-location lines do not.
    r"^\s+Location is (.+?) of size (\d+) at (0x[0-9a-f]+)(?:\s+\((.+?)\))?"
)
_THREAD_CREATE_RE = re.compile(
    r"^\s+Thread T(\d+)\s+'?(.*?)'?\s+(?:\(tid=\d+.*?\)\s+)?created by (.*?) at:\s*$"
)
_SUMMARY_RE = re.compile(
    r"^SUMMARY: ThreadSanitizer: (.+?)\s+(\S+?)(?::(\d+))?(?::(\d+))?"
    r"\s+in\s+(.+?)\s*$"
)
_SEPARATOR_RE = re.compile(r"^={10,}$")

# Frames that are pure scaffolding / test-harness / third-party — NOT the
# target. Checked *before* the CPython-source patterns so that, e.g., the thread
# bootstrap living in Modules/_threadmodule.c is classified as noise rather than
# as a CPython-source race.
_NOISE_PATTERNS = [
    # CPython's built-in test-harness modules (present in the tree, not target).
    r"_testcapi",
    r"_testinternalcapi",
    r"_testlimitedcapi",
    r"_testbuffer",
    r"_testmultiphase",
    r"_testsinglephase",
    r"_testimportmultiple",
    r"_testclinic",
    r"_testexternalinspection",
    r"_xxtestfuzz",
    r"_ctypes_test",
    # Python-level test tree.
    r"/Lib/test/",
    # Thread scaffolding / interpreter bootstrap plumbing.
    r"\bt_bootstrap\b",
    r"\bthread_run\b",
    r"\bpythread_wrapper\b",
    r"\bbootstrap_thread\b",
    r"\bstart_new_thread\b",
    r"\bdo_start_new_thread\b",
    r"\bstart_thread\b",
    r"\bpthread_\w+",
    # Sanitizer runtime.
    r"__tsan",
    r"__sanitizer",
    r"libtsan",
    r"libclang_rt",
    # System / third-party libraries.
    r"libc\.so",
    r"libpthread",
    r"libm\.so",
    r"ld-linux",
    r"/usr/lib/",
    r"/usr/include/",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))

# Frames that live in CPython's own runtime source — the target.
_CPYTHON_SOURCE_PATTERNS = [
    r"/Objects/",
    r"/Python/",
    r"/Modules/",
    r"/Include/",
    r"/Parser/",
    r"pycore_",
    r"pyconfig",
]
_CPYTHON_SOURCE_RE = re.compile("|".join(_CPYTHON_SOURCE_PATTERNS))


def _parse_stack_frame(line: str) -> dict | None:
    """Parse a single stack-frame line.

    Returns dict with: frame_num, function, location, module, file, line, col.
    """
    m = _FRAME_RE.match(line)
    if not m:
        return None

    frame_num = int(m.group(1))
    function = m.group(2)
    location = m.group(3).strip()
    module = m.group(4) or ""

    # Parse file:line:col out of the location component.
    file_path = None
    line_num = None
    col_num = None
    loc_match = re.match(r"(.+?):(\d+):(\d+)", location)
    if loc_match:
        file_path = loc_match.group(1)
        line_num = int(loc_match.group(2))
        col_num = int(loc_match.group(3))
    else:
        loc_match = re.match(r"(.+?):(\d+)\s*$", location)
        if loc_match:
            file_path = loc_match.group(1)
            line_num = int(loc_match.group(2))
        elif location and location != "<null>":
            file_path = location

    return {
        "frame_num": frame_num,
        "function": function,
        "location": location,
        "module": module,
        "file": file_path,
        "line": line_num,
        "col": col_num,
    }


def _classify_frame(frame: dict) -> str:
    """Classify a frame as ``cpython`` (target), ``noise``, or ``unknown``.

    Noise is checked first so thread-bootstrap / test-harness frames that
    happen to live under ``Modules/`` are not mistaken for target code.
    """
    combined = " ".join(
        str(frame.get(k, "") or "") for k in ("location", "module", "function")
    )
    if _NOISE_RE.search(combined):
        return "noise"
    if _CPYTHON_SOURCE_RE.search(combined):
        return "cpython"
    return "unknown"


def _get_cpython_frame(frames: list[dict]) -> dict | None:
    """Return the first CPython-source frame in a stack (skipping scaffolding)."""
    for frame in frames:
        if _classify_frame(frame) == "cpython":
            return frame
    return None


def _frame_site(frame: dict) -> str:
    """Render a ``file:func`` site string for a frame (basename for stability)."""
    file_path = frame.get("file")
    base = os.path.basename(file_path) if file_path else (frame.get("module") or "?")
    func = frame.get("function") or "?"
    return f"{base}:{func}"


def _parse_tsan_block(lines: list[str]) -> dict | None:
    """Parse a single TSan warning block into a structured finding."""
    if not lines:
        return None

    race_type = None
    for line in lines:
        m = _WARNING_RE.match(line)
        if m:
            race_type = m.group(1)
            break
    if not race_type:
        return None

    accesses: list[dict] = []
    current_access: dict | None = None
    thread_info: list[dict] = []
    location_info: dict | None = None

    for line in lines:
        # Access header (Write/Read of size N).
        access_m = _ACCESS_RE.match(line)
        if access_m:
            if current_access:
                accesses.append(current_access)
            current_access = {
                "access_type": access_m.group(1).strip(),
                "size": int(access_m.group(2)),
                "address": access_m.group(3),
                "thread": access_m.group(4).strip(),
                "frames": [],
            }
            continue

        # Location info — ends the current access; its frames are not part of it.
        loc_m = _LOCATION_RE.match(line)
        if loc_m:
            location_info = {
                "description": loc_m.group(1),
                "size": int(loc_m.group(2)),
                "address": loc_m.group(3),
                "module": loc_m.group(4) or "",
            }
            if current_access:
                accesses.append(current_access)
                current_access = None
            continue

        # Thread-creation info — also ends the current access.
        thread_m = _THREAD_CREATE_RE.match(line)
        if thread_m:
            if current_access:
                accesses.append(current_access)
                current_access = None
            thread_info.append(
                {
                    "thread_id": int(thread_m.group(1)),
                    "thread_name": thread_m.group(2),
                    "creator": thread_m.group(3),
                }
            )
            continue

        # Stack frame — only attach while we are inside an access.
        frame = _parse_stack_frame(line)
        if frame and current_access:
            current_access["frames"].append(frame)
            continue

    if current_access:
        accesses.append(current_access)

    # Summary line.
    summary = None
    for line in lines:
        sum_m = _SUMMARY_RE.match(line)
        if sum_m:
            summary = {
                "type": sum_m.group(1),
                "file": sum_m.group(2),
                "line": int(sum_m.group(3)) if sum_m.group(3) else None,
                "col": int(sum_m.group(4)) if sum_m.group(4) else None,
                "function": sum_m.group(5),
            }
            break

    if not accesses:
        return None

    # The racing sites: the first CPython-source frame of each access (or the
    # top frame if none is CPython source).
    sites: list[dict] = []
    cpython_frames: list[dict] = []
    for access in accesses:
        cf = _get_cpython_frame(access["frames"])
        if cf:
            cpython_frames.append(cf)
        chosen = cf or (access["frames"][0] if access["frames"] else None)
        if chosen:
            sites.append(
                {
                    "file": os.path.basename(chosen["file"])
                    if chosen.get("file")
                    else None,
                    "func": chosen.get("function"),
                    "line": chosen.get("line"),
                    "site": _frame_site(chosen),
                    "is_cpython": _classify_frame(chosen) == "cpython",
                }
            )

    is_cpython_race = len(cpython_frames) > 0
    is_noise = not is_cpython_race

    return {
        "race_type": race_type,
        "accesses": accesses,
        "location": location_info,
        "thread_info": thread_info,
        "summary": summary,
        "is_cpython_race": is_cpython_race,
        "is_noise": is_noise,
        "sites": sites,
        "cpython_frames": cpython_frames,
    }


def _split_tsan_blocks(text: str) -> list[list[str]]:
    """Split TSan output into individual warning blocks by separator lines."""
    blocks: list[list[str]] = []
    current_block: list[str] = []
    in_block = False

    for line in text.splitlines():
        if _SEPARATOR_RE.match(line):
            if in_block and current_block:
                blocks.append(current_block)
                current_block = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current_block.append(line)

    if current_block:
        blocks.append(current_block)

    return blocks


def _race_signature(finding: dict) -> str:
    """Unordered pair of racing ``file:func`` sites — the findings-repo signature."""
    parts = [s["site"] for s in finding.get("sites", []) if s.get("site")]
    if not parts:
        # Fall back to raw top frames so distinct races don't all collapse.
        for access in finding.get("accesses", []):
            if access["frames"]:
                parts.append(_frame_site(access["frames"][0]))
    parts.sort()
    return " | ".join(parts)


def _deduplicate_races(findings: list[dict]) -> list[dict]:
    """Deduplicate findings by their racing-site-pair signature."""
    seen: dict[str, int] = {}
    deduped: list[dict] = []

    for finding in findings:
        key = _race_signature(finding)
        finding["signature"] = key
        if key in seen:
            idx = seen[key]
            deduped[idx]["frequency"] = deduped[idx].get("frequency", 1) + 1
        else:
            seen[key] = len(deduped)
            finding["frequency"] = 1
            deduped.append(finding)

    return deduped


def _classify_severity(finding: dict) -> tuple[str, str]:
    """Classify a finding's classification and severity."""
    race_type = finding.get("race_type", "")
    location = finding.get("location", {}) or {}
    loc_desc = location.get("description", "") or ""

    # A race on a global / static variable is the most dangerous shape.
    if "global" in loc_desc:
        return "RACE", "CRITICAL"

    access_types = [
        a.get("access_type", "").lower() for a in finding.get("accesses", [])
    ]
    has_write = any("write" in t for t in access_types)

    if has_write and len(access_types) >= 2:
        return "RACE", "HIGH"
    if "data race" in race_type.lower():
        return "RACE", "HIGH"
    return "RACE", "MEDIUM"


def parse_report(text: str) -> dict:
    """Parse raw TSan report text into the structured findings envelope.

    Pure function of the report text — no filesystem access — so it can be
    driven directly from an inline fixture.
    """
    blocks = _split_tsan_blocks(text)
    raw_findings: list[dict] = []
    for block in blocks:
        finding = _parse_tsan_block(block)
        if finding:
            raw_findings.append(finding)

    deduped = _deduplicate_races(raw_findings)

    findings: list[dict] = []
    for finding in deduped:
        classification, severity = _classify_severity(finding)
        finding["classification"] = classification
        finding["severity"] = severity
        findings.append(finding)

    cpython_races = [f for f in findings if f["is_cpython_race"]]
    noise_races = [f for f in findings if f["is_noise"]]

    return {
        "total_warnings": len(raw_findings),
        "unique_races": len(deduped),
        "cpython_races": len(cpython_races),
        "noise_races": len(noise_races),
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_classification": {
                "RACE": len([f for f in findings if f["classification"] == "RACE"]),
            },
            "by_severity": {
                s: len([f for f in findings if f["severity"] == s])
                for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                if any(f["severity"] == s for f in findings)
            },
            "actionable": len(cpython_races),
            "noise": len(noise_races),
        },
        "findings_repo": "cpython-tsan-findings",
    }


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Parse a TSan report *file* into the structured findings envelope.

    Args:
        target: Path to the TSan report text file.
        max_files: Unused — kept for calling-convention compatibility.
    """
    report_path = Path(target).resolve()

    if not report_path.exists():
        return {
            "error": f"TSan report not found: {report_path}",
            "report_path": str(report_path),
        }

    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {
            "error": f"Failed to read report: {e}",
            "report_path": str(report_path),
        }

    report = parse_report(text)
    report = {"report_path": str(report_path), **report}
    return report


def main() -> None:
    if len(sys.argv) < 2:
        json.dump(
            {"error": "Usage: parse_tsan_report.py <tsan_report_file>"}, sys.stdout
        )
        sys.stdout.write("\n")
        sys.exit(2)

    target = sys.argv[1]
    try:
        result = analyze(target)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if "error" in result:
            sys.exit(1)
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
