BEGIN TRANSACTION;

-- CeX catalog table: one row per CeX box (product), deduplicated by box_id.
-- cash_price is the cash payment CeX gives when selling an item to them
-- (cashPriceCalculated from the search API).
CREATE TABLE IF NOT EXISTS cex_catalog
(
    box_id        TEXT PRIMARY KEY,
    title         TEXT,
    cash_price    NUMERIC,
    sell_price    NUMERIC,
    category_id   INTEGER,
    category_name TEXT,
    last_seen     NUMERIC
);

UPDATE parameters
SET value = '1.0.5.5'
WHERE key = 'version';

COMMIT;
