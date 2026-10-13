#!/usr/bin/env python3
"""
ExaBGP health process for one data center.

Every 2 seconds it asks the local HAProxy (same network namespace, 127.0.0.1:7000)
which PostgreSQL servers are healthy, asks Patroni who the leader is, and tells
ExaBGP (via stdout) what to do with the two anycast VIPs:

  write VIP  announced while this HAProxy can reach the primary.
             MED_LEADER if the primary is in THIS data center, else MED_OTHER.
  read VIP   announced while this HAProxy can reach at least one replica.
             MED_LEADER if one of those replicas is in THIS data center, else MED_OTHER.
  Either is withdrawn as soon as its backend has nothing healthy.

Keeping them separate means reads stay available while a new primary is being
elected. The edge router compares MEDs across data centers (always-compare-med).
A status file is written to /bgp-status/<name>.json for the landing page.
Standard library only.
"""
import base64
import json
import os
import sys
import time
import urllib.request

NAME = os.environ["SPEAKER_NAME"]                 # bgp-a
DC = os.environ["DC_NAME"]                        # DC-A
VIP = os.environ["VIP"]
VIP_READ = os.environ["VIP_READ"]
LOCAL_NODES = os.environ["LOCAL_NODES"].split(",")
ALL_NODES = os.environ["ALL_NODES"].split(",")
MED_LEADER = int(os.environ.get("MED_LEADER", "50"))
MED_OTHER = int(os.environ.get("MED_OTHER", "100"))
AUTH = os.environ.get("HAPROXY_STATS_AUTH", "")
STATUS = f"/bgp-status/{NAME}.json"
INTERVAL = 2


def get(url, auth=None, timeout=1.5):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def haproxy_state():
    """{'write': {'BACKEND': 'UP', 'patroni1': 'DOWN', ...}, 'read': {...}, 'read_any': {...}}

    read     = replicas only (preferred), read_any = fallback incl. the primary.
    """
    out = {"write": {}, "read": {}, "read_any": {}}
    for line in get("http://127.0.0.1:7000/;csv", auth=AUTH).splitlines():
        f = line.split(",")
        key = {"postgresql_write": "write", "postgresql_replicas": "read",
               "postgresql_read_any": "read_any"}.get(f[0])
        if key and len(f) > 17 and f[1] != "FRONTEND":
            out[key][f[1]] = f[17]
    return out


def leader():
    for n in LOCAL_NODES + [x for x in ALL_NODES if x not in LOCAL_NODES]:
        try:
            for m in json.loads(get(f"http://{n}:8008/cluster")).get("members", []):
                if m.get("role") == "leader":
                    return m["name"]
            return None
        except Exception:
            continue
    return None


def say(cmd):
    sys.stdout.write(cmd + "\n")
    sys.stdout.flush()


def write_status(st):
    tmp = STATUS + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, STATUS)
    except OSError:
        pass


class Prefix:
    """One /32 we may announce; only talks to ExaBGP when its decision changes."""

    def __init__(self, ip):
        self.ip, self.med = ip, None          # med None = withdrawn

    def want(self, med):
        if med is None and self.med is not None:
            say(f"withdraw route {self.ip}/32 next-hop self")
        elif med is not None and med != self.med:
            say(f"announce route {self.ip}/32 next-hop self med {med}")
        self.med = med


write_vip, read_vip = Prefix(VIP), Prefix(VIP_READ)
# Watchdog: this container shares HAProxy's network namespace. If HAProxy is
# restarted outside Compose, we're left in a dead namespace (no interfaces).
# After WATCHDOG_FAILS consecutive misses, end the whole container on purpose;
# Docker's restart policy brings it back inside HAProxy's current namespace.
WATCHDOG_FAILS = int(os.environ.get("WATCHDOG_FAILS", "10"))
misses = 0
while True:
    try:
        hp, err = haproxy_state(), None
        misses = 0
    except Exception as e:
        hp, err = {"write": {}, "read": {}, "read_any": {}}, f"local HAProxy not answering ({type(e).__name__})"
        misses += 1
        if misses >= WATCHDOG_FAILS:
            sys.stderr.write(f"local HAProxy unreachable {misses} times in a row; restarting the speaker\n")
            sys.stderr.flush()
            os.kill(1, 15)          # PID 1 is exabgp (exec'd by entrypoint.sh): stop the container
            time.sleep(30)
    lead = leader()
    up_replicas = [s for s, v in hp["read"].items() if s != "BACKEND" and v == "UP"]
    up_readable = [s for s, v in hp["read_any"].items() if s != "BACKEND" and v == "UP"]

    if hp["write"].get("BACKEND") == "UP":
        local = lead in LOCAL_NODES
        write_vip.want(MED_LEADER if local else MED_OTHER)
        w_reason = f"primary {lead} is in {DC}" if local else f"primary {lead or 'unknown'} is in another DC"
    else:
        write_vip.want(None)
        w_reason = err or "local HAProxy can't reach any primary"

    if up_replicas:
        local_r = [s for s in up_replicas if s in LOCAL_NODES]
        read_vip.want(MED_LEADER if local_r else MED_OTHER)
        r_reason = f"local replica {', '.join(local_r)}" if local_r else f"only remote replicas ({', '.join(up_replicas)})"
    elif up_readable:
        # No replica anywhere: HAProxy falls back to the primary for reads.
        read_vip.want(MED_LEADER if any(s in LOCAL_NODES for s in up_readable) else MED_OTHER)
        r_reason = f"no replicas left, reads fall back to {', '.join(up_readable)}"
    else:
        read_vip.want(None)
        r_reason = err or "local HAProxy can't reach any node that serves reads"

    write_status({
        "speaker": NAME, "dc": DC, "leader": lead, "updated": time.time(),
        "write": {"vip": VIP, "announcing": write_vip.med is not None, "med": write_vip.med, "reason": w_reason},
        "read": {"vip": VIP_READ, "announcing": read_vip.med is not None, "med": read_vip.med, "reason": r_reason,
                 "replicas": up_replicas},
        "backends": {k: v.get("BACKEND") for k, v in hp.items()},
        # kept for older readers
        "vip": VIP, "announcing": write_vip.med is not None, "med": write_vip.med, "reason": w_reason,
    })
    time.sleep(INTERVAL)
