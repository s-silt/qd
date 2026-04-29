#!/usr/bin/env bash
# Build the QD Cookies Helper extension into a deterministic .zip suitable
# for a GitHub Release asset.
#
# Usage:
#   bash scripts/build-extension.sh [output_dir]
#
# Default output_dir is ./dist
#
# The output filename is qd-get-cookies-v<manifest.version>.zip.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="$REPO_ROOT/web/extension/get-cookies"
OUT_DIR="${1:-$REPO_ROOT/dist}"

if [[ ! -f "$EXT_DIR/manifest.json" ]]; then
  echo "error: $EXT_DIR/manifest.json not found" >&2
  exit 1
fi

# Validate JSON files (manifest + locale)
python3 -c "
import json, sys
files = [
  '$EXT_DIR/manifest.json',
  '$EXT_DIR/_locales/zh_CN/messages.json',
  '$EXT_DIR/_locales/en/messages.json',
]
for f in files:
  try:
    json.load(open(f))
  except Exception as e:
    print(f'invalid JSON: {f}: {e}', file=sys.stderr); sys.exit(1)
print('JSON files OK')
"

# Pull the version from manifest.json (no jq dependency)
VERSION="$(python3 -c "import json; print(json.load(open('$EXT_DIR/manifest.json'))['version'])")"

# Optional Node syntax check (skip if node missing)
if command -v node >/dev/null 2>&1; then
  for f in "$EXT_DIR/service_worker.js" "$EXT_DIR/content.js" "$EXT_DIR/options/options.js"; do
    node --check "$f"
  done
  echo "JS syntax OK"
fi

mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/qd-get-cookies-v$VERSION.zip"
rm -f "$ZIP"

# Build a deterministic zip (sorted file order, fixed mtime).
# Using python instead of `zip` to keep timestamps reproducible.
python3 - "$EXT_DIR" "$ZIP" <<'PY'
import os, sys, zipfile, time
src, out = sys.argv[1], sys.argv[2]
files = []
for root, _, names in os.walk(src):
    for n in names:
        ap = os.path.join(root, n)
        rp = os.path.relpath(ap, src).replace(os.sep, "/")
        files.append((rp, ap))
files.sort()
ts = (2026, 1, 1, 0, 0, 0)  # fixed mtime → deterministic zip
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for rp, ap in files:
        info = zipfile.ZipInfo(rp, ts)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(ap, "rb") as fh:
            zf.writestr(info, fh.read())
PY

SIZE="$(wc -c < "$ZIP" | tr -d ' ')"
SHA256="$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$ZIP")"

echo "built: $ZIP"
echo "  version : $VERSION"
echo "  size    : $SIZE bytes"
echo "  sha256  : $SHA256"
