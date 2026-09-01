"""Case Card 加载器。

这个文件负责从 data/cases 或用户传入路径读取 YAML，
并在 episode 开始前调用 schema 校验，确保缺字段或格式错误尽早失败。
"""

from pathlib import Path

from sandbox.schemas import validate_case
from sandbox.utils.yaml_utils import load_yaml


def project_root():
    """Return the emotional_sandbox directory by walking up from this source file."""
    return Path(__file__).resolve().parents[1]


def default_cases_dir():
    """Return the default data/cases directory used when no case directory is supplied."""
    return project_root() / "data" / "cases"


def list_case_files(cases_dir=None):
    """解析 case 目录并返回排序后的 YAML 文件列表。

    目录不存在时返回空列表而不是抛错，便于 --list-cases 和批量运行给出友好提示。
    """
    directory = resolve_path(cases_dir) if cases_dir else default_cases_dir()
    if not directory.exists():
        return []
    return sorted(directory.glob("*.yaml"))


def load_case(case_ref):
    """加载单个 case。

    case_ref 可以是完整路径，也可以是 case_id；后者会自动补 .yaml 并在默认
    data/cases 目录查找。读取后会校验结构并写入 _source_path 供报错/报告使用。
    """
    root = project_root()
    candidate = resolve_path(case_ref)

    if candidate.exists():
        path = candidate
    else:
        name = str(case_ref)
        if not name.endswith(".yaml") and not name.endswith(".yml"):
            name = name + ".yaml"
        path = root / "data" / "cases" / name

    if not path.exists():
        raise FileNotFoundError("Cannot find case: %s" % case_ref)

    case_data = load_yaml(path)
    case_data = validate_case_card(case_data, path)
    case_data["_source_path"] = str(path)
    return case_data


def resolve_path(path):
    """解析用户传入路径。

    绝对路径或当前目录下已存在的路径原样使用；否则尝试相对 project_root 解析，
    兼容从项目根目录或其他工作目录启动 CLI。
    """
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    project_candidate = project_root() / candidate
    if project_candidate.exists():
        return project_candidate
    return candidate


def validate_case_card(case_data, source):
    """包装 schema 校验错误，附带出错 YAML 文件路径。"""
    try:
        return validate_case(case_data, source)
    except ValueError as exc:
        message = [
            "Invalid Case Card: %s" % source,
            str(exc),
            "Please update the YAML file so every required D/P/C/S, initial_state, director_plan, and evaluation_rubric field is present.",
        ]
        raise ValueError("\n".join(message))


def load_all_cases(cases_dir=None):
    """Load and validate every YAML case from a directory in stable sorted order."""
    cases = []
    for path in list_case_files(cases_dir):
        cases.append(load_case(path))
    return cases
