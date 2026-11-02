# Maintenance

Day-two operations for a host built by this repo: keeping it patched,
reading its logs, and working out why mail is not moving. Setup lives in
[README.md](README.md); this file covers everything after that.

## Ground rules

**The host is not the source of truth, the repo is.** Everything under
`/data/mailstack` (compose file, Caddyfile, `plan.ndjson`, `provision.ndjson`,
the CLI env files) is rendered by `roles/mailstack` and overwritten on the
next playbook run. Edit files in this repo, then re-run:

```bash
cd ansible
ansible-playbook playbooks/10-site.yml
```

**Stalwart's infrastructure configuration is declarative too.** Listeners,
the Cloudflare DNS provider, logging, S3, CORS and the proxy settings come
from `group_vars/all.yml` via `plan.ndjson.j2`, applied on every playbook
run. Changing them in the admin console works until the next run
reconciles them back.

**Domains and accounts are not.** The primary domain, its ACME provider
and `stalwart_accounts` are in `seed.ndjson.j2`, applied once when a fresh
server is provisioned. After that the admin console owns them, along with
everything else the plan does not describe: other domains, tenants, the
Enterprise license, mailboxes, Sieve scripts, blocked IPs, certificates
and DKIM keys. Editing `stalwart_accounts` on a provisioned server changes
nothing. Before this split the seed was re-applied on every run, which
reset the seeded accounts' passwords and deleted their app passwords and
TOTP, and registered a new Let's Encrypt account each time.

**Upserts never destroy.** Nothing the playbook applies deletes an object.
Deleting is a deliberate act in the console or through `stalwart-cli
delete`.

## Getting onto the box

```bash
ssh <admin_user>@<tailscale-ip>     # or the tailnet name, e.g. mail01
cd /data/mailstack
docker compose ps
```

There is no public SSH. If Tailscale is down for you, the way in is your
VPS provider's console, not this repo.

`admin_user` is in the `docker` group, so `docker compose` needs no sudo.
The two `.cli*.env` files are root-owned `0600`, so reading those does.

## What is automated, and what is not

Automatic:

- Ubuntu security and ESM security patches, with a reboot at 03:30 in the
  host timezone when one is needed (`roles/bootstrap`, `Automatic-Reboot`).
- Containers restarting after that reboot (`restart: unless-stopped`).
- Stalwart's TLS certificate, issued and renewed over ACME DNS-01.
- DKIM key rotation every 90 days, with the new selector published to
  Cloudflare by Stalwart itself.
- Docker log rotation, 10 MB per file and 3 files per container by default;
  OVH LDP deployments instead use the bounded persistent system journal.

Not automatic, and yours to do:

- **Container image updates.** Nothing pulls new images on its own, by
  design. See below.
- **Backups.** Still unbuilt, see [Backups](#backups-not-built-yet).
- Non-security OS updates (kernel HWE jumps, release upgrades).
- Renewing the Cloudflare API token and keeping the Tailscale node from
  expiring, both of which break things quietly months later.

## Updating the containers

### What is pinned where

| Component | Image | Pinned in |
|---|---|---|
| Stalwart | `stalwartlabs/stalwart:v1.0` | `ansible/roles/mailstack/templates/docker-compose.yml.j2` |
| Bulwark | `ghcr.io/bulwarkmail/webmail:latest` (unpinned, `pull_policy: always`) | same file |
| Caddy | `caddy:2-alpine` (built locally) | `ansible/roles/mailstack/files/Dockerfile.caddy` |
| stalwart-cli (container) | `ghcr.io/stalwartlabs/cli:1.0.14` (pulled every run) | `stalwart_cli_image` in `group_vars/all.yml` |
| stalwart-cli (your machine) | `1.0.14` | `mise.toml` |
| stalwart-migrate (0.16 hosts only) | `stalwartlabs/stalwart-migrate:1.0.5` | `stalwart_migrate_version` role default |
| Vector (optional OVH LDP) | `timberio/vector:0.58.0-debian` | `ovh_ldp_vector_image` role default |

All of these are series tags, not digests, so the upstream tag moves as
patch releases land. That means two different update paths.

Bulwark is the exception for now: it tracks `latest` with
`pull_policy: always`, so every playbook run pulls whatever Bulwark last
released. It will go back to a pinned series once its release pace slows.

### Patch updates inside a pinned series

Re-running the playbook does **not** do this. The deploy task runs
`docker compose up -d --build`, and compose only pulls an image it does
not already have locally, so a moved tag is invisible to it. Pulling is
an explicit, deliberate step:

```bash
cd /data/mailstack
docker compose pull                  # stalwart (bulwark also pulls on every up)
docker compose build --pull caddy    # refreshes the caddy:2-alpine base
docker compose up -d                 # recreates only what actually changed
```

`--pull` on the build matters: without it, Docker keeps using the
`caddy:2-alpine` layer it cached the first time and the rebuild is a no-op.

Then check what you are actually running and that it came back:

```bash
docker compose images
docker compose ps
docker compose logs --since 5m stalwart
```

Expect a short interruption. Stalwart is recreated, which drops live IMAP
and SMTP connections; clients reconnect on their own.

### Moving to a new minor or major version

Change the tag in the repo, not on the host:

```bash
$EDITOR ansible/roles/mailstack/templates/docker-compose.yml.j2
cd ansible && ansible-playbook playbooks/10-site.yml
```

The new tag is not present on the host, so compose pulls it as part of the
normal `up -d`. No separate pull step is needed for a tag change.

For Stalwart specifically, do three things first:

1. Read the release notes for data-store migrations and for changes to the
   configuration schema. `plan.ndjson.j2` and `seed.ndjson.j2` were
   derived from a `stalwart-cli snapshot` of one specific version; a renamed field or a
   changed enum variant surfaces as an `apply` failure mid-playbook, and
   occasionally as a setting silently reverting to its default.
2. Take a backup (see below). A data-store migration is one-way.
3. Take a configuration snapshot so you can diff the schema afterwards:
   ```bash
   stalwart-cli --url https://mail.example.com --user admin@example.com \
     snapshot Domain Account NetworkListener Tracer AllowedIp DnsServer \
     --output before-upgrade.ndjson
   ```
   Secrets come back as `****` unless you pass `--include-secrets`, so this
   is a schema record, not a restorable backup.

Keep `stalwart_cli_image` and the `mise.toml` pin on the same exact
release, and move them to the latest CLI whenever the server moves, even
for a patch release. The CLI ships separately and versions independently
of the server, but `apply` parses the server's schema, and an older CLI
can fail on a newer one outright: 1.0.12 could not read Stalwart 1.0.2's
schema. The playbook pulls the pinned tag on every run, so changing the
pin is all it takes.

### Rolling back

Revert the tag in the repo and re-run the playbook. Caddy and Bulwark roll
back cleanly. **Stalwart may not**: once it has migrated its RocksDB store,
an older binary can refuse to open it or, worse, open it wrongly. Restoring
the pre-upgrade backup is the real rollback for Stalwart. The 0.16 to 1.0
move has its own rollback, covered in the next section.

### Restarting things

```bash
docker compose restart stalwart          # same image, same config, fast
docker compose up -d --force-recreate stalwart   # after a config change
docker compose down && docker compose up -d --build   # whole stack
```

`docker compose down` is safe here. All persistent state is in bind mounts
under `/data`, not in named volumes. Do not add `-v`.

### Host tooling

```bash
mise install                                              # Python, uv, Ansible, stalwart-cli
ansible-galaxy collection install -r ansible/requirements.yml --upgrade
```

Ansible is pinned to `latest` in `mise.toml`, so `mise install` can move it
under you. If a playbook run starts failing for no reason you changed,
check whether Ansible itself moved.

## Migrating from Stalwart 0.16 to 1.0

A one-off. Stalwart 1.0 uses a new store layout and cannot open a 0.16
store, so there is no in-place upgrade: `stalwart-migrate` copies
everything into a new, empty store, and the server is switched over to
it. Delete this section once every host is on 1.0 and the old store is
gone.

Message bodies are in S3 and the migration reuses them where they are, so
only the RocksDB metadata is copied. The copy runs while 0.16 keeps
serving mail; the outage is only the final sync and the switch.

### How the repo enforces this

`roles/mailstack` keys on a marker file, `/data/stalwart/.store-v1`, next
to `etc/` and `data/` but outside both container mounts:

- **No store yet** (fresh host): the marker is written and 1.0 creates
  its own store.
- **Store with the marker**: normal deploy.
- **Store without the marker** (a 0.16 host): the playbook creates
  `/data/stalwart-v1/{etc,data}` and `/data/stalwart-migrate`, renders
  the target `config.json` and `docker-compose.migrate.yml`, and then
  **fails on purpose**, before it renders the stack's compose file or
  touches a container. The running 0.16 server is left alone.

The marker lives in `/data/stalwart-v1` from the start, so it moves into
place with the directory at cutover.

### What users will notice

- **Everyone signs in again.** OAuth tokens are not migrated, and Bulwark
  uses OAuth tokens for password+TOTP logins as well.
- IMAP and JMAP clients do not resync: account, mailbox and message ids,
  UIDs and UIDVALIDITY are preserved. CalDAV and CardDAV clients refetch
  once, because ETags change.
- Search is incomplete for a while after the switch, until 1.0 has
  rebuilt the full-text index in the background.
- Mail queued for delivery carries over (the final run copies the queue).

### Before the window

1. **Bulwark on 1.18 or later, while still on 0.16.** Every release from
   1.18 works with both server versions, so it goes first and the two
   upgrades do not land together. Deploy it from a branch that does not
   carry the Stalwart bump: on a 0.16 host, this branch's playbook stops
   before it renders the compose file, so it cannot update Bulwark.
   Confirm with `docker compose images bulwark`.
2. **Bucket versioning on** (it is), with noncurrent versions kept for
   longer than you would ever want to roll back. The rollback below
   depends on it.
3. **`bulwark_admin_password` set in `group_vars/all.yml`**, and
   `group_vars/all.yml` and `.env` in the password manager. Without the
   admin password Bulwark's dashboard is disabled and cutover step 7
   cannot be done. It reaches the host with the cutover's playbook run,
   which is the first one to render the compose file on this branch.
4. **Disk.** The target is a second copy of the RocksDB store, plus the
   backup tarball:
   ```bash
   sudo du -sh /data/stalwart/data
   df -h /
   ```
5. **The source must be 0.16.27**, the only version the tool accepts.
   The `v0.16` tag points to it, but the host only has whatever it pulled
   last. Do this with the old compose file still in place:
   ```bash
   cd /data/mailstack
   docker compose pull stalwart && docker compose up -d stalwart
   docker compose logs --since 2m stalwart | grep -i version
   ```

6. **Tenant domains need tenant-owned providers.** In 1.0 an object in a
   tenant may only reference objects in the same tenant. A tenant's
   domain pointing at a global ACME provider or DnsServer is switched to
   manual certificate (and DNS) management by the migration, so its
   certificate stops renewing and DKIM rotation stops publishing, quietly.
   List them:
   ```bash
   C="sudo docker run --rm --env-file .cli.env ghcr.io/stalwartlabs/cli:1.0"
   for id in $($C query Domain --json | jq -r '.[]'); do
     $C get Domain $id --json | jq -c '{name, memberTenantId,
       acme: .certificateManagement.acmeProviderId,
       dns: .dnsManagement.dnsServerId}'
   done
   ```
   For each tenant with a domain on a global provider, create one
   AcmeProvider and one DnsServer with that tenant set, and point its
   domains at them. This can be done in the 0.16 console beforehand, or in
   1.0 right after cutover.
7. **Optionally, delete unused ACME providers.** Before the seed was split
   out, every playbook run registered a new one. Any provider no domain
   references can go, which also keeps it out of the migration.

### Setting up the tool

Run the playbook from this branch. It stops with "holds a Stalwart 0.16
store, which 1.0 cannot open"; that is expected, and means the
directories and the tool are in place. Then, on the host:

```bash
cd /data/mailstack
alias migrate='docker compose -f docker-compose.migrate.yml run --rm migrate'

migrate prepare \
  --source /etc/stalwart/config.json \
  --state /var/lib/stalwart-migrate
```

`prepare` is interactive and needs a terminal. Two questions always come
up:

- **Reuse the external blob store?** Yes. Bodies stay where they are in
  the bucket, and the target only records which keys belong to which
  message.
- **Which data categories to copy?** All of them.

The rest depend on the configuration (OAuth clients, forwarded headers,
changed defaults, new permissions). It writes `answers.toml` and
`prepare-report.txt` to `/data/stalwart-migrate`. Read the report before
going further:

```bash
sudo less /data/stalwart-migrate/prepare-report.txt
```

### Copying while 0.16 runs

Repeat as often as you like. Each run copies only what changed since the
last one, and an interrupted run resumes:

```bash
migrate run \
  --source /etc/stalwart/config.json \
  --target /etc/stalwart-v1/config.json \
  --target-path-map /var/lib/stalwart=/var/lib/stalwart-v1 \
  --state /var/lib/stalwart-migrate

migrate status --state /var/lib/stalwart-migrate
```

`--target-path-map` is there because the target `config.json` names
`/var/lib/stalwart/db`, the path the live server will use after cutover,
while the tool sees the target under `/var/lib/stalwart-v1`.

`--max-ops-per-sec` limits the load on the live server if it is busy. If
runs fail on S3 timeouts, raise the BlobStore timeout and retries on the
0.16 server in the admin console (the tool reads them from the 0.16
store) and pass a longer `--retry-budget`.

Get to the point where a run finishes quickly with little to copy. That
is roughly how long the final sync will take.

### Cutover

1. **Stop and remove the 0.16 container.** Removing rather than stopping
   means nothing, including a 03:30 reboot, can start the 0.16 image on
   the 1.0 store later. Starting either version on the other's store
   corrupts it.
   ```bash
   cd /data/mailstack
   docker compose rm -sf stalwart
   ```
2. **Cold backup of the 0.16 store**, then copy it off the box:
   ```bash
   sudo tar -C /data/stalwart -czf /root/stalwart-0.16-$(date +%F).tar.gz data etc
   ```
3. **Final sync.** It asks you to type `yes`, watches the 0.16 store for
   30 seconds to make sure nothing is still writing to it, then copies
   the last changes plus the queue, the scheduled tasks and the spam
   model, and verifies:
   ```bash
   migrate run --final \
     --source /etc/stalwart/config.json \
     --target /etc/stalwart-v1/config.json \
     --target-path-map /var/lib/stalwart=/var/lib/stalwart-v1 \
     --state /var/lib/stalwart-migrate

   migrate report --state /var/lib/stalwart-migrate --severity error
   ```
   Plain `run --final`, without `--verify-full`, checks "1% of accounts,
   at least 16", which on a server with 16 accounts or fewer is all of
   them anyway. On mail01 (10 accounts, about 18k messages) that took 12
   minutes of the 15-minute outage; the copy itself took 3 seconds. A
   `verify --full` the day before tells you what to expect.

   Errors block the final run. `skipped` rows for spam training samples
   whose blob no longer exists are harmless; re-run with `--allow-errors`
   for those only. Anything else, and any verify mismatch: stop here
   and [roll back](#rolling-back-the-migration), which at this point is
   cheap.
4. **Swap the directories:**
   ```bash
   sudo mv /data/stalwart /data/stalwart-0.16
   sudo mv /data/stalwart-v1 /data/stalwart
   ```
5. **Run the playbook** from your machine. The marker is now in place, so
   it renders the 1.0 compose file, starts `stalwart:v1.0`, waits for its
   management API, applies the plan and deletes
   `docker-compose.migrate.yml`. Bump `stalwart_cli_image` first if the
   server is newer than the CLI it names.
6. **Check it came back:**
   ```bash
   docker compose images stalwart
   docker compose logs --since 10m stalwart | grep -i -E 'error|warn|fail'
   ```
   Then IMAPS on 993, submission on 465, a message in from outside and
   one out with `dkim=pass`, as in
   [Diagnosing mail delivery](#diagnosing-mail-delivery). Check the
   certificate is still the one from before (`openssl s_client`, as in
   [It worked yesterday](#it-worked-yesterday-and-stopped-today)).
7. **Bulwark:** sign in to `https://<webmail_hostname>/admin` with
   `bulwark_admin_password` and, under Authentication, run **Auto-configure
   OAuth** once. 1.0 only accepts OAuth clients it knows about (0.16
   accepted any), and this registers Bulwark and `bulwark-mobile`.
   Without it mail works, but the password, two-factor and app-password
   settings do not. Then sign in to webmail yourself.
8. **Scheduled tasks.** In the console, check there is an ACME renewal
   task and a DKIM management task for the domain. The final run copies
   them; if they are missing, certificates and DKIM keys stop rotating
   silently, months later. Create one of each if so.
9. **Admin console returns 404:** a stale web UI bundle from 0.16. The
   1.0.2 server and migrate 1.0.4+ both deal with it, but if it happens,
   run the `UpdateApps` action.

Keep `/data/stalwart-0.16` and `/data/stalwart-migrate` until 1.0 has run
cleanly for a few weeks. Rollback after the fact needs the state
directory.

### Rolling back the migration

**Before 1.0 has accepted any mail** (the final run failed, or step 5 or 6
went wrong): the 0.16 store is untouched, so put it back.

```bash
cd /data/mailstack
docker compose rm -sf stalwart
sudo mv /data/stalwart /data/stalwart-v1          # only if step 4 ran
sudo mv /data/stalwart-0.16 /data/stalwart        # only if step 4 ran
```

One thing in the bucket did change: a completed final run overwrites
`STALWART_SPAM_CLASSIFIER_MODEL.lz4` and `STALWART_SPAM_TRAIN_DATA.lz4`
(under the bucket's key prefix) with the 1.0 format, and deletes the
cached web UI bundles `STALWART_APP_*`. 0.16 refuses to start on the 1.0
spam model. Restore the previous version of each key from versioning, or
delete the two spam keys and let 0.16 retrain:

```bash
aws --endpoint-url "$S3_ENDPOINT" s3api list-object-versions \
  --bucket "$S3_BUCKET" --prefix "${S3_KEY_PREFIX}STALWART_"
aws --endpoint-url "$S3_ENDPOINT" s3api copy-object \
  --bucket "$S3_BUCKET" --key <key> --copy-source "$S3_BUCKET/<key>?versionId=<id>"
```

Then deploy `master` (still on `v0.16`, no store guard) with the
playbook.

**After 1.0 has served mail**, the 0.16 store is stale and is not a
fallback: 1.0's blob cleanup deletes bodies it no longer references,
including ones the 0.16 store still points to. The way back is
`stalwart-migrate rollback`, which copies the 1.0 store into a new, empty
0.16 store, using `/data/stalwart-migrate` as its record of the forward
run. Drain the outbound queue first (queue and tasks stay with 1.0), and
expect anything that only exists in 1.0 to be dropped. Follow
[Stalwart's rollback guide](https://stalw.art/docs/migration/upgrade-0-16/rollback/)
for the exact invocation; with a shared bucket it needs
`--allow-shared-deletes` or `--keep-v1-spam-model`.

### Afterwards

Once 1.0 has proved itself:

```bash
sudo rm -rf /data/stalwart-0.16 /data/stalwart-migrate
```

and remove `docker-compose.migrate.yml.j2`, the `stalwart_migrate_version`
default, and this section. In the "Prepare the Stalwart 0.16 to 1.0
migration" block of `roles/mailstack/tasks/main.yml`, drop the setup
tasks but keep the final `fail`: a store without the marker should still
stop the playbook rather than be handed to a server that cannot read it.

## Reading the logs

Stalwart logs to stdout because the plan configures a `Stdout` tracer. This
is not the default: Stalwart's out-of-the-box tracer writes to a file
nowhere reads inside the container, which makes `docker logs` look empty
and every failure invisible. If you ever find `docker compose logs stalwart`
silent, that tracer is the first thing to check.

```bash
cd /data/mailstack
docker compose logs -f stalwart              # follow
docker compose logs --since 1h stalwart      # last hour
docker compose logs --tail 200 caddy
docker compose logs bulwark
docker compose logs --since 24h stalwart | grep -i -E 'error|warn|fail'
```

Retention is 10 MB x 3 files per container (`/etc/docker/daemon.json`,
set by `roles/docker`). On a busy or noisy day that can be less than 24
hours. Raise it there and re-run the playbook if you need more history.

### Turning up the verbosity

The tracer level is `info`. For a debugging session, raise it in the admin
console under the tracing settings and put it back when you are done. The
next playbook run reconciles it to `info` regardless, which is a safety net
rather than a nuisance.

To change it persistently, edit the `Tracer` block in
`roles/mailstack/templates/plan.ndjson.j2` and re-run the playbook.

Be careful with `trace`: it logs message envelopes and protocol exchanges,
so the output contains recipient addresses and can contain credentials.
Do not paste it into an issue tracker without reading it first.

### Host-level logs

```bash
journalctl -u docker --since today
journalctl -u tailscaled --since today
sudo cat /var/log/unattended-upgrades/unattended-upgrades.log
sudo grep UFW /var/log/syslog | tail -50
sudo tail -5 /var/log/apt/history.log     # what the last auto-patch did
```

## Diagnosing mail delivery

Work out which half is broken before anything else, because the checks
have almost nothing in common.

- **Outbound**: your users send, recipients never receive.
- **Inbound**: senders get a bounce or a timeout, or nothing arrives.

Then check the queue and the log for the specific message. Stalwart's admin
console at `https://<mail_hostname>/admin` has the queue views, and the
queue entry carries the last error and the next retry time, which is
usually the answer on its own.

`stalwart-cli describe` with no arguments lists every object type the
server exposes. That is the authoritative list for your version, since it
moves between releases.

### Outbound mail is not arriving

Most outbound failures are one of four things: port 25 blocked by the
provider, a missing or wrong PTR record, broken SPF/DKIM/DMARC, or the IP
sitting on a blocklist.

**Is outbound 25 actually open?** Many providers block it silently on new
instances, OVH among them. From the host:

```bash
timeout 5 bash -c 'exec 3<>/dev/tcp/alt1.aspmx.l.google.com/25; head -1 <&3'
```

A `220` banner means open. A timeout with no output means blocked, and it
is a support ticket, not a configuration problem.

**Does the PTR match?** From your own machine:

```bash
dig -x <vps-public-ip> +short     # must be exactly <mail_hostname>.
```

Set it in your VPS provider's control panel. Nothing in this repo can.

**Are the DNS records published?** Stalwart manages the zone through the
Cloudflare token, so these should exist without you touching Cloudflare:

```bash
dig +short MX  example.com
dig +short TXT example.com                      # SPF
dig +short TXT _dmarc.example.com
dig +short TXT _mta-sts.example.com
```

In the console, under the domain's management view, the domain should
report no outstanding records. If it reports missing ones, the Cloudflare
token is the suspect.

**Is DKIM signing?** Selectors follow the template
`v{version}-{algorithm}-{date-%Y%m%d}` and rotate every 90 days, with
Stalwart publishing each new one itself. Read the current selector off an
outbound message's `DKIM-Signature` header, then:

```bash
dig +short TXT <selector>._domainkey.example.com
```

An empty answer for a selector that is actively signing means the
publishing step failed, which again points at the Cloudflare token.

**End-to-end check.** Send a message to a mailbox you control at a large
provider and read the received headers for `spf=pass`, `dkim=pass` and
`dmarc=pass`. Any of the public deliverability checkers work too.

**Blocklists.** A new VPS IP may arrive with history. Check the IP against
the major DNSBLs and use the delisting forms if it is listed. Nothing on
the host will tell you this, the symptom is only that some recipients
reject and others do not.

### Inbound mail is not arriving

**Do the public ports answer?** From your own machine, not the host:

```bash
openssl s_client -starttls smtp -connect mail.example.com:25 -servername mail.example.com
openssl s_client -connect mail.example.com:993 -servername mail.example.com
```

If these hang, work down the layers:

```bash
# on the host
docker compose ps                  # is stalwart up, are 25/465/993 published
sudo ufw status verbose            # public_tcp_ports should be allowed
docker compose logs --since 10m stalwart | grep -i listen
```

Two failure modes look identical from outside and are worth ruling out
early:

- **The listener does not exist.** A server provisioned through recovery
  mode has only the management listener until the plan's
  `stalwart_listeners` are applied. Docker publishes the port, nothing is
  behind it, and the connection is refused exactly as if the firewall were
  blocking it.
- **Cloudflare is proxying `mail.<domain>`.** The orange cloud carries HTTP
  and HTTPS only, so the web console on that hostname keeps working
  perfectly while IMAPS and submission fail. Set that record to DNS only.

**Does the MX point at the right place?**

```bash
dig +short MX example.com
dig +short A  mail.example.com      # must be the VPS public IP
```

**Was the message rejected on purpose?** Search the log for the sender or
the recipient. Stalwart logs its own reason for a rejection, which beats
guessing from the sender's bounce text.

```bash
docker compose logs --since 2h stalwart | grep -i <sender@domain>
```

### Transactions are slow rather than failing

MXToolbox flags an SMTP transaction taking more than about five seconds.
Slow is worth treating as a real fault: senders with short timeouts give
up, and a server that is slow under no load is usually slow because it is
waiting on something (DNS, a blocklist, a directory lookup) rather than
because it is busy.

`tools/smtp_probe.py` measures this continuously and splits the
transaction into phases, which is what turns "six seconds" into a cause.
Run it from outside the server, not on the host:

```bash
./tools/smtp_probe.py mail.example.com --rcpt you@example.com --interval 60
```

Ctrl-C prints a per-phase summary and names the slowest phase. See
[tools/README.md](tools/README.md) for how to read it, including which
phases point at DNS resolution inside the container.

### It worked yesterday and stopped today

In rough order of likelihood:

1. **The Cloudflare token expired or was revoked.** It is the single
   credential this deployment leans on hardest, and it fails quietly. See
   the callout below.
2. **The certificate did not renew.** Check the expiry:
   ```bash
   echo | openssl s_client -connect mail.example.com:993 -servername mail.example.com 2>/dev/null \
     | openssl x509 -noout -subject -dates
   ```
   Renewal is ACME DNS-01 through that same Cloudflare token, so a failure
   here usually is failure 1.
3. **The IP got blocklisted**, typically after a compromised account or a
   forwarding loop.
4. **Stalwart banned something it should not have.** See below.
5. **Disk full.** RocksDB stops writing and the symptoms are bizarre.
   `df -h /` first, always.

### Webmail or the admin console is broken

- **"The server is blocking cross-origin requests"**: the plan's `Http`
  object did not apply. Bulwark's JMAP calls go from `webmail.<domain>` to
  `mail.<domain>`, and Stalwart sends no CORS headers without
  `usePermissiveCors`. Re-run the playbook and check the apply output.
- **TOTP login never completes, or "SESSION_SECRET not configured"** in
  Bulwark's logs: `bulwark_session_secret` is unset in `group_vars/all.yml`.
  Changing it invalidates every remembered session.
- **A refresh logs the user out, with "OAuth token endpoint not found"** in
  Bulwark's logs: its OAuth discovery rejected the private address that
  `mail.<domain>` intentionally resolves to inside the Docker network.
  `OAUTH_ALLOW_PRIVATE_ENDPOINTS=true` in the compose template permits this
  trusted split-DNS route; it does not enable OAuth-only login.
- **502 from Caddy**: the internal 8080 listener came up with TLS enabled,
  so it resets Caddy's plaintext request. `use_tls: false` on that listener
  in `stalwart_listeners` is what prevents it.
- **"invalid proxy header" in Stalwart's log**: the PROXY protocol is
  enabled on one side only. `Caddyfile.j2`'s `proxy_protocol v2` and
  `SystemSettings.proxyTrustedNetworks` in the plan have to move together.

### Bans and locking yourself out

Stalwart bans aggressively. A single scanner probe for a path like
`/wp-login.php` is enough, and if it attributes that traffic to Caddy
rather than the real client, it takes the console and webmail down with it.
That is what `proxyTrustedNetworks` and the `AllowedIp` entries exist to
prevent.

Inspect and clear bans (from your machine, `mise` provides the CLI):

```bash
export STALWART_URL=https://mail.example.com
export STALWART_USER=admin@example.com
stalwart-cli query BlockedIp
stalwart-cli delete BlockedIp --ids <id>
```

You are prompted for the password. To exempt an address permanently, add
it as an `AllowedIp` in `plan.ndjson.j2` rather than clearing the ban each
time.

If the console itself is unreachable because of a ban, the same commands
work against the internal listener from the host:

```bash
cd /data/mailstack
sudo docker run --rm --network mailstack_internal \
  -e STALWART_URL=http://stalwart:8080 \
  -e STALWART_USER=admin@example.com -e STALWART_PASSWORD='...' \
  ghcr.io/stalwartlabs/cli:1.0 query BlockedIp
```

## The Cloudflare token is the thing to watch

One token does three jobs: publishing DNS records, answering the ACME
DNS-01 challenge for certificate renewal, and publishing rotated DKIM
selectors. If it expires or is revoked, nothing breaks immediately. The
certificate keeps working until it expires, DKIM keeps signing with the
current selector until it rotates. Weeks later, mail starts failing
authentication and TLS starts failing, together, for no apparent reason.

If you create it with an expiry, put the date in a calendar. To rotate:
update `CLOUDFLARE_API_TOKEN` in `.env` and re-run the playbook. The
`DnsServer` upsert matches on description and updates the secret in place.

## Tailscale node expiry is the other one

Tailnets expire node keys periodically by default. This host's only SSH
path is the tailnet, so an expired node key locks you out to the provider
console. Disable key expiry for this machine in the Tailscale admin
console, and confirm occasionally:

```bash
tailscale status
```

## Backups, not built yet

There is no backup automation in this repo. Until there is, this is the
manual version.

What actually needs backing up:

- **`/data/stalwart/data`**, the RocksDB store. Metadata, mailbox
  structure, indexes, the TLS certificate, the ACME account key and the
  DKIM private keys. Message bodies are in S3, but this directory is what
  maps them to mailboxes. Without it, the bucket is unreadable noise.
- **The S3 bucket**, which is your provider's problem. Versioning is on,
  which is what makes a RocksDB copy and the bucket line up: Stalwart's
  daily blob cleanup (04:00) deletes blobs nothing references any more,
  and without versioning a store restored from yesterday would point at
  bodies that are already gone. Confirm how long noncurrent versions are
  kept, since that is the real limit on how old a usable backup can be.
- **`ansible/inventory/group_vars/all.yml` and `.env`.** Both gitignored,
  both carrying every password in this deployment. A password manager is
  the right place, not a second copy on the same VPS.

`/data/stalwart/etc/config.json` does not need a backup, the playbook
re-renders it.

Cold copy, which is the only kind worth trusting for RocksDB:

```bash
cd /data/mailstack
docker compose stop stalwart
sudo tar -C /data/stalwart -czf /root/stalwart-$(date +%F).tar.gz data etc
docker compose start stalwart
```

Then get the tarball off the box. A tar of a running RocksDB can be
internally inconsistent in ways that only show up when you restore it,
which is the worst possible time to find out.

To restore onto a rebuilt host: run the playbooks as normal, then stop the
stack, replace `/data/stalwart/data` with the backup (preserving
`2000:2000` ownership), and start it. Restoring keeps the existing DKIM
keys and certificate, so nothing needs republishing.

A backup only restores onto the Stalwart version that wrote it. A 0.16
tarball is not a way back from 1.0 once 1.0 has served mail; see
[Rolling back](#rolling-back-the-migration) in the migration section.

A from-scratch rebuild with no backup also works, in the sense that
Stalwart issues a new certificate and publishes new DKIM keys itself. What
it does not bring back is anyone's mail.

## Disk and housekeeping

```bash
df -h /
sudo du -sh /data/stalwart/data       # RocksDB, should stay modest
docker system df
docker image prune -f                 # untagged layers, safe
```

Avoid `docker system prune -a --volumes`. Persistent state here is in bind
mounts so it survives, but `--volumes` would remove any anonymous volume,
and an anonymous volume holding Stalwart state is precisely the failure
mode the two explicit volume mappings exist to prevent.

Full disk is worth catching before it happens: RocksDB, Docker logs and
Docker images all grow, and Stalwart's behaviour once writes start failing
is confusing rather than obvious.

## Re-provisioning from scratch

`roles/mailstack` decides between two paths on the presence of
`/data/mailstack/.provisioned`:

- **Absent**: brings Stalwart up in recovery mode with a temporary admin
  credential, applies `provision.ndjson` (plan plus seed), writes the
  marker, and restarts clean.
- **Present**: applies the plan alone to the running server as
  `stalwart_admin_account`.

Deleting the marker on a live server drops the stack into recovery mode
briefly, which takes mail services down for the duration, and re-applies
the seed. That resets every seeded account to `stalwart_accounts`
(password, role, and no app passwords or TOTP) and registers yet another
ACME provider, so do not do it to a server people are using. A genuine wipe means stopping the stack, removing
`/data/stalwart/data`, deleting the marker, and re-running the playbook.
That destroys every mailbox mapping. The S3 bodies remain, orphaned.

## Quick reference

| Symptom | Likely cause |
|---|---|
| `docker compose logs stalwart` is empty | Stdout tracer missing, server is logging to a file nobody reads |
| Caddy returns 502 for the console | 8080 listener came up with `useTls: true` |
| "invalid proxy header", connection resets | PROXY protocol enabled on Caddy or Stalwart but not both |
| Console and webmail both die after a few hours | Caddy banned, because `proxyTrustedNetworks` is not set |
| Webmail: "blocking cross-origin requests" | `Http.usePermissiveCors` not applied |
| Ports 993 and 465 time out, console fine | Cloudflare proxy (orange cloud) on `mail.<domain>` |
| Connection refused on a published port | Listener missing from `stalwart_listeners` |
| Outbound mail silently never delivered | Provider blocking egress on 25 |
| Some recipients reject, others accept | Missing PTR, or the IP is on a blocklist |
| Bind mounts appear empty inside containers | Docker installed as a snap (the playbook removes it and asserts) |
| Config change in the console reverted itself | The plan reconciled it, put it in `group_vars` instead |
