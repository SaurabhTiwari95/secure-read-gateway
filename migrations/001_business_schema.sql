-- Sample business data for "Acme Traders", a made-up wholesaler.
CREATE SCHEMA IF NOT EXISTS acme;

CREATE TABLE acme.customers (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    email       text NOT NULL,
    city        text NOT NULL,
    created_on  date NOT NULL
);
CREATE INDEX customers_city_idx ON acme.customers (city);

CREATE TABLE acme.products (
    id          serial PRIMARY KEY,
    sku         text NOT NULL UNIQUE,
    name        text NOT NULL,
    unit_price  numeric(12, 2) NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE acme.invoices (
    id           serial PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES acme.customers (id),
    issued_on    date NOT NULL,
    due_on       date NOT NULL,
    status       text NOT NULL CHECK (status IN ('open', 'paid', 'void')),
    total        numeric(14, 2) NOT NULL CHECK (total >= 0)
);
CREATE INDEX invoices_customer_idx ON acme.invoices (customer_id);
CREATE INDEX invoices_status_due_idx ON acme.invoices (status, due_on);
CREATE INDEX invoices_issued_idx ON acme.invoices (issued_on);

CREATE TABLE acme.invoice_lines (
    id          serial PRIMARY KEY,
    invoice_id  integer NOT NULL REFERENCES acme.invoices (id),
    product_id  integer NOT NULL REFERENCES acme.products (id),
    quantity    integer NOT NULL CHECK (quantity > 0),
    unit_price  numeric(12, 2) NOT NULL CHECK (unit_price >= 0)
);
CREATE INDEX invoice_lines_invoice_idx ON acme.invoice_lines (invoice_id);
CREATE INDEX invoice_lines_product_idx ON acme.invoice_lines (product_id);

CREATE TABLE acme.payments (
    id          serial PRIMARY KEY,
    invoice_id  integer NOT NULL REFERENCES acme.invoices (id),
    paid_on     date NOT NULL,
    amount      numeric(14, 2) NOT NULL CHECK (amount > 0)
);
CREATE INDEX payments_invoice_idx ON acme.payments (invoice_id);
