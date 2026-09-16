# tools

Standalone troubleshooting scripts for a deployment built by this repo.

Everything here is stdlib-only Python with no dependencies and no
virtualenv, so a script can be copied to any box with a recent Python and
run as-is. Nothing in `ansible/` installs or depends on these: they are
diagnostic instruments, not part of the deployment.

| Script | What it does |
|---|---|
| `smtp_probe.py` | Loops an SMTP transaction against a mail host and times every phase separately |

## smtp_probe.py

The self-hosted equivalent of MXToolbox's
[SMTP Transaction Time](https://mxtoolbox.com/problem/smtp/smtp-transaction-time)
check. It walks a full envelope, timing each phase, and repeats on an
interval so you can watch the number move instead of refreshing a web page.

It never sends a message. The conversation runs to RCPT TO, then RSET and
QUIT. DATA is never issued.

```bash
./tools/smtp_probe.py mail.example.com --rcpt you@example.com
```

Useful flags: `--interval` (default 60s), `--count`, `--from`, `--helo`,
`--port` with `--tls implicit` for 465, `--ipv4` / `--ipv6`, `--slow-after`
(default 5s, roughly where MXToolbox starts complaining). Ctrl-C prints a
per-phase summary, which is the part worth reading.

### Run it from outside the server

This matters more than anything else about the tool. Most of the time in a
slow transaction is spent on work the server does *about the client*:
reverse DNS on the connecting IP, DNS blocklist lookups, SPF on the sender
domain. Probing from the mail host itself, or from inside the tailnet,
skips most of that and will look fast while the outside world sees six
seconds.

Your numbers will not match MXToolbox exactly either, for the same reason.
Part of what is being measured depends on which IP is connecting. Trends
over time and the split between phases are what this tool gives you.

### Reading the phases

| Phase | Covers | A slow number here means |
|---|---|---|
| `dns` | resolving the target from the probe machine | your resolver, not the mail server |
| `tcp` | the TCP handshake | network latency or a loaded host |
| `banner` | connect to the 220 greeting | connect-time work on the server: reverse DNS of your IP, blocklist lookups, tarpitting |
| `ehlo` | the EHLO round trip | rarely anything, the server has already done its thinking |
| `tls` | STARTTLS plus handshake and certificate verification | certificate chain or CPU on a small VPS |
| `ehlo2` | the second EHLO, required after STARTTLS | as `ehlo` |
| `mail` | MAIL FROM | sender-side checks: SPF lookups on the sender domain |
| `rcpt` | RCPT TO | the recipient directory lookup and recipient policy |
| `rset` / `quit` | teardown | almost never the problem |

The summary names the slowest phase by median and says where to look.

For this deployment specifically, the phases worth suspecting first when
`banner` or `mail` dominate are DNS-shaped. Stalwart resolves names from
inside its container, over a Docker network that has IPv6 enabled. If the
host has no working IPv6 egress, lookups can stall on the v6 attempt and
fall back to v4 only after a timeout, which lands as several seconds in
exactly those two phases. Worth confirming before going anywhere near the
mail configuration:

```bash
docker compose exec stalwart nslookup example.com     # if the image has it
docker compose logs --since 10m stalwart | grep -i -E 'dns|resolve|timeout'
```

### Things to know before you leave it running

- **`--from` affects the result.** The server looks up SPF for the sender
  domain, so a domain with a long SPF chain adds real time. Use a domain
  you control and keep it constant across runs, or the numbers are not
  comparable.
- **`--helo` should resolve.** The default is your machine's FQDN. A name
  that does not resolve earns a penalty on some servers, which shows up as
  time you did not mean to measure.
- **`--rcpt` should be a real mailbox** on the target. A nonexistent
  recipient can be rejected before the work you are trying to measure
  happens.
- **You may get banned.** Stalwart bans aggressively, and a probe hitting
  it every 30 seconds from one address is unusual traffic. If probes start
  failing at the TCP or banner phase after a while, check `BlockedIp` and
  consider adding the probe's address as an `AllowedIp` in
  `plan.ndjson.j2`. See
  [MAINTENANCE.md](../MAINTENANCE.md#bans-and-locking-yourself-out).
- **Point it at your own server.** A transaction every 30 seconds against
  someone else's mail host is abuse, even without DATA.

### Pairing it with the server's own logs

The probe says which phase is slow. Stalwart's log says why. Run both and
line up the timestamps:

```bash
# on the mail host
docker compose logs -f --since 1m stalwart
```

Raising the tracer to `debug` for the duration of a probing session is
usually what turns "banner takes 5 seconds" into a specific lookup. See
[MAINTENANCE.md](../MAINTENANCE.md#turning-up-the-verbosity).
