"""OpenAI-compatible LLM 客户端。

这个文件把“怎么连模型”从各个 agent 中拆出来：按角色读取配置、
拼接 Chat Completions URL、发送 JSON 请求，并把模型返回解析为文本或 JSON。
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib import error
from urllib import request


class LLMClient:
    """按角色隔离配置的模型客户端。

    每个角色可以有自己的 base_url、model、temperature 和 token 限制；
    客户端只暴露 chat/chat_json，具体 prompt 组装留给各个 agent。
    """
    ROLE_NAMES = [
        "companion",
        "evaluator",
        "empathy_evaluator",
        "naturalness_evaluator",
        "audio_delivery_planner",
        "audio_evaluator",
        "tts",
        "human_presence",
        "user_thinker",
        "user_talker",
    ]

    def __init__(self, config=None, role=None):
        """读取角色配置并缓存请求参数。

        配置合并逻辑在 ``_role_config`` 中完成；这里把字符串、数字和超时值
        预处理成请求时可直接使用的字段。
        """
        config = config or {}
        self.role = self._clean(role or "default")
        self.config = self._role_config(config, self.role)
        self.provider = self.config.get("provider", self.config.get("protocol", "openai_compatible"))
        self.protocol = self.provider
        self.base_url = self._clean(self.config.get("base_url"))
        self.api_key_env = self._clean(self.config.get("api_key_env"))
        self.model_name = self._clean(self.config.get("model_name"))
        self.temperature = self._number(self.config.get("temperature"), 0.3)
        self.max_tokens = int(self._number(self.config.get("max_tokens"), 800))
        self.timeout = int(self._number(self.config.get("timeout"), 60))
        self.json_retries = int(self._number(self.config.get("json_retries"), 1))
        self.retry_backoff_seconds = max(0.0, self._number(self.config.get("retry_backoff_seconds"), 1.0))
        self.last_error = ""
        self.last_content = ""
        self.last_request_seconds = 0.0
        self.last_json_attempts = 0
        self.last_json_retry_count = 0
        self.last_json_failed_contents = []

    def is_available(self):
        """Return whether this role has a model name, base URL, and populated API-key environment variable."""
        return bool(self.api_key() and self.base_url and self.model_name)

    def missing_config_message(self):
        """Report exactly which role-specific LLM config or environment variable is missing."""
        missing = []
        if not self.base_url:
            missing.append(self._config_path("base_url"))
        if not self.model_name:
            missing.append(self._config_path("model_name"))
        if not self.api_key_env:
            missing.append(self._config_path("api_key_env"))
        elif not self.api_key():
            missing.append("environment variable %s" % self.api_key_env)
        if not missing:
            return ""
        return "missing " + ", ".join(missing)

    def api_key(self):
        """Read the configured API key from the environment so secrets never live in source files."""
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "")

    def chat(self, messages, json_mode=False):
        """发送一次 Chat Completions 请求并返回消息正文。

        当 json_mode=True 时，会给兼容 OpenAI 的服务传入 response_format；
        网络、HTTP、响应结构异常都记录到 last_error 并返回 None，由上层 agent
        决定如何把错误呈现给 runner。
        """
        self.last_error = ""
        self.last_content = ""
        if not self.is_available():
            self.last_error = "missing api key, base_url, or model_name"
            return None
        if self.provider != "openai_compatible":
            self.last_error = "unsupported provider: %s" % self.provider
            return None

        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {
                "type": "json_object",
            }

        started_at = time.time()
        try:
            response = self._post_json(self._chat_url(), payload)
        except Exception as exc:
            self.last_request_seconds = round(time.time() - started_at, 3)
            self.last_error = str(exc)
            return None
        self.last_request_seconds = round(time.time() - started_at, 3)

        try:
            content = self._response_content(response)
        except (AttributeError, IndexError):
            self.last_error = "provider response did not contain choices[0].message.content"
            return None
        if content:
            self.last_content = content
            return content

        self.last_error = "provider returned empty content: %s" % self._response_summary(response)
        return None

    def extract_string_field(self, content, field_name):
        """Recover a JSON string field from otherwise incomplete model output."""
        text = str(content or "")
        marker = '"%s"' % field_name
        marker_index = text.find(marker)
        if marker_index < 0:
            return ""

        colon_index = text.find(":", marker_index + len(marker))
        if colon_index < 0:
            return ""

        value_index = colon_index + 1
        while value_index < len(text) and text[value_index].isspace():
            value_index += 1
        if value_index >= len(text) or text[value_index] != '"':
            return ""

        try:
            value, _ = json.JSONDecoder().raw_decode(text[value_index:])
        except ValueError:
            return self._scan_partial_json_string(text[value_index:])
        if isinstance(value, str):
            return value.strip()
        return ""

    def chat_json(self, messages):
        """Call chat in JSON mode and parse the model content into a dictionary."""
        attempts = max(1, self.json_retries + 1)
        retry_messages = messages
        last_content = ""
        self.last_json_attempts = 0
        self.last_json_retry_count = 0
        self.last_json_failed_contents = []

        for attempt in range(attempts):
            self.last_json_attempts = attempt + 1
            content = self.chat(retry_messages, json_mode=True)
            if not content:
                if not self.last_error:
                    self.last_error = "model returned empty content"
                if attempt + 1 < attempts:
                    self.last_json_retry_count += 1
                    self._log_json_retry(attempt + 1, attempts, self.last_error)
                    # A transport/provider failure produced no answer to repair. Retry
                    # the original request after a small backoff instead of claiming
                    # that the model returned malformed JSON.
                    retry_messages = messages
                    if self.retry_backoff_seconds:
                        time.sleep(self.retry_backoff_seconds * (2 ** attempt))
                    continue
                if last_content:
                    self.last_content = last_content
                return None

            last_content = content
            parsed = self.parse_json(content)
            if isinstance(parsed, dict):
                self.last_error = ""
                return parsed

            self.last_json_failed_contents.append(content)
            self.last_error = "could not parse JSON from model output: %s" % self._content_snippet(content)
            if attempt + 1 < attempts:
                self.last_json_retry_count += 1
                self._log_json_retry(attempt + 1, attempts, self.last_error)
                retry_messages = self._json_retry_messages(messages, content, self.last_error)

        if last_content:
            self.last_content = last_content
            self.last_error = "could not parse JSON from model output: %s" % self._content_snippet(last_content)
        return None


    def post_binary(self, endpoint, payload):
        """POST JSON to an OpenAI-compatible endpoint and return raw bytes."""
        self.last_error = ""
        if not self.is_available():
            self.last_error = self.missing_config_message()
            return None
        url = self.base_url.rstrip("/") + "/" + str(endpoint).lstrip("/")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer %s" % self.api_key())
        started_at = time.time()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = response.read()
        except Exception as exc:
            self.last_request_seconds = round(time.time() - started_at, 3)
            self.last_error = str(exc)
            return None
        self.last_request_seconds = round(time.time() - started_at, 3)
        if not data:
            self.last_error = "provider returned empty binary response"
            return None
        return data

    def post_json(self, endpoint, payload):
        """POST JSON to a role-relative endpoint and return the decoded object."""
        if not self.is_available():
            self.last_error = self.missing_config_message()
            return None
        url = self.base_url.rstrip("/") + "/" + str(endpoint).lstrip("/")
        try:
            return self._post_json(url, payload)
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def get_binary(self, url):
        """Download a provider-created artifact from its temporary URL."""
        try:
            with request.urlopen(str(url), timeout=self.timeout) as response:
                return response.read()
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def chat_json_stream(self, messages, extra_payload=None):
        """Consume an OpenAI-compatible SSE response and parse its text as JSON."""
        if not self.is_available():
            self.last_error = self.missing_config_message()
            return None
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        payload.update(extra_payload or {})
        req = request.Request(
            self._chat_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer %s" % self.api_key())
        pieces = []
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    choices = event.get("choices", [])
                    if choices:
                        content = choices[0].get("delta", {}).get("content", "")
                        if isinstance(content, str):
                            pieces.append(content)
        except Exception as exc:
            self.last_error = str(exc)
            return None
        content = "".join(pieces).strip()
        self.last_content = content
        parsed = self.parse_json(content)
        if not isinstance(parsed, dict):
            self.last_error = "could not parse JSON from streamed model output: %s" % self._content_snippet(content)
            return None
        return parsed

    def _log_json_retry(self, attempt, attempts, reason):
        """Print a compact warning when JSON parsing forces another model request."""
        print(
            "[llm-json-retry] role=%s attempt=%s/%s reason=%s"
            % (self.role, attempt, attempts, self._content_snippet(reason, 180)),
            file=sys.stderr,
        )

    def _response_content(self, response):
        """Extract assistant text from common OpenAI-compatible response shapes."""
        choice = response.get("choices", [])[0]
        message = choice.get("message", {})
        content = message.get("content", "")

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "".join(parts).strip()
        if content is None:
            return str(choice.get("text", "") or "").strip()
        return str(content).strip()

    def _response_summary(self, response):
        """Summarize non-secret provider metadata for empty-content diagnostics."""
        try:
            choice = response.get("choices", [])[0]
            message = choice.get("message", {})
            summary = {
                "finish_reason": choice.get("finish_reason", ""),
                "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
                "model": response.get("model", ""),
            }
            usage = response.get("usage", {})
            if isinstance(usage, dict):
                summary["usage"] = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            return json.dumps(summary, ensure_ascii=False, default=str)
        except Exception:
            return self._content_snippet(response)

    def _scan_partial_json_string(self, text):
        """Best-effort decode for a quoted string when the rest of the JSON is broken."""
        if not text.startswith('"'):
            return ""
        escaped = False
        pieces = []
        for character in text[1:]:
            if escaped:
                pieces.append("\\" + character)
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                try:
                    return json.loads('"' + "".join(pieces) + '"').strip()
                except ValueError:
                    return "".join(pieces).strip()
            pieces.append(character)
        return "".join(pieces).strip()

    def parse_json(self, content):
        """把模型输出解析成 JSON object。

        先尝试直接 json.loads；如果模型在 JSON 外包了说明文字，就截取第一个
        ``{`` 到最后一个 ``}`` 之间的内容再解析，提升对轻微格式漂移的容错。
        """
        text = str(content or "").strip()
        if not text:
            return None
        for candidate in self._json_candidates(text):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass

        decoded = self._decode_first_json_object(text)
        if isinstance(decoded, dict):
            return decoded
        return None

    def _json_candidates(self, text):
        """Return likely JSON object strings from direct or fenced model output."""
        candidates = [text]
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                candidates.append("\n".join(lines[1:-1]).strip())

        extracted = self._extract_json_object(text)
        if extracted:
            candidates.append(extracted)
        return candidates

    def _decode_first_json_object(self, text):
        """Scan prose/code-fenced output and decode the first complete JSON object."""
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _json_retry_messages(self, messages, content, reason):
        """Ask the same model to restate the answer as a strict JSON object."""
        retry_messages = list(messages)
        if content:
            retry_messages.append({
                "role": "assistant",
                "content": str(content)[:2000],
            })
        retry_messages.append({
            "role": "user",
            "content": (
                "Your previous response could not be parsed as a JSON object (%s). "
                "Return the answer again as exactly one valid JSON object. "
                "Use double quotes for every key and string value. "
                "Do not include Markdown, code fences, comments, explanations, or text outside the object."
            ) % reason,
        })
        return retry_messages

    def _content_snippet(self, content, limit=240):
        """Format a short one-line model-output sample for error messages."""
        text = " ".join(str(content or "").split())
        if len(text) > limit:
            text = text[:limit] + "..."
        return repr(text)

    def prompt(self, filename):
        """Load a role prompt file from sandbox/prompts using UTF-8 text."""
        path = Path(__file__).resolve().parents[1] / "prompts" / filename
        with path.open("r", encoding="utf-8") as handle:
            return handle.read().strip()

    def _post_json(self, url, payload):
        """执行底层 HTTP POST 并返回解析后的 JSON。

        这里集中设置 Authorization 和 Content-Type；HTTPError/URLError 会被转换成
        带细节的 RuntimeError，方便上层把 provider 错误写入 last_error。
        """
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.api_key(),
        }
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
                return json.loads(text)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("llm http error %s: %s" % (exc.code, detail))
        except error.URLError as exc:
            raise RuntimeError("llm url error: %s" % exc)

    def _chat_url(self):
        """Build the final /chat/completions URL from either a base URL or full endpoint."""
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _extract_json_object(self, text):
        """Recover a JSON object from model text by slicing between the first opening and last closing brace."""
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return ""
        return text[start:end + 1]

    def _role_config(self, config, role):
        """合并全局默认配置和角色专属配置。

        新格式使用 ``llm.defaults`` 加 ``llm.<role>``；如果发现旧版扁平字段，
        会剔除角色块后作为 legacy 配置返回，保证旧配置还能跑。
        """
        llm_config = config.get("llm", config)
        if not isinstance(llm_config, dict):
            return {}

        defaults = {}
        if isinstance(llm_config.get("defaults"), dict):
            defaults.update(llm_config.get("defaults", {}))

        role_config = llm_config.get(role)
        if isinstance(role_config, dict):
            merged = dict(defaults)
            merged.update(role_config)
            return merged

        if self._looks_like_legacy_config(llm_config):
            legacy = dict(llm_config)
            for key in ["defaults"] + self.ROLE_NAMES:
                legacy.pop(key, None)
            return legacy

        return dict(defaults)

    def _looks_like_legacy_config(self, llm_config):
        """判断配置是否是旧版扁平结构。

        只要顶层直接出现 base_url/api_key_env/model_name，就认为调用方传的是
        旧格式，而不是按角色拆分的新格式。
        """
        for key in ["base_url", "api_key_env", "model_name"]:
            if key in llm_config:
                return True
        return False

    def _config_path(self, key):
        """Build a human-readable config path for role-specific error messages."""
        if self.role and self.role != "default":
            return "llm.%s.%s" % (self.role, key)
        return "llm.%s" % key

    def _clean(self, value):
        """Normalize optional config values into stripped strings."""
        if value is None:
            return ""
        return str(value).strip()

    def _number(self, value, default):
        """把配置值转换成 float，失败时返回调用方给定的默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
