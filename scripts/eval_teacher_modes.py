"""Compare default, frozen v1 and current teacher prompts with identical facts.

Reads private review cases. Makes fresh bounded model calls without creating
application conversations, modifying sessions, or accessing recorder credentials.
Results are private; success alone is not a teaching-quality score.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Production debug logging contains credential prefixes; no API values are logged.
logging.disable(logging.CRITICAL)
import main
from services.teacher_skill import prepare_skill


async def evaluate(args):
    cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))
    semaphore = asyncio.Semaphore(args.parallel)

    async def answer(case, variant):
        async with semaphore:
            question = case["question"]
            if variant == "v1":
                addon, metadata = case["v1_addon"], case["v1_metadata"]
            else:
                addon, metadata = prepare_skill("chemistry-teacher" if variant == "v2" else "none", question, args.owner_account)
            # Use the same nonempty facts for all variants. An empty RAG prompt
            # would tell the default model to refuse and confound the comparison.
            material = "[1] 以下為本次統一校驗材料，屬一般化學知識，不是老師課堂逐字原話。\n" + case["reference_material"]
            prompt = main.build_rag_prompt(question, "", material) + addon
            started = time.monotonic()
            content, errors = "", []
            try:
                async for event in main.stream_deepseek(prompt, question, think=False):
                    for line in event.splitlines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data.get("type") == "content":
                                content += data.get("text", "")
                            elif data.get("type") == "error":
                                errors.append("model_stream_error")
            except Exception as error:
                errors.append(type(error).__name__)
            result = {"case_id": case["id"], "question": question, "variant": variant, "answer": content,
                      "metadata": metadata, "errors": errors, "elapsed_seconds": round(time.monotonic() - started, 2)}
            print(json.dumps({"case": case["id"], "variant": variant, "characters": len(content), "errors": errors}), flush=True)
            return result

    results = await asyncio.gather(*(answer(case, variant) for case in cases for variant in args.variants))
    output = {"method": "identical question and nonempty reference material; fresh context for every generation",
              "default_mode_unchanged": True, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(result["errors"] or not result["answer"] for result in results))


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--owner-account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parallel", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--variants", nargs="+", choices=("none", "v1", "v2"), default=["none", "v1", "v2"])
    return asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(run())
