from __future__ import annotations

from typing import Any, Dict

import httpx

from packages.core.settings import get_settings


def _strip_provider_prefix(model_id: str) -> str:
    return model_id.split(":", 1)[1] if model_id.startswith("lm:") else model_id


async def fetch_model_info(model_id: str) -> Dict[str, Any]:
    settings = get_settings()
    mid = _strip_provider_prefix(model_id)
    base = str(settings.lmstudio_base_url).rstrip("/")
    url = f"{base}/api/v0/models/{mid}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code == 404:
                # Fallback: search in the list (some servers expose only collection endpoint)
                r = await client.get(f"{base}/api/v0/models")
                r.raise_for_status()
                items = r.json() or []
                data = None
                for it in items:
                    if it.get("id") == mid or it.get("model") == mid or it.get("name") == mid:
                        data = it
                        break
                if data is None:
                    raise httpx.HTTPStatusError("model not found", request=r.request, response=r)
            else:
                r.raise_for_status()
                data = r.json() or {}
    except Exception as e:
        return {
            "id": mid,
            "max_context_length": settings.ctx_default_context_length,
            "loaded_context_length": None,
            "source": "default",
            "error": str(e),
        }

    # Normalize keys from various LM Studio versions
    def pick(d: Dict[str, Any], *keys: str) -> Any:
        for k in keys:
            v = d.get(k)
            if isinstance(v, int) and v > 0:
                return v
        return None

    loaded_len = data.get("loaded_context_length") or pick(data, "context_length", "context_window", "ctx_window")
    max_len = data.get("max_context_length") or pick(data, "max_context_window", "max_ctx", "n_ctx", "max_position_embeddings")

    src = "lmstudio.loaded_context_length" if loaded_len else ("lmstudio.max_context_length" if max_len else "default")

    return {
        "id": mid,
        "max_context_length": max_len if max_len else (settings.ctx_default_context_length if not loaded_len else None),
        "loaded_context_length": loaded_len,
        "source": src,
        "state": data.get("state"),
    }
