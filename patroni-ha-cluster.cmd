@echo off
setlocal
chcp 437 >nul 2>&1
title PostgreSQL HA Cluster Manager

REM ============================================================================
REM  POSTGRESQL HIGH-AVAILABILITY CLUSTER (Docker Compose Edition)
REM ============================================================================
REM
REM  This script creates, manages, and tears down a full PostgreSQL HA cluster
REM  running entirely in Docker on Windows. All containers appear as a single
REM  logical stack ("patroni-ha") in Docker Desktop.
REM
REM  ARCHITECTURE (13 containers total):
REM  +-----------+    +-----------+    +-----------+
REM  |  etcd1    |    |  etcd2    |    |  etcd3    |   Distributed consensus
REM  |  :2379    |<-->|  :2379    |<-->|  :2379    |   store (Raft protocol).
REM  +-----------+    +-----------+    +-----------+   Stores cluster state.
REM        |                |                |
REM  +-----------+    +-----------+    +-----------+
REM  | patroni1  |    | patroni2  |    | patroni3  |   PostgreSQL 18 managed
REM  | PG :5432  |    | PG :5432  |    | PG :5432  |   by Patroni 4.1.5.
REM  | API:8008  |    | API:8008  |    | API:8008  |   One leader, two replicas.
REM  +-----------+    +-----------+    +-----------+
REM        |                |                |
REM  +---------------------------------------------+
REM  |              HAProxy  :5000 (write)          |   Routes writes to leader,
REM  |                       :5001 (read)           |   reads across replicas.
REM  |                       :7000 (stats)          |   Health-checks via Patroni
REM  +---------------------------------------------+   REST API on port 8008.
REM        |                |                |
REM  +-----------+    +-----------+    +-----------+
REM  |pg-export1 |    |pg-export2 |    |pg-export3 |   One exporter PER node
REM  |  :9187    |    |  :9188    |    |  :9189    |   for full replica metrics.
REM  +-----------+    +-----------+    +-----------+
REM        |                |                |
REM  +---------------------------------------------+
REM  |           Prometheus  :9090                  |   Scrapes Patroni /metrics,
REM  +---------------------------------------------+   pg_exporters, and etcd.
REM        |
REM  +---------------------------------------------+
REM  |           Grafana     :3000                  |   Pre-provisioned with 40+
REM  +---------------------------------------------+   panel HA dashboard.
REM
REM  HOW IT WORKS:
REM  1. etcd forms a 3-node Raft cluster for distributed consensus
REM  2. Patroni on each node registers with etcd and runs leader election
REM  3. The elected leader initialises PostgreSQL; replicas stream from it
REM  4. HAProxy health-checks Patroni's REST API (/primary, /replica)
REM     to route connections to the correct node
REM  5. If the leader fails, Patroni automatically promotes a replica
REM     and HAProxy re-routes within seconds
REM  6. Prometheus collects metrics from all components
REM  7. Grafana visualises cluster health, replication, performance
REM
REM  PREREQUISITES:
REM  - Docker Desktop for Windows (with Docker Compose v2)
REM  - Minimum 8GB RAM allocated to Docker (16GB recommended)
REM  - ~5GB disk space for images + data
REM
REM  USAGE:
REM    patroni-ha-cluster.cmd              (interactive menu)
REM    patroni-ha-cluster.cmd setup        (generate + build + start all)
REM    patroni-ha-cluster.cmd start        (start existing stack)
REM    patroni-ha-cluster.cmd stop         (stop without removing)
REM    patroni-ha-cluster.cmd remove       (tear down everything)
REM    patroni-ha-cluster.cmd status       (show cluster health)
REM    patroni-ha-cluster.cmd logs         (view container logs)
REM    patroni-ha-cluster.cmd failover-test (simulate primary failure)
REM
REM ============================================================================

REM ============================================================================
REM  CONFIGURATION VARIABLES
REM  Edit these to customise your cluster. All paths, ports, credentials, and
REM  image versions are defined here - nothing is hardcoded elsewhere.
REM ============================================================================

REM --- BASE_DIR: Root directory for ALL generated files.                    ---
REM --- Defaults to the folder this script lives in, so the repo works from ---
REM --- any location. Subdirectories created automatically: config/, certs/,---
REM --- pgdata/, etcd-data/, patroni-image/, prometheus-data/, grafana-data/ ---
set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

REM --- PG_DATA_DIR: Where PostgreSQL data files are persisted on the host. ---
REM --- Each Patroni node gets its own subdirectory (patroni1/2/3).         ---
REM --- This survives container restarts and rebuilds. Keep it at           ---
REM --- BASE_DIR\pgdata: docker-compose.yml mounts ./pgdata relative paths.  ---
set "PG_DATA_DIR=%BASE_DIR%\pgdata"

REM --- COMPOSE_PROJECT: The name shown in Docker Desktop's container list. ---
REM --- All containers will be grouped under this name.                     ---
set "COMPOSE_PROJECT=patroni-ha"

REM --- CLUSTER_NAME: Patroni "scope" - identifies this cluster in etcd.    ---
REM --- Must be unique if running multiple Patroni clusters on same etcd.   ---
set "CLUSTER_NAME=pg-ha-cluster"

REM --- Container names (also used as Docker network hostnames)             ---
set "ETCD1_NAME=etcd1"
set "ETCD2_NAME=etcd2"
set "ETCD3_NAME=etcd3"
set "PATRONI1_NAME=patroni1"
set "PATRONI2_NAME=patroni2"
set "PATRONI3_NAME=patroni3"
set "HAPROXY_NAME=haproxy"
set "PG_EXPORTER1_NAME=pg-exporter1"
set "PG_EXPORTER2_NAME=pg-exporter2"
set "PG_EXPORTER3_NAME=pg-exporter3"
set "PROMETHEUS_NAME=prometheus"
set "GRAFANA_NAME=grafana"
set "LANDING_NAME=landing"

REM --- Image versions (all pinned - last reviewed 2026-10-13)              ---
REM --- PG_VERSION: PostgreSQL major version (names the Patroni image)      ---
REM --- PG_IMAGE_TAG: exact postgres base image tag for the Patroni build   ---
REM --- PATRONI_VERSION: Patroni release installed from PyPI                ---
REM --- ETCD_VERSION: v3.6+ removed the --enable-v2 flag (v3 API only).     ---
REM ---   etcd data can only be upgraded one minor at a time (3.5-3.6-3.7) ---
REM --- NOTE: changing PG_VERSION (major) needs a fresh data dir or a       ---
REM ---   dump/restore - see "Upgrading" in README.md                       ---
set "PG_VERSION=18"
set "PG_IMAGE_TAG=18.6"
set "PATRONI_VERSION=4.1.5"
set "PSYCOPG2_VERSION=2.9.13"
set "ETCD_VERSION=v3.7.2"
set "ETCD_IMAGE=gcr.io/etcd-development/etcd:%ETCD_VERSION%"
set "HAPROXY_IMAGE=haproxy:3.4.6"
set "PROMETHEUS_IMAGE=prom/prometheus:v3.15.0"
set "GRAFANA_IMAGE=grafana/grafana:13.2.3"
set "PG_EXPORTER_IMAGE=quay.io/prometheuscommunity/postgres-exporter:v0.20.1"
set "LANDING_IMAGE=python:3.15.0-slim"
set "PATRONI_IMAGE_NAME=patroni-pg%PG_VERSION%:%PATRONI_VERSION%"

REM --- PostgreSQL credentials                                              ---
REM --- PG_SUPERUSER: Used by Patroni to manage PostgreSQL                  ---
REM --- PG_REPL_USER: Used for streaming replication between nodes          ---
REM --- PG_APP_USER: Application-level user (created by post-setup step)    ---
set "PG_SUPERUSER=postgres"
set "PG_SUPERUSER_PASSWORD=SuperSecret2026"
set "PG_REPL_USER=replicator"
set "PG_REPL_PASSWORD=ReplSecret2026"
set "PG_APP_DB=appdb"
set "PG_APP_USER=appuser"
set "PG_APP_PASSWORD=AppSecret2026"

REM --- etcd cluster token (must match across all nodes)                    ---
set "ETCD_CLUSTER_TOKEN=patroni-etcd-token"

REM --- HAProxy stats dashboard credentials                                 ---
set "HAPROXY_STATS_USER=admin"
set "HAPROXY_STATS_PASSWORD=HaproxyStats2026"

REM --- Grafana admin credentials                                           ---
set "GRAFANA_ADMIN_USER=admin"
set "GRAFANA_ADMIN_PASSWORD=GrafanaAdmin2026"

REM --- Exposed host ports                                                  ---
REM --- These are the ports you connect to from your Windows host.          ---
REM --- Internal container ports are fixed (5432, 8008, 2379, etc.)         ---
set "HAPROXY_WRITE_PORT=5000"
set "HAPROXY_READ_PORT=5001"
set "HAPROXY_STATS_PORT=7000"
set "PATRONI1_PG_PORT=5433"
set "PATRONI2_PG_PORT=5434"
set "PATRONI3_PG_PORT=5435"
set "PATRONI1_API_PORT=8008"
set "PATRONI2_API_PORT=8009"
set "PATRONI3_API_PORT=8010"
set "ETCD1_CLIENT_PORT=2379"
set "ETCD2_CLIENT_PORT=2381"
set "ETCD3_CLIENT_PORT=2383"
set "PG_EXPORTER1_PORT=9187"
set "PG_EXPORTER2_PORT=9188"
set "PG_EXPORTER3_PORT=9189"
set "PROMETHEUS_PORT=9091"
set "LANDING_PORT=9090"
set "GRAFANA_PORT=3000"

REM --- TLS Certificate parameters                                          ---
REM --- Used when generating the self-signed CA and server certificates.    ---
set "CERT_COUNTRY=GB"
set "CERT_STATE=Hertfordshire"
set "CERT_CITY=Stevenage"
set "CERT_ORG=PatroniCluster"
set "CERT_VALIDITY_DAYS=3650"

REM ============================================================================
REM  DERIVED PATHS (calculated from BASE_DIR - do not edit)
REM ============================================================================
set "CONFIG_DIR=%BASE_DIR%\config"
set "CERTS_DIR=%BASE_DIR%\certs"
set "PATRONI_BUILD_DIR=%BASE_DIR%\patroni-image"
set "HAPROXY_CFG_DIR=%CONFIG_DIR%\haproxy"
set "PROMETHEUS_CFG_DIR=%CONFIG_DIR%\prometheus"
set "GRAFANA_PROV_DIR=%CONFIG_DIR%\grafana\provisioning"
set "GRAFANA_DASH_DIR=%CONFIG_DIR%\grafana\dashboards"
set "PG_EXPORTER_CFG_DIR=%CONFIG_DIR%\pg-exporter"
set "ETCD_DATA_DIR=%BASE_DIR%\etcd-data"
set "COMPOSE_FILE=%BASE_DIR%\docker-compose.yml"
set "LANDING_DIR=%BASE_DIR%\landing"

REM ============================================================================
REM  COMMAND ROUTER - handles both CLI arguments and interactive menu
REM ============================================================================
if "%~1"=="" goto :menu
if /i "%~1"=="setup"            goto :full_setup
if /i "%~1"=="generate"         goto :generate_all
if /i "%~1"=="start"            goto :start_all
if /i "%~1"=="start-etcd"       goto :start_etcd
if /i "%~1"=="start-patroni"    goto :start_patroni
if /i "%~1"=="start-haproxy"    goto :start_haproxy
if /i "%~1"=="start-monitoring" goto :start_monitoring
if /i "%~1"=="stop"             goto :stop_all
if /i "%~1"=="remove"           goto :remove_all
if /i "%~1"=="status"           goto :show_status
if /i "%~1"=="logs"             goto :show_logs
if /i "%~1"=="failover-test"    goto :failover_test
echo Unknown command: %~1
goto :eof

REM ============================================================================
REM  INTERACTIVE MENU
REM ============================================================================
:menu
cls
echo.
echo  +-------------------------------------------------------------+
echo  :      PostgreSQL HA Cluster Manager (Docker Compose)         :
echo  :      Patroni / etcd / HAProxy / Prometheus / Grafana        :
echo  +-------------------------------------------------------------+
echo  :                                                             :
echo  :   1.  Full Setup + Start All                                :
echo  :   2.  Generate Configs, Certs and Images Only               :
echo  :                                                             :
echo  :   3.  Start All Services                                    :
echo  :   4.  Step: Start etcd Cluster                              :
echo  :   5.  Step: Start Patroni / PostgreSQL                      :
echo  :   6.  Step: Start HAProxy                                   :
echo  :   7.  Step: Start Monitoring + Landing Page                 :
echo  :                                                             :
echo  :   8.  Stop All Services                                     :
echo  :   9.  REMOVE Everything (stack, images, data)               :
echo  :                                                             :
echo  :  10.  Cluster Status                                        :
echo  :  11.  Show Logs                                             :
echo  :  12.  Failover Test                                         :
echo  :                                                             :
echo  :   0.  Exit                                                  :
echo  :                                                             :
echo  +-------------------------------------------------------------+
echo.
set /p "choice=  Select option: "
if "%choice%"=="1"  goto :full_setup
if "%choice%"=="2"  goto :generate_all
if "%choice%"=="3"  goto :start_all
if "%choice%"=="4"  goto :start_etcd
if "%choice%"=="5"  goto :start_patroni
if "%choice%"=="6"  goto :start_haproxy
if "%choice%"=="7"  goto :start_monitoring
if "%choice%"=="8"  goto :stop_all
if "%choice%"=="9"  goto :remove_all
if "%choice%"=="10" goto :show_status
if "%choice%"=="11" goto :show_logs
if "%choice%"=="12" goto :failover_test
if "%choice%"=="0"  goto :eof
echo Invalid option.
timeout /t 2 >nul
goto :menu

REM ============================================================================
REM  FULL SETUP - The one-command-does-everything entry point.
REM  Generates all configs, builds the Patroni image, and starts the stack.
REM ============================================================================
:full_setup
echo.
echo ========================================
echo  FULL SETUP - Generate + Build + Start
echo ========================================
call :check_docker
if errorlevel 1 goto :done
call :generate_all
if errorlevel 1 goto :done
call :start_all
call :post_setup_extensions
echo.
echo ========================================
echo  SETUP COMPLETE
echo ========================================
echo.
echo  Access points:
echo    Landing page:      http://localhost:%LANDING_PORT%  (topology, status, configs)
echo    PostgreSQL Write (primary):  localhost:%HAPROXY_WRITE_PORT%
echo    PostgreSQL Read  (replicas): localhost:%HAPROXY_READ_PORT%
echo    HAProxy Stats:    http://localhost:%HAPROXY_STATS_PORT%  (%HAPROXY_STATS_USER%/%HAPROXY_STATS_PASSWORD%)
echo    Patroni 1 API:    http://localhost:%PATRONI1_API_PORT%
echo    Patroni 2 API:    http://localhost:%PATRONI2_API_PORT%
echo    Patroni 3 API:    http://localhost:%PATRONI3_API_PORT%
echo    Prometheus:        http://localhost:%PROMETHEUS_PORT%
echo    Grafana:           http://localhost:%GRAFANA_PORT%  (%GRAFANA_ADMIN_USER%/%GRAFANA_ADMIN_PASSWORD%)
echo.
echo  Connect via psql:
echo    psql -h localhost -p %HAPROXY_WRITE_PORT% -U %PG_SUPERUSER% -d postgres
echo.
echo  Check cluster:
echo    docker exec -it %PATRONI1_NAME% patronictl -c /etc/patroni/patroni.yml list
echo.
echo  PG data persisted at: %PG_DATA_DIR%
echo.
goto :done

REM ============================================================================
REM  DOCKER CHECK - Ensures Docker Desktop is running before any operation.
REM ============================================================================
:check_docker
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker is not running. Please start Docker Desktop first.
    exit /b 1
)
echo [OK] Docker is running.
exit /b 0

REM ============================================================================
REM  GENERATE ALL - Creates every config file, certificate, and Docker image
REM  needed to run the cluster. Safe to re-run (skips existing certs).
REM ============================================================================
:generate_all
echo.
echo [PHASE] Generating all configs, certificates and Docker images...
call :create_dirs
call :generate_certs
if errorlevel 1 exit /b 1
call :gen_patroni_dockerfile
call :gen_patroni_entrypoint
call :gen_patroni_configs
call :gen_haproxy_config
call :gen_prometheus_config
call :gen_grafana_configs
call :gen_dashboard
call :check_landing
call :gen_docker_compose
call :fix_line_endings
call :build_patroni_image
if errorlevel 1 exit /b 1
echo.
echo [PHASE] All configs and images generated successfully.
exit /b 0

REM ============================================================================
REM  CREATE DIRECTORY STRUCTURE
REM  Creates the full directory tree under BASE_DIR.
REM ============================================================================
:create_dirs
echo [STEP] Creating directory structure at %BASE_DIR%...
if not exist "%BASE_DIR%" mkdir "%BASE_DIR%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%CERTS_DIR%" mkdir "%CERTS_DIR%"
if not exist "%PATRONI_BUILD_DIR%" mkdir "%PATRONI_BUILD_DIR%"
if not exist "%HAPROXY_CFG_DIR%" mkdir "%HAPROXY_CFG_DIR%"
if not exist "%PROMETHEUS_CFG_DIR%" mkdir "%PROMETHEUS_CFG_DIR%"
if not exist "%PG_EXPORTER_CFG_DIR%" mkdir "%PG_EXPORTER_CFG_DIR%"
if not exist "%GRAFANA_PROV_DIR%\datasources" mkdir "%GRAFANA_PROV_DIR%\datasources"
if not exist "%GRAFANA_PROV_DIR%\dashboards" mkdir "%GRAFANA_PROV_DIR%\dashboards"
if not exist "%GRAFANA_DASH_DIR%" mkdir "%GRAFANA_DASH_DIR%"
if not exist "%PG_DATA_DIR%\patroni1" mkdir "%PG_DATA_DIR%\patroni1"
if not exist "%PG_DATA_DIR%\patroni2" mkdir "%PG_DATA_DIR%\patroni2"
if not exist "%PG_DATA_DIR%\patroni3" mkdir "%PG_DATA_DIR%\patroni3"
if not exist "%ETCD_DATA_DIR%\etcd1" mkdir "%ETCD_DATA_DIR%\etcd1"
if not exist "%ETCD_DATA_DIR%\etcd2" mkdir "%ETCD_DATA_DIR%\etcd2"
if not exist "%ETCD_DATA_DIR%\etcd3" mkdir "%ETCD_DATA_DIR%\etcd3"
if not exist "%BASE_DIR%\prometheus-data" mkdir "%BASE_DIR%\prometheus-data"
if not exist "%BASE_DIR%\grafana-data" mkdir "%BASE_DIR%\grafana-data"
echo [OK] Directory structure created.
exit /b 0

REM ============================================================================
REM  GENERATE TLS CERTIFICATES
REM  Creates a self-signed CA and issues certs for etcd, PostgreSQL, HAProxy.
REM  All certs share one CA so components can verify each other.
REM  Runs OpenSSL inside an Alpine container to avoid needing it on Windows.
REM  Skips if certificates already exist - delete certs/ folder to regenerate.
REM ============================================================================
:generate_certs
if exist "%CERTS_DIR%\ca.crt" (
    echo [SKIP] Certificates already exist. Delete %CERTS_DIR% to regenerate.
    exit /b 0
)
echo [STEP] Generating TLS certificates...
set "CS=%CERTS_DIR%\gen.sh"
>"%CS%" echo #!/bin/sh
>>"%CS%" echo set -e
>>"%CS%" echo cd /certs
>>"%CS%" echo echo "--- CA ---"
>>"%CS%" echo openssl genrsa -out ca.key 4096
>>"%CS%" echo openssl req -x509 -new -nodes -key ca.key -sha256 -days %CERT_VALIDITY_DAYS% -subj "/C=%CERT_COUNTRY%/ST=%CERT_STATE%/L=%CERT_CITY%/O=%CERT_ORG%/CN=PatroniCA" -out ca.crt
>>"%CS%" echo echo "--- etcd server ---"
>>"%CS%" echo openssl genrsa -out etcd-server.key 2048
>>"%CS%" echo openssl req -new -key etcd-server.key -subj "/CN=etcd" -addext "subjectAltName=DNS:etcd1,DNS:etcd2,DNS:etcd3,DNS:localhost,IP:127.0.0.1" -out etcd-server.csr
>>"%CS%" echo openssl x509 -req -in etcd-server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out etcd-server.crt -days %CERT_VALIDITY_DAYS% -sha256 -copy_extensions copyall
>>"%CS%" echo echo "--- etcd peer ---"
>>"%CS%" echo openssl genrsa -out etcd-peer.key 2048
>>"%CS%" echo openssl req -new -key etcd-peer.key -subj "/CN=etcd-peer" -addext "subjectAltName=DNS:etcd1,DNS:etcd2,DNS:etcd3,DNS:localhost,IP:127.0.0.1" -out etcd-peer.csr
>>"%CS%" echo openssl x509 -req -in etcd-peer.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out etcd-peer.crt -days %CERT_VALIDITY_DAYS% -sha256 -copy_extensions copyall
>>"%CS%" echo echo "--- etcd client ---"
>>"%CS%" echo openssl genrsa -out etcd-client.key 2048
>>"%CS%" echo openssl req -new -key etcd-client.key -subj "/CN=etcd-client" -addext "subjectAltName=DNS:patroni1,DNS:patroni2,DNS:patroni3,DNS:localhost,IP:127.0.0.1" -out etcd-client.csr
>>"%CS%" echo openssl x509 -req -in etcd-client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out etcd-client.crt -days %CERT_VALIDITY_DAYS% -sha256 -copy_extensions copyall
>>"%CS%" echo echo "--- pg server ---"
>>"%CS%" echo openssl genrsa -out pg-server.key 2048
>>"%CS%" echo openssl req -new -key pg-server.key -subj "/CN=postgresql" -addext "subjectAltName=DNS:patroni1,DNS:patroni2,DNS:patroni3,DNS:haproxy,DNS:localhost,IP:127.0.0.1" -out pg-server.csr
>>"%CS%" echo openssl x509 -req -in pg-server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out pg-server.crt -days %CERT_VALIDITY_DAYS% -sha256 -copy_extensions copyall
>>"%CS%" echo echo "--- haproxy ---"
>>"%CS%" echo openssl genrsa -out haproxy.key 2048
>>"%CS%" echo openssl req -new -key haproxy.key -subj "/CN=haproxy" -addext "subjectAltName=DNS:haproxy,DNS:localhost,IP:127.0.0.1" -out haproxy.csr
>>"%CS%" echo openssl x509 -req -in haproxy.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out haproxy.crt -days %CERT_VALIDITY_DAYS% -sha256 -copy_extensions copyall
>>"%CS%" echo cat haproxy.crt haproxy.key ^> haproxy-combined.pem
>>"%CS%" echo rm -f *.csr
>>"%CS%" echo chmod 600 *.key haproxy-combined.pem
>>"%CS%" echo echo "=== DONE ==="
docker run --rm -v "%CERTS_DIR%:/certs" alpine:latest sh -c "apk add --no-cache openssl > /dev/null 2>&1 && sed -i 's/\r$//' /certs/gen.sh && sh /certs/gen.sh"
if %errorlevel% neq 0 (
    echo [ERROR] Certificate generation failed.
    exit /b 1
)
del "%CS%" 2>nul
echo [OK] TLS certificates generated in %CERTS_DIR%
exit /b 0

REM ============================================================================
REM  PATRONI DOCKERFILE
REM  Builds on the official postgres image (PG_IMAGE_TAG). Installs Patroni
REM  via pip with the [etcd3] extra for etcd v3 API support.
REM  The entrypoint script waits for etcd then starts Patroni as the
REM  postgres user (via gosu).
REM ============================================================================
:gen_patroni_dockerfile
echo [STEP] Generating Patroni Dockerfile...
set "DF=%PATRONI_BUILD_DIR%\Dockerfile"
>"%DF%" echo FROM postgres:%PG_IMAGE_TAG%
>>"%DF%" echo ENV PATRONI_VERSION=%PATRONI_VERSION%
>>"%DF%" echo ENV PSYCOPG2_VERSION=%PSYCOPG2_VERSION%
>>"%DF%" echo RUN apt-get update ^&^& apt-get install -y --no-install-recommends python3-pip python3-venv curl jq ^&^& pip3 install --break-system-packages --no-cache-dir "patroni[etcd3]==${PATRONI_VERSION}" "psycopg2-binary==${PSYCOPG2_VERSION}" ^&^& apt-get clean ^&^& rm -rf /var/lib/apt/lists/*
>>"%DF%" echo RUN mkdir -p /etc/patroni/certs /var/lib/postgresql/data
>>"%DF%" echo COPY entrypoint.sh /usr/local/bin/entrypoint.sh
>>"%DF%" echo RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh
>>"%DF%" echo RUN chmod +x /usr/local/bin/entrypoint.sh
>>"%DF%" echo EXPOSE 5432 8008
>>"%DF%" echo ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
echo [OK] Patroni Dockerfile created.
exit /b 0

REM ============================================================================
REM  PATRONI ENTRYPOINT
REM  Shell script that runs inside the container at startup:
REM  1. Fixes certificate permissions (certs are mounted read-only)
REM  2. Creates data directory with correct ownership
REM  3. Waits for etcd to be healthy (polls /health endpoint)
REM  4. Starts Patroni as the postgres user
REM ============================================================================
:gen_patroni_entrypoint
echo [STEP] Generating Patroni entrypoint script...
set "EP=%PATRONI_BUILD_DIR%\entrypoint.sh"
>"%EP%" echo #!/bin/bash
>>"%EP%" echo set -e
>>"%EP%" echo echo "=== Patroni Entrypoint ==="
>>"%EP%" echo echo "Node: ${PATRONI_NAME}"
>>"%EP%" echo if [ -d /etc/patroni/certs ]; then
>>"%EP%" echo   chmod 600 /etc/patroni/certs/*.key 2^>/dev/null ^|^| true
>>"%EP%" echo   chown -R postgres:postgres /etc/patroni/certs 2^>/dev/null ^|^| true
>>"%EP%" echo fi
>>"%EP%" echo mkdir -p /var/lib/postgresql/data
>>"%EP%" echo chown -R postgres:postgres /var/lib/postgresql/data
>>"%EP%" echo chmod 700 /var/lib/postgresql/data
>>"%EP%" echo echo "Waiting for etcd cluster..."
>>"%EP%" echo ETCD_HOST=$(echo ${ETCD_HOSTS:-etcd1:2379} ^| cut -d',' -f1)
>>"%EP%" echo until curl -sf "http://${ETCD_HOST}/health" ^> /dev/null 2^>^&1; do
>>"%EP%" echo   echo "  etcd not ready at ${ETCD_HOST}, retrying in 3s..."
>>"%EP%" echo   sleep 3
>>"%EP%" echo done
>>"%EP%" echo echo "etcd is available."
>>"%EP%" echo exec gosu postgres patroni /etc/patroni/patroni.yml
echo [OK] Patroni entrypoint script created.
exit /b 0

REM ============================================================================
REM  PATRONI CONFIGURATION (YAML)
REM  One file per node, differing only in name and connect_address.
REM
REM  Key sections:
REM    scope:      Cluster name in etcd
REM    restapi:    Patroni REST API (used by HAProxy for health checks)
REM    etcd3:      Connection to etcd cluster (v3 protocol)
REM    bootstrap:  One-time cluster initialisation config:
REM      dcs:        Dynamic config stored in etcd (editable later via
REM                  patronictl edit-config)
REM      initdb:     PostgreSQL initdb options (checksums, encoding)
REM      pg_hba:     Client authentication rules
REM      (bootstrap.users was removed in Patroni 4.0 - the replication and
REM       superuser roles come from postgresql.authentication; the app user
REM       is created by :post_setup_extensions)
REM    postgresql: Per-node PostgreSQL settings
REM
REM  IMPORTANT: After initial bootstrap, the 'bootstrap.dcs' section is
REM  ignored. Use 'patronictl edit-config' to change cluster-wide settings.
REM ============================================================================
:gen_patroni_configs
echo [STEP] Generating Patroni YAML configurations...
call :write_patroni_yml "%CONFIG_DIR%\patroni1.yml" "patroni1"
call :write_patroni_yml "%CONFIG_DIR%\patroni2.yml" "patroni2"
call :write_patroni_yml "%CONFIG_DIR%\patroni3.yml" "patroni3"
echo [OK] Patroni configs created.
exit /b 0

:write_patroni_yml
set "YF=%~1"
set "NN=%~2"
>"%YF%" echo scope: %CLUSTER_NAME%
>>"%YF%" echo name: %NN%
>>"%YF%" echo.
>>"%YF%" echo restapi:
>>"%YF%" echo   listen: 0.0.0.0:8008
>>"%YF%" echo   connect_address: %NN%:8008
>>"%YF%" echo.
>>"%YF%" echo etcd3:
>>"%YF%" echo   hosts: etcd1:2379,etcd2:2379,etcd3:2379
>>"%YF%" echo.
>>"%YF%" echo bootstrap:
>>"%YF%" echo   dcs:
>>"%YF%" echo     ttl: 30
>>"%YF%" echo     loop_wait: 10
>>"%YF%" echo     retry_timeout: 10
>>"%YF%" echo     maximum_lag_on_failover: 1048576
>>"%YF%" echo     postgresql:
>>"%YF%" echo       use_pg_rewind: true
>>"%YF%" echo       use_slots: true
>>"%YF%" echo       parameters:
>>"%YF%" echo         max_connections: 200
>>"%YF%" echo         max_worker_processes: 8
>>"%YF%" echo         wal_level: replica
>>"%YF%" echo         hot_standby: "on"
>>"%YF%" echo         max_wal_senders: 10
>>"%YF%" echo         max_replication_slots: 10
>>"%YF%" echo         hot_standby_feedback: "on"
>>"%YF%" echo         wal_log_hints: "on"
>>"%YF%" echo         wal_keep_size: "512MB"
>>"%YF%" echo         shared_buffers: "256MB"
>>"%YF%" echo         work_mem: "8MB"
>>"%YF%" echo         maintenance_work_mem: "128MB"
>>"%YF%" echo         effective_cache_size: "768MB"
>>"%YF%" echo         checkpoint_completion_target: 0.9
>>"%YF%" echo         default_statistics_target: 100
>>"%YF%" echo         logging_collector: "on"
>>"%YF%" echo         log_directory: "log"
>>"%YF%" echo         log_filename: "postgresql-%%Y-%%m-%%d_%%H%%M%%S.log"
>>"%YF%" echo         log_statement: "ddl"
>>"%YF%" echo         log_min_duration_statement: 1000
>>"%YF%" echo         track_activities: "on"
>>"%YF%" echo         track_counts: "on"
>>"%YF%" echo         track_io_timing: "on"
>>"%YF%" echo         track_functions: "all"
>>"%YF%" echo         shared_preload_libraries: "pg_stat_statements"
>>"%YF%" echo   initdb:
>>"%YF%" echo     - encoding: UTF8
>>"%YF%" echo     - data-checksums
>>"%YF%" echo   pg_hba:
>>"%YF%" echo     - host replication %PG_REPL_USER% 0.0.0.0/0 scram-sha-256
>>"%YF%" echo     - host all all 0.0.0.0/0 scram-sha-256
>>"%YF%" echo.
>>"%YF%" echo postgresql:
>>"%YF%" echo   listen: 0.0.0.0:5432
>>"%YF%" echo   connect_address: %NN%:5432
>>"%YF%" echo   data_dir: /var/lib/postgresql/data/patroni
>>"%YF%" echo   pgpass: /tmp/pgpass0
>>"%YF%" echo   authentication:
>>"%YF%" echo     replication:
>>"%YF%" echo       username: %PG_REPL_USER%
>>"%YF%" echo       password: "%PG_REPL_PASSWORD%"
>>"%YF%" echo     superuser:
>>"%YF%" echo       username: %PG_SUPERUSER%
>>"%YF%" echo       password: "%PG_SUPERUSER_PASSWORD%"
>>"%YF%" echo   parameters:
>>"%YF%" echo     unix_socket_directories: "/var/run/postgresql"
>>"%YF%" echo   create_replica_methods:
>>"%YF%" echo     - basebackup
>>"%YF%" echo   basebackup:
>>"%YF%" echo     checkpoint: fast
>>"%YF%" echo     max-rate: "100M"
exit /b 0

REM ============================================================================
REM  HAPROXY CONFIGURATION
REM  Routes PostgreSQL connections based on Patroni REST API health checks.
REM
REM  postgresql_write (port 5000):
REM    - Checks GET /primary on Patroni API (port 8008)
REM    - Returns 200 ONLY from the current leader
REM    - Uses "first" algorithm: all traffic to the single healthy leader
REM
REM  postgresql_read (port 5001):
REM    - Checks GET /replica on Patroni API
REM    - Returns 200 ONLY from standbys
REM    - Uses round-robin across available replicas
REM
REM  stats (port 7000):
REM    - HTTP dashboard showing backend health (green/red per node)
REM ============================================================================
:gen_haproxy_config
echo [STEP] Generating HAProxy configuration...
set "HF=%HAPROXY_CFG_DIR%\haproxy.cfg"
>"%HF%" echo global
>>"%HF%" echo     maxconn 2000
>>"%HF%" echo     log stdout format raw local0
>>"%HF%" echo.
>>"%HF%" echo defaults
>>"%HF%" echo     mode tcp
>>"%HF%" echo     log global
>>"%HF%" echo     retries 3
>>"%HF%" echo     timeout connect 10s
>>"%HF%" echo     timeout client 30m
>>"%HF%" echo     timeout server 30m
>>"%HF%" echo     timeout check 5s
>>"%HF%" echo     maxconn 1000
>>"%HF%" echo.
>>"%HF%" echo # Docker embedded DNS: lets HAProxy start before the Patroni containers
>>"%HF%" echo # exist and follow their IPs if they are recreated.
>>"%HF%" echo resolvers docker
>>"%HF%" echo     nameserver dns1 127.0.0.11:53
>>"%HF%" echo     resolve_retries 3
>>"%HF%" echo     timeout resolve 1s
>>"%HF%" echo     timeout retry 1s
>>"%HF%" echo     hold valid 10s
>>"%HF%" echo     hold other 10s
>>"%HF%" echo     hold refused 10s
>>"%HF%" echo     hold nx 10s
>>"%HF%" echo     hold timeout 10s
>>"%HF%" echo.
>>"%HF%" echo listen stats
>>"%HF%" echo     mode http
>>"%HF%" echo     bind *:7000
>>"%HF%" echo     stats enable
>>"%HF%" echo     stats uri /
>>"%HF%" echo     stats refresh 10s
>>"%HF%" echo     stats show-legends
>>"%HF%" echo     stats show-node
>>"%HF%" echo     stats auth %HAPROXY_STATS_USER%:%HAPROXY_STATS_PASSWORD%
>>"%HF%" echo.
>>"%HF%" echo listen postgresql_write
>>"%HF%" echo     bind *:5000
>>"%HF%" echo     option httpchk GET /primary
>>"%HF%" echo     http-check expect status 200
>>"%HF%" echo     default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions resolvers docker init-addr last,libc,none
>>"%HF%" echo     server patroni1 patroni1:5432 maxconn 200 check port 8008
>>"%HF%" echo     server patroni2 patroni2:5432 maxconn 200 check port 8008
>>"%HF%" echo     server patroni3 patroni3:5432 maxconn 200 check port 8008
>>"%HF%" echo.
>>"%HF%" echo listen postgresql_read
>>"%HF%" echo     bind *:5001
>>"%HF%" echo     balance roundrobin
>>"%HF%" echo     option httpchk GET /replica
>>"%HF%" echo     http-check expect status 200
>>"%HF%" echo     default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions resolvers docker init-addr last,libc,none
>>"%HF%" echo     server patroni1 patroni1:5432 maxconn 200 check port 8008
>>"%HF%" echo     server patroni2 patroni2:5432 maxconn 200 check port 8008
>>"%HF%" echo     server patroni3 patroni3:5432 maxconn 200 check port 8008
echo [OK] HAProxy config created.
exit /b 0

REM ============================================================================
REM  PROMETHEUS CONFIGURATION
REM  Scrapes metrics from:
REM  - 3x Patroni REST API /metrics endpoints (patroni_* metrics)
REM  - 3x postgres_exporters (pg_* metrics, one per PG node)
REM  - 3x etcd /metrics endpoints (etcd_* metrics)
REM ============================================================================
:gen_prometheus_config
echo [STEP] Generating Prometheus configuration...
set "PF=%PROMETHEUS_CFG_DIR%\prometheus.yml"
>"%PF%" echo global:
>>"%PF%" echo   scrape_interval: 15s
>>"%PF%" echo   evaluation_interval: 15s
>>"%PF%" echo.
>>"%PF%" echo scrape_configs:
>>"%PF%" echo   - job_name: "patroni"
>>"%PF%" echo     scrape_interval: 10s
>>"%PF%" echo     metrics_path: /metrics
>>"%PF%" echo     static_configs:
>>"%PF%" echo       - targets:
>>"%PF%" echo           - "patroni1:8008"
>>"%PF%" echo           - "patroni2:8008"
>>"%PF%" echo           - "patroni3:8008"
>>"%PF%" echo         labels:
>>"%PF%" echo           cluster: "%CLUSTER_NAME%"
>>"%PF%" echo.
>>"%PF%" echo   # Job name "postgres" matches what standard Grafana community
>>"%PF%" echo   # dashboards expect (e.g. dashboards 9628, 455, 12273, 14114).
>>"%PF%" echo   # relabel_configs rewrites the instance label from the exporter
>>"%PF%" echo   # address (pg-exporter1:9187) to the actual PG server
>>"%PF%" echo   # (patroni1:5432) so standard dashboard variables work.
>>"%PF%" echo   - job_name: "postgres"
>>"%PF%" echo     scrape_interval: 15s
>>"%PF%" echo     static_configs:
>>"%PF%" echo       - targets: ["pg-exporter1:9187"]
>>"%PF%" echo         labels:
>>"%PF%" echo           cluster: "%CLUSTER_NAME%"
>>"%PF%" echo           pg_node: "patroni1"
>>"%PF%" echo       - targets: ["pg-exporter2:9187"]
>>"%PF%" echo         labels:
>>"%PF%" echo           cluster: "%CLUSTER_NAME%"
>>"%PF%" echo           pg_node: "patroni2"
>>"%PF%" echo       - targets: ["pg-exporter3:9187"]
>>"%PF%" echo         labels:
>>"%PF%" echo           cluster: "%CLUSTER_NAME%"
>>"%PF%" echo           pg_node: "patroni3"
>>"%PF%" echo     relabel_configs:
>>"%PF%" echo       - source_labels: [pg_node]
>>"%PF%" echo         target_label: instance
>>"%PF%" echo         regex: (.+)
>>"%PF%" echo         replacement: ${1}:5432
>>"%PF%" echo.
>>"%PF%" echo   - job_name: "etcd"
>>"%PF%" echo     scrape_interval: 15s
>>"%PF%" echo     metrics_path: /metrics
>>"%PF%" echo     static_configs:
>>"%PF%" echo       - targets:
>>"%PF%" echo           - "etcd1:2379"
>>"%PF%" echo           - "etcd2:2379"
>>"%PF%" echo           - "etcd3:2379"
echo [OK] Prometheus config created.
exit /b 0

REM ============================================================================
REM  GRAFANA PROVISIONING - auto-configures datasource and dashboard provider
REM ============================================================================
:gen_grafana_configs
echo [STEP] Generating Grafana provisioning configs...
set "GD=%GRAFANA_PROV_DIR%\datasources\datasource.yml"
>"%GD%" echo apiVersion: 1
>>"%GD%" echo datasources:
>>"%GD%" echo   - name: Prometheus
>>"%GD%" echo     type: prometheus
>>"%GD%" echo     access: proxy
>>"%GD%" echo     url: http://prometheus:9090
>>"%GD%" echo     isDefault: true
>>"%GD%" echo     editable: true
set "GP=%GRAFANA_PROV_DIR%\dashboards\provider.yml"
>"%GP%" echo apiVersion: 1
>>"%GP%" echo providers:
>>"%GP%" echo   - name: "default"
>>"%GP%" echo     orgId: 1
>>"%GP%" echo     folder: "PostgreSQL HA"
>>"%GP%" echo     type: file
>>"%GP%" echo     disableDeletion: false
>>"%GP%" echo     editable: true
>>"%GP%" echo     options:
>>"%GP%" echo       path: /var/lib/grafana/dashboards
>>"%GP%" echo       foldersFromFilesStructure: false
echo [OK] Grafana provisioning created.
exit /b 0

REM ============================================================================
REM  GRAFANA DASHBOARD
REM  Generated by gen_monitoring.py (placed alongside this CMD script).
REM  Uses ONLY default postgres_exporter metrics + Patroni /metrics.
REM  No custom queries = no exporter crashes.
REM ============================================================================
:gen_dashboard
echo [STEP] Generating Grafana dashboard via gen_monitoring.py...
if not exist "%BASE_DIR%\gen_monitoring.py" (
    echo [ERROR] gen_monitoring.py not found in %BASE_DIR%
    echo         Place gen_monitoring.py alongside this CMD script.
    exit /b 1
)
copy /y "%BASE_DIR%\gen_monitoring.py" "%CONFIG_DIR%\gen_monitoring.py" >nul
docker run --rm -v "%CONFIG_DIR%:/config" -v "%GRAFANA_DASH_DIR%:/out" python:3-slim sh -c "sed -i 's/\r$//' /config/gen_monitoring.py && python3 /config/gen_monitoring.py /out/pg-ha-cluster.json"
if %errorlevel% neq 0 (
    echo [WARN] Dashboard generation failed. Creating fallback.
    >"%GRAFANA_DASH_DIR%\pg-ha-cluster.json" echo {"title":"PostgreSQL HA","uid":"pg-ha-patroni-cluster","panels":[],"schemaVersion":39}
)
echo [OK] Grafana dashboard created.
exit /b 0

REM ============================================================================
REM  LANDING PAGE
REM  The landing app (server.py + index.html) lives in landing\ alongside this
REM  script, like gen_monitoring.py. Nothing is generated - it is just mounted.
REM ============================================================================
:check_landing
if not exist "%LANDING_DIR%\server.py" (
    echo [WARN] %LANDING_DIR%\server.py not found - the landing container will not start.
    exit /b 0
)
echo [OK] Landing page found at %LANDING_DIR%
exit /b 0

REM ============================================================================
REM  DOCKER COMPOSE FILE
REM  Defines all 13 services as a single stack. Key design decisions:
REM  - etcd nodes have healthchecks; Patroni depends_on them with
REM    condition: service_healthy (won't start until etcd is ready)
REM  - HAProxy depends on Patroni (won't start until PG is initialising)
REM  - 3 pg_exporters connect DIRECTLY to each PG node (not through HAProxy)
REM    so we get metrics from both primary and replicas
REM  - All services share a single Docker network for inter-container DNS
REM  - Host paths are RELATIVE (./pgdata/...). Compose resolves them against
REM    the compose file's folder, so the generated file contains no machine-
REM    specific paths and the whole folder can be moved or cloned anywhere.
REM ============================================================================
:gen_docker_compose
echo [STEP] Generating docker-compose.yml...
set "CF=%COMPOSE_FILE%"
set "IC=%ETCD1_NAME%=http://%ETCD1_NAME%:2380,%ETCD2_NAME%=http://%ETCD2_NAME%:2380,%ETCD3_NAME%=http://%ETCD3_NAME%:2380"

>"%CF%" echo services:
>>"%CF%" echo.
>>"%CF%" echo   # --- ETCD CLUSTER (3 nodes) ---
>>"%CF%" echo   %ETCD1_NAME%:
>>"%CF%" echo     image: %ETCD_IMAGE%
>>"%CF%" echo     container_name: %ETCD1_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%ETCD1_CLIENT_PORT%:2379"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./etcd-data/etcd1:/etcd-data
>>"%CF%" echo     command: ["/usr/local/bin/etcd", "--name=%ETCD1_NAME%", "--data-dir=/etcd-data", "--listen-client-urls=http://0.0.0.0:2379", "--advertise-client-urls=http://%ETCD1_NAME%:2379", "--listen-peer-urls=http://0.0.0.0:2380", "--initial-advertise-peer-urls=http://%ETCD1_NAME%:2380", "--initial-cluster=%IC%", "--initial-cluster-token=%ETCD_CLUSTER_TOKEN%", "--initial-cluster-state=new", "--logger=zap", "--log-level=info"]
>>"%CF%" echo     healthcheck:
>>"%CF%" echo       test: ["CMD", "/usr/local/bin/etcdctl", "endpoint", "health", "--endpoints=http://localhost:2379"]
>>"%CF%" echo       interval: 10s
>>"%CF%" echo       timeout: 5s
>>"%CF%" echo       retries: 5
>>"%CF%" echo.
>>"%CF%" echo   %ETCD2_NAME%:
>>"%CF%" echo     image: %ETCD_IMAGE%
>>"%CF%" echo     container_name: %ETCD2_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%ETCD2_CLIENT_PORT%:2379"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./etcd-data/etcd2:/etcd-data
>>"%CF%" echo     command: ["/usr/local/bin/etcd", "--name=%ETCD2_NAME%", "--data-dir=/etcd-data", "--listen-client-urls=http://0.0.0.0:2379", "--advertise-client-urls=http://%ETCD2_NAME%:2379", "--listen-peer-urls=http://0.0.0.0:2380", "--initial-advertise-peer-urls=http://%ETCD2_NAME%:2380", "--initial-cluster=%IC%", "--initial-cluster-token=%ETCD_CLUSTER_TOKEN%", "--initial-cluster-state=new", "--logger=zap", "--log-level=info"]
>>"%CF%" echo     healthcheck:
>>"%CF%" echo       test: ["CMD", "/usr/local/bin/etcdctl", "endpoint", "health", "--endpoints=http://localhost:2379"]
>>"%CF%" echo       interval: 10s
>>"%CF%" echo       timeout: 5s
>>"%CF%" echo       retries: 5
>>"%CF%" echo.
>>"%CF%" echo   %ETCD3_NAME%:
>>"%CF%" echo     image: %ETCD_IMAGE%
>>"%CF%" echo     container_name: %ETCD3_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%ETCD3_CLIENT_PORT%:2379"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./etcd-data/etcd3:/etcd-data
>>"%CF%" echo     command: ["/usr/local/bin/etcd", "--name=%ETCD3_NAME%", "--data-dir=/etcd-data", "--listen-client-urls=http://0.0.0.0:2379", "--advertise-client-urls=http://%ETCD3_NAME%:2379", "--listen-peer-urls=http://0.0.0.0:2380", "--initial-advertise-peer-urls=http://%ETCD3_NAME%:2380", "--initial-cluster=%IC%", "--initial-cluster-token=%ETCD_CLUSTER_TOKEN%", "--initial-cluster-state=new", "--logger=zap", "--log-level=info"]
>>"%CF%" echo     healthcheck:
>>"%CF%" echo       test: ["CMD", "/usr/local/bin/etcdctl", "endpoint", "health", "--endpoints=http://localhost:2379"]
>>"%CF%" echo       interval: 10s
>>"%CF%" echo       timeout: 5s
>>"%CF%" echo       retries: 5
>>"%CF%" echo.
>>"%CF%" echo   # --- PATRONI / POSTGRESQL (3 nodes) ---
>>"%CF%" echo   %PATRONI1_NAME%:
>>"%CF%" echo     image: %PATRONI_IMAGE_NAME%
>>"%CF%" echo     container_name: %PATRONI1_NAME%
>>"%CF%" echo     hostname: %PATRONI1_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PATRONI1_PG_PORT%:5432"
>>"%CF%" echo       - "%PATRONI1_API_PORT%:8008"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./pgdata/patroni1:/var/lib/postgresql/data
>>"%CF%" echo       - ./config/patroni1.yml:/etc/patroni/patroni.yml:ro
>>"%CF%" echo       - ./certs:/etc/patroni/certs:ro
>>"%CF%" echo     environment:
>>"%CF%" echo       - PATRONI_NAME=%PATRONI1_NAME%
>>"%CF%" echo       - ETCD_HOSTS=etcd1:2379,etcd2:2379,etcd3:2379
>>"%CF%" echo     depends_on:
>>"%CF%" echo       %ETCD1_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD2_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD3_NAME%: { condition: service_healthy }
>>"%CF%" echo.
>>"%CF%" echo   %PATRONI2_NAME%:
>>"%CF%" echo     image: %PATRONI_IMAGE_NAME%
>>"%CF%" echo     container_name: %PATRONI2_NAME%
>>"%CF%" echo     hostname: %PATRONI2_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PATRONI2_PG_PORT%:5432"
>>"%CF%" echo       - "%PATRONI2_API_PORT%:8008"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./pgdata/patroni2:/var/lib/postgresql/data
>>"%CF%" echo       - ./config/patroni2.yml:/etc/patroni/patroni.yml:ro
>>"%CF%" echo       - ./certs:/etc/patroni/certs:ro
>>"%CF%" echo     environment:
>>"%CF%" echo       - PATRONI_NAME=%PATRONI2_NAME%
>>"%CF%" echo       - ETCD_HOSTS=etcd1:2379,etcd2:2379,etcd3:2379
>>"%CF%" echo     depends_on:
>>"%CF%" echo       %ETCD1_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD2_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD3_NAME%: { condition: service_healthy }
>>"%CF%" echo.
>>"%CF%" echo   %PATRONI3_NAME%:
>>"%CF%" echo     image: %PATRONI_IMAGE_NAME%
>>"%CF%" echo     container_name: %PATRONI3_NAME%
>>"%CF%" echo     hostname: %PATRONI3_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PATRONI3_PG_PORT%:5432"
>>"%CF%" echo       - "%PATRONI3_API_PORT%:8008"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./pgdata/patroni3:/var/lib/postgresql/data
>>"%CF%" echo       - ./config/patroni3.yml:/etc/patroni/patroni.yml:ro
>>"%CF%" echo       - ./certs:/etc/patroni/certs:ro
>>"%CF%" echo     environment:
>>"%CF%" echo       - PATRONI_NAME=%PATRONI3_NAME%
>>"%CF%" echo       - ETCD_HOSTS=etcd1:2379,etcd2:2379,etcd3:2379
>>"%CF%" echo     depends_on:
>>"%CF%" echo       %ETCD1_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD2_NAME%: { condition: service_healthy }
>>"%CF%" echo       %ETCD3_NAME%: { condition: service_healthy }
>>"%CF%" echo.
>>"%CF%" echo   # --- HAPROXY ---
>>"%CF%" echo   %HAPROXY_NAME%:
>>"%CF%" echo     image: %HAPROXY_IMAGE%
>>"%CF%" echo     container_name: %HAPROXY_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%HAPROXY_WRITE_PORT%:5000"
>>"%CF%" echo       - "%HAPROXY_READ_PORT%:5001"
>>"%CF%" echo       - "%HAPROXY_STATS_PORT%:7000"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./config/haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PATRONI1_NAME%
>>"%CF%" echo       - %PATRONI2_NAME%
>>"%CF%" echo       - %PATRONI3_NAME%
>>"%CF%" echo.
>>"%CF%" echo   # --- POSTGRES EXPORTERS (one per PG node, default metrics only) ---
>>"%CF%" echo   # No custom queries - built-in collectors only (--extend.query-path and
>>"%CF%" echo   # --auto-discover-databases are deprecated upstream). Extra collectors:
>>"%CF%" echo   # stat_checkpointer (PG17+ checkpoint stats), stat_statements, long_running_transactions.
>>"%CF%" echo   %PG_EXPORTER1_NAME%:
>>"%CF%" echo     image: %PG_EXPORTER_IMAGE%
>>"%CF%" echo     container_name: %PG_EXPORTER1_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PG_EXPORTER1_PORT%:9187"
>>"%CF%" echo     environment:
>>"%CF%" echo       - DATA_SOURCE_NAME=postgresql://%PG_SUPERUSER%:%PG_SUPERUSER_PASSWORD%@patroni1:5432/postgres?sslmode=disable
>>"%CF%" echo     command: ["--collector.stat_checkpointer", "--collector.stat_statements", "--collector.long_running_transactions"]
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PATRONI1_NAME%
>>"%CF%" echo.
>>"%CF%" echo   %PG_EXPORTER2_NAME%:
>>"%CF%" echo     image: %PG_EXPORTER_IMAGE%
>>"%CF%" echo     container_name: %PG_EXPORTER2_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PG_EXPORTER2_PORT%:9187"
>>"%CF%" echo     environment:
>>"%CF%" echo       - DATA_SOURCE_NAME=postgresql://%PG_SUPERUSER%:%PG_SUPERUSER_PASSWORD%@patroni2:5432/postgres?sslmode=disable
>>"%CF%" echo     command: ["--collector.stat_checkpointer", "--collector.stat_statements", "--collector.long_running_transactions"]
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PATRONI2_NAME%
>>"%CF%" echo.
>>"%CF%" echo   %PG_EXPORTER3_NAME%:
>>"%CF%" echo     image: %PG_EXPORTER_IMAGE%
>>"%CF%" echo     container_name: %PG_EXPORTER3_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PG_EXPORTER3_PORT%:9187"
>>"%CF%" echo     environment:
>>"%CF%" echo       - DATA_SOURCE_NAME=postgresql://%PG_SUPERUSER%:%PG_SUPERUSER_PASSWORD%@patroni3:5432/postgres?sslmode=disable
>>"%CF%" echo     command: ["--collector.stat_checkpointer", "--collector.stat_statements", "--collector.long_running_transactions"]
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PATRONI3_NAME%
>>"%CF%" echo.
>>"%CF%" echo   # --- MONITORING ---
>>"%CF%" echo   %PROMETHEUS_NAME%:
>>"%CF%" echo     image: %PROMETHEUS_IMAGE%
>>"%CF%" echo     container_name: %PROMETHEUS_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%PROMETHEUS_PORT%:9090"
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./config/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
>>"%CF%" echo       - ./prometheus-data:/prometheus
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PG_EXPORTER1_NAME%
>>"%CF%" echo.
>>"%CF%" echo   %GRAFANA_NAME%:
>>"%CF%" echo     image: %GRAFANA_IMAGE%
>>"%CF%" echo     container_name: %GRAFANA_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%GRAFANA_PORT%:3000"
>>"%CF%" echo     environment:
>>"%CF%" echo       - GF_SECURITY_ADMIN_USER=%GRAFANA_ADMIN_USER%
>>"%CF%" echo       - GF_SECURITY_ADMIN_PASSWORD=%GRAFANA_ADMIN_PASSWORD%
>>"%CF%" echo       - GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/pg-ha-cluster.json
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./config/grafana/provisioning:/etc/grafana/provisioning:ro
>>"%CF%" echo       - ./config/grafana/dashboards:/var/lib/grafana/dashboards:ro
>>"%CF%" echo       - ./grafana-data:/var/lib/grafana
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %PROMETHEUS_NAME%
>>"%CF%" echo.
>>"%CF%" echo   # --- LANDING PAGE (topology, live status, config viewer) ---
>>"%CF%" echo   # Stdlib-only Python app from landing\. Probes every service over the
>>"%CF%" echo   # compose network and serves their configs (mounted read-only).
>>"%CF%" echo   %LANDING_NAME%:
>>"%CF%" echo     image: %LANDING_IMAGE%
>>"%CF%" echo     container_name: %LANDING_NAME%
>>"%CF%" echo     restart: unless-stopped
>>"%CF%" echo     ports:
>>"%CF%" echo       - "%LANDING_PORT%:8080"
>>"%CF%" echo     environment:
>>"%CF%" echo       - HAPROXY_STATS_AUTH=%HAPROXY_STATS_USER%:%HAPROXY_STATS_PASSWORD%
>>"%CF%" echo     volumes:
>>"%CF%" echo       - ./landing:/app:ro
>>"%CF%" echo       - ./config:/srv/config:ro
>>"%CF%" echo       - ./patroni-image:/srv/patroni-image:ro
>>"%CF%" echo       - ./docker-compose.yml:/srv/docker-compose.yml:ro
>>"%CF%" echo     command: ["python", "-u", "/app/server.py"]
>>"%CF%" echo     healthcheck:
>>"%CF%" echo       test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"]
>>"%CF%" echo       interval: 15s
>>"%CF%" echo       timeout: 5s
>>"%CF%" echo       retries: 3
>>"%CF%" echo     depends_on:
>>"%CF%" echo       - %HAPROXY_NAME%
>>"%CF%" echo       - %PROMETHEUS_NAME%
echo [OK] docker-compose.yml generated at %CF%
exit /b 0

REM ============================================================================
REM  FIX LINE ENDINGS
REM  CMD echo writes CRLF (\r\n) line endings. Linux containers (pg_exporter,
REM  Prometheus, HAProxy) need LF (\n) only. This step strips \r from ALL
REM  generated config files in one pass using an Alpine container.
REM  Without this fix, YAML parsers and config readers inside containers fail
REM  with cryptic errors (HTTP 500, parse errors, etc.)
REM ============================================================================
:fix_line_endings
echo [STEP] Fixing line endings (CRLF to LF) on all config files...
docker run --rm -v "%CONFIG_DIR%:/config" alpine:latest sh -c "find /config -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.cfg' -o -name '*.json' -o -name '*.sh' \) -exec sed -i 's/\r$//' {} +"
if %errorlevel% neq 0 (
    echo [WARN] Line ending fix had issues, but continuing...
)
echo [OK] Line endings fixed.
exit /b 0

REM ============================================================================
REM  BUILD PATRONI DOCKER IMAGE
REM ============================================================================
:build_patroni_image
echo [STEP] Building Patroni Docker image (%PATRONI_IMAGE_NAME%)...
echo        This may take a few minutes on first run...
docker build -t %PATRONI_IMAGE_NAME% "%PATRONI_BUILD_DIR%"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to build Patroni image.
    exit /b 1
)
echo [OK] Patroni image built: %PATRONI_IMAGE_NAME%
exit /b 0

REM ============================================================================
REM  POST-SETUP: Create extensions and app database
REM  Runs AFTER the cluster is healthy. Waits for a leader to appear, then
REM  creates pg_stat_statements and the application database on the primary.
REM  Extensions replicate automatically to standbys via streaming replication.
REM  This is safer than post_bootstrap which has quote-escaping issues.
REM ============================================================================
:post_setup_extensions
echo.
echo [STEP] Waiting for cluster leader to be ready...
set "WAIT_COUNT=0"
:wait_leader_loop
if %WAIT_COUNT% geq 12 (
    echo [WARN] No leader found after 60 seconds. Create extensions manually:
    echo        docker exec -it patroni1 psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
    exit /b 0
)
timeout /t 5 /nobreak >nul
set /a WAIT_COUNT=%WAIT_COUNT%+1
echo   Checking for leader (attempt %WAIT_COUNT%/12)...

REM Try patroni1
docker exec %PATRONI1_NAME% psql -U postgres -tAc "SELECT CASE WHEN pg_is_in_recovery() THEN 'replica' ELSE 'primary' END" 2>nul | findstr /c:"primary" >nul 2>&1
if %errorlevel% equ 0 goto :found_leader1

REM Try patroni2
docker exec %PATRONI2_NAME% psql -U postgres -tAc "SELECT CASE WHEN pg_is_in_recovery() THEN 'replica' ELSE 'primary' END" 2>nul | findstr /c:"primary" >nul 2>&1
if %errorlevel% equ 0 goto :found_leader2

REM Try patroni3
docker exec %PATRONI3_NAME% psql -U postgres -tAc "SELECT CASE WHEN pg_is_in_recovery() THEN 'replica' ELSE 'primary' END" 2>nul | findstr /c:"primary" >nul 2>&1
if %errorlevel% equ 0 goto :found_leader3

goto :wait_leader_loop

:found_leader1
set "LEADER_NODE=%PATRONI1_NAME%"
goto :create_extensions
:found_leader2
set "LEADER_NODE=%PATRONI2_NAME%"
goto :create_extensions
:found_leader3
set "LEADER_NODE=%PATRONI3_NAME%"
goto :create_extensions

:create_extensions
echo [OK] %LEADER_NODE% is the leader. Creating extensions, app user and app database...
docker exec %LEADER_NODE% psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
docker exec %LEADER_NODE% psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='%PG_APP_USER%'" | findstr /c:"1" >nul 2>&1 || docker exec %LEADER_NODE% psql -U postgres -c "CREATE ROLE %PG_APP_USER% LOGIN PASSWORD '%PG_APP_PASSWORD%';"
docker exec %LEADER_NODE% psql -U postgres -c "CREATE DATABASE %PG_APP_DB% OWNER %PG_APP_USER%;" 2>nul
echo [OK] Extensions and app database created.
exit /b 0

REM ============================================================================
REM  START / STOP / REMOVE OPERATIONS
REM ============================================================================

:start_all
echo.
echo [PHASE] Starting all services via Docker Compose...
call :check_docker
if errorlevel 1 exit /b 1
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" up -d
if %errorlevel% neq 0 (
    echo [ERROR] Docker Compose up failed.
    exit /b 1
)
echo [OK] All services started. Check Docker Desktop for the "%COMPOSE_PROJECT%" stack.
echo      Wait ~20-30 seconds for Patroni leader election and replica sync.
exit /b 0

:start_etcd
call :check_docker
if errorlevel 1 exit /b 1
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" up -d %ETCD1_NAME% %ETCD2_NAME% %ETCD3_NAME%
echo [OK] etcd cluster starting.
goto :done

:start_patroni
call :check_docker
if errorlevel 1 exit /b 1
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" up -d %PATRONI1_NAME% %PATRONI2_NAME% %PATRONI3_NAME%
echo [OK] Patroni nodes starting.
goto :done

:start_haproxy
call :check_docker
if errorlevel 1 exit /b 1
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" up -d %HAPROXY_NAME%
echo [OK] HAProxy started. Stats: http://localhost:%HAPROXY_STATS_PORT%
goto :done

:start_monitoring
call :check_docker
if errorlevel 1 exit /b 1
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" up -d %PG_EXPORTER1_NAME% %PG_EXPORTER2_NAME% %PG_EXPORTER3_NAME% %PROMETHEUS_NAME% %GRAFANA_NAME% %LANDING_NAME%
echo [OK] Monitoring started. Landing page: http://localhost:%LANDING_PORT%  Grafana: http://localhost:%GRAFANA_PORT%
goto :done

:stop_all
echo.
echo [PHASE] Stopping all services (data is preserved)...
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" stop
echo [OK] All services stopped. Use option 3 to restart.
goto :done

REM ============================================================================
REM  REMOVE EVERYTHING
REM  Tears down the Docker Compose stack and optionally deletes data.
REM  IMPORTANT: Protects the batch script itself from deletion. Only removes
REM  generated subdirectories (config/, certs/, pgdata/, etc.), NOT the
REM  root BASE_DIR or any .cmd files within it.
REM ============================================================================
:remove_all
echo.
echo  +-------------------------------------------------------+
echo  :  WARNING: This will remove the Docker Compose stack    :
echo  :  and optionally all generated data (configs, certs,    :
echo  :  PG data, Prometheus data, Grafana data).              :
echo  :  Your .cmd script files will NOT be deleted.           :
echo  +-------------------------------------------------------+
echo.
set /p "confirm=  Type YES to confirm stack removal: "
if /i not "%confirm%"=="YES" (
    echo Cancelled.
    goto :done
)

echo.
echo [STEP] Removing Docker Compose stack...
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" down --remove-orphans 2>nul
echo [STEP] Removing Patroni Docker image...
docker rmi %PATRONI_IMAGE_NAME% >nul 2>&1

echo.
set /p "confirmdata=  Also delete generated data (configs, certs, pgdata)? (YES/no): "
if /i "%confirmdata%"=="YES" (
    echo [STEP] Removing generated subdirectories...
    echo        Keeping: %BASE_DIR%\*.cmd  %BASE_DIR%\*.bat  %BASE_DIR%\*.md
    if exist "%CONFIG_DIR%" rd /s /q "%CONFIG_DIR%" 2>nul
    if exist "%CERTS_DIR%" rd /s /q "%CERTS_DIR%" 2>nul
    if exist "%PATRONI_BUILD_DIR%" rd /s /q "%PATRONI_BUILD_DIR%" 2>nul
    if exist "%PG_DATA_DIR%" rd /s /q "%PG_DATA_DIR%" 2>nul
    if exist "%ETCD_DATA_DIR%" rd /s /q "%ETCD_DATA_DIR%" 2>nul
    if exist "%BASE_DIR%\prometheus-data" rd /s /q "%BASE_DIR%\prometheus-data" 2>nul
    if exist "%BASE_DIR%\grafana-data" rd /s /q "%BASE_DIR%\grafana-data" 2>nul
    if exist "%COMPOSE_FILE%" del "%COMPOSE_FILE%" 2>nul
    echo   [OK] Generated data removed. Script files preserved.
) else (
    echo   [SKIP] Data kept.
)
echo.
echo [PHASE] Cleanup complete.
goto :done

REM ============================================================================
REM  STATUS - Shows comprehensive cluster health at a glance
REM ============================================================================
:show_status
echo.
echo  +-------------------------------------------------+
echo  :  PostgreSQL HA Cluster Status                    :
echo  +-------------------------------------------------+
echo.
echo --- Docker Compose Stack ---
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" ps 2>nul
echo.
echo --- etcd Cluster Health ---
docker exec %ETCD1_NAME% /usr/local/bin/etcdctl endpoint health --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379 2>nul
if %errorlevel% neq 0 echo   etcd not available
echo.
echo --- Patroni Cluster ---
docker exec %PATRONI1_NAME% patronictl -c /etc/patroni/patroni.yml list 2>nul
if %errorlevel% neq 0 echo   Patroni not available
echo.
echo --- Patroni REST API ---
echo   patroni1 (%PATRONI1_API_PORT%):
curl -sf http://localhost:%PATRONI1_API_PORT%/ 2>nul
echo.
echo   patroni2 (%PATRONI2_API_PORT%):
curl -sf http://localhost:%PATRONI2_API_PORT%/ 2>nul
echo.
echo   patroni3 (%PATRONI3_API_PORT%):
curl -sf http://localhost:%PATRONI3_API_PORT%/ 2>nul
echo.
echo.
goto :done

REM ============================================================================
REM  LOGS
REM ============================================================================
:show_logs
echo.
echo Which service logs?
echo   1. patroni1        5. etcd1          9.  haproxy
echo   2. patroni2        6. etcd2         10.  prometheus
echo   3. patroni3        7. etcd3         11.  grafana
echo   4. ALL (follow)    8. pg-exporters
echo.
set /p "logchoice=  Select: "
if "%logchoice%"=="1" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %PATRONI1_NAME%
if "%logchoice%"=="2" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %PATRONI2_NAME%
if "%logchoice%"=="3" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %PATRONI3_NAME%
if "%logchoice%"=="4" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 50
if "%logchoice%"=="5" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %ETCD1_NAME%
if "%logchoice%"=="6" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %ETCD2_NAME%
if "%logchoice%"=="7" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %ETCD3_NAME%
if "%logchoice%"=="8" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 50 %PG_EXPORTER1_NAME% %PG_EXPORTER2_NAME% %PG_EXPORTER3_NAME%
if "%logchoice%"=="9"  docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %HAPROXY_NAME%
if "%logchoice%"=="10" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %PROMETHEUS_NAME%
if "%logchoice%"=="11" docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" logs -f --tail 100 %GRAFANA_NAME%
goto :done

REM ============================================================================
REM  FAILOVER TEST
REM ============================================================================
:failover_test
echo.
echo  +-------------------------------------------------+
echo  :  FAILOVER TEST                                  :
echo  +-------------------------------------------------+
echo.
echo  Current cluster state:
docker exec %PATRONI1_NAME% patronictl -c /etc/patroni/patroni.yml list 2>nul
echo.
set /p "fconfirm=  Stop patroni1 to test failover? (yes/no): "
if /i not "%fconfirm%"=="yes" (
    echo Cancelled.
    goto :done
)
echo.
echo [TEST] Stopping patroni1...
docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" stop %PATRONI1_NAME%
echo [TEST] Waiting 15 seconds for failover...
timeout /t 15 /nobreak >nul
echo.
echo [TEST] New cluster state (via patroni2):
docker exec %PATRONI2_NAME% patronictl -c /etc/patroni/patroni.yml list 2>nul
echo.
set /p "rconfirm=  Restart patroni1 to rejoin as replica? (yes/no): "
if /i "%rconfirm%"=="yes" (
    docker compose -p %COMPOSE_PROJECT% -f "%COMPOSE_FILE%" start %PATRONI1_NAME%
    echo [TEST] Waiting 10 seconds for rejoin...
    timeout /t 10 /nobreak >nul
    echo [TEST] Final cluster state:
    docker exec %PATRONI2_NAME% patronictl -c /etc/patroni/patroni.yml list 2>nul
    echo.
)
goto :done

REM ============================================================================
:done
echo.
if "%~1"=="" (
    echo Press any key to return to menu...
    pause >nul
    goto :menu
)
goto :eof

:eof
endlocal
exit /b 0
