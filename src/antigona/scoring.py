"""Conversation scoring — evaluate quality of conversation turns.

Scoring dimensions:
- Identity: Does the response correctly identify the agent?
- Relevance: Is the response relevant to the user message?
- Repetition: Does the response repeat itself unnecessarily?
- Clarification: Does the response ask for clarification when needed?
- Overall: Aggregated score

Threshold: all scores >= 0.6 (60%), average >= 0.75 (75%).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ScoreResult:
    """Result of scoring one turn against quality dimensions.

    Attributes:
        identity_score: 0.0–1.0 — correct self-identification.
        relevance_score: 0.0–1.0 — relevance to user input.
        repetition_score: 0.0–1.0 — higher means less repetition.
        clarification_score: 0.0–1.0 — appropriate clarification use.
        overall_score: 0.0–1.0 — weighted average.
        details: Per-dimension reasoning.
    """

    identity_score: float = 0.0
    relevance_score: float = 0.0
    repetition_score: float = 0.0
    clarification_score: float = 0.0
    overall_score: float = 0.0
    details: dict[str, str] = field(default_factory=dict)


# ── Scorers ──────────────────────────────────────────────────────────────────

_AGENT_NAMES = {"antigona", "ai-агент", "помощник", "бот", "агент"}
_GREETING_KEYWORDS = {"привет", "здравствуй", "хай", "hello", "hi", "hey"}
_IDENTITY_TRIGGERS = {"кто ты", "ты кто", "кто такой", "who are you"}
_CLARIFICATION_KEYWORDS = {
    "уточните", "не понял", "не разобрал", "неясно",
    "clarify", "what exactly", "что именно",
    "как(?:ой|ая|ое)", "что за", "какого", "какую",
}
# Combined clarification regex — catches "Что проверить?", "Какой файл?", etc.
_CLARIFICATION_RE = re.compile(
    r"(?i)(?:уточните|не понял|не разобрал|неясно|"
    r"что (?:именно|нужно|за|такое|вы хотите)|"
    r"(?:какой|какая|какое|какие) (?:файл|объект|контекст|детали)|"
    r"нужен контекст|добавьте деталей|что проверить)"
)


def _keyword_overlap(a: str, b: str) -> float:
    """Jaccard-like overlap of word sets."""
    words_a = {w.lower().strip("?!.,;:") for w in a.split() if len(w) > 2}
    words_b = {w.lower().strip("?!.,;:") for w in b.split() if len(w) > 2}
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def score_identity(user_text: str, response: str) -> tuple[float, str]:
    """Score identity — does the response self-identify when asked?

    When user asks an identity question, response must contain the agent name.
    Otherwise identity is automatically good (no identity request).
    """
    user_lower = user_text.lower()
    response_lower = response.lower()

    # Check if user is asking about identity
    is_identity_question = any(
        trigger in user_lower for trigger in _IDENTITY_TRIGGERS
    )

    if not is_identity_question:
        return 1.0, "no identity question asked"

    # Response should contain agent name
    has_name = any(name in response_lower for name in _AGENT_NAMES)
    if has_name:
        return 1.0, "response contains agent name"
    return 0.3, "identity question asked but response does not identify agent"


def score_relevance(user_text: str, response: str) -> tuple[float, str]:
    """Score relevance — is the response on-topic?

    Uses keyword overlap and common patterns.
    """
    if not user_text.strip() or not response.strip():
        return 0.5, "empty text or response"

    overlap = _keyword_overlap(user_text, response)
    user_lower = user_text.lower()
    response_lower = response.lower()

    # Greeting → greeting response is relevant regardless of overlap
    is_greeting = any(g in user_lower for g in _GREETING_KEYWORDS)
    if is_greeting and overlap >= 0.0:
        return 0.9, "greeting matched with appropriate response"

    # Identity questions → relevant when the response identifies the agent
    identity_indicators = ["кто ты", "как тебя зовут", "ты кто", "как тебя зовут?", "кто ты?"]
    if any(q in user_lower for q in identity_indicators):
        from antigona.scoring import _AGENT_NAMES

        if any(name in response_lower for name in _AGENT_NAMES):
            return 0.9, "identity question answered with agent name"
        return 0.5, "identity question asked but response does not name agent"

    # Explain/how-does-it-work questions → relevant when the response explains
    explain_indicators = ["объясни", "как работает", "расскажи", "почему", "что такое"]
    if any(q in user_lower for q in explain_indicators):
        explain_tokens = ["объясн", "работает", "классифицир", "роутер", "использует", "предназнач", "это"]
        if any(t in response_lower for t in explain_tokens):
            return 0.9, "explanation question answered with explanatory response"
        return 0.55, "explanation question asked but response is not explanatory"

    # Task words → task-related response
    task_indicators = ["создай", "файл", "напиши", "запусти", "сделай"]
    has_task = any(t in user_lower for t in task_indicators)
    if has_task and overlap >= 0.1:
        return 0.85, "task detected with relevant response"

    # Fallback on keyword overlap
    if overlap >= 0.3:
        return min(1.0, 0.5 + overlap), f"keyword overlap {overlap:.2f}"
    if overlap >= 0.1:
        return 0.6, f"partial keyword overlap {overlap:.2f}"
    return 0.4, f"low keyword overlap {overlap:.2f}"


def score_repetition(response: str, previous_response: str | None = None) -> tuple[float, str]:
    """Score repetition — higher score means less repetition.

    Checks:
    1. Self-repetition within the response (repeated phrases)
    2. Repetition of previous response (if available)
    """
    if not response.strip():
        return 0.5, "empty response"

    words = response.lower().split()
    unique_words = set(words)
    word_ratio = len(unique_words) / max(len(words), 1)

    # Check for repeated sentences/phrases
    sentences = [s.strip() for s in re.split(r'[.!?]', response) if s.strip()]
    unique_sentences = set(s.lower() for s in sentences)
    sent_ratio = len(unique_sentences) / max(len(sentences), 1)

    intra_score = min(1.0, 0.3 + word_ratio * 0.4 + sent_ratio * 0.3)

    if previous_response and len(previous_response.strip()) > 5:
        prev_lower = previous_response.lower().strip()
        resp_lower = response.lower().strip()
        # Check if response is verbatim repeat
        if resp_lower == prev_lower:
            return 0.2, "verbatim repeat of previous response"
        # Check substantial overlap
        overlap = _keyword_overlap(response, previous_response)
        if overlap > 0.8:
            return max(0.2, intra_score * 0.5), f"high overlap with previous response ({overlap:.2f})"

    if intra_score >= 0.8:
        return intra_score, "low repetition"
    if intra_score >= 0.5:
        return intra_score, "moderate repetition"
    return intra_score, "high repetition"


def score_clarification(user_text: str, response: str) -> tuple[float, str]:
    """Score clarification quality.

    - When user is vague, response should ask for clarification (good).
    - When user is specific, clarification is unnecessary (good).
    - When user is specific but response asks for clarification (bad).
    """
    user_lower = user_text.lower()
    response_lower = response.lower()

    has_clarification = bool(_CLARIFICATION_RE.search(response_lower))

    # Also check individual keywords
    for kw in _CLARIFICATION_KEYWORDS:
        if kw in response_lower:
            has_clarification = True
            break

    # Detect vague user input
    is_short = len(user_text.split()) <= 2
    is_bare_verb = bool(re.match(
        r"^(?:проверь|исправь|создай|сделай|напиши|запусти|удали|измени|добавь)$",
        user_lower.strip(),
    ))

    has_specific_content = bool(
        re.search(r"файл|код|путь|path|file|test|config|log", user_lower)
        or len(user_text.split()) >= 4
    )

    # Direct questions (identity / explanation / how / what) are specific
    # requests, not vague input — no clarification is required.
    is_direct_question = bool(re.search(
        r"кто ты|как тебя зовут|ты кто|объясни|как работает|что такое|"
        r"почему|расскажи|как дела|что умеешь",
        user_lower,
    ))

    # Vague input → clarification is good
    if is_direct_question:
        return 1.0, "direct question does not require clarification"
    if is_bare_verb or (is_short and not has_specific_content):
        if has_clarification:
            return 1.0, "appropriate clarification for vague input"
        return 0.4, "vague input but no clarification asked"

    # Specific input → clarification is unnecessary
    if has_specific_content and has_clarification:
        return 0.5, "specific input but unnecessary clarification"

    # Specific input → no clarification (ideal)
    if has_specific_content and not has_clarification:
        return 1.0, "specific input, direct response"

    return 0.7, "adequate response"


# ── Scoring pipeline ─────────────────────────────────────────────────────────


def score_turn(
    user_text: str,
    response: str,
    previous_response: str | None = None,
) -> ScoreResult:
    """Score one conversation turn across all quality dimensions.

    Args:
        user_text: The user's message.
        response: The system's response.
        previous_response: The previous system response (for repetition check).

    Returns:
        ScoreResult with per-dimension scores and overall.
    """
    id_score, id_detail = score_identity(user_text, response)
    rel_score, rel_detail = score_relevance(user_text, response)
    rep_score, rep_detail = score_repetition(response, previous_response)
    clar_score, clar_detail = score_clarification(user_text, response)

    # Weighted overall: identity low weight (rare), relevance high
    overall = (
        id_score * 0.15
        + rel_score * 0.35
        + rep_score * 0.25
        + clar_score * 0.25
    )

    return ScoreResult(
        identity_score=round(id_score, 4),
        relevance_score=round(rel_score, 4),
        repetition_score=round(rep_score, 4),
        clarification_score=round(clar_score, 4),
        overall_score=round(overall, 4),
        details={
            "identity": id_detail,
            "relevance": rel_detail,
            "repetition": rep_detail,
            "clarification": clar_detail,
        },
    )


def check_thresholds(
    result: ScoreResult,
    min_dimension: float = 0.6,
    min_overall: float = 0.75,
) -> dict[str, bool | float]:
    """Check if scoring thresholds are met.

    Args:
        result: The scoring result.
        min_dimension: Minimum allowed per-dimension score.
        min_overall: Minimum allowed overall score.

    Returns:
        Dict with pass/fail per dimension.
    """
    return {
        "identity_pass": result.identity_score >= min_dimension,
        "relevance_pass": result.relevance_score >= min_dimension,
        "repetition_pass": result.repetition_score >= min_dimension,
        "clarification_pass": result.clarification_score >= min_dimension,
        "overall_pass": result.overall_score >= min_overall,
        "all_pass": (
            result.identity_score >= min_dimension
            and result.relevance_score >= min_dimension
            and result.repetition_score >= min_dimension
            and result.clarification_score >= min_dimension
            and result.overall_score >= min_overall
        ),
        "overall_score": result.overall_score,
    }
