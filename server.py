"""FastAPI server exposing the 5 endpoints required by the judge harness.

Run:
    uvicorn server:app --port 8080
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from vera_bot.compose import compose
from vera_bot.conversation import respond
from vera_bot.state import get_store


VERSION = "1.0.0"
TEAM_NAME = os.environ.get("TEAM_NAME", "Solo Submission")
TEAM_MEMBERS = [m.strip() for m in os.environ.get("TEAM_MEMBERS", "").split(",") if m.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "aggs1825@gmail.com")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Hard cap on actions emitted per /v1/tick to stay inside Gemini free-tier
# rate limits.
TICK_ACTION_BUDGET = int(os.environ.get("TICK_ACTION_BUDGET", "3"))


app = FastAPI(title="Vera Bot")


# ---- request / response models ---------------------------------------------


class ContextPush(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: Literal["merchant", "customer"]
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


# ---- endpoints --------------------------------------------------------------


@app.post("/v1/context")
def post_context(req: ContextPush) -> JSONResponse:
    store = get_store()
    accepted, current = store.upsert_context(req.scope, req.context_id, req.version, req.payload)
    if not accepted:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current,
            },
        )
    return JSONResponse(
        content={
            "accepted": True,
            "ack_id": f"ack_{uuid.uuid4().hex[:10]}",
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.post("/v1/tick")
def post_tick(req: TickRequest) -> dict[str, Any]:
    store = get_store()
    actions: list[dict] = []
    budget = TICK_ACTION_BUDGET

    for trigger_id in req.available_triggers:
        if budget <= 0:
            break
        trigger = store.get_context("trigger", trigger_id)
        if trigger is None:
            continue
        suppression_key = trigger.get("suppression_key", "")
        if store.is_suppressed(suppression_key):
            continue

        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            continue
        merchant = store.get_context("merchant", merchant_id)
        if merchant is None:
            continue
        category_slug = merchant.get("category_slug") or trigger.get("payload", {}).get("category")
        if not category_slug:
            continue
        category = store.get_context("category", category_slug)
        if category is None:
            continue

        customer_id = trigger.get("customer_id")
        customer = store.get_context("customer", customer_id) if customer_id else None

        msg = compose(category, merchant, trigger, customer)
        if not msg or not msg.get("body"):
            continue

        conv_id = f"conv_{uuid.uuid4().hex[:10]}"
        send_as = msg.get("send_as", "merchant_on_behalf" if customer else "vera")
        conv = store.get_or_create_conversation(
            conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trigger_id,
            send_as=send_as,
        )
        conv.last_bot_body = msg["body"]
        conv.add_turn("bot", msg["body"])

        store.suppress(suppression_key)
        budget -= 1

        actions.append(
            {
                "conversation_id": conv_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": send_as,
                "trigger_id": trigger_id,
                "template_name": f"vera_{trigger.get('kind','generic')}_v1",
                "template_params": [],
                "body": msg["body"],
                "cta": msg.get("cta", "open_ended"),
                "suppression_key": msg.get("suppression_key", suppression_key),
                "rationale": msg.get("rationale", ""),
            }
        )

    return {"actions": actions}


@app.post("/v1/reply")
def post_reply(req: ReplyRequest) -> dict[str, Any]:
    store = get_store()
    return respond(
        store,
        conversation_id=req.conversation_id,
        merchant_id=req.merchant_id,
        customer_id=req.customer_id,
        from_role=req.from_role,
        message=req.message,
        turn_number=req.turn_number,
    )


@app.get("/v1/healthz")
def healthz() -> dict[str, Any]:
    store = get_store()
    return {
        "status": "ok",
        "uptime_seconds": store.uptime_seconds(),
        "contexts_loaded": store.context_counts(),
    }


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS or ["solo"],
        "model": MODEL_NAME,
        "approach": "Trigger-routed prompts (6 families) + Gemini compose + validator with retry",
        "contact_email": CONTACT_EMAIL,
        "version": VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
