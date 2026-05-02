#!/usr/bin/env python3
"""Build submission.jsonl from the canonical 30 test pairs.

Usage:
    python scripts/build_submission.py \
        --dataset dataset/expanded \
        --out submission.jsonl
    # Re-run only the lines that fell back to the deterministic safety net:
    python scripts/build_submission.py --resume

Reads ./dataset/expanded/test_pairs.json and the corresponding category /
merchant / customer / trigger files, calls bot.compose() for each pair, and
writes one JSONL line per pair.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Make the project root importable when run as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_one(directory: Path, id_value: str) -> dict:
    matches = sorted(directory.glob(f"{id_value}*.json"))
    if not matches:
        for path in directory.glob("*.json"):
            obj = _load_json(path)
            for key in ("id", "merchant_id", "customer_id"):
                if obj.get(key) == id_value:
                    return obj
        raise FileNotFoundError(f"no file in {directory} matches id={id_value!r}")
    return _load_json(matches[0])


def _is_fallback_record(record: dict) -> bool:
    rationale = (record.get("rationale") or "").strip()
    return rationale.startswith("Fallback message")


def _read_existing(out_path: Path) -> dict[str, dict]:
    """Load whatever is already on disk, keyed by test_id."""
    if not out_path.exists():
        return {}
    by_id: dict[str, dict] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "test_id" in rec:
            by_id[rec["test_id"]] = rec
    return by_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset/expanded")
    parser.add_argument("--out", default="submission.jsonl")
    parser.add_argument(
        "--pace",
        type=float,
        default=13.0,
        help=(
            "Min seconds between LLM calls. Default 13s keeps under Gemini "
            "free-tier 5 RPM (gemini-2.5-flash). For flash-lite (15 RPM) try "
            "5. Set to 0 to disable pacing on paid tiers."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Read --out, keep all non-fallback lines, and re-compose only the "
            "lines whose rationale starts with 'Fallback message'."
        ),
    )
    args = parser.parse_args()

    if args.pace > 0:
        os.environ["GEMINI_MIN_INTERVAL"] = str(args.pace)

    dataset_dir = Path(args.dataset)
    out_path = Path(args.out)
    test_pairs = _load_json(dataset_dir / "test_pairs.json")["pairs"]

    cat_dir = dataset_dir / "categories"
    mer_dir = dataset_dir / "merchants"
    cus_dir = dataset_dir / "customers"
    trg_dir = dataset_dir / "triggers"

    existing = _read_existing(out_path) if args.resume else {}
    if args.resume:
        kept = sum(1 for r in existing.values() if not _is_fallback_record(r))
        todo = [p for p in test_pairs if _is_fallback_record(existing.get(p["test_id"], {}))
                or p["test_id"] not in existing]
        print(f"Resume: keeping {kept} good lines, re-composing {len(todo)} fallbacks/missing")
    else:
        todo = list(test_pairs)

    results: dict[str, dict] = {tid: rec for tid, rec in existing.items()
                                if not args.resume or not _is_fallback_record(rec)}

    for i, pair in enumerate(todo, 1):
        test_id = pair["test_id"]
        trigger = _find_one(trg_dir, pair["trigger_id"])
        merchant = _find_one(mer_dir, pair["merchant_id"])
        category_slug = merchant.get("category_slug") or trigger.get("payload", {}).get("category")
        if not category_slug:
            raise ValueError(f"could not resolve category for pair {test_id}")
        category = _load_json(cat_dir / f"{category_slug}.json")
        customer = None
        if pair.get("customer_id"):
            customer = _find_one(cus_dir, pair["customer_id"])

        t0 = time.monotonic()
        msg = compose(category, merchant, trigger, customer)
        elapsed = time.monotonic() - t0

        results[test_id] = {"test_id": test_id, **msg}
        status = "FALLBACK" if _is_fallback_record(results[test_id]) else "ok      "
        print(f"[{i:02d}/{len(todo)}] {test_id} {trigger['kind']:>30s}  {elapsed:5.1f}s  {status}")

        # Write incrementally so a daily-quota crash doesn't lose progress.
        ordered = [results[p["test_id"]] for p in test_pairs if p["test_id"] in results]
        out_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in ordered) + "\n",
            encoding="utf-8",
        )

    final_records = [results[p["test_id"]] for p in test_pairs if p["test_id"] in results]
    fallbacks = sum(1 for r in final_records if _is_fallback_record(r))
    print(f"\nWrote {len(final_records)} lines to {out_path}  ({fallbacks} fallback)")


if __name__ == "__main__":
    main()
