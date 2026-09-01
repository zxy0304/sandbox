"""Offline tests for TTS and multimodal audio evaluation."""

from pathlib import Path

from sandbox.agents.audio_evaluator_agent import AudioEvaluatorAgent
from sandbox.agents.tts_agent import TTSAgent


class FakeTTSClient:
    model_name = "fake-tts"
    last_error = ""

    def post_binary(self, endpoint, payload):
        assert endpoint == "audio/speech"
        assert payload["input"] == "你好呀"
        return b"RIFF-fake-wave"


class FakeAudioClient:
    model_name = "fake-audio-judge"
    last_error = ""

    def __init__(self):
        self.calls = []

    def chat_json(self, messages):
        self.calls.append(messages)
        assert messages[0]["content"][1]["type"] == "input_audio"
        index = len(self.calls)
        return {
            "audio_naturalness": 80 + index * 10,
            "audio_colloquialness": 70 + index * 10,
            "overall": 75 + index * 10,
            "evidence": ["自然停连"],
            "problems": [],
        }

    def chat_json_stream(self, messages, extra_payload):
        assert messages[0]["content"][1]["input_audio"]["data"].startswith("data:;base64,")
        assert extra_payload == {"modalities": ["text"], "enable_thinking": False}
        return self.chat_json(messages)


class FakeDashScopeTTSClient:
    model_name = "qwen-audio-3.0-tts-flash"
    last_error = ""

    def post_json(self, endpoint, payload):
        assert endpoint == "services/audio/tts/SpeechSynthesizer"
        assert payload["input"]["voice"] == "longanhuan_v3.6"
        return {"output": {"audio": {"url": "https://example.invalid/audio.wav"}}}

    def get_binary(self, url):
        assert url == "https://example.invalid/audio.wav"
        return b"RIFF-qwen-wave"


def test_tts_and_audio_evaluation(tmp_path):
    config = {
        "tts": {"format": "wav", "voice": "alloy", "endpoint": "audio/speech"},
        "audio_evaluation": {"passes": 1},
    }
    audio_path = tmp_path / "turn_001.wav"
    generated = TTSAgent(config=config, client=FakeTTSClient()).generate({
        "text": "你好呀",
        "output_path": audio_path,
    })
    assert Path(generated["path"]).read_bytes() == b"RIFF-fake-wave"

    client = FakeAudioClient()
    result = AudioEvaluatorAgent(config=config, client=client).evaluate({
        "audio_path": audio_path,
        "audio_format": "wav",
        "user_message": "在吗",
        "assistant_message": "你好呀",
    })
    assert len(client.calls) == 1
    assert result["audio_naturalness"] == 90.0
    assert result["audio_colloquialness"] == 80.0
    assert result["overall"] == 85.0
    assert [item["pass"] for item in result["passes"]] == [1]


def test_qwen_protocol_adapters(tmp_path):
    config = {
        "tts": {
            "protocol": "dashscope_tts",
            "format": "wav",
            "voice": "longanhuan_v3.6",
            "endpoint": "services/audio/tts/SpeechSynthesizer",
        },
        "audio_evaluation": {"protocol": "qwen_omni_stream", "passes": 1},
    }
    path = tmp_path / "qwen.wav"
    generated = TTSAgent(config=config, client=FakeDashScopeTTSClient()).generate({
        "text": "你好呀", "output_path": path
    })
    assert Path(generated["path"]).read_bytes() == b"RIFF-qwen-wave"
    result = AudioEvaluatorAgent(config=config, client=FakeAudioClient()).evaluate({
        "audio_path": path,
        "audio_format": "wav",
        "user_message": "在吗",
        "assistant_message": "你好呀",
    })
    assert result["overall"] == 85.0
