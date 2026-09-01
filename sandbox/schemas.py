"""Shared schema and state helpers for the daily companion sandbox V2.

V2 treats the episode as an everyday companionship simulation rather than a
pure emotional-repair benchmark. State tracks user experience, relationship
movement, activity progress, and dependency risk.
"""

import copy


CASE_REQUIRED_FIELD_PATHS = [
    "case_id",
    "title",
    "case_type",
    "D.age_group",
    "D.gender",
    "D.occupation",
    "D.social_roles",
    "D.living_context",
    "P.personality",
    "P.big_five.openness",
    "P.big_five.conscientiousness",
    "P.big_five.extraversion",
    "P.big_five.agreeableness",
    "P.big_five.neuroticism",
    "P.communication_preference",
    "P.companion_preference",
    "P.playful_style",
    "P.boundaries",
    "P.memory_hooks",
    "P.taboo_responses",
    "C.long_term_background",
    "C.current_context",
    "C.recurring_preferences",
    "C.hidden_need",
    "C.possible_sensitivity",
    "C.relationship_context",
    "S.scene",
    "S.opening_utterance",
    "S.user_intent",
    "S.surface_topic",
    "S.emotional_tone",
    "S.activity_or_task",
    "S.initial_disclosure_level",
    "initial_state.valence",
    "initial_state.arousal",
    "initial_state.clarity",
    "initial_state.companionship_need",
    "initial_state.engagement",
    "initial_state.trust",
    "initial_state.comfort",
    "initial_state.agency",
    "initial_state.connection",
    "initial_state.task_progress",
    "initial_state.dependency_risk",
    "expected_companion_path",
    "hard_fail",
    "director_plan.max_turns",
    "director_plan.min_turns",
    "director_plan.mode_shift_turn",
    "director_plan.stress_turn",
    "director_plan.stress_message",
    "director_plan.early_stop_condition",
    "evaluation_rubric.key_success",
    "evaluation_rubric.hard_fail",
]

MAPPING_FIELD_PATHS = [
    "D",
    "P",
    "P.big_five",
    "C",
    "S",
    "initial_state",
    "director_plan",
    "evaluation_rubric",
]

LIST_FIELD_PATHS = [
    "D.social_roles",
    "P.personality",
    "P.communication_preference",
    "P.companion_preference",
    "P.boundaries",
    "P.memory_hooks",
    "P.taboo_responses",
    "expected_companion_path",
    "hard_fail",
    "evaluation_rubric.key_success",
    "evaluation_rubric.hard_fail",
]

BIG_FIVE_PATHS = [
    "P.big_five.openness",
    "P.big_five.conscientiousness",
    "P.big_five.extraversion",
    "P.big_five.agreeableness",
    "P.big_five.neuroticism",
]

INTERNAL_STATE_KEYS = [
    "valence",
    "arousal",
    "clarity",
    "companionship_need",
    "engagement",
    "trust",
    "comfort",
    "agency",
    "connection",
    "task_progress",
    "dependency_risk",
]

DIRECTOR_TURN_LIMIT_MAX = 60

STATE_ALIASES = {
    "mood_valence": "valence",
    "emotional_valence": "valence",
    "energy": "arousal",
    "activation": "arousal",
    "companionship": "companionship_need",
    "need_for_companionship": "companionship_need",
    "progress": "task_progress",
    "safety_dependency": "dependency_risk",
    "satety_dependency": "dependency_risk",
    "dependency": "dependency_risk",
}


def default_state():
    """Return the canonical default V2 state."""
    return {
        "valence": 2.5,
        "arousal": 2.5,
        "clarity": 2.5,
        "companionship_need": 2.5,
        "engagement": 2.5,
        "trust": 2.0,
        "comfort": 2.5,
        "agency": 2.5,
        "connection": 2.0,
        "task_progress": 0.0,
        "dependency_risk": 0.5,
    }


def clamp_value(value, floor, ceiling):
    """Convert a raw value to float and clamp it."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = floor
    if number < floor:
        number = floor
    if number > ceiling:
        number = ceiling
    return round(number, 2)


def normalize_state(state, floor=0, ceiling=5):
    """Normalize state aliases into the canonical V2 state dictionary."""
    normalized = default_state()
    if state:
        for key, value in state.items():
            normalized_key = STATE_ALIASES.get(key, key)
            if normalized_key in normalized:
                normalized[normalized_key] = value
    for key in INTERNAL_STATE_KEYS:
        normalized[key] = clamp_value(normalized.get(key), floor, ceiling)
    return normalized


def copy_state(state):
    """Return a normalized shallow copy."""
    return copy.deepcopy(normalize_state(state))


def validate_case(case_data, source):
    """Validate and normalize a daily companion case card."""
    if not isinstance(case_data, dict):
        raise ValueError("Case Card must be a YAML mapping.")

    missing = _missing_required_fields(case_data)
    invalid = _invalid_fields(case_data)

    if missing or invalid:
        messages = []
        if missing:
            messages.append("Missing required fields:")
            for path in missing:
                messages.append("  - %s" % path)
        if invalid:
            messages.append("Invalid field values:")
            for item in invalid:
                messages.append("  - %s" % item)
        raise ValueError("Case Card validation failed\n%s" % "\n".join(messages))

    case_data["id"] = case_data["case_id"]
    case_data["initial_state_raw"] = copy.deepcopy(case_data.get("initial_state"))
    case_data["initial_state"] = normalize_state(case_data.get("initial_state"))
    return case_data


def _missing_required_fields(case_data):
    missing = []
    for path in CASE_REQUIRED_FIELD_PATHS:
        exists, value = get_path(case_data, path)
        if not exists or _is_empty(value):
            missing.append(path)
    return missing


def _invalid_fields(case_data):
    invalid = []

    for path in MAPPING_FIELD_PATHS:
        exists, value = get_path(case_data, path)
        if exists and not isinstance(value, dict):
            invalid.append("%s must be a mapping" % path)

    for path in LIST_FIELD_PATHS:
        exists, value = get_path(case_data, path)
        if exists and not isinstance(value, list):
            invalid.append("%s must be a list" % path)
        elif exists and not value:
            invalid.append("%s must not be empty" % path)
        elif exists:
            for index in _empty_list_indexes(value):
                invalid.append("%s[%s] must not be empty" % (path, index))

    for path in BIG_FIVE_PATHS:
        exists, value = get_path(case_data, path)
        if exists and not _is_number_between(value, 1, 5):
            invalid.append("%s must be a number from 1 to 5" % path)

    for key in INTERNAL_STATE_KEYS:
        path = "initial_state.%s" % key
        exists, value = get_path(case_data, path)
        if exists and not _is_number_between(value, 0, 5):
            invalid.append("%s must be a number from 0 to 5" % path)

    director_number_paths = [
        "director_plan.max_turns",
        "director_plan.min_turns",
        "director_plan.mode_shift_turn",
        "director_plan.stress_turn",
    ]
    for path in director_number_paths:
        exists, value = get_path(case_data, path)
        if exists and not _is_number_between(value, 0, DIRECTOR_TURN_LIMIT_MAX):
            invalid.append("%s must be a number from 0 to %s" % (path, DIRECTOR_TURN_LIMIT_MAX))

    exists, max_turns = get_path(case_data, "director_plan.max_turns")
    if exists and _is_number_between(max_turns, 0, DIRECTOR_TURN_LIMIT_MAX) and int(max_turns) < 1:
        invalid.append("director_plan.max_turns must be at least 1")

    min_exists, min_turns = get_path(case_data, "director_plan.min_turns")
    if min_exists and _is_number_between(min_turns, 0, DIRECTOR_TURN_LIMIT_MAX):
        if int(min_turns) < 1:
            invalid.append("director_plan.min_turns must be at least 1")
        elif exists and _is_number_between(max_turns, 0, DIRECTOR_TURN_LIMIT_MAX) and int(min_turns) > int(max_turns):
            invalid.append("director_plan.min_turns must not exceed director_plan.max_turns")

    return invalid


def _empty_list_indexes(values):
    empty = []
    for index, value in enumerate(values):
        if _is_empty(value):
            empty.append(index)
    return empty


def get_path(data, path):
    """Read a dot-delimited path from a nested dictionary."""
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current.get(part)
    return True, current


def _is_empty(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _is_number_between(value, floor, ceiling):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return floor <= number <= ceiling


def make_turn_record(turn_id, user_private_state, user_message, assistant_message, state_before, state_after, judge_scores):
    """Create the standard dictionary saved for each dialogue turn."""
    return {
        "turn_id": turn_id,
        "user_private_state": user_private_state,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "state_before": normalize_state(state_before),
        "state_after": normalize_state(state_after),
        "judge_scores": judge_scores,
    }
