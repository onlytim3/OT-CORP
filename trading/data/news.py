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
    ],
    "commodities": [
        "https://oilprice.com/rss/main",
        "https://news.google.com/rss/search?q=gold+silver+commodities+precious+metals&hl=en-US&gl=US&ceid=US:en",
    ],
    "macro": [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "https://news.google.com/rss/search?q=central+bank+interest+rate+federal+reserve+economic+data&hl=en-US&gl=US&ceid=US:en",
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

    return data
