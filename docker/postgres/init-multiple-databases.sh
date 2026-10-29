#!/bin/bash

set -e
set -u

function create_user_and_database() {
    local database=$1
    local username=$2
    local password=$3
    echo "Creating user '$username' and database '$database'"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
        CREATE USER $username WITH PASSWORD '$password';
        CREATE DATABASE $database;
        GRANT ALL PRIVILEGES ON DATABASE $database TO $username;
EOSQL
    echo "  User '$username' and database '$database' created successfully"
}

# Metadata database
create_user_and_database $METADATA_DATABASE_NAME $METADATA_DATABASE_USERNAME $METADATA_DATABASE_PASSWORD

# Celery result backend database
create_user_and_database $CELERY_BACKEND_NAME $CELERY_BACKEND_USERNAME $CELERY_BACKEND_PASSWORD

# ELT database
create_user_and_database $ELT_DATABASE_NAME $ELT_DATABASE_USERNAME $ELT_DATABASE_PASSWORD

# Fixed synthetic database for `youtube-intel eval-assistant`, owned by the ELT
# user so its migrations and seed run as that user. NOTE: this script only runs
# on an EMPTY data volume; on an existing machine create it once by hand (README).
echo "Creating database 'yti_eval' (assistant evaluation)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE yti_eval OWNER $ELT_DATABASE_USERNAME;
EOSQL

echo "All databases and users created successfully"