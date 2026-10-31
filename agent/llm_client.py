import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

AI_CORE_BASE_URL = os.environ.get("AI_CORE_BASE_URL", "")
AI_CORE_TOKEN_URL = os.environ.get("AI_CORE_TOKEN_URL", "")
AI_CORE_CLIENT_ID = os.environ.get("AI_CORE_CLIENT_ID", "")
AI_CORE_CLIENT_SECRET = os.environ.get("AI_CORE_CLIENT_SECRET", "")
AI_CORE_DEPLOYMENT_ID = os.environ.get("AI_CORE_DEPLOYMENT_ID", "")
AI_CORE_RESOURCE_GROUP = os.environ.get("AI_CORE_RESOURCE_GROUP", "default")

_token_cache = {"token": None, "expires_at": 0}


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(AI_CORE_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": AI_CORE_CLIENT_ID,
        "client_secret": AI_CORE_CLIENT_SECRET,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def analyze(system_prompt: str, user_prompt: str) -> str:
    """Call AI Core LLM. Returns response text."""
    if not AI_CORE_BASE_URL:
        logger.warning("AI Core not configured — returning mock response")
        return "[AI Core not configured — placeholder analysis]"

    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "AI-Resource-Group": AI_CORE_RESOURCE_GROUP,
    }

    # Claude models use /invoke with Anthropic message format
    # GPT models use /chat/completions with OpenAI format
    deployment_id = AI_CORE_DEPLOYMENT_ID
    url_invoke = f"{AI_CORE_BASE_URL}/v2/inference/deployments/{deployment_id}/invoke"
    url_chat = f"{AI_CORE_BASE_URL}/v2/inference/deployments/{deployment_id}/chat/completions"

    # Try Claude (Anthropic) format first
    payload_claude = {
        "anthropic_version": "bedrock-2023-05-31",
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": 4000,
    }
    resp = requests.post(url_invoke, json=payload_claude, headers=headers, timeout=120)

    if resp.status_code == 200:
        data = resp.json()
        # Anthropic format: content[0].text
        if "content" in data and data["content"]:
            return data["content"][0]["text"]

    # Fallback: OpenAI chat/completions format
    payload_oai = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4000,
        "temperature": 0.2,
    }
    resp2 = requests.post(url_chat, json=payload_oai, headers=headers, timeout=120)
    resp2.raise_for_status()
    return resp2.json()["choices"][0]["message"]["content"]
