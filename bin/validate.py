#!/usr/bin/env python3
"""validate.py — Living PRD bundle validator (standard v1.2).

Usage:
  bin/validate.py <bundle-dir>        validate a bundle (a folder made by new-prd.sh)
  bin/validate.py --kit [kit-dir]     kit self-check (placeholders allowed)

Exit 0 = pass (warnings allowed), 1 = errors.
Requires: pyyaml, jsonschema — both are hard requirements for a bundle check.
If either is missing this now FAILS CLOSED (exit 1), it does not warn-and-pass.
"""
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _release

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
# Regression-test fixtures are deliberately-planted bad content (that's the
# point of tests/fixtures/broken-bundle's fake PAN). Kit self-check skips
# this path *relative to the kit root it's scanning*; a direct run of
# `validate.py tests/fixtures/broken-bundle` is not kit_mode, so the
# exclusion does not apply there and the PII check still gets exercised —
# see tests/run_tests.sh, which relies on exactly that.
KIT_SELF_CHECK_SKIP_PREFIX = Path("tests/fixtures")
# .sql/.dump/.dmp included on purpose: staging-dump exports are exactly the
# highest-risk PII format and were previously never opened at all.
TEXT_EXT = {".md", ".yaml", ".yml", ".json", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css",
            ".txt", ".csv", ".sh", ".py", ".env", ".sql", ".dump", ".dmp"}
ADVANCED_STATUS = ("frozen", "in-build", "reconciled", "shipped", "archived")
ALL_GATES = ["g0_frame", "g1_solution", "g2_alpha_review", "g3_bundle_complete", "g4_freeze", "g5_productionise", "g6_reconcile"]

def load_manifest(path: Path):
    try:
        raw = path.read_text()
    except OSError as e:
        err(f"cannot read {path}: {e}"); return None
    try:
        m = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        err(f"prd.manifest.yaml is not valid YAML: {e}"); return None
    if m is None or not isinstance(m, dict):
        err("prd.manifest.yaml parsed to empty/non-object content (e.g. a bare 'null') — this is not a valid manifest")
        return None
    return m

def schema_check(manifest, kit_dir: Path):
    schema_path = kit_dir / "schema" / "prd.manifest.schema.json"
    if not schema_path.exists():
        err(f"schema not found at {schema_path} — cannot validate without it"); return
    try:
        import jsonschema
    except ImportError:
        err("jsonschema not installed — cannot validate without it (pip install jsonschema). "
            "This used to warn-and-pass; it now fails closed on purpose."); return
    schema = json.loads(schema_path.read_text())
    V = getattr(jsonschema, "Draft202012Validator", None) or getattr(jsonschema, "Draft7Validator", None)
    if V is None:
        err("jsonschema installed but has neither Draft202012Validator nor Draft7Validator — "
            "too old to validate this schema (pip install -U jsonschema)"); return
    try:
        found_any = False
        for e in V(schema).iter_errors(manifest):
            found_any = True
            err(f"schema: at {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}")
        if not found_any:
            pass  # clean pass
    except Exception as ex:
        err(f"schema validation could not run on this jsonschema version ({type(ex).__name__}) — pip install -U jsonschema")

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
        # a dir claimed 'complete' with nothing in it but its own scaffolded README
        # is still an empty promise — catch the emptiest case without being precious
        # about exactly what "enough" evidence looks like.
        if a.get("status") == "complete" and a.get("dir") and (bundle / a["dir"]).is_dir():
            real_files = [f for f in (bundle / a["dir"]).rglob("*") if f.is_file() and f.name.lower() != "readme.md"]
            if not real_files:
                err(f"artifacts.{name} is 'complete' but {a['dir']} has no files besides its scaffolded README")

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
        if s.get("status") == "demonstrable" and not (links.get("spec") or "").strip():
            err(f"scenario {sid} is 'demonstrable' but links.spec is empty — no traceability to SPEC.md")
        for ev in links.get("events") or []:
            if tracking and ev not in tracking:
                err(f"scenario {sid} links event '{ev}' not found in TRACKING.md")
        for m in links.get("mocks") or []:
            if m not in mock_ids:
                err(f"scenario {sid} links mock '{m}' not found in MOCKS.md")
        evd = links.get("evidence")
        if evd and evd not in ("pending",) and not (bundle / evd).exists():
            err(f"scenario {sid} evidence file missing: {evd}")

def gate_consistency(manifest, bundle: Path):
    status = (manifest.get("prd") or {}).get("status")
    rel, cold = manifest.get("release") or {}, manifest.get("cold_session") or {}
    scenarios = (manifest.get("coverage_plan") or {}).get("scenarios") or []
    gates = manifest.get("gates") or {}
    if status in ADVANCED_STATUS:
        # every gate up to and including g4 must actually be marked passed —
        # previously only g4_freeze was checked, so g1/g2/g3/g5/g6 could sit
        # 'pending' on a 'shipped' bundle and nothing caught it.
        for gk in ALL_GATES:
            passed = (gates.get(gk) or {}).get("passed")
            if passed in ("pending", "", None):
                err(f"status is '{status}' but gates.{gk}.passed is pending")
        if cold.get("open_defects") != 0:
            err(f"status is '{status}' but cold_session.open_defects is {cold.get('open_defects')} (must be 0)")
        if not (cold.get("transcript") or "").strip() or not (bundle / cold.get("transcript", "")).exists():
            err(f"status is '{status}' but cold_session.transcript ('{cold.get('transcript')}') does not exist on disk")
        if rel.get("frozen") in ("pending", "", None):
            err(f"status is '{status}' but release.frozen is pending")
        for k in ("pm_intent_behaviour", "tech_feasibility_delta", "qa_coverage_testability"):
            a = (rel.get("approvals") or {}).get(k) or {}
            if not a.get("by"):
                err(f"status is '{status}' but release.approvals.{k}.by is empty — all three signatures required before freeze")
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
        # release identity must be real, not just shaped like it — recompute and
        # verify commit/tag references instead of trusting the manifest's word.
        release_id = rel.get("release_id", "")
        digest = rel.get("digest", "")
        if digest and digest != "pending":
            try:
                expected = "sha256:" + _release.compute_digest(bundle, manifest, release_id)
                if digest != expected:
                    err(f"release.digest does not match a fresh hash of the bundle's current content "
                        f"(manifest says {digest[:18]}…, recomputed {expected[:18]}…) — bundle changed since freeze, or digest was hand-edited")
            except Exception as ex:
                warn(f"could not recompute digest to verify it ({type(ex).__name__})")
        commits = rel.get("prototype_commits") or []
        if commits:
            if _release.in_git_repo(bundle):
                for sha in commits:
                    if isinstance(sha, str) and sha and not _release.commit_exists(bundle, sha):
                        err(f"release.prototype_commits contains '{sha}' which does not exist in this git repo")
            else:
                warn("release.prototype_commits is set but this bundle isn't inside a git repo — can't verify the SHAs are real")
        tag = rel.get("evidence_tag", "")
        if tag and tag not in ("pending",):
            if _release.in_git_repo(bundle):
                if not _release.tag_exists(bundle, tag):
                    err(f"release.evidence_tag '{tag}' does not exist as a git tag in this repo")
            else:
                warn(f"release.evidence_tag is '{tag}' but this bundle isn't inside a git repo — can't verify the tag exists")
    if (manifest.get("data") or {}).get("provenance") == "staging-dump" and (manifest.get("data") or {}).get("pii_scrubbed") is not True:
        err("data.provenance is staging-dump but pii_scrubbed is not attested true (gated: scrub pipeline required)")

def pii_scan(root: Path, kit_mode: bool = False):
    for p in root.rglob("*"):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts): continue
        if kit_mode and p.relative_to(root).parts[:len(KIT_SELF_CHECK_SKIP_PREFIX.parts)] == KIT_SELF_CHECK_SKIP_PREFIX.parts:
            continue
        if p.suffix.lower() not in TEXT_EXT: continue
        try: text = p.read_text(errors="ignore")
        except OSError: continue
        for rx, label in PII_PATTERNS:
            m = rx.search(text)
            if m:
                # blocking, not advisory: a scrubbing gap is exactly the failure
                # mode this scan exists to catch before it reaches a commit.
                err(f"{label} in {p.relative_to(root)} (match: {m.group(0)[:24]}…) — verify and remove if real, or scrub it")

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
            gate_consistency(manifest, target)
    pii_scan(target, kit_mode=kit_mode)
    print(f"== validate: {target} ({'kit self-check' if kit_mode else 'bundle'}) ==")
    for w in WARNINGS: print(f"  WARN  {w}")
    for e in ERRORS:   print(f"  ERROR {e}")
    print(f"== {len(ERRORS)} error(s), {len(WARNINGS)} warning(s) ==")
    sys.exit(1 if ERRORS else 0)

if __name__ == "__main__":
    main()
