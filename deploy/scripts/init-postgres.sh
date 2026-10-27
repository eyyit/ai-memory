#!/bin/sh
set -eu
# Never print credentials; the API role is not the bootstrap superuser.
export MEMORY_DB_PASSWORD="$(cat /run/secrets/postgres_password)"
psql --quiet --set ON_ERROR_STOP=1 --username postgres --dbname ai_memory <<'SQL'
\getenv memory_password MEMORY_DB_PASSWORD
SELECT format('CREATE ROLE ai_memory LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'memory_password') \gexec
ALTER DATABASE ai_memory OWNER TO ai_memory;
SQL
unset MEMORY_DB_PASSWORD
