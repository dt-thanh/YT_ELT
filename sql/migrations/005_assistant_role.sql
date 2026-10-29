-- =============================================================================
-- 005_assistant_role.sql  -  read-only database login for the Q&A assistant
--
-- The assistant's tools run pre-written SELECTs only, and they connect as
-- yti_assistant: even a bug (or a prompt-injected request) cannot write data.
--
--   yti_readonly   NOLOGIN group: CONNECT + USAGE on schema yti + SELECT on its
--                  tables/views. No access to yti_staging or to yti.llm_calls.
--   yti_assistant  LOGIN, member of yti_readonly, read-only by default.
--
-- ⚠️ MUST RUN AS A SUPERUSER (creating roles needs CREATEROLE, which the ELT
-- user does not have). The password comes from a psql variable, never from the
-- file itself:
--
--   docker exec -i postgres psql -U "$POSTGRES_CONN_USERNAME" -d "$ELT_DATABASE_NAME" \
--     -v ON_ERROR_STOP=1 -v assistant_password="$ASSISTANT_DB_PASSWORD" \
--     < sql/migrations/005_assistant_role.sql
--
-- Without -v assistant_password the roles and grants are still created, but
-- yti_assistant has no password and cannot log in yet. Safe to re-run.
--
-- Convention: migration files ending in _role.sql are run by the superuser.
-- Later migrations that add objects the assistant must read GRANT SELECT to
-- yti_readonly themselves (GRANT ... ON ALL TABLES only covers existing ones).
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_readonly') THEN
        CREATE ROLE yti_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yti_assistant') THEN
        CREATE ROLE yti_assistant LOGIN IN ROLE yti_readonly;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO yti_readonly', current_database());
END
$$;

GRANT USAGE ON SCHEMA yti TO yti_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA yti TO yti_readonly;
-- The LLM call log is operational data, not something the assistant answers from.
REVOKE ALL ON yti.llm_calls FROM yti_readonly;

-- Defense in depth: even a statement the code forgets to wrap is read-only and bounded.
ALTER ROLE yti_assistant SET default_transaction_read_only = on;
ALTER ROLE yti_assistant SET statement_timeout = '5s';

\if :{?assistant_password}
ALTER ROLE yti_assistant PASSWORD :'assistant_password';
\else
\echo 'NOTICE: no -v assistant_password given; yti_assistant cannot log in until a password is set.'
\endif

INSERT INTO schema_migrations (version) VALUES ('005_assistant_role')
ON CONFLICT (version) DO NOTHING;

COMMIT;
