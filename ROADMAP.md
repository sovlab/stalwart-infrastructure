# Roadmap

> **Status:** living document, last updated October 2026.
> This roadmap describes where we want to go, not a contract. Timelines and
> technical choices will change as we test, measure and learn. We will
> update this file when they do.

## What we are building

A sovereign, open source email platform designed to grow from a small pilot
to **1 million mailboxes**, built on:

- **[Stalwart](https://stalw.art)** as the mail server (SMTP, IMAP, JMAP,
  POP3, spam filtering, and more in a single Rust codebase)
- **[Bulwark](https://github.com/bulwarkmail/webmail)** as the JMAP webmail
  client
- **Managed Kubernetes** hosted by a regional, sovereign cloud provider
  (OVHcloud and other sovereign providers are under evaluation)

The infrastructure should be elastic: it scales up and down with load
instead of being sized by hand for the peak. It should also be:

- **Tested.** Every change to the infrastructure code goes through
  automated tests, from linting up to end-to-end checks that send and
  receive real mail against a disposable environment.
- **Monitored.** Metrics, logs and alerting ship with the deployment, with
  dashboards for delivery, queue depth, storage and per-tenant usage.
- **Automated.** Provisioning, upgrades, backups and restores run from
  version-controlled code and CI, with no manual steps on the happy path.

## Where we are today

This repo deploys Stalwart and Bulwark on a **single VPS on OVH Canada**
with Ansible. It works and it is the base we learn from, but it is one
node with local RocksDB metadata, no automated tests, and monitoring
limited to log forwarding to OVH Logs Data Platform. Message bodies already
live in S3-compatible object storage, which carries over to the target
architecture.

## Guiding principles

- **Sovereignty first.** Data and infrastructure stay with local or
  regional providers, under the jurisdiction where our users live.
- **Open source and open standards.** JMAP, IMAP, SMTP and the usual
  authentication standards. No lock-in for us or for our users.
- **Grow by an order of magnitude at a time.** Each step is roughly ×10 the
  previous one. We only move on once the current step is stable.
- **Measure before we scale.** Decisions are driven by load tests and real
  usage, not assumptions.
- **Learn in public.** We will share what worked, what didn't, and why we
  changed course.

## The path: ×10 at every step

| Step | Target | Indicative horizon |
|------|--------|--------------------|
| 1 | 1,000 mailboxes | ~3 months |
| 2 | 10,000 mailboxes | ~6 months |
| 3 | 100,000 mailboxes | ~12 months |
| 4 | 1,000,000 mailboxes | ~24 months |

Horizons are indicative. A step is "done" when its goals below are met, not
when a date is reached.

---

### Step 0: Foundations (now)

Exploring and prototyping before anyone relies on the platform.

- Keep the single-host OVH Canada deployment running, and add backups with
  restore drills so it is safe to depend on in the meantime
- Run Stalwart and Bulwark on a test Kubernetes cluster
- Compare hosting providers on sovereignty, managed Kubernetes maturity,
  storage options, networking and cost
- Evaluate storage backends supported by Stalwart (relational database,
  key-value store, S3-compatible object storage for message blobs), since
  single-node RocksDB does not scale out
- Set up infrastructure as code and a basic CI/CD pipeline
- Write down the first architecture decisions (ADRs)

### Step 1: 1,000 mailboxes

A first real deployment with pilot users.

**Focus areas**

- Single cluster, single region, with a simple and well-understood
  architecture
- Webmail through Bulwark, plus standard IMAP/SMTP clients
- Deliverability basics: SPF, DKIM, DMARC, MTA-STS, reverse DNS, TLS
  everywhere
- Backups with tested restores
- Monitoring, logging and alerting for the core services
- End-to-end tests in CI against a disposable environment
- A first round of load testing to find our early limits

**What we want to learn**

- How Stalwart behaves on Kubernetes under real (if small) traffic
- Which storage setup is simplest to operate and recover
- Where the first bottlenecks appear

### Step 2: 10,000 mailboxes

From pilot to a dependable service.

**Focus areas**

- High availability for every component, no single points of failure
- Scaling services horizontally, possibly separating roles (inbound MX,
  submission, IMAP/JMAP access)
- Spam and abuse protection tuned on real traffic, and IP reputation
  management
- Migration tooling to import existing mailboxes from other providers
- Self-service onboarding and basic administration
- Documented operations runbooks

**What we want to learn**

- Real cost per mailbox and how it trends
- Which components need to scale independently
- How much operational effort the platform needs week to week

### Step 3: 100,000 mailboxes

Resilience and operational maturity.

**Focus areas**

- Spread across multiple availability zones, and explore a second region
- Disaster recovery plan, tested on a regular schedule
- Stronger tenant isolation for organizations
- Service level objectives (SLOs) published and tracked
- External security review
- Work toward relevant compliance requirements

**What we want to learn**

- Whether our storage choice holds at this size or needs to change
- How to roll out upgrades with no visible downtime
- What breaks first under sustained growth

### Step 4: 1,000,000 mailboxes

Operating at scale.

**Focus areas**

- Possibly a cell-based design (many independent, identical units) to limit
  the blast radius of failures
- Multi-region operations, and possibly multiple sovereign providers
- Automation for capacity planning, scaling and routine operations
- Cost efficiency at scale
- Deliverability and reputation management at large volume

**What we want to learn**

- The right size for a single "cell"
- How far automation can take day-to-day operations

---

## Open questions

These are things we have not decided yet and expect to answer through
testing:

- Which provider (or combination of providers) best fits our sovereignty
  and reliability needs?
- Which storage backend combination should Stalwart use at each scale?
- Should we run one large Stalwart cluster, or many smaller ones?
- How do we handle outbound IP reputation as volume grows?
- What does a realistic cost model look like at each step?

## Out of scope for now

We are deliberately not committing to these yet. Some may come later:

- Calendar, contacts and file storage beyond what Stalwart and Bulwark
  provide out of the box
- Native mobile apps
- Specific compliance certifications or dates

## Transparency and deploying it yourself

We want to be fully transparent. The code, the architecture decisions and
the test results live in this repo under the [AGPL-3.0](LICENSE). If you
want to deploy the same infrastructure on your own cloud accounts, you are
welcome to, and we will share what we learn running it.

Our reference infrastructure will be available in **private beta later
this year**.

## Following along and contributing

- Progress updates will be reflected in this file.
- Feedback, questions and ideas are welcome through issues and discussions.
  If you are interested in the private beta, open an issue.
- Where we find bugs or improvements in Stalwart or Bulwark, we intend to
  contribute them upstream.
