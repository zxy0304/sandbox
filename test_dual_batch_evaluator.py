import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sandbox.agents.dual_batch_evaluator_agent import DualBatchEvaluatorAgent
from sandbox.main import run_report_evaluation
from sandbox.report_writer import ReportWriter
from sandbox.utils.json_utils import save_json
from scripts.run_evaluator_stability import summarize


class DualBatchEvaluatorScoringTest(unittest.TestCase):
    def setUp(self):
        self.agent = DualBatchEvaluatorAgent(config={})

    def test_integer_ratings_gain_gradient_when_aggregated(self):
        checks = {
            "a": [{"rating": 3}],
            "b": [{"rating": 4}],
        }
        self.assertEqual(3.5, self.agent._combined_check_score(checks, ["a", "b"], 0))

    def test_rating_is_clamped(self):
        self.assertEqual(5.0, self.agent._check_rating({"rating": 8}))
        self.assertEqual(1.0, self.agent._check_rating({"rating": -4}))

    def test_legacy_label_only_output_still_works(self):
        checks = {
            "a": [{"result": "pass"}],
            "b": [{"result": "partial"}],
        }
        self.assertEqual(3.5, self.agent._combined_check_score(checks, ["a", "b"], 0))

    def test_compact_check_objects_are_aggregated(self):
        checks = {
            "a": {"rating": 4, "evidence": "a"},
            "b": {"rating": 3, "evidence": "b"},
        }
        self.assertEqual(3.5, self.agent._combined_check_score(checks, ["a", "b"], 0))

    def test_result_label_is_derived_from_rating(self):
        checks = {"a": {"rating": 3, "evidence": "mixed"}}
        self.assertEqual("acceptable", self.agent._checks_with_results(checks)["a"]["result"])

    def test_exact_echo_guard_detects_normalized_copy(self):
        self.assertTrue(self.agent._is_exact_echo("海盐牛角包！", " 海盐牛角包！\n"))
        self.assertFalse(self.agent._is_exact_echo("海盐牛角包！", "听着就很香。"))

    def test_weighted_turn_and_episode_aggregation(self):
        turn = {"turn_id": 1, "user_message": "好消息", "assistant_message": "真替你开心"}
        empathy_checks = {
            "emotional_attunement": {"rating": 4}, "contextual_grounding": {"rating": 4},
            "conversation_fit": {"rating": 4}, "continuation_affordance": {"rating": 3},
        }
        natural_checks = {
            "spoken_immediacy": {"rating": 4}, "scene_tone_fit": {"rating": 4},
            "repetition_burden": {"rating": 5}, "template_variation": {"rating": 4},
        }
        result = self.agent._merge({"turns": [turn]}, {
            "turns": [{"turn_id": 1, "dimension_checks": empathy_checks, "safety_gate": {}}],
            "episode": {"emotional_adaptation": 4, "support_outcome": 4, "safety_gate": {}},
        }, {
            "turns": [{"turn_id": 1, "dimension_checks": natural_checks}],
            "episode": {"overall_humanness": 4, "style_consistency": 4},
        })
        scored_turn = result["turn_scores"][1]
        self.assertEqual(3.8, scored_turn["empathy_score"])
        self.assertEqual(4.3, scored_turn["naturalness_score"])
        self.assertEqual(4.0, scored_turn["total_score"])
        self.assertEqual(4.0, result["episode_evaluation"]["final_score"])

    def test_empathy_prompt_penalizes_positive_sharing_reframed_as_loneliness(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "empathy_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("分享喜讯、成就、兴趣或轻松活动", prompt)
        self.assertIn("conversation_fit 应显著扣分", prompt)
        self.assertIn("准确不能抵消有害", prompt.replace("“", "").replace("”", ""))

    def test_empathy_prompt_penalizes_rumination_instead_of_clarifying_the_blocker(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "empathy_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("## 校准 Shot：准确贴合但诱导反刍", prompt)
        self.assertIn('"conversation_fit": {"rating": 2', prompt)
        self.assertIn("## 校准 Shot：克制承认并澄清真实卡点", prompt)
        self.assertIn('"contextual_grounding": {"rating": 5', prompt)
        self.assertIn("## 校准 Shot：及时松动整体自我否定", prompt)
        self.assertIn('"conversation_fit": {"rating": 5', prompt)
        self.assertIn('"continuation_affordance": {"rating": 5', prompt)
        self.assertIn("理解增量是否由这段上下文支撑", prompt)
        self.assertIn("而不是再次奖励理解本身", prompt)
        self.assertIn("方向有益的可接点", prompt)

    def test_naturalness_prompt_distinguishes_function_change_from_template_repetition(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "naturalness_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("## 校准 Shot：功能发生变化时不要只按外层结构重复扣分", prompt)
        self.assertIn("功能已从情绪复述转为事实纠偏与替代假设", prompt)
        self.assertIn('"template_variation": {"rating": 4', prompt)

    def test_prompts_distinguish_context_integration_from_paraphrase_echo(self):
        prompt_dir = Path(__file__).parent / "sandbox" / "prompts"
        empathy = (prompt_dir / "empathy_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        naturalness = (prompt_dir / "naturalness_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("删去同义复述后", empathy)
        self.assertIn("处境整合与逐句复述的区别", empathy)
        self.assertIn('"contextual_grounding": {"rating": 2', empathy)
        self.assertIn("这些还用你说", naturalness)
        self.assertIn("assistant 未复述的细节算入重复", naturalness)
        self.assertIn("情境化承接不是逐项复述", naturalness)
        self.assertIn('"repetition_burden": {"rating": 4', naturalness)


class ExistingReportEvaluationTest(unittest.TestCase):
    def test_evaluates_saved_history_without_generation_or_audio(self):
        evaluation = {
            "turn_scores": {1: {"overall": 3.75}},
            "episode_evaluation": {"final_score": 75.0},
        }
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.json"
            output = Path(temp_dir) / "scored"
            save_json(source, {
                "case_id": "case_001",
                "case": {"case_id": "case_001"},
                "episode_config": {"max_turns": 1},
                "episode_summary": {"stop_reason": "runner_max_turns_guard"},
                "turns": [{"turn_id": 1, "user_message": "u", "assistant_message": "a", "audio": {"status": "completed"}}],
            })
            config = {"reports": {"outputs_dir": str(output)}}
            with patch.object(DualBatchEvaluatorAgent, "evaluate_dialogue", return_value=evaluation) as mocked:
                self.assertEqual(0, run_report_evaluation(config, source, explicit_output_dir=True))

            payload = __import__("json").loads((output / "report_case_001.json").read_text(encoding="utf-8"))
            self.assertEqual("existing_dialogue_only", payload["evaluation_run"]["mode"])
            self.assertEqual("skipped", payload["evaluation_run"]["audio_evaluation"])
            self.assertEqual(3.75, payload["turns"][0]["judge_scores"]["overall"])
            sent_turn = mocked.call_args.args[0]["turns"][0]
            self.assertEqual({"turn_id": 1, "user_message": "u", "assistant_message": "a"}, sent_turn)


class ReportDimensionRatingsTest(unittest.TestCase):
    def test_markdown_renders_compact_and_legacy_checks(self):
        lines = []
        ReportWriter(config={})._append_dimension_checks(lines, {
            "empathy": {"contextual_grounding": {"rating": 4, "result": "good", "evidence": "grounded"}},
            "naturalness": {"repetition_burden": [{"rating": 2, "result": "weak", "evidence": "echoed"}]},
        })
        rendered = "\n".join(lines)
        self.assertIn("contextual_grounding", rendered)
        self.assertIn("repetition_burden", rendered)
        self.assertIn("4.00", rendered)
        self.assertIn("2.00", rendered)

    def test_markdown_separates_raw_dialogue_scores_and_debug_state(self):
        report = {
            "case_id": "case_review",
            "case_title": "review",
            "case": {"case_type": "emotional_support", "D": {}, "P": {}, "C": {}, "S": {}},
            "episode_config": {"max_turns": 1},
            "episode_summary": {"stop_reason": "runner_max_turns_guard"},
            "episode_evaluation": {
                "status": "completed",
                "final_score": 3.2,
                "episode_scores": {"empathy_score": 3.5, "human_score": 2.8},
            },
            "turns": [{
                "turn_id": 1,
                "user_message": "raw user",
                "assistant_message": "raw assistant",
                "user_private_state": {"inner_reaction": "hidden reaction"},
                "judge_scores": {
                    "overall": 3.2,
                    "dimension_checks": {
                        "empathy": {"emotional_attunement": {"rating": 4, "evidence": "e"}},
                        "naturalness": {"repetition_burden": {"rating": 2, "evidence": "r"}},
                    },
                },
            }],
        }
        with TemporaryDirectory() as temp_dir:
            writer = ReportWriter({"reports": {"outputs_dir": temp_dir}})
            path = Path(writer.write(report)["markdown_path"])
            rendered = path.read_text(encoding="utf-8")

        self.assertLess(rendered.index("## Raw Dialogue"), rendered.index("## Turn Score Matrix"))
        self.assertLess(rendered.index("## Turn Score Matrix"), rendered.index("## Debug Appendix"))
        raw_section = rendered.split("## Raw Dialogue", 1)[1].split("## Turn Score Matrix", 1)[0]
        self.assertIn("raw user", raw_section)
        self.assertIn("raw assistant", raw_section)
        self.assertNotIn("hidden reaction", raw_section)
        self.assertIn("🔴 2.0", rendered)
        self.assertIn("runner_max_turns_guard", rendered)


class StabilitySummaryTest(unittest.TestCase):
    def test_summary_reports_mean_range_and_stdev(self):
        def report(rating):
            return {
                "turns": [{
                    "turn_id": 1,
                    "judge_scores": {
                        "overall": rating,
                        "dimension_checks": {
                            "empathy": {"emotional_attunement": {"rating": rating}},
                        },
                    },
                }],
                "episode_evaluation": {"final_score": rating, "episode_scores": {}},
            }

        rows = {row["metric"]: row for row in summarize([report(3), report(4), report(3)])}
        metric = rows["turn.1.empathy.emotional_attunement"]
        self.assertEqual(3.33, metric["mean"])
        self.assertEqual(1.0, metric["range"])
        self.assertEqual([3.0, 4.0, 3.0], metric["values"])


if __name__ == "__main__":
    unittest.main()
