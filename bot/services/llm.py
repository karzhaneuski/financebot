"""LLM provider abstraction for the JSON-extraction calls (receipt/bank
screenshot vision, search-query parsing).

LLM_PROVIDER selects the backend: "gemini" (default, Google Gemini via the
google-genai SDK with structured output) or "anthropic" (Claude). Both return
the already-parsed JSON value, so callers are provider-agnostic.

Errors:
- LLMUnavailableError — the provider can't serve requests right now (quota /
  rate limit / exhausted balance / auth or billing problem / outage). Shown to
  the user as "recognition temporarily unavailable".
- LLMError — any other provider failure.
- InvalidLLMResponse (a ValueError) — the model answered, but not with valid
  JSON. Callers treat it like an unreadable image.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Protocol

from bot.config import settings

logger = logging.getLogger(__name__)

Tier = Literal["vision", "fast"]

# HTTP statuses that mean "the service is unavailable to us right now"
# rather than "this particular request is bad".
_UNAVAILABLE_STATUSES = {401, 402, 403, 429, 500, 502, 503, 504, 529}
_BALANCE_MARKERS = ("credit balance", "billing", "quota", "insufficient", "rate limit")
# google.rpc.ErrorInfo reasons that mean a configuration/account problem, not
# a bad request. Google answers an invalid key with HTTP 400 INVALID_ARGUMENT,
# so the status code alone would classify it as an ordinary request error.
_GEMINI_UNAVAILABLE_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}


class LLMError(Exception):
    """Provider call failed for a reason other than availability."""


class LLMUnavailableError(LLMError):
    """Quota, rate limit, balance, auth/billing or outage — try again later."""


class InvalidLLMResponse(ValueError):
    """The model's output was not valid JSON."""


class LLMProvider(Protocol):
    name: str

    async def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict,
        tier: Tier,
        image: bytes | list[bytes] | None = None,
        max_tokens: int = 1024,
        schema_hint: str | None = None,
    ) -> Any:
        """Return the parsed JSON answer. `schema` is a JSON Schema the answer
        must follow (enforced natively by Gemini, described in the prompt for
        Claude). `schema_hint` is extra instruction for schema-enforcing
        providers only (e.g. how to express "not applicable" without a null
        root). `image` may be several JPEG pages of one document (multi-page
        PDF receipts), sent in order."""
        ...


def _images(image: bytes | list[bytes] | None) -> list[bytes]:
    if image is None:
        return []
    return list(image) if isinstance(image, (list, tuple)) else [image]


def _extract_json(raw: str) -> str:
    """Strip markdown code fences if the model wrapped the JSON despite instructions."""
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


def _loads(raw: str, provider: str) -> Any:
    try:
        return json.loads(_extract_json(raw))
    except json.JSONDecodeError as e:
        # The raw output is the image's content (items, amounts) — DEBUG only.
        logger.error("%s returned invalid JSON (%d chars)", provider, len(raw))
        logger.debug("%s invalid JSON output: %r", provider, raw)
        raise InvalidLLMResponse(f"{provider} returned invalid JSON") from e


def _is_unavailable(status: int | None, message: str) -> bool:
    if status in _UNAVAILABLE_STATUSES:
        return True
    low = message.lower()
    return any(marker in low for marker in _BALANCE_MARKERS)


# ── Anthropic ────────────────────────────────────────────────────────────────

class AnthropicProvider:
    name = "anthropic"
    _MODELS = {"vision": "claude-sonnet-5", "fast": "claude-haiku-4-5-20251001"}

    def __init__(self, api_key: str | None):
        if not api_key:
            raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def generate_json(self, *, system, prompt, schema, tier, image=None, max_tokens=1024,
                            schema_hint=None):
        import base64

        pages = _images(image)
        if pages:
            content: Any = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(page).decode("utf-8"),
                    },
                }
                for page in pages
            ] + [{"type": "text", "text": prompt}]
        else:
            content = prompt
        try:
            response = await self._client.messages.create(
                model=self._MODELS[tier],
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
        except self._anthropic.APIStatusError as e:
            raise _classify(self.name, e.status_code, str(e)) from e
        except self._anthropic.APIConnectionError as e:
            raise LLMUnavailableError(f"{self.name}: connection error") from e
        except self._anthropic.APIError as e:
            raise LLMError(f"{self.name}: {type(e).__name__}") from e

        raw = response.content[0].text.strip()
        logger.debug("%s raw response: %r", self.name, raw)
        return _loads(raw, self.name)


# ── Gemini ───────────────────────────────────────────────────────────────────

class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None, model: str):
        if not api_key:
            raise LLMUnavailableError("GEMINI_API_KEY is not set")
        from google import genai
        from google.genai import errors, types

        self._errors = errors
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def generate_json(self, *, system, prompt, schema, tier, image=None, max_tokens=1024,
                            schema_hint=None):
        types = self._types
        contents: list[Any] = [
            types.Part.from_bytes(data=page, mime_type="image/jpeg") for page in _images(image)
        ]
        contents.append(prompt)
        instruction = system if not schema_hint else f"{system}\n\n{schema_hint}"
        # max_tokens is deliberately not forwarded: on thinking models the
        # output budget also covers thinking, and a tight cap truncates JSON.
        config = types.GenerateContentConfig(
            system_instruction=instruction,
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config,
            )
        except self._errors.APIError as e:
            reasons = _gemini_error_reasons(e.details)
            if reasons & _GEMINI_UNAVAILABLE_REASONS:
                raise LLMUnavailableError(
                    f"{self.name}: HTTP {e.code}: {', '.join(sorted(reasons))} (check GEMINI_API_KEY)"
                ) from e
            raise _classify(self.name, e.code, f"{e.status} {e.message}") from e
        except Exception as e:  # network errors surface as httpx exceptions
            import httpx

            if isinstance(e, httpx.HTTPError):
                raise LLMUnavailableError(f"{self.name}: connection error") from e
            raise

        raw = (response.text or "").strip()
        logger.debug("%s raw response: %r", self.name, raw)
        if not raw:
            # Empty candidate: blocked by safety filters or cut off.
            raise InvalidLLMResponse(f"{self.name} returned an empty response")
        return _loads(raw, self.name)


def _gemini_error_reasons(details) -> set[str]:
    """ErrorInfo.reason values from a Gemini error body ({"error": {"details": [...]}})."""
    if not isinstance(details, dict):
        return set()
    error = details.get("error", details)
    items = error.get("details") if isinstance(error, dict) else None
    return {
        d["reason"] for d in (items or [])
        if isinstance(d, dict) and isinstance(d.get("reason"), str)
    }


def _classify(provider: str, status: int | None, message: str) -> LLMError:
    # Provider error messages carry no secrets (keys are sent in headers),
    # but can be long; keep the log line short.
    short = f"{provider}: HTTP {status}: {message[:200]}"
    if _is_unavailable(status, message):
        return LLMUnavailableError(short)
    return LLMError(short)


_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """The configured provider (cached). Raises LLMUnavailableError if its
    API key is missing, so a misconfiguration reads as "unavailable" to users
    and is logged by the caller."""
    global _provider
    if _provider is None:
        choice = (settings.LLM_PROVIDER or "gemini").lower()
        if choice == "gemini":
            _provider = GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
        elif choice == "anthropic":
            _provider = AnthropicProvider(settings.ANTHROPIC_API_KEY)
        else:
            raise LLMError(f"Unknown LLM_PROVIDER {settings.LLM_PROVIDER!r}")
    return _provider


def reset_provider() -> None:
    """Drop the cached provider (tests, config changes)."""
    global _provider
    _provider = None
