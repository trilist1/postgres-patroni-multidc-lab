#!/usr/bin/env python3
"""
Landing page for the PostgreSQL HA cluster.

Runs inside the compose network so it can probe every service directly:
  GET /                    -> index.html (topology diagram + service table)
  GET /api/status          -> live health of all 13 services (JSON)
  GET /api/config/<name>   -> compose definition, mounted config files and
                              live runtime config for one service (JSON)
                              add ?reveal=1 to show passwords
  GET /api/history         -> Patroni leader history (timelines), newest first
  POST /api/switchover     -> {"candidate": "patroniN"} planned leader change
  POST /api/failover       -> {"candidate": "patroniN"} emergency promotion
                              (POSTs must come from this page: Origin check)

Standard library only - no pip install needed.
Paths are read-only bind mounts set up in docker-compose.yml:
  /srv/docker-compose.yml, /srv/config, /srv/patroni-image, /app
"""
import base64
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SRV = Path(os.environ.get("SRV_DIR", "/srv"))
COMPOSE_FILE = SRV / "docker-compose.yml"
CONFIG_DIR = SRV / "config"
IMAGE_DIR = SRV / "patroni-image"
PORT = int(os.environ.get("LANDING_LISTEN_PORT", "8080"))
HAPROXY_AUTH = os.environ.get("HAPROXY_STATS_AUTH", "")  # user:password, set in docker-compose.yml
TIMEOUT = 2.0

STATE_DIR = SRV / "state"
BGP_STALE_S = 15     # a status file older than this means its writer is down

# Layout comes from lab.py (config/landing/topology.json): which DC holds what.
TOPO = json.loads((CONFIG_DIR / "landing" / "topology.json").read_text())
DCS = TOPO["dcs"]
ETCD = [e for d in DCS.values() for e in d["etcd"]]
PATRONI = [p for d in DCS.values() for p in d["patroni"]]
EXPORTERS = [TOPO["exporters"][p] for p in PATRONI]
HAPROXIES = [d["haproxy"] for d in DCS.values() if d["haproxy"]]
SPEAKERS = [d["bgp"] for d in DCS.values() if d["bgp"]]
DC_OF = {n: k for k, d in DCS.items()
         for n in d["patroni"] + d["etcd"] + [x for x in (d["haproxy"], d["bgp"]) if x]}
DC_OF.update({TOPO["exporters"][p]: DC_OF[p] for p in PATRONI})
GW_DC = {d["wan_ip"]: k for k, d in DCS.items() if d["wan_ip"]}   # BGP next hop -> DC

# Which mounted files belong to which service (relative to /srv)
SERVICE_FILES = {
    **{p: [f"config/{p}.yml", "patroni-image/Dockerfile", "patroni-image/entrypoint.sh"] for p in PATRONI},
    **{h: ["config/haproxy/haproxy.cfg"] for h in HAPROXIES},
    **{b: ["bgp/health.py", "bgp/entrypoint.sh", "bgp/Dockerfile"] for b in SPEAKERS},
    "edge": ["config/edge/frr.conf", "config/edge/daemons", "edge/entrypoint.sh", "edge/status.sh", "edge/Dockerfile"],
    "prometheus": ["config/prometheus/prometheus.yml"],
    "grafana": ["config/grafana/provisioning/datasources/datasource.yml",
                "config/grafana/provisioning/dashboards/provider.yml"],
    "landing": ["app/server.py"],
}

SERVICE_INFO = {
    "etcd": "Distributed key-value store. Holds Patroni's leader lock and cluster state (Raft, 2 of 3 for quorum).",
    "patroni": "PostgreSQL managed by Patroni. One leader accepts writes; replicas stream WAL from it.",
    "haproxy": "Routes port 5000 to the current primary (in any DC) and port 5001 to replicas, falling back to the primary when no replica is left. Same config in every DC.",
    "bgp": "ExaBGP speaker sharing this DC's HAProxy network namespace. Announces the write VIP while HAProxy reaches the primary (lower MED when the primary is in this DC) and the read VIP while it reaches a replica.",
    "edge": "Edge router (FRR, BGP). Learns the VIPs from every DC, picks the lowest MED, and forwards localhost:6432/6433 to the winning DC's HAProxy.",
    "pg-exporter": "Turns PostgreSQL statistics views into Prometheus metrics for one node.",
    "prometheus": "Scrapes Patroni, etcd and the exporters every 10-15 seconds and stores the time series.",
    "grafana": "Dashboards on top of Prometheus. The PostgreSQL HA dashboard is the home page.",
    "landing": "This page.",
}


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def fetch(url, method="GET", body=None, auth=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, method=method, data=body)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_json(url, **kw):
    code, text = fetch(url, **kw)
    return code, json.loads(text)


# --------------------------------------------------------------------------
# Probes - each returns {"status": up|degraded|down, "summary": str, ...}
# --------------------------------------------------------------------------
def probe_etcd(name):
    _, health = fetch_json(f"http://{name}:2379/health")
    _, st = fetch_json(f"http://{name}:2379/v3/maintenance/status", method="POST", body=b"{}")
    is_leader = st.get("leader") == st.get("header", {}).get("member_id")
    ok = health.get("health") == "true"
    return {
        "status": "up" if ok else "degraded",
        "role": "raft leader" if is_leader else "raft follower",
        "version": st.get("version"),
        "summary": f"{'Raft leader' if is_leader else 'Follower'}, DB {int(st.get('dbSize', 0)) // 1024} KB",
    }


def probe_patroni(name):
    code, p = fetch_json(f"http://{name}:8008/patroni")
    role = p.get("role", "unknown")
    state = p.get("state", "unknown")
    healthy = state == "running" and (role == "primary" or p.get("replication_state") == "streaming")
    ver = p.get("server_version", 0)
    return {
        "status": "up" if healthy else ("degraded" if state == "running" else "down"),
        "role": "leader" if role in ("primary", "master") else role,
        "state": p.get("replication_state") or state,
        "timeline": p.get("timeline"),
        "version": f"PG {ver // 10000}.{ver % 100}, Patroni {p.get('patroni', {}).get('version')}",
        "pg_version": f"{ver // 10000}.{ver % 100}" if ver else None,
        "pg_state": state,
        "pg_started": p.get("postmaster_start_time"),
        "patroni_version": p.get("patroni", {}).get("version"),
        "dcs_last_seen": p.get("dcs_last_seen"),
        "summary": (f"PostgreSQL {'primary' if role == 'primary' else 'replica'}, "
                    f"{'read-write' if role == 'primary' else p.get('replication_state') or state}, "
                    f"timeline {p.get('timeline')}. Patroni agent OK"),
    }


def probe_cluster():
    """Replication lag per member from whichever Patroni node answers first."""
    for n in PATRONI:
        try:
            _, c = fetch_json(f"http://{n}:8008/cluster")
            return {m["name"]: m for m in c.get("members", [])}
        except Exception:
            continue
    return {}


def probe_haproxy(name):
    code, csv = fetch(f"http://{name}:7000/;csv", auth=HAPROXY_AUTH)
    if code != 200:
        raise RuntimeError(f"stats page returned HTTP {code}")
    rows = [l.split(",") for l in csv.splitlines() if l and not l.startswith("#")]
    backends = {"write": {}, "read": {}, "read_any": {}}
    for r in rows:
        px, sv, status = r[0], r[1], r[17]
        key = {"postgresql_write": "write", "postgresql_replicas": "read", "postgresql_read_any": "read_any"}.get(px)
        if key and sv not in ("FRONTEND", "BACKEND"):
            backends[key][sv] = status
    w = [s for s, v in backends["write"].items() if v == "UP"]
    rd = [s for s, v in backends["read"].items() if v == "UP"]
    fallback = not rd and [s for s, v in backends["read_any"].items() if v == "UP"]
    reads = ", ".join(rd) if rd else (f"{', '.join(fallback)} (fallback, no replicas)" if fallback else "nobody")
    return {
        "status": "up" if w and (rd or fallback) else ("degraded" if w or rd or fallback else "down"),
        "backends": backends,
        "summary": f"Writes to {', '.join(w) or 'nobody'}; reads from {reads}",
    }


def read_status_file(name):
    """JSON a sidecar writes into state/bgp/. Raises if missing or stale."""
    p = STATE_DIR / "bgp" / f"{name}.json"
    d = json.loads(p.read_text())
    age = time.time() - float(d.get("updated", 0))
    if age > BGP_STALE_S:
        raise RuntimeError(f"no update for {int(age)} s")
    return d


def probe_bgp(name):
    d = read_status_file(name)
    w, r = d.get("write", {}), d.get("read", {})
    parts = []
    parts.append(f"write VIP MED {w['med']}" if w.get("announcing") else "write VIP withdrawn")
    parts.append(f"read VIP MED {r['med']}" if r.get("announcing") else "read VIP withdrawn")
    return {
        "status": "up" if w.get("announcing") and r.get("announcing") else ("degraded" if w.get("announcing") or r.get("announcing") else "down"),
        "summary": ", ".join(parts),
        "write": w, "read": r, "leader_seen": d.get("leader"),
    }


def _paths(paths_json):
    out = []
    for pth in (paths_json or {}).get("paths", []):
        hop = (pth.get("nexthops") or [{}])[0].get("ip") or pth.get("peer", {}).get("peerId")
        out.append({"dc": GW_DC.get(hop), "next_hop": hop, "med": pth.get("metric"),
                    "best": bool(pth.get("bestpath", {}).get("overall")),
                    "as_path": pth.get("aspath", {}).get("string")})
    return out


def probe_edge():
    d = read_status_file("edge")
    summ = d.get("summary", {})
    peers = summ.get("peers") or summ.get("ipv4Unicast", {}).get("peers", {})   # shape depends on the vtysh command
    sessions = {GW_DC.get(ip, ip): {"state": p.get("state"), "prefixes": p.get("pfxRcd", 0), "up": p.get("peerUptime")}
                for ip, p in peers.items()}

    def route(kernel):
        gw = (kernel or [{}])[0].get("gateway") if kernel else None
        return GW_DC.get(gw)

    write_dc, read_dc = route(d.get("kernel")), route(d.get("read_kernel"))
    name = lambda k: DCS[k]["name"] if k else "nowhere"
    est = sum(1 for x in sessions.values() if x["state"] == "Established")
    return {
        "status": "up" if write_dc and read_dc else ("degraded" if write_dc or read_dc else "down"),
        "summary": f"Write VIP to {name(write_dc)}, read VIP to {name(read_dc)}; {est} of {len(sessions)} BGP sessions up",
        "write_dc": write_dc, "read_dc": read_dc, "sessions": sessions,
        "write_paths": _paths(d.get("paths")), "read_paths": _paths(d.get("read_paths")),
        "vip": d.get("vip"), "vip_read": d.get("vip_read"), "table_text": d.get("table_text", ""),
    }


def lab_state():
    try:
        return json.loads((STATE_DIR / "lab-state.json").read_text())
    except (OSError, ValueError):
        return {"down": [], "partitioned": []}


def probe_exporter(name):
    code, text = fetch(f"http://{name}:9187/metrics", timeout=5.0)  # a full scrape can take >2 s
    m = re.search(r"^pg_up(?:\{[^}]*\})?\s+([0-9.]+)", text, re.M)
    pg_up = bool(m and float(m.group(1)) == 1)
    out = {
        "status": "up" if pg_up else "degraded",
        "summary": "Connected to PostgreSQL" if pg_up else "Running, but cannot reach PostgreSQL",
        "pg_up": pg_up,
    }
    if pg_up:
        def samples(metric):
            return [(dict(re.findall(r'(\w+)="([^"]*)"', lbl or "")), float(v)) for lbl, v in
                    re.findall(rf"^{metric}(?:\{{([^}}]*)\}})?\s+(\S+)$", text, re.M)]
        act = samples("pg_stat_activity_count")
        sizes = [(l.get("datname"), v) for l, v in samples("pg_database_size_bytes")
                 if not l.get("datname", "").startswith("template")]
        mx = samples("pg_settings_max_connections")
        out.update({
            "connections": int(sum(v for _, v in act)),
            "active": int(sum(v for l, v in act if l.get("state") == "active")),
            "max_connections": int(mx[0][1]) if mx else None,
            "databases": len(sizes),
            "size_bytes": int(sum(v for _, v in sizes)),
        })
    return out


def probe_prometheus():
    code, _ = fetch("http://prometheus:9090/-/healthy")
    _, t = fetch_json("http://prometheus:9090/api/v1/targets?state=active")
    targets = t.get("data", {}).get("activeTargets", [])
    up = sum(1 for x in targets if x.get("health") == "up")
    return {
        "status": "up" if code == 200 and up == len(targets) else "degraded",
        "summary": f"{up} of {len(targets)} scrape targets up",
    }


def probe_grafana():
    _, h = fetch_json("http://grafana:3000/api/health")
    return {
        "status": "up" if h.get("database") == "ok" else "degraded",
        "version": h.get("version"),
        "summary": f"Database {h.get('database')}",
    }


PROBES = {
    **{n: (lambda n=n: probe_etcd(n)) for n in ETCD},
    **{n: (lambda n=n: probe_patroni(n)) for n in PATRONI},
    **{h: (lambda h=h: probe_haproxy(h)) for h in HAPROXIES},
    **{b: (lambda b=b: probe_bgp(b)) for b in SPEAKERS},
    "edge": probe_edge,
    **{n: (lambda n=n: probe_exporter(n)) for n in EXPORTERS},
    "prometheus": probe_prometheus,
    "grafana": probe_grafana,
}


def safe(fn):
    try:
        return fn()
    except Exception as e:  # unreachable container, bad JSON, timeout...
        return {"status": "down", "summary": f"Not reachable ({type(e).__name__})"}


def collect_status():
    with ThreadPoolExecutor(max_workers=len(PROBES) + 1) as pool:
        futures = {name: pool.submit(safe, fn) for name, fn in PROBES.items()}
        members = pool.submit(lambda: safe(probe_cluster))
        services = {name: f.result() for name, f in futures.items()}
        cluster = members.result()
    compose = parse_compose()
    for name, svc in services.items():
        svc["image"] = compose.get(name, {}).get("image")
        svc["ports"] = compose.get(name, {}).get("ports", [])
        svc["kind"] = kind_of(name)
        svc["dc"] = DC_OF.get(name)
    for i, name in enumerate(PATRONI):
        m = cluster.get(name) if isinstance(cluster, dict) else None
        if m:
            services[name]["lag"] = m.get("lag")
        services[name]["pg"] = postgres_view(services[name], services[EXPORTERS[i]])
    services["landing"] = {"status": "up", "summary": "Serving this page", "kind": "landing",
                           "image": compose.get("landing", {}).get("image"),
                           "ports": compose.get("landing", {}).get("ports", [])}
    return {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "services": services,
            "topology": TOPO, "lab": lab_state()}


def postgres_view(pat, exp):
    """PostgreSQL's own health and stats for one node, separate from the Patroni agent.

    Patroni reports the postmaster state; the exporter independently confirms it
    accepts connections and supplies connection/size stats. If Patroni's API is
    unreachable the exporter alone decides whether PostgreSQL is up.
    """
    agent_up = "pg_state" in pat
    if agent_up:
        running = pat["pg_state"] == "running"
        status = "up" if running and exp.get("pg_up", True) else ("degraded" if running else "down")
    else:
        status = "up" if exp.get("pg_up") else "down"
    view = {"status": status, "state": pat.get("pg_state") or ("running" if exp.get("pg_up") else "unreachable"),
            "version": pat.get("pg_version"), "started": pat.get("pg_started"),
            "agent": "up" if agent_up else "down", "agent_version": pat.get("patroni_version")}
    for k in ("connections", "active", "max_connections", "databases", "size_bytes"):
        if k in exp:
            view[k] = exp[k]
    return view


def kind_of(name):
    """patroni2 -> patroni, haproxy-a -> haproxy, bgp-b -> bgp"""
    return re.sub(r"(\d+|-[a-z])$", "", name)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def compose_blocks():
    """Split docker-compose.yml into {service: text block} without a YAML lib."""
    if not COMPOSE_FILE.exists():
        return {}
    lines = COMPOSE_FILE.read_text(encoding="utf-8", errors="replace").replace("\r", "").split("\n")
    blocks, current = {}, None
    for line in lines:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = [line]
        elif current and (line.startswith("    ") or not line.strip()):
            blocks[current].append(line)
        elif current and re.match(r"^  #", line):
            current = None
    return {k: "\n".join(v).rstrip() + "\n" for k, v in blocks.items()}


def parse_compose():
    out = {}
    for name, text in compose_blocks().items():
        img = re.search(r"^\s+image:\s*(\S+)", text, re.M)
        ports = re.findall(r'^\s+-\s*"(\d+):(\d+)"', text, re.M)
        out[name] = {"image": img and img.group(1).strip("\"'"), "ports": [{"host": int(h), "container": int(c)} for h, c in ports]}
    return out


SECRET_RE = re.compile(
    r"(?i)((?:password|passwd|secret|auth)[\"']?\s*[:=]\s*[\"']?|stats auth \S+?:|postgresql://[^:\s]+:|PASSWORD=)([^\s\"'@,]+)")


def redact(text):
    return SECRET_RE.sub(lambda m: m.group(1) + "********", text)


def live_config(name):
    k = kind_of(name)
    if k == "patroni":
        code, text = fetch(f"http://{name}:8008/config")
        return [{"name": "Dynamic config (stored in etcd, shared by all nodes)", "lang": "json",
                 "content": json.dumps(json.loads(text), indent=2)}]
    if k == "etcd":
        _, m = fetch_json(f"http://{name}:2379/v3/cluster/member/list", method="POST", body=b"{}")
        members = [{"name": x.get("name"), "peerURLs": x.get("peerURLs"), "clientURLs": x.get("clientURLs")}
                   for x in m.get("members", [])]
        return [{"name": "Cluster members", "lang": "json", "content": json.dumps(members, indent=2)}]
    if k == "prometheus":
        _, c = fetch_json("http://prometheus:9090/api/v1/status/config")
        return [{"name": "Loaded configuration", "lang": "yaml", "content": c.get("data", {}).get("yaml", "")}]
    if k == "grafana":
        _, h = fetch_json("http://grafana:3000/api/health")
        return [{"name": "Health", "lang": "json", "content": json.dumps(h, indent=2)}]
    if k == "edge":
        d = read_status_file("edge")
        return [{"name": "BGP table (show bgp ipv4 unicast)", "lang": "text", "content": d.get("table_text", "")},
                {"name": "Sessions (show bgp summary json)", "lang": "json",
                 "content": json.dumps(d.get("summary", {}), indent=2)}]
    if k == "bgp":
        d = read_status_file(name)
        return [{"name": "What this speaker announces right now", "lang": "json", "content": json.dumps(d, indent=2)}]
    return []


def get_config(name, reveal):
    blocks = compose_blocks()
    if name not in blocks:
        return None
    files = []
    for rel in SERVICE_FILES.get(name, []):
        p = (APP_DIR / Path(rel).name) if rel.startswith("app/") else SRV / rel
        if p.exists():
            files.append({"name": rel.replace("app/", "landing/"),
                          "lang": p.suffix.lstrip(".") or "text",
                          "content": p.read_text(encoding="utf-8", errors="replace").replace("\r", "")})
    try:
        live = live_config(name)
    except Exception as e:
        live = [{"name": "Live config", "lang": "text", "content": f"Could not read live config: {e}"}]
    sections = [{"group": "Compose definition", "items": [
        {"name": "docker-compose.yml", "lang": "yaml", "content": blocks[name]}]}]
    if files:
        sections.append({"group": "Mounted files", "items": files})
    if live:
        sections.append({"group": "Running config", "items": live})
    if not reveal:
        for s in sections:
            for it in s["items"]:
                it["content"] = redact(it["content"])
    return {"service": name, "about": SERVICE_INFO.get(kind_of(name), ""), "sections": sections}


# --------------------------------------------------------------------------
# Leadership changes (Patroni REST API)
# --------------------------------------------------------------------------
# switchover: planned. The healthy leader demotes itself and hands over to the
#             candidate. Patroni refuses if the leader or candidate is unhealthy.
# failover:   emergency. Promotes the candidate even when there is no healthy
#             leader. Can lose transactions the candidate had not received yet.
ACTION_TIMEOUT = 60.0


def current_leader():
    members = probe_cluster()
    for name, m in members.items():
        if m.get("role") == "leader":
            return name, members
    return None, members


def change_leader(action, candidate):
    if action not in ("switchover", "failover"):
        return 400, {"ok": False, "message": "Action must be switchover or failover."}
    if candidate not in PATRONI:
        return 400, {"ok": False, "message": f"{candidate} is not a Patroni node."}
    leader, members = current_leader()
    if candidate == leader:
        return 409, {"ok": False, "message": f"{candidate} is already the leader."}
    if candidate not in members:
        return 409, {"ok": False, "message": f"{candidate} is not a running cluster member, so it can't be promoted."}

    if action == "switchover":
        if not leader:
            return 409, {"ok": False, "message": "There is no healthy leader to hand over from. Use failover instead."}
        target, body = leader, {"leader": leader, "candidate": candidate}
    else:
        # Any reachable node can accept a failover request; prefer the candidate itself.
        target, body = candidate, {"candidate": candidate}

    print(f"[action] {action} requested: {leader or 'no leader'} -> {candidate} via {target}", flush=True)
    try:
        code, text = fetch(f"http://{target}:8008/{action}", method="POST",
                           body=json.dumps(body).encode(), timeout=ACTION_TIMEOUT)
    except Exception as e:
        return 502, {"ok": False, "message": f"Couldn't reach {target}'s Patroni API: {e}"}
    ok = 200 <= code < 300
    print(f"[action] {action} result: HTTP {code} {text.strip()}", flush=True)
    return (200 if ok else 409), {"ok": ok, "action": action, "from": leader, "to": candidate,
                                  "message": text.strip() or f"Patroni answered HTTP {code}"}


def leader_history():
    """[[timeline, lsn, reason, timestamp, new_leader], ...] newest first."""
    for n in PATRONI:
        try:
            _, h = fetch_json(f"http://{n}:8008/history")
            rows = [{"timeline": r[0], "lsn": r[1], "reason": r[2],
                     "at": r[3] if len(r) > 3 else None, "new_leader": r[4] if len(r) > 4 else None}
                    for r in h]
            return list(reversed(rows))
        except Exception:
            continue
    return []


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html")
        if path == "/healthz":
            return self._send(200, '{"ok":true}')
        if path == "/api/status":
            return self._send(200, json.dumps(collect_status()))
        m = re.fullmatch(r"/api/config/([A-Za-z0-9_-]+)", path)
        if m:
            cfg = get_config(m.group(1), "reveal=1" in query)
            if cfg is None:
                return self._send(404, json.dumps({"error": f"No service named {m.group(1)}"}))
            return self._send(200, json.dumps(cfg))
        if path == "/api/history":
            return self._send(200, json.dumps(leader_history()))
        self._send(404, json.dumps({"error": "Not found"}))

    def _same_origin(self):
        # Block cross-site requests: a page on another site can't make the
        # browser POST here, because it can't send a matching Origin header.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is not None and origin.split("://", 1)[-1] == host

    def do_POST(self):
        path = self.path.partition("?")[0]
        m = re.fullmatch(r"/api/(switchover|failover)", path)
        if not m:
            return self._send(404, json.dumps({"error": "Not found"}))
        if not self._same_origin():
            return self._send(403, json.dumps({"ok": False, "message": "Leadership changes must come from this page."}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"ok": False, "message": "Request body must be JSON."}))
        code, result = change_leader(m.group(1), str(body.get("candidate", "")))
        self._send(code, json.dumps(result))

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/status", "/api/history", "/healthz")):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    print(f"Landing page listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
