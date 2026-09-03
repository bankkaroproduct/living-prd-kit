#!/usr/bin/env bash
# Regression tests for bin/validate.py and bin/freeze.py — proves the
# fail-closed behaviour the second external review found missing actually
# holds, instead of just asserting it in prose. Run from the kit root:
#   tests/run_tests.sh
set -u
cd "$(dirname "$0")/.."
FAIL=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAIL=1; }

echo "Building fixtures..."
python3 tests/build_fixtures.py > /tmp/_fixtures.log 2>&1 || { cat /tmp/_fixtures.log; fail "fixture build"; exit 1; }

echo "== kit self-check must be clean =="
python3 bin/validate.py --kit > /tmp/_kit.log 2>&1
[ $? -eq 0 ] && pass "kit self-check exits 0" || { fail "kit self-check exits 0"; cat /tmp/_kit.log; }

echo "== complete-bundle fixture must pass clean =="
python3 bin/validate.py tests/fixtures/complete-bundle > /tmp/_complete.log 2>&1
[ $? -eq 0 ] && pass "complete-bundle exits 0" || { fail "complete-bundle exits 0"; cat /tmp/_complete.log; }
grep -q "^== 0 error" /tmp/_complete.log && pass "complete-bundle has 0 errors" || fail "complete-bundle has 0 errors"

echo "== broken-bundle fixture must fail, catching every planted defect =="
python3 bin/validate.py tests/fixtures/broken-bundle > /tmp/_broken.log 2>&1
[ $? -ne 0 ] && pass "broken-bundle exits nonzero" || fail "broken-bundle exits nonzero"
for signal in \
  "links.spec is empty" \
  "has no files besides its scaffolded README" \
  "gates.g1_solution.passed is pending" \
  "cold_session.transcript.*does not exist on disk" \
  "release.approvals.tech_feasibility_delta.by is empty" \
  "release.digest does not match a fresh hash" \
  "possible PAN in notes.sql"
do
  grep -qE "$signal" /tmp/_broken.log && pass "caught: $signal" || fail "did NOT catch: $signal"
done

echo "== a bare-null manifest must fail, not silently pass =="
rm -rf /tmp/_null-bundle && mkdir -p /tmp/_null-bundle && echo "null" > /tmp/_null-bundle/prd.manifest.yaml
python3 bin/validate.py /tmp/_null-bundle > /tmp/_null.log 2>&1
[ $? -ne 0 ] && pass "null manifest exits nonzero" || fail "null manifest exits nonzero"

echo "== missing jsonschema must fail closed, not warn-and-pass =="
rm -rf /tmp/_fake_no_jsonschema && mkdir -p /tmp/_fake_no_jsonschema
printf 'raise ImportError("simulated: jsonschema not installed")\n' > /tmp/_fake_no_jsonschema/jsonschema.py
PYTHONPATH=/tmp/_fake_no_jsonschema python3 bin/validate.py tests/fixtures/complete-bundle > /tmp/_nojs.log 2>&1
[ $? -ne 0 ] && pass "missing-jsonschema exits nonzero" || fail "missing-jsonschema exits nonzero"
grep -q "fails closed on purpose" /tmp/_nojs.log && pass "missing-jsonschema gives the fail-closed message" || fail "missing-jsonschema gives the fail-closed message"

echo "== freeze.py must refuse an incomplete bundle =="
python3 bin/freeze.py tests/fixtures/broken-bundle > /tmp/_freeze_broken.log 2>&1
[ $? -ne 0 ] && pass "freeze refuses broken-bundle" || fail "freeze refuses broken-bundle"

echo "== freeze.py must auto-increment past an existing tag =="
python3 bin/freeze.py tests/fixtures/complete-bundle > /tmp/_freeze_complete.log 2>&1
[ $? -eq 0 ] && pass "freeze succeeds on complete-bundle" || { fail "freeze succeeds on complete-bundle"; cat /tmp/_freeze_complete.log; }
grep -q 'release_id: "prd-demo-feature-r2"' /tmp/_freeze_complete.log && pass "freeze skipped the already-tagged r1, used r2" || fail "freeze skipped the already-tagged r1, used r2"

echo "== bin/validate.py must be executable (git mode bit, not just chmod) =="
[ -x bin/validate.py ] && pass "bin/validate.py has the executable bit" || fail "bin/validate.py has the executable bit"

echo
if [ "$FAIL" -eq 0 ]; then
  echo "ALL TESTS PASSED"
else
  echo "SOME TESTS FAILED — see above"
fi
exit $FAIL
