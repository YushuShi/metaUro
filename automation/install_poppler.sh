#!/usr/bin/env bash
# Build the exact PDF parser required by the saved source-validation contract.
set -euo pipefail

POPPLER_VERSION=26.07.0
POPPLER_SHA256=304832f48f8a47fdca90c6b6d1f684e68f37c10c9a0726f345f4ca9df4ca01e2
POPPLER_URL="https://poppler.freedesktop.org/poppler-${POPPLER_VERSION}.tar.xz"

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 INSTALL_PREFIX [VERIFIED_SOURCE_ARCHIVE]" >&2
  exit 2
fi
prefix="$1"
if [[ "$prefix" != /* || "$prefix" == / ]]; then
  echo 'INSTALL_PREFIX must be an absolute, non-root path.' >&2
  exit 2
fi
activate_prefix() {
  if [[ -n "${GITHUB_PATH:-}" ]]; then
    echo "$prefix/bin" >> "$GITHUB_PATH"
  fi
}
# Reuse only a complete installation with matching source and binary receipts,
# and repeat the substantive extraction check. No apt/network calls on reuse.
if [[ $# == 1 && -f "$prefix/verification.json" ]] && \
  python3 - "$prefix" "$POPPLER_SHA256" "$(dirname "$0")" <<'PY'
import json, pathlib, sys
sys.path.insert(0, sys.argv[3])
from verify_poppler import verify
try:
    prefix = pathlib.Path(sys.argv[1])
    saved = json.loads((prefix/'verification.json').read_text())
    current = verify(prefix/'bin')
    if saved.get('source_sha256') != sys.argv[2] or saved.get('tools') != current['tools']:
        raise ValueError('Saved installation provenance differs')
except Exception:
    raise SystemExit(1)
print('Reusing verified Poppler 26.07.0 installation')
PY
then
  activate_prefix
  exit 0
fi
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/monthly-poppler.XXXXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
archive="$build_dir/poppler.tar.xz"
if [[ $# == 2 ]]; then
  cp -- "$2" "$archive"
else
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
    --connect-timeout 30 --max-time 180 --output "$archive" "$POPPLER_URL"
fi
# Verify before extraction or running any code from the archive.
python3 - "$archive" "$POPPLER_SHA256" <<'PY'
import hashlib, pathlib, sys
actual = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
if actual != sys.argv[2]:
    raise SystemExit('Official Poppler source checksum mismatch')
PY
if [[ "$(uname -s)" != Linux ]]; then
  echo 'This installer targets the Ubuntu 24.04 monthly runner.' >&2
  exit 2
fi
python3 - <<'PY'
from pathlib import Path
release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
    raise SystemExit('This installer requires Ubuntu 24.04 build dependencies')
PY
privilege=()
if [[ "$EUID" -ne 0 ]]; then
  privilege=(sudo)
fi
"${privilege[@]}" apt-get update
"${privilege[@]}" apt-get install --yes --no-install-recommends \
  cmake ninja-build g++ pkg-config curl libfreetype-dev libfontconfig-dev \
  libjpeg-dev libopenjp2-7-dev libpng-dev zlib1g-dev poppler-data
tar -xJf "$archive" -C "$build_dir"
cmake -S "$build_dir/poppler-$POPPLER_VERSION" -B "$build_dir/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DPOPPLER_DATADIR=/usr/share/poppler -DBUILD_SHARED_LIBS=OFF \
  -DENABLE_UTILS=ON -DENABLE_CPP=OFF -DENABLE_GLIB=OFF \
  -DENABLE_GOBJECT_INTROSPECTION=OFF -DENABLE_QT5=OFF -DENABLE_QT6=OFF \
  -DENABLE_BOOST=OFF -DENABLE_GPGME=OFF -DENABLE_NSS3=OFF \
  -DENABLE_LIBCURL=OFF -DENABLE_LIBTIFF=OFF -DENABLE_LCMS=OFF \
  -DBUILD_GTK_TESTS=OFF -DBUILD_QT5_TESTS=OFF -DBUILD_QT6_TESTS=OFF \
  -DBUILD_CPP_TESTS=OFF -DBUILD_MANUAL_TESTS=OFF
cmake --build "$build_dir/build" --target pdfinfo pdftotext pdfdetach --parallel 2
mkdir -p "$prefix/bin"
for tool in pdfinfo pdftotext pdfdetach; do
  install -m 755 "$build_dir/build/utils/$tool" "$prefix/bin/$tool"
done
python3 "$(dirname "$0")/verify_poppler.py" --bin-dir "$prefix/bin" \
  --receipt "$prefix/verification.json"
python3 - "$prefix" "$POPPLER_URL" "$POPPLER_SHA256" <<'PY'
import json, pathlib, subprocess, sys
prefix = pathlib.Path(sys.argv[1])
receipt = json.loads((prefix/'verification.json').read_text())
receipt.update(source_url=sys.argv[2], source_sha256=sys.argv[3],
               compiler=subprocess.check_output(['c++', '--version'], text=True).splitlines()[0],
               cmake=subprocess.check_output(['cmake', '--version'], text=True).splitlines()[0],
               build='static Poppler core; system codec/font libraries; Ubuntu 24.04')
(prefix/'verification.json').write_text(json.dumps(receipt, indent=2)+'\n')
PY
activate_prefix
