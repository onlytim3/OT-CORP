"""Intraday Activity Schedule — volume-based execution scaling.

Crypto markets have distinct 'peak' and 'dead' hours based on global session
confluences (US Open, HK/Singapore Open, etc.). This module provides
multipliers to scale position sizes based on expected liquidity and momentum.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Activity multipliers by UTC hour (0-23)
# 00-02: Asia session start (High)
# 03-07: Late Asia / Mid-day (Low)
# 08-11: Europe Open (Med-High)
# 12-16: US Open / Europe overlap (Peak)
# 17-20: Late US (Med)
# 21-23: Asia lead-up / US close (Low)
HOURLY_MULTIPLIERS = {
    0: 1.2, 1: 1.2, 2: 1.1,
    3: 0.7, 4: 0.6, 5: 0.6, 6: 0.7, 7: 0.8,
    8: 1.0, 9: 1.1, 10: 1.2, 11: 1.2,
    12: 1.4, 13: 1.5, 14: 1.5, 15: 1.4, 16: 1.3,
    17: 1.1, 18: 1.0, 19: 0.9, 20: 0.8,
    21: 0.7, 22: 0.8, 23: 1.0
}

def get_intraday_activity_mult() -> float:
    """Return the volume scaling multiplier for the current UTC hour.
    
    Returns 1.0 as fallback or if hour not in map.
    """
    try:
        current_hour = datetime.now(timezone.utc).hour
        mult = HOURLY_MULTIPLIERS.get(current_hour, 1.0)
        
        # Log if we are in a 'dead' or 'peak' zone
        if mult >= 1.4:
            log.debug("PEAK TRADING HOURS: Scaling size by %.1fx", mult)
        elif mult <= 0.7:
            log.debug("DEAD TRADING HOURS: Reducing size by %.1fx", mult)
            
        return mult
    except Exception as e:
        log.warning("Could not calculate intraday multiplier: %s", e)
        return 1.0
