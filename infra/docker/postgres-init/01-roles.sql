-- Local development roles and databases (specs/29 §2).
-- va_app runs tenant-scoped queries under row-level security; va_system runs the system-scope repositories.
-- Runs once, when the pgdata volume is first initialised.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'va_app') THEN
        CREATE ROLE va_app LOGIN PASSWORD 'va';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'va_system') THEN
        CREATE ROLE va_system LOGIN PASSWORD 'va';
    END IF;
END
$$;

SELECT 'CREATE DATABASE temporal OWNER va'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal')\gexec
