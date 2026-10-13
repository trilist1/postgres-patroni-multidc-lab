#!/usr/bin/env python3
"""
PostgreSQL HA lab across three simulated data centers.

    DC-A   haproxy-a (+ bgp-a), patroni1, patroni2, etcd1
    DC-B   haproxy-b (+ bgp-b), patroni3, etcd2
    DC-C   etcd3 only (witness: keeps etcd quorum when DC-A or DC-B is lost)
    WAN    edge router (FRR, AS 65000) learns the anycast VIP over BGP

Usage:  lab <command> [args]        (lab.cmd wraps this on Windows)

  setup [--restore FILE]   generate + build + start + post-setup (+ restore a pg_dumpall)
  generate                 write docker-compose.yml and every config file
  build                    build the Patroni, BGP speaker and edge router images
  up | stop | down         start / stop (keep containers) / remove containers (data kept);
                           also starts / stops the drill runner
  status                   cluster, BGP and data-center state
  logs [service]           follow logs
  bgp                      the edge router's BGP table and sessions
  dc-down <a|b|c> [--graceful]  cut a data center's power (SIGKILL; --graceful = orderly stop)
  dc-up <a|b|c>            power it back on
  partition <a|b|c>        cut a data center off the WAN (containers keep running)
  heal <a|b|c>             reconnect it to the WAN
  restore FILE             load a pg_dumpall file into the current primary
  drills [start|stop|status]  the hidden background runner behind the landing page's
                           DC failure buttons (lab up starts it; log: state/drills-runner.log)
  remove [--data]          remove containers and images (--data also deletes data folders)

Everything is configured in SETTINGS / DCS below. Standard library only (Python 3.10+).
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ============================================================================
# CONFIGURATION - the single source of truth. Edit, then run: lab generate
# ============================================================================
SETTINGS = {
    "project": "patroni-mdc",            # compose project name (Docker Desktop group)
    "cluster": "pg-ha-cluster",          # Patroni scope
    # Images (pinned, last reviewed 2026-10-13)
    "pg_image_tag": "18.6",
    "patroni_version": "4.1.5",
    "psycopg2_version": "2.9.13",
    "patroni_image": "patroni-pg18:4.1.5",
    "etcd_image": "gcr.io/etcd-development/etcd:v3.7.2",
    "haproxy_image": "haproxy:3.4.6",
    "exporter_image": "quay.io/prometheuscommunity/postgres-exporter:v0.20.1",
    "prometheus_image": "prom/prometheus:v3.15.0",
    "grafana_image": "grafana/grafana:13.2.3",
    "landing_image": "python:3.15.0-slim",
    "bgp_image": "lab-bgp:exabgp-5.0.14",
    "edge_image": "lab-edge:frr-10.7.1",
    # Credentials (public lab defaults on purpose)
    "pg_superuser": "postgres", "pg_superuser_password": "SuperSecret2026",
    "pg_repl_user": "replicator", "pg_repl_password": "ReplSecret2026",
    "pg_app_user": "appuser", "pg_app_password": "AppSecret2026", "pg_app_db": "appdb",
    "haproxy_stats_user": "admin", "haproxy_stats_password": "HaproxyStats2026",
    "grafana_user": "admin", "grafana_password": "GrafanaAdmin2026",
    "etcd_token": "patroni-etcd-token",
    # Anycast VIP and the edge router
    "vip": "10.99.0.10",                 # write VIP: announced by the DC(s) that can reach the primary
    "vip_read": "10.99.0.11",            # read VIP: announced by every DC with a healthy replica
    "edge_asn": 65000,
    "vip_write_port": 6432,              # localhost:6432 -> VIP:5000 (primary)
    "vip_read_port": 6433,               # localhost:6433 -> VIP:5001 (replicas)
    "med_leader_dc": 50,                 # DC that holds the primary announces this MED
    "med_other_dc": 100,                 # healthy DC without the primary
    # Host ports for the shared services
    "prometheus_port": 9091, "grafana_port": 3000, "landing_port": 9090,
    # Networks
    "wan_subnet": "10.210.0.0/24", "edge_wan_ip": "10.210.0.2",
    "prometheus_wan_ip": "10.210.0.41", "landing_wan_ip": "10.210.0.42", "grafana_wan_ip": "10.210.0.43",
}

# Which node lives where. Host ports keep the single-DC numbering.
DCS = {
    "a": {"name": "DC-A", "subnet": "10.210.1.0/24", "asn": 65001,
          "haproxy": {"name": "haproxy-a", "wan_ip": "10.210.0.11", "ports": (5000, 5001, 7000)},
          "patroni": ["patroni1", "patroni2"], "etcd": ["etcd1"]},
    "b": {"name": "DC-B", "subnet": "10.210.2.0/24", "asn": 65002,
          "haproxy": {"name": "haproxy-b", "wan_ip": "10.210.0.12", "ports": (5100, 5101, 7100)},
          "patroni": ["patroni3"], "etcd": ["etcd2"]},
    "c": {"name": "DC-C (witness)", "subnet": "10.210.3.0/24", "asn": None,
          "haproxy": None, "patroni": [], "etcd": ["etcd3"]},
}
NODE = {   # per-node host ports and WAN addresses
    "patroni1": {"pg": 5433, "api": 8008, "wan_ip": "10.210.0.21", "exporter": ("pg-exporter1", 9187)},
    "patroni2": {"pg": 5434, "api": 8009, "wan_ip": "10.210.0.22", "exporter": ("pg-exporter2", 9188)},
    "patroni3": {"pg": 5435, "api": 8010, "wan_ip": "10.210.0.23", "exporter": ("pg-exporter3", 9189)},
    "etcd1": {"client": 2379, "wan_ip": "10.210.0.31"},
    "etcd2": {"client": 2381, "wan_ip": "10.210.0.32"},
    "etcd3": {"client": 2383, "wan_ip": "10.210.0.33"},
}
S = SETTINGS
ALL_PATRONI = [p for d in DCS.values() for p in d["patroni"]]
ALL_ETCD = [e for d in DCS.values() for e in d["etcd"]]
ETCD_HOSTS = ",".join(f"{e}:2379" for e in ALL_ETCD)
STATE_FILE = BASE / "state" / "lab-state.json"


def dc_of(name):
    for k, d in DCS.items():
        if name in d["patroni"] or name in d["etcd"] or (d["haproxy"] and name in (d["haproxy"]["name"], "bgp-" + k)):
            return k
    for p, n in NODE.items():
        if n.get("exporter", (None,))[0] == name:
            return dc_of(p)
    return None


# ============================================================================
# Small helpers
# ============================================================================
def log(msg, kind="STEP"):
    print(f"[{kind}] {msg}", flush=True)


def run(cmd, check=True, capture=False, quiet=False, input_bytes=None):
    if not quiet:
        print("  $ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=BASE, input=input_bytes,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    if check and r.returncode != 0:
        out = r.stdout.decode(errors="replace") if capture and r.stdout else ""
        raise SystemExit(f"[ERROR] command failed ({r.returncode}): {' '.join(cmd)}\n{out}")
    return r.stdout.decode(errors="replace") if capture else r.returncode


def compose(*args, **kw):
    return run(["docker", "compose", "-p", S["project"], "-f", str(BASE / "docker-compose.yml"), *args], **kw)


def write(rel, text, mode="w"):
    p = BASE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode, encoding="utf-8", newline="\n") as fh:   # LF: read by Linux containers
        fh.write(text)


def http_json(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"down": [], "partitioned": []}


def save_state(st):
    st["updated"] = time.time()
    write("state/lab-state.json", json.dumps(st, indent=2))


# ============================================================================
# YAML emitter (compose only needs dicts, lists and scalars)
# ============================================================================
FLOW_KEYS = {"command", "test"}


def yaml(obj, indent=0):
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v and not (k in FLOW_KEYS and isinstance(v, list)):
                out.append(f"{pad}{k}:")
                out.append(yaml(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {scalar(v)}")
    else:
        for v in obj:
            if isinstance(v, dict):
                first, *rest = yaml(v, indent + 1).split("\n")
                out.append(f"{pad}- {first.strip()}")
                out.extend(rest)
            else:
                out.append(f"{pad}- {scalar(v)}")
    return "\n".join(out)


def scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return json.dumps(v)
    if v == {}:
        return "{}"
    return json.dumps(str(v))


# ============================================================================
# Config files
# ============================================================================
def patroni_yml(name):
    n = NODE[name]
    return f"""scope: {S['cluster']}
name: {name}

restapi:
  listen: 0.0.0.0:8008
  connect_address: {name}:8008

etcd3:
  hosts: {ETCD_HOSTS}

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    # Keep a primary running when etcd is briefly unreachable, as long as it can
    # still reach every other Patroni node (WAN blips between data centers).
    failsafe_mode: true
    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
        max_connections: 200
        max_worker_processes: 8
        wal_level: replica
        hot_standby: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        hot_standby_feedback: "on"
        wal_log_hints: "on"
        wal_keep_size: "512MB"
        shared_buffers: "256MB"
        work_mem: "8MB"
        maintenance_work_mem: "128MB"
        effective_cache_size: "768MB"
        checkpoint_completion_target: 0.9
        default_statistics_target: 100
        logging_collector: "on"
        log_directory: "log"
        log_filename: "postgresql-%Y-%m-%d_%H%M%S.log"
        log_statement: "ddl"
        log_min_duration_statement: 1000
        track_activities: "on"
        track_counts: "on"
        track_io_timing: "on"
        track_functions: "all"
        shared_preload_libraries: "pg_stat_statements"
  initdb:
    - encoding: UTF8
    - data-checksums
  pg_hba:
    - host replication {S['pg_repl_user']} 0.0.0.0/0 scram-sha-256
    - host all all 0.0.0.0/0 scram-sha-256

postgresql:
  listen: 0.0.0.0:5432
  connect_address: {name}:5432
  data_dir: /var/lib/postgresql/data/patroni
  pgpass: /tmp/pgpass0
  authentication:
    replication:
      username: {S['pg_repl_user']}
      password: "{S['pg_repl_password']}"
    superuser:
      username: {S['pg_superuser']}
      password: "{S['pg_superuser_password']}"
  parameters:
    unix_socket_directories: "/var/run/postgresql"
  create_replica_methods:
    - basebackup
  basebackup:
    checkpoint: fast
    max-rate: "100M"

# This node is in {DCS[dc_of(name)]['name']}.
tags:
  dc: {DCS[dc_of(name)]['name']}
"""


def haproxy_cfg():
    servers = "\n".join(f"    server {p} {p}:5432 maxconn 200 check port 8008" for p in ALL_PATRONI)
    hold = "\n".join(f"    hold {k} 10s" for k in ("valid", "other", "refused", "nx", "timeout"))
    return f"""# Shared by haproxy-a (DC-A) and haproxy-b (DC-B): identical configuration in
# both data centers. Each one routes writes to whichever node is primary, wherever it is.
global
    maxconn 2000
    log stdout format raw local0

defaults
    mode tcp
    log global
    retries 3
    timeout connect 10s
    timeout client 30m
    timeout server 30m
    timeout check 5s
    maxconn 1000

# Docker embedded DNS: start before the Patroni containers exist and follow IP changes.
resolvers docker
    nameserver dns1 127.0.0.11:53
    resolve_retries 3
    timeout resolve 1s
    timeout retry 1s
{hold}

listen stats
    mode http
    bind *:7000
    stats enable
    stats uri /
    stats refresh 10s
    stats show-legends
    stats show-node
    stats auth {S['haproxy_stats_user']}:{S['haproxy_stats_password']}

# Built-in Prometheus exporter, scraped by Prometheus (not published to the host).
frontend prometheus
    mode http
    bind *:8404
    http-request use-service prometheus-exporter if {{ path /metrics }}
    no log

listen postgresql_write
    bind *:5000
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions resolvers docker init-addr last,libc,none
{servers}

# Reads: replicas first. If no replica is healthy (e.g. a whole DC is down and only
# the primary is left), fall back to any node that can serve reads, i.e. the primary.
frontend postgresql_read
    bind *:5001
    use_backend postgresql_replicas if {{ nbsrv(postgresql_replicas) gt 0 }}
    default_backend postgresql_read_any

backend postgresql_replicas
    balance roundrobin
    option httpchk GET /replica
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions resolvers docker init-addr last,libc,none
{servers}

backend postgresql_read_any
    balance roundrobin
    option httpchk GET /read-only
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions resolvers docker init-addr last,libc,none
{servers}
"""


def frr_conf():
    peers = [(k, d) for k, d in DCS.items() if d["haproxy"]]
    nbr = "\n".join(f" neighbor {d['haproxy']['wan_ip']} remote-as {d['asn']}\n"
                    f" neighbor {d['haproxy']['wan_ip']} description bgp-{k} ({d['name']})" for k, d in peers)
    af = "\n".join(f"  neighbor {d['haproxy']['wan_ip']} route-map FROM-DC in\n"
                   f"  neighbor {d['haproxy']['wan_ip']} route-map NOTHING out" for k, d in peers)
    return f"""! Edge router - the client side of the lab ("the internet").
! Learns the anycast VIPs from each data center's BGP speaker:
!   {S['vip']}/32 (writes, follows the primary) and {S['vip_read']}/32 (reads).
frr defaults traditional
hostname edge
log stdout informational
!
ip prefix-list VIP seq 10 permit {S['vip']}/32
ip prefix-list VIP seq 20 permit {S['vip_read']}/32
route-map FROM-DC permit 10
 match ip address prefix-list VIP
! The edge only listens: it never advertises anything back into the data centers.
route-map NOTHING deny 10
!
router bgp {S['edge_asn']}
 bgp router-id {S['edge_wan_ip']}
 no bgp ebgp-requires-policy
 ! Compare MED between different data centers (different ASes): the DC holding
 ! the primary announces a lower MED and wins.
 bgp always-compare-med
 timers bgp 3 9
{nbr}
 address-family ipv4 unicast
{af}
 exit-address-family
!
"""


FRR_DAEMONS = """bgpd=yes
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
pathd=no
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
mgmtd_options="  -A 127.0.0.1"
bgpd_options="   -A 127.0.0.1"
"""


def prometheus_yml():
    pat = "\n".join(f'          - "{p}:8008"' for p in ALL_PATRONI)
    exp = "\n".join(f'      - targets: ["{NODE[p]["exporter"][0]}:9187"]\n        labels:\n'
                    f'          cluster: "{S["cluster"]}"\n          pg_node: "{p}"\n'
                    f'          dc: "{DCS[dc_of(p)]["name"]}"' for p in ALL_PATRONI)
    etc = "\n".join(f'          - "{e}:2379"' for e in ALL_ETCD)
    hap = "\n".join(f'      - targets: ["{d["haproxy"]["name"]}:8404"]\n        labels:\n          dc: "{d["name"]}"'
                    for d in DCS.values() if d["haproxy"])
    return f"""global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "patroni"
    scrape_interval: 10s
    static_configs:
      - targets:
{pat}
        labels:
          cluster: "{S['cluster']}"

  # Job "postgres" + instance relabel to patroniN:5432 so community dashboards work.
  - job_name: "postgres"
    scrape_interval: 15s
    static_configs:
{exp}
    relabel_configs:
      - source_labels: [pg_node]
        target_label: instance
        regex: (.+)
        replacement: ${{1}}:5432

  - job_name: "etcd"
    scrape_interval: 15s
    static_configs:
      - targets:
{etc}

  # HAProxy's built-in exporter, one per data center.
  - job_name: "haproxy"
    scrape_interval: 10s
    static_configs:
{hap}

  # BGP / VIP routing and simulated data-center state, served by the landing page.
  - job_name: "lab"
    scrape_interval: 5s
    static_configs:
      - targets: ["landing:8080"]
"""


GRAFANA_DS = """apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: true
"""
GRAFANA_PROVIDER = """apiVersion: 1
providers:
  - name: "default"
    orgId: 1
    folder: "PostgreSQL HA"
    type: file
    disableDeletion: false
    editable: true
    updateIntervalSeconds: 10   # poll: file events don't cross the Windows bind mount
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
"""


def topology():
    """What the landing page needs to know about the layout."""
    return {
        "cluster": S["cluster"],
        "vip": S["vip"], "vip_read": S["vip_read"], "vip_write_port": S["vip_write_port"], "vip_read_port": S["vip_read_port"],
        "edge": {"asn": S["edge_asn"], "wan_ip": S["edge_wan_ip"]},
        "dcs": {k: {"name": d["name"], "asn": d["asn"], "patroni": d["patroni"], "etcd": d["etcd"],
                    "haproxy": d["haproxy"]["name"] if d["haproxy"] else None,
                    "haproxy_ports": list(d["haproxy"]["ports"]) if d["haproxy"] else [],
                    "bgp": f"bgp-{k}" if d["haproxy"] else None,
                    "wan_ip": d["haproxy"]["wan_ip"] if d["haproxy"] else None}
                for k, d in DCS.items()},
        "exporters": {p: NODE[p]["exporter"][0] for p in ALL_PATRONI},
    }


# ============================================================================
# docker-compose.yml
# ============================================================================
def compose_doc():
    svc = {}
    stats_auth = f"{S['haproxy_stats_user']}:{S['haproxy_stats_password']}"
    labels = lambda dc, role: {"lab.dc": dc, "lab.role": role}

    def nets(dc, wan_ip=None, wan=True):
        n = {}
        if wan:
            n["wan"] = {"ipv4_address": wan_ip} if wan_ip else {}
        if dc:
            n[f"dc-{dc}"] = {}
        return n

    initial = ",".join(f"{e}=http://{e}:2380" for e in ALL_ETCD)
    for e in ALL_ETCD:
        dc = dc_of(e)
        svc[e] = {
            "image": S["etcd_image"], "container_name": e, "hostname": e, "restart": "unless-stopped",
            "labels": labels(dc, "etcd"),
            "networks": nets(dc, NODE[e]["wan_ip"]),
            "ports": [f"{NODE[e]['client']}:2379"],
            "volumes": [f"./etcd-data/{e}:/etcd-data"],
            "command": ["/usr/local/bin/etcd", f"--name={e}", "--data-dir=/etcd-data",
                        "--listen-client-urls=http://0.0.0.0:2379", f"--advertise-client-urls=http://{e}:2379",
                        "--listen-peer-urls=http://0.0.0.0:2380", f"--initial-advertise-peer-urls=http://{e}:2380",
                        f"--initial-cluster={initial}", f"--initial-cluster-token={S['etcd_token']}",
                        "--initial-cluster-state=new", "--logger=zap", "--log-level=info"],
            "healthcheck": {"test": ["CMD", "/usr/local/bin/etcdctl", "endpoint", "health", "--endpoints=http://localhost:2379"],
                            "interval": "10s", "timeout": "5s", "retries": 5},
        }
    for p in ALL_PATRONI:
        dc = dc_of(p)
        svc[p] = {
            "image": S["patroni_image"], "container_name": p, "hostname": p, "restart": "unless-stopped",
            "labels": labels(dc, "patroni"),
            "networks": nets(dc, NODE[p]["wan_ip"]),
            "ports": [f"{NODE[p]['pg']}:5432", f"{NODE[p]['api']}:8008"],
            "volumes": [f"./pgdata/{p}:/var/lib/postgresql/data", f"./config/{p}.yml:/etc/patroni/patroni.yml:ro",
                        "./certs:/etc/patroni/certs:ro"],
            "environment": [f"PATRONI_NAME={p}", f"ETCD_HOSTS={ETCD_HOSTS}"],
            "depends_on": {e: {"condition": "service_healthy"} for e in DCS[dc]["etcd"]},
        }
    for k, d in DCS.items():
        h = d["haproxy"]
        if not h:
            continue
        w, r, st = h["ports"]
        svc[h["name"]] = {
            "image": S["haproxy_image"], "container_name": h["name"], "hostname": h["name"], "restart": "unless-stopped",
            "labels": labels(k, "haproxy"),
            "networks": nets(k, h["wan_ip"]),
            "ports": [f"{w}:5000", f"{r}:5001", f"{st}:7000"],
            "volumes": ["./config/haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro"],
            "depends_on": d["patroni"] or [],
        }
        svc[f"bgp-{k}"] = {
            "image": S["bgp_image"], "container_name": f"bgp-{k}", "restart": "unless-stopped",
            "labels": labels(k, "bgp"),
            # Same network namespace as this DC's HAProxy: it owns the VIP on lo and
            # peers with the edge router from HAProxy's WAN address.
            "network_mode": f"service:{h['name']}",
            "cap_add": ["NET_ADMIN"],
            "environment": [f"SPEAKER_NAME=bgp-{k}", f"DC_NAME={d['name']}", f"VIP={S['vip']}", f"VIP_READ={S['vip_read']}",
                            f"LOCAL_IP={h['wan_ip']}", f"LOCAL_AS={d['asn']}",
                            f"PEER_IP={S['edge_wan_ip']}", f"PEER_AS={S['edge_asn']}",
                            f"LOCAL_NODES={','.join(d['patroni'])}", f"ALL_NODES={','.join(ALL_PATRONI)}",
                            f"MED_LEADER={S['med_leader_dc']}", f"MED_OTHER={S['med_other_dc']}",
                            f"HAPROXY_STATS_AUTH={stats_auth}"],
            "volumes": ["./state/bgp:/bgp-status"],
            "depends_on": {h["name"]: {"condition": "service_started", "restart": True}},
        }
    svc["edge"] = {
        "image": S["edge_image"], "container_name": "edge", "hostname": "edge", "restart": "unless-stopped",
        "labels": labels("wan", "edge"),
        "networks": {"wan": {"ipv4_address": S["edge_wan_ip"]}},
        "ports": [f"{S['vip_write_port']}:{S['vip_write_port']}", f"{S['vip_read_port']}:{S['vip_read_port']}"],
        "cap_add": ["NET_ADMIN", "NET_RAW", "SYS_ADMIN"],
        "sysctls": {"net.ipv4.ip_forward": 1},
        "environment": [f"VIP={S['vip']}", f"VIP_READ={S['vip_read']}", f"VIP_WRITE_PORT={S['vip_write_port']}", f"VIP_READ_PORT={S['vip_read_port']}"],
        "volumes": ["./config/edge/frr.conf:/etc/frr/frr.conf:ro", "./config/edge/daemons:/etc/frr/daemons:ro",
                    "./config/edge/vtysh.conf:/etc/frr/vtysh.conf:ro", "./state/bgp:/bgp-status"],
    }
    dsn = f"postgresql://{S['pg_superuser']}:{S['pg_superuser_password']}@{{}}:5432/postgres?sslmode=disable"
    for p in ALL_PATRONI:
        name, port = NODE[p]["exporter"]
        dc = dc_of(p)
        svc[name] = {
            "image": S["exporter_image"], "container_name": name, "restart": "unless-stopped",
            "labels": labels(dc, "exporter"),
            "networks": nets(dc, wan=False),     # local to its data center
            "ports": [f"{port}:9187"],
            "environment": [f"DATA_SOURCE_NAME={dsn.format(p)}"],
            "command": ["--collector.stat_checkpointer", "--collector.stat_statements",
                        "--collector.stat_statements.include_query", "--collector.long_running_transactions"],
            "depends_on": [p],
        }
    every_dc = {f"dc-{k}": {} for k in DCS}
    svc["prometheus"] = {
        "image": S["prometheus_image"], "container_name": "prometheus", "restart": "unless-stopped",
        "labels": labels("mgmt", "monitoring"),
        "networks": {"wan": {"ipv4_address": S["prometheus_wan_ip"]}, **every_dc},
        "ports": [f"{S['prometheus_port']}:9090"],
        "volumes": ["./config/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro", "./prometheus-data:/prometheus"],
    }
    svc["grafana"] = {
        "image": S["grafana_image"], "container_name": "grafana", "restart": "unless-stopped",
        "labels": labels("mgmt", "monitoring"),
        "networks": {"wan": {"ipv4_address": S["grafana_wan_ip"]}},
        "ports": [f"{S['grafana_port']}:3000"],
        "environment": [f"GF_SECURITY_ADMIN_USER={S['grafana_user']}", f"GF_SECURITY_ADMIN_PASSWORD={S['grafana_password']}",
                        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/pg-ha-overview.json"],
        "volumes": ["./config/grafana/provisioning:/etc/grafana/provisioning:ro",
                    "./config/grafana/dashboards:/var/lib/grafana/dashboards:ro", "./grafana-data:/var/lib/grafana"],
        "depends_on": ["prometheus"],
    }
    svc["landing"] = {
        "image": S["landing_image"], "container_name": "landing", "restart": "unless-stopped",
        "labels": labels("mgmt", "landing"),
        # Management station: reaches every DC directly, so it can show a partitioned DC.
        "networks": {"wan": {"ipv4_address": S["landing_wan_ip"]}, **every_dc},
        # localhost only: the page can trigger switchovers and data-center failures
        "ports": [f"127.0.0.1:{S['landing_port']}:8080"],
        "environment": [f"HAPROXY_STATS_AUTH={stats_auth}"],
        # state/ is read-only except state/requests, where the DC failure buttons queue
        # work for `lab drills` on the host.
        "volumes": ["./landing:/app:ro", "./config:/srv/config:ro", "./patroni-image:/srv/patroni-image:ro",
                    "./bgp:/srv/bgp:ro", "./edge:/srv/edge:ro", "./state:/srv/state:ro",
                    "./state/requests:/srv/state/requests",
                    "./docker-compose.yml:/srv/docker-compose.yml:ro"],
        "command": ["python", "-u", "/app/server.py"],
        "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"],
                        "interval": "15s", "timeout": "5s", "retries": 3},
    }
    networks = {"wan": {"ipam": {"config": [{"subnet": S["wan_subnet"]}]}}}
    for k, d in DCS.items():
        networks[f"dc-{k}"] = {"ipam": {"config": [{"subnet": d["subnet"]}]}}
    return {"name": S["project"], "services": svc, "networks": networks}


COMPOSE_HEADER = """# GENERATED by lab.py - edit SETTINGS/DCS in lab.py and run `lab generate`.
#
# Three simulated data centers on their own Docker networks, joined by a "wan"
# network. Host paths are relative to this file, so the folder can live anywhere.
#   DC-A: haproxy-a + bgp-a, patroni1, patroni2, etcd1
#   DC-B: haproxy-b + bgp-b, patroni3, etcd2
#   DC-C: etcd3 (witness)
#   WAN : edge router (FRR) - localhost:6432/6433 -> anycast VIP
"""


# ============================================================================
# Commands
# ============================================================================
CERT_SCRIPT = r"""#!/bin/sh
set -e
cd /certs
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -subj "/C=GB/ST=Hertfordshire/L=Stevenage/O=PatroniCluster/CN=PatroniCA" -out ca.crt
sign() { openssl genrsa -out $1.key 2048; openssl req -new -key $1.key -subj "/CN=$2" -addext "subjectAltName=$3" -out $1.csr; openssl x509 -req -in $1.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out $1.crt -days 3650 -sha256 -copy_extensions copyall; }
sign etcd-server etcd "DNS:etcd1,DNS:etcd2,DNS:etcd3,DNS:localhost,IP:127.0.0.1"
sign etcd-peer etcd-peer "DNS:etcd1,DNS:etcd2,DNS:etcd3,DNS:localhost,IP:127.0.0.1"
sign etcd-client etcd-client "DNS:patroni1,DNS:patroni2,DNS:patroni3,DNS:localhost,IP:127.0.0.1"
sign pg-server postgresql "DNS:patroni1,DNS:patroni2,DNS:patroni3,DNS:haproxy-a,DNS:haproxy-b,DNS:localhost,IP:127.0.0.1"
sign haproxy haproxy "DNS:haproxy-a,DNS:haproxy-b,DNS:localhost,IP:127.0.0.1"
cat haproxy.crt haproxy.key > haproxy-combined.pem
rm -f *.csr gen.sh
chmod 600 *.key haproxy-combined.pem
"""


def reload_grafana():
    """Ask a running Grafana to re-read the provisioned dashboards.

    Grafana doesn't notice file changes through a Windows bind mount, so without
    this a regenerated dashboard only appears after a Grafana restart.
    """
    import base64
    req = urllib.request.Request(f"http://localhost:{S['grafana_port']}/api/admin/provisioning/dashboards/reload",
                                 method="POST", data=b"")
    auth = base64.b64encode(f"{S['grafana_user']}:{S['grafana_password']}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    try:
        urllib.request.urlopen(req, timeout=3)
        log("Grafana reloaded the dashboards", "OK")
    except Exception:
        pass   # Grafana not running yet: it loads them when it starts


def cmd_generate(_args=None):
    log("Creating folders")
    for d in ["config/haproxy", "config/prometheus", "config/edge", "config/landing",
              "config/grafana/provisioning/datasources", "config/grafana/provisioning/dashboards",
              "config/grafana/dashboards", "state/bgp", "state/requests/results", "prometheus-data", "grafana-data", "certs",
              *[f"pgdata/{p}" for p in ALL_PATRONI], *[f"etcd-data/{e}" for e in ALL_ETCD]]:
        (BASE / d).mkdir(parents=True, exist_ok=True)
    if not (BASE / "certs" / "ca.crt").exists():
        log("Generating TLS certificates")
        write("certs/gen.sh", CERT_SCRIPT)
        run(["docker", "run", "--rm", "-v", f"{BASE / 'certs'}:/certs", "alpine:3.22",
             "sh", "-c", "apk add --no-cache openssl >/dev/null && sh /certs/gen.sh"])
    else:
        log("Certificates exist, keeping them (delete certs\\ to regenerate)", "SKIP")
    log("Writing configs")
    for p in ALL_PATRONI:
        write(f"config/{p}.yml", patroni_yml(p))
    write("config/haproxy/haproxy.cfg", haproxy_cfg())
    write("config/edge/frr.conf", frr_conf())
    write("config/edge/daemons", FRR_DAEMONS)
    write("config/edge/vtysh.conf", "service integrated-vtysh-config\n")
    write("config/prometheus/prometheus.yml", prometheus_yml())
    write("config/grafana/provisioning/datasources/datasource.yml", GRAFANA_DS)
    write("config/grafana/provisioning/dashboards/provider.yml", GRAFANA_PROVIDER)
    write("config/landing/topology.json", json.dumps(topology(), indent=2))
    log("Generating Grafana dashboards")
    r = subprocess.run([sys.executable, str(BASE / "grafana_dashboards.py"),
                        str(BASE / "config/grafana/dashboards")], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"dashboard generation failed: {r.stderr.strip()[:200]}", "WARN")
    reload_grafana()
    write("docker-compose.yml", COMPOSE_HEADER + yaml(compose_doc()) + "\n")
    if not STATE_FILE.exists():
        save_state({"down": [], "partitioned": []})
    log("Generated docker-compose.yml and all configs", "OK")


def cmd_build(_args=None):
    log("Building images")
    run(["docker", "build", "-t", S["patroni_image"],
         "--build-arg", f"PG_IMAGE_TAG={S['pg_image_tag']}", str(BASE / "patroni-image")])
    run(["docker", "build", "-t", S["bgp_image"], str(BASE / "bgp")])
    run(["docker", "build", "-t", S["edge_image"], str(BASE / "edge")])
    log("Images built", "OK")


def find_leader(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        for p in ALL_PATRONI:
            try:
                for m in http_json(f"http://localhost:{NODE[p]['api']}/cluster").get("members", []):
                    if m.get("role") == "leader":
                        return m["name"]
            except Exception:
                pass
        time.sleep(3)
    return None


def psql(node, sql, check=True):
    return run(["docker", "exec", node, "psql", "-U", S["pg_superuser"], "-h", "localhost", "-Atc", sql],
               check=check, capture=True, quiet=True)


def cmd_post_setup(_args=None):
    log("Waiting for a Patroni leader")
    leader = find_leader()
    if not leader:
        raise SystemExit("[ERROR] no leader after 120 s - check: lab status")
    log(f"{leader} is the leader. Creating extension, app user and app database", "OK")
    psql(leader, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;")
    if psql(leader, f"SELECT 1 FROM pg_roles WHERE rolname='{S['pg_app_user']}'").strip() != "1":
        psql(leader, f"CREATE ROLE {S['pg_app_user']} LOGIN PASSWORD '{S['pg_app_password']}';")
    if psql(leader, f"SELECT 1 FROM pg_database WHERE datname='{S['pg_app_db']}'").strip() != "1":
        psql(leader, f"CREATE DATABASE {S['pg_app_db']} OWNER {S['pg_app_user']};")
    return leader


def cmd_restore(args):
    if not args:
        raise SystemExit("usage: lab restore <pg_dumpall.sql>")
    f = Path(args[0]).resolve()
    if not f.exists():
        raise SystemExit(f"[ERROR] {f} not found")
    leader = find_leader()
    log(f"Restoring {f.name} into {leader} (errors about existing roles are expected)")
    run(["docker", "cp", str(f), f"{leader}:/tmp/restore.sql"])
    run(["docker", "exec", leader, "psql", "-U", S["pg_superuser"], "-h", "localhost", "-q",
         "-v", "ON_ERROR_STOP=0", "-f", "/tmp/restore.sql"], check=False, capture=True)
    run(["docker", "exec", leader, "rm", "-f", "/tmp/restore.sql"], check=False, quiet=True)
    dbs = psql(leader, "select string_agg(datname, ', ' order by datname) from pg_database where not datistemplate")
    log(f"Databases now: {dbs.strip()}", "OK")


def cmd_setup(args):
    cmd_generate()
    cmd_build()
    log("Starting all services")
    compose("up", "-d")
    cmd_post_setup()
    if args and args[0] == "--restore":
        cmd_restore(args[1:])
    drills_start()
    print(f"""
==================================================================
 SETUP COMPLETE
==================================================================
 Landing page      http://localhost:{S['landing_port']}
 VIP (via BGP)     localhost:{S['vip_write_port']} write, localhost:{S['vip_read_port']} read
 DC-A HAProxy      localhost:5000 write, :5001 read, http://localhost:7000 stats
 DC-B HAProxy      localhost:5100 write, :5101 read, http://localhost:7100 stats
 Grafana           http://localhost:{S['grafana_port']}  ({S['grafana_user']}/{S['grafana_password']})
 Prometheus        http://localhost:{S['prometheus_port']}
 DC failure drills buttons on the landing page (runner started in the background)
                   or from the CLI: lab dc-down a / lab dc-up a / lab partition b / lab heal b
""")


def containers_of(dc, all_states=True):
    out = run(["docker", "ps", *(["-a"] if all_states else []), "--format", "{{.Names}}",
               "--filter", f"label=com.docker.compose.project={S['project']}",
               "--filter", f"label=lab.dc={dc}"], capture=True, quiet=True)
    names = [n for n in out.split() if n]
    # bgp-x shares haproxy-x's network namespace: start it after, stop it before.
    return sorted(names, key=lambda n: (n.startswith("bgp-"), n))


def need_dc(args):
    if not args or args[0].lower() not in DCS:
        raise SystemExit(f"usage: lab <dc-down|dc-up|partition|heal> <{'|'.join(DCS)}>")
    return args[0].lower()


def cmd_dc_down(args):
    dc = need_dc(args)
    names = containers_of(dc)
    if "--graceful" in args:
        # Orderly shutdown: Patroni releases its leader lock, so failover is fast.
        log(f"Shutting down {DCS[dc]['name']} gracefully: {', '.join(names)}")
        run(["docker", "stop", "-t", "5", *reversed(names)], quiet=True)
    else:
        # Power cut (SIGKILL): nothing gets to clean up, the other DCs must wait for
        # the leader lock TTL and the BGP hold timer. This is the realistic worst case.
        log(f"Cutting power to {DCS[dc]['name']}: {', '.join(names)}")
        run(["docker", "kill", *reversed(names)], quiet=True, capture=True)
    st = load_state()
    st["down"] = sorted(set(st.get("down", [])) | {dc})
    save_state(st)
    log(f"{DCS[dc]['name']} is down. Watch the failover: lab status (or the landing page)", "OK")


def cmd_dc_up(args):
    dc = need_dc(args)
    names = containers_of(dc)
    log(f"Powering on {DCS[dc]['name']}: {', '.join(names)}")
    for n in names:              # haproxy before its bgp sidecar
        run(["docker", "start", n], quiet=True, capture=True)
    st = load_state()
    st["down"] = [d for d in st.get("down", []) if d != dc]
    save_state(st)
    what = ("Its PostgreSQL nodes rejoin as replicas (pg_rewind if needed)" if DCS[dc]["patroni"]
            else "etcd rejoins the quorum")
    log(f"{DCS[dc]['name']} is back. {what}", "OK")


def wan_members(dc):
    """Containers of a DC that are attached to the WAN, with their WAN IP."""
    d = DCS[dc]
    members = [(n, NODE[n]["wan_ip"]) for n in d["patroni"] + d["etcd"]]
    if d["haproxy"]:
        members.append((d["haproxy"]["name"], d["haproxy"]["wan_ip"]))
    return members


def cmd_partition(args):
    dc = need_dc(args)
    net = f"{S['project']}_wan"
    log(f"Cutting {DCS[dc]['name']} off the WAN (its containers keep running and still see each other)")
    for n, _ip in wan_members(dc):
        run(["docker", "network", "disconnect", "-f", net, n], check=False, quiet=True, capture=True)
    st = load_state()
    st["partitioned"] = sorted(set(st.get("partitioned", [])) | {dc})
    save_state(st)
    log(f"{DCS[dc]['name']} is partitioned. Expect its primary to step down and the other side to take over", "OK")


def cmd_heal(args):
    dc = need_dc(args)
    net = f"{S['project']}_wan"
    log(f"Reconnecting {DCS[dc]['name']} to the WAN")
    for n, ip in wan_members(dc):
        run(["docker", "network", "connect", "--ip", ip, net, n], check=False, quiet=True, capture=True)
    # The BGP speaker shares HAProxy's namespace; restart it so it re-peers promptly.
    if DCS[dc]["haproxy"]:
        run(["docker", "restart", "-t", "2", f"bgp-{dc}"], check=False, quiet=True, capture=True)
    st = load_state()
    st["partitioned"] = [d for d in st.get("partitioned", []) if d != dc]
    save_state(st)
    log(f"{DCS[dc]['name']} is reconnected", "OK")


def cmd_bgp(_args=None):
    print(run(["docker", "exec", "edge", "vtysh", "-c", "show bgp ipv4 unicast summary",
               "-c", f"show bgp ipv4 unicast {S['vip']}/32", "-c", f"show bgp ipv4 unicast {S['vip_read']}/32",
               "-c", f"show ip route {S['vip']}", "-c", f"show ip route {S['vip_read']}"],
              check=False, capture=True, quiet=True))


def cmd_status(_args=None):
    st = load_state()
    print("\n--- Data centers ---")
    for k, d in DCS.items():
        flags = (["POWERED OFF"] if k in st.get("down", []) else []) + (["PARTITIONED"] if k in st.get("partitioned", []) else [])
        print(f"  {d['name']:15} {', '.join(d['patroni'] + d['etcd'] + ([d['haproxy']['name']] if d['haproxy'] else [])):40} {' '.join(flags) or 'ok'}")
    print("\n--- Patroni ---")
    for p in ALL_PATRONI:
        if p not in containers_of(dc_of(p), all_states=False):
            continue
        print(run(["docker", "exec", p, "patronictl", "-c", "/etc/patroni/patroni.yml", "list"],
                  check=False, capture=True, quiet=True))
        break
    else:
        print("  no Patroni node is running")
    print("--- BGP (edge router) ---")
    cmd_bgp()


def cmd_remove(args):
    if input("Type YES to remove all containers and the built images: ").strip() != "YES":
        return print("Cancelled.")
    compose("down", "--remove-orphans", check=False)
    for img in (S["patroni_image"], S["bgp_image"], S["edge_image"]):
        run(["docker", "rmi", img], check=False, quiet=True, capture=True)
    if "--data" in args and input("Also DELETE pgdata, etcd-data, prometheus-data, grafana-data, state? Type YES: ").strip() == "YES":
        for d in ("pgdata", "etcd-data", "prometheus-data", "grafana-data", "state"):
            shutil.rmtree(BASE / d, ignore_errors=True)
        log("Data deleted", "OK")


# ============================================================================
# Drill runner: carries out the landing page's data-center failure buttons
# ============================================================================
# The landing page can't control Docker (no container gets the Docker socket).
# Its buttons drop a request file into state/requests/; this loop, running on the
# host with your Docker CLI, picks it up and runs the same code as the CLI
# commands, then writes the result next to it. It only knows these actions:
DRILLS = {
    "power-off": lambda dc: cmd_dc_down([dc]),
    "power-off-graceful": lambda dc: cmd_dc_down([dc, "--graceful"]),
    "power-on": lambda dc: cmd_dc_up([dc]),
    "partition": lambda dc: cmd_partition([dc]),
    "heal": lambda dc: cmd_heal([dc]),
}
REQ_DIR = BASE / "state" / "requests"
RUNNER_FILE = BASE / "state" / "drills-runner.json"


def _runner_beat(busy=None, last=None, started=None):
    st = {"pid": os.getpid(), "started": started, "updated": time.time(), "busy": busy, "last": last}
    tmp = RUNNER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, RUNNER_FILE)


STOP_FILE = BASE / "state" / "drills-runner.stop"
RUNNER_LOG = BASE / "state" / "drills-runner.log"


def runner_alive():
    try:
        return time.time() - float(json.loads(RUNNER_FILE.read_text())["updated"]) < 6
    except (OSError, ValueError, KeyError):
        return False


def drills_start(quiet=False):
    """Start the runner in the background: no window, survives this terminal."""
    if runner_alive():
        return quiet or log("Drill runner is already running", "OK")
    (BASE / "state").mkdir(exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    logf = open(RUNNER_LOG, "w", encoding="utf-8")
    kw = {"cwd": BASE, "stdin": subprocess.DEVNULL, "stdout": logf, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        # A hidden console of its own: no window, and the docker.exe calls it makes
        # don't flash windows either. Detached from the terminal that started it.
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "drills", "run"], **kw)
    for _ in range(20):
        if runner_alive():
            return quiet or log(f"Drill runner started in the background (log: state\\{RUNNER_LOG.name})", "OK")
        time.sleep(0.5)
    log(f"Drill runner didn't report in; see state\\{RUNNER_LOG.name}", "WARN")


def drills_stop(quiet=False):
    if not runner_alive():
        RUNNER_FILE.unlink(missing_ok=True)
        return quiet or log("Drill runner isn't running", "OK")
    STOP_FILE.write_text("stop")                  # the loop checks this every second
    for _ in range(10):
        if not runner_alive() and not RUNNER_FILE.exists():
            return quiet or log("Drill runner stopped", "OK")
        time.sleep(0.5)
    try:                                           # still there: end it by PID
        pid = int(json.loads(RUNNER_FILE.read_text())["pid"])
        if os.name == "nt":
            run(["taskkill", "/PID", str(pid), "/F"], check=False, quiet=True, capture=True)
        else:
            os.kill(pid, 15)
    except (OSError, ValueError, KeyError):
        pass
    RUNNER_FILE.unlink(missing_ok=True)
    quiet or log("Drill runner stopped", "OK")


def cmd_drills(args):
    sub = args[0] if args else "status"
    if sub == "start":
        return drills_start()
    if sub == "stop":
        return drills_stop()
    if sub == "status":
        if runner_alive():
            st = json.loads(RUNNER_FILE.read_text())
            return log(f"Drill runner running (pid {st['pid']}), busy: {st.get('busy') or 'no'}. Log: state\\{RUNNER_LOG.name}", "OK")
        return log("Drill runner isn't running. Start it: lab drills start", "INFO")
    if sub != "run":
        raise SystemExit("usage: lab drills [start|stop|status|run]")
    # --- the loop itself (lab drills run: foreground, useful for debugging) ---
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    (REQ_DIR / "results").mkdir(exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    started = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] drill runner {os.getpid()} waiting for requests from "
          f"http://localhost:{S['landing_port']}", flush=True)
    last = None
    try:
        while True:
            if STOP_FILE.exists():
                STOP_FILE.unlink(missing_ok=True)
                break
            try:                                   # another runner took over: let it
                cur = json.loads(RUNNER_FILE.read_text())
                if cur.get("pid") != os.getpid() and time.time() - float(cur.get("updated", 0)) < 3 \
                        and float(cur.get("started") or 0) > started:
                    print("another drill runner started; exiting", flush=True)
                    return
            except (OSError, ValueError):
                pass
            _runner_beat(last=last, started=started)
            for f in sorted(REQ_DIR.glob("*.json")):
                try:
                    req = json.loads(f.read_text())
                    f.unlink()                                # claim it
                except (OSError, ValueError):
                    continue
                rid, dc, action = str(req.get("id", ""))[:40], req.get("dc"), req.get("action")
                result = {"id": rid, "dc": dc, "action": action, "started": time.time()}
                if dc not in DCS or action not in DRILLS or not rid.replace("-", "").isalnum():
                    result.update(ok=False, message="Rejected: unknown data center or action")
                else:
                    print(f"\n[{time.strftime('%H:%M:%S')}] {action} {DCS[dc]['name']} (requested from the landing page)", flush=True)
                    _runner_beat(busy=f"{action} {dc}", last=last, started=started)
                    try:
                        DRILLS[action](dc)
                        result.update(ok=True, message=f"Done: {action.replace('-', ' ')} {DCS[dc]['name']}")
                    except (SystemExit, Exception) as e:
                        result.update(ok=False, message=f"Failed: {e}")
                result["finished"] = time.time()
                last = result
                (REQ_DIR / "results" / f"{rid or 'invalid'}.json").write_text(json.dumps(result))
                _runner_beat(last=last, started=started)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    RUNNER_FILE.unlink(missing_ok=True)
    print(f"[{time.strftime('%H:%M:%S')}] drill runner stopped", flush=True)


def cmd_up(_args=None):
    compose("up", "-d")
    drills_start()


def cmd_stop(_args=None):
    drills_stop()
    compose("stop")


def cmd_down(_args=None):
    drills_stop()
    compose("down")


COMMANDS = {
    "setup": cmd_setup, "generate": cmd_generate, "build": cmd_build, "drills": cmd_drills,
    "up": cmd_up, "start": cmd_up, "stop": cmd_stop, "down": cmd_down,
    "status": cmd_status, "logs": lambda a: compose("logs", "-f", "--tail", "100", *a, check=False),
    "bgp": cmd_bgp, "dc-down": cmd_dc_down, "dc-up": cmd_dc_up,
    "partition": cmd_partition, "heal": cmd_heal,
    "restore": cmd_restore, "post-setup": cmd_post_setup, "remove": cmd_remove,
}


def menu():
    items = [("setup", "Full setup (generate, build, start)"), ("status", "Status"), ("bgp", "BGP table"),
             ("drills status", "Drill runner status (landing page DC buttons)"),
             ("dc-down a", "Power off DC-A"), ("dc-up a", "Power on DC-A"),
             ("dc-down b", "Power off DC-B"), ("dc-up b", "Power on DC-B"),
             ("partition a", "Partition DC-A from the WAN"), ("heal a", "Heal DC-A"),
             ("stop", "Stop all"), ("up", "Start all")]
    while True:
        print("\n PostgreSQL HA lab - three data centers\n")
        for i, (_, label) in enumerate(items, 1):
            print(f"  {i:2}. {label}")
        print("   0. Exit\n")
        c = input("  Select: ").strip()
        if c in ("0", ""):
            return
        if c.isdigit() and 1 <= int(c) <= len(items):
            name, *rest = items[int(c) - 1][0].split()
            COMMANDS[name](rest)


def main(argv):
    if not argv:
        return menu()
    name, args = argv[0], argv[1:]
    if name in ("-h", "--help", "help") or name not in COMMANDS:
        print(__doc__)
        return
    COMMANDS[name](args)


if __name__ == "__main__":
    main(sys.argv[1:])
