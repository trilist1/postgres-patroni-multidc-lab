ARG PG_IMAGE_TAG=18.6
FROM postgres:${PG_IMAGE_TAG}
ENV PATRONI_VERSION=4.1.5
ENV PSYCOPG2_VERSION=2.9.13
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip python3-venv curl jq && pip3 install --break-system-packages --no-cache-dir "patroni[etcd3]==${PATRONI_VERSION}" "psycopg2-binary==${PSYCOPG2_VERSION}" && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /etc/patroni/certs /var/lib/postgresql/data
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
EXPOSE 5432 8008
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
