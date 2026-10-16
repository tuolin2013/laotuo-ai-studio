"""加载 config/categories 与 config/platforms 下的 JSON 配置。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.schemas.config import CategoryConfig, PlatformConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigError(ValueError):
    pass


def _load_dir(directory: Path, model, id_field: str) -> dict:
    result = {}
    for path in sorted(directory.glob("*.json")):
        try:
            obj = model.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError) as e:
            raise ConfigError(f"配置文件 {path.name} 格式错误：{e}") from e
        key = getattr(obj, id_field)
        if key != path.stem:
            raise ConfigError(f"配置文件 {path.name} 的 {id_field}={key!r} 应与文件名一致")
        result[key] = obj
    return result


def load_categories(config_dir: Path = CONFIG_DIR) -> dict[str, CategoryConfig]:
    return _load_dir(config_dir / "categories", CategoryConfig, "category_id")


def load_platforms(config_dir: Path = CONFIG_DIR) -> dict[str, PlatformConfig]:
    return _load_dir(config_dir / "platforms", PlatformConfig, "platform_id")
