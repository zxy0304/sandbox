import unittest
import json
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

    def test_ratings_are_quantized_to_half_steps(self):
        self.assertEqual(3.5, self.agent._check_rating({"rating": 3.7}))
        self.assertEqual(4.0, self.agent._judge_rating(3.8, 1))
        self.assertEqual(4.5, self.agent._judge_rating(4.5, 1))
        self.assertEqual(3.8, self.agent._score(3.8, 5, 1))

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

    def test_empathy_case_includes_success_tiers_without_private_case_material(self):
        result = self.agent._empathy_case({
            "case_id": "daily_003",
            "case_type": "task_planning",
            "C": {"latent_material": ["隐藏往事"]},
            "evaluation_rubric": {
                "key_success": ["接住真心"],
                "excellence_criteria": ["只选杯子最高 4 分"],
                "scoring_guard": ["不要读心"],
                "hard_fail": ["贬低用户"],
            },
        })
        self.assertEqual(["接住真心"], result["success_criteria"])
        self.assertEqual(["只选杯子最高 4 分"], result["excellence_criteria"])
        self.assertEqual(["不要读心"], result["scoring_guard"])
        self.assertNotIn("C", result)

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

    def test_incomplete_nested_empathy_json_is_repaired_instead_of_defaulting_to_three(self):
        valid = {
            "turns": [{
                "turn_id": 1,
                "dimension_checks": {
                    key: {"rating": 4, "evidence": "visible evidence"}
                    for key in ["emotional_attunement", "contextual_grounding", "conversation_fit", "continuation_affordance"]
                },
            }],
            "episode": {
                "emotional_adaptation": 4,
                "support_outcome": 4,
                "evidence": ["episode evidence"],
            },
        }

        class FakeClient:
            last_error = ""
            last_content = ""

            def __init__(self):
                self.outputs = [{"rating": 3, "evidence": "nested fragment"}, valid]
                self.calls = []

            def prompt(self, _):
                return "prompt"

            def chat_json(self, messages):
                self.calls.append(messages)
                result = self.outputs.pop(0)
                self.last_content = json.dumps(result)
                return result

        client = FakeClient()
        result = self.agent._call(
            client, "empathy_batch_evaluator_prompt.txt",
            {"turns": [{"turn_id": 1}], "turn_count": 1}, "empathy",
        )
        self.assertEqual(valid, result)
        self.assertEqual(2, len(client.calls))
        self.assertIn("不是完整", client.calls[1][-1]["content"])

    def test_incomplete_schema_after_repair_raises_instead_of_creating_fake_scores(self):
        class FakeClient:
            last_error = ""
            last_content = '{"rating": 3}'

            def prompt(self, _):
                return "prompt"

            def chat_json(self, _):
                return {"rating": 3}

        with self.assertRaisesRegex(ValueError, "incomplete schema after repair"):
            self.agent._call(
                FakeClient(), "empathy_batch_evaluator_prompt.txt",
                {"turns": [{"turn_id": 1}], "turn_count": 1}, "empathy",
            )

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

    def test_empathy_prompt_rewards_concise_inferred_understanding_and_holding_space(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "empathy_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("理解可以通过一个简短但精准的回应动作体现", prompt)
        self.assertIn("不得因为助手没有冗长复述", prompt)
        self.assertIn("承认受挫后温和地不跟随用户的全盘自我否定", prompt)
        self.assertIn("先判断对话动能", prompt)
        self.assertIn("用户明显还没说完时", prompt)

    def test_empathy_prompt_prevents_hindsight_scoring_from_later_disclosure(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "empathy_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("当时可见信息边界", prompt)
        self.assertIn("不能被当成助手在第 t 轮本应知道的事实", prompt)
        self.assertIn("不能仅因用户后来澄清", prompt)
        self.assertIn("没有预知未披露细节不是扣分点", prompt)
        self.assertIn("下一轮是否及时调整", prompt)

    def test_naturalness_prompt_distinguishes_function_change_from_template_repetition(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "naturalness_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("## 校准 Shot：功能发生变化时不要只按外层结构重复扣分", prompt)
        self.assertIn("功能已从情绪复述转为事实纠偏与替代假设", prompt)
        self.assertIn('"template_variation": {"rating": 4', prompt)

    def test_naturalness_prompt_penalizes_fabricated_human_autobiography(self):
        prompt = (Path(__file__).parent / "sandbox" / "prompts" / "naturalness_batch_evaluator_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("身份呈现单独记录", prompt)
        self.assertIn("我刚工作那会儿也被当众批过", prompt)
        self.assertIn("identity_claims", prompt)
        self.assertIn("不得由 `identity_claims` 调整", prompt)

    def test_identity_claims_are_reported_without_changing_naturalness_scores(self):
        turn = {"turn_id": 1, "user_message": "u", "assistant_message": "我以前也有个领导"}
        empathy = {
            "turns": [{"turn_id": 1, "dimension_checks": {
                key: {"rating": 4, "evidence": "e"}
                for key in ["emotional_attunement", "contextual_grounding", "conversation_fit", "continuation_affordance"]
            }}],
            "episode": {"emotional_adaptation": 4, "support_outcome": 4, "evidence": ["e"]},
        }
        natural = {
            "turns": [{"turn_id": 1, "dimension_checks": {
                key: {"rating": 4, "evidence": "e"}
                for key in ["spoken_immediacy", "scene_tone_fit", "repetition_burden", "template_variation"]
            }, "identity_claims": ["我以前也有个领导"]}],
            "episode": {"overall_humanness": 4, "style_consistency": 4, "evidence": ["e"]},
        }
        result = self.agent._merge({"turns": [turn]}, empathy, natural)
        self.assertEqual(4.0, result["turn_scores"][1]["naturalness_score"])
        self.assertEqual(["我以前也有个领导"], result["turn_scores"][1]["identity_claims"])

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
