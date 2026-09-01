"""JSON 工具函数。

集中处理 UTF-8、缩进和父目录创建，避免报告写入和未来配置读取时重复样板代码。
"""

import json
from pathlib import Path


def save_json(path, data):
    """Create parent directories and write JSON with UTF-8, indentation, and stable key order."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def load_json(path):
    """Read a UTF-8 JSON file and return the parsed Python value."""
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
