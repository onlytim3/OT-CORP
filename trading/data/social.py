"""Reddit social sentiment — r/CryptoCurrency, r/Bitcoin, r/ethereum.

Uses PRAW in read-only mode (no user auth): set REDDIT_CLIENT_ID and
REDDIT_CLIENT_SECRET env vars from apps.reddit.com (free, instant).
Falls back gracefully if PRAW is not installed or credentials are missing.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from trading.data.cache import cached

log = logging.getLogger(__name__)

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderAnalyzer
    _vader = _VaderAnalyzer()
    _VADER_AVAILABLE = True
except ImportError:
    _vader = None
    _VADER_AVAILABLE = False

SUBREDDITS = ["CryptoCurrency", "Bitcoin", "ethereum", "solana", "altcoin"]

BULLISH_KW = [
    "bull", "moon", "buy", "long", "breakout", "undervalued",
    "accumulate", "strong", "rally", "pump", "bullish", "upside",
    "support", "bounce", "recovery",
]
BEARISH_KW = [
    "bear", "crash", "sell", "short", "overvalued", "dump",
    "rug", "scam", "falling", "drop", "fear", "bearish", "downside",
    "resistance", "correction", "warning",
]

_COIN_KEYWORDS: dict[str, list[str]] = {
    "bitcoin": ["bitcoin", "btc", "satoshi"],
    "ethereum": ["ethereum", "eth", "ether"],
    "solana": ["solana", "sol"],
    "bnb": ["bnb", "binance coin", "binance smart chain"],
    "xrp": ["xrp", "ripple"],
}


def _praw_available() -> bool:
    try:
        import praw  # noqa: F401
        return True
    except ImportError:
        return False


def _get_reddit():
    client_id = os.getenv("REDDIT_CLIENT_ID", "")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    try:
        import praw
        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="ot-corp-sentiment/1.0 (trading bot)",
        )
    except Exception as e:
        log.warning("Reddit init failed: %s", e)
        return None


_NEGATION_WORDS = {"not", "no", "never", "neither", "nor", "cannot", "can't", "won't", "don't", "doesn't", "didn't"}


def _score_text(text: str) -> tuple[int, int]:
    """Return (bull_count, bear_count) for text.

    When VADER is available, converts the compound sentiment score directly into
    weighted bull/bear counts so the rest of the pipeline is unchanged.  Falls
    back to a negation-aware keyword scan when VADER is not installed.
    """
    if _VADER_AVAILABLE and _vader is not None:
        vs = _vader.polarity_scores(text)
        compound = vs["compound"]            # -1.0 … +1.0
        magnitude = round(abs(compound) * 10)  # scale to keyword-equivalent counts
        if compound >= 0.05:
            return magnitude, 0
        elif compound <= -0.05:
            return 0, magnitude
        else:
            return 0, 0

    # --- negation-aware keyword fallback ---
    tokens = text.lower().split()
    bulls = bears = 0
    for i, token in enumerate(tokens):
        # check for a negation word in a 3-token window before this token
        negated = any(tokens[max(0, i - 3):i][j] in _NEGATION_WORDS
                      for j in range(min(3, i)))
        clean = token.strip(".,!?;:'\"")
        if clean in BULLISH_KW:
            if negated:
                bears += 1          # "not bullish" → bearish signal
            else:
                bulls += 1
        elif clean in BEARISH_KW:
            if negated:
                bulls += 1          # "not crashing" → bullish signal
            else:
                bears += 1
    return bulls, bears


@cached(ttl=1800)
def get_reddit_sentiment(coin: str = "bitcoin", hours: int = 24) -> dict:
    """Fetch Reddit post sentiment for a coin over the past `hours`.

    Returns score -1.0 (fully bearish) to +1.0 (fully bullish).
    Returns a zeroed dict if PRAW unavailable or credentials missing.
    """
    empty = {
        "score": 0.0, "post_count": 0, "bull_count": 0, "bear_count": 0,
        "top_posts": [], "coin": coin, "source": "reddit",
    }
    if not _praw_available():
        return empty

    reddit = _get_reddit()
    if reddit is None:
        return empty

    keywords = _COIN_KEYWORDS.get(coin.lower(), [coin.lower()])
    cutoff = time.time() - hours * 3600

    bull_total = bear_total = post_count = 0
    top_posts: list[dict] = []

    try:
        for sub_name in SUBREDDITS[:3]:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.hot(limit=50):
                    if post.created_utc < cutoff:
                        continue
                    text = f"{post.title} {post.selftext or ''}"
                    if not any(kw in text.lower() for kw in keywords):
                        continue
                    bulls, bears = _score_text(text)
                    bull_total += bulls
                    bear_total += bears
                    post_count += 1
                    if len(top_posts) < 5:
                        top_posts.append({
                            "title": post.title[:120],
                            "score": post.score,
                            "url": f"https://reddit.com{post.permalink}",
                            "sentiment": "bullish" if bulls > bears else "bearish" if bears > bulls else "neutral",
                        })
            except Exception as e:
                log.debug("Reddit subreddit %s failed: %s", sub_name, e)
    except Exception as e:
        log.warning("Reddit sentiment fetch failed for %s: %s", coin, e)
        return empty

    if post_count == 0:
        return empty

    total_signals = bull_total + bear_total
    if total_signals == 0:
        net_score = 0.0
    else:
        net_score = round((bull_total - bear_total) / total_signals, 4)

    return {
        "score": net_score,
        "post_count": post_count,
        "bull_count": bull_total,
        "bear_count": bear_total,
        "top_posts": top_posts,
        "coin": coin,
        "source": "reddit",
    }


@cached(ttl=1800)
def get_social_sentiment_summary() -> dict:
    """Aggregate Reddit sentiment across BTC, ETH, SOL for market overview.

    Returns composite score (-1 to +1) plus per-coin breakdowns.
    """
    coins = ["bitcoin", "ethereum", "solana"]
    results = {}
    composite = 0.0
    active = 0

    for coin in coins:
        try:
            data = get_reddit_sentiment(coin)
            results[coin[:3]] = data
            if data["post_count"] > 0:
                composite += data["score"]
                active += 1
        except Exception as e:
            log.debug("Social summary failed for %s: %s", coin, e)
            results[coin[:3]] = {"score": 0.0, "post_count": 0}

    composite = round(composite / active, 4) if active else 0.0
    return {**results, "composite": composite, "active_coins": active}
