import re
import time
import requests
from logger import get_logger

logger = get_logger(__name__)

CEX_SEARCH_URL = "https://search.webuy.io/1/indexes/prod_cex_pt/query"
CEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Referer": "https://pt.webuy.com/",
    "Origin": "https://pt.webuy.com",
}


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
        dict: {"model": str or None, "storage": str or None, "processor": str or None}
    """
    result = {"model": None, "storage": None, "processor": None}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        response = requests.get(item_url, headers=headers, timeout=15)
        if response.status_code != 200:
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

        return result
    except Exception as e:
        logger.error(f"Error getting Vinted item details: {e}", exc_info=True)
        return result
