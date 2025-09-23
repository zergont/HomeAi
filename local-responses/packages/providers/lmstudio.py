# packages/providers/lmstudio.py
from __future__ import annotations

import asyncio
import json
import math
from typing import Any, AsyncIterator, Dict, Optional, List

import httpx

from packages.core.settings import get_settings

# Compatibility shim for tests/imports expecting package-like structure
# Expose submodules as attributes on this module
try:
    import packages.providers.lmstudio_model_info as model_info  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    model_info = None  # type: ignore
try:
    import packages.providers.lmstudio_cache as cache  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    cache = None  # type: ignore


def approx_tokens(text: str) -> int:
    return int(math.ceil(len(text) / 4))


class LMStudioProvider:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _post_json(self, url: str, payload: Dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(url, json=payload)

    async def generate(
        self,
        *,
        system: str | None,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> tuple[str, Dict[str, Any] | None]:
        url_chat = f"{self.base_url}/v1/chat/completions"
        url_comp = f"{self.base_url}/v1/completions"
        msg_list: List[Dict[str, str]]
        if messages is not None and len(messages) > 0:
            msg_list = messages
        else:
            msg_list = [
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": user},
            ]
        payload_chat: Dict[str, Any] = {
            "model": model,
            "messages": msg_list,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Try chat endpoint first
        try:
            resp = await self._post_json(url_chat, payload_chat)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            # Fallback to non-chat completions if chat endpoint is not available (404)
            if e.response is not None and e.response.status_code == 404:
                # Build a prompt from messages
                if messages is not None and len(messages) > 0:
                    prompt = "\n".join(m.get("content", "") for m in messages)
                else:
                    prompt = f"{system or ''}\n{user}"
                payload_comp: Dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                resp2 = await self._post_json(url_comp, payload_comp)
                resp2.raise_for_status()
                data = resp2.json()
            else:
                raise

        # Extract text from either chat or completion response
        text: str = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if not text:
            text = data.get("choices", [{}])[0].get("text", "") or ""

        raw_usage: Optional[Dict[str, Any]] = data.get("usage")
        usage: Optional[Dict[str, Any]]
        if raw_usage is None:
            # approximate from provided inputs
            if messages is not None:
                prompt_concat = "".join(m.get("content", "") for m in messages)
            else:
                prompt_concat = (system or "") + user
            input_tokens = approx_tokens(prompt_concat)
            output_tokens = approx_tokens(text)
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        else:
            if "input_tokens" in raw_usage or "output_tokens" in raw_usage:
                inp = int(raw_usage.get("input_tokens", 0) or 0)
                out = int(raw_usage.get("output_tokens", 0) or 0)
                tot = int(raw_usage.get("total_tokens", inp + out) or (inp + out))
            else:
                inp = int(raw_usage.get("prompt_tokens", 0) or 0)
                out = int(raw_usage.get("completion_tokens", raw_usage.get("generated_tokens", 0)) or 0)
                tot = int(raw_usage.get("total_tokens", inp + out) or (inp + out))
            usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": tot}
        return text, usage

    async def agenerate_stream(
        self,
        *,
        system: str | None,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[str]:
        """Yield assistant text fragments from LM Studio SSE stream.
        Tries /v1/chat/completions, falls back to /v1/completions on 404.
        """
        url_chat = f"{self.base_url}/v1/chat/completions"
        url_comp = f"{self.base_url}/v1/completions"
        msg_list: List[Dict[str, str]]
        if messages is not None and len(messages) > 0:
            msg_list = messages
        else:
            msg_list = [
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": user},
            ]
        payload_chat: Dict[str, Any] = {
            "model": model,
            "messages": msg_list,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async def _stream_lines(url: str, payload: Dict[str, Any]):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[len("data: "):]
                        elif line.startswith("data:"):
                            data_str = line[len("data:"):].lstrip()
                        else:
                            continue
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        # Try chat delta first
                        delta = (choices[0] or {}).get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
                            continue
                        # Fallbacks for non-chat completions formats
                        text = (choices[0] or {}).get("text")
                        if text:
                            yield text
                            continue
                        # Some servers may use different key for streaming token
                        token = (choices[0] or {}).get("token") or (choices[0] or {}).get("text_delta")
                        if token:
                            yield token

        try:
            # Try chat stream
            async for chunk in _stream_lines(url_chat, payload_chat):
                yield chunk
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                # Fallback to non-chat completions stream
                if messages is not None and len(messages) > 0:
                    prompt = "\n".join(m.get("content", "") for m in messages)
                else:
                    prompt = f"{system or ''}\n{user}"
                payload_comp: Dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                }
                async for chunk in _stream_lines(url_comp, payload_comp):
                    yield chunk
            else:
                raise
        except asyncio.CancelledError:
            # Stream was cancelled by caller (client disconnect/cancel)
            return
        except Exception:
            # Let API handle and send error downstream
            raise


def get_lmstudio_provider() -> LMStudioProvider:
    settings = get_settings()
    if not settings.lmstudio_base_url:
        raise RuntimeError("LMSTUDIO_BASE_URL is not configured")
    return LMStudioProvider(base_url=str(settings.lmstudio_base_url))
