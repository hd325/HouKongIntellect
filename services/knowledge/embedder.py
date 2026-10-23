"""
可插拔文本嵌入器（Embedder）抽象层。

为 RAG 提供统一的向量化接口，支持三种后端，按可用性自动降级，
为「段落级溯源 + 向量检索」提供轻量默认后端与可选后端：

1. tencent  —— 腾讯云 混元 Embedding API（GetEmbedding，1024 维）
               * 复用既有 TENCENT_SECRET_ID / TENCENT_SECRET_KEY（与 OCR 同一套凭证）
               * 无需下载模型权重，线上调用，质量高、零本地依赖
2. local    —— sentence-transformers 本地模型（shibing624/text2vec-base-chinese，768 维）
               * 读取部署者预先下载的本机权重，不在运行时下载
               * 需另装 requirements-local.txt 中的可选依赖
3. tfidf    —— numpy 哈希词袋兜底嵌入（固定维度，零网络/零模型）
               * 核心安装即可使用，不需要外部密钥或模型下载

集合命名规则：hk_knowledge_{backend}_{dim}，避免不同后端维度冲突导致 Chroma 报错。

默认使用 tfidf 后端；通过环境变量
EMBEDDING_BACKEND 可强制指定后端（auto | tencent | local | tfidf）。
"""
import json
import logging
import math
import os
import re
import time
from collections import Counter
from typing import List

logger = logging.getLogger("embedder")

# 防止 sentence-transformers / torch 在 Windows 下因 OpenMP/MKL 多线程竞争触发段错误
# （实测构造 LocalEmbedder 时若不限线程会直接 SIGSEGV 杀掉整个进程，auto 降级路径会踩到）。
# 在模块导入早期设置，确保无论生产/脚本环境都生效。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


# ----------------------------------------------------------------------
# 基类
# ----------------------------------------------------------------------
class BaseEmbedder:
    name = "base"
    dim = 0

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


# ----------------------------------------------------------------------
# 1. 腾讯云 混元 Embedding
# ----------------------------------------------------------------------
class TencentEmbedder(BaseEmbedder):
    name = "tencent"
    dim = 1024

    def __init__(self):
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.hunyuan.v20230901 import hunyuan_client, models
        from config.settings import (
            TENCENT_SECRET_ID,
            TENCENT_SECRET_KEY,
            TENCENT_OCR_REGION,
        )

        if not (TENCENT_SECRET_ID and TENCENT_SECRET_KEY):
            raise RuntimeError("腾讯云凭证未配置，无法使用 Hunyuan Embedding")

        cred = credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
        http_profile = HttpProfile()
        http_profile.endpoint = "hunyuan.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        self._client = hunyuan_client.HunyuanClient(
            cred, TENCENT_OCR_REGION or "ap-guangzhou", client_profile
        )
        self._models = models

    @staticmethod
    def _truncate(text: str, max_chars: int = 3000) -> str:
        # 混元 Embedding 单条输入约 1024 token，中文粗略按字符截断以保安全
        return text[:max_chars]

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        batch_size = 50  # 接口限制 InputList 最多 50 条
        for i in range(0, len(texts), batch_size):
            batch = [self._truncate(t) for t in texts[i : i + batch_size]]
            req = self._models.GetEmbeddingRequest()
            req.InputList = batch
            data = self._call_with_retry(req)
            for item in data:
                vec = getattr(item, "Embedding", None)
                if not vec:
                    raise RuntimeError("腾讯云 Embedding 返回空向量")
                out.append([float(x) for x in vec])
        return out

    def _call_with_retry(self, req, max_retries: int = 4):
        """
        调用腾讯云 Embedding，遇到瞬时网络错误（连接中断/超时/限流）自动退避重试。

        背景：混元 Embedding 接口偶发 ClientNetworkError(RemoteDisconnected) 等瞬时故障，
        若不做重试，一次抖动就会让「全量重建索引」整批失败、/api/chat 检索无结果。
        这里用指数退避（2/4/8/16s）吸收抖动，仅在全部重试后仍失败才抛出。
        """
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return self._client.GetEmbedding(req).Data
            except Exception as e:  # 凭证/产品未开通/网络抖动
                last_err = e
                # 永久错误（配额用尽 / 产品未开通）无需重试，立即失败以便快速降级到 local/tfidf
                err_str = str(e)
                if "ResourcePackExhausted" in err_str or "ResourceUnavailable" in err_str:
                    logger.error(f"❌ 腾讯云 Embedding 永久错误（配额/开通），跳过重试: {e}")
                    raise RuntimeError(f"腾讯云 Embedding 调用失败: {e}")
                if attempt < max_retries:
                    wait = 2 ** attempt  # 2, 4, 8, 16s
                    logger.warning(
                        f"⚠️ 腾讯云 Embedding 第{attempt}次调用失败，{wait}s 后重试: {e}"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"❌ 腾讯云 Embedding 重试{max_retries}次仍失败: {last_err}")
        raise RuntimeError(f"腾讯云 Embedding 调用失败: {last_err}")


# ----------------------------------------------------------------------
# 2. 本地 sentence-transformers
# ----------------------------------------------------------------------
class LocalEmbedder(BaseEmbedder):
    name = "local"
    dim = 768
    _MODEL = "shibing624/text2vec-base-chinese"

    def __init__(self, model_name: str = _MODEL):
        # 香港及海外网络默认使用官方源；仍可通过 HF_ENDPOINT 手动切换镜像。
        os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")
        from sentence_transformers import SentenceTransformer

        # 模型已由部署步骤预下载；运行期禁止联网探测，避免网络波动产生警告。
        self._model = SentenceTransformer(model_name, local_files_only=True)
        self.dim = self._model.get_embedding_dimension()

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vecs = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return vecs.tolist()


# ----------------------------------------------------------------------
# 3. numpy 哈希词袋兜底（永远可用）
# ----------------------------------------------------------------------
class TfidfEmbedder(BaseEmbedder):
    name = "tfidf"

    def __init__(self, dim: int = 2048):
        self.dim = dim

    def _tokens(self, text: str):
        text = (text or "").lower()
        tokens = []
        chars = [c for c in text if "\u4e00" <= c <= "\u9fa5"]
        for i, c in enumerate(chars):
            tokens.append(("c", c))
            if i + 1 < len(chars):
                tokens.append(("b", c + chars[i + 1]))
        for w in re.findall(r"[a-z0-9]+", text):
            tokens.append(("w", w))
        return tokens

    def _vector(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        toks = self._tokens(text)
        if not toks:
            return vec
        cnt = Counter(toks)
        norm = 0.0
        for tok, tf in cnt.items():
            key = f"{tok[0]}:{tok[1]}".encode("utf-8")
            h = int.from_bytes(
                __import__("hashlib").md5(key).digest()[:8], "big"
            ) % self.dim
            w = 1.0 + math.log(tf)
            vec[h] += w
            norm += vec[h] * vec[h]
        if norm > 0:
            s = math.sqrt(norm)
            vec = [v / s for v in vec]
        return vec

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._vector(t) for t in texts]


# ----------------------------------------------------------------------
# 工厂：自动选择/缓存后端
# ----------------------------------------------------------------------
_EMBEDDER: "BaseEmbedder | None" = None


def _build(backend: str) -> BaseEmbedder:
    if backend == "tencent":
        return TencentEmbedder()
    if backend == "local":
        from config.settings import LOCAL_EMBEDDING_MODEL
        return LocalEmbedder(model_name=LOCAL_EMBEDDING_MODEL)
    if backend == "tfidf":
        return TfidfEmbedder()
    raise ValueError(f"未知嵌入后端: {backend}")


def get_embedder() -> BaseEmbedder:
    """返回（并缓存）当前激活的嵌入器；默认本地，失败时降级 TF-IDF。"""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER

    backend = os.environ.get("EMBEDDING_BACKEND", "tfidf").lower()
    if backend == "auto":
        from config.settings import is_tencent_ocr_configured

        candidates = (
            ["tencent", "local", "tfidf"]
            if is_tencent_ocr_configured()
            else ["local", "tfidf"]
        )
    else:
        # 显式指定也保留 tfidf 兜底
        candidates = [backend, "tfidf"]

    last_err = None
    for cand in candidates:
        try:
            e = _build(cand)
            # 探针：真实调用一次，确认可用
            e.embed(["探针_health_check"])
            _EMBEDDER = e
            logger.info(f"✅ 激活嵌入后端: {e.name} (dim={e.dim})")
            return _EMBEDDER
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            logger.warning(f"⚠️ 嵌入后端 {cand} 不可用: {ex}")

    # 极端兜底
    _EMBEDDER = TfidfEmbedder()
    logger.warning(f"⚠️ 所有首选后端均不可用（末因: {last_err}），回退 TF-IDF 兜底")
    return _EMBEDDER


def reset_embedder() -> None:
    """测试/配置变更后清除缓存，下次调用重新解析后端。"""
    global _EMBEDDER
    _EMBEDDER = None
