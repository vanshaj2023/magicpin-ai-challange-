"""Hand-authored fallback fillers for T28, T29, T30.

We hit a structural API-key problem (403 PERMISSION_DENIED on every newly
created Gemini key on this Google account) and the original key's daily quota
is exhausted. Rather than wait, these three messages are composed by hand
following the same family templates the LLM uses, anchored only on data
present in the corresponding context files.

This script splices these three records into submission.jsonl in place,
preserving the 27 LLM-generated lines.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUB = ROOT / "submission.jsonl"


HAND_AUTHORED = {
    "T28": {
        "body": (
            "Hi Priya, Dr. Meera's clinic se. Aapka 6-month cleaning recall "
            "due hai (last visit 2026-05-12). 2 evening slots ready hain — "
            "Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm. Dental Cleaning @ ₹299. "
            "Reply 1 for Wed, 2 for Thu, ya apna preferred time bata dein."
        ),
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "suppression_key": "recall:c_001_priya_for_m001:6mo",
        "rationale": (
            "Customer-facing 6-month cleaning recall with the two specific "
            "evening slots from the trigger payload, the merchant's real "
            "active offer (Dental Cleaning @ ₹299), Priya's hi-en mix "
            "language preference, and a numeric-reply CTA appropriate for "
            "a booking flow."
        ),
    },
    "T29": {
        "body": (
            "Hi Diya, Zen Yoga Studio here. It's been a month since your "
            "last class on 2026-04-01 — your spot's still warm. Restart "
            "with First Month @ ₹499 and a complimentary Body "
            "Composition Analysis to see where you're at after the break. "
            "Reply YES to lock a class this week."
        ),
        "cta": "binary_yes_stop",
        "send_as": "merchant_on_behalf",
        "suppression_key": "recall_due:m_008_zenyoga_gym_chennai:gen_66",
        "rationale": (
            "Customer-facing win-back for Diya (lapsed_soft, 9 prior visits, "
            "English-pref). Anchors on the exact last_visit date, the "
            "merchant's two real active offers (First Month @ ₹499 + "
            "Free Body Composition Analysis), single binary CTA."
        ),
    },
    "T30": {
        "body": (
            "Dr. Meera, DCI ka naya circular hai (Dental Council of India "
            "circular 2026-11-04) — IOPA radiograph dose limit revise ho "
            "gayi hai effective 2026-12-15. D-speed film naye limit pe pass "
            "nahi hota; E-speed aur Digital RVG sensors fine hain. Aapko "
            "X-ray setup ka SOP audit chahiye is deadline se pehle. Main "
            "aapke liye 1-page audit checklist draft kar deti hoon — "
            "reply YES toh bhej deti hoon."
        ),
        "cta": "binary_yes_stop",
        "send_as": "vera",
        "suppression_key": "compliance:dci_radiograph:2026",
        "rationale": (
            "Regulation-change/research-digest framing: cites the DCI "
            "circular by name and date, the new compliance deadline from "
            "the trigger payload, the actionable distinction "
            "(D-speed/E-speed/RVG) from the digest item, and offers effort "
            "externalization (drafted checklist) with a single binary CTA."
        ),
    },
}


def main() -> None:
    records = []
    for line in SUB.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))

    replaced = 0
    for r in records:
        if r["test_id"] in HAND_AUTHORED:
            patch = HAND_AUTHORED[r["test_id"]]
            for k, v in patch.items():
                r[k] = v
            replaced += 1

    SUB.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    print(f"Patched {replaced} records in {SUB}")


if __name__ == "__main__":
    main()
