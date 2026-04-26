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

_GN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

_RSS_FEEDS = {
    # ── CRYPTO ────────────────────────────────────────────────────────────────
    "crypto": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://cryptoslate.com/feed/",
        "https://bitcoinmagazine.com/feed",
        "https://www.theblock.co/rss.xml",
        "https://cryptobriefing.com/feed/",
        _GN.format(q="Bitcoin+BTC+cryptocurrency+price+market"),
        _GN.format(q="Ethereum+ETH+DeFi+altcoin+crypto"),
        _GN.format(q="crypto+regulation+SEC+CFTC+blockchain"),
    ],

    # ── EQUITIES ──────────────────────────────────────────────────────────────
    "equities": [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://seekingalpha.com/feed.xml",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        _GN.format(q="S%26P+500+NASDAQ+Dow+Jones+stock+market+rally"),
        _GN.format(q="earnings+revenue+profit+guidance+quarterly+results"),
        _GN.format(q="IPO+merger+acquisition+buyback+dividend"),
    ],

    # ── TECHNOLOGY ────────────────────────────────────────────────────────────
    "technology": [
        "https://techcrunch.com/feed/",
        "https://feeds.feedburner.com/venturebeat/SZYF",
        "https://www.theverge.com/rss/index.xml",
        _GN.format(q="NVIDIA+AMD+semiconductor+chip+AI+artificial+intelligence"),
        _GN.format(q="Apple+Microsoft+Google+Meta+Amazon+tech+stocks"),
        _GN.format(q="AI+machine+learning+OpenAI+ChatGPT+LLM+data+center"),
        _GN.format(q="cybersecurity+cloud+computing+SaaS+software"),
    ],

    # ── ENERGY ────────────────────────────────────────────────────────────────
    "energy": [
        "https://oilprice.com/rss/main",
        _GN.format(q="oil+price+WTI+Brent+crude+OPEC+petroleum"),
        _GN.format(q="natural+gas+LNG+pipeline+energy+supply"),
        _GN.format(q="renewable+energy+solar+wind+EV+electric+vehicle+battery"),
        _GN.format(q="ExxonMobil+Chevron+Shell+BP+TotalEnergies+energy+stocks"),
    ],

    # ── HEALTHCARE / PHARMA ───────────────────────────────────────────────────
    "healthcare": [
        "https://www.statnews.com/feed/",
        _GN.format(q="FDA+approval+drug+pharmaceutical+clinical+trial"),
        _GN.format(q="biotech+Pfizer+Moderna+Johnson+Eli+Lilly+healthcare"),
        _GN.format(q="healthcare+insurance+hospital+Medicare+policy"),
    ],

    # ── COMMODITIES ───────────────────────────────────────────────────────────
    "commodities": [
        "https://oilprice.com/rss/main",
        "https://www.investing.com/rss/news_11.rss",
        _GN.format(q="gold+silver+platinum+palladium+precious+metals"),
        _GN.format(q="copper+aluminum+zinc+nickel+iron+ore+mining"),
        _GN.format(q="commodity+futures+raw+materials+supply+chain"),
    ],

    # ── AGRICULTURE ───────────────────────────────────────────────────────────
    "agriculture": [
        _GN.format(q="wheat+corn+soybean+agriculture+crop+harvest+USDA"),
        _GN.format(q="food+price+inflation+drought+climate+farming"),
        _GN.format(q="coffee+cocoa+sugar+cotton+agricultural+commodity"),
    ],

    # ── FOREX / FX ────────────────────────────────────────────────────────────
    "forex": [
        "https://www.investing.com/rss/news_1.rss",
        _GN.format(q="dollar+DXY+EUR+USD+JPY+GBP+currency+exchange+rate"),
        _GN.format(q="Federal+Reserve+ECB+Bank+of+England+BOJ+central+bank+rate"),
        _GN.format(q="emerging+market+currency+peso+lira+rupee+real+FX"),
    ],

    # ── MACRO / ECONOMY ───────────────────────────────────────────────────────
    "macro": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        _GN.format(q="inflation+CPI+PCE+GDP+recession+economic+data"),
        _GN.format(q="Federal+Reserve+FOMC+interest+rate+hike+cut+Powell"),
        _GN.format(q="unemployment+jobs+nonfarm+payroll+labor+market"),
        _GN.format(q="US+budget+deficit+debt+ceiling+treasury+fiscal+policy"),
    ],

    # ── FIXED INCOME / BONDS ─────────────────────────────────────────────────
    "bonds": [
        _GN.format(q="bond+yield+treasury+10+year+2+year+spread+inversion"),
        _GN.format(q="high+yield+junk+bond+credit+spread+investment+grade"),
        _GN.format(q="corporate+bond+debt+default+credit+rating+Moody%27s"),
    ],

    # ── BANKING / FINANCE ─────────────────────────────────────────────────────
    "banking": [
        _GN.format(q="JPMorgan+Goldman+Sachs+Morgan+Stanley+bank+earnings"),
        _GN.format(q="banking+crisis+financial+stability+Fed+liquidity+credit"),
        _GN.format(q="hedge+fund+private+equity+asset+management+BlackRock"),
    ],

    # ── REAL ESTATE ───────────────────────────────────────────────────────────
    "real_estate": [
        _GN.format(q="housing+market+mortgage+rate+home+price+real+estate"),
        _GN.format(q="REIT+commercial+real+estate+office+retail+construction"),
    ],

    # ── GEOPOLITICAL / RISK ──────────────────────────────────────────────────
    "geopolitical": [
        _GN.format(q="war+conflict+sanctions+tariff+trade+war+military"),
        _GN.format(q="US+China+Russia+Middle+East+NATO+geopolitical+risk"),
        _GN.format(q="election+government+policy+regulation+legislation+law"),
        _GN.format(q="Iran+Israel+Ukraine+Taiwan+North+Korea+geopolitics"),
    ],

    # ── EMERGING MARKETS ─────────────────────────────────────────────────────
    "emerging_markets": [
        _GN.format(q="China+PBOC+yuan+renminbi+economy+growth+Xi"),
        _GN.format(q="India+Brazil+Mexico+Indonesia+emerging+markets+GDP"),
        _GN.format(q="EM+capital+flows+foreign+investment+developing+economy"),
        _GN.format(q="Africa+Middle+East+Latin+America+frontier+markets"),
    ],

    # ── ASIA PACIFIC ─────────────────────────────────────────────────────────
    "asia": [
        _GN.format(q="Bank+of+Japan+BOJ+yen+Nikkei+Japan+economy"),
        _GN.format(q="Hong+Kong+China+markets+Hang+Seng+CSI"),
        _GN.format(q="Korea+Taiwan+Singapore+ASEAN+Asia+Pacific+economy"),
    ],

    # ── EUROPE ───────────────────────────────────────────────────────────────
    "europe": [
        _GN.format(q="ECB+European+Central+Bank+eurozone+inflation+rate"),
        _GN.format(q="Bank+of+England+pound+sterling+UK+economy+gilt"),
        _GN.format(q="Germany+France+Italy+EU+economy+DAX+CAC"),
    ],

    # ── CORPORATE / M&A ──────────────────────────────────────────────────────
    "corporate": [
        _GN.format(q="merger+acquisition+takeover+deal+M%26A+buyout"),
        _GN.format(q="IPO+SPAC+listing+spinoff+restructuring+bankruptcy"),
    ],

    # ── STOCKS (legacy key kept) ──────────────────────────────────────────────
    "stocks": [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://seekingalpha.com/feed.xml",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    ],

    # ── GLOBAL ────────────────────────────────────────────────────────────────
    "global": [
        _GN.format(q="global+financial+markets+economy+GDP+trade"),
        _GN.format(q="US+economy+recession+inflation+federal+reserve+outlook"),
        _GN.format(q="world+economy+IMF+World+Bank+G7+G20+growth"),
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


@cached(ttl=3600)
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
        symbols = [
            # Crypto-native / BTC proxies
            "COIN", "MSTR", "RIOT", "MARA", "CLSK",
            # BTC ETFs (institutional demand signal)
            "IBIT", "FBTC", "ARKB", "BITB",
            # Mega-cap tech (NASDAQ ↔ crypto correlation)
            "NVDA", "AAPL", "MSFT", "GOOGL", "META", "TSLA", "AMD",
            # Finance / banking (risk sentiment)
            "JPM", "GS", "MS", "BAC", "BLK",
            # Energy (inflation / macro signal)
            "XOM", "CVX",
            # Healthcare (defensive sector)
            "LLY", "MRNA", "PFE",
            # Broad market ETFs (macro backdrop)
            "SPY", "QQQ", "IWM",
            # Commodity ETFs (alternative asset flows)
            "GLD", "SLV", "USO",
        ]

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


@cached(ttl=3600)
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


@cached(ttl=600)
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


@cached(ttl=3600)
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


@cached(ttl=86400)
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


@cached(ttl=86400)
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


@cached(ttl=86400)
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


@cached(ttl=3600)
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


@cached(ttl=86400)
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


@cached(ttl=300)
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
    Uses a thread pool to fetch independent sources in parallel — reduces cold-start
    latency from ~25 minutes (sequential) to ~2-3 minutes (parallel).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    data: dict = {
        "headlines": {},
        "macro": {},
        "crypto_global": None,
        "fear_greed": None,
        "calendar": [],
    }

    # ------------------------------------------------------------------
    # Define all independent fetch tasks as (result_key, callable) pairs.
    # Tasks that write to nested dicts (headlines, macro) use special keys.
    # ------------------------------------------------------------------

    def _rss(category):
        return ("rss", category, fetch_rss_headlines(category))

    def _fed_funds():
        return ("macro", "fed_funds_rate", fetch_fed_funds_rate())

    def _cpi():
        return ("macro", "cpi", fetch_bls_series("CUUR0000SA0"))

    def _unemployment():
        return ("macro", "unemployment", fetch_bls_series("LNS14000000"))

    def _yields():
        return ("macro", "yield_curve", fetch_treasury_yields())

    def _dxy():
        return ("macro", "dxy_index", fetch_dxy_yahoo())

    def _coingecko():
        return ("top", "crypto_global", fetch_coingecko_global())

    def _fear_greed():
        from trading.data.sentiment import get_fear_greed
        fg = get_fear_greed(limit=7)
        return ("top", "fear_greed", fg.get("current") if fg else None)

    def _calendar():
        return ("top", "calendar", fetch_economic_calendar())

    def _company_news():
        return ("company_news", None, fetch_finnhub_company_news())

    def _sentiment():
        return ("finnhub", "finnhub_sentiment", fetch_finnhub_sentiment())

    def _social():
        return ("finnhub", "finnhub_social_sentiment", fetch_finnhub_social_sentiment())

    def _econ_cal():
        return ("finnhub", "finnhub_economic_calendar", fetch_finnhub_economic_calendar())

    def _earnings():
        return ("finnhub", "finnhub_earnings_calendar", fetch_finnhub_earnings_calendar())

    def _recs():
        return ("finnhub", "finnhub_recommendations", fetch_finnhub_recommendations())

    def _insider():
        return ("finnhub", "finnhub_insider_transactions", fetch_finnhub_insider_transactions())

    def _econ_ind():
        return ("finnhub", "finnhub_economic_indicators", fetch_finnhub_economic_indicators())

    def _inst_own():
        return ("finnhub", "finnhub_institutional_ownership", fetch_finnhub_institutional_ownership())

    def _fred_series():
        try:
            from trading.data.commodities import get_fred_series
            from trading.config import FRED_API_KEY
            if not FRED_API_KEY:
                return ("fred", None, {})
            fred_map = {
                "CPIAUCSL": "cpi_fred", "PCEPI": "pce_fred",
                "FEDFUNDS": "fedfunds_fred", "DGS10": "yield_10y_fred",
                "DGS2": "yield_2y_fred", "T10Y2Y": "yield_spread_fred",
                "UNRATE": "unrate_fred", "INDPRO": "indpro_fred",
                "RSXFS": "retail_fred", "M2SL": "m2_fred",
                "BAMLH0A0HYM2": "hy_spread_fred", "VIXCLS": "vix_fred",
                "DCOILWTICO": "wti_fred", "GOLDAMGBD228NLBM": "gold_fred",
                "DTWEXBGS": "dxy_fred",
            }
            results = {}
            for series_id, key in fred_map.items():
                try:
                    df = get_fred_series(series_id, limit=3)
                    if df is not None and not df.empty:
                        latest = float(df["value"].iloc[-1])
                        prev = float(df["value"].iloc[-2]) if len(df) > 1 else latest
                        results[key] = {"value": latest, "change": round(latest - prev, 4), "series": series_id}
                except Exception as e:
                    log.debug("FRED %s failed: %s", series_id, e)
            return ("fred", None, results)
        except Exception as e:
            log.debug("FRED macro fetch failed: %s", e)
            return ("fred", None, {})

    tasks = (
        [lambda cat=c: _rss(cat) for c in _RSS_FEEDS]
        + [_fed_funds, _cpi, _unemployment, _yields, _dxy,
           _coingecko, _fear_greed, _calendar,
           _company_news, _sentiment, _social,
           _econ_cal, _earnings, _recs, _insider, _econ_ind, _inst_own,
           _fred_series]
    )

    # Collect all results before applying — avoids race on data["headlines"]["crypto"]
    # which is written by both _rss("crypto") and _company_news().
    raw_results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(fn) for fn in tasks]
        for future in as_completed(futures, timeout=30):
            try:
                raw_results.append(future.result(timeout=15))
            except Exception as e:
                log.debug("Parallel fetch task failed: %s", e)

    # Apply in deterministic order: rss → macro/top/finnhub → company_news (append)
    for kind, key, value in sorted(raw_results, key=lambda r: 1 if r[0] == "company_news" else 0):
        if kind == "rss":
            data["headlines"][key] = value if value is not None else []
        elif kind == "macro":
            if value:
                data["macro"][key] = value
        elif kind == "top":
            data[key] = value
        elif kind == "company_news":
            if value:
                data["headlines"]["crypto"] = data["headlines"].get("crypto", []) + value
        elif kind == "finnhub":
            data[key] = value
        elif kind == "fred":
            data["macro"].update(value or {})

    return data
