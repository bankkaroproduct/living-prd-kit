#!/usr/bin/env python3
"""validate.py — Living PRD bundle validator (standard v1.2).

Usage:
  bin/validate.py <bundle-dir>        validate a bundle (a folder made by new-prd.sh)
  bin/validate.py --kit [kit-dir]     kit self-check (placeholders allowed)

Exit 0 = pass (warnings allowed), 1 = errors.
Requires: pyyaml; jsonschema (optional but recommended — CI installs both).
"""
import json, re, sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed (pip install pyyaml)"); sys.exit(1)

ERRORS, WARNINGS = [], []
def err(m): ERRORS.append(m)
def warn(m): WARNINGS.append(m)

PLACEHOLDER = re.compile(r"<[^<>\n]{2,80}>")
PII_PATTERNS = [
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "possible Aadhaar number"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "possible PAN"),
    (re.compile(r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"\s]{8,}"), "hardcoded secret"),
]
SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "__pycache__"}
TEXT_EXT = {".md", ".yaml", ".yml", ".json", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".txt", ".csv", ".sh", ".py", ".env"}

def load_manifest(path: Path):
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        err(f"prd.manifest.yaml is not valid YAML: {e}"); return None

def schema_check(manifest, kit_dir: Path):
    schema_path = kit_dir / "schema" / "prd.manifest.schema.json"
    if not schema_path.exists():
        warn(f"schema not found at {schema_path} — skipping schema validation"); return
    try:
        import jsonschema
    except ImportError:
        warn("jsonschema not installed — skipping schema validation (pip install jsonschema)"); return
    schema = json.loads(schema_path.read_text())
    for e in jsonschema.Draft202012Validator(schema).iter_errors(manifest):
        err(f"schema: at {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}")

def placeholder_check(manifest):
    found = []
    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items(): walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node): walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and PLACEHOLDER.search(node):
            found.append(path)
    walk(manifest, "manifest")
    for p in found: err(f"unfilled placeholder at {p}")

def artifact_check(manifest, bundle: Path):
    for name, a in (manifest.get("artifacts") or {}).items():
        target = a.get("file") or a.get("dir")
        if a.get("status") != "missing" and target and not (bundle / target).exists():
            err(f"artifacts.{name} is '{a.get('status')}' but {target} does not exist")

def xref_check(manifest, bundle: Path):
    spec = (bundle / "SPEC.md").read_text() if (bundle / "SPEC.md").exists() else ""
    tracking = (bundle / "TRACKING.md").read_text() if (bundle / "TRACKING.md").exists() else ""
    mocks = (bundle / "MOCKS.md").read_text() if (bundle / "MOCKS.md").exists() else ""
    mock_ids = set(re.findall(r"\bM\d+\b", mocks))
    for s in (manifest.get("coverage_plan") or {}).get("scenarios") or []:
        sid = s.get("id", "?")
        if s.get("status") != "pending" and spec and sid not in spec:
            warn(f"scenario {sid} not referenced anywhere in SPEC.md")
        links = s.get("links") or {}
        for ev in links.get("events") or []:
            if tracking and ev not in tracking:
                err(f"scenario {sid} links event '{ev}' not found in TRACKING.md")
        for m in links.get("mocks") or []:
            if m not in mock_ids:
                err(f"scenario {sid} links mock '{m}' not found in MOCKS.md")
        evd = links.get("evidence")
        if evd and evd not in ("pending",) and not (bundle / evd).exists():
            err(f"scenario {sid} evidence file missing: {evd}")

def gate_consistency(manifest):
    status = (manifest.get("prd") or {}).get("status")
    rel, cold = manifest.get("release") or {}, manifest.get("cold_session") or {}
    scenarios = (manifest.get("coverage_plan") or {}).get("scenarios") or []
    if status in ("frozen", "in-build", "reconciled", "shipped", "archived"):
        if (manifest.get("gates") or {}).get("g4_freeze", {}).get("passed") in ("pending", "", None):
            err(f"status is '{status}' but gates.g4_freeze.passed is pending")
        if cold.get("open_defects") != 0:
            err(f"status is '{status}' but cold_session.open_defects is {cold.get('open_defects')} (must be 0)")
        if rel.get("frozen") in ("pending", "", None):
            err(f"status is '{status}' but release.frozen is pending")
        for k, a in (rel.get("approvals") or {}).items():
            if not a.get("by"): err(f"status is '{status}' but release.approvals.{k}.by is empty")
        for s in scenarios:
            if s.get("status") == "pending":
                err(f"status is '{status}' but scenario {s.get('id')} is still pending (must be demonstrable or not-demonstrable)")
        # artifact completeness at freeze — T1-lite may leave mocks/contracts missing
        tier = (manifest.get("prd") or {}).get("tier")
        arts = manifest.get("artifacts") or {}
        for name in ("spec", "tracking", "decisions", "handoff", "evidence"):
            if arts.get(name, {}).get("status") != "complete":
                err(f"status is '{status}' but artifacts.{name} is not complete")
        if tier != "T1":
            for name in ("mocks", "contracts"):
                if arts.get(name, {}).get("status") == "missing":
                    err(f"status is '{status}' and tier {tier}: artifacts.{name} cannot be missing (T1-lite is T1 only)")
    if (manifest.get("data") or {}).get("provenance") == "staging-dump" and (manifest.get("data") or {}).get("pii_scrubbed") is not True:
        err("data.provenance is staging-dump but pii_scrubbed is not attested true (gated: scrub pipeline required)")

def pii_scan(root: Path):
    for p in root.rglob("*"):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts): continue
        if p.suffix.lower() not in TEXT_EXT: continue
        try: text = p.read_text(errors="ignore")
        except OSError: continue
        for rx, label in PII_PATTERNS:
            m = rx.search(text)
            if m: warn(f"{label} in {p.relative_to(root)} (match: {m.group(0)[:24]}…) — verify and remove if real")

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(1)
    kit_mode = args[0] == "--kit"
    script_kit = Path(__file__).resolve().parent.parent
    target = Path(args[1]) if kit_mode and len(args) > 1 else (script_kit if kit_mode else Path(args[0]))
    manifest_path = target / "prd.manifest.yaml"
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found"); sys.exit(1)
    manifest = load_manifest(manifest_path)
    if manifest is not None:
        schema_check(manifest, script_kit)
        if not kit_mode:
            placeholder_check(manifest)
            artifact_check(manifest, target)
            xref_check(manifest, target)
            gate_consistency(manifest)
    pii_scan(target)
    print(f"== validate: {target} ({'kit self-check' if kit_mode else 'bundle'}) ==")
    for w in WARNINGS: print(f"  WARN  {w}")
    for e in ERRORS:   print(f"  ERROR {e}")
    print(f"== {len(ERRORS)} error(s), {len(WARNINGS)} warning(s) ==")
    sys.exit(1 if ERRORS else 0)

if __name__ == "__main__":
    main()
