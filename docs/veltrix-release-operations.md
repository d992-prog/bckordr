# Veltrix release operations

This runbook covers the independent backup and release watchdog jobs. Installing
the files does not authorize a public launch: payment, public trial, fleet health,
backup and watchdog remain disabled until their own acceptance steps pass.

## Fail-closed configuration

Keep the following values in `/opt/domain-drop-catcher/backend/.env` while the
units are being installed:

```dotenv
VPN_BACKUP_ENABLED=false
VPN_BACKUP_DIRECTORY=/var/backups/domain-drop-catcher
VPN_BACKUP_RETENTION=7
VPN_WATCHDOG_ENABLED=false
VPN_ALERT_TELEGRAM_USER_ID=
VPN_WATCHDOG_STATE_PATH=/var/lib/veltrix-watchdog/state.json
VPN_CONTROL_KNOWN_HOSTS_PATH=/etc/veltrix/known_hosts
```

All three paths must be absolute. Retention must be from 2 through 31. The watchdog
cannot be enabled without the positive numeric Telegram user ID of the owner.
It reuses `VPN_TELEGRAM_BOT_TOKEN`; never put either value in a command line or
unit file.

The backup unit supplies these non-secret source paths itself:

- `/opt/domain-drop-catcher/backend/.env`;
- `/etc/systemd/system/domain-drop-control.service`;
- `/etc/nginx`;
- `/opt/domain-drop-catcher/frontend/dist`.

If production uses different real paths, update the four `VPN_BACKUP_*` source
variables in `deploy/veltrix-backup.service` before installation. Do not back up
an example unit instead of the unit that systemd actually loads.

The watchdog unit has `ProtectHome=true`, so a path below `/root`, `/home` or
`/run/user` is deliberately inaccessible. Install the reviewed literal Ed25519
pins as one regular file outside home directories and point the application
setting at that exact file. The file is root-owned, group-readable by the
effective group of `domain-drop-control.service`, and writable by neither the
control service nor the watchdog. Resolve both unit fields first; an empty
`User` means `root`, while an empty `Group` means the configured user's primary
group:

```bash
sudo install -d -o root -g root -m 0755 /etc/veltrix
systemctl show -p User -p Group domain-drop-control.service
sudo install -o root -g CONTROL_SERVICE_GROUP -m 0640 \
  /PATH/TO/REVIEWED/known_hosts /etc/veltrix/known_hosts
```

Replace the placeholder with the unit's explicit group, or with the configured
user's primary group when `Group` is empty. Use group `root` only when the unit
runs as root. The file must contain one canonical literal Ed25519 pin per node,
be non-empty, no larger than 64 KiB, be owned by
`root:CONTROL_SERVICE_GROUP` and have exact mode `0640`. Node onboarding appends
its reviewed pin under the shared lock. A repeated identical pin is idempotent;
a different key for an existing target is rejected and is never treated as
implicit key rotation. The same file is used by strict fleet health and the
root watchdog; neither path generates or accepts host keys at runtime, and the
watchdog has no DAC-bypass capability.

## Install without enabling

Run this only after a reviewed build is present under
`/opt/domain-drop-catcher`:

```bash
sudo install -d -o root -g root -m 0700 /var/backups/domain-drop-catcher
sudo install -o root -g root -m 0644 deploy/veltrix-backup.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/veltrix-backup.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/veltrix-watchdog.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/veltrix-watchdog.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/veltrix-operations.env /etc/veltrix-operations.env
sudo systemd-analyze verify --recursive-errors=yes \
  /etc/systemd/system/veltrix-backup.service \
  /etc/systemd/system/veltrix-backup.timer \
  /etc/systemd/system/veltrix-watchdog.service \
  /etc/systemd/system/veltrix-watchdog.timer
sudo systemctl daemon-reload
sudo systemctl disable --now veltrix-backup.timer veltrix-watchdog.timer
```

The watchdog state directory is created by `StateDirectory=veltrix-watchdog`.
The backup directory must already exist, be owned by root and have mode `0700`.
The mandatory `/etc/veltrix-operations.env` is loaded after the application
`.env`; it pins both writable paths to the paths allowed by `ProtectSystem`.
Omitting it makes both services fail before execution. If paths must change,
change this file and the matching `ReadWritePaths`/`ReadOnlyPaths` together in a
reviewed unit override. Do not enable either timer merely because
`systemd-analyze` accepts its syntax.

## First manual backup

Before the first run, confirm the database URL, free space, actual source paths
and that no deployment is replacing the frontend or Nginx tree. Then set only
`VPN_BACKUP_ENABLED=true`, reload the unit environment and start one job:

```bash
sudo systemctl start veltrix-backup.service
sudo systemctl status --no-pager veltrix-backup.service
sudo stat -c '%U %G %a %n' /var/backups/domain-drop-catcher
sudo stat -c '%U %G %a %n' /var/backups/domain-drop-catcher/latest-success.json
```

A successful set is a timestamped directory without the `.partial` suffix. Its
`backup.json` is byte-for-byte equal to `latest-success.json`; `manifest.json`
contains SHA-256 for every copied file. A failed run returns non-zero, leaves the
previous marker untouched and may leave a `.partial` directory for diagnosis.
Never rename a partial set into a successful set manually.

Verify the dump can be read in full without exposing rows:

```bash
sudo /usr/bin/pg_restore --list \
  /var/backups/domain-drop-catcher/SET_NAME/database.dump >/dev/null
```

## Isolated restore rehearsal

Use a new local database with synthetic data first. For a protected production
copy, keep the rehearsal on the same trusted server, bind PostgreSQL only to a
local socket, and remove the disposable database after verification.

```bash
sudo -u postgres createdb veltrix_restore_rehearsal
sudo sh -c 'exec sudo -u postgres /usr/bin/pg_restore --exit-on-error \
  --dbname=veltrix_restore_rehearsal < "$1"' sh \
  /var/backups/domain-drop-catcher/SET_NAME/database.dump
sudo -u postgres dropdb veltrix_restore_rehearsal
```

Inspect schema counts and application startup against the disposable database;
do not point the running service at it. A successful `pg_restore --list` alone is
not a restore rehearsal.

After the manual backup and restore rehearsal pass, enable only its timer:

```bash
sudo systemctl enable --now veltrix-backup.timer
sudo systemctl list-timers --all veltrix-backup.timer
```

## Watchdog acceptance

Set the owner's numeric ID and keep the token only in the root-private `.env`.
Run the watchdog manually before enabling its timer. It checks systemd, local and
public HTTP, cabinet HTTP, disk, backup freshness and the shared database-backed
release evaluator. It never restarts a service or changes a VPN node.

Each command and HTTP operation is bounded. Operational observations remain
valid for 10 minutes; the trusted successful-backup marker remains valid for 36
hours. The watchdog stores the same bounded observation snapshot used by the
admin release-readiness screen. If PostgreSQL is unavailable, the local checks
still run and the alert also contains `control_database_unavailable`.

For one unchanged set of failing check codes it sends one alert. It sends another
alert only when the set changes, and one recovery when all checks recover. Failed
Telegram delivery remains retryable. Messages contain fixed check codes only,
never exception text, database URLs, host credentials, VPN UUIDs or connection
URIs.

Rehearse the alert/recovery pair first with synthetic settings and a fake or
dedicated staging Telegram endpoint; do not overwrite the production dedupe
state to manufacture a recovery. On production, a manual run may send a real
alert for the checks that are genuinely red. Recovery is sent only after those
checks actually become green.

After a reviewed alert/recovery rehearsal, set `VPN_WATCHDOG_ENABLED=true` and:

```bash
sudo systemctl start veltrix-watchdog.service
sudo systemctl status --no-pager veltrix-watchdog.service
sudo stat -c '%U %G %a %n' /var/lib/veltrix-watchdog/state.json
sudo systemctl enable --now veltrix-watchdog.timer
sudo systemctl list-timers --all veltrix-watchdog.timer
```

The state file must be root-owned and `0600`; its directory must be `0700`.

## Disable and investigate

Disabling the jobs does not affect existing VPN sessions:

```bash
sudo systemctl disable --now veltrix-watchdog.timer veltrix-backup.timer
```

Read only bounded service status and fixed exit codes. Do not paste `.env`, dump
content, Telegram request URLs or raw watchdog state into tickets or chat. A
backup failure does not justify deleting the previous successful set. A watchdog
failure does not authorize an automatic service or node restart.

## Rollback and recovery

1. Disable both timers and record the current application revision.
2. Preserve the failed/partial set for local diagnosis; do not make it current.
3. Choose the set named by a previously verified `backup.json`, verify its
   manifest hashes and complete an isolated restore rehearsal.
4. Stop only the control service during the approved maintenance window.
5. Restore the database into a new database first, then switch the application
   using the normal database migration/rollback procedure. Restore `.env`, the
   actual systemd unit, Nginx and frontend only from the same validated set.
6. Run `nginx -t`, `systemd-analyze verify --recursive-errors=yes`, local
   `/api/health`, public `/api/health`, cabinet login and an existing VPN profile
   before reopening any public gate.
7. Re-enable timers separately only after their manual runs pass.

Rollback never enables payment or public trial and never rewrites VPN client
identities. If the chosen application revision cannot read the current additive
schema, restore its matching validated database instead of editing production
rows by hand.
