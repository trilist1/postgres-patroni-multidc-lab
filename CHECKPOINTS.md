# Checkpoints

A checkpoint has two halves:

- **Code and config** live in git: this folder is a repository pushed to a **private** GitHub
  repo (see `git remote -v`). Each checkpoint is a tag, pushed too.
- **Data** (databases, etcd state, the custom Patroni image) is too big for git and lives in
  `backups\checkpoint-<date>\`, which git ignores. Keep that folder with the tag.

| Tag | Date | State |
|-----|------|-------|
| `checkpoint-2026-10-13-single-dc` | 2026-10-13 | Single data center, 13 containers, all healthy. PostgreSQL 18.6, Patroni 4.1.5, etcd 3.7.2, HAProxy 3.4.6, Prometheus 3.15.0, Grafana 13.2.3, postgres_exporter 0.20.1. Landing page on :9090 with topology, config viewer, switchover/failover and PostgreSQL-first node boxes. Leader at checkpoint time: patroni2. Databases: appdb, db1, db2, postgres. |

## What is in `backups\checkpoint-2026-10-13\`

| File | What it is |
|------|------------|
| `pg_dumpall.sql` | Every database and role, taken from the leader |
| `etcd-snapshot.db` | etcd keyspace (Patroni's cluster state). Rarely needed: Patroni rebuilds it |
| `patroni-dcs-config.yml` | Patroni's dynamic config (`patronictl show-config`) at checkpoint time |
| `cluster-state.txt` | `patronictl list` at checkpoint time |
| `images.txt` | Exact image tags and IDs that were running |
| `patroni-pg18-4.1.5.image.tar` | The custom Patroni image, so a rollback doesn't depend on a rebuild |
| `certs\` | The TLS CA, certificates and **private keys**. Not in git (never commit keys); restore from here |

## See what changed since the checkpoint

```cmd
git status
git diff checkpoint-2026-10-13-single-dc
git log --oneline
```

## Roll back to this checkpoint

**Config and code only** (keep the current data):

```cmd
docker compose -p patroni-ha down
git checkout checkpoint-2026-10-13-single-dc -- .
if not exist certs\ca.crt xcopy /e /i backups\checkpoint-2026-10-13\certs certs
docker load -i backups\checkpoint-2026-10-13\patroni-pg18-4.1.5.image.tar
docker compose -p patroni-ha up -d
```

**Full rollback including data** (replaces the databases with the checkpoint copy):

```cmd
docker compose -p patroni-ha down
git checkout checkpoint-2026-10-13-single-dc -- .
REM move the current data aside rather than deleting it
move pgdata pgdata-before-rollback
move etcd-data etcd-data-before-rollback
if not exist certs\ca.crt xcopy /e /i backups\checkpoint-2026-10-13\certs certs
docker load -i backups\checkpoint-2026-10-13\patroni-pg18-4.1.5.image.tar
patroni-ha-cluster.cmd setup
REM wait for a leader (landing page or patronictl list), then restore:
docker cp backups\checkpoint-2026-10-13\pg_dumpall.sql patroni1:/tmp/all.sql
docker exec patroni1 psql -U postgres -h localhost -f /tmp/all.sql
```

Run the restore against whichever node is the leader (`patroni1` above is an example).
"role already exists" errors for `postgres` and `replicator` are expected.

## Make a new checkpoint

```cmd
git add -A
git commit -m "Describe the state"
git tag -a checkpoint-<date>-<name> -m "Describe the state"
git push --follow-tags
REM plus a data snapshot:
mkdir backups\checkpoint-<date>
docker exec <leader> pg_dumpall -U postgres -h localhost > backups\checkpoint-<date>\pg_dumpall.sql
```
