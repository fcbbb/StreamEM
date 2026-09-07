from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

import numpy as np


class Encoder(Protocol):
    def encode(self, texts: Any, **kwargs: Any) -> Any: ...


class HashEncoder:
    """Deterministic dependency-light encoder for tests and smoke runs."""

    dimension = 384

    @staticmethod
    def _tokens(text: str) -> list[str]:
        lowered = str(text).casefold()
        tokens = re.findall(r"[a-z0-9_']+|[\u3400-\u9fff]", lowered)
        cjk = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
        tokens.extend(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
        return tokens

    def encode(self, texts: Any, **_: Any) -> np.ndarray:
        scalar = isinstance(texts, str)
        values = [texts] if scalar else list(texts)
        output = np.zeros((len(values), self.dimension), dtype=np.float32)
        for row, text in enumerate(values):
            for token in self._tokens(str(text)):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimension
                output[row, index] += 1.0 if digest[4] & 1 else -1.0
        return output[0] if scalar else output


def load_encoder(model_name: str = "hash", device: str = "cpu") -> Encoder:
    if not model_name or model_name.casefold() == "hash":
        return HashEncoder()
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is required for a non-hash encoder; "
            "install the project's graph optional dependencies"
        ) from exc
    return SentenceTransformer(model_name, device=device)


def unit_vector(value: Any) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector

