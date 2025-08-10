import json
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

ORS_KEY = getattr(settings, "ORS_API_KEY", "")

def _ensure_key():
    if not ORS_KEY:
        return JsonResponse({"error": "ORS_API_KEY não configurada no .env"}, status=500)

def _validate_point(p, name):
    try:
        lat = float(p["lat"]); lng = float(p["lng"])
    except Exception:
        raise ValueError(f"{name} inválido. Esperado {{lat, lng}} numéricos.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"{name} fora de faixa.")
    return lat, lng

def _build_coordinates(origin, waypoints, destination):
    # ORS usa [lon, lat]
    coords = []
    o_lat, o_lng = _validate_point(origin, "origin")
    coords.append([o_lng, o_lat])
    for i, w in enumerate(waypoints or []):
        w_lat, w_lng = _validate_point(w, f"waypoint[{i}]")
        coords.append([w_lng, w_lat])
    d_lat, d_lng = _validate_point(destination, "destination")
    coords.append([d_lng, d_lat])
    return coords

def _call_ors_directions(profile, payload):
    r = requests.post(
        f"https://api.openrouteservice.org/v2/directions/{profile}/geojson",
        headers={"Authorization": ORS_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    try:
        data = r.json()
    except Exception:
        return JsonResponse({"error":"Falha ao interpretar resposta do ORS","raw":r.text}, status=502)
    if r.status_code != 200:
        return JsonResponse({"error":"Erro do ORS","status":r.status_code,"detail":data}, status=r.status_code)
    return data

def _extract_summary(geojson):
    try:
        props = geojson["features"][0]["properties"]
        summary = props["summary"]
        return {
            "distance_m": summary.get("distance"),
            "duration_s": summary.get("duration"),
            "segments": props.get("segments"),
            "bbox": geojson.get("bbox"),
        }
    except Exception:
        return None

@csrf_exempt
def rota_carro(request):
    """
    POST /api/rota-carro
    Body:
    {
      "origin": {"lat": -25.51, "lng": -54.58},
      "destination": {"lat": -25.44, "lng": -54.62},
      "waypoints": [{"lat":..., "lng":...}],       # opcional
      "avoid_features": ["tollways","ferries"]     # opcional
    }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    if (err := _ensure_key()) is not None:
        return err

    try:
        body = json.loads(request.body.decode("utf-8"))
        coords = _build_coordinates(body["origin"], body.get("waypoints"), body["destination"])
        avoid = body.get("avoid_features") or []
        payload = {
            "coordinates": coords,
            "instructions": True,
            "options": {"avoid_features": avoid} if avoid else {}
        }
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    data = _call_ors_directions("driving-car", payload)
    if isinstance(data, JsonResponse):
        return data

    summary = _extract_summary(data)
    return JsonResponse({"summary": summary, "geojson": data}, status=200, safe=False)

@csrf_exempt
def rota_caminhao(request):
    """
    POST /api/rota-caminhao
    Body:
    {
      "origin": {"lat": -25.51, "lng": -54.58},
      "destination": {"lat": -25.44, "lng": -54.62},
      "waypoints": [{"lat":..., "lng":...}],
      "truck": {
        "height": 4.2, "width": 2.6, "length": 18.5,
        "weight": 38000, "axleload": 10000
      },
      "avoid_features": ["tollways"]   # opcional
    }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    if (err := _ensure_key()) is not None:
        return err

    try:
        body = json.loads(request.body.decode("utf-8"))
        coords = _build_coordinates(body["origin"], body.get("waypoints"), body["destination"])

        truck = body.get("truck", {})
        restrictions = {
            "height": truck.get("height"),
            "width": truck.get("width"),
            "length": truck.get("length"),
            "weight": truck.get("weight"),
            "axleload": truck.get("axleload"),
        }
        restrictions = {k: v for k, v in restrictions.items() if v is not None}
        avoid = body.get("avoid_features") or []

        payload = {
            "coordinates": coords,
            "instructions": True,
            "options": {"avoid_features": avoid} if avoid else {},
            "profile_params": {"restrictions": restrictions} if restrictions else {}
        }
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    data = _call_ors_directions("driving-hgv", payload)
    if isinstance(data, JsonResponse):
        return data

    summary = _extract_summary(data)
    return JsonResponse({"summary": summary, "geojson": data}, status=200, safe=False)
