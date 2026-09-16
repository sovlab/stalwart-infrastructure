#!/usr/bin/env python3
"""Measure SMTP transaction time against a mail server, on a loop.

This is the self-hosted version of MXToolbox's "SMTP Transaction Time"
check: it walks a full SMTP envelope and times every phase separately, so
a slow transaction points at a cause instead of just a number.

It never sends a message. The conversation stops at RCPT TO, then RSET and
QUIT, which is the same shape MXToolbox uses.

Run it from OUTSIDE the mail server (a laptop or a second VPS). Most of the
cost in a slow transaction comes from work the server does on the
connecting IP and the sender domain: reverse DNS, SPF, DNS blocklists.
Probing from the mail host itself skips all of that and will look fast
while the outside world sees six seconds.

Your numbers will not match MXToolbox exactly, for the same reason: part of
what is being measured depends on which IP is connecting and how quickly
its rDNS resolves. Trends and the per-phase split are what this is for.

Typical use:

    ./tools/smtp_probe.py mail.example.com --rcpt you@example.com

    ./tools/smtp_probe.py mail.example.com --rcpt you@example.com \\
        --interval 30 --from probe@example.com --helo probe.example.com

Stdlib only, no virtualenv needed. Copy it to any box with Python 3.11+.

Ctrl-C prints a summary of every phase, which is the part worth reading.
"""

from __future__ import annotations

import argparse
import re
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Phases in conversation order. Every probe reports the ones it reached,
# which is also how a failed probe says where it died.
PHASES = [
    "dns",      # name resolution
    "tcp",      # TCP handshake
    "banner",   # waiting for the 220 greeting: where tarpitting shows up
    "ehlo",
    "tls",      # STARTTLS command plus the TLS handshake and cert verify
    "ehlo2",    # the second EHLO, required after STARTTLS
    "mail",     # MAIL FROM: sender checks, SPF lookups
    "rcpt",     # RCPT TO: directory lookup, recipient policy
    "rset",
    "quit",
]


class ProbeError(Exception):
    """A probe that could not complete, carrying the phase it failed in."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message


@dataclass
class Probe:
    started: datetime
    ok: bool
    total: float
    phases: dict[str, float] = field(default_factory=dict)
    peer: str | None = None
    rcpt_code: int | None = None
    rcpt_text: str = ""
    failed_phase: str | None = None
    error: str | None = None


class SMTPConn:
    """Just enough SMTP to time a transaction.

    smtplib would do most of this, but it folds the connect, the banner and
    the first EHLO into one call, and the banner wait is exactly the phase
    worth isolating: a server that tarpits or does a slow rDNS lookup on
    connect spends its seconds there and nowhere else.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def _readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read_response(self) -> tuple[int, str]:
        """Read one complete reply, following multiline continuations."""
        lines: list[str] = []
        while True:
            raw = self._readline().decode("utf-8", "replace")
            lines.append(raw)
            # "250-STARTTLS" continues, "250 OK" ends. Anything that is not
            # a 3-digit code followed by '-' or ' ' is a protocol violation.
            if len(raw) < 4 or not raw[:3].isdigit():
                raise ProbeError("protocol", f"malformed reply: {raw!r}")
            if raw[3] != "-":
                break
        return int(lines[-1][:3]), "\n".join(lines)

    def command(self, text: str) -> tuple[int, str]:
        self.sock.sendall(text.encode("utf-8") + b"\r\n")
        return self.read_response()

    def starttls(self, ctx: ssl.SSLContext, server_hostname: str) -> None:
        code, reply = self.command("STARTTLS")
        if code != 220:
            raise ProbeError("tls", f"STARTTLS refused: {reply.strip()}")
        if self.buf:
            # Data sitting in the buffer after the 220 would be pipelined
            # plaintext arriving before the handshake. Nothing legitimate
            # does that, and reading it post-wrap would desynchronise us.
            raise ProbeError("tls", "unexpected data before the TLS handshake")
        self.sock = ctx.wrap_socket(self.sock, server_hostname=server_hostname)


def resolve(host: str, port: int, family: int, timeout: float) -> tuple[tuple, float]:
    """Resolve once per probe, timed, so slow DNS on this side is visible.

    getaddrinfo takes no timeout and blocks in C, so a stalled resolver on
    the probe machine would hang the loop indefinitely with no output. It
    runs on a daemon thread instead: if it overruns, the probe is recorded
    as a DNS failure and the abandoned thread dies with the process.
    """
    result: list = []
    error: list[BaseException] = []

    def lookup() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, family, socket.SOCK_STREAM))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            error.append(exc)

    start = time.perf_counter()
    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    elapsed = time.perf_counter() - start

    if worker.is_alive():
        raise ProbeError("dns", f"resolution of {host} timed out after {timeout:g}s")
    if error:
        raise ProbeError("dns", str(error[0]))
    if not result or not result[0]:
        raise ProbeError("dns", "no addresses returned")
    return result[0][0], elapsed


def tls_context(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        # Certificate verification is part of the handshake and therefore
        # part of the measurement. Turning it off is for probing a host
        # whose certificate is not valid yet, not for routine use.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def run_probe(args: argparse.Namespace) -> Probe:
    started = datetime.now(timezone.utc)
    phases: dict[str, float] = {}
    peer: str | None = None
    overall = time.perf_counter()

    def mark(phase: str, since: float) -> float:
        phases[phase] = time.perf_counter() - since
        return time.perf_counter()

    try:
        info, dns_elapsed = resolve(args.host, args.port, args.family, args.timeout)
        phases["dns"] = dns_elapsed
        family, socktype, proto, _canon, sockaddr = info
        peer = sockaddr[0]

        t = time.perf_counter()
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(args.timeout)
        try:
            sock.connect(sockaddr)
        except OSError as exc:
            raise ProbeError("tcp", str(exc)) from exc
        t = mark("tcp", t)

        with sock:
            ctx = tls_context(args.insecure)
            conn = SMTPConn(sock)

            if args.tls == "implicit":
                # Port 465: the handshake happens before the banner, so it
                # is timed as "tls" and the banner phase starts after it.
                try:
                    conn.sock = ctx.wrap_socket(sock, server_hostname=args.host)
                except (ssl.SSLError, OSError) as exc:
                    raise ProbeError("tls", str(exc)) from exc
                t = mark("tls", t)

            code, reply = conn.read_response()
            if code != 220:
                raise ProbeError("banner", f"greeting was {code}: {reply.strip()}")
            t = mark("banner", t)

            code, reply = conn.command(f"EHLO {args.helo}")
            if code != 250:
                raise ProbeError("ehlo", f"EHLO rejected: {reply.strip()}")
            t = mark("ehlo", t)

            if args.tls == "starttls":
                if "STARTTLS" not in reply.upper():
                    raise ProbeError("tls", "server did not advertise STARTTLS")
                try:
                    conn.starttls(ctx, args.host)
                except (ssl.SSLError, OSError) as exc:
                    raise ProbeError("tls", str(exc)) from exc
                t = mark("tls", t)

                # RFC 3207: everything learned before the handshake is
                # discarded, so the EHLO is repeated. It is timed
                # separately because it runs over the encrypted channel.
                code, reply = conn.command(f"EHLO {args.helo}")
                if code != 250:
                    raise ProbeError("ehlo2", f"EHLO rejected after STARTTLS: {reply.strip()}")
                t = mark("ehlo2", t)

            code, reply = conn.command(f"MAIL FROM:<{args.mail_from}>")
            if code != 250:
                raise ProbeError("mail", f"MAIL FROM rejected: {reply.strip()}")
            t = mark("mail", t)

            rcpt_code, rcpt_reply = conn.command(f"RCPT TO:<{args.rcpt}>")
            t = mark("rcpt", t)

            # RSET before QUIT so the server drops the envelope it has been
            # building rather than holding it until the connection closes.
            # Nothing was ever going to be sent: DATA is never issued.
            conn.command("RSET")
            t = mark("rset", t)

            conn.command("QUIT")
            mark("quit", t)

        return Probe(
            started=started,
            ok=True,
            total=time.perf_counter() - overall,
            phases=phases,
            peer=peer,
            rcpt_code=rcpt_code,
            rcpt_text=rcpt_reply.strip().replace("\n", " | "),
        )

    except ProbeError as exc:
        return Probe(
            started=started,
            ok=False,
            total=time.perf_counter() - overall,
            phases=phases,
            peer=peer,
            failed_phase=exc.phase,
            error=exc.message,
        )
    except (OSError, ConnectionError) as exc:
        # A timeout or a reset mid-conversation: blame the phase that was
        # running, which is the one after the last phase recorded.
        done = [p for p in PHASES if p in phases]
        index = PHASES.index(done[-1]) + 1 if done else 0
        current = PHASES[index] if index < len(PHASES) else "teardown"
        return Probe(
            started=started,
            ok=False,
            total=time.perf_counter() - overall,
            phases=phases,
            peer=peer,
            failed_phase=current,
            error=f"{type(exc).__name__}: {exc}",
        )


def format_probe(probe: Probe, slow_after: float) -> str:
    stamp = probe.started.astimezone().strftime("%H:%M:%S")
    breakdown = "  ".join(
        f"{name} {probe.phases[name]:.2f}" for name in PHASES if name in probe.phases
    )

    if not probe.ok:
        return (
            f"{stamp}  FAIL  {probe.total:6.2f}s  in {probe.failed_phase}: "
            f"{probe.error}\n          partial: {breakdown or 'nothing'}"
        )

    flag = "SLOW" if probe.total >= slow_after else "ok  "
    line = f"{stamp}  {flag}  {probe.total:6.2f}s  {breakdown}"
    if probe.rcpt_code is not None and probe.rcpt_code not in (250, 251):
        # Not a failure of the measurement, but the recipient was refused,
        # which means the phases after it measured a rejection path.
        line += f"\n          RCPT {probe.rcpt_code}: {probe.rcpt_text}"
    return line


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. statistics.quantiles needs n >= 2 and
    interpolates, which is misleading on the handful of samples you have in
    the first few minutes of a run."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100 * len(ordered))))
    return ordered[rank - 1]


def print_summary(probes: list[Probe]) -> None:
    if not probes:
        print("\nNo probes completed.")
        return

    ok = [p for p in probes if p.ok]
    failed = len(probes) - len(ok)
    print(f"\n{len(probes)} probes, {len(ok)} ok, {failed} failed")

    if not ok:
        for probe in probes[-5:]:
            print(f"  last error in {probe.failed_phase}: {probe.error}")
        return

    totals = [p.total for p in ok]
    print(
        f"total    n={len(totals):<5} min {min(totals):6.2f}  p50 {percentile(totals, 50):6.2f}"
        f"  p95 {percentile(totals, 95):6.2f}  max {max(totals):6.2f}"
    )
    print("per phase:")
    for name in PHASES:
        values = [p.phases[name] for p in ok if name in p.phases]
        if not values:
            continue
        print(
            f"  {name:<8} n={len(values):<5} min {min(values):6.2f}"
            f"  p50 {percentile(values, 50):6.2f}  p95 {percentile(values, 95):6.2f}"
            f"  max {max(values):6.2f}"
        )

    # The phase with the largest median is the one to go and look at. Said
    # out loud because reading it off the table takes a moment.
    medians = {
        name: percentile([p.phases[name] for p in ok if name in p.phases], 50)
        for name in PHASES
        if any(name in p.phases for p in ok)
    }
    worst = max(medians, key=medians.get)
    print(f"\nSlowest phase by median: {worst} ({medians[worst]:.2f}s)")
    if worst == "banner":
        print("  The server is slow to greet. Look at connection-time work:")
        print("  reverse DNS of the probe's IP, DNS blocklist lookups, tarpitting.")
    elif worst in {"mail", "rcpt"}:
        print("  The server is slow inside the envelope. Look at SPF/DMARC")
        print("  lookups on the sender domain, and the directory lookup for")
        print("  the recipient. DNS resolution inside the container is the")
        print("  usual culprit.")
    elif worst == "tls":
        print("  The TLS handshake dominates. Check the certificate chain")
        print("  being served and whether OCSP or chain fetching is involved.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Loop an SMTP transaction and time each phase.",
        epilog="No message is ever sent: the conversation stops at RCPT TO.",
    )
    parser.add_argument("host", help="mail host to probe, e.g. mail.example.com")
    parser.add_argument(
        "--rcpt",
        required=True,
        help="RCPT TO address. Use a real mailbox on the target so the "
        "recipient lookup actually runs.",
    )
    parser.add_argument(
        "--from",
        dest="mail_from",
        help="MAIL FROM address. Defaults to probe@<helo>. Its domain's SPF "
        "record is looked up by the server, so this affects the timing.",
    )
    parser.add_argument(
        "--helo",
        default=socket.getfqdn(),
        help="name sent in EHLO (default: this machine's FQDN). A name that "
        "does not resolve can earn you a penalty on some servers.",
    )
    parser.add_argument("--port", type=int, default=25, help="default: 25")
    parser.add_argument(
        "--tls",
        choices=["starttls", "implicit", "none"],
        default="starttls",
        help="starttls for port 25/587, implicit for 465 (default: starttls)",
    )
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between probes (default: 60)"
    )
    parser.add_argument(
        "--count", type=int, default=0, help="number of probes, 0 for unlimited (default: 0)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="socket timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "--slow-after",
        type=float,
        default=5.0,
        help="mark a probe SLOW at or above this many seconds (default: 5, "
        "which is roughly where MXToolbox starts complaining)",
    )
    parser.add_argument(
        "--ipv4", action="store_true", help="force IPv4 (timings differ per family)"
    )
    parser.add_argument("--ipv6", action="store_true", help="force IPv6")
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS certificate verification"
    )
    parser.add_argument(
        "--summary-every",
        type=int,
        default=10,
        help="print a running summary every N probes, 0 to disable (default: 10)",
    )

    args = parser.parse_args(argv)

    if args.ipv4 and args.ipv6:
        parser.error("--ipv4 and --ipv6 are mutually exclusive")
    args.family = socket.AF_INET if args.ipv4 else socket.AF_INET6 if args.ipv6 else socket.AF_UNSPEC

    if args.port == 465 and args.tls == "starttls":
        # Port 465 is implicit TLS. Sending EHLO in the clear there just
        # hangs until the timeout, which looks like a firewall problem.
        parser.error("port 465 needs --tls implicit")

    if not args.mail_from:
        args.mail_from = f"probe@{args.helo}"

    if not re.fullmatch(r"[^@\s]+@[^@\s]+", args.rcpt):
        parser.error(f"--rcpt does not look like an address: {args.rcpt}")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    print(f"probing {args.host}:{args.port} ({args.tls})")
    print(f"  EHLO {args.helo}   MAIL FROM <{args.mail_from}>   RCPT TO <{args.rcpt}>")
    print(f"  every {args.interval:g}s, timeout {args.timeout:g}s, Ctrl-C for the summary\n")

    probes: list[Probe] = []
    last_peer: str | None = None
    try:
        while args.count == 0 or len(probes) < args.count:
            started = time.perf_counter()
            probe = run_probe(args)
            probes.append(probe)
            print(format_probe(probe, args.slow_after), flush=True)

            # Which address answered, noted on the first probe and whenever
            # it changes. A host with both A and AAAA records can behave
            # very differently over each, and comparing probes that hit
            # different addresses is how you end up chasing a ghost.
            if probe.peer and probe.peer != last_peer:
                print(f"          via {probe.peer}", flush=True)
                last_peer = probe.peer

            if args.summary_every and len(probes) % args.summary_every == 0:
                ok = [p.total for p in probes if p.ok]
                if ok:
                    print(
                        f"          -- {len(probes)} probes, p50 {percentile(ok, 50):.2f}s,"
                        f" p95 {percentile(ok, 95):.2f}s, {len(probes) - len(ok)} failed",
                        flush=True,
                    )

            if args.count and len(probes) >= args.count:
                break

            # Pace from the start of the probe, not the end, so a slow
            # transaction does not push the whole schedule out.
            time.sleep(max(0.0, args.interval - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        pass

    print_summary(probes)
    return 0 if any(p.ok for p in probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
