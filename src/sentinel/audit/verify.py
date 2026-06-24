"""False-positive control for audit findings.

Two cheap, high-leverage steps applied after structuring:

1. `dedup_findings` — merge findings that point at the same defect (same category +
   location + near-identical title), so the user never sees the same bug twice.

2. `verify_findings` — adversarial re-check. A deterministic pre-pass drops findings
   whose cited file doesn't exist (clear hallucinations), then ONE skeptical Claude Code
   pass re-reads each remaining finding's cited code and rules it real or refuted. Each
   finding ends up with `verified` set and `status` = verified | false_positive, plus the
   verifier's reason in `evidence.tool_output`.

The addon source is required to verify against code; with no source we leave findings
as-is (status stays "new", verified False) and report them as unverified.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sentinel.audit.runner import _norm_severity, _source_dir, parse_json_object
from sentinel.core.models import Finding
from sentinel.engine import ClaudeCodeEngine

_WORD = re.compile(r"[a-z0-9]+")


def _title_key(title: str) -> str:
    return " ".join(_WORD.findall((title or "").lower()))[:60]


def _dedup_key(f: Finding) -> str:
    raw = f"{f.category}|{(f.location.file or '').lower()}|{f.location.line_start}|{_title_key(f.title)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Merge duplicates by (category, location, normalised title). Keeps the strongest
    (highest confidence) instance and stamps a stable `dedup_key` on each survivor."""
    from sentinel.core.models import SEVERITY_ORDER

    best: dict[str, Finding] = {}
    for f in findings:
        key = _dedup_key(f)
        f.dedup_key = key
        cur = best.get(key)
        if cur is None:
            best[key] = f
            continue
        # keep the more severe / more confident one
        better = (SEVERITY_ORDER.get(f.severity, 9), -f.confidence) < (
            SEVERITY_ORDER.get(cur.severity, 9), -cur.confidence
        )
        if better:
            best[key] = f
    # preserve original order of survivors
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        k = f.dedup_key
        if k in seen:
            continue
        seen.add(k)
        out.append(best[k])
    return out


# --- deterministic location pre-check ----------------------------------------


def _resolve_cited_file(root: Path, cited: str) -> bool:
    """True if the finding's cited file plausibly exists under the addon root."""
    cited = cited.replace("\\", "/").strip()
    if (root / cited).exists():
        return True
    base = Path(cited).name
    return any(True for _ in root.rglob(base)) if base else False


# --- adversarial LLM verification --------------------------------------------

_VERIFY_SYSTEM = (
    "You are a SKEPTICAL senior Odoo 18 reviewer doing false-positive control on QA findings. "
    "The addon source is in the granted directory — Read/Grep/Glob it. For EACH finding, locate "
    "the cited file/method and decide if the defect is REAL by reading the ACTUAL code. Try to "
    "REFUTE it: a finding is real ONLY if the code genuinely exhibits the problem. Mark real=false "
    "if the file/line/method does not exist, the code already guards/handles the case, or the claim "
    "misreads the code. Do not be charitable; when unsure, lean real=false.\n"
    'Output ONLY JSON: {"verdicts":[{"i":int,"real":bool,"reason":str,"severity":str|null,'
    '"confidence":number}]}  where confidence (0..1) is how sure you are the finding is a '
    "REAL defect (so LOW for refuted ones). reason <= 200 chars."
)


def _conf_opt(v) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _verify_user(findings: list[Finding]) -> str:
    lines = ["Verify each finding against the addon source. Read the cited location before deciding.\n"]
    for i, f in enumerate(findings):
        loc = f.location.short()
        snippet = (f.evidence.code_snippet or "").strip().replace("\n", " ")[:200]
        lines.append(
            f"[{i}] {f.title}\n  location: {loc}  ({f.category}/{f.severity})\n"
            f"  claim: {f.description[:200]}\n  evidence: {snippet}"
        )
    return "\n".join(lines)


def verify_findings(
    engine: ClaudeCodeEngine, findings: list[Finding], *, addons: str | None,
    timeout: int = 600,
) -> tuple[list[Finding], dict]:
    """Return (findings, stats). stats = {verified, refuted, unverified}."""
    stats = {"verified": 0, "refuted": 0, "unverified": 0, "cost": 0.0}
    if not findings:
        return findings, stats

    src = _source_dir(addons)
    if not src or not engine.available():
        stats["unverified"] = len(findings)  # nothing to verify against
        return findings, stats

    root = Path(src)
    to_check: list[Finding] = []
    for f in findings:
        if f.location.file and not _resolve_cited_file(root, f.location.file):
            f.verified = False
            f.status = "false_positive"
            f.confidence = min(f.confidence, 0.15)
            f.evidence.tool_output = "refuted: cited file not found in the addon source"
            stats["refuted"] += 1
        else:
            to_check.append(f)

    if not to_check:
        return findings, stats

    try:
        res = engine.run_sync(
            _verify_user(to_check), code_dir=src, system_prompt=_VERIFY_SYSTEM,
            max_turns=60, timeout=timeout,
        )
        stats["cost"] = res.cost_usd or 0.0
        verdicts = {int(v["i"]): v for v in parse_json_object(res.text).get("verdicts", [])
                    if isinstance(v, dict) and "i" in v}
    except Exception:  # noqa: BLE001 — verification is best-effort; leave findings unverified
        stats["unverified"] += len(to_check)
        return findings, stats

    for i, f in enumerate(to_check):
        v = verdicts.get(i)
        if v is None:
            stats["unverified"] += 1
            continue
        reason = str(v.get("reason") or "").strip()[:240]
        conf = _conf_opt(v.get("confidence"))
        if v.get("real"):
            f.verified = True
            f.status = "verified"
            f.confidence = conf if conf is not None else max(f.confidence, 0.85)
            if v.get("severity"):
                f.severity = _norm_severity(str(v.get("severity")))
            f.evidence.tool_output = f"verified: {reason}" if reason else "verified"
            stats["verified"] += 1
        else:
            f.verified = False
            f.status = "false_positive"
            f.confidence = conf if conf is not None else 0.15
            f.evidence.tool_output = f"refuted: {reason}" if reason else "refuted"
            stats["refuted"] += 1
    return findings, stats
