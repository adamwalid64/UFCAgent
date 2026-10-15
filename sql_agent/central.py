"""Final central comparison: odds and community percentages never enter source analysis."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Odds(StrictModel):
    bookmaker: str = Field(min_length=1)
    observed_at: datetime
    moneyline: dict[str, int]

    @model_validator(mode="after")
    def validate_odds(self):
        if len(self.moneyline) != 2 or any(abs(value) < 100 for value in self.moneyline.values()):
            raise ValueError("Supply two valid American moneylines (absolute value at least 100)")
        return self


class PublicPicks(StrictModel):
    source: str = Field(min_length=1)
    observed_at: datetime
    percentages: dict[str, float]
    sample_size: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shares(self):
        if (len(self.percentages) != 2 or any(not 0 <= value <= 100 for value in self.percentages.values())
                or abs(sum(self.percentages.values()) - 100) > 0.1):
            raise ValueError("Supply two community pick percentages totaling 100")
        return self


class MarketRecord(StrictModel):
    event_date: date
    fighter_a: str
    fighter_b: str
    odds: Odds | None = None
    public_sentiment: PublicPicks | None = None


def implied_probability(line):
    return -line / (-line + 100) if line < 0 else 100 / (line + 100)


class CentralNode:
    """Reads an explicit local, timestamped market snapshot only after prediction finalization."""
    def __init__(self, market_path=None, now=None):
        self.market_path = Path(market_path) if market_path else None
        self.now = now or (lambda: datetime.now(timezone.utc))

    def finalize(self, prediction):
        if not self.market_path:
            return prediction
        result = dict(prediction)
        result["caveats"] = list(prediction.get("caveats", []))
        try:
            payload = json.loads(self.market_path.read_text(encoding="utf-8"))
            if set(payload) != {"fights"} or not isinstance(payload["fights"], list):
                raise ValueError("Market snapshot requires a fights list")
            fighters = {result["fighter_a"], result["fighter_b"]}
            cutoff = datetime.fromisoformat(result["as_of_date"]).replace(tzinfo=timezone.utc)
            records = [MarketRecord.model_validate(item) for item in payload["fights"]]
            matches = [item for item in records if {item.fighter_a, item.fighter_b} == fighters
                       and item.event_date.isoformat() == result["as_of_date"]]
            if len(matches) != 1:
                raise ValueError("No unique market snapshot for this matchup and cutoff date")
            record = matches[0]
            context = {"odds": None, "public_sentiment": None, "comparisons": [], "caveats": []}
            for key in ("odds", "public_sentiment"):
                item = getattr(record, key)
                if item is None:
                    continue
                if item.observed_at.tzinfo is None:
                    context["caveats"].append(f"{key}: excluded timestamp without a timezone.")
                    continue
                if item.observed_at >= cutoff or item.observed_at > self.now():
                    context["caveats"].append(f"{key}: excluded post-cutoff or future observation.")
                    continue
                values = item.moneyline if key == "odds" else item.percentages
                if set(values) != fighters:
                    context["caveats"].append(f"{key}: excluded mismatched fighter names.")
                    continue
                context[key] = item.model_dump(mode="json")
            odds = context["odds"]
            probabilities = ({name: implied_probability(line) for name, line in odds["moneyline"].items()}
                             if odds else {})
            overround = sum(probabilities.values()) if probabilities else None
            for fighter in sorted(fighters):
                model_probability = (result["win_probability"] if fighter == result["predicted_winner"]
                                     else 1 - result["win_probability"])
                comparison = {"fighter": fighter, "uncalibrated_model_probability": model_probability}
                if probabilities:
                    comparison["market_probability_no_vig"] = probabilities[fighter] / overround
                    comparison["model_minus_market"] = model_probability - comparison["market_probability_no_vig"]
                public = context["public_sentiment"]
                if public:
                    comparison["public_pick_share"] = public["percentages"][fighter] / 100
                    comparison["model_minus_public"] = model_probability - comparison["public_pick_share"]
                    comparison["potentially_overlooked_by_public"] = comparison["model_minus_public"] > 0
                context["comparisons"].append(comparison)
            context["caveats"].append("Community picks and odds may overlap in information; they are compared, never counted as independent votes. "
                                      "Disagreement with uncalibrated model confidence is a research signal, not a measured betting edge.")
            result["market_context"] = context
        except Exception as exc:
            result["caveats"].append(f"Central market context unavailable ({type(exc).__name__}); finalized prediction preserved.")
        return result
