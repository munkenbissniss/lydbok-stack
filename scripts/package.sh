#!/usr/bin/env bash
# Bygger dist/lydbok-stack-{linux,macos,windows}.zip fra repoet.
# Brukes av GitHub Actions ved ny versjon (tag v*), men kan også kjøres lokalt.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION="$(cat VERSION)"
COMMON=(compose.yaml compose.*.yaml tools homepage README.md LICENSE VERSION CHANGELOG.md)

rm -rf dist && mkdir -p dist

build() {
  local os="$1"; shift
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/lydbok-stack"
  cp -r "${COMMON[@]}" "$tmp/lydbok-stack/"
  for extra in "$@"; do cp -r $extra "$tmp/lydbok-stack/"; done
  find "$tmp" \( -name __pycache__ -o -name '*.pyc' -o -name .DS_Store \) -prune -exec rm -rf {} +
  find "$tmp" \( -name '*.sh' -o -name '*.command' \) -exec chmod 755 {} +
  (cd "$tmp" && zip -qrX "$ROOT/dist/lydbok-stack-$os.zip" lydbok-stack)
  rm -rf "$tmp"
  echo "  dist/lydbok-stack-$os.zip"
}

echo "Lydbok-stack $VERSION"
build linux   linux "packaging/linux/*"
build macos   "packaging/macos/*"
build windows "packaging/windows/*"
