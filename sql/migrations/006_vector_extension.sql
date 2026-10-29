-- =============================================================================
-- 006_vector_extension.sql  -  enable pgvector
--
-- Needs the Postgres image that ships pgvector (docker-compose.yaml:
-- pgvector/pgvector:0.8.1-pg13). With the plain postgres:13 image this fails
-- with "extension vector is not available".
--
-- ⚠️ MUST RUN AS A SUPERUSER: the database is owned by the superuser, not by
-- the ELT user, so the ELT user cannot create extensions. Same convention as
-- 005: files ending in _extension.sql or _role.sql are run by the superuser.
--
--   docker exec -i postgres psql -U "$POSTGRES_CONN_USERNAME" -d "$ELT_DATABASE_NAME" \
--     -v ON_ERROR_STOP=1 < sql/migrations/006_vector_extension.sql
-- Safe to re-run.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

INSERT INTO schema_migrations (version) VALUES ('006_vector_extension')
ON CONFLICT (version) DO NOTHING;

COMMIT;
