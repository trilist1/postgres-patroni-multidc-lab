# PostgreSQL High-Availability Cluster

## Patroni + etcd + HAProxy + Prometheus + Grafana on Docker for Windows

_Last upgraded 2026-10-13: PostgreSQL 18.6, Patroni 4.1.5, etcd 3.7.2, HAProxy 3.4.6, Prometheus 3.15.0, Grafana 13.2.3, postgres_exporter 0.20.1. See [Upgrade Notes](#upgrade-notes-2026-10-13)._

A production-style HA PostgreSQL lab environment running entirely in Docker Desktop.
13 containers managed as a single stack via Docker Compose, including a landing page at
**http://localhost:9090** that draws the topology, shows live status and opens every service's configuration.

---

## Architecture Overview

```
                    ┌─────────────────────────────────────────────┐
                    │           CLIENT APPLICATIONS               │
                    │  psql -h localhost -p 5000 -U postgres      │
                    └──────────┬──────────────┬───────────────────┘
                               │              │
                        Write :5000     Read :5001
                               │              │
                    ┌──────────┴──────────────┴───────────────────┐
                    │              HAProxy                         │
                    │  Health checks Patroni REST API (:8008)     │
                    │  Write → /primary (200 = leader only)       │
                    │  Read  → /replica (200 = standbys only)     │
                    │  Stats dashboard at :7000                    │
                    └────┬─────────────┬─────────────┬────────────┘
                         │             │             │
              ┌──────────┴──┐  ┌──────┴───────┐  ┌──┴──────────┐
              │  patroni1   │  │  patroni2    │  │  patroni3   │
              │  PG :5432   │  │  PG :5432    │  │  PG :5432   │
              │  API :8008  │  │  API :8008   │  │  API :8008  │
              │  (LEADER)   │  │  (REPLICA)   │  │  (REPLICA)  │
              └──────┬──────┘  └──────┬───────┘  └──────┬──────┘
                     │                │                  │
              ┌──────┴──────┐  ┌──────┴───────┐  ┌──────┴──────┐
              │   etcd1     │  │   etcd2      │  │   etcd3     │
              │   :2379     │◄─┤   :2379      ├─►│   :2379     │
              │             │  │              │  │             │
              └─────────────┘  └──────────────┘  └─────────────┘
                     Distributed Consensus (Raft Protocol)
```

### Monitoring Stack

```
   patroni1:8008 ──┐                    ┌── pg-exporter1 ──┐
   patroni2:8008 ──┼── /metrics ──►     │   (→ patroni1)   │
   patroni3:8008 ──┘                    ├── pg-exporter2 ──┼──► Prometheus ──► Grafana
                                        │   (→ patroni2)   │      :9091        :3000
   etcd1:2379 ─────┐                    ├── pg-exporter3 ──┘
   etcd2:2379 ─────┼── /metrics ──►     │   (→ patroni3)
   etcd3:2379 ─────┘                    │
                                        └──────────────────
```

**Key design decision:** 3 separate postgres_exporters (one per PostgreSQL node)
instead of 1 through HAProxy. This ensures we collect metrics from **both the
primary and replicas**, including replica-specific stats like replication lag
and WAL receive positions.

---

## Component Summary

| Component | Image | Version | Purpose |
|-----------|-------|---------|---------|
| **etcd** | `gcr.io/etcd-development/etcd` | v3.7.2 | Distributed key-value store. Stores Patroni cluster state and handles leader election via Raft consensus. 3 nodes for quorum. v3 API only (v2 was removed in 3.6). |
| **Patroni** | Custom `patroni-pg18:4.1.5` (built from `postgres:18.6`) | 4.1.5 | PostgreSQL HA manager. Runs on each PG node, manages replication, handles automatic failover, exposes REST API for health checks. |
| **PostgreSQL** | Bundled in Patroni image | 18.6 | The database. One primary (read-write) and two streaming replicas (read-only). Patroni manages the lifecycle. |
| **HAProxy** | `haproxy` | 3.4.6 (LTS) | TCP load balancer. Routes write traffic to the primary, read traffic across replicas. Uses Patroni REST API for backend health and Docker DNS for runtime name resolution. |
| **postgres_exporter** | `quay.io/prometheuscommunity/postgres-exporter` | v0.20.1 | Converts PostgreSQL statistics views into Prometheus metrics. Built-in collectors plus `stat_checkpointer`, `stat_statements` and `long_running_transactions`. |
| **Prometheus** | `prom/prometheus` | v3.15.0 | Time-series database. Scrapes metrics from Patroni, pg_exporters, and etcd every 10-15 seconds. |
| **Grafana** | `grafana/grafana` | 13.2.3 | Visualisation. Pre-provisioned with Prometheus datasource and a 40+ panel dashboard. |
| **Landing page** | `python:3.15.0-slim` + `landing\` | n/a | Topology diagram, live status of all 13 services and a configuration viewer. Standard-library Python, no build step. |

All tags are pinned (no `:latest`) so a rebuild gives the same stack. Versions live in the
`Image versions` block at the top of `patroni-ha-cluster.cmd`.

---

## Port Map

| Service | Host Port | Container Port | Protocol | Purpose |
|---------|-----------|----------------|----------|---------|
| HAProxy Write | **5000** | 5000 | TCP/PostgreSQL | Connect here for read-write queries |
| HAProxy Read | **5001** | 5001 | TCP/PostgreSQL | Connect here for read-only queries |
| HAProxy Stats | **7000** | 7000 | HTTP | HAProxy dashboard |
| patroni1 PG | 5433 | 5432 | TCP/PostgreSQL | Direct access (bypass HAProxy) |
| patroni2 PG | 5434 | 5432 | TCP/PostgreSQL | Direct access |
| patroni3 PG | 5435 | 5432 | TCP/PostgreSQL | Direct access |
| patroni1 API | 8008 | 8008 | HTTP/REST | Patroni REST API |
| patroni2 API | 8009 | 8008 | HTTP/REST | Patroni REST API |
| patroni3 API | 8010 | 8008 | HTTP/REST | Patroni REST API |
| etcd1 | 2379 | 2379 | HTTP/gRPC | etcd client API |
| etcd2 | 2381 | 2379 | HTTP/gRPC | etcd client API |
| etcd3 | 2383 | 2379 | HTTP/gRPC | etcd client API |
| pg-exporter1 | 9187 | 9187 | HTTP | Prometheus metrics (patroni1) |
| pg-exporter2 | 9188 | 9187 | HTTP | Prometheus metrics (patroni2) |
| pg-exporter3 | 9189 | 9187 | HTTP | Prometheus metrics (patroni3) |
| Landing page | **9090** | 8080 | HTTP | Topology, live status, configuration viewer |
| Prometheus | **9091** | 9090 | HTTP | Prometheus web UI. Host port moved from 9090 to free it for the landing page; inside the Docker network it is still `prometheus:9090` |
| Grafana | **3000** | 3000 | HTTP | Grafana dashboards |

---

## Quick Start

### Prerequisites

- Docker Desktop for Windows with WSL2 backend
- Minimum 8GB RAM allocated to Docker (Settings → Resources)
- ~5GB free disk space

### Setup (One Command)

```cmd
patroni-ha-cluster.cmd setup
```

This will:
1. Create directory structure under `BASE_DIR`
2. Generate TLS certificates (CA + server/client certs)
3. Generate Patroni, HAProxy, Prometheus, Grafana configs
4. Check the landing page app in `landing\`
5. Generate `docker-compose.yml`
6. Build the Patroni Docker image (first run: ~3-5 minutes)
7. Start all 13 containers via `docker compose up -d`
8. Create `pg_stat_statements`, the `appuser` role and the `appdb` database on the leader

Then open **http://localhost:9090** for the landing page.

### Verify

```cmd
REM Check cluster status
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml list

REM Expected output (any node can be the leader):
REM + Cluster: pg-ha-cluster (7696080993598394415) --+-------------+-----+------------+-----+
REM | Member   | Host     | Role    | State     | TL | Receive LSN | Lag | Replay LSN | Lag |
REM +----------+----------+---------+-----------+----+-------------+-----+------------+-----+
REM | patroni1 | patroni1 | Replica | streaming |  1 |   0/4000060 |   0 |  0/4000060 |   0 |
REM | patroni2 | patroni2 | Replica | streaming |  1 |   0/4000060 |   0 |  0/4000060 |   0 |
REM | patroni3 | patroni3 | Leader  | running   |  1 |             |     |            |     |
REM +----------+----------+---------+-----------+----+-------------+-----+------------+-----+

REM Connect to the cluster
psql -h localhost -p 5000 -U postgres -d postgres
```

---

## Step-by-Step Guide

For learning purposes, you can bring up the cluster layer by layer:

### Step 1: Generate Everything

```cmd
patroni-ha-cluster.cmd generate
```

Examine the generated files:
- `config\patroni1.yml` — Patroni YAML for node 1
- `config\haproxy\haproxy.cfg` — HAProxy load balancer config
- `config\prometheus\prometheus.yml` — Prometheus scrape targets
- `docker-compose.yml` — The full Docker Compose definition
- `certs\` — TLS certificates (CA, etcd, PG, HAProxy)

### Step 2: Start etcd

```cmd
patroni-ha-cluster.cmd start-etcd
```

Verify:
```cmd
docker exec etcd1 /usr/local/bin/etcdctl endpoint health ^
  --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379
```

**What's happening:** Three etcd nodes form a Raft cluster. They elect a leader
among themselves (not related to the PostgreSQL leader). etcd needs 2 of 3 nodes
for quorum. This is where Patroni stores all cluster metadata.

### Step 3: Start Patroni / PostgreSQL

```cmd
patroni-ha-cluster.cmd start-patroni
```

Wait ~20 seconds, then:
```cmd
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml list
```

**What's happening:**
1. Each Patroni node connects to etcd
2. First node to set the leader key in etcd becomes the primary
3. Primary runs `initdb` to create the PostgreSQL cluster
4. Other nodes use `pg_basebackup` to clone from the primary
5. Replicas start streaming WAL from the primary

### Step 4: Start HAProxy

```cmd
patroni-ha-cluster.cmd start-haproxy
```

Open http://localhost:7000 (admin/HaproxyStats2026) to see backend health.

**What's happening:** HAProxy polls each Patroni node's REST API:
- `GET /primary` returns 200 only from the leader → write backend
- `GET /replica` returns 200 only from standbys → read backend
- If a node is down or switching roles, it returns non-200 and HAProxy marks it down

### Step 5: Start Monitoring

```cmd
patroni-ha-cluster.cmd start-monitoring
```

This starts the three exporters, Prometheus, Grafana and the landing page.

Open:
- Landing page: http://localhost:9090
- Prometheus: http://localhost:9091
- Grafana: http://localhost:3000 (admin/GrafanaAdmin2026)

---

## How Failover Works

```
BEFORE:                          AFTER (patroni1 fails):

patroni1 [LEADER]               patroni1 [DOWN]
  ├─ patroni2 [REPLICA]           patroni2 [LEADER] ◄── promoted
  └─ patroni3 [REPLICA]           └─ patroni3 [REPLICA] ◄── re-pointed

1. patroni1 stops responding
2. Its leader key in etcd expires (TTL: 30s)
3. patroni2 and patroni3 race to acquire the key
4. Winner runs pg_promote() to become new primary
5. Loser reconfigures to stream from new primary
6. HAProxy detects role change via REST API health checks
7. Client connections route to new primary within ~3 seconds

When patroni1 comes back:
8. It detects another node holds the leader key
9. Uses pg_rewind to sync its diverged timeline
10. Starts streaming as a replica from the new primary
```

### Test Failover

Easiest: use **Change leader** on the landing page (http://localhost:9090), see
[Changing the leader](#changing-the-leader-from-the-landing-page). To simulate a crash instead:

```cmd
patroni-ha-cluster.cmd failover-test
```

Or manually:
```cmd
REM See who is leader
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml list

REM Stop the leader
docker compose -p patroni-ha stop patroni1

REM Wait 15 seconds, then check
docker exec -it patroni2 patronictl -c /etc/patroni/patroni.yml list

REM Bring it back (rejoins as replica)
docker compose -p patroni-ha start patroni1
```

---

## Configuration Management

### Changing PostgreSQL Parameters

**DO NOT** edit `postgresql.conf` directly — Patroni overwrites it.

```cmd
REM View current dynamic config (stored in etcd)
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml show-config

REM Edit (opens editor inside container)
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml edit-config

REM After editing, check if restart is needed
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml list
REM Look for "pending restart" flag

REM Rolling restart (one node at a time, no downtime)
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml restart pg-ha-cluster
```

### Configuration Hierarchy

| Level | Source | Scope | Persistence |
|-------|--------|-------|-------------|
| Dynamic (DCS) | `patronictl edit-config` | All nodes | Stored in etcd |
| Local | `patroni.yml` → `postgresql.parameters` | Per node | In config file |
| Bootstrap | `patroni.yml` → `bootstrap.dcs` | Initial only | Used once at init |

---

## Monitoring: Grafana Dashboard

The pre-provisioned dashboard has **40+ panels** across these sections:

| Section | Metrics Shown |
|---------|--------------|
| **Cluster Overview** | Node states (running/stopped), leader indicator, timeline, uptime |
| **Replication** | WAL positions, lag in seconds and bytes, WAL file count, streaming detail |
| **Database Activity** | Connections by state per node, TPS (commits/rollbacks), row operations |
| **Performance** | Cache hit ratio (gauge + per-DB), block I/O, checkpoints, buffer writes, temp files |
| **Locks and Conflicts** | Lock types/modes, deadlock rate, replication conflicts |
| **Table Health** | Dead tuples, live vs dead ratio, HOT update ratio, table sizes |
| **Database Size** | Size trends and current size per database |
| **Query Performance** | Top queries by total/mean time (pg_stat_statements) |
| **Long Running Queries** | Currently active queries sorted by duration |
| **etcd Cluster** | Leader changes, Raft proposals, DB size, WAL fsync latency |

### postgres_exporter collectors

There are **no custom queries** (`--extend.query-path` and `--auto-discover-databases`
are deprecated upstream). The exporters use the built-in collectors plus three that are
off by default:

| Flag | Why |
|------|-----|
| `--collector.stat_checkpointer` | PostgreSQL 17+ moved checkpoint counters from `pg_stat_bgwriter` to `pg_stat_checkpointer`. The checkpoint panels need it. |
| `--collector.stat_statements` | Top queries from `pg_stat_statements` (the extension is created by setup). |
| `--collector.long_running_transactions` | Oldest/longest-running transaction metrics. |

The dashboard is generated by `gen_monitoring.py`. Metric names it relies on that changed in
recent releases: `patroni_postgres_running`, `patroni_postgres_timeline` and `patroni_version`
(Patroni 4), `pg_replication_lag_seconds` (exporter), `etcd_mvcc_db_total_size_in_bytes` (etcd 3.6+).

---

## Landing Page

**http://localhost:9090** is the front door to the cluster.

- **Topology diagram**: your applications, HAProxy, the three database containers and the etcd
  quorum, with Prometheus and Grafana in a monitoring column. Each database node is drawn as
  what it really is:
  - a dashed frame labelled **container patroni1/2/3** (the Docker container name);
  - inside it, the **PostgreSQL 18.6** card, which is the part that matters: its own status dot, primary or
    replica role, replication state and lag, connections in use of `max_connections`, number of
    databases, total size and uptime. A **Primary** tag marks the node taking writes;
  - under the card, the **Patroni agent** row with its own dot (the HA manager running in the
    same container, REST API on :8008);
  - below the frame, the **pg-exporter** chip, which is a separate container reading that PostgreSQL's
    stats.
  The thick blue line is the write path to the current leader and teal lines are read paths;
  dashed blue arcs are WAL streaming from leader to replicas. Box outlines are green (healthy),
  amber (degraded) or red (down). It refreshes every 5 seconds, so a failover is visible live.
- **Services table**: status, what each service is doing right now, pinned image, and links to
  each HTTP endpoint (Patroni REST API, exporter `/metrics`, HAProxy stats, Grafana, Prometheus).
- **View config**: select a box or a table button to see the service's block in
  `docker-compose.yml`, the files mounted into it (for example `patroni1.yml`, `haproxy.cfg`,
  the Patroni `Dockerfile`), and its *running* config where one exists: Patroni's dynamic DCS
  config, Prometheus's loaded config, etcd's member list. Passwords are masked unless you tick
  **Show secrets**.

### Changing the leader from the landing page

The **Change leader** panel (below the diagram) moves the primary role to another node:

1. Pick the **new leader**: each replica is shown with its state and replication lag.
2. Pick the **method**:
   - **Switchover** (planned, no data loss): the healthy leader hands over and rejoins as a
     replica. Patroni refuses if either node isn't healthy. This is what you want for maintenance.
   - **Failover** (emergency): promotes the replica immediately, even if the leader is down or
     stuck. Anything the replica hadn't received is lost. It's the only option when there's no leader.
3. Select **Switch over to ...** / **Fail over to ...**, read the confirmation, then **Confirm**.

The page waits until the new node is leader *and* HAProxy routes port 5000 to it, then reports
how long it took (about 2-8 seconds here). Clients on port 5000 have their connections to the
old leader dropped and reconnect to the new one. **Leader history** lists every promotion with
its time and the timeline it started.

Same thing from the command line:

```cmd
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml switchover --candidate patroni2 --force
curl -X POST -H "Origin: http://localhost:9090" http://localhost:9090/api/switchover -d "{\"candidate\":\"patroni2\"}"
```

> The landing page only accepts these requests from itself (it checks the `Origin` header), so
> other websites open in your browser can't trigger them. Note that the Patroni REST API on
> ports 8008-8010 has no authentication in this lab. Anyone who can reach those ports can
> switch over too, so don't expose them beyond your machine.

How it works: `landing\server.py` is a standard-library Python server running in the compose
network, so it probes services by container name (no CORS, nothing exposed beyond the existing
ports). Config files are mounted read-only. Its API is plain JSON if you want to script against it:

```cmd
curl http://localhost:9090/api/status
curl http://localhost:9090/api/config/patroni1
curl http://localhost:9090/api/history
```

Edit `landing\index.html` or `server.py`, then `docker restart landing`.

---

## File Structure

```
D:\Software\Containers\PostgresPatroni\Claude\
├── patroni-ha-cluster.cmd          ← This management script
├── README.md                       ← This documentation
├── docker-compose.yml              ← Generated Docker Compose file
├── pgadmin-servers.json            ← pgAdmin import file (all endpoints, gssencmode=disable)
│
├── config\
│   ├── patroni1.yml                ← Patroni config (node 1)
│   ├── patroni2.yml                ← Patroni config (node 2)
│   ├── patroni3.yml                ← Patroni config (node 3)
│   ├── haproxy\
│   │   └── haproxy.cfg             ← HAProxy load balancer config
│   ├── prometheus\
│   │   └── prometheus.yml          ← Prometheus scrape config
│   └── grafana\
│       ├── provisioning\
│       │   ├── datasources\
│       │   │   └── datasource.yml  ← Auto-configures Prometheus datasource
│       │   └── dashboards\
│       │       └── provider.yml    ← Points Grafana to dashboard JSON
│       └── dashboards\
│           └── pg-ha-cluster.json  ← The 40+ panel dashboard
│
├── certs\                          ← TLS certificates (auto-generated)
│   ├── ca.crt / ca.key             ← Certificate Authority
│   ├── etcd-server.crt / .key      ← etcd server certificate
│   ├── etcd-peer.crt / .key        ← etcd peer-to-peer certificate
│   ├── etcd-client.crt / .key      ← Client cert for Patroni→etcd
│   ├── pg-server.crt / .key        ← PostgreSQL server certificate
│   └── haproxy.crt / .key          ← HAProxy certificate
│
├── gen_monitoring.py               ← Grafana dashboard generator
│
├── landing\                        ← Landing page (port 9090)
│   ├── server.py                   ← Status probes + config API (stdlib Python)
│   └── index.html                  ← Topology diagram, service table, config viewer
│
├── patroni-image\                  ← Patroni Docker build context
│   ├── Dockerfile
│   └── entrypoint.sh
│
├── pgdata\                         ← PostgreSQL data (HOST MAPPED)
│   ├── patroni1\                   ← Node 1 PGDATA
│   ├── patroni2\                   ← Node 2 PGDATA
│   └── patroni3\                   ← Node 3 PGDATA
│
├── etcd-data\                      ← etcd persistent data
│   ├── etcd1\
│   ├── etcd2\
│   └── etcd3\
│
├── prometheus-data\                ← Prometheus TSDB
├── grafana-data\                   ← Grafana state (dashboards, users)
│
└── backups\                        ← Created by the 2026-10-13 upgrade
    ├── pre-upgrade-files\          ← Previous compose, configs, script, README
    └── pg17-20261013\              ← db1.dump, globals.sql, old pgdata + etcd data
```

---

## Connecting with pgAdmin

Import the ready-made server list: in pgAdmin choose **Tools > Import/Export Servers**, pick
**Import**, select `pgadmin-servers.json` from this folder, and confirm. That adds the write
endpoint (:5000), the read endpoint (:5001) and the three nodes directly (:5433-5435), all as
user `postgres`. pgAdmin asks for the password (`SuperSecret2026`) on first connect.

Every entry sets `gssencmode=disable`. Without it, pgAdmin 9.x on Windows can crash with
`exception: access violation writing 0x0000000000000000` while it probes for Kerberos
encryption. The crash is client-side and the cluster doesn't use Kerberos, so disabling GSS
negotiation has no downside. For a server you add by hand: **Properties > Parameters >
add "GSS encryption mode" = disable**.

---

## Useful Commands

```cmd
REM === PATRONI ===
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml list
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml show-config
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml edit-config
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml history
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml switchover
docker exec -it patroni1 patronictl -c /etc/patroni/patroni.yml restart pg-ha-cluster

REM === ETCD ===
docker exec etcd1 etcdctl endpoint health --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379
docker exec etcd1 etcdctl member list -w table --endpoints=http://etcd1:2379
docker exec etcd1 etcdctl get --prefix /service/pg-ha-cluster --endpoints=http://etcd1:2379

REM === POSTGRESQL (via HAProxy) ===
psql -h localhost -p 5000 -U postgres -c "SELECT pg_is_in_recovery();"
psql -h localhost -p 5000 -U postgres -c "SELECT * FROM pg_stat_replication;"
psql -h localhost -p 5000 -U postgres -c "SELECT * FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;"

REM === DOCKER COMPOSE ===
docker compose -p patroni-ha ps
docker compose -p patroni-ha logs -f --tail 100 patroni1
docker compose -p patroni-ha stop
docker compose -p patroni-ha start
docker compose -p patroni-ha down --remove-orphans
```

---

## Credentials

| Service | Username | Password | Notes |
|---------|----------|----------|-------|
| PostgreSQL superuser | `postgres` | `SuperSecret2026` | Full access |
| PostgreSQL replication | `replicator` | `ReplSecret2026` | Streaming replication |
| PostgreSQL app user | `appuser` | `AppSecret2026` | Created by the setup post-step (owner of `appdb`) |
| HAProxy stats | `admin` | `HaproxyStats2026` | http://localhost:7000 |
| Grafana | `admin` | `GrafanaAdmin2026` | http://localhost:3000 |

**Change these** in the `CONFIGURATION` section of `patroni-ha-cluster.cmd` before
running setup in any shared or non-isolated environment.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| etcd won't start | Stale data from previous cluster | Delete `etcd-data\` folders, re-run setup |
| Patroni stuck "creating replica" | etcd not ready | Check etcd health, ensure all 3 nodes are up |
| HAProxy shows all backends red | Patroni hasn't finished init | Wait 30s; check `patronictl list` |
| pg-exporter connection refused | PG not ready on that node | Exporter retries automatically; wait 30s |
| Ports 8009/8010 not responding | Old containers from non-compose run | Run option 9 (remove), then option 1 (setup) |
| "Data directory has wrong ownership" | Windows volume permission issue | Delete `pgdata\patroniX\` folder for that node |
| Dashboard shows "No data" | Prometheus hasn't scraped yet | Wait 30s; check Prometheus targets at http://localhost:9091/targets |
| Patroni loops on "etcd not ready", HAProxy/exporters say `no such host` | Containers reference a deleted Docker network (e.g. after `docker network prune`) | `docker compose -p patroni-ha up -d --force-recreate` |
| etcd exits with `unknown flag: --enable-v2` | Old compose file with etcd 3.6+ | Re-run `patroni-ha-cluster.cmd generate` (the flag was removed) |
| etcd refuses to start after a version bump | etcd only upgrades one minor version at a time | Go 3.5 to 3.6 to 3.7, or start with an empty `etcd-data\` (Patroni re-registers) |
| Patroni log: "User creation is not supported starting from v4.0.0" | `bootstrap.users` was removed in Patroni 4 | Already handled: setup creates `appuser` after the leader is up |
| pgAdmin: `exception: access violation writing 0x0000000000000000` | pgAdmin on Windows crashes inside libpq while probing Kerberos (GSSAPI) encryption; the server is never reached | Set **GSS encryption mode = disable** on the server (Parameters tab), or import `pgadmin-servers.json` - see [Connecting with pgAdmin](#connecting-with-pgadmin) |
| Landing page says it can't reach its own backend | `landing` container stopped | `docker compose -p patroni-ha up -d landing` |
| Port 9090 already in use | Something else on the host owns 9090 | Change `LANDING_PORT` in the script and re-run `generate` |

## Using Standard Grafana Community Dashboards

Standard PostgreSQL dashboards from [grafana.com/dashboards](https://grafana.com/grafana/dashboards/)
work with this setup because the Prometheus config uses:

- **`job_name: "postgres"`** — matches what most community dashboards expect
- **`relabel_configs`** — rewrites `instance` from the exporter address
  (`pg-exporter1:9187`) to the actual PostgreSQL server (`patroni1:5432`)
  so dashboard template variables populate correctly

### Recommended Community Dashboards

Import these via Grafana UI (+ → Import → paste ID):

| Dashboard ID | Name | Notes |
|-------------|------|-------|
| **9628** | PostgreSQL Database | Most popular. Works out of the box. |
| **14114** | PostgreSQL Exporter Quickstart | Good overview with connection pool stats. |
| **12273** | PostgreSQL (pgwatch) | Detailed per-query performance. |
| **455** | Postgres Overview | Classic simple overview. |

After importing, select **Prometheus** as the datasource and the `instance`
dropdown should show `patroni1:5432`, `patroni2:5432`, `patroni3:5432`.

### If a Specific Dashboard Still Shows "No data"

1. Check what `job` value the dashboard expects:
   - In Grafana, edit the dashboard → Variables → look at the query for `$job`
   - If it expects something other than `"postgres"`, edit the variable default

2. Check metric names — some dashboards use older metric name formats.
   Open Prometheus at http://localhost:9091 and search for the metric name
   used in the failing panel to verify it exists.

3. Some older dashboards expect `--auto-discover-databases`, which is deprecated
   and **not** enabled here. Per-database metrics from `pg_stat_database` are still collected.

---

## Upgrade Notes (2026-10-13)

| Component | Before | After |
|-----------|--------|-------|
| PostgreSQL | 17 (floating tag) | 18.6 |
| Patroni | 4.1.0 | 4.1.5 |
| etcd | v3.5.27 | v3.7.2 |
| HAProxy | 3.1 | 3.4.6 |
| postgres_exporter | latest | v0.20.1 |
| Prometheus | latest | v3.15.0 |
| Grafana | latest | 13.2.3 |
| Landing page | (none) | new, python 3.15.0-slim |

PostgreSQL 19 is still in beta, so 18.6 is the newest stable release.

**How the data was carried over.** A new PostgreSQL major version can't open the old data
directory, so the PG17 cluster was dumped (`pg_dumpall --roles-only` + `pg_dump -Fc db1`), the old
`pgdata\` and `etcd-data\` were moved to `backups\pg17-20261013\`, the new cluster
bootstrapped empty, and `db1` was restored (6 tables, 414,000 rows, verified on a replica).
To repeat this for a future major upgrade:

```cmd
docker exec patroni1 pg_dumpall -U postgres -h localhost --roles-only > globals.sql
docker exec patroni1 pg_dump -U postgres -h localhost -Fc db1 > db1.dump
REM stop the stack, move pgdata\ and etcd-data\ aside, change PG_VERSION / PG_IMAGE_TAG
patroni-ha-cluster.cmd setup
docker cp db1.dump <leader>:/tmp/db1.dump
docker exec <leader> pg_restore -U postgres -h localhost -C -d postgres /tmp/db1.dump
```

**Fixes made during the upgrade**

- Patroni containers were attached to a Docker network that no longer existed, so they could
  never reach etcd and nothing could resolve `patroni1-3`. Containers are now recreated cleanly.
- HAProxy exited at startup when backends didn't resolve yet. It now uses Docker's DNS
  (`resolvers docker`, `init-addr last,libc,none`), so it starts in any order and follows IP changes.
- `--enable-v2=false` removed from etcd (the flag no longer exists in 3.6+).
- `pg_hba` switched from `md5` to `scram-sha-256` (md5 passwords are deprecated in PostgreSQL 18).
- `bootstrap.users` removed (ignored since Patroni 4.0, so `appuser` was never created). Setup now
  creates it.
- Grafana panels that showed "No data" because of renamed metrics are fixed (see
  [postgres_exporter collectors](#postgres_exporter-collectors)).
- All images pinned to exact versions; this README updated to match the generated files.
