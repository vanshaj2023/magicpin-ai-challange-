"""Output validator for composed messages.

Cheap, deterministic checks. Returns (ok, errors). One retry on failure.
"""

from __future__ import annotations

import json
import re

VALID_CTA = {"binary_yes_stop", "open_ended", "none"}
VALID_SEND_AS = {"vera", "merchant_on_behalf"}

# Romanized Hindi tokens that signal hi-en code-mix.
_HI_TOKENS = re.compile(
    r"\b(aap|aapka|aapki|hai|hain|kar|karna|karein|karenge|kya|kyun|"
    r"haan|nahi|main|mera|meri|mere|chahiye|chahingi|chahenge|"
    r"abhi|kal|aaj|ko|ke|ki|ka|liye|saath|bhi|toh|paas|bolein|bolo|"
    r"shukriya|dhanyavaad|namaste|bahut|thoda|chalega|thik|theek|"
    r"samajh|raha|rahi|rahe|gayi|gaya|hua|hui|kiya|diya|lagta|lagti)\b",
    re.IGNORECASE,
)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Stock anti-patterns the brief calls out.
_BANNED_PHRASES = [
    "hope you're doing well",
    "hope you are doing well",
    "i'm reaching out",
    "i am reaching out",
    "amazing deal",
    "limited time only",
    "dear sir/madam",
    "dear sir or madam",
]


def _flatten_strings(obj) -> str:
    """Concatenate all string-ish values in a nested dict/list. Used for
    substring-based citation checks."""
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (str, int, float)) and x is not None and x is not False and x is not True:
            out.append(str(x))

    walk(obj)
    return " || ".join(out)


def _required_language(merchant: dict) -> str:
    langs = (merchant.get("identity") or {}).get("languages") or []
    if "hi" in langs:
        return "hi-en"
    return "en"


def _is_hi_en(text: str) -> bool:
    return bool(_DEVANAGARI.search(text)) or bool(_HI_TOKENS.search(text))


def validate(
    output: dict,
    *,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: dict | None,
) -> tuple[bool, list[str]]:
    """Validate a compose() LLM output. Returns (ok, errors)."""
    errs: list[str] = []

    # Shape
    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        if key not in output:
            errs.append(f"missing required key: {key}")
    if errs:
        return False, errs

    body = (output.get("body") or "").strip()
    cta = output.get("cta")
    send_as = output.get("send_as")

    # Enums
    if cta not in VALID_CTA:
        errs.append(f"cta must be one of {sorted(VALID_CTA)}; got {cta!r}")
    if send_as not in VALID_SEND_AS:
        errs.append(f"send_as must be one of {sorted(VALID_SEND_AS)}; got {send_as!r}")

    # send_as must match customer presence
    if customer is not None and send_as != "merchant_on_behalf":
        errs.append("customer context provided but send_as is not 'merchant_on_behalf'")
    if customer is None and send_as == "merchant_on_behalf":
        errs.append("send_as is 'merchant_on_behalf' but no customer context provided")

    # Length sanity
    if not body:
        errs.append("body is empty")
    elif len(body) > 1200:
        errs.append(f"body too long ({len(body)} chars); aim for under ~120 words")

    # Banned phrases (anti-patterns)
    body_lower = body.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in body_lower:
            errs.append(f"banned anti-pattern phrase present: {phrase!r}")

    # Voice taboos from category
    voice = (category or {}).get("voice") or {}
    for taboo in voice.get("taboos") or []:
        # Word-boundary match to avoid false positives.
        if re.search(rf"\b{re.escape(taboo)}\b", body, flags=re.IGNORECASE):
            errs.append(f"category voice taboo word used: {taboo!r}")

    # Language match
    required_lang = _required_language(merchant)
    if required_lang == "hi-en":
        if not _is_hi_en(body):
            errs.append(
                "merchant language preference includes Hindi but body has no "
                "Hindi tokens (use natural hi-en code-mix)"
            )
    else:
        # Pure English merchant: Devanagari is a hard fail; romanized Hindi tolerated.
        if _DEVANAGARI.search(body):
            errs.append("merchant language is English-only but body uses Devanagari")

    # Anti-hallucination: any 4+ digit number in the body that isn't a year
    # 19xx/20xx must appear in the input contexts. Best-effort.
    flat = _flatten_strings({"c": category, "m": merchant, "t": trigger, "u": customer})
    for num in re.findall(r"\b\d{3,}\b", body):
        if re.match(r"^(19|20)\d{2}$", num):
            continue  # year-like
        if num not in flat:
            errs.append(
                f"number {num!r} in body does not appear in input contexts "
                f"(possible hallucination)"
            )

    # Anti-repetition: don't send a message verbatim already in conversation_history.
    history = (merchant.get("conversation_history") or [])
    for turn in history:
        prior = (turn.get("body") or "").strip()
        if prior and prior == body:
            errs.append("body is verbatim identical to a prior message in conversation_history")
            break

    return (len(errs) == 0), errs


def parse_json_loose(text: str) -> dict:
    """Parse JSON that may be wrapped in ```json fences or have leading prose."""
    text = text.strip()
    # Strip ``` fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Find first {...} block if there's extraneous text
    if not text.lstrip().startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)
