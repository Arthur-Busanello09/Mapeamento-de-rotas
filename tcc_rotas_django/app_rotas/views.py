import json
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

# ---- Config ORS ----
ORS_KEY = getattr(settings, "ORS_API_KEY", "")

# ---- HTTP Session com retry/timeout ----
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = requests.Session()
_retry = Retry(
    total=3, backoff_factor=0.4,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"])
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
DEFAULT_TIMEOUT = 30

# ---- Helpers ----

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

_ALLOWED_AVOIDS = {"tollways", "ferries", "highways", "steps", "fords", "pavedroads", "unpavedroads"}

def _sanitize_avoids(avoid_features):
    if not avoid_features:
        return []
    return [a for a in avoid_features if a in _ALLOWED_AVOIDS]

def _kg_to_t_if_needed(v):
    """Converte kg para toneladas se parecer estar em kg (v > 1000)."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return v
    return v / 1000.0 if v > 1000 else v

def _call_ors_directions(profile, payload):
    try:
        r = _session.post(
            f"https://api.openrouteservice.org/v2/directions/{profile}/geojson",
            headers={"Authorization": ORS_KEY, "Content-Type": "application/json"},
            json=payload, timeout=DEFAULT_TIMEOUT
        )
        data = r.json()
    except ValueError:
        return JsonResponse({"error": "Falha ao interpretar resposta do ORS", "raw": r.text if 'r' in locals() else ""}, status=502)
    except Exception as e:
        return JsonResponse({"error": f"Erro de rede ao chamar ORS: {e}"}, status=502)

    if r.status_code != 200:
        return JsonResponse({"error": "Erro do ORS", "status": r.status_code, "detail": data}, status=r.status_code)
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

# ---- Endpoints ----

@csrf_exempt
def health(request):
    return JsonResponse({"ok": True})

@csrf_exempt
def geocode_search(request):
    """
    POST /api/geocode
    Body:
    {
      "q": "rua, cidade",
      "limit": 5,          # opcional
      "country": "BR"      # opcional (prioriza país)
    }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    if (err := _ensure_key()) is not None:
        return err

    try:
        body = json.loads(request.body.decode("utf-8"))
        q = (body.get("q") or "").strip()
        if not q:
            return JsonResponse({"error": "q (texto de busca) é obrigatório"}, status=400)

        size = int(body.get("limit") or 5)
        country = (body.get("country") or "").strip()

        params = {
            "api_key": ORS_KEY,
            "text": q,
            "size": size
        }
        if country:
            params["boundary.country"] = country

        r = _session.get("https://api.openrouteservice.org/geocode/search", params=params, timeout=DEFAULT_TIMEOUT)
        data = r.json()

        results = []
        for feat in data.get("features", []):
            coords = feat.get("geometry", {}).get("coordinates", [])
            props = feat.get("properties", {})
            if len(coords) == 2:
                results.append({
                    "label": props.get("label") or props.get("name") or "resultado",
                    "lng": coords[0],
                    "lat": coords[1],
                })

        return JsonResponse({"results": results}, status=200)
    except Exception as e:
        return JsonResponse({"error": f"Falha no geocode: {e}"}, status=500)

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
        avoid = _sanitize_avoids(body.get("avoid_features"))
        payload = {
            "coordinates": coords,
            "instructions": True,
        }
        options = {}
        if avoid:
            options["avoid_features"] = avoid
        if options:
            payload["options"] = options
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
            "height":   truck.get("height"),
            "width":    truck.get("width"),
            "length":   truck.get("length"),
            "weight":   _kg_to_t_if_needed(truck.get("weight")),     # toneladas
            "axleload": _kg_to_t_if_needed(truck.get("axleload")),   # toneladas
        }
        restrictions = {k: v for k, v in restrictions.items() if v is not None}

        avoid = _sanitize_avoids(body.get("avoid_features"))

        payload = {
            "coordinates": coords,
            "instructions": True,
        }
        options = {}
        if avoid:
            options["avoid_features"] = avoid
        if restrictions:
            options["profile_params"] = {"restrictions": restrictions}
        # informe o tipo de veículo (recomendado p/ driving-hgv)
        options["vehicle_type"] = "hgv"
        if options:
            payload["options"] = options
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    data = _call_ors_directions("driving-hgv", payload)
    if isinstance(data, JsonResponse):
        return data

    summary = _extract_summary(data)
    return JsonResponse({"summary": summary, "geojson": data}, status=200, safe=False)
