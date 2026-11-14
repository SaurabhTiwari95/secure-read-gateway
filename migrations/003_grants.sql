-- Least privilege for the role the API connects as. The role itself is created by
-- app/migrate.py (its password comes from APP_DB_PASSWORD, never from a file).
REVOKE ALL ON SCHEMA acme, gateway FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA acme, gateway TO gateway_app;
GRANT SELECT ON ALL TABLES IN SCHEMA acme TO gateway_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA acme GRANT SELECT ON TABLES TO gateway_app;

GRANT SELECT ON gateway.functions TO gateway_app;
GRANT INSERT ON gateway.audit_log TO gateway_app;
GRANT USAGE ON SEQUENCE gateway.audit_log_id_seq TO gateway_app;

ALTER ROLE gateway_app SET statement_timeout = '5s';
ALTER ROLE gateway_app SET idle_in_transaction_session_timeout = '10s';
