#!/usr/bin/env bash
# validate-catalog.sh — render every catalog preset through the chart.
#
# For each catalog/models/*.yaml preset:
#   1. wrap it under `serveModels:` and flip its `enabled: false` to true
#      (pure-text merge via python3 — presets are single-top-level-key
#      fragments with 2-space indentation, so no YAML library is needed);
#   2. run `helm template` with the merged values overlay, which also
#      validates against charts/open-serve/values.schema.json.
#
# Finishes with a combined render of ALL presets enabled at once to catch
# cross-preset collisions. Fails loudly on the first broken preset.
#
# Usage: scripts/validate-catalog.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART="$ROOT/charts/open-serve"
CATALOG="$ROOT/catalog/models"

command -v helm >/dev/null 2>&1 || { echo "ERROR: helm not found on PATH" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found on PATH" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Wrap a preset under serveModels: and enable it. Text-based: indents every
# line by two spaces and flips the first direct-child `enabled: false`.
merge_preset() {
  python3 - "$1" "$2" <<'PY'
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    lines = f.read().splitlines()

out = ["serveModels:"]
flipped = False
for line in lines:
    if not flipped and re.match(r"^  enabled:\s*false\s*(#.*)?$", line):
        line = "  enabled: true"
        flipped = True
    out.append("  " + line if line.strip() else line)

if not flipped:
    sys.exit(f"ERROR: {src}: no '  enabled: false' line found to flip")

with open(dst, "w") as f:
    f.write("\n".join(out) + "\n")
PY
}

shopt -s nullglob
presets=("$CATALOG"/*.yaml)
if [ "${#presets[@]}" -eq 0 ]; then
  echo "ERROR: no presets found in $CATALOG" >&2
  exit 1
fi

fail=0
for preset in "${presets[@]}"; do
  name="$(basename "$preset" .yaml)"
  merged="$TMP/$name-values.yaml"
  merge_preset "$preset" "$merged"
  if helm template test "$CHART" -f "$merged" > "$TMP/$name-render.yaml" 2> "$TMP/$name-err.txt"; then
    docs="$(grep -c '^kind:' "$TMP/$name-render.yaml" || true)"
    echo "OK   $name (rendered $docs manifests)"
  else
    echo "FAIL $name" >&2
    sed 's/^/     /' "$TMP/$name-err.txt" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "" >&2
  echo "Catalog validation FAILED — see errors above." >&2
  exit 1
fi

# Combined render: every preset enabled in one release. Catches duplicate
# model keys / manifest name collisions between presets.
combined_args=()
for preset in "${presets[@]}"; do
  name="$(basename "$preset" .yaml)"
  combined_args+=(-f "$TMP/$name-values.yaml")
done
if helm template test "$CHART" "${combined_args[@]}" > "$TMP/all-render.yaml" 2> "$TMP/all-err.txt"; then
  docs="$(grep -c '^kind:' "$TMP/all-render.yaml" || true)"
  echo "OK   combined (all ${#presets[@]} presets enabled, $docs manifests)"
else
  echo "FAIL combined render with all presets enabled" >&2
  sed 's/^/     /' "$TMP/all-err.txt" >&2
  exit 1
fi

echo "Catalog validation passed: ${#presets[@]} presets."
