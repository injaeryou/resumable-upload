#!/usr/bin/env sh
# Download the latest tusd release binary from GitHub into tests/interop/tusd/
# (gitignored) and print its path. Skips the download if already present;
# delete tests/interop/tusd/ to force a refresh to the newest release.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/tusd/tusd"

if [ -x "$BIN" ]; then
    echo "$BIN"
    exit 0
fi

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64 | amd64) ARCH=amd64 ;;
    aarch64 | arm64) ARCH=arm64 ;;
    *)
        echo "fetch_tusd: unsupported arch: $ARCH" >&2
        exit 1
        ;;
esac
case "$OS" in
    linux | darwin) ;;
    *)
        echo "fetch_tusd: unsupported os: $OS" >&2
        exit 1
        ;;
esac

ASSET="tusd_${OS}_${ARCH}"
URL="https://github.com/tus/tusd/releases/latest/download/${ASSET}.zip"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "fetch_tusd: downloading latest tusd ($ASSET)..." >&2
curl -fsSL "$URL" -o "$TMP/tusd.zip"
unzip -q -o "$TMP/tusd.zip" -d "$TMP"
mkdir -p "$DIR/tusd"
cp "$TMP/$ASSET/tusd" "$BIN"
chmod +x "$BIN"
echo "$BIN"
