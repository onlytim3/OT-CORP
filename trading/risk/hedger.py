"""Tail Risk Hedger — institutional-grade protection against market crashes.

Monitors the global regime score from the Intelligence Briefing and
triggers active hedging or exposure neutralization when extreme tail
risk is detected.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Thresholds
TAIL_RISK_THRESHOLD = -0.6    # Regime score below this triggers hedging
EMERGENCY_EXIT_THRESHOLD = -0.8  # Regime score below this moves 100% to cash

class TailHedgeManager:
    """Manages active hedging and exposure caps during market crises."""

    def __init__(self):
        self._last_score: float = 0.0
        self._is_hedging: bool = False

    def check_regime(self, regime_score: float):
        """Update internal state based on latest regime score."""
        self._last_score = regime_score
        
        if regime_score <= TAIL_RISK_THRESHOLD:
            if not self._is_hedging:
                log.warning(
                    "CRISIS DETECTED: Regime score %.2f below threshold %.2f. Triggering active hedging.",
                    regime_score, TAIL_RISK_THRESHOLD
                )
                self._is_hedging = True
        else:
            if self._is_hedging:
                log.info(
                    "CRISIS RECOVERED: Regime score %.2f above threshold. Disabling hedging logic.",
                    regime_score
                )
                self._is_hedging = False

    def get_hedge_multiplier(self) -> float:
        """Returns a multiplier to scale down ALL long exposure.
        
        1.0 = No change
        0.5 = Scale all positions by 50%
        0.0 = Emergency exit (liquidate all)
        """
        if self._last_score <= EMERGENCY_EXIT_THRESHOLD:
            return 0.0
        if self._last_score <= TAIL_RISK_THRESHOLD:
            # Linear scaling from 0.7 to 0.1 between thresholds
            # -0.6 -> 0.7
            # -0.8 -> 0.0
            range_len = abs(EMERGENCY_EXIT_THRESHOLD - TAIL_RISK_THRESHOLD)
            dist = abs(self._last_score - TAIL_RISK_THRESHOLD)
            mult = max(0.0, 0.7 * (1.0 - (dist / range_len)))
            return round(mult, 2)
            
        return 1.0

    def get_hedge_recommendations(self) -> list[dict]:
        """Produce recommendations for the Autonomous Cycle."""
        recs = []
        if self._last_score <= TAIL_RISK_THRESHOLD:
            recs.append({
                "from_agent": "risk_agent",
                "to_agent": "executor_agent",
                "category": "change_regime",
                "action": "active_hedge",
                "target": "portfolio",
                "reasoning": (
                    f"Tail Risk Alert: Global regime score is {self._last_score:+.2f}. "
                    f"Risk of cascading liquidation. Activating tail hedge multiplier "
                    f"({self.get_hedge_multiplier():.2f}x) to protect capital."
                ),
                "data": {
                    "regime_score": self._last_score,
                    "hedge_multiplier": self.get_hedge_multiplier(),
                    "auto_approve": True,
                }
            })
        return recs

# Singleton instance
hedger = TailHedgeManager()
