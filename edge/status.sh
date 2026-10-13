#!/bin/sh
# Every 2 s, write the edge router's BGP view to /bgp-status/edge.json for the
# landing page: sessions, every path for the VIP (and which one wins), and the
# route the kernel is actually using.
OUT=/bgp-status/edge.json
while true; do
  sleep 2
  summary=$(vtysh -c "show bgp ipv4 unicast summary json" 2>/dev/null) || continue
  paths=$(vtysh -c "show bgp ipv4 unicast ${VIP}/32 json" 2>/dev/null) || paths='{}'
  rpaths=$(vtysh -c "show bgp ipv4 unicast ${VIP_READ}/32 json" 2>/dev/null) || rpaths='{}'
  kernel=$(ip -j route show "${VIP}" 2>/dev/null) || kernel='[]'
  rkernel=$(ip -j route show "${VIP_READ}" 2>/dev/null) || rkernel='[]'
  table=$(vtysh -c "show bgp ipv4 unicast" 2>/dev/null | sed 's/"/\\"/g' | awk '{printf "%s\\n", $0}')
  printf '{"updated": %s, "vip": "%s", "vip_read": "%s", "summary": %s, "paths": %s, "kernel": %s, "read_paths": %s, "read_kernel": %s, "table_text": "%s"}\n' \
    "$(date +%s)" "${VIP}" "${VIP_READ}" "${summary:-{\}}" "${paths:-{\}}" "${kernel:-[]}" "${rpaths:-{\}}" "${rkernel:-[]}" "${table}" > "${OUT}.tmp" \
    && jq -e . "${OUT}.tmp" >/dev/null 2>&1 && mv "${OUT}.tmp" "${OUT}"
done
