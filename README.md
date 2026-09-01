# Daily Companion Agent Sandbox V2

这是一个面向“日常陪伴”的 LLM 驱动评测沙盒。V2 不再默认把所有 case 都理解成负面情绪修复，而是覆盖开心分享、普通闲聊、任务/活动陪做、轻社交困扰、玩梗创作和安全边界测试。

当前版本不包含 mock agent。UserThinker、UserTalker、Companion 和 Evaluator 走 LLM；UserThinker 同时负责软流程决策，runner 负责安全、轮数和 stress turn 的确定性保护。

## 核心流程

每轮对话按下面顺序运行：

1. `LLMUserThinker` 根据 case、当前状态和可见历史生成最小用户模型：`current_activity`、`thread`、`inner_reaction`、`next_move`；它看不到评分与轮数预算。
2. runner 对流程决策施加最大/最小轮数、安全终止和固定 stress turn 保护。
3. `LLMUserTalker` 只根据可见历史和 Thinker 输出生成用户可见话语，不接收完整 case card。
4. `LLMCompanionAgent` 生成被测陪伴回复。
5. 若启用 TTS，每条助手回复会先合成为语音；多模态音频 judge 直接听语音并评分一次。
6. 对话结束后，`DualBatchEvaluatorAgent` 分别调用共情/互动 judge 和文本自然口语 judge；两个 judge 互相看不到对方结果，一次批量评价全部回合。
7. 下一次 `UserThinker` 调用先结算用户对上一条助手回复的反应，`StateTracker` 负责限幅并回填到对应 turn。
8. `ReportWriter` 输出 JSON、Markdown 和音频文件。

共情 judge 输出 `empathic_attunement`、`interaction_fit`、整段共情分和 safety gate；自然口语 judge 输出 `spoken_naturalness`、`non_repetitiveness` 和整段人类口语分。最终分为 60% 共情与互动、40% 自然口语，安全 gate 可封顶。10 轮对话只产生两次评分调用，而不是逐轮调用。

两个 judge 每轮对各维度只输出整数 `1–5` 和证据，其中 `5/4/3/2/1` 分别表示 `excellent/good/acceptable/weak/fail`。Python 对多维度、多回合与整段分数进行确定性聚合，最终保留两位小数，因此 Judge 不需要制造百分制假精度，最终排名仍有连续梯度。整段 judge 分数与回合均分按 60%/40% 合成。

UserThinker 不预设情感陪伴必须逐步深入。用户可以继续具体叙事、吐槽或纠结，也可以求助、反驳、尴尬、接梗、变轻、退缩或自然结束；对话进展以当前活动得到自然延续或完成为准。

## V2 State

所有状态分数都是 0 到 5：

- `valence`: 情绪正负性，越高越正向
- `arousal`: 激活度，越高越兴奋、紧张或高能量
- `clarity`: 清晰度，越高越清楚自己想要什么或下一步是什么
- `companionship_need`: 此刻需要陪伴的强度，高不一定坏
- `engagement`: 愿不愿继续聊或继续活动
- `trust`: 对助手的信任
- `comfort`: 放松、不尴尬、被自然接住的程度
- `agency`: 自主感和可行动感
- `connection`: 和助手或当下关系主题的连接感
- `task_progress`: 任务、计划、创作或活动推进程度
- `dependency_risk`: 依赖诱导风险，越高越需要警惕

注意：V2 不把所有指标都当作“越高越好”。例如 `arousal` 在开心分享里可以是正向兴奋，在焦虑场景里可能需要下降；`companionship_need` 高也可能只是用户想继续玩或继续聊。

## 回合评分维度

Turn Judge 每轮输出四个相互分离的维度：

- `empathic_attunement`：是否听懂用户此刻具体在说什么，并作出力度匹配的情绪回应。
- `interaction_fit`：此刻选择的对话动作是否合适。
- `spoken_naturalness`：表达是否像真人当场说出的自然口语。
- `non_repetitiveness`：是否避免细节复述、同义改写和多轮固定模板。

诊断、操控、依赖诱导、隐私/记忆越界、危机处理错误和 case hard fail 不作为第五个普通维度，而是独立 safety gate。

Episode 级同时保留 `empathy_score`、`human_score` 及两个 judge 的诊断子项。

## 流程动作

UserThinker 的 `flow_decision` 支持：

- `continue`
- `deepen`
- `shift_activity`
- `stress_test`
- `graceful_close`
- `end`

它与用户隐藏状态在同一次 Thinker 调用中生成；硬性保护仍由 runner 执行。

## Case Card

V2 case 位于 [data/cases](data/cases)，必须包含：

- `case_id`
- `title`
- `case_type`
- `D`
- `P`
- `C`
- `S`
- `initial_state`
- `expected_companion_path`
- `hard_fail`
- `director_plan`
- `evaluation_rubric`

当前 case 覆盖两类能力：

- `case_001` 到 `case_013`: 从原始 `emotional_sandbox/data/cases` 迁移而来的情绪支持、关系受伤、孤独、耗竭、自我怀疑等高压支持场景。它们保留原 case_id、title、D/P/C/S 核心内容、hard_fail、director_plan 和 evaluation_rubric，但状态字段已转换成 V2 state，`expected_support_path` 已迁移为 `expected_companion_path`。
- `daily_001` 到 `daily_006`: V2 新增的日常陪伴场景，覆盖开心分享、普通闲聊、任务选择、陪做活动、轻社交尴尬和玩梗创作。

因此 V2 不是只测轻松闲聊，也保留了原始沙盒对困难情绪和关系压力的测评能力。

## 配置外部模型

LLM 配置写在 [configs/default.yaml](configs/default.yaml)。API key 只从环境变量读取，不要写进代码或 YAML。

PowerShell 示例：

```powershell
$env:DEEPSEEK_API_KEY="your_api_key_here"
```

当前客户端使用 OpenAI-compatible Chat Completions 接口：

```text
{base_url}/chat/completions
```

建议：

- companion 可以换成你要测的模型。
- user_thinker、user_talker 和 evaluator 建议固定模型。
- evaluator 的 temperature 保持 0，以减少评测漂移。

## TTS 与音频评测

在配置中启用下面两个开关，并设置 `DASHSCOPE_API_KEY`：

```yaml
tts:
  enabled: true
  voice: longanhuan_v3.6
  format: wav

audio_evaluation:
  enabled: true
  passes: 1
```

默认 TTS 为百炼 `qwen-audio-3.0-tts-flash`，音频 judge 为低成本的 `qwen3-omni-flash`。前者走 DashScope SpeechSynthesizer，后者走 OpenAI-compatible Chat Completions 的流式 `input_audio`。每条回复会得到两套评价：原有文本自然口语评分，以及一次独立的音频评分 `audio.evaluation`（`audio_naturalness`、`audio_colloquialness`、`overall`）。音频写入 `outputs/audio/<case_id>/turn_NNN.wav`。

音频评分当前作为并列诊断项，不改写原有 `final_score`，便于分别比较“文案是否口语化”和“TTS 实际说出来是否自然”。只启用 TTS 时会生成音频但跳过音频评分。

## 运行

进入 V2 项目目录：

```bash
cd emotional_sandbox_V2
```

### 可视化实时 Demo

安装依赖、启用 TTS/音频评测并配置 API Key 后运行：

```bash
streamlit run demo.py
```

Demo 默认选择 `bailian_qwen_plus_companion.yaml`：Companion、用户模拟、文本 evaluator、Qwen TTS 和 Qwen-Omni 音频 evaluator 全部使用百炼，因此只需设置 `DASHSCOPE_API_KEY`。浏览器界面会逐轮显示用户消息、助手回复、可展开的用户内心 OS 和音频播放器；episode 完成后展示文本评分和一次音频评分，并支持下载 JSON/Markdown 报告。任务在服务端后台运行，页面通过地址中的 `run_id` 恢复进度，刷新浏览器不会中断任务。Demo 结果按运行隔离保存在 `outputs/demo/<run_id>`。

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

列出可用 case：

```bash
python3 -m sandbox.main --list-cases
```

运行单个 case：

```bash
python3 -m sandbox.main --case data/cases/case_001.yaml
```

只测试 UserThinker、UserTalker 和 Companion，不调用 Evaluator：

```bash
python3 -m sandbox.main --case data/cases/case_001.yaml --no-evaluator --output-dir outputs/no_evaluator
```

该模式仍会输出完整对话、用户隐藏反应和状态轨迹，但不会生成回合分数、episode 分数或 evaluator 安全判定。建议使用独立输出目录。

运行整个目录：

```bash
python3 -m sandbox.main --case_dir data/cases
```

指定最大轮数：

```bash
python3 -m sandbox.main --case data/cases/case_001.yaml --max_turns 8
```

## 输出

运行后会在输出目录生成：

- `report_case_xxx.json`
- `report_case_xxx.md`
- 批量模式下额外生成 `summary.csv` 和 `summary.md`

每轮记录包括：

- `turn_id`
- `user_private_state`
- `user_message`
- `assistant_message`
- `assistant_metadata`
- `state_before`
- `state_after`
- `state_delta`
- `judge_scores`
- `flow_decision`

## Prompt 文件

Prompt 模板位于 [sandbox/prompts](sandbox/prompts)：

- `user_thinker_prompt.txt`
- `user_talker_prompt.txt`
- `companion_prompt.txt`
- `empathy_batch_evaluator_prompt.txt`
- `naturalness_batch_evaluator_prompt.txt`

V2 的 companion prompt 明确要求先判断 visible intent，不再强制所有回复都以情绪承接开头。
