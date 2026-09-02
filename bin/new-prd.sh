#!/usr/bin/env bash
# new-prd.sh — generate a complete Living PRD bundle from the kit.
# Usage: bin/new-prd.sh <feature-slug> [target-parent-dir]
# Creates <target-parent-dir>/prd-<feature-slug>/ with the manifest (slug filled in),
# all templates, checklists, patterns, contracts/ and EVIDENCE/ — the full structure.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <feature-slug> [target-parent-dir]" >&2
  exit 1
fi

SLUG="$1"
if ! printf '%s' "$SLUG" | grep -Eq '^[a-z0-9][a-z0-9-]*$'; then
  echo "Error: slug must be lowercase letters, digits, hyphens (got: $SLUG)" >&2
  exit 1
fi

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="${2:-.}"
BUNDLE="$PARENT/prd-$SLUG"

if [ -e "$BUNDLE" ]; then
  echo "Error: $BUNDLE already exists — refusing to overwrite" >&2
  exit 1
fi

mkdir -p "$BUNDLE/EVIDENCE/screens" "$BUNDLE/contracts" "$BUNDLE/checklists" "$BUNDLE/patterns"

# Manifest with the slug substituted
sed "s/<feature-slug>/$SLUG/" "$KIT_DIR/prd.manifest.yaml" > "$BUNDLE/prd.manifest.yaml"

# Artifact templates land at the bundle root (they ARE the bundle's documents)
for f in SPEC.md TRACKING.md MOCKS.md DECISIONS.md HANDOFF.md; do
  cp "$KIT_DIR/templates/$f" "$BUNDLE/$f"
done
cp "$KIT_DIR/templates/EVIDENCE.md" "$BUNDLE/EVIDENCE/README.md"

# Gate checklists and build patterns travel with the bundle
cp "$KIT_DIR"/checklists/G*.md "$BUNDLE/checklists/"
cp "$KIT_DIR"/patterns/*.md "$BUNDLE/patterns/"

# contracts/ starter
cat > "$BUNDLE/contracts/README.md" <<'EOF'
# contracts/ — pinned API truth

Put OpenAPI specs, JSON Schemas, and response fixtures here, with exact versions in the
filename (e.g. `crosssell-api.v3.2.openapi.yaml`). Pin each one in the manifest under
`release.api_contracts` at freeze.

Rule of authority: for API shapes and integration behaviour, these files outrank SPEC.md
and any PM-written assumption. For product behaviour (what the user sees and can do),
SPEC.md outranks everything. A conflict between the two is a defect — fix it the day
it is found and log it in the manifest changelog.
EOF

# Safety net: keep dumps and secrets out of git
cat > "$BUNDLE/.gitignore" <<'EOF'
.DS_Store
.env
.env.*
*.sql
*.dump
*.sqlite
*.db
node_modules/
EOF

echo "Created $BUNDLE"
find "$BUNDLE" -type f | sort | sed "s|^$BUNDLE/|  |"
echo ""
echo "Next: fill prd.manifest.yaml header + mandate (G0), then validate:"
echo "  $KIT_DIR/bin/validate.py $BUNDLE"
