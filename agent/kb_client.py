import os
import logging
import requests

logger = logging.getLogger(__name__)

KB_SEARCH_URL = os.environ.get("KB_SEARCH_URL", "https://kb-search.c3378d8.kyma.ondemand.com")
KB_SEARCH_TOKEN = os.environ.get("KB_SEARCH_TOKEN", "")  # JWT token for /sse auth


def search_kbas(product: str, query: str, top_k: int = 5, source: str = None) -> list:
    """Search KB via kb-search-kyma HTTP API."""
    payload = {"product": product, "query": query, "top_k": top_k}
    if source:
        payload["source"] = source
    headers = {"Content-Type": "application/json"}
    if KB_SEARCH_TOKEN:
        headers["Authorization"] = f"Bearer {KB_SEARCH_TOKEN}"
    resp = requests.post(f"{KB_SEARCH_URL}/search", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_kba(product: str, doc_id: str) -> dict:
    """Fetch full KBA content by ID."""
    payload = {"product": product, "id": doc_id}
    headers = {"Content-Type": "application/json"}
    if KB_SEARCH_TOKEN:
        headers["Authorization"] = f"Bearer {KB_SEARCH_TOKEN}"
    resp = requests.post(f"{KB_SEARCH_URL}/get", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()
