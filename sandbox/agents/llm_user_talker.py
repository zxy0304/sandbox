"""可见用户发言生成器。

UserTalker 接收 UserThinker 的隐藏心理状态，但只输出用户真实会说出口的短句，
用来模拟对话中 companion 能看到的用户消息。
"""

import json

from sandbox.agents.base_agent import BaseAgent
from sandbox.agents.llm_client import LLMClient


class LLMUserTalker(BaseAgent):
    """把 UserThinker 的简短表达意图翻译成自然发言。

    它只看得到可见历史、UserThinker 的私有状态和当前流程指令，输出必须限制在
    user_message，避免把隐藏动机或 case 说明原文暴露给 companion。
    """
    def __init__(self, name=None, config=None, client=None):
        """初始化 user_talker 角色客户端。

        默认读取 ``llm.user_talker`` 配置；测试时可传入假 client 来控制模型输出。
        """
        BaseAgent.__init__(self, name=name, config=config)
        self.client = client or LLMClient(config or {}, role="user_talker")
        self.last_schema_failure = {}

    def generate(self, context):
        """生成本轮用户可见消息。

        模型输出必须是 JSON 且包含非空 user_message；其他辅助字段仅由 prompt
        约束模型思考，不进入 runner 的公开对话文本。
        """
        if not self.client.is_available():
            raise ValueError("LLM user talker is not configured: %s" % self.client.missing_config_message())

        messages = self.build_messages(context)
        data = self.client.chat_json(messages)
        if not isinstance(data, dict):
            recovered_message = self._recover_from_raw_output()
            if recovered_message:
                return recovered_message
            self._remember_schema_failure(data, "invalid_json")
            raise ValueError("LLM user talker returned invalid JSON: %s" % self.client.last_error)

        message = self._message_from_data(data)
        if not message:
            first_content = self.client.last_content
            repaired = self.client.chat_json(self._repair_messages(messages, data))
            message = self._message_from_data(repaired)
            if not message:
                self._remember_schema_failure(
                    data,
                    "missing_user_message_after_repair",
                    first_content=first_content,
                    repair_output=self.client.last_content,
                )
                raise ValueError("LLM user talker response is missing user_message after format repair.")
        self.last_schema_failure = {}
        return message

    def _message_from_data(self, data):
        """Accept the required field plus a small set of unambiguous provider aliases."""
        if not isinstance(data, dict):
            return ""
        containers = [data]
        for key in ["output", "result", "data"]:
            if isinstance(data.get(key), dict):
                containers.append(data.get(key))
        for container in containers:
            for key in ["user_message", "message", "response", "text", "content"]:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _recover_from_raw_output(self):
        candidates = list(getattr(self.client, "last_json_failed_contents", []) or [])
        if self.client.last_content:
            candidates.append(self.client.last_content)
        for content in candidates:
            for key in ["user_message", "message", "response", "text", "content"]:
                message = self.client.extract_string_field(content, key)
                if message:
                    return message
        return ""

    def _repair_messages(self, messages, data):
        repair = list(messages)
        repair.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False, default=str)[:3000]})
        repair.append({
            "role": "user",
            "content": (
                "格式错误：把刚才要说给陪伴者的用户话语原样放入 user_message。"
                "只返回一个 JSON object，且只能是 {\"user_message\": \"非空字符串\"}。"
                "不要解释、不要增加字段、不要输出 Markdown。"
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

    def build_messages(self, context):
        """构造 UserTalker 的模型输入。

        payload 不包含完整 case card、evaluation_rubric、hard_fail 或 director_plan。
        Talker 只负责将 Thinker 控制好透露程度的状态翻译成可见发言。
        """
        private_state = context.get("user_private_state", {})
        case = context.get("case", {}) if isinstance(context.get("case"), dict) else {}
        persona = case.get("P", {}) if isinstance(case.get("P"), dict) else {}

        payload = {
            "visible_dialogue_history": self._visible_history(context.get("history", [])),
            "intent": private_state.get("intent", {}),
            "persona_style": {
                "communication_preference": persona.get("communication_preference", []),
                "playful_style": persona.get("playful_style", ""),
            },
        }
        return [
            {
                "role": "system",
                "content": self.client.prompt("user_talker_prompt.txt"),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            },
        ]

    def _visible_history(self, history):
        """把完整 turn record 压缩成用户和助手都实际说过的话。"""
        visible = []
        for turn in history:
            visible.append({
                "turn_id": turn.get("turn_id"),
                "user_message": turn.get("user_message", ""),
                "assistant_message": turn.get("assistant_message", ""),
            })
        return visible

