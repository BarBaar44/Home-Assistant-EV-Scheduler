"""
routing (shared pyscript module)
--------------------------------
Waze driving distance and time, shared by ev_trip_energy (trip energy)
and trip_scheduler (refusing an "arrive by" trip that can no longer be
reached).

Lives at /config/pyscript/modules/routing.py.
    import routing as routing_mod

Requires in /config/pyscript/requirements.txt: pywaze, curl_cffi.

WAZE REFUSES NON BROWSER CLIENTS (Oct 2026). A plain HTTP client gets
"403 Forbidden"; Waze checks the TLS fingerprint. pywaze fixed this in
1.2.3 with curl_cffi, but Home Assistant pins pywaze 1.2.0. So when
curl_cffi is installed waze_legs() calls the routing endpoint itself, the
same request pywaze makes. Without curl_cffi it uses pywaze. Remove the
curl_cffi path once HA ships pywaze >= 1.2.3.
"""
import math
import logging

_logger = logging.getLogger(__name__)

WAZE_ROUTING_URL = "https://routing-livemap-row.waze.com/RoutingManager/routingRequest"


def straight_km(a, b):
    """Great circle distance in km between two (lat, lon) pairs."""
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@pyscript_executor
def waze_legs(start, end, include_return):
    """Both legs, (out_km, out_min, back_km, back_min); Nones on failure.
    `start` and `end` are "lat,lon" strings. Current traffic.

    Everything is inline: no calls to other pyscript functions from an
    executor thread (they return unrun coroutines there).
    """
    try:
        from curl_cffi import requests as cffi_requests
    except Exception:
        cffi_requests = None

    if cffi_requests is not None:
        legs = [(start, end)]
        if include_return:
            legs.append((end, start))
        out = []
        session = cffi_requests.Session(impersonate="chrome")
        try:
            for frm, to in legs:
                flat, flon = frm.split(",")
                tlat, tlon = to.split(",")
                params = {
                    "from": f"x:{flon.strip()} y:{flat.strip()}",
                    "to": f"x:{tlon.strip()} y:{tlat.strip()}",
                    "at": 0,
                    "returnJSON": "true",
                    "returnGeometries": "false",
                    "returnInstructions": "true",
                    "timeout": 60000,
                    "nPaths": 1,
                    "options": "AVOID_TRAILS:t,AVOID_TOLL_ROADS:f,AVOID_FERRIES:f",
                    "subscription": "*",
                }
                resp = session.get(
                    WAZE_ROUTING_URL,
                    params=params,
                    headers={"referer": "https://www.waze.com/"},
                    timeout=30,
                )
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(f"Waze error: {data.get('error')}")
                if data.get("alternatives"):
                    route = data["alternatives"][0]["response"]
                else:
                    route = data["response"]
                    if isinstance(route, list):
                        route = route[0]
                segments = route.get("results") or route.get("result") or []
                seconds = 0
                meters = 0
                for seg in segments:
                    if "crossTime" in seg:
                        seconds += seg["crossTime"]
                    else:
                        seconds += seg.get("cross_time", 0)
                    meters += seg.get("length", 0)
                if meters <= 0:
                    raise RuntimeError("Waze returned an empty route")
                out.append((meters / 1000.0, seconds / 60.0))
        except Exception as e:
            _logger.warning(f"Waze route calc failed: {str(e)[:200]}")
            return None, None, None, None
        finally:
            try:
                session.close()
            except Exception:
                pass
        if include_return:
            return out[0][0], out[0][1], out[1][0], out[1][1]
        return out[0][0], out[0][1], None, None

    import asyncio
    import pywaze.route_calculator as route_calculator

    async def _calc():
        async with route_calculator.WazeRouteCalculator() as client:
            out = (await client.calc_routes(start, end))[0]
            if not include_return:
                return out.distance, out.duration, None, None
            back = (await client.calc_routes(end, start))[0]
            return out.distance, out.duration, back.distance, back.duration

    try:
        return asyncio.run(_calc())
    except Exception as e:
        _logger.warning(f"Waze route calc failed (pywaze, no curl_cffi): {str(e)[:200]}")
        return None, None, None, None
