"""News Sentiment Strategy — LLM-interpreted headlines driving trading signals.

Fetches headlines from RSS, GDELT, and crypto news APIs. Calls
analyze_news_impact() which returns a structured JSON dict (via Claude
reasoning tier). Signals are parsed directly from the JSON — no regex.

Deduplication: results are cached for 55 minutes so the briefing cycle and
this strategy don't make two identical Claude calls in the same cycle.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from trading.config import (
    BYBIT_SYMBOLS,
    CRYPTO_L1, CRYPTO_L2, CRYPTO_DEFI, CRYPTO_AI, CRYPTO_MEME,
    STOCK_PERPS, COMMODITY_PERPS, INDEX_PERPS,
)
from trading.strategy.base import Signal, Strategy
from trading.strategy.registry import register

log = logging.getLogger(__name__)

# Per-asset cooldown — don't re-signal same asset within 1 hour
_last_signal_time: dict[str, float] = {}
_signal_time_lock = threading.Lock()
_COOLDOWN_SECONDS = 3600

# Hard cap: max signals emitted from a single news batch (guards against
# LLM over-enthusiasm or prompt-injection attempting mass trades)
_MAX_SIGNALS_PER_CYCLE = 3

# Analysis result cache — shared with briefing cycle to avoid duplicate LLM calls
_analysis_cache: dict = {}
_analysis_cache_time: float = 0.0
_ANALYSIS_CACHE_TTL = 3300  # 55 minutes


def _get_all_tradeable_symbols() -> dict[str, str]:
    """Build map of coin_id → trading symbol for all tradeable assets."""
    symbols = {}
    for coin_id in (CRYPTO_L1 + CRYPTO_L2 + CRYPTO_DEFI + CRYPTO_AI + CRYPTO_MEME):
        sym = BYBIT_SYMBOLS.get(coin_id)
        if sym:
            symbols[coin_id] = sym
    for asset_id in (STOCK_PERPS + COMMODITY_PERPS + INDEX_PERPS):
        sym = BYBIT_SYMBOLS.get(asset_id)
        if sym:
            symbols[asset_id] = sym
    return symbols


def _resolve_asset(raw: str, asset_map: dict[str, str]) -> str | None:
    """Resolve an asset name/symbol from LLM output to a coin_id in asset_map.

    Tries exact match, then case-insensitive, then substring. Returns coin_id or None.
    """
    if not raw:
        return None
    raw_lower = raw.lower().strip()

    # Build reverse lookup on first call
    name_to_id: dict[str, str] = {}
    for coin_id, sym in asset_map.items():
        name_to_id[coin_id.lower()] = coin_id
        name_to_id[sym.lower()] = coin_id
        # BTC/USD → btcusd, btc
        clean = sym.lower().replace("/usd", "").replace("usdt", "").replace("/", "")
        if clean:
            name_to_id[clean] = coin_id
        # bitcoin-cash → bitcoincash
        name_to_id[coin_id.lower().replace("-", "")] = coin_id

    # Exact match first
    if raw_lower in name_to_id:
        return name_to_id[raw_lower]

    # Substring match (prefer longer keys to avoid "bit" → "bitcoin-cash" vs "bitcoin")
    candidates = [(k, v) for k, v in name_to_id.items()
                  if raw_lower in k or k in raw_lower]
    if candidates:
        candidates.sort(key=lambda x: len(x[0]), reverse=True)
        return candidates[0][1]

    return None


def _get_cached_or_fresh_analysis(
    headlines: list[dict],
    positions: list[dict],
    regime: str,
    assets: list[str],
) -> dict:
    """Return cached analysis if fresh, otherwise call Claude."""
    global _analysis_cache, _analysis_cache_time
    now = time.monotonic()
    if _analysis_cache and (now - _analysis_cache_time) < _ANALYSIS_CACHE_TTL:
        age_min = (now - _analysis_cache_time) / 60
        log.debug("news_sentiment: using cached analysis (%.0fm old)", age_min)
        return _analysis_cache
    from trading.llm.engine import analyze_news_impact
    result = analyze_news_impact(headlines, positions, regime, assets)
    _analysis_cache = result
    _analysis_cache_time = now
    return result


def warm_analysis_cache(analysis: dict) -> None:
    """Called by the briefing cycle to pre-populate the cache.

    When the scheduler runs generate_briefing() it can call this so the
    news_sentiment strategy reuses the same Claude result without a second call.
    """
    global _analysis_cache, _analysis_cache_time
    if isinstance(analysis, dict) and analysis.get("model_used") == "llm":
        _analysis_cache = analysis
        _analysis_cache_time = time.monotonic()
        log.debug("news_sentiment: analysis cache warmed from briefing cycle")


@register
class NewsSentimentStrategy(Strategy):
    """Generate trading signals from LLM-interpreted news headlines.

    Pipeline:
    1. Fetch headlines (RSS, GDELT, crypto API, Finnhub)
    2. Call analyze_news_impact() via Claude (or use briefing cache)
    3. Parse structured JSON signals — no regex
    4. Return Signal objects with full attribution data
    """

    name = "news_sentiment"

    def __init__(self):
        self._last_context: dict = {}
        self._asset_map = _get_all_tradeable_symbols()

    def generate_signals(self) -> list[Signal]:
        # Step 1: Fetch headlines
        try:
            from trading.data.news import fetch_all_headlines
            headlines = fetch_all_headlines(max_per_source=8)
        except Exception as e:
            log.warning("news_sentiment: headline fetch failed: %s", e)
            return [Signal(strategy=self.name, symbol="BTC/USD", action="hold",
                           strength=0.0, reason="Headlines unavailable")]

        if len(headlines) < 3:
            return [Signal(strategy=self.name, symbol="BTC/USD", action="hold",
                           strength=0.0,
                           reason=f"Only {len(headlines)} headlines — insufficient for analysis")]

        # Step 2: Current positions and regime
        try:
            from trading.execution.router import get_positions_from_bybit
            positions = get_positions_from_bybit()[:20]
        except Exception:
            positions = []

        regime = "unknown"
        try:
            from trading.db.store import get_action_log
            import json as _json
            for b in get_action_log(limit=3, category="intelligence"):
                data = b.get("data", {})
                if isinstance(data, str):
                    try:
                        data = _json.loads(data)
                    except Exception:
                        data = {}
                if isinstance(data, dict) and data.get("overall_regime"):
                    regime = data["overall_regime"]
                    break
        except Exception:
            pass

        # Step 3: Get structured analysis (cached or fresh Claude call)
        try:
            analysis = _get_cached_or_fresh_analysis(
                headlines, positions, regime,
                list(self._asset_map.keys())[:40],
            )
        except Exception as e:
            log.warning("news_sentiment: analysis failed: %s", e)
            return [Signal(strategy=self.name, symbol="BTC/USD", action="hold",
                           strength=0.0, reason=f"Analysis error: {e}")]

        # Step 4: Parse signals directly from JSON structure
        raw_signals = analysis.get("signals", [])
        asset_impacts = analysis.get("asset_impacts", {})
        key_events = analysis.get("key_events", [])
        risk_alerts = analysis.get("risk_alerts", [])

        self._last_context = {
            "headline_count": len(headlines),
            "headlines_used": analysis.get("headline_count_used", 0),
            "stale_excluded": analysis.get("stale_excluded", 0),
            "sources": len({h.get("source", "") for h in headlines}),
            "regime": regime,
            "asset_impacts": {k: v.get("score", v) if isinstance(v, dict) else v
                              for k, v in asset_impacts.items()},
            "signal_count": len(raw_signals),
            "key_events": [e.get("headline", "") for e in key_events[:3]],
            "risk_alerts": [a.get("alert", "") for a in risk_alerts],
            "model_used": analysis.get("model_used", "unknown"),
        }

        now = time.monotonic()
        signals: list[Signal] = []

        # Sort by strength descending — emit highest-conviction signals first
        sorted_signals = sorted(raw_signals, key=lambda s: s.get("strength", 0), reverse=True)

        for raw in sorted_signals:
            if len(signals) >= _MAX_SIGNALS_PER_CYCLE:
                log.info("news_sentiment: max signal cap (%d) reached — dropping remainder", _MAX_SIGNALS_PER_CYCLE)
                break

            action = str(raw.get("action", "")).lower().strip()
            if action not in ("buy", "sell"):
                continue

            strength = float(raw.get("strength", 0.0))
            strength = max(0.0, min(1.0, strength))  # clamp to valid range
            if strength < 0.5:
                continue

            asset_raw = str(raw.get("asset", ""))
            coin_id = _resolve_asset(asset_raw, self._asset_map)
            if not coin_id or coin_id not in self._asset_map:
                log.debug("news_sentiment: unresolved asset '%s' — skipping", asset_raw)
                continue

            symbol = self._asset_map[coin_id]

            # Per-asset cooldown — thread-safe
            with _signal_time_lock:
                last_time = _last_signal_time.get(symbol, 0)
                if now - last_time < _COOLDOWN_SECONDS:
                    log.debug("news_sentiment: %s on cooldown — skipping", symbol)
                    continue
                _last_signal_time[symbol] = now

            # Enrich attribution data
            impact_data = asset_impacts.get(asset_raw, asset_impacts.get(coin_id, {}))
            impact_score = (impact_data.get("score", strength) if isinstance(impact_data, dict)
                            else float(impact_data) if impact_data else strength)
            impact_reason = (impact_data.get("reason", "") if isinstance(impact_data, dict) else "")
            impact_headline_count = (impact_data.get("headline_count", 1)
                                     if isinstance(impact_data, dict) else 1)

            signals.append(Signal(
                strategy=self.name,
                symbol=symbol,
                action=action,
                strength=strength,
                reason=f"News {action.upper()} {asset_raw}: {raw.get('reason', '')[:120]}",
                data={
                    "coin": coin_id,
                    "sentiment_strength": strength,
                    "impact_score": impact_score,
                    "impact_reason": impact_reason,
                    "headline_count": len(headlines),
                    "supporting_headlines": impact_headline_count,
                    "time_horizon": raw.get("time_horizon", "day"),
                    "key_events": [e.get("headline", "") for e in key_events[:2]],
                    "risk_alerts": [a.get("alert", "") for a in risk_alerts
                                    if a.get("asset", "").lower() in (coin_id.lower(), "portfolio")],
                    "regime": regime,
                    "model_used": analysis.get("model_used", "unknown"),
                },
            ))

        if not signals:
            event_summary = (f" Key: {key_events[0]['headline'][:60]}" if key_events else "")
            signals.append(Signal(
                strategy=self.name, symbol="BTC/USD", action="hold",
                strength=0.0,
                reason=f"No high-conviction signals from {len(headlines)} headlines.{event_summary}",
            ))

        return signals

    def get_market_context(self) -> dict:
        return {
            "strategy": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self._last_context,
        }
