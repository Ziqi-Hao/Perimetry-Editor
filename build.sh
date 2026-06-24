#!/usr/bin/env bash
# Build a single-file, double-click desktop executable with PyInstaller.
#
#   pip install -r requirements-dev.txt   # one-time (build tooling only)
#   ./build.sh
#
# Output:
#   dist/PerimetryEditor        (macOS / Linux)
#   dist/PerimetryEditor.exe    (Windows, when run there)
#
# The runtime app needs no third-party packages — this is purely the packager.
# Windows/macOS/Linux release binaries are also built automatically by
# .github/workflows/build.yml on every version tag.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m PyInstaller \
  --onefile \
  --name PerimetryEditor \
  --paths app \
  --hidden-import server \
  --hidden-import hvf_24_2 \
  --console \
  --clean --noconfirm \
  app/desktop.py

echo
echo "Built → dist/PerimetryEditor$( [ "${OS:-}" = "Windows_NT" ] && echo .exe )"
