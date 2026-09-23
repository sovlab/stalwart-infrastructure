# OVH Logs Data Platform

This deployment can forward host and mail container logs to OVH Logs Data
Platform (LDP) through Vector. Without LDP, the mail containers use bounded
Docker `json-file` logging.

## Set up LDP

1. In the OVHcloud Control Panel, open **Identity, Security & Operations →
   Logs Data Platform** and create an LDP service in the intended region.
2. Create a data stream (for example `mail01-journal`) with indexation
   enabled. Enable long-term archiving only if its retention and cost are
   intentional.
3. Copy the stream's write token into `OVH_LDP_STREAM_TOKEN` in the project's
   `.env`. Set `ovh_ldp_cluster` in `ansible/inventory/group_vars/all.yml` to
   the LDP account's access-point hostname, without `https://` or port `9200`.
4. Create a dedicated LDP IAM identity with permission to ingest through the
   OpenSearch endpoint. Put its token in `OVH_LDP_IAM_TOKEN` in `.env`; set
   `ovh_ldp_iam_username` in `all.yml` to the associated `pat_jwt_` username.
5. Set `ovh_ldp_enabled: true` in `all.yml` and run
   `ansible-playbook playbooks/10-site.yml` from `ansible/`.

The Docker daemon's service logs are in the journal, and the mail containers
use Docker's `journald` logging driver. The role bounds persistent journal
storage, mounts it read-only into Vector,
grants Vector only the host's numeric `systemd-journal` group, and keeps a
durable delivery buffer. Vector has no Docker socket or published port.
`docker logs` continues to work.

## Configuration options

Set these in `ansible/inventory/group_vars/all.yml`. The examples are in
[all.yml.example](ansible/inventory/group_vars/all.yml.example), and the role
defaults are in [ovh_ldp/defaults/main.yml](ansible/roles/ovh_ldp/defaults/main.yml).

| Option | Default | Purpose |
| --- | --- | --- |
| `ovh_ldp_enabled` | `false` | Enable forwarding and journald container logging. |
| `ovh_ldp_cluster` | empty | LDP access-point hostname. |
| `ovh_ldp_host` | inventory hostname | Host field added to every event. |
| `ovh_ldp_iam_username` | `pat_jwt_mailserver` | IAM identity used for OpenSearch Basic authentication. |
| `ovh_ldp_journal_max_use` | `512M` | Cap persistent journal usage. |
| `ovh_ldp_journal_keep_free` | `1G` | Free disk space journald preserves. |
| `ovh_ldp_journal_retention` | `7day` | Journal age limit. |
| `ovh_ldp_vector_buffer_bytes` | `536870912` | Vector disk buffer size in bytes; must be at least `268435488`. |

Set the credentials in the project `.env`:

| Variable | Purpose |
| --- | --- |
| `OVH_LDP_STREAM_TOKEN` | Routes each event to the chosen data stream. |
| `OVH_LDP_IAM_TOKEN` | Authenticates Vector to the OpenSearch endpoint. |

The role also exposes `ovh_ldp_vector_image`, `ovh_ldp_vector_uid`,
`ovh_ldp_vector_gid`, and `ovh_ldp_dir` for deployments that need to override
the container image, runtime identity, or state location.

## Verify and search in Graylog

After deployment, emit a test event on the host:

```bash
logger -t ldp-test "OVH LDP forwarding test"
```

Search the new Graylog stream for `SYSLOG_IDENTIFIER:ldp-test`.

### Unified fields

Each container writes a different format: Caddy emits JSON, the Docker daemon
emits logfmt, Stalwart emits plain text. Vector normalises all of them onto
one set of fields before shipping, so a search does not have to know which
producer it is looking at.

| Field | Present for | Meaning |
| --- | --- | --- |
| `service` | everything | `caddy`, `stalwart`, `bulwark`, `vector`, or the unit's syslog identifier (`dockerd`, `sshd`, `systemd`) for host logs. |
| `app_level` | everything | `error`, `warn`, `notice`, `info` or `debug`. Taken from the application's own level when it has one, otherwise derived from journald's `PRIORITY`. |
| `message` / `short_message` | everything | One readable line. For structured producers this is the message plus request context, not the raw payload. |
| `full_message` | JSON and logfmt lines | The untouched original payload. |
| `logger` | JSON lines | Subsystem, e.g. `http.handlers.reverse_proxy`. |
| `app_error` | JSON and logfmt lines | The `error` or `err` field. |
| `client_ip`, `http_method`, `http_host`, `http_path`, `http_proto` | Caddy | Request context. |
| `http_status`, `http_bytes`, `http_duration_ms` | Caddy | Numeric; absent rather than zero when the producer did not report them. |
| `upstream` | Caddy | Proxy target, e.g. `stalwart:8080`. |
| `app_client_addr`, `app_dns_server`, `app_question` | Docker daemon | Resolver diagnostics. |

The readable form of the severity is `app_level`, not `level`: GELF reserves
`level` for the numeric syslog value, and a string under that name conflicts
with the index mapping.

Structured payloads are read for named fields only, never flattened wholesale.
Caddy's JSON carries the entire request header map; flattening it would add a
Graylog field per header ever seen and walk the stream towards LDP's
per-stream field limit for no search value. The headers stay searchable as
text inside `full_message`.

| Graylog query | Finds |
| --- | --- |
| `app_level:(error OR warn)` | Everything unhealthy, across all containers. |
| `service:stalwart AND app_level:error` | Stalwart errors only. |
| `service:caddy AND http_status:[500 TO 599]` | Proxy and upstream failures. |
| `http_path:/jmap*` | JMAP traffic, by request path. |
| `client_ip:203.0.113.7` | One client, across Caddy and the mail containers. |
| `http_duration_ms:>1000` | Requests slower than a second. |
| `service:dockerd AND app_level:error` | Docker daemon errors, including resolver failures. |
| `full_message:"context canceled"` | Text that only exists in the raw payload. |

Stalwart's stdout tracer is plain text, so it gets `service` and `app_level`
but no structured fields. Parsing applies to newly ingested messages after
redeployment, not to entries already in Graylog.

Known-benign noise worth a Graylog stream rule rather than a search: Caddy
logs `aborting with incomplete response` with `error: reading: context
canceled` every time a browser closes a JMAP EventSource stream, which is
ordinary client behaviour on `/jmap/eventsource/`.

## Disable forwarding

Set `ovh_ldp_enabled: false` and rerun the site playbook. This stops Vector,
removes rendered credentials, restores bounded `json-file` logging, and
recreates the mail containers with that driver. Vector's state directory is
retained so a later re-enable can resume from its prior cursor. On hosts with
systemd 250–257, Vector reads the current boot only; logs left unread when
the host reboots cannot be replayed from the previous boot.
