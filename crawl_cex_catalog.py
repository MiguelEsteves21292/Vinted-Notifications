"""
Standalone crawler that populates the cex_catalog table with every CeX product
(title + cash payout) for a given store.

Usage:
    python crawl_cex_catalog.py            # Spain (prod_cex_es)
    python crawl_cex_catalog.py prod_cex_pt
    python crawl_cex_catalog.py prod_cex_es 0.8   # custom delay between calls

The DB must already be initialised/migrated (run vinted_notifications.py once,
or apply migrations) so the cex_catalog table exists.
"""
import sys
import time

import cex
import db
from logger import get_logger

logger = get_logger(__name__)


def main():
    index = sys.argv[1] if len(sys.argv) > 1 else "prod_cex_es"
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    now = int(time.time())
    logger.info(f"Starting CeX catalog crawl for index '{index}' (delay={delay}s)")
    total = cex.crawl_cex_catalog(index=index, now=now, delay=delay)
    stored = db.get_cex_catalog_count()
    logger.info(f"Done. Collected {total} distinct products; cex_catalog now holds {stored} rows.")
    print(f"Collected {total} distinct products; cex_catalog now holds {stored} rows.")


if __name__ == "__main__":
    main()
