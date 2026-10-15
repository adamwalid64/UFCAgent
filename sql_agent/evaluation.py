"""Review contracts and deterministic checks for a candidate prediction."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis: str = Field(min_length=1, description="Concise evidence-based assessment with named opponents, sample limits, and implications")
    fight_ids: list[str] = Field(description="Supporting bouts from this fighter's mandatory evidence; empty only when there are no eligible bouts")


class MatchupAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: EvidenceAnalysis
    b: EvidenceAnalysis


class ReviewChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_grounded: bool
    opponent_quality_addressed: bool
    style_history_addressed: bool
    confidence_justified: bool
    internally_consistent: bool
    temporal_integrity: bool


class ClaimCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fighter_side: Literal["a", "b"]
    category: Literal["opposition", "style_history", "other"]
    claim: str = Field(min_length=1, description="Specific factual claim quoted or closely paraphrased from the draft")
    evidence_path: str = Field(description="Dot-separated source path; historical bouts are keyed by fight_id and style buckets by label, e.g. a.bouts.2ba11e0bf32829d6.ending_round. An empty tags list must be cited as a list, not indexed.")
    evidence_value: str = Field(description="Exact source value as a string: plain higher for a string tier, 7 for a count, [] for empty tags, or {} for an empty named collection. Legacy JSON-quoted strings are also accepted. Do not copy nonempty objects.")
    supported: bool = Field(description="Whether this evidence actually supports the draft claim; false requires a correction")


class PredictionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: ReviewChecks
    issues: list[str] = Field(description="Blocking errors and concrete corrections; empty only when all checks pass")
    cautions: list[str] = Field(description="Unavoidable limitations already acknowledged by the draft, not uncorrected errors")
    summary: str = Field(min_length=1, description="Brief review finding, without private deliberation")
    claim_checks: list[ClaimCheck] = Field(min_length=4, description="Verify specific opponent-quality and historical-style claims for BOTH fighters, then other suspect factual claims")


class PredictionReviewError(ValueError):
    """Raised when the bounded draft/review cycle cannot approve a prediction."""

    def __init__(self, message: str, *, review_history: list[dict] | None = None):
        super().__init__(message)
        self.review_history = review_history or []


def review_evidence_view(evidence: dict) -> dict:
    """Stable named references avoid making reviewers count positions in long arrays."""
    return {**evidence, **{side: {**evidence[side],
                "bouts": {bout["fight_id"]: bout for bout in evidence[side]["bouts"]},
                "history_by_opponent_style": {group["opponent_style"]: group for group in evidence[side]["history_by_opponent_style"]},
                "history_against_next_opponent_tags": {group["opponent_style"]: group for group in evidence[side]["history_against_next_opponent_tags"]},
                "similar_to_next_opponent": {bout["fight_id"]: bout for bout in evidence[side]["similar_to_next_opponent"]}}
            for side in ("a", "b")}}


def validate_candidate(candidate: dict, evidence: dict, scheduled_rounds: int) -> list[str]:
    issues = []
    names = {evidence[side]["fighter"][key].casefold()
             for side in ("a", "b") for key in ("fighter_id", "fighter_name")}
    if candidate["predicted_winner"].casefold() not in names:
        issues.append("Model predicted a fighter outside this matchup")
    if candidate["predicted_round"] > scheduled_rounds:
        issues.append(f"Ending round exceeds the {scheduled_rounds}-round scheduled distance")
    if candidate["predicted_method"] == "Decision" and candidate["predicted_round"] != scheduled_rounds:
        issues.append(f"A decision must end in round {scheduled_rounds}")
    for section in ("opponent_quality_analysis", "style_history_analysis"):
        for side in ("a", "b"):
            available = {row["fight_id"] for row in evidence[side]["bouts"]}
            cited = set(candidate[section][side]["fight_ids"])
            if not cited and available:
                issues.append(f"{section}.{side} must cite at least one eligible historical bout")
            if cited - available:
                issues.append(f"{section}.{side} cites unavailable or wrong-fighter bouts: {sorted(cited - available)}")
    return issues


def require_finalized(result: dict) -> None:
    if "error" in result:
        return
    review = result.get("evaluation", {})
    if review.get("mode") == "deterministic":
        expected = ("schema", "matchup_identity", "scheduled_distance", "historical_references", "cutoff_isolation")
        if review.get("status") == "validated" and all(review.get("deterministic_checks", {}).get(k) is True for k in expected):
            return
        raise ValueError("Prediction has not passed deterministic evaluation; report publication was blocked")
    if (review.get("status") != "approved"
            or any(review.get("checks", {}).get(name) is not True for name in ReviewChecks.model_fields)):
        raise ValueError("Prediction has not passed final evaluation; report publication was blocked")


def validate_review_claims(review: PredictionReview, evidence: dict, *, include_unsupported: bool = True) -> list[str]:
    """Verify the review's quoted source values; semantic support remains a model judgment."""
    issues = []
    for side in ("a", "b"):
        for category in ("opposition", "style_history"):
            if not any(check.fighter_side == side and check.category == category for check in review.claim_checks):
                issues.append(f"Reviewer must check a specific {category} claim for fighter {side}")
    view = review_evidence_view(evidence)
    for check in review.claim_checks:
        path = check.evidence_path.removeprefix("mandatory_evidence.")
        if (check.category == "style_history" and evidence[check.fighter_side]["career"]["fights"]
                and not any(path == f"{check.fighter_side}.{section}" or path.startswith(f"{check.fighter_side}.{section}.") for section in (
                    "history_by_opponent_style", "history_against_next_opponent_tags", "similar_to_next_opponent", "bouts"))):
            issues.append(f"Review style history for {check.fighter_side} using historical exposure or bouts, not just their current profile")
        try:
            if not path.startswith(check.fighter_side + "."):
                raise KeyError("wrong fighter")
            actual = view
            for part in path.split("."):
                # Backward compatibility for saved reviews using array indices.
                if isinstance(actual, dict) and part not in actual and part.isdecimal() and actual and all(isinstance(v, dict) for v in actual.values()):
                    actual = list(actual.values())[int(part)]
                    continue
                if isinstance(actual, list):
                    named = [item for item in actual if isinstance(item, dict)
                             and part in (item.get("fight_id"), item.get("opponent_style"))]
                    if len(named) == 1:
                        actual = named[0]
                    elif part.isdecimal():
                        actual = actual[int(part)]
                    else:
                        raise KeyError(part)
                else:
                    actual = actual[part]
            if (isinstance(actual, dict) and actual) or (isinstance(actual, list) and any(isinstance(v, (list, dict)) for v in actual)):
                raise ValueError("Choose a scalar or simple list source value")
        except (KeyError, IndexError, TypeError, ValueError):
            issues.append(f"Reviewer cited an invalid evidence path: {check.evidence_path}")
            continue
        try:
            # A string field already has JSON escaping at the API boundary. Accept
            # an exact source string directly as well as legacy double-encoded JSON.
            quoted = (check.evidence_value if isinstance(actual, str) and check.evidence_value == actual
                      else json.loads(check.evidence_value))
            if quoted != actual or (isinstance(quoted, bool) != isinstance(actual, bool)):
                issues.append(f"Reviewer misquoted evidence at {check.evidence_path}; actual source value is {json.dumps(actual)}. Recheck whether it supports the draft claim")
        except (TypeError, ValueError):
            issues.append(f"Reviewer used an invalid value encoding at {check.evidence_path}; actual source value is {json.dumps(actual)}")
        if include_unsupported and not check.supported:
            issues.append(f"Unsupported claim: {check.claim} (check {check.evidence_path})")
    return issues
