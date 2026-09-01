"""YAML 读写工具。

优先使用 PyYAML；如果依赖缺失，则用本文件内的简易 parser/dumper 支撑项目
当前使用的 YAML 子集，保证基础 case/config 仍然能读取。
"""

from pathlib import Path


def _yaml_module():
    """Import PyYAML lazily so the project can still run with the built-in parser when PyYAML is absent."""
    try:
        import yaml
    except ImportError as exc:
        return None
    return yaml


def load_yaml(path):
    """Read YAML with PyYAML when available, otherwise use the local simple parser."""
    yaml = _yaml_module()
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as handle:
        content = handle.read()
    if yaml:
        data = yaml.safe_load(content)
    else:
        data = simple_load_yaml(content)
    if data is None:
        return {}
    return data


def save_yaml(path, data):
    """Write YAML with PyYAML when available, otherwise use the local simple dumper."""
    yaml = _yaml_module()
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as handle:
        if yaml:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
        else:
            handle.write(simple_dump_yaml(data))


def simple_load_yaml(content):
    """解析项目当前使用的简化 YAML 子集。

    实现方式是先丢弃空行/注释，记录每行缩进和内容，再按缩进递归解析 mapping
    或 list；不支持 tab 缩进和复杂 YAML 特性。
    """
    content = _join_multiline_quoted_scalars(content)
    items = []
    for raw_line in content.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise ValueError("Tabs are not supported by the fallback YAML parser.")
        items.append((indent, raw_line.strip()))

    if not items:
        return {}

    data, index = _parse_block(items, 0, items[0][0])
    if index != len(items):
        raise ValueError("Could not parse all YAML content with fallback parser.")
    return data


def _join_multiline_quoted_scalars(content):
    """Collapse simple single/double quoted multiline scalar values into one line.

    This keeps the fallback parser useful for migrated case cards that rely on
    standard YAML multiline quoted strings, without trying to implement the full
    YAML grammar.
    """
    lines = []
    buffer = []
    quote = ""

    for raw_line in content.splitlines():
        if buffer:
            stripped = raw_line.strip()
            if stripped:
                buffer.append(stripped)
            if quote and _has_odd_quote_count(stripped, quote):
                lines.append(" ".join(buffer))
                buffer = []
                quote = ""
            continue

        opening_quote = _opening_unclosed_quote(raw_line)
        if opening_quote:
            buffer = [raw_line.rstrip()]
            quote = opening_quote
        else:
            lines.append(raw_line)

    if buffer:
        lines.append(" ".join(buffer))

    return "\n".join(lines)


def _opening_unclosed_quote(text):
    """Return the quote character if a line opens but does not close a scalar."""
    for quote in ["'", '"']:
        if _has_odd_quote_count(text, quote):
            return quote
    return ""


def _has_odd_quote_count(text, quote):
    """Count non-escaped quote characters for fallback multiline detection."""
    count = 0
    escaped = False
    for character in str(text):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == quote:
            count += 1
    return count % 2 == 1


def _parse_block(items, index, indent):
    """根据当前行前缀决定解析 mapping 还是 list。"""
    if index >= len(items):
        return {}, index

    current_indent, current_text = items[index]
    if current_indent < indent:
        return {}, index

    if current_text.startswith("- "):
        return _parse_list(items, index, indent)
    return _parse_mapping(items, index, indent)


def _parse_mapping(items, index, indent):
    """解析同一缩进层级下的 key/value 映射。

    value 为空时读取下一层缩进作为子块；value 非空时直接按标量解析。
    """
    result = {}

    while index < len(items):
        current_indent, text = items[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError("Unexpected nested YAML line: %s" % text)
        if text.startswith("- "):
            break
        if ":" not in text:
            raise ValueError("Expected key and value separated by colon: %s" % text)

        key, value = text.split(":", 1)
        key = key.strip()
        value = value.strip()
        index += 1

        if value:
            result[key] = _parse_scalar(value)
        else:
            if index < len(items) and items[index][0] == current_indent and items[index][1].startswith("- "):
                child, index = _parse_list(items, index, current_indent)
                result[key] = child
                continue
            if index < len(items) and items[index][0] > current_indent:
                child_indent = items[index][0]
                child, index = _parse_block(items, index, child_indent)
                result[key] = child
            else:
                result[key] = {}

    return result, index


def _parse_list(items, index, indent):
    """解析同一缩进层级下的 YAML 列表。

    ``- value`` 会作为标量；单独的 ``-`` 会继续读取下一层缩进作为嵌套结构。
    """
    result = []

    while index < len(items):
        current_indent, text = items[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError("Unexpected nested YAML list line: %s" % text)
        if not text.startswith("- "):
            break

        value = text[2:].strip()
        index += 1

        if value:
            result.append(_parse_scalar(value))
        else:
            if index < len(items) and items[index][0] > current_indent:
                child_indent = items[index][0]
                child, index = _parse_block(items, index, child_indent)
                result.append(child)
            else:
                result.append(None)

    return result, index


def _parse_scalar(value):
    """把 YAML 标量文本转换成 Python 的 None/bool/int/float/str。"""
    if value in ["null", "Null", "NULL", "~"]:
        return None
    if value in ["true", "True", "TRUE"]:
        return True
    if value in ["false", "False", "FALSE"]:
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def simple_dump_yaml(data, indent=0):
    """把 dict/list/scalar 写成简化 YAML 文本。"""
    lines = []
    _append_yaml_lines(lines, data, indent)
    return "\n".join(lines) + "\n"


def _append_yaml_lines(lines, data, indent):
    """Recursively serialize dictionaries and lists into indented YAML lines."""
    prefix = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append("%s%s:" % (prefix, key))
                _append_yaml_lines(lines, value, indent + 2)
            else:
                lines.append("%s%s: %s" % (prefix, key, _format_scalar(value)))
    elif isinstance(data, list):
        for value in data:
            if isinstance(value, (dict, list)):
                lines.append("%s-" % prefix)
                _append_yaml_lines(lines, value, indent + 2)
            else:
                lines.append("%s- %s" % (prefix, _format_scalar(value)))
    else:
        lines.append("%s%s" % (prefix, _format_scalar(data)))


def _format_scalar(value):
    """把 Python 标量转换成简化 YAML 可写出的文本。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)
