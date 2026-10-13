# Edge router: the "internet" side of the lab. Runs FRR (BGP AS 65000), learns the
# anycast VIP from the data centers, and forwards Windows-host traffic on
# localhost:6432/6433 to whichever DC currently wins the BGP route. Built by: lab build
FROM quay.io/frrouting/frr:10.7.1
RUN apk add --no-cache iptables jq
COPY entrypoint.sh status.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh /usr/local/bin/status.sh \
 && chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/status.sh
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
