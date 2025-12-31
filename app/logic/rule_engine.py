"""Rule engine for MVP adaptation logic."""

from __future__ import annotations

from typing import Dict, Optional


class RuleEngine:
    """Evaluate adaptation rules for MVP interventions."""

    ACTION_ACTIVATE_RED_ZONE = "activate_red_zone"
    ACTION_SUGGEST_MODE_SWITCH = "suggest_mode_switch"
    ACTION_NO_INTERVENTION = "no_intervention"

    PROPOSAL_REDUCE_LOAD = "reduce_load"
    PROPOSAL_OBSERVATION_MODE = "observation_mode"

    LOAD_RULES = {
        "INTENSIVE": {"threshold": 9, "target": "MID"},
        "MID": {"threshold": 6, "target": "LITE"},
        "LITE": {"threshold": 3, "target": "OBSERVATION"},
    }

    def evaluate(
        self,
        *,
        current_load: Optional[str],
        skip_streak: int,
        user_mode: Optional[str],
        days_since_last_active: int,
    ) -> Dict[str, Optional[str]]:
        """Return the action proposal based on MVP rule set."""
        normalized_mode = (user_mode or "").strip().upper()
        if normalized_mode == "OBSERVATION":
            return self._no_intervention()

        if days_since_last_active >= 7:
            return {
                "action": self.ACTION_SUGGEST_MODE_SWITCH,
                "proposal": self.PROPOSAL_OBSERVATION_MODE,
                "target_value": "OBSERVATION",
            }

        normalized_load = (current_load or "").strip().upper()
        rule = self.LOAD_RULES.get(normalized_load)
        if rule and skip_streak >= rule["threshold"]:
            return {
                "action": self.ACTION_ACTIVATE_RED_ZONE,
                "proposal": self.PROPOSAL_REDUCE_LOAD,
                "target_value": rule["target"],
            }

        return self._no_intervention()

    def _no_intervention(self) -> Dict[str, Optional[str]]:
        return {
            "action": self.ACTION_NO_INTERVENTION,
            "proposal": None,
            "target_value": None,
        }
