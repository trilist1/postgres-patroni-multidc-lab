# BGP speaker that sits next to one HAProxy (shares its network namespace).
# Announces the anycast VIP to the edge router only while that HAProxy can
# reach a PostgreSQL primary. Built by: lab build
FROM python:3.15.0-slim
ARG EXABGP_VERSION=5.0.14
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "exabgp==${EXABGP_VERSION}"
COPY entrypoint.sh health.py /app/
RUN sed -i 's/\r$//' /app/entrypoint.sh /app/health.py && chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
