#!/bin/sh
# Edge router start-up:
#   1. NAT: localhost:6432 -> write VIP:5000, localhost:6433 -> read VIP:5001.
#      The kernel then routes each VIP by the BGP route FRR installed, i.e. to the
#      HAProxy of the winning data center. MASQUERADE makes HAProxy reply via this
#      router so the reverse NAT happens here.
#   2. Status writer for the landing page.
#   3. FRR itself.
set -e
iptables -t nat -A PREROUTING -p tcp --dport "${VIP_WRITE_PORT}" -j DNAT --to-destination "${VIP}:5000"
iptables -t nat -A PREROUTING -p tcp --dport "${VIP_READ_PORT}" -j DNAT --to-destination "${VIP_READ}:5001"
iptables -t nat -A POSTROUTING -d "${VIP}" -j MASQUERADE
iptables -t nat -A POSTROUTING -d "${VIP_READ}" -j MASQUERADE
echo "edge: ${VIP_WRITE_PORT} -> ${VIP}:5000 (write), ${VIP_READ_PORT} -> ${VIP_READ}:5001 (read)"

/usr/local/bin/status.sh &
exec /usr/lib/frr/docker-start
