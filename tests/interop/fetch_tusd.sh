#!/usr/bin/env sh
# Download the latest tusd release binary from GitHub into tests/interop/tusd/
# (gitignored) and print its path. Skips the download if already present;
# delete tests/interop/tusd/ to force a refresh to the newest release.
#
# The lane deliberately tracks the newest tusd rather than a pinned version:
# the point of the interop suite is to find out when upstream changes break
# compatibility. The download is checked against the sha256 GitHub publishes
# alongside the asset, which catches a truncated or corrupted transfer — it is
# not a supply-chain pin, since both come from the same release.
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
    linux) EXT=tar.gz ;;
    darwin) EXT=zip ;;
    *)
        echo "fetch_tusd: unsupported os: $OS" >&2
        exit 1
        ;;
esac

ASSET="tusd_${OS}_${ARCH}"
URL="https://github.com/tus/tusd/releases/latest/download/${ASSET}.${EXT}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "fetch_tusd: downloading latest tusd ($ASSET)..." >&2
curl -fsSL "$URL" -o "$TMP/$ASSET.$EXT"

EXPECTED="$(curl -fsSL "$URL.sha256" | cut -d' ' -f1)"
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "$TMP/$ASSET.$EXT" | cut -d' ' -f1)"
else
    ACTUAL="$(shasum -a 256 "$TMP/$ASSET.$EXT" | cut -d' ' -f1)"
fi
if [ -z "$EXPECTED" ] || [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "fetch_tusd: sha256 mismatch for $ASSET.$EXT: expected ${EXPECTED:-<none published>}, got $ACTUAL" >&2
    exit 1
fi

if [ "$EXT" = "zip" ]; then
    unzip -q -o "$TMP/$ASSET.$EXT" -d "$TMP"
else
    tar -xzf "$TMP/$ASSET.$EXT" -C "$TMP"
fi
mkdir -p "$DIR/tusd"
cp "$TMP/$ASSET/tusd" "$BIN"
chmod +x "$BIN"
echo "$BIN"
