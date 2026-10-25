"""本地向量模型的缓存探测。

运行时禁止隐式联网下载：只有在显式准备步骤中才允许下载，且 revision 必须精确。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["model_is_cached", "huggingface_cache_dir", "describe_cache"]


def huggingface_cache_dir(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        return cache_dir
    env = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if env:
        base = Path(env)
        return base if base.name == "hub" else base / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def describe_cache(model_id: str, cache_dir: Path | None = None) -> dict[str, object]:
    root = huggingface_cache_dir(cache_dir)
    folder = root / ("models--" + model_id.replace("/", "--"))
    snapshots = sorted(path.name for path in (folder / "snapshots").glob("*")) if folder.exists() else []
    return {
        "cache_root": str(root),
        "model_folder": str(folder),
        "exists": folder.exists(),
        "snapshots": snapshots,
    }


def model_is_cached(model_id: str, revision: str, cache_dir: Path | None = None) -> bool:
    """缓存中是否存在该精确 revision 的快照。"""
    if not revision:
        return False
    info = describe_cache(model_id, cache_dir)
    return revision in info["snapshots"]  # type: ignore[operator]
