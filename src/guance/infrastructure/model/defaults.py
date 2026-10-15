"""Initial source discovery only; provider compatibility never acts as fallback."""
from pathlib import Path


def initial_model_source(paths: dict[str, Path]) -> Path:
    sources = (
        paths["models_dir"] / "default" / "manifest.json",
        paths["root_dir"] / "data" / "models" / "default" / "manifest.json",
        paths["models_dir"] / "best.npz",
        paths["root_dir"] / "data" / "profiles" / "tencent_daguandan" / "models" / "best.npz",
    )
    return next((p for p in sources if p.exists()), sources[0])
