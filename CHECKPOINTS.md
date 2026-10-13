# Checkpoints

A checkpoint has two halves:

- **Code and config** in git. Each checkpoint is an annotated tag, pushed to the private GitHub repo.
- **Data** (database dump, the locally built images) in `backups\checkpoint-<date>-<name>\`, which
  git ignores. Keep that folder with the tag.

| Tag | Date | State |
|-----|------|-------|
| `checkpoint-2026-10-13-single-dc` | 2026-10-13 | Inherited from postgres-patroni-ha-lab: the single-DC lab this repo grew from. |
| `checkpoint-2026-10-13-multi-dc` | 2026-10-13 | Three simulated DCs (DC-A, DC-B, witness DC-C), BGP anycast write and read VIPs via ExaBGP + FRR, landing page with DC frames and BGP panel. All DR drills passed (see README). |

## Roll back

```cmd
lab down
git checkout <tag> -- .
docker load -i backups\checkpoint-<date>-<name>\images.tar
lab generate
lab up
REM optional, to restore the data as well (into a fresh cluster):
lab restore backups\checkpoint-<date>-<name>\pg_dumpall.sql
```

## Make a new checkpoint

```cmd
git add -A
git commit -m "Describe the state"
git tag -a checkpoint-<date>-<name> -m "Describe the state"
git push --follow-tags
mkdir backups\checkpoint-<date>-<name>
docker exec <leader> pg_dumpall -U postgres -h localhost > backups\checkpoint-<date>-<name>\pg_dumpall.sql
docker save patroni-pg18:4.1.5 lab-bgp:exabgp-5.0.14 lab-edge:frr-10.7.1 -o backups\checkpoint-<date>-<name>\images.tar
```
