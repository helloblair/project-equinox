"""
Equinox API — FastAPI endpoints for cross-venue prediction market aggregation.

Endpoints:
- GET /health — Service health check
- POST /pipeline/run — Execute the full pipeline
- GET /markets/{venue} — Fetch normalized markets from a single venue
- GET /matches — Get most recent matched pairs
- GET /route — Simulate routing for matched pairs
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.pipeline import EquinoxPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Pipeline state (in-memory; sufficient for prototype)
pipeline_state = {
    "pipeline": None,
    "last_results": None,
    "last_run": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize pipeline on startup."""
    pipeline_state["pipeline"] = EquinoxPipeline()
    yield
    # Cleanup
    if pipeline_state["pipeline"]:
        await pipeline_state["pipeline"].polymarket.close()
        await pipeline_state["pipeline"].kalshi.close()


app = FastAPI(
    title="Project Equinox",
    description="Cross-Venue Prediction Market Aggregation & Routing Simulation",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "equinox",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_run": pipeline_state["last_run"],
    }


@app.post("/pipeline/run")
async def run_pipeline(limit: int = Query(default=50, ge=1, le=200)):
    """Execute the full Equinox pipeline: fetch → normalize → match → route."""
    try:
        pipeline = EquinoxPipeline()
        results = await pipeline.run(market_limit=limit)
        pipeline_state["last_results"] = results
        pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return results
    except Exception as e:
        logging.error(f"Pipeline error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/markets/{venue}")
async def get_markets(venue: str, limit: int = Query(default=20, ge=1, le=100)):
    """Fetch and normalize markets from a single venue."""
    from src.venues.polymarket import PolymarketAdapter
    from src.venues.kalshi import KalshiAdapter

    if venue.lower() == "polymarket":
        adapter = PolymarketAdapter()
    elif venue.lower() == "kalshi":
        adapter = KalshiAdapter()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown venue: {venue}. Use 'polymarket' or 'kalshi'.")

    try:
        markets = await adapter.fetch_markets(limit=limit)
        await adapter.close()
        return {
            "venue": venue,
            "count": len(markets),
            "markets": [m.to_dict() for m in markets],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/matches")
async def get_matches():
    """Return the most recent matched pairs from the last pipeline run."""
    results = pipeline_state.get("last_results")
    if not results:
        raise HTTPException(status_code=404, detail="No pipeline results available. Run POST /pipeline/run first.")
    return {
        "matched_pairs": results.get("matches", []),
        "total_matches": results["summary"]["matched_pairs"],
    }


@app.get("/routing")
async def get_routing():
    """Return routing decisions from the last pipeline run."""
    results = pipeline_state.get("last_results")
    if not results:
        raise HTTPException(status_code=404, detail="No pipeline results available. Run POST /pipeline/run first.")
    return {
        "decisions": results.get("routing_decisions", []),
        "total_decisions": results["summary"]["routing_decisions"],
    }
