"""ML Signal Filter — autonomous guardrail for agent recommendations.

Analyzes historical 'resolve_recommendation' outcomes to identify failing patterns
and prevent the system from auto-applying similar 'bad' alpha.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from trading.db.store import get_recommendation_history

log = logging.getLogger(__name__)

class RecommendationGuardrail:
    """Pattern-matching guardrail that learns from historical failures."""

    def __init__(self, lookback_count=200):
        self.lookback_count = lookback_count
        self.stats = {}  # {(agent, category, action): (successes, failures)}
        self._refresh_stats()

    def _refresh_stats(self):
        """Build the pattern memory from historical outcomes."""
        try:
            history = get_recommendation_history(limit=self.lookback_count)
            # Only look at resolved recommendations with a clear outcome
            resolved = [r for r in history if r.get("status") == "resolved" and r.get("outcome")]
            
            new_stats = {}
            for r in resolved:
                key = (r["from_agent"], r["category"], r["action"])
                if key not in new_stats:
                    new_stats[key] = {"positive": 0, "negative": 0}
                
                outcome = r["outcome"]
                if outcome == "positive":
                    new_stats[key]["positive"] += 1
                elif outcome == "negative":
                    new_stats[key]["negative"] += 1
            
            self.stats = new_stats
            log.info("Signal filter refreshed: analyzed %d resolved recommendations", len(resolved))
        except Exception as e:
            log.error("Failed to refresh signal filter stats: %s", e)

    def get_success_rate(self, from_agent, category, action):
        """Get the success rate for a specific pattern."""
        key = (from_agent, category, action)
        pattern = self.stats.get(key)
        if not pattern:
            return None  # Neutral (never seen this pattern before)
            
        pos = pattern["positive"]
        neg = pattern["negative"]
        total = pos + neg
        
        if total < 5:  # Need at least 5 samples to judge (was 3 — too easy to blacklist)
            return None

        return pos / total

    def should_allow(self, from_agent, category, action, threshold=0.2):
        """Determine if a recommendation should be allowed to auto-execute.
        
        If the success rate for this pattern is below the threshold, returns False.
        """
        success_rate = self.get_success_rate(from_agent, category, action)
        
        if success_rate is not None and success_rate < threshold:
            log.warning(
                "GUARDRAIL: Pattern (%s, %s, %s) has low success rate (%.1f%%). Vetoing auto-execution.",
                from_agent, category, action, success_rate * 100
            )
            return False
            
        return True

    def get_blacklist(self):
        """Return patterns that are currently being filtered."""
        blacklist = []
        for key, pattern in self.stats.items():
            pos, neg = pattern["positive"], pattern["negative"]
            total = pos + neg
            if total >= 5 and (pos / total) < 0.2:
                blacklist.append({
                    "pattern": key,
                    "success_rate": round(pos / total, 3),
                    "samples": total
                })
        return blacklist

# Singleton instance
guardrail = RecommendationGuardrail()

def refresh_guardrail():
    """Manually trigger a refresh of the guardrail stats."""
    guardrail._refresh_stats()

def is_recommendation_safe(from_agent, category, action) -> bool:
    """Public interface for the guardrail."""
    return guardrail.should_allow(from_agent, category, action)
