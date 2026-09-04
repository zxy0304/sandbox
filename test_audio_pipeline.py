"""Offline tests for TTS, delivery planning, and Gemini audio evaluation."""

from pathlib import Path
import tempfile
import unittest

from sandbox.agents.audio_delivery_planner_agent import AudioDeliveryPlannerAgent
from sandbox.agents.audio_evaluator_agent import AudioEvaluatorAgent
from sandbox.agents.tts_agent import TTSAgent


class FakeTTSClient:
    model_name = "fake-tts"
    last_error = ""

    def post_binary(self, endpoint, payload):
        assert endpoint == "audio/speech"
        assert payload["input"] == "你好呀"
        return b"RIFF-fake-wave"


class FakePlannerClient:
    model_name = "fake-planner"
    provider = "fake"
    last_error = ""

    def is_available(self):
        return True

    def prompt(self, filename):
        assert filename == "audio_delivery_planner_prompt.txt"
        return "planner prompt"

    def chat_json(self, messages):
        assert "hidden" not in str(messages)
        return {
            "emotion": "温柔关心",
            "emotion_intensity": 2.5,
            "speaking_rate": "slightly_slow",
            "delivery_style": "像朋友认真倾听后自然接话",
        }


class FakeGeminiAudioClient:
    model_name = "fake-gemini-audio-judge"
    provider = "gemini"
    last_error = ""

    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def generate_json(self, prompt, audio_b64, mime_type, schema):
        self.calls.append((prompt, audio_b64, mime_type, schema))
        assert "温柔关心" in prompt
        assert "你好呀" in prompt
        assert audio_b64
        assert mime_type == "audio/wav"
        return {
            "naturalness": {"score": 4.1, "reason": "开头停顿略显生硬"},
            "emotional_fit": {"score": 3.5, "reason": "情绪方向正确但稍显平淡"},
            "conversational_delivery": {"score": 3.0, "reason": "略有朗读感"},
        }


class AudioPipelineTest(unittest.TestCase):
    def test_planner_uses_only_visible_history(self):
        planner = AudioDeliveryPlannerAgent(
            config={"audio_evaluation": {"planner_history_turns": 1}},
            client=FakePlannerClient(),
        )
        result = planner.generate({
            "visible_history": [
                {"user_message": "旧消息", "assistant_message": "旧回复", "hidden": "不可见"},
                {"user_message": "有点累", "assistant_message": "听起来今天挺辛苦"},
            ],
            "current_user_message": "是啊",
            "assistant_message": "那就先歇一会儿。",
        })
        self.assertEqual("温柔关心", result["emotion"])
        self.assertEqual(2.5, result["emotion_intensity"])
        self.assertEqual("slightly_slow", result["speaking_rate"])
        self.assertEqual("像朋友认真倾听后自然接话", result["delivery_style"])

    def test_tts_planner_and_gemini_audio_evaluation(self):
        config = {
            "tts": {"format": "wav", "voice": "alloy", "endpoint": "audio/speech"},
            "audio_evaluation": {"passes": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "turn_001.wav"
            generated = TTSAgent(config=config, client=FakeTTSClient()).generate({
                "text": "你好呀", "output_path": audio_path,
            })
            self.assertEqual(b"RIFF-fake-wave", Path(generated["path"]).read_bytes())
            client = FakeGeminiAudioClient()
            result = AudioEvaluatorAgent(config=config, client=client).evaluate({
                "audio_path": audio_path,
                "audio_format": "wav",
                "assistant_message": "你好呀",
                "delivery_plan": {
                    "emotion": "温柔关心",
                    "emotion_intensity": 2.5,
                    "speaking_rate": "slightly_slow",
                    "delivery_style": "像朋友自然接话",
                },
            })
        self.assertEqual(1, len(client.calls))
        self.assertEqual(4.0, result["naturalness"]["score"])
        self.assertEqual(3.5, result["emotional_fit"]["score"])
        self.assertEqual(3.0, result["conversational_delivery"]["score"])

    def test_audio_judge_rejects_missing_reason(self):
        class MissingReasonClient(FakeGeminiAudioClient):
            def generate_json(self, prompt, audio_b64, mime_type, schema):
                return {
                    "naturalness": {"score": 4, "reason": ""},
                    "emotional_fit": {"score": 4, "reason": "合适"},
                    "conversational_delivery": {"score": 4, "reason": "自然"},
                }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            path.write_bytes(b"RIFF")
            with self.assertRaisesRegex(ValueError, "reason must be non-empty"):
                AudioEvaluatorAgent(config={}, client=MissingReasonClient()).evaluate({
                    "audio_path": path,
                    "assistant_message": "你好",
                    "delivery_plan": {"emotion": "平静"},
                })
