-- The allow-list: what agents may call. Each SQL uses $1..$n placeholders only.
-- Customer emails are deliberately not exposed by any function (data minimisation).
INSERT INTO gateway.functions (name, description, sql, params, max_rows) VALUES
(
    'list_customers',
    'Customers, optionally filtered by city, oldest first.',
    $q$SELECT id, name, city, created_on
       FROM acme.customers
       WHERE ($1::text IS NULL OR city = $1)
       ORDER BY id$q$,
    '[{"name": "city", "type": "str", "required": false, "max_length": 80}]',
    200
),
(
    'customer_balance',
    'Amount invoiced, paid and outstanding for one customer.',
    $q$SELECT c.id, c.name,
              COALESCE((SELECT sum(i.total) FROM acme.invoices i
                        WHERE i.customer_id = c.id AND i.status <> 'void'), 0) AS invoiced,
              COALESCE((SELECT sum(p.amount) FROM acme.payments p
                        JOIN acme.invoices i ON i.id = p.invoice_id
                        WHERE i.customer_id = c.id), 0) AS paid
       FROM acme.customers c
       WHERE c.id = $1$q$,
    '[{"name": "customer_id", "type": "int", "min": 1}]',
    1
),
(
    'list_overdue_invoices',
    'Open invoices past their due date on a given day, oldest first.',
    $q$SELECT i.id, i.customer_id, c.name AS customer_name, i.due_on, i.total,
              ($1::date - i.due_on) AS days_overdue
       FROM acme.invoices i
       JOIN acme.customers c ON c.id = i.customer_id
       WHERE i.status = 'open' AND i.due_on < $1::date AND i.total >= $2
       ORDER BY i.due_on, i.id$q$,
    '[{"name": "as_of", "type": "date"},
      {"name": "min_amount", "type": "decimal", "required": false, "default": "0", "min": 0}]',
    200
),
(
    'sales_by_month',
    'Invoice count and revenue per month for one year (void invoices excluded).',
    $q$SELECT date_trunc('month', issued_on)::date AS month,
              count(*) AS invoices,
              sum(total) AS revenue
       FROM acme.invoices
       WHERE status <> 'void'
         AND issued_on >= make_date($1, 1, 1)
         AND issued_on < make_date($1 + 1, 1, 1)
       GROUP BY 1
       ORDER BY 1$q$,
    '[{"name": "year", "type": "int", "min": 2000, "max": 2100}]',
    12
),
(
    'top_products',
    'Products by revenue since a date.',
    $q$SELECT p.sku, p.name, sum(l.quantity) AS units, sum(l.quantity * l.unit_price) AS revenue
       FROM acme.invoice_lines l
       JOIN acme.products p ON p.id = l.product_id
       JOIN acme.invoices i ON i.id = l.invoice_id
       WHERE i.issued_on >= $1 AND i.status <> 'void'
       GROUP BY p.sku, p.name
       ORDER BY revenue DESC, p.sku$q$,
    '[{"name": "since", "type": "date"}]',
    50
),
(
    -- Deliberately unsafe, to show the guard working: it is refused at load and never runs.
    'purge_old_invoices',
    'DEMO OF A REFUSED FUNCTION: tries to delete data.',
    $q$WITH gone AS (DELETE FROM acme.invoices WHERE issued_on < $1 RETURNING id)
       SELECT count(*) AS deleted FROM gone$q$,
    '[{"name": "before", "type": "date"}]',
    1
);
