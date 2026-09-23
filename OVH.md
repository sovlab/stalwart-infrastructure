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

Vector parses logfmt lines such as Docker daemon messages containing
`level=error msg="..."`. It preserves `message` and `short_message` and adds
`app_level`, `app_message`, and available `app_error`/DNS fields. The journal's
`PRIORITY` is separate; it can be informational even when the application
writes `level=error`.

| Graylog query | Finds |
| --- | --- |
| `app_level:error` | Newly ingested logfmt errors. |
| `SYSLOG_IDENTIFIER:dockerd AND app_level:error` | Docker daemon errors, including resolver failures. |
| `app_error:"dns: overflowing header size"` | The parsed DNS error in the example. |
| `CONTAINER_NAME:mailstack-stalwart-1 AND message:(error OR ERROR)` | Plain-text Stalwart messages mentioning errors. |

`CONTAINER_NAME` identifies container output. Docker daemon messages have
`SYSLOG_IDENTIFIER:dockerd` and no `CONTAINER_NAME`. Mail containers that
write plain text can be searched by `message`; `app_level` is present only
when Vector parses a logfmt `level=` field. Parsing affects newly ingested
messages after redeployment, not entries already in Graylog.

## Disable forwarding

Set `ovh_ldp_enabled: false` and rerun the site playbook. This stops Vector,
removes rendered credentials, restores bounded `json-file` logging, and
recreates the mail containers with that driver. Vector's state directory is
retained so a later re-enable can resume from its prior cursor. On hosts with
systemd 250–257, Vector reads the current boot only; logs left unread when
the host reboots cannot be replayed from the previous boot.
