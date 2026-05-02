# Vera Bot — Design Spec

**Status:** Draft
**Date:** 2026-05-02
**Goal:** Submit both a static `bot.py` + `submission.jsonl` (Path A) and a live HTTP bot runnable against `judge_simulator.py` (Path B) for the magicpin AI Challenge.

---

## 1. Objective

Build a merchant-AI bot that outperforms production Vera on the 5-dimension rubric (Specificity, Category fit, Merchant fit, Trigger relevance, Engagement compulsion) by composing WhatsApp messages from 4 context layers (category / merchant / trigger / optional customer).

Two deliverables:

- **Static submission:** `bot.py` exposing `compose(category, merchant, trigger, customer) -> dict` + `submission.jsonl` (30 lines, one per canonical test pair) + `README.md`.
- **Live HTTP bot:** stateful FastAPI server exposing 5 endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) that the judge harness drives in real time.

The static `compose()` is the core; the HTTP server wraps it and adds state + multi-turn handling.

## 2. Approach: Trigger-routed prompts

The chosen architecture is **Option 2** from brainstorming. The ~15 trigger kinds in the brief cluster into a small number of message families that need different framing. We route on `trigger.kind` to a family-specific prompt template, then call Gemini (temp=0, JSON output) to compose the message.

Rationale: "Trigger relevance" is a scored rubric dimension. A single generic prompt smears across families and produces same-shape output for very different events (research digest vs perf dip vs recall). Per-family templates anchor each message in its own framing without needing retrieval infra.

**Trigger families (6):**

| Family | Trigger kinds | Prompt focus |
|---|---|---|
| `research_digest` | `category_research_digest_release`, `regulation_change`, `category_trend_movement` | Cite source, peer/clinical voice, low-friction "want me to draft X?" CTA |
| `recall_recurring` | `customer_lapsed_soft`, `appointment_tomorrow`, `scheduled_recurring` | Customer-facing if customer present; specific slot/service offer |
| `perf_signal` | `perf_spike`, `perf_dip`, `milestone_reached` | Lead with concrete number + 7d delta; loss aversion on dips, social proof on spikes |
| `external_event` | `festival_upcoming`, `weather_heatwave`, `local_news_event`, `competitor_opened` | Tie event to merchant's category-specific opportunity; effort-externalization CTA |
| `dormant_nudge` | `dormant_with_vera`, `review_theme_emerged` | Curiosity / asking-the-merchant lever; binary CTA |
| `default` | anything unmapped | Generic compose with full context, lower confidence |

## 3. Components

### 3.1 `bot/compose.py`
Pure function `compose(category, merchant, trigger, customer=None) -> ComposedMessage`. No I/O outside the LLM call.

Pipeline:
1. Route `trigger["kind"]` → family.
2. Build prompt: shared system prompt (voice rules, anti-patterns, compulsion levers) + family-specific instruction + JSON-schema reminder + the 4 contexts as compact JSON.
3. Call Gemini (`temperature=0`, JSON response mode).
4. Validate output via `validator.py`. If invalid, retry once with the validation error appended.
5. Return `{body, cta, send_as, suppression_key, rationale}`.

### 3.2 `bot/templates.py`
- `SYSTEM_PROMPT`: voice rules, anti-patterns (§11 of brief), compulsion levers (§10), language-match rule, "don't fabricate" rule, output JSON schema.
- `FAMILY_PROMPTS`: dict family → template string with `{focus}`, `{do}`, `{avoid}` slots.
- `route_trigger(kind) -> family`.

### 3.3 `bot/validator.py`
Cheap, deterministic checks on the LLM output:
- JSON shape: `body`, `cta`, `send_as`, `suppression_key`, `rationale` present.
- `cta` ∈ `{"binary_yes_stop", "open_ended", "none"}`.
- `send_as` ∈ `{"vera", "merchant_on_behalf"}` and matches whether `customer` is populated.
- Anti-hallucination: every numeric or quoted citation in `body` must appear as a substring in the input contexts (best-effort substring scan over flattened context JSON).
- Language match: if merchant `languages` is hi-en mix, body should contain at least one Devanagari OR Hindi-romanization token (`hai`, `aap`, `kar`, etc.); if pure `en`, no Devanagari.
- No banned phrases from category `voice.taboos`.
- Length sanity: 1 ≤ `len(body)` ≤ 800 chars.

Returns `(ok: bool, errors: list[str])`. One retry on failure.

### 3.4 `bot/state.py`
In-memory store for the HTTP server:
- `contexts: dict[(scope, id), (version, payload)]` — versioned upsert with 409 on stale.
- `conversations: dict[conv_id, ConversationState]` — turns, last-bot-msg, last-merchant-msg, auto-reply count, intent flags, status (`active|waiting|ended`).
- `suppression: set[str]` — keys of recently-sent messages, with TTL.

### 3.5 `bot/conversation.py`
`respond(state, conv_id, message) -> {action, ...}` for `/v1/reply`.

Logic, in order:
1. **Hard-stop intent** ("not interested", "stop", "STOP", "unsubscribe") → `end`.
2. **Auto-reply detection:** verbatim repeat of a prior merchant message ≥2 times, OR contains stock phrases ("automated", "team tak pahuncha", "thank you for contacting"). If detected once → try one nudge ("samajh gayi, aap khud dekhna chahingi?"); twice → `end` politely.
3. **Action intent** ("yes", "haan", "go ahead", "let's do it", "join", "judrna hai", "send it") → `send` with the next-best action message (drafted by Gemini given the context + intent).
4. **Question / curveball** → `send`, route through `compose()` again with synthesized continuation trigger.
5. **Wait signal** ("later", "busy", "tomorrow") → `wait` with 1800s.
6. Default → one curiosity follow-up; after 3 unanswered nudges → `end`.

### 3.6 `server.py`
FastAPI app:
- `POST /v1/context` — versioned upsert into `state.contexts`. 409 on stale, 400 on malformed.
- `POST /v1/tick` — for each active trigger (capped to a budget per tick), if not in suppression and merchant is present, call `compose()` and emit an action. Empty list is valid.
- `POST /v1/reply` — delegate to `conversation.respond()`.
- `GET /v1/healthz` — uptime + counts.
- `GET /v1/metadata` — team info.

Run with `uvicorn server:app --port 8080`.

### 3.7 `scripts/build_submission.py`
Reads `dataset/expanded/test_pairs.json`, calls `compose()` for each, writes `submission.jsonl` (one line per test pair, with `test_id`).

### 3.8 `bot.py` (top-level)
Thin re-export of `compose` from `bot/compose.py` to satisfy the static-submission contract.

### 3.9 `README.md`
1 page: approach summary, model used, tradeoffs, what extra context would help.

## 4. Data flow

```
generate_dataset.py  →  dataset/expanded/{categories,merchants,customers,triggers}/  +  test_pairs.json
                                            │
              ┌─────────────────────────────┴───────────────────────────┐
              ▼                                                         ▼
  build_submission.py  →  submission.jsonl              server.py (FastAPI)
              │                                                ▲
              ▼                                                │
        bot/compose.py  ← templates / validator         judge_simulator.py
              │
              ▼
           Gemini (temp=0, JSON)
```

## 5. LLM contract

- Model: Gemini 2.5 Flash via `google-genai` SDK (free-tier friendly, fast). Pro can be swapped in via env var `GEMINI_MODEL`.
- API key: `GEMINI_API_KEY` env var. Fail loud at startup if missing.
- All calls: `temperature=0`, `response_mime_type="application/json"`, response schema enforced.
- Per-call timeout 25s (bot must answer in 30s). On timeout, fall back to a deterministic templated message keyed off trigger family.
- One retry on validator failure with the errors appended to the prompt.

## 6. Testing

- **Unit:** `validator.py` checks (each rule has a positive + negative case); `route_trigger` mapping; `state.py` upsert/version semantics; auto-reply detector across the 3 patterns.
- **Integration:** golden tests for `compose()` on 3 representative test pairs (research digest, perf dip, customer recall) — assert structural rubric (cites a real number from input, language matches, single CTA). LLM call is real but cached to disk on first run.
- **End-to-end:** run `judge_simulator.py` against the local server. Capture score; iterate.

Testing rule: integration tests hit real Gemini behind a disk cache, not a mock — mocked-LLM tests give false confidence on prompt quality.

## 7. Order of work

1. Run `generate_dataset.py` → produce `dataset/expanded/`.
2. Build `bot/templates.py` + `bot/compose.py` + `bot/validator.py` + `bot.py` shim with one trigger family end-to-end (research_digest); validate against 1 test pair manually.
3. Add the other 5 families.
4. Write `scripts/build_submission.py`; produce first `submission.jsonl`.
5. Wrap in `server.py` (no multi-turn yet); pass warmup + tick smoke tests.
6. Add `state.py` + `conversation.py` for `/v1/reply` multi-turn.
7. Run `judge_simulator.py` end-to-end; iterate prompts on weak rubric dimensions.
8. Write `README.md`.

## 8. Out of scope

- Real Meta WhatsApp API integration — judge doesn't actually call Meta.
- Persistent storage — in-memory is fine; harness doesn't restart the bot mid-test.
- Custom retrieval / vector index — contexts are small enough to inline.
- Multi-language detection beyond {en, hi-en mix}; if a merchant's `languages` doesn't include `en` or `hi`, fall back to English.
- Auth on HTTP endpoints — judge harness is trusted.

## 9. Risks

- **Gemini free-tier rate limits** during a 60-tick test window. Mitigation: per-tick action budget (≤3), in-process queue, cache compose() outputs by `(merchant_id, trigger_id)` for idempotent re-computation.
- **Hallucinated citations** despite validator. Mitigation: validator's substring scan; if it fires on a citation, retry forces "remove fabricated citations".
- **Auto-reply detector false positives** on real merchant short replies ("ok thanks"). Mitigation: only trigger on the verbatim-repeat OR stock-phrase paths, not on length alone.

## 10. Open questions

- Exact Gemini model: Flash vs Pro. Default Flash; bench against Pro on 5 test pairs and pick.
- Whether to ship `conversation_handlers.py` separately for the static submission. Decision: yes, since the brief says it's a tiebreaker and we have it anyway.
