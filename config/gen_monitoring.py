#!/usr/bin/env python3
"""
Grafana Dashboard Generator for PostgreSQL HA Cluster
Uses ONLY default postgres_exporter metrics + Patroni /metrics.
No custom queries.yml needed. Zero exporter configuration required.

Default postgres_exporter metrics used:
  pg_database_size_bytes, pg_stat_database_*, pg_stat_bgwriter_*,
  pg_stat_checkpointer_* (needs --collector.stat_checkpointer, PG17+),
  pg_stat_activity_count, pg_locks_count, pg_replication_lag_seconds,
  pg_settings_max_connections, pg_up

Patroni /metrics used:
  patroni_postgres_running, patroni_primary, patroni_postgres_timeline,
  patroni_xlog_location, patroni_xlog_received_location,
  patroni_xlog_replayed_location, patroni_postmaster_start_time,
  patroni_version, patroni_postgres_server_version
"""
import json, sys, os

DASHBOARD_OUT = sys.argv[1] if len(sys.argv) > 1 else "/out/pg-ha-cluster.json"

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

def P(title, ptype, gx, gy, gw, gh, targets, **kw):
    p = {"title": title, "type": ptype,
         "gridPos": {"h": gh, "w": gw, "x": gx, "y": gy},
         "targets": targets, "datasource": DS}
    for k in ("fieldConfig", "options", "description"):
        if k in kw:
            p[k] = kw[k]
    return p

def T(expr, legend="", ref="A", fmt="time_series", instant=False):
    t = {"expr": expr, "refId": ref}
    if legend:
        t["legendFormat"] = legend
    if fmt != "time_series":
        t["format"] = fmt
    if instant:
        t["instant"] = True
    return t

def R(title, y):
    return {"type": "row", "title": title,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False, "panels": []}

def FC(unit=None, decimals=None, thr=None, mappings=None, mn=None, mx=None):
    d = {}
    if unit: d["unit"] = unit
    if decimals is not None: d["decimals"] = decimals
    if thr: d["thresholds"] = {"mode": "absolute", "steps": thr}
    if mappings: d["mappings"] = mappings
    if mn is not None: d["min"] = mn
    if mx is not None: d["max"] = mx
    return {"defaults": d, "overrides": []}

GREEN = {"color": "green", "value": None}
YELLOW90 = {"color": "yellow", "value": 90}
RED = {"color": "red", "value": None}

panels = []
y = 0

# ===== CLUSTER OVERVIEW (Patroni metrics) =====
panels.append(R("CLUSTER OVERVIEW", y)); y += 1

panels.append(P("Patroni Cluster Info", "table", 0, y, 24, 5,
    [T("patroni_version", fmt="table", instant=True)],
    description="All Patroni node metadata")); y += 5

sm = [{"type": "value", "options": {
    "1": {"text": "RUNNING", "color": "green"},
    "0": {"text": "STOPPED", "color": "red"}}}]
panels.append(P("patroni1", "stat", 0, y, 4, 4,
    [T('patroni_postgres_running{instance=~"patroni1.*"}', "patroni1")], fieldConfig=FC(mappings=sm)))
panels.append(P("patroni2", "stat", 4, y, 4, 4,
    [T('patroni_postgres_running{instance=~"patroni2.*"}', "patroni2")], fieldConfig=FC(mappings=sm)))
panels.append(P("patroni3", "stat", 8, y, 4, 4,
    [T('patroni_postgres_running{instance=~"patroni3.*"}', "patroni3")], fieldConfig=FC(mappings=sm)))

pm = [{"type": "value", "options": {
    "1": {"text": "PRIMARY", "color": "green"},
    "0": {"text": "REPLICA", "color": "blue"}}}]
panels.append(P("Role", "stat", 12, y, 4, 4,
    [T("patroni_primary", "{{instance}}")], fieldConfig=FC(mappings=pm)))
panels.append(P("Timeline", "stat", 16, y, 4, 4,
    [T("patroni_postgres_timeline", "{{instance}}")], fieldConfig=FC(decimals=0)))
panels.append(P("PG Up", "stat", 20, y, 4, 4,
    [T("pg_up", "{{instance}}")],
    fieldConfig=FC(mappings=[{"type": "value", "options": {
        "1": {"text": "UP", "color": "green"},
        "0": {"text": "DOWN", "color": "red"}}}]))); y += 4

# ===== REPLICATION (Patroni metrics) =====
panels.append(R("REPLICATION", y)); y += 1

panels.append(P("WAL Position (Patroni)", "timeseries", 0, y, 12, 7,
    [T("patroni_xlog_location", "primary {{instance}}", "A"),
     T("patroni_xlog_received_location", "received {{instance}}", "B"),
     T("patroni_xlog_replayed_location", "replayed {{instance}}", "C")],
    fieldConfig=FC(unit="bytes"),
    description="WAL byte positions from Patroni REST API"))

panels.append(P("Replication Lag (default exporter)", "timeseries", 12, y, 12, 7,
    [T("pg_replication_lag_seconds", "{{instance}}")],
    fieldConfig=FC(unit="s", thr=[GREEN, {"color": "yellow", "value": 1}, {"color": "red", "value": 10}]),
    description="Replication lag from default pg_exporter (replicas only)")); y += 7

# ===== DATABASE ACTIVITY (default exporter) =====
panels.append(R("DATABASE ACTIVITY", y)); y += 1

panels.append(P("Active Connections by State", "timeseries", 0, y, 8, 7,
    [T('pg_stat_activity_count{datname!=""}', "{{instance}} {{datname}} {{state}}")],
    description="Default exporter: pg_stat_activity_count"))

panels.append(P("Transactions per Second", "timeseries", 8, y, 8, 7,
    [T('rate(pg_stat_database_xact_commit{datname!=""}[5m])', "commit {{instance}} {{datname}}", "A"),
     T('rate(pg_stat_database_xact_rollback{datname!=""}[5m])', "rollback {{instance}} {{datname}}", "B")],
    fieldConfig=FC(unit="ops")))

panels.append(P("Active Backends per DB", "gauge", 16, y, 8, 7,
    [T('pg_stat_database_numbackends{datname!=""}', "{{instance}} {{datname}}")],
    fieldConfig=FC(mn=0, mx=200, thr=[GREEN, {"color": "yellow", "value": 50}, {"color": "red", "value": 150}]))); y += 7

panels.append(P("Rows Inserted/Updated/Deleted per Sec", "timeseries", 0, y, 12, 7,
    [T('rate(pg_stat_database_tup_inserted{datname!=""}[5m])', "ins {{instance}} {{datname}}", "A"),
     T('rate(pg_stat_database_tup_updated{datname!=""}[5m])', "upd {{instance}} {{datname}}", "B"),
     T('rate(pg_stat_database_tup_deleted{datname!=""}[5m])', "del {{instance}} {{datname}}", "C")],
    fieldConfig=FC(unit="ops")))

panels.append(P("Rows Fetched / Returned per Sec", "timeseries", 12, y, 12, 7,
    [T('rate(pg_stat_database_tup_fetched{datname!=""}[5m])', "fetched {{instance}} {{datname}}", "A"),
     T('rate(pg_stat_database_tup_returned{datname!=""}[5m])', "returned {{instance}} {{datname}}", "B")],
    fieldConfig=FC(unit="ops"))); y += 7

# ===== PERFORMANCE =====
panels.append(R("PERFORMANCE", y)); y += 1

panels.append(P("Cache Hit Ratio per DB (%)", "gauge", 0, y, 8, 7,
    [T('(pg_stat_database_blks_hit{datname!=""} / (pg_stat_database_blks_hit{datname!=""} + pg_stat_database_blks_read{datname!=""} + 0.001)) * 100',
       "{{instance}} {{datname}}")],
    fieldConfig=FC(unit="percent", mn=0, mx=100, decimals=2,
        thr=[RED, YELLOW90, {"color": "green", "value": 99}]),
    description="Buffer cache hit ratio. Should be >99% in production."))

panels.append(P("Block Hits vs Reads Rate", "timeseries", 8, y, 8, 7,
    [T('rate(pg_stat_database_blks_hit{datname!=""}[5m])', "hits {{instance}} {{datname}}", "A"),
     T('rate(pg_stat_database_blks_read{datname!=""}[5m])', "reads {{instance}} {{datname}}", "B")],
    fieldConfig=FC(unit="ops"),
    description="Shared buffer cache hits vs disk reads"))

panels.append(P("Temp File Bytes Rate", "timeseries", 16, y, 8, 7,
    [T('rate(pg_stat_database_temp_bytes{datname!=""}[5m])', "{{instance}} {{datname}}")],
    fieldConfig=FC(unit="Bps"),
    description="Temp file creation rate. High = work_mem too low.")); y += 7

panels.append(P("Checkpoints", "timeseries", 0, y, 8, 7,
    [T("rate(pg_stat_checkpointer_num_timed_total[5m])", "timed {{instance}}", "A"),
     T("rate(pg_stat_checkpointer_num_requested_total[5m])", "requested {{instance}}", "B")],
    fieldConfig=FC(unit="ops"),
    description="Timed = normal scheduled. Requested = forced (e.g. too much WAL)."))

panels.append(P("Buffer Writes by Source", "timeseries", 8, y, 8, 7,
    [T("rate(pg_stat_checkpointer_buffers_written_total[5m])", "checkpoint {{instance}}", "A"),
     T("rate(pg_stat_bgwriter_buffers_clean_total[5m])", "bgwriter {{instance}}", "B"),
     T("rate(pg_stat_bgwriter_buffers_alloc_total[5m])", "allocated {{instance}}", "C")],
    fieldConfig=FC(unit="ops"),
    description="Buffers written by checkpointer vs bgwriter, and buffers allocated (PG17+ moved backend writes to pg_stat_io)."))

panels.append(P("Checkpoint Write / Sync Time", "timeseries", 16, y, 8, 7,
    [T("rate(pg_stat_checkpointer_write_time_total[5m])", "write {{instance}}", "A"),
     T("rate(pg_stat_checkpointer_sync_time_total[5m])", "sync {{instance}}", "B")],
    fieldConfig=FC(unit="ms"),
    description="Milliseconds per second spent writing/syncing checkpoint files. Sustained high sync time = slow storage.")); y += 7

# ===== LOCKS AND CONFLICTS =====
panels.append(R("LOCKS AND CONFLICTS", y)); y += 1

panels.append(P("Locks by Mode", "timeseries", 0, y, 12, 7,
    [T("pg_locks_count", "{{instance}} {{datname}} {{mode}}")],
    description="Default exporter: pg_locks_count"))

panels.append(P("Conflicts and Deadlocks Rate", "timeseries", 12, y, 12, 7,
    [T('rate(pg_stat_database_conflicts{datname!=""}[5m])', "conflicts {{instance}} {{datname}}", "A"),
     T('rate(pg_stat_database_deadlocks{datname!=""}[5m])', "deadlocks {{instance}} {{datname}}", "B")],
    fieldConfig=FC(unit="ops"))); y += 7

# ===== DATABASE SIZE =====
panels.append(R("DATABASE SIZE", y)); y += 1

panels.append(P("Database Size (trend)", "timeseries", 0, y, 12, 7,
    [T('pg_database_size_bytes{datname!~"template.*"}', "{{instance}} {{datname}}")],
    fieldConfig=FC(unit="bytes")))

panels.append(P("Database Size (current)", "bargauge", 12, y, 12, 7,
    [T('pg_database_size_bytes{datname!~"template.*"}', "{{instance}} {{datname}}", instant=True)],
    fieldConfig=FC(unit="bytes"),
    options={"orientation": "horizontal", "displayMode": "gradient"})); y += 7

# ===== CONNECTION UTILISATION =====
panels.append(R("CONNECTION UTILISATION", y)); y += 1

panels.append(P("Connection % of max_connections", "gauge", 0, y, 8, 7,
    [T('sum by (instance) (pg_stat_activity_count) / on(instance) pg_settings_max_connections * 100',
       "{{instance}}")],
    fieldConfig=FC(unit="percent", mn=0, mx=100,
        thr=[GREEN, {"color": "yellow", "value": 70}, {"color": "red", "value": 90}]),
    description="Pct of max_connections currently used."))

panels.append(P("max_connections Setting", "stat", 8, y, 4, 7,
    [T("pg_settings_max_connections", "{{instance}}")],
    fieldConfig=FC(decimals=0)))

panels.append(P("Connections by State (current)", "table", 12, y, 12, 7,
    [T('pg_stat_activity_count{datname!=""}', fmt="table", instant=True)],
    description="Snapshot of connections by database and state")); y += 7

# ===== ETCD CLUSTER =====
panels.append(R("ETCD CLUSTER", y)); y += 1

panels.append(P("etcd Leader Changes", "timeseries", 0, y, 8, 7,
    [T("rate(etcd_server_leader_changes_seen_total[5m])", "{{instance}}")],
    fieldConfig=FC(unit="ops"),
    description="Should be near zero in steady state"))

panels.append(P("etcd Raft Proposals", "timeseries", 8, y, 8, 7,
    [T("rate(etcd_server_proposals_committed_total[5m])", "committed {{instance}}", "A"),
     T("rate(etcd_server_proposals_failed_total[5m])", "failed {{instance}}", "B"),
     T("etcd_server_proposals_pending", "pending {{instance}}", "C")],
    fieldConfig=FC(unit="ops")))

panels.append(P("etcd DB Size", "timeseries", 16, y, 8, 7,
    [T("etcd_mvcc_db_total_size_in_bytes", "{{instance}}")],
    fieldConfig=FC(unit="bytes"))); y += 7

panels.append(P("etcd WAL Fsync p99", "timeseries", 0, y, 12, 7,
    [T("histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m]))",
       "p99 {{instance}}")],
    fieldConfig=FC(unit="s"),
    description="99th pctl WAL fsync duration. Should be <10ms."))

panels.append(P("etcd gRPC Rate", "timeseries", 12, y, 12, 7,
    [T("rate(grpc_server_started_total[5m])", "{{grpc_method}}")],
    fieldConfig=FC(unit="ops"))); y += 7


# Assemble
dashboard = {
    "annotations": {"list": []},
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "id": None,
    "links": [],
    "panels": panels,
    "refresh": "10s",
    "schemaVersion": 39,
    "tags": ["postgresql", "patroni", "ha", "etcd", "replication"],
    "templating": {
        "list": [{
            "name": "DS_PROMETHEUS",
            "type": "datasource",
            "query": "prometheus",
            "current": {"text": "Prometheus", "value": "Prometheus"},
        }]
    },
    "time": {"from": "now-1h", "to": "now"},
    "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
    "timezone": "browser",
    "title": "PostgreSQL HA Cluster - Patroni / etcd / HAProxy",
    "uid": "pg-ha-patroni-cluster",
    "version": 4,
}

os.makedirs(os.path.dirname(DASHBOARD_OUT), exist_ok=True)
with open(DASHBOARD_OUT, 'w', newline='\n') as f:
    json.dump(dashboard, f, indent=2)
print(f"Dashboard: {len(panels)} panels written to {DASHBOARD_OUT}")
