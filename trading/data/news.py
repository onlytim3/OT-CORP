"""Market category news feeds — free, no-API-key data sources.

All sources are free and require zero registration:
  - BLS API v1 (CPI, unemployment — 25 queries/day, no key)
  - NY Fed Markets API (fed funds rate — no key)
  - US Treasury XML feed (yield curve — no key)
  - Yahoo Finance (DXY — unofficial, no key)
  - CoinGecko global metrics (crypto market health — no key)
  - Fear & Greed (crypto sentiment — no key)
  - RSS feeds: CoinDesk, CoinTelegraph, CNBC, Google News, OilPrice
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from trading.data.cache import cached

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "TradingIntelligence/1.0"}

# ---------------------------------------------------------------------------
# RSS feeds — verified working 2026-03-14
# ---------------------------------------------------------------------------

_RSS_FEEDS = {
    "crypto": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://cryptoslate.com/feed/",
        "https://bitcoinmagazine.com/feed",
    ],
    "stocks": [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://seekingalpha.com/feed.xml",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    ],
    "commodities": [
        "https://oilprice.com/rss/main",
        "https://news.google.com/rss/search?q=gold+silver+commodities+precious+metals&hl=en-US&gl=US&ceid=US:en",
        "https://www.investing.com/rss/news_11.rss",
    ],
    "forex": [
        "https://www.investing.com/rss/news_1.rss",
        "https://seekingalpha.com/tag/forex.xml",
    ],
    "macro": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "https://news.google.com/rss/search?q=central+bank+interest+rate+federal+reserve+economic+data&hl=en-US&gl=US&ceid=US:en",
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    ],
    "global": [
        # Reuters global business/markets (English, no geo-redirect)
        "https://news.google.com/rss/search?q=global+financial+markets+economy+GDP&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=US+economy+recession+inflation+federal+reserve&hl=en-US&gl=US&ceid=US:en",
    ],
}


@cached(ttl=600)
def fetch_rss_headlines(category: str, max_items: int = 10) -> list[dict]:
    """Fetch recent headlines from RSS feeds for a market category."""
    urls = _RSS_FEEDS.get(category, [])
    headlines = []

    for url in urls:
        try:
            resp = requests.get(url, timeout=10, headers=_HEADERS)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            items = root.findall(".//item") or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry"
            )

            for item in items[:max_items]:
                title_el = (
                    item.find("title")
                    or item.find("{http://www.w3.org/2005/Atom}title")
                )
                pub_el = (
                    item.find("pubDate")
                    or item.find("{http://www.w3.org/2005/Atom}published")
                )

                if title_el is not None and title_el.text:
                    headlines.append({
                        "title": title_el.text.strip(),
                        "published": pub_el.text.strip() if pub_el is not None and pub_el.text else "",
                        "source": url.split("/")[2],
                        "category": category,
                    })

        except Exception as e:
            log.debug("RSS fetch failed for %s: %s", url, e)

    return headlines[:max_items]


# ---------------------------------------------------------------------------
# Macro data — all free, no API keys
# ---------------------------------------------------------------------------

@cached(ttl=3600)
def fetch_fed_funds_rate() -> dict | None:
    """Fetch effective federal funds rate from NY Fed Markets API (no key).

    Source: https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json
    """
    try:
        resp = requests.get(
            "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/2.json",
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        rates = resp.json().get("refRates", [])
        if not rates:
            return None

        current = rates[0]
        result = {
            "value": float(current["percentRate"]),
            "date": current["effectiveDate"],
            "target_low": float(current.get("targetRateFrom", 0)),
            "target_high": float(current.get("targetRateTo", 0)),
        }

        if len(rates) > 1:
            prev = rates[1]
            result["prev_value"] = float(prev["percentRate"])
            result["prev_date"] = prev["effectiveDate"]
            result["change"] = result["value"] - result["prev_value"]

        return result

    except Exception as e:
        log.debug("NY Fed EFFR fetch failed: %s", e)
        return None


@cached(ttl=3600)
def fetch_bls_series(series_id: str) -> dict | None:
    """Fetch latest value from BLS API v1 (no key, 25 queries/day limit).

    Series IDs:
      CUUR0000SA0  — CPI-U All Items
      LNS14000000  — Unemployment Rate
    """
    try:
        resp = requests.get(
            f"https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}",
            timeout=15, headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "REQUEST_SUCCEEDED":
            return None

        series_data = data.get("Results", {}).get("series", [])
        if not series_data or not series_data[0].get("data"):
            return None

        observations = series_data[0]["data"]
        current = observations[0]
        value = float(current["value"])

        result = {
            "value": value,
            "date": f"{current['year']}-{current['period'].replace('M', '')}",
            "period_name": current.get("periodName", ""),
            "year": current["year"],
        }

        if len(observations) > 1:
            prev = observations[1]
            prev_val = float(prev["value"])
            result["prev_value"] = prev_val
            result["prev_date"] = f"{prev['year']}-{prev['period'].replace('M', '')}"
            result["change"] = value - prev_val

        return result

    except Exception as e:
        log.debug("BLS fetch failed for %s: %s", series_id, e)
        return None


@cached(ttl=3600)
def fetch_treasury_yields() -> dict | None:
    """Fetch yield curve data from Treasury.gov XML feed (no key).

    Returns 2Y, 10Y, and the 10Y-2Y spread.
    """
    try:
        year = datetime.now(timezone.utc).year
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/pages/xmlview"
            f"?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
        )
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        # Find all entries
        ns_atom = "{http://www.w3.org/2005/Atom}"
        ns_m = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
        ns_d = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"

        entries = root.findall(f".//{ns_atom}entry")
        if not entries:
            return None

        # Last entry is most recent
        latest = entries[-1]
        props = latest.find(f".//{ns_m}properties")
        if props is None:
            return None

        def _get_yield(tag: str) -> float | None:
            el = props.find(f"{ns_d}{tag}")
            if el is not None and el.text:
                try:
                    return float(el.text)
                except ValueError:
                    pass
            return None

        ten_yr = _get_yield("BC_10YEAR")
        two_yr = _get_yield("BC_2YEAR")
        date_el = props.find(f"{ns_d}NEW_DATE")
        date_str = date_el.text[:10] if date_el is not None and date_el.text else ""

        if ten_yr is None or two_yr is None:
            return None

        spread = ten_yr - two_yr

        return {
            "ten_year": round(ten_yr, 3),
            "two_year": round(two_yr, 3),
            "spread": round(spread, 3),
            "date": date_str,
        }

    except Exception as e:
        log.debug("Treasury yield fetch failed: %s", e)
        return None


@cached(ttl=3600)
def fetch_dxy_yahoo() -> dict | None:
    """Fetch DXY (US Dollar Index) from Yahoo Finance (no key).

    Uses unofficial Yahoo Finance chart API.
    """
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"range": "1mo", "interval": "1d"},
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        current = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose")

        if current is None:
            return None

        out = {
            "value": round(float(current), 2),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        if prev_close:
            out["prev_value"] = round(float(prev_close), 2)
            out["change"] = round(float(current) - float(prev_close), 2)

        return out

    except Exception as e:
        log.debug("Yahoo DXY fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CoinGecko global metrics (no key)
# ---------------------------------------------------------------------------

@cached(ttl=600)
def fetch_coingecko_global() -> dict | None:
    """Fetch global crypto market metrics from CoinGecko (no key)."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        return {
            "total_market_cap_usd": data.get("total_market_cap", {}).get("usd", 0),
            "total_volume_24h_usd": data.get("total_volume", {}).get("usd", 0),
            "btc_dominance": data.get("market_cap_percentage", {}).get("btc", 0),
            "eth_dominance": data.get("market_cap_percentage", {}).get("eth", 0),
            "active_cryptocurrencies": data.get("active_cryptocurrencies", 0),
            "market_cap_change_24h_pct": data.get("market_cap_change_percentage_24h_usd", 0),
        }
    except Exception as e:
        log.debug("CoinGecko global fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Economic calendar
# ---------------------------------------------------------------------------

@cached(ttl=600)
def fetch_economic_calendar() -> list[dict]:
    """Return upcoming known economic events that could cause volatility."""
    now = datetime.now(timezone.utc)
    day_of_week = now.strftime("%A")
    upcoming = []

    if day_of_week in ("Tuesday", "Wednesday"):
        upcoming.append({
            "event": "FOMC Decision Window",
            "impact": "critical",
            "category": "macro",
            "note": "Potential rate decision — expect volatility across all categories",
        })

    if day_of_week == "Friday" and now.day <= 7:
        upcoming.append({
            "event": "Non-Farm Payrolls (NFP)",
            "impact": "high",
            "category": "macro",
            "note": "Employment data affects dollar, rates, and risk assets",
        })

    if 10 <= now.day <= 14 and day_of_week in ("Tuesday", "Wednesday", "Thursday"):
        upcoming.append({
            "event": "CPI Release Window",
            "impact": "high",
            "category": "macro",
            "note": "Inflation data directly impacts rate expectations",
        })

    return upcoming


# ---------------------------------------------------------------------------
# GDELT global events (no API key)
# ---------------------------------------------------------------------------

@cached(ttl=600)
def fetch_gdelt_news(query: str = "economy finance markets", max_items: int = 20) -> list[dict]:
    """Fetch global news from GDELT Project API (no key, unlimited).

    Returns articles with tone scores (built-in sentiment).
    Covers 65 languages, translates to English automatically.
    """
    try:
        resp = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query,
                "mode": "artlist",
                "maxrecords": min(max_items * 3, 75),
                "format": "json",
                "sort": "datedesc",
            },
            timeout=15, headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])

        headlines = []
        for a in articles[:max_items]:
            headlines.append({
                "title": a.get("title", "").strip(),
                "published": a.get("seendate", ""),
                "source": a.get("domain", "gdelt"),
                "category": "global",
                "url": a.get("url", ""),
                "tone": a.get("tone", 0),  # GDELT sentiment: negative=bearish, positive=bullish
                "language": a.get("language", "English"),
            })
        return headlines

    except Exception as e:
        log.debug("GDELT fetch failed for query '%s': %s", query, e)
        return []


@cached(ttl=600)
def fetch_crypto_news_api(max_items: int = 20) -> list[dict]:
    """Fetch crypto news from cryptocurrency.cv API (no key required).

    662k+ articles from 75+ sources. Free and open.
    """
    try:
        resp = requests.get(
            "https://cryptocurrency.cv/api/news",
            params={"limit": max_items},
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", data) if isinstance(data, dict) else data

        headlines = []
        for a in (articles if isinstance(articles, list) else []):
            headlines.append({
                "title": a.get("title", "").strip(),
                "published": a.get("publishedAt", a.get("date", "")),
                "source": a.get("source", {}).get("name", "cryptocurrency.cv") if isinstance(a.get("source"), dict) else a.get("source", "cryptocurrency.cv"),
                "category": "crypto",
            })
        return headlines[:max_items]

    except Exception as e:
        log.debug("cryptocurrency.cv fetch failed: %s", e)
        return []


@cached(ttl=600)
def fetch_finnhub_news(category: str = "general") -> list[dict]:
    """Fetch financial news from Finnhub (requires FINNHUB_API_KEY env var).

    Free tier: 60 calls/min. Categories: general, forex, crypto, merger.
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return []

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": category, "token": api_key},
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        articles = resp.json()

        headlines = []
        for a in articles[:20]:
            headlines.append({
                "title": a.get("headline", "").strip(),
                "published": datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc).isoformat() if a.get("datetime") else "",
                "source": a.get("source", "finnhub"),
                "category": category,
                "url": a.get("url", ""),
                "summary": a.get("summary", "")[:200],
            })
        return headlines

    except Exception as e:
        log.debug("Finnhub news fetch failed: %s", e)
        return []


def fetch_finnhub_company_news(symbols: list[str] | None = None) -> list[dict]:
    """Fetch company-specific news from Finnhub for crypto-correlated equities.

    Targets COIN (Coinbase), MSTR (MicroStrategy), IBIT (BlackRock BTC ETF) —
    these move with BTC and carry information about institutional sentiment
    before it shows up in crypto prices.
    """
    import os
    from datetime import date, timedelta
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return []

    if symbols is None:
        symbols = ["COIN", "MSTR", "IBIT"]

    from_date = (date.today() - timedelta(days=3)).isoformat()
    to_date = date.today().isoformat()

    headlines = []
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": sym, "from": from_date, "to": to_date, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            articles = resp.json()
            for a in articles[:5]:
                headlines.append({
                    "title": a.get("headline", "").strip(),
                    "published": datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc).isoformat() if a.get("datetime") else "",
                    "source": a.get("source", "finnhub"),
                    "category": "crypto",
                    "url": a.get("url", ""),
                    "summary": a.get("summary", "")[:200],
                    "related_symbol": sym,
                })
        except Exception as e:
            log.debug("Finnhub company news failed for %s: %s", sym, e)

    return headlines


def fetch_finnhub_sentiment(symbols: list[str] | None = None) -> dict[str, float]:
    """Fetch Finnhub's NLP sentiment scores for crypto-correlated equities.

    Returns dict of symbol → sentiment score (-1 bearish to +1 bullish).
    Uses the /news-sentiment endpoint which aggregates recent articles.
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}

    if symbols is None:
        symbols = ["COIN", "MSTR", "IBIT"]

    scores = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/news-sentiment",
                params={"symbol": sym, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            # buzz.weeklyAverage gives article volume, sentiment.bullishPercent/bearishPercent are 0-1
            bull = data.get("sentiment", {}).get("bullishPercent", 0)
            bear = data.get("sentiment", {}).get("bearishPercent", 0)
            if bull or bear:
                scores[sym] = round(bull - bear, 3)
        except Exception as e:
            log.debug("Finnhub sentiment failed for %s: %s", sym, e)

    return scores


def fetch_finnhub_social_sentiment(symbols: list[str] | None = None) -> dict:
    """Reddit + Twitter mention counts and sentiment for crypto-correlated equities.

    Endpoint: GET /stock/social-sentiment
    Cache: 10 min — social signal is fast-moving.
    Returns: {symbol: {reddit_mentions, reddit_sentiment, twitter_mentions, twitter_sentiment}}
    """
    import os
    from datetime import date, timedelta
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}
    if symbols is None:
        symbols = ["COIN", "MSTR", "BTC"]

    from_date = (date.today() - timedelta(days=7)).isoformat()
    to_date = date.today().isoformat()
    result = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/social-sentiment",
                params={"symbol": sym, "from": from_date, "to": to_date, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            reddit = data.get("reddit", [])
            twitter = data.get("twitter", [])
            r_mention = sum(d.get("mention", 0) for d in reddit)
            r_sent = (sum(d.get("score", 0) for d in reddit) / len(reddit)) if reddit else 0
            t_mention = sum(d.get("mention", 0) for d in twitter)
            t_sent = (sum(d.get("score", 0) for d in twitter) / len(twitter)) if twitter else 0
            result[sym] = {
                "reddit_mentions": r_mention,
                "reddit_sentiment": round(r_sent, 3),
                "twitter_mentions": t_mention,
                "twitter_sentiment": round(t_sent, 3),
            }
        except Exception as e:
            log.debug("Finnhub social sentiment failed for %s: %s", sym, e)
    return result


def fetch_finnhub_economic_calendar() -> list[dict]:
    """Economic calendar with consensus vs actual — calculates economic surprise.

    Endpoint: GET /calendar/economic
    Surprise = (actual - estimate) / |estimate| * 100
    Positive surprise = economy beating expectations = risk-on.
    """
    import os
    from datetime import date, timedelta
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return []
    try:
        from_date = (date.today() - timedelta(days=7)).isoformat()
        to_date = (date.today() + timedelta(days=7)).isoformat()
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": from_date, "to": to_date, "token": api_key},
            timeout=10, headers=_HEADERS,
        )
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", [])
        result = []
        for e in events:
            if e.get("country", "").upper() != "US":
                continue
            actual = e.get("actual")
            estimate = e.get("estimate")
            surprise_pct = None
            if actual is not None and estimate and abs(estimate) > 0:
                surprise_pct = round((actual - estimate) / abs(estimate) * 100, 2)
            result.append({
                "event": e.get("event", ""),
                "date": e.get("time", "")[:10],
                "actual": actual,
                "estimate": estimate,
                "surprise_pct": surprise_pct,
                "impact": e.get("impact", "low"),
            })
        return result
    except Exception as e:
        log.debug("Finnhub economic calendar failed: %s", e)
        return []


def fetch_finnhub_earnings_calendar(symbols: list[str] | None = None) -> list[dict]:
    """Upcoming and recent earnings with EPS surprise for crypto-correlated equities.

    Endpoint: GET /calendar/earnings
    COIN earnings = exchange volume proxy; MSTR = BTC exposure proxy.
    """
    import os
    from datetime import date, timedelta
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return []
    if symbols is None:
        symbols = ["COIN", "MSTR", "NVDA"]

    from_date = (date.today() - timedelta(days=30)).isoformat()
    to_date = (date.today() + timedelta(days=30)).isoformat()
    result = []
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={"symbol": sym, "from": from_date, "to": to_date, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            earnings = resp.json().get("earningsCalendar", [])
            for e in earnings:
                eps_est = e.get("epsEstimate")
                eps_act = e.get("epsActual")
                surprise_pct = None
                if eps_est and eps_act and abs(eps_est) > 0:
                    surprise_pct = round((eps_act - eps_est) / abs(eps_est) * 100, 2)
                result.append({
                    "symbol": sym,
                    "date": e.get("date", ""),
                    "eps_estimate": eps_est,
                    "eps_actual": eps_act,
                    "surprise_pct": surprise_pct,
                    "quarter": e.get("quarter"),
                    "year": e.get("year"),
                })
        except Exception as e:
            log.debug("Finnhub earnings calendar failed for %s: %s", sym, e)
    return result


def fetch_finnhub_recommendations(symbols: list[str] | None = None) -> dict:
    """Analyst buy/sell/hold consensus for crypto-correlated equities.

    Endpoint: GET /stock/recommendation
    net_score = (strongBuy*2 + buy - sell - strongSell*2) / total → -1..+1
    Analyst downgrades of COIN often precede BTC pullbacks.
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}
    if symbols is None:
        symbols = ["COIN", "MSTR"]

    result = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/recommendation",
                params={"symbol": sym, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            recs = resp.json()
            if not recs:
                continue
            latest = recs[0]  # Most recent month
            sb = latest.get("strongBuy", 0)
            b = latest.get("buy", 0)
            h = latest.get("hold", 0)
            s = latest.get("sell", 0)
            ss = latest.get("strongSell", 0)
            total = sb + b + h + s + ss
            net_score = round((sb * 2 + b - s - ss * 2) / total, 3) if total > 0 else 0
            result[sym] = {
                "strong_buy": sb, "buy": b, "hold": h, "sell": s, "strong_sell": ss,
                "total": total, "net_score": net_score, "period": latest.get("period", ""),
            }
        except Exception as e:
            log.debug("Finnhub recommendations failed for %s: %s", sym, e)
    return result


def fetch_finnhub_insider_transactions(symbols: list[str] | None = None) -> dict:
    """Insider buy/sell transactions for crypto-correlated equities (last 30 days).

    Endpoint: GET /stock/insider-transactions
    P = purchase (buy), S = sale (sell).
    Heavy insider selling at COIN = leading indicator of trouble.
    """
    import os
    from datetime import date, timedelta
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}
    if symbols is None:
        symbols = ["COIN", "MSTR"]

    from_date = (date.today() - timedelta(days=30)).isoformat()
    result = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/insider-transactions",
                params={"symbol": sym, "from": from_date, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            txns = resp.json().get("data", [])
            result[sym] = [
                {
                    "name": t.get("name", ""),
                    "shares": t.get("share", 0),
                    "value": t.get("value", 0),
                    "date": t.get("transactionDate", ""),
                    "action": "buy" if t.get("transactionCode", "") == "P" else "sell",
                }
                for t in txns
                if t.get("transactionCode") in ("P", "S")
            ]
        except Exception as e:
            log.debug("Finnhub insider transactions failed for %s: %s", sym, e)
    return result


def fetch_finnhub_economic_indicators() -> dict:
    """Same-day macro economic indicators from Finnhub — faster than FRED.

    Endpoint: GET /economic?code=<series_code>
    Complements FRED (which lags by days on release date).
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}

    # Finnhub economic series codes
    series = {
        "united states cpi": "MA-USA-656880",
        "united states unemployment": "RBUSR",
        "united states interest rate": "IR",
        "united states gdp growth": "GDPC1",
    }
    result = {}
    for name, code in series.items():
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/economic",
                params={"code": code, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            if data and len(data) >= 1:
                latest = data[-1]
                prev = data[-2] if len(data) >= 2 else latest
                val = latest.get("v") or latest.get("value")
                prev_val = prev.get("v") or prev.get("value")
                if val is not None:
                    result[name] = {
                        "value": round(float(val), 4),
                        "previous": round(float(prev_val), 4) if prev_val is not None else None,
                        "change": round(float(val) - float(prev_val), 4) if prev_val is not None else None,
                        "date": latest.get("period", ""),
                    }
        except Exception as e:
            log.debug("Finnhub economic indicator %s failed: %s", code, e)
    return result


def fetch_finnhub_institutional_ownership(symbols: list[str] | None = None) -> dict:
    """Institutional ownership changes for BTC ETFs — tracks smart money flows.

    Endpoint: GET /institutional/ownership
    Increasing institutional BTC ETF holdings = sustained demand signal.
    Symbols: IBIT (BlackRock), FBTC (Fidelity), ARKB (ARK).
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return {}
    if symbols is None:
        symbols = ["IBIT", "FBTC", "ARKB"]

    result = {}
    for sym in symbols:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/institutional/ownership",
                params={"symbol": sym, "token": api_key},
                timeout=10, headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            ownership = data.get("ownership", [])
            if not ownership:
                continue
            latest = ownership[0]
            prev = ownership[1] if len(ownership) > 1 else None
            cur_shares = latest.get("share", 0)
            prev_shares = prev.get("share", 0) if prev else cur_shares
            change_pct = round((cur_shares - prev_shares) / prev_shares * 100, 2) if prev_shares else 0
            result[sym] = {
                "total_shares": cur_shares,
                "num_holders": latest.get("investor", 0),
                "change_pct": change_pct,
                "quarter": latest.get("reportDate", ""),
            }
        except Exception as e:
            log.debug("Finnhub institutional ownership failed for %s: %s", sym, e)
    return result


def fetch_all_headlines(max_per_source: int = 10) -> list[dict]:
    """Fetch headlines from ALL sources — RSS, GDELT, crypto API, Finnhub.

    Returns a combined, deduplicated list of headlines across all categories.
    """
    all_headlines = []

    # RSS feeds (all categories)
    for category in _RSS_FEEDS:
        try:
            hl = fetch_rss_headlines(category, max_items=max_per_source)
            all_headlines.extend(hl)
        except Exception:
            pass

    # GDELT global events
    for query in ["economy markets finance", "cryptocurrency bitcoin", "oil gold commodities"]:
        try:
            all_headlines.extend(fetch_gdelt_news(query, max_items=max_per_source))
        except Exception:
            pass

    # Crypto news API
    try:
        all_headlines.extend(fetch_crypto_news_api(max_items=max_per_source))
    except Exception:
        pass

    # Finnhub — skip "general" (returns CNBC lifestyle/parenting content)
    for cat in ["crypto", "forex", "merger"]:
        try:
            all_headlines.extend(fetch_finnhub_news(cat))
        except Exception:
            pass

    # Finnhub company news — COIN, MSTR, IBIT (crypto-correlated equities)
    try:
        all_headlines.extend(fetch_finnhub_company_news())
    except Exception:
        pass

    # Deduplicate by title and filter non-English headlines
    seen = set()
    unique = []
    for h in all_headlines:
        title = h.get("title", "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        # Reject headlines where >15% of chars are non-ASCII (indicates non-English)
        non_ascii = sum(1 for c in title if ord(c) > 127)
        if len(title) > 0 and non_ascii / len(title) > 0.15:
            continue
        unique.append(h)

    return unique


# ---------------------------------------------------------------------------
# Aggregate fetcher
# ---------------------------------------------------------------------------

def fetch_all_category_data() -> dict:
    """Fetch all available data across all market categories.

    All sources are free and require no API keys.
    """
    data = {
        "headlines": {},
        "macro": {},
        "crypto_global": None,
        "fear_greed": None,
        "calendar": [],
    }

    # Headlines by category
    for category in _RSS_FEEDS:
        try:
            data["headlines"][category] = fetch_rss_headlines(category)
        except Exception as e:
            log.debug("Headlines fetch failed for %s: %s", category, e)
            data["headlines"][category] = []

    # Macro data from free government APIs (no keys)
    try:
        ffr = fetch_fed_funds_rate()
        if ffr:
            data["macro"]["fed_funds_rate"] = ffr
    except Exception as e:
        log.debug("Fed funds rate failed: %s", e)

    try:
        cpi = fetch_bls_series("CUUR0000SA0")
        if cpi:
            data["macro"]["cpi"] = cpi
    except Exception as e:
        log.debug("CPI fetch failed: %s", e)

    try:
        unemp = fetch_bls_series("LNS14000000")
        if unemp:
            data["macro"]["unemployment"] = unemp
    except Exception as e:
        log.debug("Unemployment fetch failed: %s", e)

    try:
        yields = fetch_treasury_yields()
        if yields:
            data["macro"]["yield_curve"] = yields
    except Exception as e:
        log.debug("Treasury yields failed: %s", e)

    try:
        dxy = fetch_dxy_yahoo()
        if dxy:
            data["macro"]["dxy_index"] = dxy
    except Exception as e:
        log.debug("DXY fetch failed: %s", e)

    # Crypto global metrics
    try:
        data["crypto_global"] = fetch_coingecko_global()
    except Exception as e:
        log.debug("CoinGecko global failed: %s", e)

    # Fear & Greed
    try:
        from trading.data.sentiment import get_fear_greed
        fg = get_fear_greed(limit=7)
        data["fear_greed"] = fg.get("current")
    except Exception as e:
        log.debug("Fear & Greed failed: %s", e)

    # Economic calendar
    try:
        data["calendar"] = fetch_economic_calendar()
    except Exception as e:
        log.debug("Calendar failed: %s", e)

    # Finnhub company news for crypto-correlated equities (COIN, MSTR, IBIT)
    try:
        company_news = fetch_finnhub_company_news()
        if company_news:
            data["headlines"]["crypto"] = data["headlines"].get("crypto", []) + company_news
    except Exception as e:
        log.debug("Finnhub company news failed: %s", e)

    # Finnhub NLP sentiment scores for crypto-correlated equities
    try:
        data["finnhub_sentiment"] = fetch_finnhub_sentiment()
    except Exception as e:
        log.debug("Finnhub sentiment failed: %s", e)

    # FRED macro series — CPI, Fed Funds rate, yield curve, unemployment
    # These supersede the BLS/NY Fed free endpoints with cleaner data
    try:
        from trading.data.commodities import get_fred_series
        from trading.config import FRED_API_KEY
        if FRED_API_KEY:
            fred_series = {
                "CPIAUCSL": "cpi_fred",        # CPI all items, seasonally adjusted
                "FEDFUNDS": "fedfunds_fred",    # Effective Fed Funds rate
                "T10Y2Y": "yield_spread_fred",  # 10Y-2Y spread (recession signal)
                "UNRATE": "unrate_fred",        # Unemployment rate
            }
            for series_id, key in fred_series.items():
                try:
                    df = get_fred_series(series_id, limit=3)
                    if df is not None and not df.empty:
                        latest = float(df["value"].iloc[-1])
                        prev = float(df["value"].iloc[-2]) if len(df) > 1 else latest
                        data["macro"][key] = {
                            "value": latest,
                            "change": round(latest - prev, 4),
                            "series": series_id,
                        }
                except Exception as e:
                    log.debug("FRED %s failed: %s", series_id, e)
    except Exception as e:
        log.debug("FRED macro fetch failed: %s", e)

    # Finnhub social sentiment (Reddit + Twitter) for COIN, MSTR, BTC
    try:
        data["finnhub_social_sentiment"] = fetch_finnhub_social_sentiment()
    except Exception as e:
        log.debug("Finnhub social sentiment failed: %s", e)

    # Finnhub economic calendar with surprise calculation
    try:
        data["finnhub_economic_calendar"] = fetch_finnhub_economic_calendar()
    except Exception as e:
        log.debug("Finnhub economic calendar failed: %s", e)

    # Finnhub earnings calendar for COIN, MSTR, NVDA
    try:
        data["finnhub_earnings_calendar"] = fetch_finnhub_earnings_calendar()
    except Exception as e:
        log.debug("Finnhub earnings calendar failed: %s", e)

    # Finnhub analyst recommendations for COIN, MSTR
    try:
        data["finnhub_recommendations"] = fetch_finnhub_recommendations()
    except Exception as e:
        log.debug("Finnhub recommendations failed: %s", e)

    # Finnhub insider transactions for COIN, MSTR (last 30 days)
    try:
        data["finnhub_insider_transactions"] = fetch_finnhub_insider_transactions()
    except Exception as e:
        log.debug("Finnhub insider transactions failed: %s", e)

    # Finnhub economic indicators (same-day data, faster than FRED)
    try:
        data["finnhub_economic_indicators"] = fetch_finnhub_economic_indicators()
    except Exception as e:
        log.debug("Finnhub economic indicators failed: %s", e)

    # Finnhub institutional ownership for BTC ETFs (IBIT, FBTC, ARKB)
    try:
        data["finnhub_institutional_ownership"] = fetch_finnhub_institutional_ownership()
    except Exception as e:
        log.debug("Finnhub institutional ownership failed: %s", e)

    return data
