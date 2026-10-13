#!/bin/sh
# Runs inside the HAProxy container's network namespace (network_mode: service:haproxy-x).
set -e

# 1. Put both anycast VIPs (write, read) on loopback so HAProxy (bound to
#    *:5000/*:5001) accepts traffic the edge router forwards to them.
ip addr add "${VIP}/32" dev lo 2>/dev/null || true
ip addr add "${VIP_READ}/32" dev lo 2>/dev/null || true

# 2. ExaBGP session to the edge router. The health process decides what to announce.
mkdir -p /etc/exabgp
cat > /etc/exabgp/exabgp.conf <<EOF
process health {
    run /usr/local/bin/python3 -u /app/health.py;
    encoder text;
}

neighbor ${PEER_IP} {
    router-id ${LOCAL_IP};
    local-address ${LOCAL_IP};
    local-as ${LOCAL_AS};
    peer-as ${PEER_AS};
    hold-time 9;
    family {
        ipv4 unicast;
    }
    api {
        processes [ health ];
    }
}
EOF

export exabgp_daemon_user=root        # keep root so health.py can write the status file
export exabgp_daemon_drop=false
export exabgp_api_ack=false           # health.py never reads replies on stdin
export exabgp_log_level=INFO
echo "bgp speaker for ${DC_NAME}: AS ${LOCAL_AS} ${LOCAL_IP} -> edge AS ${PEER_AS} ${PEER_IP}, write VIP ${VIP}, read VIP ${VIP_READ}"
exec exabgp server /etc/exabgp/exabgp.conf
