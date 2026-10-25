#!/bin/bash
# Postgres first-boot initialiser. Runs ONLY when the data directory is empty
# (i.e. on a freshly created postgres_data volume). Creates both the dev
# database (trinity) and the isolated test database (trinity_test) so the test
# suite never has to touch dev data.
#
# For an already-initialised volume this script does NOT run — create the test
# DB manually instead:
#   docker compose exec postgres createdb -U trinity trinity_test
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
  SELECT 'CREATE DATABASE trinity'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'trinity')\gexec
  SELECT 'CREATE DATABASE trinity_test'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'trinity_test')\gexec
  GRANT ALL PRIVILEGES ON DATABASE trinity TO trinity;
  GRANT ALL PRIVILEGES ON DATABASE trinity_test TO trinity;
EOSQL
