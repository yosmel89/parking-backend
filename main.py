import json
import math
import os
from datetime import datetime, timedelta, timezone

import httpx
import redis
from fastapi import FastAPI, Query

app = FastAPI()
r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))

ARCGIS = "https://services.arcgis.com/YOUR_ORG/arcgis/rest/services/MiamiBeach_Meters/FeatureServer/0/query"
PARKENDD = "https://api.parkendd.de/dresden"

CACHE_KEY = "parking:all"
CACHE_TTL = 120


@app.on_event("startup")
async def startup():
    try:
        await poll_all()
    except Exception as e:
        print(f"Startup poll failed (will retry later): {e}")


async def poll_all():
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            inv = await c.get(
                ARCGIS,
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "f": "json",
                    "resultRecordCount": 5000,
                },
            )
            inv_json = inv.json()
        except Exception:
            inv_json = {"features": []}

        try:
            occ = await c.get(PARKENDD)
            occ_json = occ.json()
        except Exception:
            occ_json = {"data": []}

    inventory = {
        f["attributes"]["METER_ID"]: f["attributes"]
        for f in inv_json.get("features", [])
        if f.get("attributes", {}).get("METER_ID")
    }

    merged = []
    for s in occ_json.get("data", []):
        meter_id = s.get("id")
        a = inventory.get(meter_id, {})
        rate = a.get("RATE_PER_HOUR", 0) or 0
        limit = a.get("TIME_LIMIT_MIN", 0) or 0
        expires = None
        event_time = s.get("event_time")
        if s.get("state") == "OCCUPIED" and limit and event_time:
            try:
                expires = (
                    datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                    + timedelta(minutes=limit)
                ).isoformat()
            except ValueError:
                expires = None
        merged.append(
            {
                "meter_id": meter_id,
                "street": a.get("STREET"),
                "zone": a.get("ZONE"),
                "lat": a.get("LAT"),
                "lng": a.get("LNG"),
                "state": s.get("state"),
                "rate_per_hour": rate,
                "rate_per_min": round(rate / 60, 4),
                "time_limit_min": limit,
                "last_event_utc": event_time,
                "expires_utc": expires,
            }
        )

    r.setex(CACHE_KEY, CACHE_TTL, json.dumps(merged))


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@app.get("/parking")
def parking(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: float = Query(1.0, gt=0, le=50),
):
    data = json.loads(r.get(CACHE_KEY) or "[]")
    return [
        s
        for s in data
        if s.get("lat") is not None
        and s.get("lng") is not None
        and haversine(lat, lng, s["lat"], s["lng"]) <= radius
    ]


@app.get("/poll")
async def poll():
    await poll_all()
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok"}