# Observability: ELK on robertdobbs, Grafana, and the bots share

Verified inventory of the destination (2026-11-25, from this workstation):

| host | what runs there | reachable from here |
|---|---|---|
| `robertdobbs` (robertdobbs) | Elasticsearch 8.19.24 (**localhost only**, security on), Kibana 8.19.24 on `:5601`, Logstash 8.19.24, Fleet Server + Elastic Agent, Grafana 13.2.5 on `:3000`, Prometheus, Loki, Alloy | `5601` yes, `3000` yes, `8220` yes, `9200` **no** |
| `us-hm-proxmox01` (us-hm-proxmox01) | Proxmox + shares; `/data_pool1` | SMB `445` yes, SSH **refused** (key is source-restricted) |

Consequence: the worker **cannot** index straight into Elasticsearch. Two
supported paths, pick one.

---

## 1. Logs -> Logstash HTTP input (recommended)

No credentials on the Windows box, one small change on `robertdobbs`.

On `robertdobbs`, add a pipeline next to the existing FortiGate one:

```ruby
# ~/elk/pipeline/sysmon.conf
input {
  http {
    port => 8080
    codec => json_lines
  }
}
filter {
  # worker already emits ECS-ish fields; just make the dataset explicit
  mutate { add_field => { "[event][module]" => "sysmon" } }
}
output {
  elasticsearch {
    hosts => ["http://elasticsearch:9200"]
    user => "elastic"
    password => "${ELASTIC_PASSWORD}"      # already in the compose env
    data_stream => false
    index => "sysmon-%{+YYYY.MM.dd}"
  }
}
```

Mount that file into `elk-logstash` and publish the port in the compose file:

```yaml
logstash:
  ports:
    - "8080:8080"   # add
```

Then on the Windows side:

```bat
python -m sysmon ...
```

and in `%LOCALAPPDATA%\hermes-sysmon\config.json`:

```json
"logs": { "ship": { "http_url": "http://robertdobbs:8080/", "batch": 200 } }
```

Restart the worker. Verify:

```bash
curl -s 'http://localhost:9200/_cat/indices/sysmon-*?v' -u elastic:$ELASTIC_PASSWORD
```

Kibana: create a data view on `sysmon-*`, time field `@timestamp`. Useful
filters: `event.kind : "transition.end"`, `event.kind : "state.drift"`,
`log.level : "error"`.

## 2. Logs -> Elastic Agent + Fleet (no compose change, needs enrolment)

Fleet Server is up on `:8220`. Install the Elastic Agent on Windows, enrol to
`https://robertdobbs:8220` with an enrolment token, and add a **Custom Logs**
integration pointing at:

```
%LOCALAPPDATA%\hermes-sysmon\logs\sysmon-*.jsonl
```

Set the dataset to `sysmon.log` and the parser to `ndjson`. The worker already
writes one JSON object per line with `@timestamp`, so no ingest pipeline is
needed. Trade-off: an agent to keep alive, but zero changes to the ELK stack.

## 3. Fallback: SMB tail (no ELK access at all)

`logs.ship.share_dir` mirrors the JSONL to
`\\us-hm-proxmox01\bots\logs\<host>\`. A cron/Alloy job on `robertdobbs` can tail
that directory and POST to Logstash or the ES bulk API. Slower and less tidy;
keep it as the "ELK is down and I still want logs" path.

---

## Create the bots share

`\\us-hm-proxmox01\bots` **does not exist yet** — the worker logs
`WinError 1326` and carries on. SSH from this box is refused, so run this on
`proxmox01` (or its web shell) as root:

```bash
mkdir -p /data_pool1/bots/{heads,logs,artifacts}
chown -R chrismok:chrismok /data_pool1/bots

# Samba share (adjust to your smb.conf layout)
cat >> /etc/samba/smb.conf <<'EOF'
[bots]
   path = /data_pool1/bots
   browseable = yes
   read only = no
   valid users = chrismok
   create mask = 0664
   directory mask = 0775
EOF

smbpasswd -a chrismok      # set a share password
systemctl restart smbd
```

Then on Windows, once, so the credentials are cached for the task context:

```bat
net use \\us-hm-proxmox01\bots /user:chrismok * /persistent:yes
```

Verify read-back before trusting it:

```bat
dir \\us-hm-proxmox01\bots\heads
type "%LOCALAPPDATA%\hermes-sysmon\head\hermes-win11-ops-sysmon-peeweeherman.json"
```

Expected layout afterwards:

```
\\us-hm-proxmox01\bots\
  heads\<agent>.json                 the rendezvous record a supervisor scans
  logs\<host>\sysmon-<host>-YYYYMMDD.jsonl
```

---

## Metrics -> Prometheus -> Grafana

`GET /api/metrics` on the worker is Prometheus text (open, no token, because a
scrape config cannot send a bearer header cleanly). Add a scrape job on the
Prometheus in `robertdobbs`:

```yaml
scrape_configs:
  - job_name: sysmon
    metrics_path: /api/metrics
    static_configs:
      - targets: ["100.x.y.z:8765"]
```

Grafana dashboard (`robertdobbs:3000`): panels for
`sysmon_cpu_percent`, `sysmon_gpu_percent`, `sysmon_mem_percent`,
`sysmon_disk_*_mbs`, `sysmon_transition_active` (stat) and
`sysmon_transition_progress` (gauge, grouped by `to`).

The worker's own dashboard already links to Grafana and Kibana via
`config.links`, so the small window is the entry point and the stack is one
click away.

---

## Supervisor wiring

The supervisor does not need a new protocol:

1. Scan `\\us-hm-proxmox01\bots\heads\*.json` (or receive the same record by
   `POST /api/head`).
2. Reject heads whose `heartbeat_at` is older than 3x `heartbeat_s`.
3. Keep the bearer token for that agent locally; compare its SHA-256 prefix with
   `api.token_fingerprint` in the head to confirm you hold the right one.
4. `GET /api/state` for status, `POST /api/transition` to move it,
   `POST /api/abort` to interrupt.

Every worker also has the supervisor endpoints (`POST /api/head`,
`GET /api/heads`), so one designated host can act as the aggregation point and
serve the "links to the other agents" page without extra software.
