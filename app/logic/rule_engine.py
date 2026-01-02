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
        current_load: Optional[str],
        skip_streak: int,
    ) -> Optional[str]:
        """Return the proposal signal based on strict skip thresholds."""
        normalized_load = (current_load or "").strip().upper()
        threshold = self.LOAD_THRESHOLDS.get(normalized_load)
        if threshold is None:
            return None
        if skip_streak >= threshold:
            if normalized_load == "LITE":
                return self.PROPOSAL_OBSERVATION
            return self.PROPOSAL_REDUCE_LOAD

        return None
