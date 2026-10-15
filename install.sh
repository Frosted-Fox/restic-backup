#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_FILE=/etc/restic-node-backup/config.json

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root: sudo sh $0" >&2
    exit 1
fi

if [ ! -t 0 ]; then
    echo "Interactive setup needs a terminal. Run: sudo sh $0" >&2
    exit 1
fi

if [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null 2>&1; then
    echo "A running systemd installation is required for the backup scheduler." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v restic >/dev/null 2>&1; then
    echo "Installing Python 3 and Restic using this system's package manager..."
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y python3 restic
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 restic
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 restic
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 restic
    elif command -v pacman >/dev/null 2>&1; then
        pacman --noconfirm --needed -S python restic
    elif command -v apk >/dev/null 2>&1; then
        apk add python3 restic
    else
        echo "No supported package manager found. Install Python 3 and Restic, then run this installer again." >&2
        exit 1
    fi
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v restic >/dev/null 2>&1; then
    echo "Python 3 or Restic is still unavailable after package installation." >&2
    exit 1
fi

echo
echo "=== Restic Node Backup setup ==="
echo "The default source is /var/lib/pterodactyl."
if [ ! -d /var/lib/pterodactyl ]; then
    echo "Note: /var/lib/pterodactyl is not present on this server; you can enter another source path."
fi
echo

if [ -f "$CONFIG_FILE" ]; then
    printf "Existing configuration found. Reconfigure it now? [y/N] "
    IFS= read -r answer
    case "$answer" in
        y|Y|yes|YES)
            python3 "$SCRIPT_DIR/restic_node_backup.py" configure
            ;;
        *)
            echo "Keeping the existing configuration."
            ;;
    esac
else
    python3 "$SCRIPT_DIR/restic_node_backup.py" configure
fi

while :; do
    echo
    if python3 "$SCRIPT_DIR/restic_node_backup.py" check-source &&
       python3 "$SCRIPT_DIR/restic_node_backup.py" ensure-repo; then
        break
    fi

    echo
    echo "Setup could not verify the backup source and S3/Restic connection."
    printf "Choose: [r]etry check, [e]dit configuration, or [q]uit: "
    IFS= read -r answer
    case "$answer" in
        r|R|retry|RETRY)
            ;;
        e|E|edit|EDIT)
            python3 "$SCRIPT_DIR/restic_node_backup.py" configure
            ;;
        q|Q|quit|QUIT)
            echo "Setup cancelled. No scheduler service was installed or started." >&2
            exit 1
            ;;
        *)
            echo "Please enter r, e, or q." >&2
            ;;
    esac
done

chmod 0600 "$CONFIG_FILE"

echo
echo "Installing and starting the scheduled backup service..."
install -d -o root -g root -m 0755 /opt/restic-node-backup
install -o root -g root -m 0755 "$SCRIPT_DIR/restic_node_backup.py" /opt/restic-node-backup/restic_node_backup.py
install -o root -g root -m 0755 "$SCRIPT_DIR/rybernt" /usr/local/bin/rybernt
install -o root -g root -m 0644 "$SCRIPT_DIR/systemd/restic-node-backup.service" /etc/systemd/system/restic-node-backup.service
install -d -o root -g root -m 0700 /var/lib/restic-node-backup
systemctl daemon-reload
systemctl enable --now restic-node-backup.service
systemctl restart restic-node-backup.service

echo
echo "Setup complete. The scheduler is enabled and running."
echo "Open dashboard anytime: rybernt backup"
echo "Check status: sudo systemctl status restic-node-backup"
echo "Other commands: rybernt status | rybernt snapshots | rybernt run"
printf "Open the dashboard now? [y/N] "
IFS= read -r answer
case "$answer" in
    y|Y|yes|YES)
        exec /usr/local/bin/rybernt backup
        ;;
esac
