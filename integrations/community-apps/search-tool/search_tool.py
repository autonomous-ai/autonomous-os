"""DuckDuckGo-backed search helper for the Autonomous OS Search Tool Space."""

import logging
from typing import Any

from ddgs import DDGS
from ddgs.exceptions import DDGSException


logger = logging.getLogger(__name__)

MAX_RESULTS = 10
DEFAULT_RESULTS = 5


def search_web(query: str, max_results: int = DEFAULT_RESULTS) -> dict[str, Any]:
    """Search the web with DuckDuckGo and return structured results."""
    query = (query or "").strip()
    if not query:
        return {"error": "query must be a non-empty string"}

    try:
        result_count = int(max_results)
    except (TypeError, ValueError):
        result_count = DEFAULT_RESULTS
    result_count = max(1, min(result_count, MAX_RESULTS))

    logger.info("search_web query=%s max_results=%d", query, result_count)
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=result_count))
    except DDGSException as exc:
        logger.warning("search_web failed for %r: %s", query, exc)
        return {"error": f"web search failed for '{query}'"}
    return {
        "query": query,
        "results": [
            {
                "title": hit.get("title", ""),
                "snippet": hit.get("body", ""),
                "url": hit.get("href", ""),
            }
            for hit in hits
        ],
    }
