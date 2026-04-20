"""Market Intelligence Engine — scores market categories from news and data.

Produces a MarketBriefing before each trading cycle with per-category
scores (-1.0 to +1.0), headline summaries, event risk flags, and
confidence levels. Strategies consume this as context, not as signals.

Categories:
  - crypto: Overall crypto market health
  - commodities: Precious metals and commodity conditions
  - currency: Dollar strength and forex environment
  - macro: Cross-asset risk regime
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trading.data.news import fetch_all_category_data

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Headline keyword scoring — maps words to sentiment weight per category
# ---------------------------------------------------------------------------

_CRYPTO_BULLISH = [
    "etf approved", "etf inflow", "institutional", "adoption", "bullish",
    "all-time high", "ath", "rally", "surge", "upgrade", "partnership",
    "halving", "accumulation", "whale buy", "treasury", "reserve",
    "approval", "mainstream", "breakout",
]
_CRYPTO_BEARISH = [
    "sec sue", "regulation crack", "ban", "hack", "exploit", "depeg",
    "crash", "plunge", "selloff", "sell-off", "bearish", "liquidat",
    "fud", "rug pull", "ponzi", "fraud", "investigation", "subpoena",
    "exchange collapse", "insolvency", "withdraw halt", "freeze",
]

_COMMODITY_BULLISH = [
    "central bank buy", "gold demand", "safe haven", "inflation hedge",
    "geopolitical", "supply disruption", "shortage", "rally", "surge",
    "record high", "bullish",
]
_COMMODITY_BEARISH = [
    "sell gold", "reserve sale", "strong dollar", "deflation",
    "demand weak", "surplus", "bearish", "plunge", "crash",
]

_MACRO_RISK_ON = [
    "rate cut", "dovish", "stimulus", "easing", "soft landing",
    "growth", "expansion", "rally", "risk-on", "optimism",
    "employment strong", "beat expectations",
]
_MACRO_RISK_OFF = [
    "rate hike", "hawkish", "tightening", "recession", "hard landing",
    "contraction", "risk-off", "fear", "volatility spike",
    "inverted yield", "crisis", "default", "shutdown",
    "miss expectations", "weak data",
]


def _score_headlines(headlines: list[dict], bullish_kw: list[str],
                     bearish_kw: list[str]) -> tuple[float, list[str]]:
    """Score a set of headlines using keyword matching.

    Returns (score, top_headlines) where score is -1.0 to 1.0.
    """
    if not headlines:
        return 0.0, []

    bull_count = 0
    bear_count = 0
    top_headlines = []

    for h in headlines:
        title = h.get("title", "").lower()
        is_bull = any(kw in title for kw in bullish_kw)
        is_bear = any(kw in title for kw in bearish_kw)

        if is_bull:
            bull_count += 1
            top_headlines.append(f"[+] {h['title']}")
        if is_bear:
            bear_count += 1
            top_headlines.append(f"[-] {h['title']}")

    total = bull_count + bear_count
    if total == 0:
        return 0.0, []

    # Net sentiment: -1.0 (all bearish) to +1.0 (all bullish)
    raw = (bull_count - bear_count) / total
    # Dampen: news is noisy, never give full conviction from headlines alone
    score = raw * 0.5

    return round(score, 3), top_headlines[:5]


# ---------------------------------------------------------------------------
# Category scoring functions
# ---------------------------------------------------------------------------

def _score_crypto(data: dict) -> dict:
    """Score the crypto market category.

    Inputs: Fear & Greed, CoinGecko global metrics, crypto headlines.
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Fear & Greed (strongest signal for crypto sentiment)
    fg = data.get("fear_greed")
    if fg:
        fg_value = fg.get("value", 50)
        # Map 0-100 to -1.0 to +1.0, centered at 50
        fg_score = (fg_value - 50) / 50.0
        score += fg_score * 0.4  # 40% weight
        confidence += 0.4
        components.append(f"F&G={fg_value} ({fg.get('classification', '?')})")

    # CoinGecko global metrics
    cg = data.get("crypto_global")
    if cg:
        mc_change = cg.get("market_cap_change_24h_pct", 0)
        # Map +-10% change to +-0.3 score
        cg_score = max(min(mc_change / 10.0, 1.0), -1.0)
        score += cg_score * 0.3  # 30% weight
        confidence += 0.3
        btc_dom = cg.get("btc_dominance", 0)
        components.append(f"MCap 24h {mc_change:+.1f}%, BTC dom {btc_dom:.1f}%")

    # AsterDex derivatives data (funding rates + order flow)
    try:
        from trading.data.aster import get_aster_market_summary
        aster = get_aster_market_summary()
        if aster:
            # Funding sentiment: negative funding = overleveraged shorts = bullish
            funding_sent = aster.get("funding_sentiment", 0)
            if funding_sent != 0:
                funding_score = -max(min(funding_sent * 100, 1.0), -1.0) * 0.15
                score += funding_score
                confidence += 0.1
                components.append(f"AsterDex funding={funding_sent*100:+.3f}%")

            # Volume flow: net taker buy/sell pressure
            vol_flow = aster.get("volume_flow", 0)
            if vol_flow != 0:
                flow_score = max(min(vol_flow, 1.0), -1.0) * 0.1
                score += flow_score
                confidence += 0.05
                components.append(f"Taker flow={vol_flow:+.2f}")
    except Exception:
        pass  # AsterDex data is supplementary, never block

    # Reddit social sentiment (15% weight)
    try:
        from trading.data.social import get_social_sentiment_summary
        social = get_social_sentiment_summary()
        composite_social = social.get("composite", 0.0)
        if social.get("active_coins", 0) > 0:
            score += composite_social * 0.15
            confidence += 0.15
            components.append(f"Reddit sentiment={composite_social:+.2f}")
    except Exception:
        pass

    # Deribit options flow — put/call ratio + DVOL + skew (20% weight)
    try:
        from trading.data.options import get_options_market_data
        opts = get_options_market_data()
        opts_composite = opts.get("composite_signal", 0.0)
        btc_pcr = opts.get("btc", {}).get("put_call_ratio")
        score += opts_composite * 0.20
        confidence += 0.20
        pcr_str = f"P/C={btc_pcr:.2f}" if btc_pcr is not None else "P/C=n/a"
        components.append(f"Options {pcr_str} signal={opts_composite:+.2f}")
    except Exception:
        pass

    # Headlines
    headlines = data.get("headlines", {}).get("crypto", [])
    hl_score, top_hl = _score_headlines(headlines, _CRYPTO_BULLISH, _CRYPTO_BEARISH)
    if headlines:
        score += hl_score * 0.3  # 30% weight
        confidence += 0.3

    score = round(max(min(score, 1.0), -1.0), 3)
    confidence = round(min(confidence, 1.0), 2)

    return {
        "category": "crypto",
        "score": score,
        "confidence": confidence,
        "label": _score_label(score),
        "components": components,
        "top_headlines": top_hl,
    }


def _score_commodities(data: dict) -> dict:
    """Score the commodities/precious metals category.

    Inputs: commodity headlines, dollar strength (inverse relationship).
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Headlines
    headlines = data.get("headlines", {}).get("commodities", [])
    hl_score, top_hl = _score_headlines(headlines, _COMMODITY_BULLISH, _COMMODITY_BEARISH)
    if headlines:
        score += hl_score * 0.5
        confidence += 0.4

    # Dollar index (inverse: strong dollar = bearish commodities)
    macro = data.get("macro", {})
    dxy = macro.get("dxy_index")
    if dxy and dxy.get("change") is not None:
        dxy_change = dxy["change"]
        dxy_score = -max(min(dxy_change / 2.0, 1.0), -1.0)
        score += dxy_score * 0.3
        confidence += 0.3
        components.append(f"DXY={dxy['value']:.1f} (chg {dxy_change:+.2f})")

    # Inflation data (rising CPI = bullish for gold)
    cpi = macro.get("cpi")
    if cpi and cpi.get("change") is not None:
        cpi_change = cpi["change"]
        cpi_score = max(min(cpi_change / 0.5, 1.0), -1.0)
        score += cpi_score * 0.2
        confidence += 0.2
        components.append(f"CPI={cpi['value']:.1f} ({cpi.get('period_name', '')} {cpi.get('year', '')})")

    score = round(max(min(score, 1.0), -1.0), 3)
    confidence = round(min(confidence, 1.0), 2)

    return {
        "category": "commodities",
        "score": score,
        "confidence": confidence,
        "label": _score_label(score),
        "components": components,
        "top_headlines": top_hl,
    }


def _score_currency(data: dict) -> dict:
    """Score the currency/dollar category.

    Inputs: FRED data (fed funds rate, DXY, yield curve).
    """
    score = 0.0
    components = []
    confidence = 0.0

    macro = data.get("macro", {})

    # Dollar index trend
    dxy = macro.get("dxy_index")
    if dxy and dxy.get("change") is not None:
        dxy_change = dxy["change"]
        dxy_score = max(min(dxy_change / 2.0, 1.0), -1.0)
        score += dxy_score * 0.4
        confidence += 0.4
        components.append(f"DXY={dxy['value']:.1f} (chg {dxy_change:+.2f})")

    # Fed funds rate
    ffr = macro.get("fed_funds_rate")
    if ffr:
        target = f"{ffr.get('target_low', 0):.2f}-{ffr.get('target_high', 0):.2f}%"
        components.append(f"Fed Funds={ffr['value']:.2f}% (target {target})")
        confidence += 0.2

    # Yield curve (10Y-2Y spread from Treasury)
    yields = macro.get("yield_curve")
    if yields:
        spread_val = yields["spread"]
        if spread_val < 0:
            components.append(f"Yield curve INVERTED ({spread_val:+.3f})")
            score -= 0.1
        else:
            components.append(f"Yield curve normal ({spread_val:+.3f})")
        confidence += 0.2

    score = round(max(min(score, 1.0), -1.0), 3)
    confidence = round(min(confidence, 1.0), 2)

    return {
        "category": "currency",
        "score": score,
        "confidence": confidence,
        "label": _score_label(score),
        "components": components,
        "top_headlines": [],
    }


def _score_macro(data: dict) -> dict:
    """Score the macro/cross-asset regime.

    Inputs: macro headlines, FRED data, economic calendar.
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Headlines
    headlines = data.get("headlines", {}).get("macro", [])
    hl_score, top_hl = _score_headlines(headlines, _MACRO_RISK_ON, _MACRO_RISK_OFF)
    if headlines:
        score += hl_score * 0.4
        confidence += 0.3

    # Unemployment trend
    macro = data.get("macro", {})
    unemp = macro.get("unemployment")
    if unemp and unemp.get("change") is not None:
        unemp_score = -max(min(unemp["change"] / 0.5, 1.0), -1.0)
        score += unemp_score * 0.3
        confidence += 0.3
        components.append(f"Unemployment={unemp['value']:.1f}% (chg {unemp['change']:+.1f})")

    # Yield curve as macro signal
    yields = macro.get("yield_curve")
    if yields:
        spread_val = yields["spread"]
        if spread_val < -0.2:
            score -= 0.2
            components.append(f"Yield curve deeply inverted ({spread_val:+.3f}) — recession risk")
        elif spread_val > 0.5:
            score += 0.1
            components.append(f"Yield curve normal ({spread_val:+.3f}) — expansion")
        confidence += 0.2

    # Event risk from calendar
    calendar = data.get("calendar", [])
    if calendar:
        critical = [e for e in calendar if e.get("impact") == "critical"]
        high = [e for e in calendar if e.get("impact") == "high"]
        if critical:
            components.append(f"EVENT RISK: {critical[0]['event']}")
        elif high:
            components.append(f"Upcoming: {high[0]['event']}")

    score = round(max(min(score, 1.0), -1.0), 3)
    confidence = round(min(confidence, 1.0), 2)

    return {
        "category": "macro",
        "score": score,
        "confidence": confidence,
        "label": _score_label(score),
        "components": components,
        "top_headlines": top_hl,
        "event_risk": [e["event"] for e in calendar] if calendar else [],
    }


def _score_label(score: float) -> str:
    """Convert numeric score to readable label."""
    if score >= 0.5:
        return "strongly bullish"
    elif score >= 0.2:
        return "bullish"
    elif score > -0.2:
        return "neutral"
    elif score > -0.5:
        return "bearish"
    else:
        return "strongly bearish"


# ---------------------------------------------------------------------------
# Main briefing
# ---------------------------------------------------------------------------

@dataclass
class MarketBriefing:
    """Pre-trade intelligence briefing across all market categories."""
    timestamp: str
    categories: dict[str, dict] = field(default_factory=dict)
    event_risk: list[str] = field(default_factory=list)
    overall_regime: str = "neutral"
    overall_score: float = 0.0
    news_interpretation: str = ""       # LLM analysis of headlines
    asset_impacts: dict = field(default_factory=dict)  # {coin_id: score}

    def category_score(self, category: str) -> float:
        """Get score for a specific category, defaulting to 0.0."""
        return self.categories.get(category, {}).get("score", 0.0)

    def has_event_risk(self) -> bool:
        return len(self.event_risk) > 0

    def summary(self) -> str:
        """One-line summary for logging."""
        parts = []
        for cat, data in self.categories.items():
            parts.append(f"{cat}={data['score']:+.2f}")
        risk = " [EVENT RISK]" if self.event_risk else ""
        return f"Market: {' | '.join(parts)} → {self.overall_regime}{risk}"

    def to_dict(self) -> dict:
        """Serialize for storage/logging."""
        d = {
            "timestamp": self.timestamp,
            "categories": self.categories,
            "event_risk": self.event_risk,
            "overall_regime": self.overall_regime,
            "overall_score": self.overall_score,
        }
        if self.news_interpretation:
            d["news_interpretation"] = self.news_interpretation[:5000]
        if self.asset_impacts:
            d["asset_impacts"] = self.asset_impacts
        return d


def generate_briefing() -> MarketBriefing:
    """Generate a complete market intelligence briefing.

    Fetches all data sources, scores each category, determines overall
    regime, and returns a MarketBriefing for the trading cycle.
    """
    log.info("Generating market intelligence briefing...")

    # Fetch all raw data
    try:
        raw = fetch_all_category_data()
    except Exception as e:
        log.warning("Failed to fetch market data: %s", e)
        return MarketBriefing(
            timestamp=datetime.now(timezone.utc).isoformat(),
            overall_regime="unknown",
        )

    # Score each category
    crypto = _score_crypto(raw)
    commodities = _score_commodities(raw)
    currency = _score_currency(raw)
    macro = _score_macro(raw)

    categories = {
        "crypto": crypto,
        "commodities": commodities,
        "currency": currency,
        "macro": macro,
    }

    # Collect event risk across categories
    event_risk = macro.get("event_risk", [])

    # Overall regime: weighted average
    # Macro gets highest weight as it affects everything
    weights = {"crypto": 0.25, "commodities": 0.20, "currency": 0.20, "macro": 0.35}
    overall = sum(
        categories[cat]["score"] * w
        for cat, w in weights.items()
    )
    overall = round(overall, 3)
    regime = _score_label(overall)

    # Event risk dampens confidence in any direction
    if event_risk:
        regime = f"{regime} (event risk)"

    briefing = MarketBriefing(
        timestamp=datetime.now(timezone.utc).isoformat(),
        categories=categories,
        event_risk=event_risk,
        overall_regime=regime,
        overall_score=overall,
    )

    # LLM news interpretation — non-fatal, enriches briefing with semantic analysis
    try:
        from trading.data.news import fetch_all_headlines
        from trading.llm.engine import analyze_news_impact
        from trading.config import CRYPTO_SYMBOLS, ASTER_SYMBOLS

        all_headlines = fetch_all_headlines(max_per_source=5)
        if len(all_headlines) >= 3:
            traded_assets = list(set(list(CRYPTO_SYMBOLS.keys()) + list(ASTER_SYMBOLS.keys())))
            interpretation = analyze_news_impact(
                all_headlines, [], regime, traded_assets[:30],
            )
            if interpretation and "LLM unavailable" not in interpretation:
                briefing.news_interpretation = interpretation
                # Parse per-asset impacts from interpretation
                from trading.strategy.news_sentiment import _parse_asset_impacts
                asset_map = {}
                for k, v in CRYPTO_SYMBOLS.items():
                    asset_map[k] = v
                for k, v in ASTER_SYMBOLS.items():
                    asset_map[k] = v
                briefing.asset_impacts = _parse_asset_impacts(interpretation, asset_map)
                log.info("News interpretation: %d asset impacts parsed", len(briefing.asset_impacts))
    except Exception as e:
        log.debug("LLM news interpretation failed (non-fatal): %s", e)

    log.info("Intelligence briefing: %s", briefing.summary())
    return briefing
