"""
Polymarket Venue Adapter

Polymarket uses two APIs:
- Gamma API (gamma-api.polymarket.com): Market metadata, events, discovery
- CLOB API (clob.polymarket.com): Prices, order books

Key normalization challenges:
- Prices come as stringified JSON arrays: '["0.65", "0.35"]'
- Volume is in USDC (1:1 with USD, so no conversion needed)
- Categories/tags are available but inconsistently applied
- Markets can be single-outcome (Yes/No) or multi-outcome (multiple options)
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.models.market import CanonicalMarket, MarketStatus, VenueType
from src.venues.base import BaseVenueAdapter

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PolymarketAdapter(BaseVenueAdapter):
    """Fetches and normalizes Polymarket prediction markets."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    @property
    def venue_name(self) -> str:
        return "Polymarket"

    async def fetch_markets(self, limit: int = 100) -> list[CanonicalMarket]:
        """
        Fetch active markets from Polymarket's Gamma API.

        We fetch events (which contain markets) because events give us
        better grouping context for matching against other venues.
        """
        markets = []
        try:
            # Fetch events with their nested markets
            response = await self.client.get(
                f"{GAMMA_API_BASE}/events",
                params={
                    "limit": limit,
                    "active": "true",
                    "closed": "false",
                },
            )
            response.raise_for_status()
            events = response.json()

            for event in events:
                event_markets = event.get("markets", [])
                for raw_market in event_markets:
                    try:
                        canonical = self.normalize(raw_market, event_context=event)
                        if canonical is not None:
                            markets.append(canonical)
                    except Exception as e:
                        logger.warning(f"Failed to normalize Polymarket market: {e}")
                        continue

            logger.info(f"Fetched {len(markets)} markets from Polymarket")

        except httpx.HTTPError as e:
            logger.warning(f"Polymarket API error: {e} — falling back to mock data")
            return self._load_mock_data()

        return markets

    def _load_mock_data(self) -> list[CanonicalMarket]:
        """Load mock data when API is unavailable (e.g., network restrictions)."""
        from src.venues.mock_data import POLYMARKET_EVENTS

        markets = []
        for event in POLYMARKET_EVENTS:
            for raw_market in event.get("markets", []):
                try:
                    canonical = self.normalize(raw_market, event_context=event)
                    if canonical is not None:
                        markets.append(canonical)
                except Exception as e:
                    logger.warning(f"Failed to normalize mock market: {e}")
        logger.info(f"Loaded {len(markets)} markets from mock data")
        return markets

    def normalize(self, raw_market: dict, event_context: dict = None) -> Optional[CanonicalMarket]:
        """
        Convert a Polymarket market to canonical form.

        Key transformations:
        - Parse stringified JSON price arrays
        - Map Polymarket's 'active'/'closed' to our MarketStatus enum
        - Extract category from event tags
        - Convert volume string to float
        """
        # Parse prices from stringified JSON arrays
        yes_price, no_price = self._parse_prices(raw_market)

        # Determine status
        if raw_market.get("closed", False):
            status = MarketStatus.CLOSED
        elif raw_market.get("active", False):
            status = MarketStatus.OPEN
        else:
            status = MarketStatus.UNKNOWN

        # Parse expiration
        expiration = None
        end_date = raw_market.get("endDate") or raw_market.get("end_date_iso")
        if end_date:
            try:
                expiration = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Extract category from event context
        category = "uncategorized"
        tags = []
        if event_context:
            # Polymarket tags come from the event level
            event_tags = event_context.get("tags", [])
            if isinstance(event_tags, list) and event_tags:
                tags = [t.get("label", t) if isinstance(t, dict) else str(t) for t in event_tags]
                category = tags[0].lower() if tags else "uncategorized"

        # Parse volume
        volume = None
        vol_str = raw_market.get("volume") or raw_market.get("volumeNum")
        if vol_str is not None:
            try:
                volume = float(vol_str)
            except (ValueError, TypeError):
                pass

        # Build canonical market
        title = raw_market.get("question") or raw_market.get("title", "Unknown")
        description = raw_market.get("description") or title

        # Track data quality issues
        warnings = []
        if yes_price is None:
            warnings.append("Missing YES price — pricing data unavailable")
        if volume is None:
            warnings.append("Missing volume data — liquidity assessment limited")
        if expiration is None:
            warnings.append("Missing expiration date — temporal matching degraded")
        if category == "uncategorized":
            warnings.append("No category tags — category-based filtering disabled for this market")

        return CanonicalMarket(
            venue=VenueType.POLYMARKET,
            venue_market_id=str(raw_market.get("id", raw_market.get("conditionId", ""))),
            venue_event_id=str(event_context.get("id", "")) if event_context else "",
            title=title,
            description=description,
            category=category,
            tags=tags,
            yes_price=yes_price,
            no_price=no_price,
            volume_usd=volume,
            expiration_date=expiration,
            status=status,
            raw_data=raw_market,
            fetched_at=datetime.now(timezone.utc),
            warnings=warnings,
        )

    def _parse_prices(self, raw_market: dict) -> tuple[Optional[float], Optional[float]]:
        """
        Polymarket stores prices as stringified JSON: '["0.65", "0.35"]'
        Index 0 = Yes price, Index 1 = No price
        """
        yes_price = None
        no_price = None

        prices_str = raw_market.get("outcomePrices")
        if prices_str:
            try:
                if isinstance(prices_str, str):
                    prices = json.loads(prices_str)
                else:
                    prices = prices_str
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        return yes_price, no_price

    async def close(self):
        await self.client.aclose()
