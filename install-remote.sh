#!/bin/sh
set -eu

REPOSITORY=Frosted-Fox/restic-backup
ARCHIVE_URL="https://github.com/$REPOSITORY/archive/refs/heads/main.tar.gz"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: curl -fsSL https://raw.githubusercontent.com/$REPOSITORY/main/install-remote.sh | sudo sh" >&2
    exit 1
fi

if [ ! -r /dev/tty ]; then
    echo "Interactive setup needs a terminal. Run the one-line installer from a terminal." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
    echo "curl and tar are required to download the installer." >&2
    exit 1
fi

TEMP_DIR=$(mktemp -d /tmp/restic-node-backup.XXXXXX)
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

echo "Downloading Restic Node Backup from $REPOSITORY..."
curl --fail --silent --show-error --location "$ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz"
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR"

if [ ! -f "$TEMP_DIR/install.sh" ]; then
    echo "Downloaded archive does not contain install.sh." >&2
    exit 1
fi

sh "$TEMP_DIR/install.sh" </dev/tty
