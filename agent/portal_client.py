import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = os.environ.get("PORTAL_TOKEN_ENDPOINT", "https://ycloud.accounts.ondemand.com/oauth2/token")
CLIENT_ID = os.environ.get("PORTAL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PORTAL_CLIENT_SECRET", "")
RESOURCE = os.environ.get("PORTAL_RESOURCE", "urn:sap:identity:application:provider:name:cp-dependency")
PORTAL_API_BASE = "https://portalapi.commerce.ondemand.com/v2"

_token_cache = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = requests.post(TOKEN_ENDPOINT, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "resource": RESOURCE,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    logger.info("Portal API token refreshed")
    return _token_cache["token"]


def _headers() -> dict:
    return {"x-approuter-authorization": f"Bearer {get_access_token()}"}


def get_build_progress(subscription_code: str, build_code: str) -> dict:
    url = f"{PORTAL_API_BASE}/subscriptions/{subscription_code}/builds/{build_code}/progress"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def download_build_logs(subscription_code: str, build_code: str, dest_path: str) -> str:
    """Download build log zip to dest_path. Returns dest_path."""
    url = f"{PORTAL_API_BASE}/subscriptions/{subscription_code}/builds/{build_code}/logs"
    resp = requests.get(url, headers={**_headers(), "Accept": "application/octet-stream"},
                        timeout=120, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    logger.info(f"Downloaded build logs to {dest_path}")
    return dest_path


def list_builds(subscription_code: str, status: str = None) -> list:
    """List builds for a subscription. Optional status filter e.g. 'FAILED'."""
    url = f"{PORTAL_API_BASE}/subscriptions/{subscription_code}/builds"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    builds = resp.json()
    if status:
        builds = [b for b in builds if b.get("status") == status]
    return builds
