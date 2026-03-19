"""News Sentiment Strategy — LLM-interpreted headlines driving trading signals.

Fetches headlines from RSS, GDELT, and crypto news APIs, then uses Gemini
to interpret impact on specific assets and generate buy/sell signals.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from trading.config import (
    CRYPTO_SYMBOLS, ASTER_SYMBOLS,
    CRYPTO_L1, CRYPTO_L2, CRYPTO_DEFI, CRYPTO_AI, CRYPTO_MEME,
    STOCK_PERPS, COMMODITY_PERPS, INDEX_PERPS,
)
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# Cooldown tracking — don't re-signal same asset within window
_last_signal_time: dict[str, float] = {}
_COOLDOWN_SECONDS = 3600  # 1 hour


def _get_all_tradeable_symbols() -> dict[str, str]:
    """Build map of coin_id → trading symbol for all tradeable assets."""
    symbols = {}
    for coin_id in (CRYPTO_L1 + CRYPTO_L2 + CRYPTO_DEFI + CRYPTO_AI + CRYPTO_MEME):
        sym = CRYPTO_SYMBOLS.get(coin_id) or ASTER_SYMBOLS.get(coin_id)
        if sym:
            symbols[coin_id] = sym
    for asset_id in (STOCK_PERPS + COMMODITY_PERPS + INDEX_PERPS):
        sym = ASTER_SYMBOLS.get(asset_id)
        if sym:
            symbols[asset_id] = sym
    return symbols


def _parse_llm_signals(analysis: str, asset_map: dict[str, str]) -> list[dict]:
    """Parse LLM analysis text for trading signals.

    Looks for patterns like:
      BUY bitcoin — reason (strength: 0.8)
      SELL ethereum — reason (strength: 0.6)
    """
    signals = []
    if not analysis:
        return signals

    # Match: BUY/SELL [asset] — [reason] (strength: [0.X])
    pattern = r"(?:^|\n)\s*[-•*]?\s*(BUY|SELL)\s+(\S+).*?(?:strength:\s*([0-9.]+))?"
    for match in re.finditer(pattern, analysis, re.IGNORECASE):
        action = match.group(1).lower()
        asset_raw = match.group(2).lower().strip("*_[]().,")
        strength_str = match.group(3)
        strength = float(strength_str) if strength_str else 0.6

        # Map asset name to coin_id
        matched_id = None
        for coin_id in asset_map:
            if asset_raw in coin_id or coin_id in asset_raw:
                matched_id = coin_id
                break
            # Try matching by symbol (BTC, ETH, etc.)
            sym = asset_map[coin_id]
            sym_short = sym.replace("/USD", "").replace("USDT", "").lower()
            if asset_raw == sym_short or asset_raw == coin_id.replace("-", ""):
                matched_id = coin_id
                break

        if matched_id and matched_id in asset_map:
            signals.append({
                "coin_id": matched_id,
                "symbol": asset_map[matched_id],
                "action": action,
                "strength": min(max(strength, 0.0), 1.0),
                "context": match.group(0).strip(),
            })

    return signals


def _parse_asset_impacts(analysis: str, asset_map: dict[str, str]) -> dict[str, float]:
    """Parse LLM analysis for per-asset impact scores.

    Looks for patterns like:
      - Bitcoin: +0.7 (reason)
      - Ethereum: -0.3 (reason)
      - Impact score: 0.5
    """
    impacts = {}
    if not analysis:
        return impacts

    # Match: asset_name ... score (like +0.7 or -0.3)
    pattern = r"[-•*]\s*(\w[\w\s/-]*?)[:—]\s*([+-]?\d\.\d)"
    for match in re.finditer(pattern, analysis):
        asset_raw = match.group(1).lower().strip()
        score = float(match.group(2))

        for coin_id in asset_map:
            if asset_raw in coin_id or coin_id in asset_raw:
                impacts[coin_id] = score
                break

    return impacts


@register
class NewsSentimentStrategy(Strategy):
    """Generate trading signals from LLM-interpreted news headlines.

    Pipeline:
    1. Fetch headlines from RSS (6 categories), GDELT, crypto API, Finnhub
    2. Send to Gemini for semantic analysis of market impact
    3. Parse LLM response for per-asset signals
    4. Return Signal objects for the aggregator
    """

    name = "news_sentiment"

    def __init__(self):
        self._last_context = {}
        self._last_analysis = ""
        self._asset_map = _get_all_tradeable_symbols()

    def generate_signals(self) -> list[Signal]:
        signals = []

        # Step 1: Fetch all headlines
        try:
            from trading.data.news import fetch_all_headlines
            headlines = fetch_all_headlines(max_per_source=8)
        except Exception as e:
            log.warning("News sentiment: headline fetch failed: %s", e)
            return [Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0, reason="Headlines unavailable",
            )]

        if len(headlines) < 3:
            return [Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0, reason=f"Only {len(headlines)} headlines — insufficient",
            )]

        # Step 2: Get current positions and regime for context
        try:
            from trading.execution.router import get_positions_from_aster
            positions = get_positions_from_aster()[:15]
        except Exception:
            positions = []

        try:
            from trading.db.store import get_action_log
            recent_briefings = get_action_log(limit=3, category="intelligence")
            regime = "unknown"
            for b in recent_briefings:
                data = b.get("data", {})
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = {}
                if isinstance(data, dict) and data.get("overall_regime"):
                    regime = data["overall_regime"]
                    break
        except Exception:
            regime = "unknown"

        # Step 3: LLM analysis
        try:
            from trading.llm.engine import analyze_news_impact
            analysis = analyze_news_impact(
                headlines, positions, regime,
                list(self._asset_map.keys())[:30],
            )
            self._last_analysis = analysis
        except Exception as e:
            log.warning("News sentiment: LLM analysis failed: %s", e)
            return [Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0, reason=f"LLM analysis failed: {e}",
            )]

        if not analysis or "LLM unavailable" in analysis:
            return [Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0, reason="LLM unavailable for news analysis",
            )]

        # Step 4: Parse signals from LLM response
        parsed_signals = _parse_llm_signals(analysis, self._asset_map)
        asset_impacts = _parse_asset_impacts(analysis, self._asset_map)

        self._last_context = {
            "headline_count": len(headlines),
            "sources": len(set(h.get("source", "") for h in headlines)),
            "regime": regime,
            "asset_impacts": asset_impacts,
            "signal_count": len(parsed_signals),
        }

        now = time.monotonic()
        signal_count = 0
        max_signals = 5

        for ps in parsed_signals:
            if signal_count >= max_signals:
                break

            # Cooldown check
            last_time = _last_signal_time.get(ps["symbol"], 0)
            if now - last_time < _COOLDOWN_SECONDS:
                continue

            # Only emit signals with sufficient strength
            if ps["strength"] < 0.4:
                continue

            _last_signal_time[ps["symbol"]] = now
            signal_count += 1

            signals.append(Signal(
                strategy=self.name,
                symbol=ps["symbol"],
                action=ps["action"],
                strength=ps["strength"],
                reason=f"News sentiment {ps['action']} — {ps['context'][:120]}",
                data={
                    "coin": ps["coin_id"],
                    "sentiment_strength": ps["strength"],
                    "headline_count": len(headlines),
                },
            ))

        # If no actionable signals, emit hold for BTC
        if not signals:
            signals.append(Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0,
                reason=f"No high-conviction news signals from {len(headlines)} headlines",
            ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self._last_context,
        }
