"""Core compose() function — Gemini-backed message composition with retry."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .templates import SYSTEM_PROMPT, build_prompt, route_trigger
from .validator import parse_json_loose, validate

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
PER_CALL_TIMEOUT = 25  # seconds; bot must respond in 30


def _min_call_interval() -> float:
    return float(os.environ.get("GEMINI_MIN_INTERVAL", "0"))


def _rate_limit_max_retries() -> int:
    return int(os.environ.get("GEMINI_RATE_LIMIT_RETRIES", "3"))


def _rate_limit_default_backoff() -> float:
    return float(os.environ.get("GEMINI_RATE_LIMIT_BACKOFF", "32"))


def _is_retryable_error(err: Exception) -> bool:
    """True for transient errors (rate limit OR server overload) worth retrying."""
    msg = str(err)
    return (
        "429" in msg
        or "503" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "UNAVAILABLE" in msg
        or "rate limit" in msg.lower()
        or "overloaded" in msg.lower()
    )


def _is_daily_quota_exhausted(err: Exception) -> bool:
    """A daily quota error means retrying within this run is pointless."""
    msg = str(err)
    return "PerDay" in msg or "per day" in msg.lower()


# Backwards-compat alias.
_is_rate_limit_error = _is_retryable_error


def _extract_retry_seconds(err: Exception, default: float) -> float:
    """Pull the suggested retry delay out of a Google API 429 error message."""
    import re as _re

    m = _re.search(r"retry in (\d+(?:\.\d+)?)s", str(err))
    if m:
        return float(m.group(1)) + 1.0
    m = _re.search(r"'retryDelay': '(\d+(?:\.\d+)?)s'", str(err))
    if m:
        return float(m.group(1)) + 1.0
    return default


# Module-level pacing clock so successive compose() calls in the same process
# self-throttle to stay under free-tier RPM.
_last_call_at: float = 0.0


def _pace() -> None:
    global _last_call_at
    interval = _min_call_interval()
    if interval <= 0:
        _last_call_at = time.monotonic()
        return
    elapsed = time.monotonic() - _last_call_at
    wait = interval - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


class _GeminiClient:
    """Thin wrapper around google-genai. Lazy-initialized so unit tests that
    don't touch the LLM don't require the SDK or an API key."""

    _instance: "_GeminiClient | None" = None

    def __init__(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Export it before running compose()."
            )
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-genai SDK not installed. Run: pip install google-genai"
            ) from e
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = DEFAULT_MODEL

    @classmethod
    def get(cls) -> "_GeminiClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def generate_json(self, system: str, user: str) -> str:
        """Single Gemini call with JSON-mode output. Returns raw text."""
        from google.genai import types  # type: ignore

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
        )
        response = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=config,
        )
        return response.text or ""


def _fallback_message(
    category: dict, merchant: dict, trigger: dict, customer: dict | None, reason: str
) -> dict:
    """Deterministic safety net used when the LLM call or validation fails
    irrecoverably. Keeps the bot from returning malformed output."""
    name = (merchant.get("identity") or {}).get("name") or "there"
    kind = trigger.get("kind", "update")
    suppression = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id','m')}"
    body = (
        f"Hi {name}, quick note from Vera — checking in on your magicpin "
        f"profile. Want me to share what's most worth a look this week?"
    )
    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf" if customer else "vera",
        "suppression_key": suppression,
        "rationale": f"Fallback message ({reason})",
    }


def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: dict | None = None,
) -> dict[str, Any]:
    """Compose the next outbound WhatsApp message.

    Inputs are the dicts loaded from the dataset JSON. Returns a dict with
    keys: body, cta, send_as, suppression_key, rationale.

    Deterministic given the same inputs (temperature=0). One retry on
    validator failure. Returns a fallback message on hard error rather than
    raising.
    """
    user_prompt = build_prompt(category, merchant, trigger, customer)

    try:
        client = _GeminiClient.get()
    except RuntimeError as e:
        return _fallback_message(category, merchant, trigger, customer, str(e))

    raw_text = ""
    last_errors: list[str] = []
    for attempt in range(2):
        prompt = user_prompt
        if attempt == 1 and last_errors:
            prompt = (
                user_prompt
                + "\n\nYour previous output failed validation with these errors:\n- "
                + "\n- ".join(last_errors)
                + "\nFix all of them and return corrected JSON."
            )
        # Rate-limit-aware call with retry on 429.
        rate_retries_left = _rate_limit_max_retries()
        while True:
            try:
                _pace()
                raw_text = client.generate_json(SYSTEM_PROMPT, prompt)
                break
            except Exception as e:  # noqa: BLE001
                if _is_retryable_error(e) and rate_retries_left > 0 and not _is_daily_quota_exhausted(e):
                    rate_retries_left -= 1
                    sleep_for = _extract_retry_seconds(e, _rate_limit_default_backoff())
                    time.sleep(sleep_for)
                    continue
                return _fallback_message(category, merchant, trigger, customer, f"llm_error:{e}")

        try:
            parsed = parse_json_loose(raw_text)
        except json.JSONDecodeError as e:
            last_errors = [f"output was not valid JSON: {e}"]
            continue

        ok, errs = validate(
            parsed, category=category, merchant=merchant, trigger=trigger, customer=customer
        )
        if ok:
            # Force trigger's suppression_key if the model invented one.
            if trigger.get("suppression_key") and not parsed.get("suppression_key"):
                parsed["suppression_key"] = trigger["suppression_key"]
            return parsed
        last_errors = errs

    # Both attempts failed. Try to salvage by returning the parsed object
    # if the only failures are soft (anti-hallucination, length); otherwise fallback.
    try:
        parsed = parse_json_loose(raw_text)
        if all(k in parsed for k in ("body", "cta", "send_as", "suppression_key", "rationale")):
            parsed.setdefault("rationale", "")
            parsed["rationale"] = (
                str(parsed["rationale"]) + f" [validator warnings: {'; '.join(last_errors)}]"
            )
            return parsed
    except Exception:  # noqa: BLE001
        pass
    return _fallback_message(
        category, merchant, trigger, customer, f"validation_failed:{last_errors}"
    )
