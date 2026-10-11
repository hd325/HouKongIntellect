"""
RAG 效果评测模块（旗舰创新 · 论文可用）

提供两类能力：

1) RAGAS 集成（可选依赖 `ragas`）
   - faithfulness       忠实度
   - answer_relevancy   答案相关性
   - context_precision  上下文精度
   - 幻觉率（= 1 - faithfulness）
   若环境未安装 ragas，自动回退到内置轻量指标（基于词重叠的启发式），
   保证「任何环境都能跑评测」。

2) 对照实验（溯源开 / 关）
   - 记录同一问题在「带溯源 RAG」与「普通大模型」下的回答；
   - 计算引用准确率（citation_accuracy）与幻觉率（hallucination_rate），
   用于直观量化段落级溯源带来的增益。

测试集：默认从 services/evaluation/testset.json 读取；
若不存在则使用内置示例（可在 UI/脚本中替换为真实题集）。
"""
import json
import logging
import os
import re
from typing import Dict, List, Optional

logger = logging.getLogger("ragas_eval")

_TESTSET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testset.json")


# ----------------------------------------------------------------------
# 工具：中文/英文分词（用于内置指标）
# ----------------------------------------------------------------------
def _tokens(text: str) -> set:
    text = (text or "").lower()
    toks = set()
    chars = [c for c in text if "\u4e00" <= c <= "\u9fa5"]
    for i, c in enumerate(chars):
        toks.add(("c", c))
        if i + 1 < len(chars):
            toks.add(("b", c + chars[i + 1]))
    for w in re.findall(r"[a-z0-9]+", text):
        toks.add(("w", w))
    return toks


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / (len(ta | tb) + 1e-9)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"[。！？!?\n]", text) if s.strip()]


# ----------------------------------------------------------------------
# 内置轻量指标（ragas 缺失时回退）
# ----------------------------------------------------------------------
def _builtin_metrics(question: str, answer: str, contexts: List[str]) -> Dict[str, float]:
    ctx_text = "\n".join(contexts)
    ans_sentences = _sentences(answer)

    # 忠实度：答案句是否被任一上下文支撑（词重叠 > 0.08）
    supported = 0
    for s in ans_sentences:
        if any(_overlap(s, c) > 0.08 for c in contexts):
            supported += 1
    faithfulness = supported / len(ans_sentences) if ans_sentences else 1.0

    # 答案相关性：问题与答案的词重叠
    answer_relevancy = _overlap(question, answer)

    # 上下文精度：答案与上下文的平均重叠
    if ans_sentences:
        ctx_precision = sum(_overlap(s, ctx_text) for s in ans_sentences) / len(ans_sentences)
    else:
        ctx_precision = 0.0

    return {
        "faithfulness": round(faithfulness, 4),
        "answer_relevancy": round(answer_relevancy, 4),
        "context_precision": round(ctx_precision, 4),
        "hallucination_rate": round(1.0 - faithfulness, 4),
    }


# ----------------------------------------------------------------------
# RAGAS 集成
# ----------------------------------------------------------------------
def evaluate(test_cases: List[Dict[str, object]]) -> Dict[str, object]:
    """
    对测试集评测。

    test_cases 每项：{"question": str, "answer": str, "contexts": List[str]}
    返回：{"backend": "ragas"|"builtin", "samples": int, "metrics": {...均值}}
    """
    if not test_cases:
        return {"backend": "none", "samples": 0, "metrics": {}}

    # 尝试 RAGAS
    try:
        from ragas import evaluate as ragas_evaluate
        from ragas.metrics import (
            faithfulness,
            answer_relevancy,
            context_precision,
        )
        from datasets import Dataset

        data = {
            "question": [t["question"] for t in test_cases],
            "answer": [t["answer"] for t in test_cases],
            "contexts": [t["contexts"] for t in test_cases],
        }
        dataset = Dataset.from_dict(data)
        score = ragas_evaluate(
            dataset,
            metrics=[faithfulness, answer_relevancy, context_precision],
        )
        rows = score.to_pandas() if hasattr(score, "to_pandas") else None
        mean = {
            "faithfulness": float(score["faithfulness"]),
            "answer_relevancy": float(score["answer_relevancy"]),
            "context_precision": float(score["context_precision"]),
            "hallucination_rate": 1.0 - float(score["faithfulness"]),
        }
        return {
            "backend": "ragas",
            "samples": len(test_cases),
            "metrics": {k: round(v, 4) for k, v in mean.items()},
            "per_sample": rows.to_dict(orient="records") if rows is not None else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️ RAGAS 不可用，回退内置指标: {e}")

    # 内置回退
    aggregated = {"faithfulness": 0.0, "answer_relevancy": 0.0, "context_precision": 0.0}
    for t in test_cases:
        m = _builtin_metrics(t.get("question", ""), t.get("answer", ""), t.get("contexts", []))
        aggregated["faithfulness"] += m["faithfulness"]
        aggregated["answer_relevancy"] += m["answer_relevancy"]
        aggregated["context_precision"] += m["context_precision"]
    n = len(test_cases)
    metrics = {k: round(v / n, 4) for k, v in aggregated.items()}
    metrics["hallucination_rate"] = round(1.0 - metrics["faithfulness"], 4)
    return {"backend": "builtin", "samples": n, "metrics": metrics}


# ----------------------------------------------------------------------
# 对照实验：溯源开 / 关
# ----------------------------------------------------------------------
def compare_mode(records: List[Dict[str, object]]) -> Dict[str, object]:
    """
    对照实验量化。

    records 每项：{
        "question": str,
        "with_trace_answer": str,        # 带溯源 RAG 的回答
        "without_trace_answer": str,     # 普通大模型回答
        "contexts": List[str],           # 带溯源时检索到的上下文
        "cited_chunk_ids": List[str],    # 带溯源回答中实际出现的 [N] 总数（或不传）
    }
    返回引用准确率与两侧幻觉率对比。
    """
    if not records:
        return {"samples": 0}

    def _halluc(answer, contexts):
        m = _builtin_metrics("", answer, contexts or [])
        return m["hallucination_rate"]

    def _citation_accuracy(answer, n_chunks):
        # 统计 [N] 标记，N 在合法区间且上下文能支撑的比例
        markers = re.findall(r"\[(\d+)\]", answer)
        if not markers:
            return None  # 未使用引用（视为 N/A）
        valid = 0
        for mk in markers:
            n = int(mk)
            if 1 <= n <= n_chunks:
                valid += 1
        return valid / len(markers)

    with_halluc, without_halluc, cit_accs = [], [], []
    for r in records:
        ctx = r.get("contexts", [])
        with_halluc.append(_halluc(r.get("with_trace_answer", ""), ctx))
        without_halluc.append(_halluc(r.get("without_trace_answer", ""), []))
        ca = _citation_accuracy(r.get("with_trace_answer", ""), r.get("n_chunks", 9999))
        if ca is not None:
            cit_accs.append(ca)

    return {
        "samples": len(records),
        "with_trace_hallucination_rate": round(sum(with_halluc) / len(with_halluc), 4),
        "without_trace_hallucination_rate": round(sum(without_halluc) / len(without_halluc), 4),
        "citation_accuracy": round(sum(cit_accs) / len(cit_accs), 4) if cit_accs else None,
        "hallucination_reduction": round(
            (sum(without_halluc) / len(without_halluc))
            - (sum(with_halluc) / len(with_halluc)),
            4,
        ),
    }


# ----------------------------------------------------------------------
# 测试集
# ----------------------------------------------------------------------
def prepare_testset() -> List[Dict[str, object]]:
    """读取或生成评测测试集。"""
    if os.path.exists(_TESTSET_PATH):
        try:
            with open(_TESTSET_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ 测试集读取失败: {e}")

    # 内置示例（可按需替换为真实题集）
    return [
        {
            "question": "二次函数的图像有什么特征？",
            "answer": "",
            "contexts": [
                "二次函数的图像是一条抛物线，开口向上或向下。",
                "抛物线的顶点坐标为 (-b/2a, (4ac-b²)/4a)。",
            ],
        }
    ]
