"""Rule engine for MVP adaptation logic."""

from __future__ import annotations

from typing import Optional


class RuleEngine:
    """Evaluate adaptation rules for MVP interventions."""

    PROPOSAL_REDUCE_LOAD = "PROPOSAL_REDUCE_LOAD"
    PROPOSAL_OBSERVATION = "PROPOSAL_OBSERVATION"

    LOAD_THRESHOLDS = {
        "INTENSIVE": 9,
        "MID": 6,
        "LITE": 3,
    }
    MAX_SKIP_THRESHOLD = max(LOAD_THRESHOLDS.values())

    def evaluate(
        self,
        *,
        load: str,
        skip_streak: int,
    ) -> Optional[str]:
        """Return the proposal signal based on strict skip thresholds."""
        threshold = self.LOAD_THRESHOLDS.get(load)
        if threshold is None:
            return None
        if skip_streak >= threshold:
            if load == "LITE":
                return self.PROPOSAL_OBSERVATION
            return self.PROPOSAL_REDUCE_LOAD

        return None
