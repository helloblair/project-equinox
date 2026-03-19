"""
Kalshi Venue Adapter

Kalshi is a CFTC-regulated prediction market. Its API structure:
- Base URL: https://api.elections.kalshi.com/trade-api/v2
- Markets endpoint returns paginated results with cursor
- Prices are in USD cents (0-100), not decimals (0.0-1.0)

Key normalization challenges:
- Prices in cents → must divide by 100 to get probability
- Status field uses different labels than Polymarket
- Category is embedded in series_ticker prefix (e.g., "KXBTC" = crypto)
- Kalshi uses "tickers" not "IDs" as primary identifiers
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.models.market import CanonicalMarket, MarketStatus, VenueType
from src.venues.base import BaseVenueAdapter

logger = logging.getLogger(__name__)

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series ticker prefixes → normalized categories
# These map to Polymarket's category labels for cross-venue matching
KALSHI_CATEGORY_MAP = {
    # Economics
    "KXFED": "economics",
    "KXCPI": "economics",
    "KXGDP": "economics",
    "KXUNEMPLOY": "economics",
    "KXINX": "economics",
    "KXRATE": "economics",
    "KXJOB": "economics",
    "KXRECESSION": "economics",
    # Crypto
    "KXBTC": "crypto",
    "KXETH": "crypto",
    "KXSOL": "crypto",
    "KXCRYPTO": "crypto",
    # Politics
    "KXPRES": "politics",
    "KXSENATE": "politics",
    "KXHOUSE": "politics",
    "KXELECT": "politics",
    "KXGOV": "politics",
    "KXTRUMP": "politics",
    "KXCONGRESS": "politics",
    "KXSUPREME": "politics",
    "KXAI": "politics",
    # Sports
    "KXNFL": "sports",
    "KXNBA": "sports",
    "KXMLB": "sports",
    "KXNHL": "sports",
    "KXMARMAD": "sports",
    "KXNCAA": "sports",
    "KXCFB": "sports",
    "KXSOCCER": "sports",
    "KXMMA": "sports",
    "KXBOXING": "sports",
    "KXMVE": "sports",
    # Weather
    "KXHIGH": "weather",
    "KXLOW": "weather",
    "KXTEMP": "weather",
    "KXHURR": "weather",
}

# Title keywords → category (fallback when ticker prefix doesn't match)
KALSHI_TITLE_CATEGORY_MAP = {
    "bitcoin": "crypto", "btc": "crypto", "ethereum": "crypto", "eth": "crypto", "crypto": "crypto",
    "fed": "economics", "cpi": "economics", "inflation": "economics", "gdp": "economics",
    "recession": "economics", "unemployment": "economics", "rate cut": "economics", "rate hike": "economics",
    "president": "politics", "senate": "politics", "house": "politics", "congress": "politics",
    "election": "politics", "trump": "politics", "biden": "politics", "republican": "politics", "democrat": "politics",
    "nba": "sports", "nfl": "sports", "mlb": "sports", "nhl": "sports", "march madness": "sports",
    "ncaa": "sports", "championship": "sports", "world series": "sports", "super bowl": "sports",
}


class KalshiAdapter(BaseVenueAdapter):
    """Fetches and normalizes Kalshi prediction markets."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    @property
    def venue_name(self) -> str:
        return "Kalshi"

    async def fetch_markets(self, limit: int = 200) -> list[CanonicalMarket]:
        """
        Fetch active markets from Kalshi.

        Strategy: Query specific series tickers that overlap with Polymarket's
        categories. The default /markets endpoint returns mostly combo/parlay
        markets that aren't useful for cross-venue matching.

        We also filter out combo markets (those with mve_selected_legs or
        strike_type="custom") since they bundle multiple outcomes and don't
        map to single binary events on other venues.
        """
        markets = []

        # Series we know have cross-venue overlap
        target_series = [
            "KXFED",        # Fed rate decisions
            "KXCPI",        # CPI / inflation
            "KXGDP",        # GDP / recession
            "KXUNEMPLOY",   # Unemployment
            "KXBTC",        # Bitcoin price
            "KXETH",        # Ethereum price
            "KXPRES",       # Presidential
            "KXSENATE",     # Senate
            "KXHOUSE",      # House
            "KXNBA",        # NBA
            "KXNFL",        # NFL
            "KXMLB",        # MLB
        ]

        try:
            # First: fetch by known series for targeted matching
            # Kalshi rate limits aggressively — add delay between requests
            for series in target_series:
                try:
                    await asyncio.sleep(0.3)  # 300ms delay to avoid 429s
                    response = await self.client.get(
                        f"{KALSHI_API_BASE}/markets",
                        params={
                            "limit": 50,
                            "status": "open",
                            "series_ticker": series,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()

                    for raw_market in data.get("markets", []):
                        if self._is_combo_market(raw_market):
                            continue
                        try:
                            canonical = self.normalize(raw_market)
                            if canonical is not None:
                                markets.append(canonical)
                        except Exception as e:
                            logger.warning(f"Failed to normalize Kalshi market: {e}")

                except httpx.HTTPError as e:
                    if "429" in str(e):
                        logger.warning(f"Rate limited on series {series} — pausing 2s")
                        await asyncio.sleep(2.0)
                    else:
                        logger.debug(f"No results for series {series}: {e}")
                    continue

            # If series queries all failed (e.g., network restricted), fall back to mock
            if not markets:
                logger.warning("All series queries failed — falling back to mock data")
                return self._load_mock_data()

            # Second: broad fetch to catch anything we missed
            # Skip if we already got a good number from series queries
            if len(markets) < 20:
                cursor = None
                broad_count = 0
                while broad_count < limit:
                    await asyncio.sleep(0.3)
                    params = {
                        "limit": min(100, limit - broad_count),
                        "status": "open",
                    }
                    if cursor:
                        params["cursor"] = cursor

                    try:
                        response = await self.client.get(
                            f"{KALSHI_API_BASE}/markets",
                            params=params,
                        )
                        response.raise_for_status()
                    except httpx.HTTPError as e:
                        if "429" in str(e):
                            logger.warning("Rate limited on broad fetch — stopping")
                        break

                    data = response.json()

                    raw_markets = data.get("markets", [])
                    if not raw_markets:
                        break

                    for raw_market in raw_markets:
                        if self._is_combo_market(raw_market):
                            continue
                        try:
                            canonical = self.normalize(raw_market)
                            if canonical is not None:
                                existing_ids = {m.venue_market_id for m in markets}
                                if canonical.venue_market_id not in existing_ids:
                                    markets.append(canonical)
                        except Exception as e:
                            logger.warning(f"Failed to normalize Kalshi market: {e}")

                    broad_count += len(raw_markets)
                    cursor = data.get("cursor")
                    if not cursor:
                        break

            logger.info(f"Fetched {len(markets)} markets from Kalshi (filtered out combo/parlay markets)")

        except httpx.HTTPError as e:
            logger.warning(f"Kalshi API error: {e}")
            if not markets:
                logger.warning("No markets fetched — falling back to mock data")
                return self._load_mock_data()
            else:
                logger.info(f"Keeping {len(markets)} markets fetched before error")

        return markets

    def _is_combo_market(self, raw_market: dict) -> bool:
        """Filter out combo/parlay markets that bundle multiple outcomes."""
        if raw_market.get("mve_selected_legs"):
            return True
        if raw_market.get("strike_type") == "custom":
            return True
        if raw_market.get("mve_collection_ticker"):
            return True
        return False

    def _load_mock_data(self) -> list[CanonicalMarket]:
        """Load mock data when API is unavailable (e.g., network restrictions)."""
        from src.venues.mock_data import KALSHI_MARKETS

        markets = []
        for raw_market in KALSHI_MARKETS:
            try:
                canonical = self.normalize(raw_market)
                if canonical is not None:
                    markets.append(canonical)
            except Exception as e:
                logger.warning(f"Failed to normalize mock market: {e}")
        logger.info(f"Loaded {len(markets)} markets from mock data")
        return markets

    def normalize(self, raw_market: dict) -> Optional[CanonicalMarket]:
        """
        Convert a Kalshi market to canonical form.

        Key transformations:
        - Convert cent prices (0-100) to probability (0.0-1.0)
        - Map Kalshi status to our enum
        - Infer category from series_ticker prefix
        - Parse ISO timestamps
        """
        # Parse prices — Kalshi uses dollar strings like "0.5600"
        yes_price = self._parse_price(raw_market.get("yes_ask_dollars") or raw_market.get("last_price_dollars"))
        no_price = self._parse_price(raw_market.get("no_ask_dollars"))

        # If we got yes but not no, compute it
        if yes_price is not None and no_price is None:
            no_price = 1.0 - yes_price

        # Map status
        status_map = {
            "open": MarketStatus.OPEN,
            "active": MarketStatus.OPEN,
            "closed": MarketStatus.CLOSED,
            "settled": MarketStatus.SETTLED,
        }
        status = status_map.get(raw_market.get("status", ""), MarketStatus.UNKNOWN)

        # Parse expiration
        expiration = None
        exp_str = raw_market.get("expiration_time") or raw_market.get("close_time")
        if exp_str:
            try:
                expiration = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Infer category from series ticker, event ticker, or market ticker
        # series_ticker isn't always present at market level, but event_ticker
        # usually contains the series prefix (e.g., "KXFED-26MAR" starts with "KXFED")
        series_ticker = raw_market.get("series_ticker", "")
        if not series_ticker:
            # Fall back to event_ticker which usually contains the series prefix
            series_ticker = raw_market.get("event_ticker", "")
        if not series_ticker:
            # Last resort: try the market ticker itself
            series_ticker = raw_market.get("ticker", "")
        category = self._infer_category(series_ticker, title=raw_market.get("title", ""))

        # Parse volume
        volume = None
        vol_str = raw_market.get("volume_fp") or raw_market.get("volume")
        if vol_str is not None:
            try:
                volume = float(vol_str)
            except (ValueError, TypeError):
                pass

        title = raw_market.get("title", "Unknown")
        subtitle = raw_market.get("subtitle", "")
        description = f"{title} {subtitle}".strip() if subtitle else title

        # Track data quality issues
        warnings = []
        if yes_price is None:
            warnings.append("Missing YES price — pricing data unavailable")
        if volume is None:
            warnings.append("Missing volume data — liquidity assessment limited")
        if expiration is None:
            warnings.append("Missing expiration date — temporal matching degraded")
        if category == "other":
            warnings.append(f"Unrecognized series ticker '{series_ticker}' — category defaulted to 'other'")
        if raw_market.get("no_ask_dollars") is None and yes_price is not None:
            warnings.append("NO price computed from YES price (1.0 - yes) — no direct ask available")

        return CanonicalMarket(
            venue=VenueType.KALSHI,
            venue_market_id=raw_market.get("ticker", ""),
            venue_event_id=raw_market.get("event_ticker", ""),
            title=title,
            description=description,
            category=category,
            tags=[category, series_ticker] if series_ticker else [category],
            yes_price=yes_price,
            no_price=no_price,
            volume_usd=volume,
            expiration_date=expiration,
            status=status,
            raw_data=raw_market,
            fetched_at=datetime.now(timezone.utc),
            warnings=warnings,
        )

    def _parse_price(self, price_val) -> Optional[float]:
        """Parse Kalshi price to 0.0-1.0 range."""
        if price_val is None:
            return None
        try:
            p = float(price_val)
            # If it looks like cents (> 1), convert
            if p > 1.0:
                return p / 100.0
            return p
        except (ValueError, TypeError):
            return None

    def _infer_category(self, series_ticker: str, title: str = "") -> str:
        """
        Infer category from Kalshi's series ticker prefix.
        Falls back to title keyword matching if ticker doesn't match.
        """
        # Try ticker prefix first
        for prefix, category in KALSHI_CATEGORY_MAP.items():
            if series_ticker.upper().startswith(prefix):
                return category

        # Fall back to title keyword matching
        title_lower = title.lower()
        for keyword, category in KALSHI_TITLE_CATEGORY_MAP.items():
            if keyword in title_lower:
                return category

        return "other"

    async def close(self):
        await self.client.aclose()
