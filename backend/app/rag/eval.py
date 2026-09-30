"""Score RAG answers against the golden set.

    python -m app.rag.eval        (from backend/; needs the index and SARVAM_API_KEY)

Prints each example and three numbers:
    accuracy              examples answered correctly (right facts, or a correct refusal)
    citation rate         answered examples that carry at least one citation
    correct-refusal rate  NO_SOURCE examples that were refused
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import yaml

from app import config
from app.rag import answer as rag_answer

GOLDEN = "golden.yaml"


def load_golden(path: Path | None = None) -> list[dict]:
    raw = yaml.safe_load(Path(path or config.EVAL_DIR / GOLDEN).read_text(encoding="utf-8")) or {}
    examples = raw.get("examples") or []
    for example in examples:
        if example.get("expect") not in {"answer", "no_source"} or not example.get("question"):
            raise ValueError(f"golden example {example.get('id')!r}: needs a question and expect answer|no_source")
    return examples


def _correct(example: dict, result: rag_answer.Answer) -> bool:
    if example["expect"] == "no_source":
        return result.status == rag_answer.NO_SOURCE
    if result.status != rag_answer.ANSWERED:
        return False
    text = result.text_en.lower()
    if not all(str(fragment).lower() in text for fragment in example.get("must_contain") or []):
        return False
    cite = example.get("cite")
    if cite:
        return any(c.doc_type == cite["doc_type"] and c.page == cite["page"] for c in result.citations)
    return True


def run(examples: list[dict], answer_fn: Callable[[dict], rag_answer.Answer]) -> dict:
    rows = []
    for example in examples:
        result = answer_fn(example)
        rows.append({"id": example.get("id"), "example": example, "result": result, "correct": _correct(example, result)})

    answered = [r for r in rows if r["result"].status == rag_answer.ANSWERED]
    refusals = [r for r in rows if r["example"]["expect"] == "no_source"]
    return {
        "rows": rows,
        "accuracy": sum(r["correct"] for r in rows) / len(rows) if rows else 0.0,
        "citation_rate": sum(bool(r["result"].citations) for r in answered) / len(answered) if answered else 0.0,
        "correct_refusal_rate": (
            sum(r["result"].status == rag_answer.NO_SOURCE for r in refusals) / len(refusals) if refusals else 0.0
        ),
    }


def _live(example: dict) -> rag_answer.Answer:
    return rag_answer.answer(
        example["question"],
        intent="question",
        language=example.get("language", rag_answer.ENGLISH),
        insurer=example.get("insurer"),
        product=example.get("product"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score RAG answers against the golden set.")
    parser.add_argument("--golden", type=Path, default=None, help="golden YAML (default: data/eval/golden.yaml)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    report = run(load_golden(args.golden), _live)
    for row in report["rows"]:
        result = row["result"]
        print(f"{'PASS' if row['correct'] else 'FAIL'}  {row['id']:<22} {result.status:<10} {result.text_en[:90]}")
    print(
        f"\naccuracy {report['accuracy']:.2f} | citation rate {report['citation_rate']:.2f} | "
        f"correct-refusal rate {report['correct_refusal_rate']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
