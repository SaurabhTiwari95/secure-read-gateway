-- The gateway's own tables: the function allow-list and an append-only audit log.
CREATE SCHEMA IF NOT EXISTS gateway;

CREATE TABLE gateway.functions (
    name         text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{2,62}$'),
    description  text NOT NULL,
    sql          text NOT NULL,
    params       jsonb NOT NULL DEFAULT '[]'::jsonb,
    max_rows     integer NOT NULL DEFAULT 100 CHECK (max_rows BETWEEN 1 AND 1000),
    enabled      boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gateway.audit_log (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    request_id     text NOT NULL,
    key_id         text NOT NULL,
    function_name  text NOT NULL,
    status         text NOT NULL,
    row_count      integer,
    duration_ms    integer NOT NULL,
    client_ip      inet
);
CREATE INDEX audit_log_at_idx ON gateway.audit_log (at);
CREATE INDEX audit_log_key_at_idx ON gateway.audit_log (key_id, at);

-- Append-only: no UPDATE, DELETE or TRUNCATE, whoever asks.
CREATE FUNCTION gateway.refuse_audit_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'gateway.audit_log is append-only';
END;
$$;

CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON gateway.audit_log
    FOR EACH ROW EXECUTE FUNCTION gateway.refuse_audit_change();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON gateway.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION gateway.refuse_audit_change();
