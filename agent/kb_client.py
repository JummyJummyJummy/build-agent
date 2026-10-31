import os
import logging
import requests

logger = logging.getLogger(__name__)

KB_SEARCH_URL = os.environ.get("KB_SEARCH_URL", "https://kb-search.c3378d8.kyma.ondemand.com")
KB_ADMIN_TOKEN = os.environ.get("KB_ADMIN_TOKEN", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if KB_ADMIN_TOKEN:
        h["X-Admin-Token"] = KB_ADMIN_TOKEN
    return h


def search_kbas(product: str, query: str, top_k: int = 5, source: str = None) -> list:
    payload = {"product": product, "query": query, "top_k": top_k}
    if source:
        payload["source"] = source
    resp = requests.post(f"{KB_SEARCH_URL}/search", json=payload, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_kba(product: str, doc_id: str) -> dict:
    payload = {"product": product, "id": doc_id}
    resp = requests.post(f"{KB_SEARCH_URL}/get", json=payload, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()
