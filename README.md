# Restic Node Backup

A root-run Linux backup manager for Pterodactyl/game nodes. It uses Restic for
encrypted, deduplicated snapshots; includes a keyboard-driven terminal
dashboard; and runs a persistent scheduler under systemd. The default source is
`/var/lib/pterodactyl`.

## Features

- Restic repository support, including AWS S3 and S3-compatible providers.
- Credentials and the Restic encryption password stored in a root-only
  configuration file (`0600`); credentials are passed to Restic through its
  environment, never command-line arguments.
- Repository encryption is optional: a blank password explicitly selects
  Restic's `--insecure-no-password` mode. This leaves backup data unencrypted
  and should only be used when that exposure is acceptable.
- Periodic backups with persistent schedule state, immediate catch-up after
  downtime, and exponential retry (1 minute up to 1 hour) after failures.
- A local process lock prevents two instances on this node from backing up at
  once. On recognized Restic lock errors, the app invokes ordinary `restic
  unlock` to clear only locks Restic confirms are stale, then retries with
  Restic's bounded 30-second `--retry-lock` wait, repeating safe stale-lock
  checks for up to 30 minutes. This lets locks left by a reboot be cleared as
  soon as Restic identifies them as stale, while allowing active operations to
  finish. It never uses `--remove-all`, so an active operation on another
  machine is not forcibly unlocked.
- Retain the latest configured number of this app's snapshots (tagged and
  host-scoped) and prune unused repository data after a successful backup.
- Snapshot listing, repository verification, and restore to a separate path.
- Rotating local activity log and systemd journal output.

## Install and configure

On a supported Linux server with systemd, run this single command as an
administrator to download the latest version and start the interactive
installer. No GitHub account or SSH key is required:

```sh
curl -fsSL https://raw.githubusercontent.com/Frosted-Fox/restic-backup/main/install-remote.sh | sudo sh
```

If you prefer to download or install manually:

```sh
git clone --depth 1 https://github.com/Frosted-Fox/restic-backup.git
cd restic-backup
sudo ./install.sh
```

The one-command bootstrap requires `curl`, `tar`, and an interactive terminal.

The installer installs Python and Restic using the system package manager when
needed, prompts for the S3
endpoint URL, bucket name, optional Restic encryption password, source path,
S3 credentials, backup interval, and retention count. Leave the password
blank to disable repository encryption, or enter one to encrypt backups. It
also asks for the snapshot display time zone as an IANA time-zone name, such
as `Asia/Kolkata`: search by country, city, or zone, then select a result
(the default is `UTC`). The dashboard's Configure action offers the same
searchable selector. It checks the S3 connection and
existing Restic repository, initializes only a repository that is not
initialized yet, then installs and enables the service:

```sh
sudo ./install.sh
```

The default backup source is `/var/lib/pterodactyl`; the installer warns if
that directory is missing so you can enter the correct path for your node.
For S3, the repository typically looks like:

```text
s3:https://s3.us-east-1.amazonaws.com/my-backup-bucket/game-node-01
```

For another S3-compatible provider, use its HTTPS endpoint and bucket path, for
example `s3:https://<account-endpoint>/<bucket>/<node>`. Leave the access key
and secret blank when the host has an appropriate instance role or AWS
credential provider configured. Use a strong, unique Restic password and keep
a separate copy: without it, encrypted snapshots cannot be restored.
Without a password, Restic stores data unencrypted; anyone who can access the
bucket can read the backup contents. This is a deliberate opt-out, not a
recommended setting.
When changing S3 credentials, leave the secret prompt blank to keep the
existing value or enter `-` to clear it and use host credentials instead.

The installer supports apt, dnf, yum, zypper, pacman, and apk package managers.
It requires an interactive terminal and a running systemd installation. To
re-run setup later, invoke the installer again; it asks whether to update the
existing configuration. If the source or S3 connection test fails, choose
retry, edit the configuration, or quit. The final prompt offers to open the
dashboard.

For a manual configuration change without reinstalling:

```sh
sudo python3 /opt/restic-node-backup/restic_node_backup.py configure
sudo systemctl restart restic-node-backup
```

The installer places the application in `/opt/restic-node-backup` and enables
`restic-node-backup.service`. It runs as root so it can read all Pterodactyl
files. Review the service's access to any custom source and local repository
paths before deployment.

## Terminal dashboard

After installing, open the interactive btop-inspired TUI with:

```sh
rybernt backup
```

The installer adds the `rybernt` command to `/usr/local/bin`. You can also run
`rybernt tui` to open the same dashboard.

The dashboard uses a compact, boxed status/actions/activity layout and carries
the footer watermark “Made by Ariyan Kumar | Rybernt”.

Keyboard controls: `r` run a backup, `s` browse all snapshots and inspect
details, `x` select a snapshot and restore it after confirming a separate
target directory, `v` verify repository indexes and packs, `i` initialize the
configured repository, `p` apply retention/prune (with an explicit
confirmation), `k` cancel a running task, `l` open the scrollable activity log,
`c` configure, and `q` quit. The dashboard displays live Restic progress when
available, current files, task status, and a few recent log entries. Restic's
high-frequency progress events are omitted from the log, and task output is
kept off the dashboard terminal so it cannot overwrite the screen. The TUI
temporarily uses the terminal's full-screen mode; close it with `q` to return
to the shell. Cancellation stops the active Restic command; for
the systemd scheduler the dashboard requests cancellation and the service
advances to the next scheduled interval. The Configure editor lists each
setting and its current value; move to a setting and press Enter to edit it,
then choose **Save changes** to validate and write or **Cancel without saving**
to discard edits. Credential values are never displayed. Blank secret input
keeps the current value, and `-` clears a credential (or disables Restic
encryption for the password). Snapshot timestamps use the configured time
zone in both the snapshot list and details. The time-zone selector filters as
you type by country, city, or zone name and previews local time and UTC offset.
The dashboard clock, last-success/next-run times, and log timestamps also use
the selected zone. Use Backspace to edit the search. In the snapshot browser,
select a snapshot and press Enter or `d` for its metadata/details; press `r` in
the list or details screen to restore it to a separate directory after
confirmation. Restore destinations must be absolute paths; their parent
directory must already exist and be writable. A missing final directory
requires typing `CREATE`, while restoring into an existing directory requires
typing `RESTORE`. Invalid paths and mistyped confirmations show a specific
error and let you retry; restores that overlap the configured live source are
rejected. In a text prompt, use Ctrl+U to clear the current value, or use
Backspace and the arrow keys to edit it. The `s` browser and `x` restore
shortcut both support this flow. Restoring to `/` is allowed but can overwrite
live files; it requires typing `RESTORE TO /` as an additional safeguard.
For full data verification (slower and more bandwidth-intensive), use:

```sh
sudo /usr/bin/python3 /opt/restic-node-backup/restic_node_backup.py verify --read-data
```

## Operations

```sh
sudo systemctl status restic-node-backup
sudo journalctl -u restic-node-backup -f
sudo /usr/bin/python3 /opt/restic-node-backup/restic_node_backup.py status
sudo /usr/bin/python3 /opt/restic-node-backup/restic_node_backup.py snapshots
sudo /usr/bin/python3 /opt/restic-node-backup/restic_node_backup.py run
sudo /usr/bin/python3 /opt/restic-node-backup/restic_node_backup.py restore \
  --snapshot <snapshot-id> --target /srv/restore
```

The restore target must be an absolute path that does not overlap the live
backup source; inspect restored files before replacing production data.
Retention only selects snapshots carrying this app's tag and the current
node's hostname, so pruning does not discard unrelated snapshots in a shared
repository.

## Scheduling and reliability notes

The scheduler persists its next due time, so after a service restart or node
outage it immediately performs one catch-up backup when a run is overdue.
Repeated failures keep that run due and retry with bounded exponential
backoff; systemd restarts the scheduler if the process exits unexpectedly.
When several interval deadlines pass while the node is offline, they are
coalesced into one current snapshot rather than replayed as redundant copies.
No software can complete a backup while the node, repository, or network is
unavailable, so monitor the service and alert on failed runs using your normal
host monitoring.

On this node, the file lock serializes manual and scheduled actions. Restic's
normal `unlock` behavior handles stale repository locks only; if another node
is actively writing, unlock can fail safely and the backup is retried later.
The `--read-data` check is recommended periodically because a normal Restic
check does not read every stored data pack.

Backups read the live source tree; they do not pause game processes or create a
filesystem-wide atomic snapshot. For worlds or databases that require
application-level consistency, schedule a server quiesce or database dump
through your existing maintenance tooling before the backup window.

Configuration is stored at `/etc/restic-node-backup/config.json`; runtime
schedule state and rotating logs are under `/var/lib/restic-node-backup`.
Restrict root access, keep the encryption password recoverable outside this
node, and monitor repository capacity and backup success.

## Development checks

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile restic_node_backup.py
```
