# Mail Sovereignty: Stalwart + Bulwark on Your Own VPS

Ansible playbooks that turn a bare Ubuntu VPS into a self-hosted mail
server: [Stalwart](https://stalw.art/) for SMTP/IMAP/JMAP, [Bulwark](https://bulwarkmail.org/)
for webmail, Docker to run them, Tailscale to lock down management access,
and unattended security patching so the box doesn't rot.

## Why

Most personal and small-business mail today sits with a handful of large
providers, governed by the laws of wherever those providers are based
rather than where the mailbox owner or their data actually lives. Keeping
mail with a local or regional provider, under your own jurisdiction, is a
choice anyone should be able to make, and an easy one to make real: mail
is a solved problem, the software is mature and free,
and running it yourself is within reach of one competent afternoon plus a
cheap VPS. This project is that afternoon, written down and made repeatable.

It targets a single VPS to start, sized to grow: message data lives in
PostgreSQL from day one (not Stalwart's embedded RocksDB store), specifically
so a second VPS can be added later to split roles (e.g. SMTP on one host,
IMAP/JMAP on another) without a data migration.

## Status

Working today:

- Base OS hardening, unattended security/critical patches with scheduled
  reboots
- Tailscale join, with SSH subsequently restricted to the tailnet only
- Docker Engine, with its storage moved onto a dedicated data volume
- A firewall (ufw) that actually applies to Docker-published ports, which
  vanilla ufw does not (see [Docker doesn't get to bypass the firewall](#security-model) below)
- The mail stack: PostgreSQL, Stalwart, Caddy, and Bulwark as one Docker
  Compose project, with a timer that keeps Stalwart's SMTP/IMAP certs in
  sync with whatever Caddy has renewed

That gets containers running with a real TLS chain, but Stalwart still
needs a short, one-time, click-through setup (admin account, CORS,
DKIM) — see [step 8](#8-complete-stalwarts-one-time-setup) below. This
repo doesn't and can't automate that part.

Not yet built:

- Automated DNS record management (SPF/DKIM/DMARC/MX) via the Cloudflare API
  — for now you publish the records Stalwart's WebUI shows you, by hand
- Backups

## Architecture

```
                              Internet
                                 │
                    25 / 465 / 993 / 80 / 443
                                 │
                    ┌────────────▼─────────────┐
                    │        Ubuntu VPS         │
                    │                           │
                    │  Caddy (:80, :443)        │──▶ webmail.example.com  (→ Bulwark)
                    │       │                   │──▶ mail.example.com      (→ Stalwart :8080, plain HTTP -
                    │       ▼                   │      JMAP and the /account webadmin console both here)
                    │  Bulwark ── (browser JMAP calls go straight to Stalwart, not through Bulwark's server)
                    │                           │
                    │  Stalwart ────────────────┼──▶ PostgreSQL
                    │  (SMTP/IMAPS: 25/465/993, │
                    │   direct on host ports,   │
                    │   TLS synced from Caddy)  │
                    └─────────────┬─────────────┘
                                  │ tailscale0 (100.x.x.x)
                                  │ SSH only, no public access
                             Your machine
```

Everything runs on one VPS today. PostgreSQL as the data store is what
keeps a second Stalwart node (added later, pointed at the same database)
an option rather than a rebuild.

Bulwark's webmail runs in your browser and talks JMAP directly to
Stalwart over `mail.example.com` — Bulwark's own Next.js server isn't in
that request path, it just serves the app itself.

## Prerequisites

- An Ubuntu 26.04 LTS VPS with:
  - A public IPv4 (and ideally IPv6) address
  - A second block/data volume attached but not yet formatted (this repo
    formats and mounts it)
  - Root access via SSH key (password auth is disabled by this playbook,
    so make sure key-based root access already works before you start)
- A domain, with DNS hosted somewhere with an API (this repo assumes
  Cloudflare; adapt `group_vars/all.yml` and the future DNS role if you
  use something else)
- A [Tailscale](https://tailscale.com/) account (the free tier is enough
  for one or two hosts)
- Reverse DNS (PTR) for the VPS's IP set to your mail hostname, done in
  your VPS provider's control panel, not by this repo. Most receiving
  mail servers junk or reject mail from IPs without a matching PTR record.
- Confirmation from your VPS provider that outbound port 25 isn't
  blocked. OVH and several other providers block it by default on new
  accounts/instances and require a support ticket to open it. Check this
  before you get to the mail stack, not after.
- On your own machine: [mise](https://mise.jdx.dev/) (this repo uses it
  to pin Python, uv, and Ansible)

## Repository layout

```
ansible/
  ansible.cfg
  requirements.yml       # Ansible collections this repo depends on
  inventory/
    hosts.yml.example    # copy to hosts.yml and edit - hosts.yml is gitignored
    group_vars/
      all.yml.example    # copy to all.yml and edit - all.yml is gitignored
  playbooks/
    00-bootstrap.yml      # run ONCE, as root, over the public IP
    10-site.yml           # run repeatedly, as your admin user, over Tailscale
  roles/
    bootstrap/            # OS setup, unattended-upgrades, data volume
    tailscale/             # joins the tailnet
    lockdown/              # binds sshd to the tailnet interface only
    docker/                 # Docker Engine + compose plugin
    firewall/               # ufw + ufw-docker
    mailstack/              # PostgreSQL + Stalwart + Caddy + Bulwark, plus cert sync
```

## Getting started

### 1. Install tooling

```bash
mise install                       # Python, uv, Ansible
ansible-galaxy collection install -r ansible/requirements.yml
```

### 2. Set up Tailscale

In the Tailscale admin console:

1. Go to **Access controls** and add `tag:mailserver` under `tagOwners`
   (the playbook advertises this tag when joining; the join fails with a
   permission error if the tag doesn't already exist in your ACL policy).
2. Go to **Settings → Keys** and generate a **reusable**, **tagged**
   (`tag:mailserver`) pre-auth key.

### 3. Configure your `.env`

```bash
cp .env.example .env   # if not already present
```

Fill in:

```
TAILSCALE_AUTHKEY=tskey-auth-...
CLOUDFLARE_API_TOKEN=...   # not used yet - see Roadmap
POSTGRES_PASSWORD=...      # e.g. `openssl rand -base64 32`
STALWART_API_KEY=          # leave blank for now - see step 8
```

`mise` loads this automatically whenever you're inside the project
directory (see `mise.toml`).

### 4. Edit your configuration

Both of these are gitignored - your real values never get committed,
only the `.example` templates do:

```bash
cp ansible/inventory/group_vars/all.yml.example ansible/inventory/group_vars/all.yml
cp ansible/inventory/hosts.yml.example ansible/inventory/hosts.yml
```

`ansible/inventory/group_vars/all.yml` — your domain, hostnames, admin username, the
data volume's device name, and which ports get exposed publicly.

`ansible/inventory/hosts.yml` — under `initial`, set `ansible_host` to
your VPS's public IP. The `mailhosts` group's `ansible_host` gets filled
in after step 5.

The admin username in `inventory/hosts.yml` (`ansible_user` under
`mailhosts`) and `admin_user` in `group_vars/all.yml` must match — the
inventory isn't templated from the group var, so a mismatch there is a
common way to lock yourself out.

### 5. Bootstrap the host

```bash
cd ansible
ansible-playbook playbooks/00-bootstrap.yml
```

This runs once, as root, over the public IP. It patches and upgrades the
OS, sets up unattended security upgrades, creates your admin user,
mounts the data volume, and joins Tailscale. It prints the host's
Tailscale IPv4 address at the end.

### 6. Point the inventory at Tailscale

Copy that IP into `inventory/hosts.yml`, under `mailhosts → mail01 →
ansible_host`.

### 7. Harden and provision

```bash
ansible-playbook playbooks/10-site.yml
```

Runs over Tailscale as your admin user, every time from now on. It
restricts sshd to the tailnet interface (double-checking that the
connection it's currently running over is actually a Tailscale one
before it does — see [roles/lockdown](ansible/roles/lockdown/tasks/main.yml)
if you're curious how), installs Docker, applies the firewall, and brings
up PostgreSQL, Stalwart, Caddy, and Bulwark.

After this runs, the public IP no longer accepts SSH at all, by design.
If you get locked out, your provider's console/KVM access is the way
back in, not this repo.

Caddy needs a minute to get its first certificate from Let's Encrypt
before `https://<mail_hostname>` and `https://<webmail_hostname>` come up
— that's normal on the very first run.

### 8. Complete Stalwart's one-time setup

Stalwart doesn't have a fully declarative first-run path yet, so this
part is click-through, once:

1. `playbooks/10-site.yml` prints Stalwart's one-time bootstrap admin
   username/password near the end of its run (pulled straight from the
   container's logs). Use it to log into
   `https://<mail_hostname>/account/Management` and follow the setup
   wizard: create your permanent admin account, add your domain,
   generate a DKIM key. That temporary credential stops working once
   you do. This console is on the public hostname — see
   [Security model](#security-model) for what that does and doesn't
   mean for exposure.
2. Under network/HTTP settings, enable permissive CORS. Bulwark's
   browser JMAP calls hit `mail.<domain>` from `webmail.<domain>`, a
   different origin, and Stalwart sends no CORS headers by default.
3. Publish the DNS records the wizard shows you (MX, SPF, DKIM, DMARC)
   in Cloudflare. Not automated yet — see Roadmap.
4. Create an API key (scope it to certificate management if the WebUI
   offers that granularity; a full-admin key otherwise), put it in
   `.env` as `STALWART_API_KEY`, and re-run
   `ansible-playbook playbooks/10-site.yml`.
   This turns on the daily timer that copies Caddy's renewed certs into
   Stalwart and reloads them — without it, Stalwart keeps whatever cert
   it started with until you do this step.
5. Create your mailboxes, and add them as accounts in Bulwark
   (`https://<webmail_hostname>`, publicly reachable).

## Security model

- **SSH is Tailscale-only.** sshd's `ListenAddress` is set to the
  tailnet address, not `0.0.0.0`. Root login and password auth are both
  disabled. There is no public SSH surface to scan or brute-force.
- **Only mail and web ports are public**: SMTP (25), submission (465),
  IMAPS (993), and HTTP/HTTPS (80/443, the latter for ACME and the
  http→https redirect). 587/143/110/995/4190 are deliberately left off
  per Stalwart's own hardening guide. PostgreSQL and Stalwart's plain-HTTP
  internal listener (port 8080) are never published to the host at all —
  Caddy reaches them over the Docker-internal network only.
- **Stalwart's admin/account console is public**, at
  `https://<mail_hostname>/account/Management`, protected by Stalwart's
  own login only — there's no network-level or IP-based restriction on
  it. This is a deliberate simplicity trade-off: an earlier version of
  this repo tried to isolate it to the tailnet (a dedicated port bound
  to the Tailscale interface), but that added real complexity for a
  single-admin setup. If you want that isolation back, it's in this
  repo's git history, or see the Roadmap.
- **Unattended upgrades** apply security and ESM security patches
  automatically, with a scheduled reboot window, so patching doesn't
  depend on someone remembering to SSH in.
- **Docker doesn't get to bypass the firewall.** Docker inserts its own
  iptables/nftables rules ahead of ufw's by default, so a container
  publishing a port is reachable from the internet even while `ufw
  status` shows that port denied. This repo installs
  [ufw-docker](https://github.com/chaifeng/ufw-docker), which patches
  ufw to route Docker-forwarded traffic through ufw's own chain, so
  `ufw` is actually authoritative over what's public.

## Platform gotchas worth knowing

- **OVH's Network Firewall** (the edge anti-DDoS one, separate from the
  host firewall) is stateless and can silently drop traffic the host
  would otherwise accept. Leave it off and let the host firewall (ufw)
  do the work, unless you specifically need it.
- **Outbound port 25** is blocked by default on many providers,
  including OVH in some ranges. It's a support ticket to lift, and mail
  delivery from your box will silently fail until you do.
- **Reverse DNS (PTR)** has to be set by your provider, not by this
  repo. Without it, most large mail providers will reject or spam-bin
  your outbound mail regardless of how correctly everything else is
  configured.

## Roadmap

- Automated SPF/DKIM/DMARC/MX record management via the Cloudflare API,
  replacing the manual step in [step 8](#8-complete-stalwarts-one-time-setup)
- Backups (PostgreSQL + message blobs) to S3-compatible object storage
- A second-node runbook for splitting SMTP and IMAP/JMAP roles across
  hosts once one VPS isn't enough
- Optionally isolating the admin console to the tailnet again, properly
  this time (real HTTPS via Cloudflare DNS-01 ACME, since the console's
  hostname wouldn't be publicly routable) - dropped for now in favor of
  keeping the setup simple; see [Security model](#security-model)

## License

Not yet decided. Don't treat this as open source until a `LICENSE` file
shows up in the repo.
