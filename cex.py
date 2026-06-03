import re
import time
import json
import unicodedata
import requests
from rapidfuzz import process, fuzz
from logger import get_logger

logger = get_logger(__name__)

# In-memory cache for local catalog matching (loaded lazily on first match).
_MATCH_NORM_TITLES = None  # list[str] normalized titles (parallel to _MATCH_ROWS)
_MATCH_ROWS = None  # list[tuple] of (original_title, cash_price, sell_price)
_MATCH_TOKEN_SETS = None  # list[set[str]] token sets (parallel to _MATCH_ROWS)

# Model qualifiers that distinguish otherwise-similar phones. An exact-model
# match must not introduce a qualifier the query lacks (so "iphone 12" never
# matches "iphone 12 pro" / "pro max"), nor drop one the query requires.
_VARIANT_TOKENS = {
    "pro", "max", "mini", "plus", "ultra", "lite", "fe", "neo", "se", "air",
}

CEX_SEARCH_URL = "https://search.webuy.io/1/indexes/prod_cex_pt/query"
CEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Referer": "https://pt.webuy.com/",
    "Origin": "https://pt.webuy.com",
}

# Catalog crawl configuration (Spain store by default).
CEX_INDEX_URL = "https://search.webuy.io/1/indexes/{index}/query"
# Algolia caps a single query to 1000 retrievable hits, so any bucket larger
# than this must be split further (here, by sellPrice range).
ALGOLIA_HIT_CAP = 1000


def search_cex(title):
    """
    Search CeX Portugal for a product by title and return the cash buy price.

    Args:
        title (str): The product title to search for

    Returns:
        dict: {"name": str, "cash_price": float} or None if not found
    """
    try:
        payload = {"params": f"query={title}&hitsPerPage=1"}
        response = requests.post(
            CEX_SEARCH_URL, json=payload, headers=CEX_HEADERS, timeout=10
        )

        if response.status_code == 403:
            logger.warning("CeX rate limited (403), waiting 2s and retrying...")
            time.sleep(2)
            response = requests.post(
                CEX_SEARCH_URL, json=payload, headers=CEX_HEADERS, timeout=10
            )
        if response.status_code != 200:
            logger.warning(f"CeX search failed with status {response.status_code}")
            return None

        data = response.json()
        hits = data.get("hits", [])

        if not hits:
            return None

        first_hit = hits[0]
        cash_price = first_hit.get("cashPriceCalculated")
        product_name = first_hit.get("boxName", "Unknown")

        if cash_price is None or cash_price <= 0:
            return None

        return {
            "name": product_name,
            "cash_price": float(cash_price),
        }
    except Exception as e:
        logger.error(f"Error searching CeX: {e}", exc_info=True)
        return None


def get_vinted_item_details(item_url):
    """
    Scrape a Vinted item page to extract model, storage and processor.

    Args:
        item_url (str): The URL of the Vinted item

    Returns:
        dict: {"model": str or None, "storage": str or None, "processor": str or None,
               "description": str or None}
    """
    result = {"model": None, "storage": None, "processor": None, "description": None}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        # Route through the proxy pool (if configured) — the item page is behind
        # the same Cloudflare challenge as the search API, so a plain request is
        # blocked (403) and the description/specs come back empty.
        proxy_dict = {}
        try:
            import proxies

            proxy_dict = proxies.convert_proxy_string_to_dict(proxies.get_random_proxy())
        except Exception:
            proxy_dict = {}
        response = requests.get(
            item_url, headers=headers, timeout=15, proxies=proxy_dict
        )
        if response.status_code != 200:
            logger.warning(
                f"Item page fetch failed ({response.status_code}) for {item_url}; "
                f"description/specs unavailable"
            )
            return result
        text = response.text

        # Extract model
        idx = text.find("model_nav")
        if idx >= 0:
            chunk = text[idx : idx + 300]
            unescaped = chunk.encode().decode("unicode_escape", errors="replace")
            match = re.search(r'"value":"([^"]+)"', unescaped)
            if match:
                result["model"] = match.group(1)

        # Extract storage (phones: internal_memory_capacity, computers: storage_capacity)
        for field in ("internal_memory_capacity", "storage_capacity"):
            idx = text.find(field)
            if idx >= 0:
                chunk = text[idx : idx + 300]
                unescaped = chunk.encode().decode("unicode_escape", errors="replace")
                match = re.search(r'"value":"([^"]+)"', unescaped)
                if match:
                    result["storage"] = match.group(1)
                    break

        # Extract processor
        idx = text.find("computer_cpu_line")
        if idx >= 0:
            chunk = text[idx : idx + 300]
            unescaped = chunk.encode().decode("unicode_escape", errors="replace")
            match = re.search(r'"value":"([^"]+)"', unescaped)
            if match:
                result["processor"] = match.group(1)

        # Extract description from the page meta tags (item description text).
        # Attribute order varies, so try a few patterns.
        for pat in (
            r'property=["\']og:description["\'][^>]*content=["\'](.*?)["\']',
            r'content=["\'](.*?)["\'][^>]*property=["\']og:description["\']',
            r'name=["\']description["\'][^>]*content=["\'](.*?)["\']',
        ):
            m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
            if m and m.group(1).strip():
                import html

                result["description"] = html.unescape(m.group(1)).strip()
                break

        return result
    except Exception as e:
        logger.error(f"Error getting Vinted item details: {e}", exc_info=True)
        return result


def _clean_title(s):
    """Strip CeX internal code prefixes like "*DNU*" or "*USE <sku>*" from a
    catalog title (cosmetic, for display)."""
    return re.sub(r"^\s*(?:\*[^*]*\*\s*)+", "", s or "").strip()


# Generic category/condition words (PT/ES/FR/EN). A query made up only of these
# carries no model, so it must not win a match by being a trivial subset
# (e.g. title "smartwatch" matching any "... Smartwatch" entry at score 100).
_GENERIC_TOKENS = {
    "smartwatch", "smartwatches", "watch", "reloj", "relogio", "montre",
    "smartband", "band", "pulseira", "wearable",
    "tablet", "tablette", "tableta",
    "phone", "smartphone", "telemovel", "telefono", "telefone", "movil",
    "portable", "portatil", "laptop", "notebook", "ordinateur", "ordenador",
    "pc", "computador", "computer", "desktop", "torre", "sobremesa",
    "camara", "camera", "appareil", "photo", "foto", "lente", "lens",
    "objetiva", "objectif", "gps", "vr", "headset", "auriculares",
    "novo", "nova", "nuevo", "nueva", "usado", "usada", "como", "bom", "boa",
    "estado", "perfeito", "perfeitas", "condicoes", "condiciones", "neuf",
    "occasion", "bon", "etat", "muito", "pouco", "uso", "vendo", "vendido",
}


def is_generic_title(text):
    """True if the text has no model-bearing token (only generic category/
    condition words), so it shouldn't be used as a match query on its own."""
    toks = _normalize_title(text).split()
    return all(t in _GENERIC_TOKENS for t in toks)


def _normalize_title(s):
    """Normalize a title for fuzzy matching: strip accents, unify storage units,
    drop punctuation, collapse whitespace."""
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # "64 gb" / "1 tb" -> "64gb" / "1tb" so capacities match regardless of spacing
    s = re.sub(r"(\d+)\s*(gb|tb|go|mb)\b", r"\1\2", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_match_cache():
    """Load all catalog titles + prices into memory for fuzzy matching."""
    global _MATCH_NORM_TITLES, _MATCH_ROWS, _MATCH_TOKEN_SETS
    import db

    rows = db.get_cex_catalog_for_matching()
    norm, kept, token_sets = [], [], []
    for title, cash, sell in rows:
        nt = _normalize_title(title)
        if not nt:
            continue
        norm.append(nt)
        kept.append((title, cash, sell))
        token_sets.append(set(nt.split()))
    _MATCH_NORM_TITLES = norm
    _MATCH_ROWS = kept
    _MATCH_TOKEN_SETS = token_sets
    logger.info(f"Loaded {len(kept)} CeX catalog titles into match cache")


def refresh_match_cache():
    """Force a reload of the match cache (e.g. after re-crawling the catalog)."""
    _load_match_cache()


def match_cex_catalog(title, min_score=65):
    """
    Find the closest catalog title to a Vinted item title (local fuzzy match).

    Args:
        title (str): the Vinted item title (or model) to match.
        min_score (int): minimum similarity (0-100) to accept a match.

    Returns:
        dict: {"name", "cash_price", "sell_price", "score"} of the closest
        catalog product, or None if nothing is similar enough.
    """
    if _MATCH_ROWS is None:
        _load_match_cache()
    if not _MATCH_NORM_TITLES:
        return None
    query = _normalize_title(title)
    if not query:
        return None
    # token_set_ratio is robust to word order and extra/missing tokens, but it
    # scores 100 for any title that merely contains the query (e.g. "i5 8600k"
    # matches both the standalone CPU and a full PC build). Among equal top
    # scores, prefer the most exact match (fewest extra tokens).
    results = process.extract(
        query, _MATCH_NORM_TITLES, scorer=fuzz.token_set_ratio,
        processor=None, limit=25,
    )
    if not results:
        return None
    top = results[0][1]
    if top < min_score:
        return None
    tied = [r for r in results if r[1] == top]
    _match_str, score, idx = min(tied, key=lambda r: len(_MATCH_TOKEN_SETS[r[2]]))
    orig_title, cash, sell = _MATCH_ROWS[idx]
    return {
        "name": _clean_title(orig_title),
        "cash_price": float(cash) if cash is not None else 0.0,
        "sell_price": float(sell) if sell is not None else 0.0,
        "score": score,
    }


def match_cex_phone(query, min_score=60):
    """
    Match a phone by brand + model + capacity with EXACT model semantics.

    A candidate is only valid if it contains every query token (brand, model
    number, capacity) and introduces no extra model qualifier (pro/max/mini/...)
    that the query doesn't have. So "apple iphone 12 64gb" matches "iPhone 12"
    but never "iPhone 12 Pro" or "iPhone 12 Pro Max".

    Args:
        query (str): e.g. "Apple iPhone 12 64gb" (brand + model + capacity).
        min_score (int): minimum token_set_ratio among the valid candidates.

    Returns:
        dict: {"name", "cash_price", "sell_price", "score"} or None.
    """
    if _MATCH_ROWS is None:
        _load_match_cache()
    if not _MATCH_NORM_TITLES:
        return None
    qnorm = _normalize_title(query)
    if not qnorm:
        return None
    qtokens = set(qnorm.split())
    q_variants = qtokens & _VARIANT_TOKENS

    # Maximize score, then prefer the most exact match (fewest extra tokens) so
    # internal-code variants ("*USE ...*") lose to the clean title.
    best_key, best_idx = None, None
    for i, ctokens in enumerate(_MATCH_TOKEN_SETS):
        # Every query token (brand, model, capacity) must be present...
        if not qtokens <= ctokens:
            continue
        # ...and the candidate must not add a model qualifier the query lacks.
        if (ctokens & _VARIANT_TOKENS) - q_variants:
            continue
        score = fuzz.token_set_ratio(qnorm, _MATCH_NORM_TITLES[i])
        key = (score, -len(ctokens))
        if best_key is None or key > best_key:
            best_key, best_idx = key, i

    if best_idx is None or best_key[0] < min_score:
        return None
    best_score = best_key[0]
    orig_title, cash, sell = _MATCH_ROWS[best_idx]
    return {
        "name": _clean_title(orig_title),
        "cash_price": float(cash) if cash is not None else 0.0,
        "sell_price": float(sell) if sell is not None else 0.0,
        "score": best_score,
    }


def extract_pc_specs(text):
    """
    Extract CPU + RAM + storage from a computer listing (title + description),
    to match CeX's "Brand Model/CPU/RAM/Storage/..." computer titles instead of
    matching the noisy free-text description (which hits Windows/Office entries).

    Returns a string like "i5-3350p 6gb 300gb" (possibly partial), or "".
    """
    t = (text or "").lower()
    parts = []
    # CPU: Intel "iX-####<suffix>" (suffix like G7/H/HX/U/K), else Ryzen, else bare "iX".
    m = re.search(r"\b(i[3579])[\s-]*(\d{3,5}[a-z0-9]{0,3})\b", t)
    if m:
        parts.append(f"{m.group(1)}-{m.group(2)}")
    else:
        m = re.search(r"\bryzen\s*([3579])\s*(\d{3,4}[a-z]{0,2})?", t)
        if m:
            parts.append(("ryzen " + m.group(1) + (m.group(2) or "")).strip())
        else:
            m = re.search(r"\b(i[3579])\b", t)
            if m:
                parts.append(m.group(1))
    # RAM: prefer "<n>GB RAM" (number before RAM), then "RAM: <n>GB".
    ram = None
    m = re.search(r"(\d{1,3})\s*(?:gb|go|giga)\s*(?:de\s*ram|ram)", t) or re.search(
        r"ram[^0-9]{0,6}(\d{1,3})\s*(?:gb|go|giga)", t
    )
    if m:
        ram = m.group(1)
        parts.append(ram + "gb")
    # Storage: the largest disk (excluding the RAM value).
    disks = [int(x) for x in re.findall(r"(\d{2,4})\s*(?:gb|go|giga|tb)", t)]
    if ram and int(ram) in disks:
        disks.remove(int(ram))
    if disks:
        parts.append(str(max(disks)) + "gb")
    return " ".join(parts)


def _cex_query(index, params, max_retries=10):
    """
    Low-level POST to the CeX (Algolia) search index with backoff on 403/429.

    Args:
        index (str): Algolia index name, e.g. "prod_cex_es".
        params (dict): Algolia query params (will be urlencoded into "params").
        max_retries (int): retries on rate-limit before giving up.

    Returns:
        dict: parsed JSON response, or None on failure.
    """
    from urllib.parse import urlencode

    url = CEX_INDEX_URL.format(index=index)
    store = index.split("_")[-1]  # "es", "pt", ...
    headers = dict(CEX_HEADERS)
    headers["Referer"] = f"https://{store}.webuy.com/"
    headers["Origin"] = f"https://{store}.webuy.com"

    payload = {"params": urlencode(params)}
    for attempt in range(max_retries):
        try:
            # Rotate through the project's proxy pool (if configured) to spread
            # load and avoid the CeX WAF rate-limiting a single IP.
            proxy_dict = {}
            try:
                import proxies

                proxy_dict = proxies.convert_proxy_string_to_dict(
                    proxies.get_random_proxy()
                )
            except Exception:
                proxy_dict = {}
            response = requests.post(
                url, json=payload, headers=headers, timeout=20, proxies=proxy_dict
            )
            if response.status_code in (403, 429):
                wait = min(90, 5 + attempt * 8)
                logger.warning(
                    f"CeX rate limited ({response.status_code}), waiting {wait}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
                continue
            if response.status_code != 200:
                logger.warning(f"CeX query failed with status {response.status_code}")
                return None
            return response.json()
        except Exception as e:
            logger.error(f"Error querying CeX: {e}", exc_info=True)
            time.sleep(2 + attempt * 3)
    logger.error("CeX query exhausted retries")
    return None


def _hit_to_row(hit, now):
    """Map a CeX search hit to a cex_catalog row dict."""
    return {
        "box_id": str(hit.get("boxId") or hit.get("objectID")),
        "title": hit.get("boxName", "Unknown"),
        "cash_price": hit.get("cashPriceCalculated"),
        "sell_price": hit.get("sellPrice"),
        "category_id": hit.get("categoryId"),
        "category_name": hit.get("categoryName"),
        "last_seen": now,
    }


def is_media_category(name):
    """
    True for film/TV/music/anime media on disc and video-game software, which
    are excluded from the catalog. Hardware is kept: DVD/Blu-ray players
    ("DVD Portatiles", "Blu-Ray Hardware"), optical drives ("Unidades Opticas"),
    consoles/controllers/accessories, and gaming laptops ("PC Gaming Portatil").
    """
    n = (name or "").lower()
    if "juego" in n:  # all "... Juegos" / "... Juego" categories are game software
        return True
    if n.startswith("dvd ") or n.startswith("blu-ray "):
        if "portatil" in n or "hardware" in n:  # players, not discs
            return False
        return True
    if "umd peliculas" in n:  # PSP UMD movies
        return True
    return False


def _crawl_category(index, category_name, now, seen, on_rows, delay):
    """
    Fetch all boxes in a category, walking sellPrice ranges iteratively so a
    bucket over the Algolia 1000-hit cap is split until it fits.

    Iterative (explicit work stack) rather than recursive: dense price clusters
    could otherwise exceed Python's recursion limit.

    Args:
        category_name (str): the categoryFriendlyName to crawl.
        seen (set): box_ids already collected (in-place dedup).
        on_rows (callable): called with a list of new row dicts to persist.
    """
    stack = [(0.0, 1_000_000.0)]
    while stack:
        lo, hi = stack.pop()
        params = {
            "query": "",
            "hitsPerPage": ALGOLIA_HIT_CAP,
            "facetFilters": json.dumps([[f"categoryFriendlyName:{category_name}"]]),
            "numericFilters": json.dumps([f"sellPrice>={lo}", f"sellPrice<={hi}"]),
            # facets_stats gives the real min/max price of the matched set, so we
            # can split on actual data instead of probing empty price ranges.
            "facets": json.dumps(["sellPrice"]),
        }
        data = _cex_query(index, params)
        if delay:
            time.sleep(delay)
        if data is None:
            logger.warning(
                f"Skipping '{category_name}' range [{lo}, {hi}] (query failed)"
            )
            continue

        nb = data.get("nbHits", 0)
        if nb > ALGOLIA_HIT_CAP:
            # Narrow the working range to where products actually are, then split.
            stats = data.get("facets_stats", {}).get("sellPrice", {})
            rlo = max(lo, stats.get("min", lo))
            rhi = min(hi, stats.get("max", hi))
            if rhi - rlo > 0.01:
                mid = round((rlo + rhi) / 2, 2)
                if mid <= rlo:  # range too tight to split further as floats
                    mid = rlo + 0.01
                stack.append((rlo, mid))
                stack.append((round(mid + 0.01, 2), rhi))
                continue
            # Cannot split further (too many items at a single price point).
            logger.warning(
                f"'{category_name}' has {nb} items at price ~{rlo}; only the "
                f"first {ALGOLIA_HIT_CAP} are retrievable via the API (truncated)."
            )

        new_rows = []
        for hit in data.get("hits", []):
            box_id = str(hit.get("boxId") or hit.get("objectID"))
            if box_id in seen or box_id == "None":
                continue
            seen.add(box_id)
            new_rows.append(_hit_to_row(hit, now))
        if new_rows:
            on_rows(new_rows)


def crawl_cex_catalog(index="prod_cex_es", now=None, delay=0.5, on_rows=None):
    """
    Crawl the full CeX catalog for a store, deduplicated by box_id.

    Iterates every product category and, within each, walks sellPrice ranges so
    that no single query exceeds the Algolia 1000-hit cap.

    Args:
        index (str): Algolia index, e.g. "prod_cex_es" (Spain) or "prod_cex_pt".
        now (int): timestamp stamped on each row (caller supplies it).
        delay (float): seconds to sleep between API calls (politeness/rate-limit).
        on_rows (callable): called with each batch of new rows; defaults to
            persisting them via db.upsert_cex_products.

    Returns:
        int: total distinct boxes collected.
    """
    import db

    if now is None:
        raise ValueError("crawl_cex_catalog requires an explicit 'now' timestamp")
    if on_rows is None:
        on_rows = db.upsert_cex_products

    # Discover all categories (by friendly name) with their counts.
    meta = _cex_query(
        index,
        {"query": "", "hitsPerPage": 0,
         "facets": json.dumps(["categoryFriendlyName"]),
         "maxValuesPerFacet": 1000},
    )
    if not meta:
        logger.error("Could not fetch CeX category facets; aborting crawl")
        return 0
    categories = meta.get("facets", {}).get("categoryFriendlyName", {})
    total_expected = meta.get("nbHits", 0)

    # Drop film/TV/music/anime and game-software categories (keep hardware).
    excluded = {n for n in categories if is_media_category(n)}
    categories = {n: c for n, c in categories.items() if n not in excluded}

    # Resume support: pre-load already-stored box_ids (avoid rewriting) and the
    # per-category stored counts (skip categories that are effectively complete).
    seen = db.get_cex_box_ids()
    stored_counts = db.get_cex_category_name_counts()
    logger.info(
        f"CeX crawl starting: {len(categories)} categories "
        f"(excluding {len(excluded)} film/game categories), ~{total_expected} "
        f"products total; resuming with {len(seen)} already stored"
    )

    for name, count in sorted(categories.items(), key=lambda kv: -kv[1]):
        have = stored_counts.get(name, 0)
        # Skip categories already (effectively) complete. A small shortfall is
        # expected from single-price buckets that exceed the API's 1000 cap.
        if have >= count * 0.97:
            logger.info(f"'{name}' already complete ({have}/{count}); skipping")
            continue
        _crawl_category(index, name, now, seen, on_rows, delay)
        logger.info(f"'{name}' done (declared {count}); total so far {len(seen)}")

    logger.info(f"CeX crawl finished: {len(seen)} distinct products collected")
    return len(seen)