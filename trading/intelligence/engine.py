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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

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

    Weights (sum to 1.0 when all sources present):
      F&G=0.30, CoinGecko=0.20, AsterDex funding=0.10,
      AsterDex flow=0.05, Social=0.10, Options=0.15, Headlines=0.10
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Fear & Greed (strongest signal for crypto sentiment) — 30% weight
    fg = data.get("fear_greed")
    if fg:
        fg_value = fg.get("value", 50)
        # Map 0-100 to -1.0 to +1.0, centered at 50
        fg_score = (fg_value - 50) / 50.0
        score += fg_score * 0.30
        confidence += 0.30
        components.append(f"F&G={fg_value} ({fg.get('classification', '?')})")

    # CoinGecko global metrics — 20% weight
    cg = data.get("crypto_global")
    if cg:
        mc_change = cg.get("market_cap_change_24h_pct", 0)
        # Map +-10% change to +-1.0 score
        cg_score = max(min(mc_change / 10.0, 1.0), -1.0)
        score += cg_score * 0.20
        confidence += 0.20
        btc_dom = cg.get("btc_dominance", 0)
        components.append(f"MCap 24h {mc_change:+.1f}%, BTC dom {btc_dom:.1f}%")

    # AsterDex derivatives data (funding rates + order flow) — 15% weight total
    try:
        from trading.data.aster import get_aster_market_summary
        aster = get_aster_market_summary()
        if aster:
            # Funding sentiment: negative funding = overleveraged shorts = bullish
            funding_sent = aster.get("funding_sentiment", 0)
            if funding_sent != 0:
                funding_score = -max(min(funding_sent * 100, 1.0), -1.0) * 0.10
                score += funding_score
                confidence += 0.10
                components.append(f"AsterDex funding={funding_sent*100:+.3f}%")

            # Volume flow: net taker buy/sell pressure — 5% weight
            vol_flow = aster.get("volume_flow", 0)
            if vol_flow != 0:
                flow_score = max(min(vol_flow, 1.0), -1.0) * 0.05
                score += flow_score
                confidence += 0.05
                components.append(f"Taker flow={vol_flow:+.2f}")
    except Exception:
        pass  # AsterDex data is supplementary, never block

    # Reddit social sentiment — 10% weight
    try:
        from trading.data.social import get_social_sentiment_summary
        social = get_social_sentiment_summary()
        composite_social = social.get("composite", 0.0)
        if social.get("active_coins", 0) > 0:
            score += composite_social * 0.10
            confidence += 0.10
            components.append(f"Reddit sentiment={composite_social:+.2f}")
    except Exception:
        pass

    # Deribit options flow — put/call ratio + DVOL + skew — 15% weight
    try:
        from trading.data.options import get_options_market_data
        opts = get_options_market_data()
        opts_composite = opts.get("composite_signal", 0.0)
        btc_pcr = opts.get("btc", {}).get("put_call_ratio")
        score += opts_composite * 0.15
        confidence += 0.15
        pcr_str = f"P/C={btc_pcr:.2f}" if btc_pcr is not None else "P/C=n/a"
        components.append(f"Options {pcr_str} signal={opts_composite:+.2f}")
    except Exception:
        pass

    # Headlines — 8% weight
    headlines = data.get("headlines", {}).get("crypto", [])
    hl_score, top_hl = _score_headlines(headlines, _CRYPTO_BULLISH, _CRYPTO_BEARISH)
    if headlines:
        score += hl_score * 0.08
        confidence += 0.08

    # Finnhub social sentiment (Reddit + Twitter for COIN, MSTR, BTC) — 7% weight
    try:
        social = data.get("finnhub_social_sentiment", {})
        if social:
            sentiments = []
            for sym_data in social.values():
                r = sym_data.get("reddit_sentiment", 0)
                t = sym_data.get("twitter_sentiment", 0)
                if r or t:
                    sentiments.append((r + t) / 2 if (r and t) else (r or t))
            if sentiments:
                avg_social = sum(sentiments) / len(sentiments)
                score += avg_social * 0.07
                confidence += 0.07
                # Mention volume as a separate signal: spike = volatility incoming
                total_mentions = sum(
                    d.get("reddit_mentions", 0) + d.get("twitter_mentions", 0)
                    for d in social.values()
                )
                components.append(f"Social sent={avg_social:+.2f} ({total_mentions:,} mentions)")
    except Exception:
        pass

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

    Weights (sum to 1.0 when all sources present):
      Headlines=0.50, DXY=0.30, CPI=0.20
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Headlines — 50% weight
    headlines = data.get("headlines", {}).get("commodities", [])
    hl_score, top_hl = _score_headlines(headlines, _COMMODITY_BULLISH, _COMMODITY_BEARISH)
    if headlines:
        score += hl_score * 0.50
        confidence += 0.50

    # Dollar index (inverse: strong dollar = bearish commodities) — 30% weight
    macro = data.get("macro", {})
    dxy = macro.get("dxy_index")
    if dxy and dxy.get("change") is not None:
        dxy_change = dxy["change"]
        dxy_score = -max(min(dxy_change / 2.0, 1.0), -1.0)
        score += dxy_score * 0.30
        confidence += 0.30
        components.append(f"DXY={dxy['value']:.1f} (chg {dxy_change:+.2f})")

    # Inflation data (rising CPI = bullish for gold) — 20% weight
    cpi = macro.get("cpi")
    if cpi and cpi.get("change") is not None:
        cpi_change = cpi["change"]
        cpi_score = max(min(cpi_change / 0.5, 1.0), -1.0)
        score += cpi_score * 0.20
        confidence += 0.20
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

    Weights (sum to 1.0 when all sources present):
      DXY=0.50, Fed Funds=0.20, Yield curve=0.30
    """
    score = 0.0
    components = []
    confidence = 0.0

    macro = data.get("macro", {})

    # Dollar index trend — 50% weight
    dxy = macro.get("dxy_index")
    if dxy and dxy.get("change") is not None:
        dxy_change = dxy["change"]
        dxy_score = max(min(dxy_change / 2.0, 1.0), -1.0)
        score += dxy_score * 0.50
        confidence += 0.50
        components.append(f"DXY={dxy['value']:.1f} (chg {dxy_change:+.2f})")

    # Fed funds rate — 20% weight (informational, higher rate = stronger dollar)
    ffr = macro.get("fed_funds_rate")
    if ffr:
        target = f"{ffr.get('target_low', 0):.2f}-{ffr.get('target_high', 0):.2f}%"
        components.append(f"Fed Funds={ffr['value']:.2f}% (target {target})")
        confidence += 0.20

    # Yield curve (10Y-2Y spread from Treasury) — 30% weight
    yields = macro.get("yield_curve")
    if yields:
        spread_val = yields["spread"]
        # Inverted curve signals tightening / dollar strength concern
        yc_score = max(min(spread_val / 1.0, 1.0), -1.0) * 0.30
        score += yc_score
        if spread_val < 0:
            components.append(f"Yield curve INVERTED ({spread_val:+.3f})")
        else:
            components.append(f"Yield curve normal ({spread_val:+.3f})")
        confidence += 0.30

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

    Inputs: macro headlines, FRED data (CPI, FEDFUNDS, T10Y2Y, UNRATE),
            Finnhub equity sentiment, economic calendar.

    Weights when all sources present:
      Headlines=0.25, Unemployment=0.20, Yield curve=0.20,
      CPI trend=0.15, Fed Funds=0.10, Finnhub equity sentiment=0.10
    """
    score = 0.0
    components = []
    confidence = 0.0

    # Headlines — 25% weight
    headlines = data.get("headlines", {}).get("macro", [])
    hl_score, top_hl = _score_headlines(headlines, _MACRO_RISK_ON, _MACRO_RISK_OFF)
    if headlines:
        score += hl_score * 0.25
        confidence += 0.25

    macro = data.get("macro", {})

    # Unemployment — prefer FRED UNRATE over BLS, 20% weight
    unemp = macro.get("unrate_fred") or macro.get("unemployment")
    if unemp and unemp.get("change") is not None:
        unemp_score = -max(min(unemp["change"] / 0.5, 1.0), -1.0)
        score += unemp_score * 0.20
        confidence += 0.20
        components.append(f"Unemployment={unemp['value']:.1f}% (chg {unemp['change']:+.2f})")

    # Yield curve — prefer FRED T10Y2Y over Treasury XML, 20% weight
    yc_fred = macro.get("yield_spread_fred")
    yields = macro.get("yield_curve")
    if yc_fred is not None:
        spread_val = yc_fred["value"]
        yc_score = max(min(spread_val / 1.0, 1.0), -1.0) * 0.20
        score += yc_score
        confidence += 0.20
        label = "inverted — recession risk" if spread_val < -0.2 else ("normal" if spread_val > 0.5 else "flat")
        components.append(f"10Y-2Y spread={spread_val:+.3f} ({label}) [FRED]")
    elif yields:
        spread_val = yields["spread"]
        yc_score = max(min(spread_val / 1.0, 1.0), -1.0) * 0.20
        score += yc_score
        confidence += 0.20
        components.append(f"Yield curve={spread_val:+.3f}")

    # CPI trend — rising inflation = hawkish Fed = risk-off for crypto, 15% weight
    cpi = macro.get("cpi_fred")
    if cpi and cpi.get("change") is not None:
        # Monthly CPI change: +0.4% = hot (risk-off), -0.1% = cooling (risk-on)
        cpi_score = -max(min(cpi["change"] / 0.5, 1.0), -1.0)
        score += cpi_score * 0.15
        confidence += 0.15
        trend = "hot" if cpi["change"] > 0.3 else ("cooling" if cpi["change"] < 0 else "moderate")
        components.append(f"CPI={cpi['value']:.1f} (MoM {cpi['change']:+.3f}, {trend}) [FRED]")

    # Fed Funds rate level — high rate = risk-off headwind, 10% weight
    fedfunds = macro.get("fedfunds_fred") or macro.get("fed_funds_rate")
    if fedfunds:
        rate = fedfunds.get("value", 0)
        # >4% = restrictive (risk-off), <2% = accommodative (risk-on)
        ffr_score = -max(min((rate - 2.5) / 2.5, 1.0), -1.0)
        score += ffr_score * 0.10
        confidence += 0.10
        components.append(f"Fed Funds={rate:.2f}% [FRED]")

    # Finnhub equity sentiment for COIN, MSTR, IBIT — 10% weight
    fh_sentiment = data.get("finnhub_sentiment", {})
    if fh_sentiment:
        avg_sentiment = sum(fh_sentiment.values()) / len(fh_sentiment)
        score += avg_sentiment * 0.10
        confidence += 0.10
        sym_parts = [f"{s}:{v:+.2f}" for s, v in fh_sentiment.items()]
        components.append(f"Equity sentiment: {', '.join(sym_parts)}")

    # Finnhub economic calendar surprises — 5% weight
    # Positive surprise (economy beating estimates) = risk-on
    try:
        fh_econ_cal = data.get("finnhub_economic_calendar", [])
        surprises = [e["surprise_pct"] for e in fh_econ_cal if e.get("surprise_pct") is not None]
        if surprises:
            avg_surprise = sum(surprises) / len(surprises)
            surprise_score = max(min(avg_surprise / 10.0, 1.0), -1.0)
            score += surprise_score * 0.05
            confidence += 0.05
            direction = "beat" if avg_surprise > 0 else "miss"
            components.append(f"Econ surprise={avg_surprise:+.1f}% avg ({direction}, {len(surprises)} events)")
    except Exception:
        pass

    # Finnhub earnings surprises for COIN/MSTR — 5% weight
    try:
        earnings = data.get("finnhub_earnings_calendar", [])
        reported = [e for e in earnings if e.get("eps_actual") is not None and e.get("surprise_pct") is not None]
        if reported:
            avg_eps_surprise = sum(e["surprise_pct"] for e in reported) / len(reported)
            eps_score = max(min(avg_eps_surprise / 20.0, 1.0), -1.0)
            score += eps_score * 0.05
            confidence += 0.05
            sym_parts = [f"{e['symbol']} {e['surprise_pct']:+.0f}%" for e in reported]
            components.append(f"EPS surprise: {', '.join(sym_parts)}")
        # Upcoming earnings = volatility warning
        upcoming = [e for e in earnings if e.get("eps_actual") is None]
        if upcoming:
            names = list({e["symbol"] for e in upcoming})
            components.append(f"Earnings due: {', '.join(names)} — expect volatility")
    except Exception:
        pass

    # Finnhub insider transactions — 4% weight
    # Net insider buying at COIN/MSTR = bullish signal; selling = bearish
    try:
        insider = data.get("finnhub_insider_transactions", {})
        net_buys = 0
        total_txns = 0
        for sym_txns in insider.values():
            buys = sum(1 for t in sym_txns if t.get("action") == "buy")
            sells = sum(1 for t in sym_txns if t.get("action") == "sell")
            net_buys += buys - sells
            total_txns += buys + sells
        if total_txns > 0:
            insider_score = max(min(net_buys / max(total_txns, 5), 1.0), -1.0)
            score += insider_score * 0.04
            confidence += 0.04
            direction = "buying" if net_buys > 0 else "selling"
            components.append(f"Insider net={net_buys:+d} ({direction}, {total_txns} txns)")
    except Exception:
        pass

    # Finnhub analyst recommendations — 4% weight
    # Strong analyst consensus on COIN/MSTR carries forward sentiment
    try:
        recs = data.get("finnhub_recommendations", {})
        if recs:
            avg_net = sum(r.get("net_score", 0) for r in recs.values()) / len(recs)
            score += avg_net * 0.04
            confidence += 0.04
            sym_parts = [f"{s}={r.get('net_score', 0):+.2f}" for s, r in recs.items()]
            components.append(f"Analyst consensus: {', '.join(sym_parts)}")
    except Exception:
        pass

    # Finnhub institutional BTC ETF ownership — 2% weight (quarterly, slow signal)
    try:
        inst = data.get("finnhub_institutional_ownership", {})
        if inst:
            changes = [v.get("change_pct", 0) for v in inst.values() if v.get("change_pct") is not None]
            if changes:
                avg_change = sum(changes) / len(changes)
                inst_score = max(min(avg_change / 10.0, 1.0), -1.0)
                score += inst_score * 0.02
                confidence += 0.02
                direction = "increasing" if avg_change > 0 else "decreasing"
                sym_parts = [f"{s}: {v.get('change_pct', 0):+.1f}%" for s, v in inst.items()]
                components.append(f"BTC ETF holdings {direction}: {', '.join(sym_parts)}")
    except Exception:
        pass

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
# Freshness tracking
# ---------------------------------------------------------------------------

def _compute_headline_freshness(headlines_by_category: dict, window_minutes: int = 60) -> dict[str, float]:
    """Compute freshness score for each news category.

    Freshness is the fraction of headlines published within *window_minutes*
    of the current UTC time.  Returns a mapping of category → 0.0-1.0.

    Args:
        headlines_by_category: dict keyed by category name, values are lists
            of headline dicts that may contain a ``published_at`` ISO string.
        window_minutes: age threshold in minutes (default 60).

    Returns:
        Dict mapping category name → freshness fraction (0.0 when no
        headlines carry parseable timestamps).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    result: dict[str, float] = {}

    for category, headlines in headlines_by_category.items():
        if not headlines:
            result[category] = 0.0
            continue

        fresh = 0
        dated = 0
        for h in headlines:
            raw_ts = h.get("published_at") or h.get("publishedAt") or h.get("timestamp")
            if not raw_ts:
                continue
            dated += 1
            try:
                # Accept ISO strings with or without timezone suffix
                ts_str = str(raw_ts).rstrip("Z")
                if "+" not in ts_str and ts_str.count("-") < 3:
                    ts_str += "+00:00"
                pub = datetime.fromisoformat(ts_str)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub >= cutoff:
                    fresh += 1
            except (ValueError, TypeError):
                pass

        result[category] = round(fresh / dated, 3) if dated > 0 else 0.0

    return result


# ---------------------------------------------------------------------------
# News burst detection
# ---------------------------------------------------------------------------

def detect_news_bursts(headlines_by_category: dict, window_minutes: int = 30,
                       burst_threshold: int = 5) -> list[dict]:
    """Detect unusual concentrations of news (bursts) per category.

    A burst is declared when the number of headlines published within
    *window_minutes* of now exceeds *burst_threshold*.

    Args:
        headlines_by_category: dict keyed by category, values are lists of
            headline dicts that may contain a ``published_at`` ISO string.
        window_minutes: rolling window for counting recent headlines.
        burst_threshold: minimum recent-headline count to declare a burst.

    Returns:
        List of burst descriptors::

            [{"category": str, "recent_count": int, "window_minutes": int}, ...]

        Empty list when no bursts are detected.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    bursts: list[dict] = []

    for category, headlines in headlines_by_category.items():
        recent = 0
        for h in headlines:
            raw_ts = h.get("published_at") or h.get("publishedAt") or h.get("timestamp")
            if not raw_ts:
                continue
            try:
                ts_str = str(raw_ts).rstrip("Z")
                if "+" not in ts_str and ts_str.count("-") < 3:
                    ts_str += "+00:00"
                pub = datetime.fromisoformat(ts_str)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub >= cutoff:
                    recent += 1
            except (ValueError, TypeError):
                pass

        if recent >= burst_threshold:
            bursts.append({
                "category": category,
                "recent_count": recent,
                "window_minutes": window_minutes,
            })

    return bursts


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
    news_interpretation: str = ""       # Human-readable LLM summary of headlines
    news_analysis: dict = field(default_factory=dict)  # Full structured dict from analyze_news_impact()
    asset_impacts: dict = field(default_factory=dict)  # {coin_id: score}
    headline_freshness: dict[str, float] = field(default_factory=dict)  # category → 0-1
    news_bursts: list[dict] = field(default_factory=list)  # detected burst events

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
            d["news_interpretation"] = str(self.news_interpretation)[:5000]
        if self.news_analysis:
            d["news_analysis"] = self.news_analysis
        if self.asset_impacts:
            d["asset_impacts"] = self.asset_impacts
        if self.headline_freshness:
            d["headline_freshness"] = self.headline_freshness
        if self.news_bursts:
            d["news_bursts"] = self.news_bursts
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

    # Overall regime: two-tier weighted average.
    # Macro (slow-moving FRED/calendar data) contributes 25% of the final score.
    # Real-time categories (crypto, commodities, currency) share the remaining 75%
    # with weights proportional to their market relevance (sum to 0.75).
    _MACRO_WEIGHT = 0.25
    _RT_WEIGHTS = {"crypto": 0.375, "commodities": 0.1875, "currency": 0.1875}
    # _RT_WEIGHTS sum: 0.375+0.1875+0.1875 = 0.75 ✓  total with macro: 1.0 ✓
    rt_score = sum(
        categories[cat]["score"] * w
        for cat, w in _RT_WEIGHTS.items()
    )
    overall = round(rt_score + categories["macro"]["score"] * _MACRO_WEIGHT, 3)
    regime = _score_label(overall)

    # Event risk dampens confidence in any direction
    if event_risk:
        regime = f"{regime} (event risk)"

    # Freshness and burst detection across all headline categories
    headlines_by_category: dict[str, list] = raw.get("headlines", {})
    freshness = _compute_headline_freshness(headlines_by_category)
    bursts = detect_news_bursts(headlines_by_category)
    if bursts:
        log.info("News bursts detected: %s", bursts)

    briefing = MarketBriefing(
        timestamp=datetime.now(timezone.utc).isoformat(),
        categories=categories,
        event_risk=event_risk,
        overall_regime=regime,
        overall_score=overall,
        headline_freshness=freshness,
        news_bursts=bursts,
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
            if isinstance(interpretation, dict) and interpretation.get("model_used") == "llm":
                briefing.news_analysis = interpretation
                # Pull structured asset_impacts directly — no regex parsing needed
                raw_impacts = interpretation.get("asset_impacts", {}) or {}
                briefing.asset_impacts = {
                    k: (v.get("score", 0.0) if isinstance(v, dict) else float(v))
                    for k, v in raw_impacts.items()
                }
                # Build a human-readable summary for logs and dashboard text display
                key_events = interpretation.get("key_events", []) or []
                risk_alerts = interpretation.get("risk_alerts", []) or []
                summary_parts = []
                for e in key_events[:3]:
                    if isinstance(e, dict) and e.get("headline"):
                        summary_parts.append(f"- {e['headline']}")
                for a in risk_alerts[:2]:
                    if isinstance(a, dict) and a.get("alert"):
                        summary_parts.append(f"RISK: {a['alert']}")
                briefing.news_interpretation = "\n".join(summary_parts)
                log.info("News interpretation: %d asset impacts, %d events, %d risk alerts",
                         len(briefing.asset_impacts), len(key_events), len(risk_alerts))
    except Exception as e:
        log.debug("LLM news interpretation failed (non-fatal): %s", e)

    log.info("Intelligence briefing: %s", briefing.summary())
    return briefing
