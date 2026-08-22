#!/bin/sh
set -eu
umask 077

: "${ENGINEERING_DB_PASSWORD:?required}"

psql --set=ON_ERROR_STOP=1 --dbname=postgres \
  --set=app_password="${ENGINEERING_DB_PASSWORD}" <<'SQL'
SELECT 'CREATE ROLE engineering_control LOGIN PASSWORD ' || quote_literal(:'app_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'engineering_control') \gexec
SELECT 'CREATE DATABASE engineering_control OWNER engineering_control'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'engineering_control') \gexec
ALTER ROLE engineering_control NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
SQL
