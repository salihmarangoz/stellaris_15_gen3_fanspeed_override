#!/usr/bin/env bash
# Build dist/StellarisFanControl-linux.tar.gz and its SHA-256 checksum.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME=StellarisFanControl-linux
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME/scripts/linux" "$STAGE/$NAME/assets" "$ROOT/dist"
for item in backend frontend shared; do
    cp -r "$ROOT/$item" "$STAGE/$NAME/$item"
done
cp "$ROOT/assets/stellaris-fan-control.png" "$STAGE/$NAME/assets/"
cp "$ROOT"/stellaris15gen3_linux_service.py "$ROOT"/stellaris15gen3_frontend.py \
   "$ROOT"/README.md "$ROOT"/THIRD_PARTY_NOTICES.md "$STAGE/$NAME/"
find "$ROOT/scripts/linux" -maxdepth 1 -type f -exec cp -p {} "$STAGE/$NAME/scripts/linux/" \;
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +

tar -C "$STAGE" --owner=0 --group=0 -czf "$ROOT/dist/$NAME.tar.gz" "$NAME"
(cd "$ROOT/dist" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "Wrote dist/$NAME.tar.gz"
