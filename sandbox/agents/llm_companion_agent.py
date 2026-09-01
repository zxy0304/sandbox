"""被测陪伴代理。

这个文件负责把“用户可见历史 + 当前用户发言”交给被测 LLM，
并把模型 JSON 输出标准化成 runner 可以记录的 assistant_message/metadata。
"""

import json
import re

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class LLMCompanionAgent(BaseAgent):
    """被测对象，只能读取可见对话信息。

    这里刻意过滤掉 case 的隐藏需求、隐藏恐惧、评分标准等字段，确保评测时
    companion agent 不会偷看模拟器和 evaluator 的内部信息。
    """
    def __init__(self, name=None, config=None, client=None):
        """初始化陪伴代理和 companion 角色客户端。

        client 可注入，便于测试时传入假客户端；生产路径默认使用 LLMClient
        从配置中读取 companion 角色的模型参数。
        """
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="companion")
        self.last_schema_failure = {}

    def generate(self, context):
        """生成助手回复。

        先校验可见输入，再请求模型 JSON；最后要求 assistant_message 非空，
        并补充来源元数据，供报告确认它没有使用隐藏字段。
        """
        self.validate_context(context, ["visible_history", "current_user_message"])
        if not self.client.is_available():
            raise ValueError("LLM companion is not configured: %s" % self.client.missing_config_message())

        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            recovered_message = self._recover_assistant_message_from_invalid_json()
            if recovered_message:
                assistant_message = recovered_message
                metadata = {
                    "recovered_from_invalid_json": True,
                    "json_error": self.client.last_error,
                }
            else:
                self._remember_schema_failure(data, "invalid_json")
                raise ValueError("LLM companion returned invalid JSON: %s" % self.client.last_error)
        else:
            assistant_message = self._message_from_data(data)
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["companion_output_format"] = "json"

        if not assistant_message:
            first_content = self.client.last_content
            repaired = self.client.chat_json(self._repair_messages(messages, data))
            assistant_message = self._message_from_data(repaired)
            if isinstance(repaired, dict) and isinstance(repaired.get("metadata"), dict):
                metadata.update(repaired.get("metadata"))
            if not assistant_message:
                self._remember_schema_failure(
                    data,
                    "missing_assistant_message_after_repair",
                    first_content=first_content,
                    repair_output=self.client.last_content,
                )
                raise ValueError("LLM companion response is missing assistant_message after format repair.")
            metadata["recovered_by_format_repair"] = True
        sanitized_message = self._sanitize_message(assistant_message)
        if sanitized_message != assistant_message:
            metadata["sanitized_output"] = True
            assistant_message = sanitized_message
        self.last_schema_failure = {}

        metadata.update({
            "agent_type": "llm_companion",
            "provider": self.client.provider,
            "model_name": self.client.model_name,
            "used_hidden_state": False,
            "used_private_fields": False,
        })
        return {
            "assistant_message": assistant_message,
            "metadata": metadata,
        }

    def _message_from_data(self, data):
        if not isinstance(data, dict):
            return ""
        containers = [data]
        for key in ["output", "result", "data"]:
            if isinstance(data.get(key), dict):
                containers.append(data.get(key))
        for container in containers:
            for key in ["assistant_message", "message", "response", "reply", "text", "content"]:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _repair_messages(self, messages, data):
        repair = list(messages)
        repair.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False, default=str)[:3000]})
        repair.append({
            "role": "user",
            "content": (
                "格式错误：把刚才准备回复用户的话原样放入 assistant_message。"
                "只返回一个 JSON object，必须包含非空 assistant_message；metadata 可以保留。"
                "不要解释、不要输出 Markdown 或 JSON 之外的文字。"
            ),
        })
        return repair

    def _remember_schema_failure(self, data, reason, first_content="", repair_output=""):
        self.last_schema_failure = {
            "reason": reason,
            "parsed_output": data,
            "first_raw_output": first_content or self.client.last_content,
            "repair_raw_output": repair_output,
            "client_error": self.client.last_error,
            "json_failed_contents": list(getattr(self.client, "last_json_failed_contents", []) or []),
        }

    def _recover_assistant_message_from_invalid_json(self):
        """Recover assistant_message from malformed provider output when possible."""
        candidates = []
        candidates.extend(getattr(self.client, "last_json_failed_contents", []) or [])
        if self.client.last_content:
            candidates.append(self.client.last_content)

        for content in candidates:
            for key in ["assistant_message", "message", "response", "reply", "text", "content"]:
                message = self.client.extract_string_field(content, key)
                if message:
                    return message
        return ""

    def _sanitize_message(self, text):
        """Remove provider/model artifact characters while preserving the reply wording."""
        cleaned = str(text or "")
        cleaned = cleaned.replace("\ufffd", "")
        cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", cleaned)
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
        return cleaned.strip()

    def build_messages(self, context):
        """组装发送给模型的 system/user messages。

        system message 来自 companion_prompt.txt；user message 是过滤后的 JSON
        payload，保证传给模型的上下文结构稳定且可审计。
        """
        safe_payload = self._visible_payload(context)
        return [
            {
                "role": "system",
                "content": self.client.prompt("companion_prompt.txt"),
            },
            {
                "role": "user",
                "content": json.dumps(safe_payload, ensure_ascii=False, default=str),
            },
        ]

    def _visible_payload(self, context):
        """只保留 companion 允许看到的字段。

        optional_memory 也只透传 turn_id/max_turns/memory_scope 这类运行信息，
        避免把模拟器隐藏状态或评分数据误传给被测模型。
        """
        optional_memory = context.get("optional_memory", {}) or {}
        safe_memory = {}
        if "memory_scope" in optional_memory:
            safe_memory["memory_scope"] = optional_memory.get("memory_scope")

        return {
            "visible_history": context.get("visible_history", []),
            "current_user_message": context.get("current_user_message", ""),
            "optional_memory": safe_memory,
        }
