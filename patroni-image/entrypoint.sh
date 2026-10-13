#!/bin/bash
set -e
echo "=== Patroni Entrypoint ==="
echo "Node: ${PATRONI_NAME}"
if [ -d /etc/patroni/certs ]; then
  chmod 600 /etc/patroni/certs/*.key 2>/dev/null || true
  chown -R postgres:postgres /etc/patroni/certs 2>/dev/null || true
fi
mkdir -p /var/lib/postgresql/data
chown -R postgres:postgres /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data
echo "Waiting for any etcd member (one data center may be down)..."
until for h in $(echo "${ETCD_HOSTS:-etcd1:2379}" | tr ',' ' '); do
        curl -sf --max-time 2 "http://${h}/health" > /dev/null 2>&1 && echo "  reached ${h}" && break
      done | grep -q reached; do
  echo "  no etcd member reachable (${ETCD_HOSTS}), retrying in 3s..."
  sleep 3
done
echo "etcd is available."
exec gosu postgres patroni /etc/patroni/patroni.yml
