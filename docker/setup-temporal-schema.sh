#!/bin/sh
set -eu
umask 077

echo "Applying Temporal PostgreSQL schemas"
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal create
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal update-schema \
  -d /etc/temporal/schema/postgresql/v12/temporal/versioned
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal_visibility create
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal_visibility setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep "${POSTGRES_SEEDS}" -u "${POSTGRES_USER}" \
  -p "${DB_PORT}" --db temporal_visibility update-schema \
  -d /etc/temporal/schema/postgresql/v12/visibility/versioned
