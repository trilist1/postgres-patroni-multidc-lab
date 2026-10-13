#!/usr/bin/env python3
"""
Grafana dashboards for the multi-DC PostgreSQL HA lab.

    python grafana_dashboards.py <output-dir>

Writes five provisioned dashboards (all tagged "pg-ha", linked to each other):

    pg-ha-overview.json      HA Overview (home): who is primary, where the VIPs land,
                             data-center state, failovers, traffic
    pg-ha-postgresql.json    PostgreSQL: throughput, connections, cache, checkpoints,
                             WAL, locks, sizes, top queries - per node and database
    pg-ha-replication.json   Replication & Failover: roles, timelines, lag, Patroni,
                             BGP sessions and VIP routing over time
    pg-ha-etcd.json          etcd: quorum, leader, Raft proposals, disk and peer latency
    pg-ha-haproxy.json       HAProxy: backend and server health per data center, sessions,
                             errors, health-check failures

Metric sources: postgres_exporter, Patroni /metrics, etcd /metrics, HAProxy's built-in
exporter, and the landing page's /metrics (lab_*: BGP, VIPs, simulated DC state).
Colours match the landing page. Standard library only.
"""
import json
import sys
from pathlib import Path

# Landing-page palette
BLUE, TEAL, GREEN, AMBER, RED, GREY = "#3352C2", "#2B8A86", "#2B8A66", "#B9800F", "#C03A3A", "#8A99AA"
DS = {"type": "prometheus", "uid": "${ds}"}
LINKS = [
    {"title": "PostgreSQL HA", "type": "dashboards", "tags": ["pg-ha"], "asDropdown": False,
     "includeVars": False, "keepTime": True},
    {"title": "Landing page", "type": "link", "url": "http://localhost:9090", "targetBlank": True,
     "icon": "external link"},
]
# Shown on every dashboard: failovers and simulated data-center events
ANNOTATIONS = [
    {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
     "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"},
    {"datasource": DS, "enable": True, "iconColor": BLUE, "name": "Failover / switchover",
     "expr": "changes(max(patroni_postgres_timeline)[1m:15s]) > 0", "step": "15s",
     "titleFormat": "New primary (timeline changed)", "textFormat": ""},
    {"datasource": DS, "enable": True, "iconColor": RED, "name": "Data center powered off / on",
     "expr": "changes(lab_dc_powered_off[1m]) > 0", "step": "15s",
     "titleFormat": "{{dc_name}} power changed", "textFormat": ""},
    {"datasource": DS, "enable": True, "iconColor": AMBER, "name": "Data center partitioned / healed",
     "expr": "changes(lab_dc_partitioned[1m]) > 0", "step": "15s",
     "titleFormat": "{{dc_name}} WAN link changed", "textFormat": ""},
]


# ----------------------------------------------------------------------------
# Building blocks
# ----------------------------------------------------------------------------
def T(expr, legend="", ref=None, instant=False, table=False):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref or "A", "range": not instant,
         "instant": instant}
    if table:
        t["format"] = "table"
    return t


def thresholds(*steps):
    """thresholds((None, GREEN), (2, AMBER)) -> Grafana threshold steps"""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def vmap(pairs):
    """{value: (text, colour)} -> value mapping"""
    return [{"type": "value", "options": {str(k): {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(pairs.items())}}]


def panel(kind, title, targets, desc="", unit=None, decimals=None, mappings=None, thr=None,
          options=None, custom=None, overrides=None, color=None, mn=None, mx=None, transformations=None):
    defaults = {"custom": custom or {}}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    if thr:
        defaults["thresholds"] = thr
    if color:
        defaults["color"] = color
    if mn is not None:
        defaults["min"] = mn
    if mx is not None:
        defaults["max"] = mx
    p = {"type": kind, "title": title, "description": desc, "datasource": DS, "targets": targets,
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def stat(title, targets, desc="", unit=None, thr=None, mappings=None, text_mode="auto", color_mode="background",
         decimals=None, graph=False):
    if not graph:
        for t in targets:          # current value only
            t["instant"], t["range"] = True, False
    return panel("stat", title, targets, desc, unit, decimals, mappings,
                 thr or thresholds((None, BLUE)),
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": color_mode, "graphMode": "area" if graph else "none",
                          "textMode": text_mode, "justifyMode": "center", "orientation": "auto",
                          "showPercentChange": False, "wideLayout": True},
                 color={"mode": "thresholds"})


def ts(title, targets, desc="", unit=None, stack=False, fill=10, mn=None, mx=None, overrides=None,
       bars=False, decimals=None, legend_calcs=("lastNotNull", "max")):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": fill,
              "gradientMode": "opacity", "showPoints": "never", "spanNulls": 30000,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "axisSoftMin": 0 if mn is None else None, "lineInterpolation": "smooth"}
    return panel("timeseries", title, targets, desc, unit, decimals, custom=custom, overrides=overrides,
                 color={"mode": "palette-classic"}, mn=mn, mx=mx,
                 options={"legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend_calcs),
                                     "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "desc"}})


def timeline(title, targets, mapping, desc=""):
    # Colour comes from thresholds built from the same mapping (lowest value is the
    # base colour), so it holds even where Grafana doesn't apply mapping colours;
    # the mapping still provides the text.
    keys = sorted(mapping)
    steps = [(None, mapping[keys[0]][1])] + [(k, mapping[k][1]) for k in keys[1:]]
    return panel("state-timeline", title, targets, desc, mappings=vmap(mapping),
                 thr=thresholds(*steps), color={"mode": "thresholds"},
                 custom={"lineWidth": 0, "fillOpacity": 85},
                 options={"showValue": "never", "rowHeight": 0.85, "mergeValues": True, "alignValue": "center",
                          "legend": {"showLegend": False}, "tooltip": {"mode": "single"}})


def table(title, targets, desc="", rename=None, exclude=(), sort_by=None, unit_overrides=None):
    tr = [{"id": "merge", "options": {}},
          {"id": "organize", "options": {"excludeByName": {k: True for k in ("Time", "__name__", "job", "cluster",
                                                                               "instance", *exclude)},
                                         "indexByName": {"query": 0, "Value": 1},
                                         "renameByName": rename or {}}}]
    if sort_by:
        tr.append({"id": "sortBy", "options": {"sort": [{"field": sort_by, "desc": True}]}})
    ov = [{"matcher": {"id": "byName", "options": f}, "properties": [{"id": "unit", "value": u}]}
          for f, u in (unit_overrides or {}).items()]
    ov.append({"matcher": {"id": "byName", "options": "query"},
               "properties": [{"id": "custom.width", "value": 560}]})
    return panel("table", title, targets, desc, overrides=ov,
                 custom={"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False},
                 options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
                 transformations=tr)


def bargauge(title, targets, desc="", unit=None):
    return panel("bargauge", title, targets, desc, unit, thr=thresholds((None, TEAL)),
                 color={"mode": "thresholds"},
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                          "valueMode": "color", "namePlacement": "left", "sizing": "auto"})


def text(content, title=""):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


class Board:
    """Collects panels and lays them out left-to-right on Grafana's 24-column grid."""

    def __init__(self, uid, title, desc, variables=()):
        self.uid, self.title, self.desc = uid, title, desc
        self.panels, self.vars = [], list(variables)
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def _newline(self):
        if self.x:
            self.y += self.row_h
        self.x = self.row_h = 0

    def row(self, title):
        self._newline()
        self.panels.append({"type": "row", "title": title, "collapsed": False, "id": self.next_id,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.next_id += 1
        self.y += 1

    def add(self, p, w, h):
        if self.x + w > 24:
            self._newline()
        p["id"] = self.next_id
        self.next_id += 1
        p["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.panels.append(p)
        self.x += w
        self.row_h = max(self.row_h, h)
        return p

    def json(self):
        ds_var = {"name": "ds", "label": "Data source", "type": "datasource", "query": "prometheus",
                  "current": {}, "hide": 0, "refresh": 1, "options": [], "includeAll": False, "multi": False}
        return {
            "uid": self.uid, "title": self.title, "description": self.desc, "tags": ["pg-ha"],
            "timezone": "browser", "editable": True, "graphTooltip": 1, "schemaVersion": 41,
            "time": {"from": "now-30m", "to": "now"}, "refresh": "10s",
            "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
            "links": LINKS, "annotations": {"list": ANNOTATIONS},
            "templating": {"list": [ds_var, *self.vars]}, "panels": self.panels,
        }


def qvar(name, label, query, multi=True, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": DS,
            "query": {"query": query, "refId": f"var-{name}"}, "definition": query,
            "refresh": 2, "sort": 1, "multi": multi, "includeAll": include_all,
            "current": {"text": ["All"], "value": ["$__all"]} if include_all else {}, "hide": 0}


# Reused expressions
PRIMARY_DC = 'max by (dc_name) ((patroni_primary == 1) * on(name) group_left(dc_name) lab_node_info)'
REPLICA_LAG_BYTES = ('clamp_min(scalar(max(patroni_xlog_location)) - '
                     '(patroni_xlog_replayed_location and on(name) patroni_replica == 1), 0)')
ROLE = {1: ("Primary", BLUE), 0: ("Replica", GREEN)}
UPDOWN = {1: ("Up", GREEN), 0: ("Down", RED)}
DC_STATE_EXPR = "lab_dc_powered_off * 2 + lab_dc_partitioned"
DC_STATE = {0: ("Running", GREEN), 1: ("Cut off from the WAN", AMBER), 2: ("Powered off", RED),
            3: ("Powered off", RED)}


# ----------------------------------------------------------------------------
# 1. HA Overview (home)
# ----------------------------------------------------------------------------
def overview():
    b = Board("pg-ha-overview", "PostgreSQL HA - Overview",
              "Who is primary, where clients enter, data-center state and failovers, at a glance.")
    b.row("Right now")
    b.add(stat("Primary", [T("patroni_primary == 1", "{{name}}")], "The PostgreSQL node that accepts writes.",
               text_mode="name"), 6, 4)
    b.add(stat("Primary's data center", [T(PRIMARY_DC, "{{dc_name}}")], text_mode="name"), 6, 4)
    b.add(stat("Writes enter through", [T('lab_vip_active{vip="write"} == 1', "{{dc_name}}")],
               "Which data center wins the BGP route for the write VIP 10.99.0.10 (localhost:6432).",
               text_mode="name", thr=thresholds((None, BLUE))), 6, 4)
    b.add(stat("Reads enter through", [T('lab_vip_active{vip="read"} == 1', "{{dc_name}}")],
               "Which data center wins the BGP route for the read VIP 10.99.0.11 (localhost:6433).",
               text_mode="name", thr=thresholds((None, TEAL))), 6, 4)

    b.add(stat("PostgreSQL nodes up", [T("sum(pg_up)")], thr=thresholds((None, RED), (2, AMBER), (3, GREEN))), 4, 4)
    b.add(stat("Replicas streaming", [T("sum(patroni_postgres_streaming)")],
               thr=thresholds((None, RED), (1, AMBER), (2, GREEN))), 4, 4)
    b.add(stat("etcd members up", [T('sum(up{job="etcd"})')], "2 of 3 keep the quorum.",
               thr=thresholds((None, RED), (2, AMBER), (3, GREEN))), 4, 4)
    b.add(stat("BGP sessions up", [T("sum(lab_bgp_session_up)")],
               thr=thresholds((None, RED), (1, AMBER), (2, GREEN))), 4, 4)
    b.add(stat("Worst replica lag", [T(f"max({REPLICA_LAG_BYTES})")], "WAL the slowest replica hasn't replayed yet.",
               unit="bytes", thr=thresholds((None, GREEN), (1048576, AMBER), (16777216, RED))), 4, 4)
    b.add(stat("Failovers in time range", [T("max(patroni_postgres_timeline) - min_over_time(max(patroni_postgres_timeline)[$__range:30s])")],
               "Each promotion starts a new PostgreSQL timeline.",
               thr=thresholds((None, GREEN), (1, AMBER)), decimals=0), 4, 4)

    b.row("Data centers and routing over time")
    b.add(timeline("Data center state", [T(DC_STATE_EXPR, "{{dc_name}}")], DC_STATE,
                   "Set by lab dc-down / partition or the landing page's failure buttons."), 24, 5)
    b.add(timeline("Which DC carries each VIP", [T("lab_vip_active", "{{vip}} VIP via {{dc_name}}")],
                   {1: ("Carrying traffic", BLUE), 0: ("Standby", "rgba(138, 153, 170, 0.18)")}), 24, 6)
    b.add(timeline("PostgreSQL role", [T("patroni_primary", "{{name}}")], ROLE,
                   "Blue = primary. A colour change is a failover or switchover."), 24, 5)

    b.row("Traffic")
    b.add(ts("Transactions per second", [T("sum by (pg_node) (rate(pg_stat_database_xact_commit[1m]) + rate(pg_stat_database_xact_rollback[1m]))", "{{pg_node}}")],
             unit="ops"), 12, 8)
    b.add(ts("Connections", [T("sum by (pg_node) (pg_stat_activity_count)", "{{pg_node}}")], unit="short"), 12, 8)
    b.add(ts("Replica lag", [T(REPLICA_LAG_BYTES, "{{name}}")],
             "WAL not yet replayed on each replica.", unit="bytes"), 12, 8)
    b.add(ts("HAProxy sessions per data center",
             [T('sum by (dc, proxy) (haproxy_backend_current_sessions{proxy=~"postgresql_.*"})', "{{dc}} {{proxy}}")],
             unit="short"), 12, 8)
    return b


# ----------------------------------------------------------------------------
# 2. PostgreSQL
# ----------------------------------------------------------------------------
def postgresql():
    N = 'pg_node=~"$node"'
    ND = 'pg_node=~"$node", datname=~"$datname"'
    b = Board("pg-ha-postgresql", "PostgreSQL HA - PostgreSQL",
              "Throughput, connections, cache, checkpoints, WAL, locks, sizes and top queries per node.",
              [qvar("node", "Node", "label_values(pg_up, pg_node)"),
               qvar("datname", "Database", 'label_values(pg_database_size_bytes{datname!~"template.*"}, datname)')])
    b.row("Health")
    b.add(stat("Up", [T(f"pg_up{{{N}}}", "{{pg_node}}")], mappings=vmap(UPDOWN), text_mode="value_and_name",
               thr=thresholds((None, RED), (1, GREEN))), 6, 4)
    b.add(stat("Uptime", [T(f"time() - patroni_postmaster_start_time", "{{name}}")], unit="s",
               text_mode="value_and_name", color_mode="value"), 6, 4)
    b.add(stat("Connections used", [T(f"sum by (pg_node) (pg_stat_activity_count{{{N}}}) / on(pg_node) group_left() max by (pg_node) (pg_settings_max_connections{{{N}}})", "{{pg_node}}")],
               unit="percentunit", text_mode="value_and_name", thr=thresholds((None, GREEN), (0.7, AMBER), (0.9, RED))), 6, 4)
    b.add(stat("Cache hit ratio", [T(f"sum by (pg_node) (rate(pg_stat_database_blks_hit{{{ND}}}[5m])) / clamp_min(sum by (pg_node) (rate(pg_stat_database_blks_hit{{{ND}}}[5m]) + rate(pg_stat_database_blks_read{{{ND}}}[5m])), 1)", "{{pg_node}}")],
               "Share of block reads served from shared_buffers.", unit="percentunit", decimals=2,
               text_mode="value_and_name", thr=thresholds((None, RED), (0.9, AMBER), (0.98, GREEN))), 6, 4)

    b.row("Throughput")
    b.add(ts("Commits", [T(f"sum by (pg_node) (rate(pg_stat_database_xact_commit{{{ND}}}[1m]))", "{{pg_node}}")], unit="ops"), 8, 8)
    b.add(ts("Rollbacks", [T(f"sum by (pg_node) (rate(pg_stat_database_xact_rollback{{{ND}}}[1m]))", "{{pg_node}}")], unit="ops"), 8, 8)
    b.add(ts("Rows written", [T(f"sum by (pg_node) (rate(pg_stat_database_tup_inserted{{{ND}}}[1m]))", "{{pg_node}} inserted", "A"),
                              T(f"sum by (pg_node) (rate(pg_stat_database_tup_updated{{{ND}}}[1m]))", "{{pg_node}} updated", "B"),
                              T(f"sum by (pg_node) (rate(pg_stat_database_tup_deleted{{{ND}}}[1m]))", "{{pg_node}} deleted", "C")],
             unit="rowsps"), 8, 8)
    b.add(ts("Rows read", [T(f"sum by (pg_node) (rate(pg_stat_database_tup_fetched{{{ND}}}[1m]))", "{{pg_node}} fetched", "A"),
                           T(f"sum by (pg_node) (rate(pg_stat_database_tup_returned{{{ND}}}[1m]))", "{{pg_node}} returned", "B")],
             "Fetched = by index, returned = scanned.", unit="rowsps"), 12, 8)
    b.add(ts("Active time", [T(f"sum by (pg_node) (rate(pg_stat_database_active_time_seconds_total{{{ND}}}[1m]))", "{{pg_node}}")],
             "Seconds spent executing statements per second (roughly: busy backends).", unit="s"), 12, 8)

    b.row("Connections and sessions")
    b.add(ts("Connections by state", [T(f'sum by (pg_node, state) (pg_stat_activity_count{{{N}, state!="disabled"}})', "{{pg_node}} {{state}}")],
             unit="short", stack=True, fill=40), 12, 8)
    b.add(ts("Longest transaction", [T(f"max by (pg_node) (pg_stat_activity_max_tx_duration{{{N}}})", "{{pg_node}}")],
             "Long transactions block vacuum and replication cleanup.", unit="s"), 6, 8)
    b.add(ts("Long-running transactions", [T(f"max by (pg_node) (pg_long_running_transactions{{{N}}})", "{{pg_node}}")],
             unit="short", decimals=0), 6, 8)

    b.row("I/O and cache")
    b.add(ts("Blocks read from disk vs cache", [T(f"sum by (pg_node) (rate(pg_stat_database_blks_read{{{ND}}}[1m]))", "{{pg_node}} disk", "A"),
                                                T(f"sum by (pg_node) (rate(pg_stat_database_blks_hit{{{ND}}}[1m]))", "{{pg_node}} cache", "B")],
             unit="ops"), 12, 8)
    b.add(ts("Temporary files written", [T(f"sum by (pg_node) (rate(pg_stat_database_temp_bytes{{{ND}}}[5m]))", "{{pg_node}}")],
             "Sorts and hashes that didn't fit in work_mem.", unit="Bps"), 6, 8)
    b.add(ts("Block I/O time", [T(f"sum by (pg_node) (rate(pg_stat_database_blk_read_time{{{ND}}}[5m]))", "{{pg_node}} read", "A"),
                                T(f"sum by (pg_node) (rate(pg_stat_database_blk_write_time{{{ND}}}[5m]))", "{{pg_node}} write", "B")],
             "Milliseconds per second (track_io_timing is on).", unit="ms"), 6, 8)

    b.row("Checkpoints, background writer and WAL")
    b.add(ts("Checkpoints", [T(f"sum by (pg_node) (increase(pg_stat_checkpointer_num_timed_total{{{N}}}[$__rate_interval]))", "{{pg_node}} timed", "A"),
                             T(f"sum by (pg_node) (increase(pg_stat_checkpointer_num_requested_total{{{N}}}[$__rate_interval]))", "{{pg_node}} requested", "B")],
             "Requested checkpoints mean max_wal_size is filling up before checkpoint_timeout.", bars=True, decimals=0), 8, 8)
    b.add(ts("Checkpoint write / sync time", [T(f"sum by (pg_node) (rate(pg_stat_checkpointer_write_time_total{{{N}}}[5m]))", "{{pg_node}} write", "A"),
                                              T(f"sum by (pg_node) (rate(pg_stat_checkpointer_sync_time_total{{{N}}}[5m]))", "{{pg_node}} sync", "B")],
             unit="ms"), 8, 8)
    b.add(ts("Buffers written", [T(f"sum by (pg_node) (rate(pg_stat_checkpointer_buffers_written_total{{{N}}}[5m]))", "{{pg_node}} checkpointer", "A"),
                                 T(f"sum by (pg_node) (rate(pg_stat_bgwriter_buffers_clean_total{{{N}}}[5m]))", "{{pg_node}} bgwriter", "B"),
                                 T(f"sum by (pg_node) (rate(pg_stat_bgwriter_buffers_alloc_total{{{N}}}[5m]))", "{{pg_node}} allocated", "C")],
             unit="ops"), 8, 8)
    b.add(ts("WAL on disk", [T(f"pg_wal_size_bytes{{{N}}}", "{{pg_node}}")], unit="bytes"), 12, 7)
    b.add(ts("WAL segments", [T(f"pg_wal_segments{{{N}}}", "{{pg_node}}")], unit="short", decimals=0), 12, 7)

    b.row("Locks and conflicts")
    b.add(ts("Locks by mode", [T(f'sum by (pg_node, mode) (pg_locks_count{{{ND}}}) > 0', "{{pg_node}} {{mode}}")],
             unit="short", stack=True, fill=30), 12, 8)
    b.add(ts("Deadlocks", [T(f"sum by (pg_node) (increase(pg_stat_database_deadlocks{{{ND}}}[$__rate_interval]))", "{{pg_node}}")],
             bars=True, decimals=0), 6, 8)
    b.add(ts("Replica query conflicts", [T(f"sum by (pg_node) (increase(pg_stat_database_conflicts{{{ND}}}[$__rate_interval]))", "{{pg_node}}")],
             "Queries on replicas cancelled because replay needed the rows.", bars=True, decimals=0), 6, 8)

    b.row("Storage")
    b.add(ts("Database size", [T(f'max by (datname) (pg_database_size_bytes{{pg_node=~"$node", datname=~"$datname", datname!~"template.*"}})', "{{datname}}")],
             unit="bytes"), 12, 8)
    b.add(bargauge("Current size by database", [T(f'max by (datname) (pg_database_size_bytes{{pg_node=~"$node", datname=~"$datname", datname!~"template.*"}})', "{{datname}}", instant=True)],
                   unit="bytes"), 12, 8)

    b.row("Top queries (pg_stat_statements)")
    # Query text is a separate mapping metric (pg_stat_statements_query_id{queryid, query}): join it on.
    QTEXT = "* on(queryid) group_left(query) max by (queryid, query) (pg_stat_statements_query_id)"
    b.add(table("Top 15 by total time", [T(f'topk(15, sum by (pg_node, datname, user, queryid) (rate(pg_stat_statements_seconds_total{{{N}}}[5m])) {QTEXT})', instant=True, table=True)],
                "Seconds of execution per second, over the last 5 minutes.",
                rename={"Value": "time (s/s)", "query": "query", "pg_node": "node"}, exclude=("queryid",),
                sort_by="time (s/s)", unit_overrides={"time (s/s)": "s"}), 24, 9)
    b.add(table("Top 15 by calls", [T(f'topk(15, sum by (pg_node, datname, user, queryid) (rate(pg_stat_statements_calls_total{{{N}}}[5m])) {QTEXT})', instant=True, table=True)],
                rename={"Value": "calls/s", "query": "query", "pg_node": "node"}, exclude=("queryid",),
                sort_by="calls/s", unit_overrides={"calls/s": "ops"}), 24, 9)
    return b


# ----------------------------------------------------------------------------
# 3. Replication & Failover
# ----------------------------------------------------------------------------
def replication():
    b = Board("pg-ha-replication", "PostgreSQL HA - Replication & Failover",
              "Roles, timelines, lag and Patroni state, plus BGP sessions and VIP routing across data centers.")
    b.row("Patroni")
    b.add(stat("Timeline", [T("max(patroni_postgres_timeline)")], "Goes up by one with every promotion.", decimals=0), 4, 4)
    b.add(stat("Primary", [T("patroni_primary == 1", "{{name}}")], text_mode="name"), 4, 4)
    b.add(stat("Failsafe mode active", [T("max(patroni_failsafe_mode_is_active)")],
               "1 = the primary is running without etcd, kept up by reaching every peer directly.",
               mappings=vmap({0: ("No", GREEN), 1: ("Yes", AMBER)}), thr=thresholds((None, GREEN), (1, AMBER))), 4, 4)
    b.add(stat("Cluster unlocked", [T("max(patroni_cluster_unlocked)")], "1 = no node holds the leader lock (no primary).",
               mappings=vmap({0: ("No", GREEN), 1: ("Yes", RED)}), thr=thresholds((None, GREEN), (1, RED))), 4, 4)
    b.add(stat("Pending restart", [T("sum(patroni_pending_restart)")], "Nodes waiting for a restart to apply a parameter change.",
               thr=thresholds((None, GREEN), (1, AMBER)), decimals=0), 4, 4)
    b.add(stat("Paused", [T("max(patroni_is_paused)")], "Maintenance mode: no automatic failover.",
               mappings=vmap({0: ("No", GREEN), 1: ("Yes", AMBER)}), thr=thresholds((None, GREEN), (1, AMBER))), 4, 4)

    b.add(timeline("Role per node", [T("patroni_primary", "{{name}}")], ROLE), 12, 6)
    b.add(timeline("Streaming from the primary", [T("patroni_postgres_streaming", "{{name}}")],
                   {1: ("Streaming", GREEN), 0: ("Not streaming", GREY)}, "The primary itself shows as not streaming."), 12, 6)

    b.row("Lag")
    b.add(ts("Replica lag in bytes", [T(REPLICA_LAG_BYTES, "{{name}}")], unit="bytes"), 12, 8)
    b.add(ts("Replica lag in seconds", [T("pg_replication_lag_seconds and on(instance) pg_replication_is_replica == 1", "{{pg_node}}")],
             "Time since the last replayed transaction. Rises on an idle primary too.", unit="s"), 12, 8)
    b.add(ts("WAL positions", [T("patroni_xlog_location > 0", "{{name}} written (primary)", "A"),
                               T("patroni_xlog_replayed_location > 0", "{{name}} replayed", "B")],
             unit="bytes", fill=0, legend_calcs=("lastNotNull",)), 12, 8)
    b.add(ts("Seconds since Patroni last reached etcd", [T("time() - patroni_dcs_last_seen", "{{name}}")],
             "Normal: a saw-tooth up to Patroni's loop_wait (10 s). Keeps climbing on a node cut off from the etcd quorum.", unit="s"), 12, 8)
    b.add(ts("Replication slots: WAL retained", [T("pg_replication_slots_pg_wal_lsn_diff", "{{pg_node}} {{slot_name}}")],
             "WAL the primary keeps for each replica's slot.", unit="bytes"), 24, 7)

    b.row("Data centers and BGP")
    b.add(timeline("Data center state", [T(DC_STATE_EXPR, "{{dc_name}}")], DC_STATE), 24, 5)
    b.add(timeline("BGP session from the edge router", [T("lab_bgp_session_up", "{{dc_name}}")], UPDOWN), 12, 5)
    b.add(timeline("VIP announced", [T("lab_vip_announced", "{{vip}} VIP by {{dc_name}}")],
                   {1: ("Announced", GREEN), 0: ("Withdrawn", RED)}), 12, 5)
    b.add(timeline("Which DC carries each VIP", [T("lab_vip_active", "{{vip}} VIP via {{dc_name}}")],
                   {1: ("Carrying traffic", BLUE), 0: ("Standby", "rgba(138, 153, 170, 0.18)")}), 12, 6)
    b.add(ts("MED announced per data center", [T("lab_vip_med", "{{vip}} VIP from {{dc_name}}")],
             "Lower wins. 50 = the primary (writes) or a local replica (reads) is in that DC.", decimals=0,
             legend_calcs=("lastNotNull",)), 12, 6)
    b.add(timeline("HAProxy: write backend per DC", [T('haproxy_backend_status{proxy="postgresql_write", state="UP"}', "{{dc}}")],
                   UPDOWN, "Down = that DC's HAProxy can't reach any primary."), 24, 4)
    return b


# ----------------------------------------------------------------------------
# 4. etcd
# ----------------------------------------------------------------------------
def etcd():
    b = Board("pg-ha-etcd", "PostgreSQL HA - etcd",
              "The consensus store behind Patroni's leader lock: quorum, Raft and latency.")
    b.row("Quorum")
    b.add(stat("Members up", [T('sum(up{job="etcd"})')], "2 of 3 are needed for a quorum.",
               thr=thresholds((None, RED), (2, AMBER), (3, GREEN))), 6, 4)
    b.add(stat("Members that see a leader", [T("sum(etcd_server_has_leader)")],
               thr=thresholds((None, RED), (2, AMBER), (3, GREEN))), 6, 4)
    b.add(stat("Raft leader", [T("etcd_server_is_leader == 1", "{{instance}}")], text_mode="name"), 6, 4)
    b.add(stat("Leader changes in range", [T("max(increase(etcd_server_leader_changes_seen_total[$__range]))")],
               thr=thresholds((None, GREEN), (1, AMBER)), decimals=0), 6, 4)
    b.add(timeline("Raft leader over time", [T("etcd_server_is_leader", "{{instance}}")],
                   {1: ("Leader", BLUE), 0: ("Follower", GREEN)}), 24, 5)

    b.row("Raft")
    b.add(ts("Proposals committed / applied", [T("rate(etcd_server_proposals_committed_total[1m])", "{{instance}} committed", "A"),
                                               T("rate(etcd_server_proposals_applied_total[1m])", "{{instance}} applied", "B")],
             unit="ops"), 12, 8)
    b.add(ts("Proposals pending / failed", [T("etcd_server_proposals_pending", "{{instance}} pending", "A"),
                                            T("increase(etcd_server_proposals_failed_total[$__rate_interval])", "{{instance}} failed", "B")],
             decimals=0), 12, 8)

    b.row("Latency")
    b.add(ts("WAL fsync p99", [T("histogram_quantile(0.99, sum by (le, instance) (rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])))", "{{instance}}")],
             "Above ~10 ms etcd starts missing heartbeats.", unit="s"), 8, 8)
    b.add(ts("Backend commit p99", [T("histogram_quantile(0.99, sum by (le, instance) (rate(etcd_disk_backend_commit_duration_seconds_bucket[5m])))", "{{instance}}")],
             unit="s"), 8, 8)
    b.add(ts("Peer round trip p99", [T("histogram_quantile(0.99, sum by (le, instance, To) (rate(etcd_network_peer_round_trip_time_seconds_bucket[5m])))", "{{instance}} to {{To}}")],
             "Between data centers: this is the WAN latency etcd sees.", unit="s"), 8, 8)

    b.row("Traffic and storage")
    b.add(ts("Peer traffic", [T("sum by (instance) (rate(etcd_network_peer_sent_bytes_total[1m]))", "{{instance}} sent", "A"),
                              T("sum by (instance) (rate(etcd_network_peer_received_bytes_total[1m]))", "{{instance}} received", "B")],
             unit="Bps"), 8, 8)
    b.add(ts("Client gRPC requests", [T('sum by (grpc_method) (rate(grpc_server_handled_total{job="etcd"}[1m])) > 0', "{{grpc_method}}")],
             "Mostly Patroni renewing its leader lease.", unit="reqps"), 8, 8)
    b.add(ts("Database size", [T("etcd_mvcc_db_total_size_in_bytes", "{{instance}} total", "A"),
                               T("etcd_mvcc_db_total_size_in_use_in_bytes", "{{instance}} in use", "B")],
             unit="bytes"), 8, 8)
    b.add(ts("Peer send failures", [T("increase(etcd_network_peer_sent_failures_total[$__rate_interval])", "{{instance}} to {{To}}")],
             "Spikes when a data center is down or partitioned.", bars=True, decimals=0), 12, 7)
    b.add(ts("Heartbeat send failures", [T("increase(etcd_server_heartbeat_send_failures_total[$__rate_interval])", "{{instance}}")],
             bars=True, decimals=0), 12, 7)
    return b


# ----------------------------------------------------------------------------
# 5. HAProxy
# ----------------------------------------------------------------------------
def haproxy():
    D = 'dc=~"$dc", proxy=~"postgresql_.*"'
    b = Board("pg-ha-haproxy", "PostgreSQL HA - HAProxy",
              "One HAProxy per data center, identical config. Writes to the primary, reads to replicas.",
              [qvar("dc", "Data center", "label_values(haproxy_backend_status, dc)")])
    b.row("Health")
    b.add(timeline("Backends", [T(f'haproxy_backend_status{{{D}, state="UP"}}', "{{dc}} {{proxy}}")], UPDOWN,
                   "postgresql_replicas falls back to postgresql_read_any (the primary) when it has no servers."), 24, 6)
    b.add(timeline("Servers", [T(f'haproxy_server_status{{{D}, state="UP"}}', "{{dc}} {{proxy}} {{server}}")],
                   {1: ("Serving", GREEN), 0: ("Not serving", "rgba(138, 153, 170, 0.35)")},
                   "Serving = that node passes this pool's Patroni check (/primary, /replica or /read-only). "
                   "Replicas are 'not serving' in the write pool by design; see Backends for real outages."), 24, 10)
    b.add(ts("Active servers per backend", [T(f"haproxy_backend_active_servers{{{D}}}", "{{dc}} {{proxy}}")], decimals=0), 12, 7)
    b.add(ts("Health-check failures", [T(f"sum by (dc, server) (increase(haproxy_server_check_failures_total{{{D}}}[$__rate_interval]))", "{{dc}} {{server}}")],
             "Includes the normal 503s from nodes in the 'wrong' role.", bars=True, decimals=0), 12, 7)

    b.row("Traffic")
    b.add(ts("Current sessions", [T(f"haproxy_backend_current_sessions{{{D}}}", "{{dc}} {{proxy}}")], decimals=0), 8, 8)
    b.add(ts("New sessions per second", [T(f"rate(haproxy_backend_sessions_total{{{D}}}[1m])", "{{dc}} {{proxy}}")], unit="ops"), 8, 8)
    b.add(ts("Sessions per server", [T(f"sum by (dc, server) (haproxy_server_current_sessions{{{D}}})", "{{dc}} {{server}}")], decimals=0), 8, 8)
    b.add(ts("Bytes in / out", [T(f"sum by (dc) (rate(haproxy_backend_bytes_in_total{{{D}}}[1m]))", "{{dc}} in", "A"),
                                T(f"sum by (dc) (rate(haproxy_backend_bytes_out_total{{{D}}}[1m]))", "{{dc}} out", "B")],
             unit="Bps"), 12, 8)
    b.add(ts("Connection errors", [T(f"sum by (dc, proxy) (increase(haproxy_backend_connection_errors_total{{{D}}}[$__rate_interval]))", "{{dc}} {{proxy}}")],
             bars=True, decimals=0), 12, 8)
    b.add(ts("Average connect time", [T(f"haproxy_backend_connect_time_average_seconds{{{D}}}", "{{dc}} {{proxy}}")],
             "Across a data center boundary this includes the WAN round trip.", unit="s"), 12, 7)
    b.add(ts("Server state changes", [T(f"sum by (dc, server) (increase(haproxy_server_check_up_down_total{{{D}}}[$__rate_interval]))", "{{dc}} {{server}}")],
             "Up/down transitions: every failover shows here.", bars=True, decimals=0), 12, 7)
    return b


BOARDS = {"pg-ha-overview.json": overview, "pg-ha-postgresql.json": postgresql,
          "pg-ha-replication.json": replication, "pg-ha-etcd.json": etcd, "pg-ha-haproxy.json": haproxy}


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):          # dashboards that no longer exist get removed from Grafana
        if old.name not in BOARDS:
            old.unlink()
    for name, build in BOARDS.items():
        (out / name).write_text(json.dumps(build().json(), indent=2), encoding="utf-8", newline="\n")
    print(f"wrote {len(BOARDS)} dashboards to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config/grafana/dashboards")
