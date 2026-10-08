-- Toon het concept voor de volgende levering (alleen lezen).
-- Gebruik: psql -h db -U kruidenier -d kruidenier -f /scripts/plan.sql
-- (bijv. in de terminal van de backup-container; die heeft PGPASSWORD al)
\pset border 2
\pset footer off
\pset null '-'

\echo
\echo '=== Concept voor de volgende levering ==='
SELECT p.delivery_date AS levering,
       to_char(p.cutoff AT TIME ZONE 'Europe/Amsterdam', 'DD-MM HH24:MI') AS "wijzigen tot",
       p.status,
       count(pl.id) AS regels
FROM plan p
LEFT JOIN plan_line pl ON pl.plan_id = p.id
WHERE p.id = (SELECT id FROM plan ORDER BY delivery_date DESC LIMIT 1)
GROUP BY p.id;

SELECT pr.title AS product,
       pl.qty AS aantal,
       pr.unit_size_text AS verpakking,
       to_char(po.price, 'FM990.00') AS prijs,
       pl.reason_text AS reden,
       fs.confidence AS zekerheid,
       fs.n_purchases AS keer
FROM plan_line pl
JOIN product pr ON pr.ah_id = pl.ah_product_id
LEFT JOIN family_stats fs ON fs.family_id = pl.family_id
LEFT JOIN LATERAL (
    SELECT price FROM price_observation o
    WHERE o.ah_product_id = pl.ah_product_id
    ORDER BY o.observed_on DESC LIMIT 1
) po ON true
WHERE pl.plan_id = (SELECT id FROM plan ORDER BY delivery_date DESC LIMIT 1)
ORDER BY pr.title;

\echo
\echo '=== Vaak gekocht maar niet in het concept (ter vergelijking) ==='
SELECT f.name AS familie,
       fs.n_purchases AS keer,
       to_char(fs.last_purchase_at, 'DD-MM') AS "laatst",
       to_char(fs.due_date, 'DD-MM') AS "op rond",
       fs.confidence AS zekerheid,
       CASE WHEN f.excluded THEN 'uitgesloten' WHEN f.pinned THEN 'vastgepind' ELSE '' END AS status
FROM product_family f
JOIN family_stats fs ON fs.family_id = f.id
WHERE fs.n_purchases >= 2
  AND f.id NOT IN (
      SELECT family_id FROM plan_line
      WHERE plan_id = (SELECT id FROM plan ORDER BY delivery_date DESC LIMIT 1)
  )
ORDER BY fs.n_purchases DESC, f.name
LIMIT 25;
