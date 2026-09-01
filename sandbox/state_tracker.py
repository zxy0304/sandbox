"""Deterministic bounds checker for simulated user-state changes."""

from sandbox.schemas import INTERNAL_STATE_KEYS, normalize_state


class StateTracker:
    """Apply only UserThinker's state delta, with deterministic safeguards."""

    def __init__(self, config=None):
        self.config = config or {}
        state_config = self.config.get("state", {})
        self.floor = float(state_config.get("floor", 0))
        self.ceiling = float(state_config.get("ceiling", 5))
        self.max_step = abs(float(state_config.get("max_step", 0.5)))

    def update(self, context):
        """Clamp and apply the user's subjective state_delta_hint.

        Evaluator scores and assistant-text heuristics are deliberately ignored:
        the evaluator is a sidecar diagnostic, not a source of user state.
        """
        before = normalize_state(context.get("state_before"), self.floor, self.ceiling)
        private_state = context.get("user_private_state", {})
        requested = private_state.get("state_delta_hint", {}) if isinstance(private_state, dict) else {}
        if not isinstance(requested, dict):
            requested = {}

        applied = {}
        after = {}
        for key in INTERNAL_STATE_KEYS:
            raw_delta = self._number(requested.get(key, 0))
            bounded_delta = max(-self.max_step, min(self.max_step, raw_delta))
            next_value = max(self.floor, min(self.ceiling, before[key] + bounded_delta))
            after[key] = round(next_value, 2)
            applied[key] = round(after[key] - before[key], 2)

        return {
            "state_after": after,
            "state_delta": applied,
            "state_update_reason": {
                "source": "user_thinker_state_delta_hint",
                "rule": "仅采用 UserThinker 模拟的用户主观变化；StateTracker 只负责单轮限幅和状态边界。",
                "max_step": self.max_step,
                "requested_delta": {
                    key: self._number(requested.get(key, 0))
                    for key in INTERNAL_STATE_KEYS
                },
            },
        }

    def _number(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
