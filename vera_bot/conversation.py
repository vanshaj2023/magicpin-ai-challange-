"""Multi-turn conversation handling for /v1/reply.

Decides one of: send | wait | end. Detects auto-replies, hard stops, and
explicit action intents. Routes everything else through compose() with a
synthesized continuation trigger.
"""

from __future__ import annotations

import re
from typing import Any

from .compose import compose
from .state import ConversationState, Store


# ---- intent detectors -------------------------------------------------------

_HARD_STOP = re.compile(
    r"\b(stop|unsubscribe|do not contact|don't contact|not interested|"
    r"nahi chahiye|nahi karna|band karo|mat bhejo|mana hai)\b",
    re.IGNORECASE,
)

_ACTION_YES = re.compile(
    r"\b(yes|yeah|yep|ya|sure|ok|okay|go ahead|let'?s do it|please do|"
    r"send it|share it|pull it|draft it|do it|"
    r"haan|haanji|jee|bilkul|theek hai|thik hai|kar do|bhej do|bhejiye|"
    r"chahiye|judrna hai|jodna hai|join karna hai)\b",
    re.IGNORECASE,
)

_WAIT = re.compile(
    r"\b(later|busy|tomorrow|kal|baad mein|abhi nahi|thodi der|"
    r"call back|call me back|ping me later)\b",
    re.IGNORECASE,
)

# Stock auto-reply phrases seen on WhatsApp Business canned replies.
_AUTOREPLY_PHRASES = [
    "thank you for contacting",
    "thanks for contacting",
    "team tak pahuncha",
    "team ko bata",
    "hamari team",
    "i am an automated",
    "this is an automated",
    "automated assistant",
    "we will get back to you",
    "will revert shortly",
    "out of office",
    "currently unavailable",
    "shukriya",
]


def _looks_like_autoreply(message: str, conv: ConversationState) -> bool:
    msg_lower = message.lower().strip()
    if not msg_lower:
        return False

    for phrase in _AUTOREPLY_PHRASES:
        if phrase in msg_lower:
            return True

    # Verbatim repeat of a prior merchant/customer turn.
    for turn in conv.turns:
        if turn.from_role in ("merchant", "customer") and turn.body.strip().lower() == msg_lower:
            return True

    return False


# ---- main entry point -------------------------------------------------------


def respond(
    store: Store,
    *,
    conversation_id: str,
    merchant_id: str,
    customer_id: str | None,
    from_role: str,
    message: str,
    turn_number: int,
) -> dict[str, Any]:
    """Handle a /v1/reply call. Returns {action, ...} per the testing brief."""
    conv = store.get_conversation(conversation_id)
    if conv is None:
        # Judge replied to a conversation we don't remember (process restart,
        # or a stale id). Create a minimal state so we can still respond.
        conv = store.get_or_create_conversation(
            conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=None,
            send_as="merchant_on_behalf" if customer_id else "vera",
        )

    # Detect auto-reply BEFORE adding this turn, so the verbatim-repeat check
    # doesn't match the just-added message against itself.
    is_autoreply = _looks_like_autoreply(message, conv)
    conv.add_turn(from_role, message)

    # 1. Hard-stop intent — gracefully exit.
    if _HARD_STOP.search(message):
        conv.status = "ended"
        return {
            "action": "end",
            "rationale": "Recipient signaled hard stop / not interested; gracefully exiting.",
        }

    # 2. Auto-reply detection.
    if is_autoreply:
        conv.auto_reply_count += 1
        if conv.auto_reply_count >= 2:
            conv.status = "ended"
            return {
                "action": "end",
                "rationale": (
                    "Repeated auto-reply detected (count="
                    f"{conv.auto_reply_count}); exiting to avoid wasting turns."
                ),
            }
        # Try one more time with a polite, short nudge that asks the human directly.
        body = (
            "Samajh gayi — ye automated reply lag raha hai. "
            "Owner/manager ke paas seedha pahuncha do toh 2 minute ka kaam hai. Chalega?"
        )
        if _is_english_only_merchant(store, merchant_id):
            body = (
                "Looks like an auto-reply. If this can reach the owner directly, "
                "it's a 2-minute thing — happy to walk through. OK?"
            )
        conv.last_bot_body = body
        conv.add_turn("bot", body)
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_stop",
            "rationale": "Detected auto-reply; one targeted nudge at the owner before exiting.",
        }

    # 3. Wait intent — back off.
    if _WAIT.search(message):
        conv.status = "waiting"
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Recipient asked for time; backing off 30 minutes.",
        }

    # 4. Action / yes intent → send next-step message.
    is_action_yes = bool(_ACTION_YES.search(message))

    # 5. For everything else (questions, curveballs, neutral replies) and for
    # action-yes, route through compose() with a synthesized continuation
    # trigger so the LLM produces a context-grounded next message.
    next_msg = _compose_followup(
        store=store,
        conv=conv,
        merchant_id=merchant_id,
        customer_id=customer_id,
        merchant_message=message,
        is_action_yes=is_action_yes,
        turn_number=turn_number,
    )

    if next_msg is None:
        # Couldn't compose; nudge cap reached.
        if conv.nudge_count >= 3:
            conv.status = "ended"
            return {
                "action": "end",
                "rationale": "3 unanswered nudges; exiting gracefully.",
            }
        conv.nudge_count += 1
        body = "Got it. Want me to share the most useful thing this week — quick read?"
        conv.last_bot_body = body
        conv.add_turn("bot", body)
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_stop",
            "rationale": "Generic curiosity follow-up; compose() unavailable.",
        }

    conv.last_bot_body = next_msg["body"]
    conv.add_turn("bot", next_msg["body"])
    return {
        "action": "send",
        "body": next_msg["body"],
        "cta": next_msg.get("cta", "open_ended"),
        "rationale": next_msg.get("rationale", "Continuation in conversation."),
    }


def _is_english_only_merchant(store: Store, merchant_id: str) -> bool:
    merchant = store.get_context("merchant", merchant_id) or {}
    langs = (merchant.get("identity") or {}).get("languages") or []
    return "hi" not in langs


def _compose_followup(
    *,
    store: Store,
    conv: ConversationState,
    merchant_id: str,
    customer_id: str | None,
    merchant_message: str,
    is_action_yes: bool,
    turn_number: int,
) -> dict | None:
    """Build a continuation trigger and call compose() to draft the next msg."""
    merchant = store.get_context("merchant", merchant_id)
    if merchant is None:
        return None
    category_slug = merchant.get("category_slug")
    if not category_slug:
        return None
    category = store.get_context("category", category_slug)
    if category is None:
        return None
    customer = store.get_context("customer", customer_id) if customer_id else None

    base_trigger = (
        store.get_context("trigger", conv.trigger_id) if conv.trigger_id else None
    ) or {}

    # Synthesize a continuation trigger so compose() has a meaningful payload.
    cont_kind = "active_planning_intent" if is_action_yes else "dormant_with_vera"
    suppression_key = (
        f"continuation:{conv.conversation_id}:{turn_number}"
    )
    cont_trigger = {
        "id": f"{conv.conversation_id}_cont_{turn_number}",
        "scope": "customer" if customer_id else "merchant",
        "kind": cont_kind,
        "source": "internal",
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "payload": {
            "original_trigger_id": base_trigger.get("id"),
            "original_kind": base_trigger.get("kind"),
            "merchant_just_said": merchant_message,
            "is_action_intent": is_action_yes,
            "turn_number": turn_number,
        },
        "urgency": 3 if is_action_yes else 2,
        "suppression_key": suppression_key,
        "expires_at": "",
    }

    return compose(category, merchant, cont_trigger, customer)
