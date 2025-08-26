import json
import re
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

# ---- Parse de maxheight (OSM -> metros) ----

_FEET_IN_M = 0.3048

def _to_float(s):
    try:
        return float(s)
    except Exception:
        try:
            return float(str(s).replace(",", "."))  # "3,8" -> 3.8
        except Exception:
            return None

def _parse_maxheight_to_meters(raw):
    """
    Converte valores OSM comuns para metros.
    Exemplos aceitos:
      "3.5", "3,5", "3.5 m", "3,5 m", "10'6\"", "10' 6\"", "10 ft", "10ft"
    Retorna float em metros ou None.
    """
    if not raw:
        return None
    s = str(raw).strip().lower()

    # 1) Já em metros explícitos ou implícitos
    m_num = re.match(r"^\s*([0-9]+[.,]?[0-9]*)\s*(m|meter|metros)?\s*$", s)
    if m_num:
        return _to_float(m_num.group(1))

    # 2) Notação pés e polegadas: 10'6" ou 10' 6"
    m_ft_in = re.match(r"^\s*(\d+)\s*'\s*([0-9]+)\s*\"?\s*$", s)
    if m_ft_in:
        ft = int(m_ft_in.group(1))
        inch = int(m_ft_in.group(2))
        return ft * _FEET_IN_M + (inch/12.0) * _FEET_IN_M

    # 3) Apenas pés: "10ft", "10 ft"
    m_ft = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(ft|foot|feet)\s*$", s)
    if m_ft:
        return _to_float(m_ft.group(1)) * _FEET_IN_M

    # 4) Valor puro onde não sabemos unidade: assume metros
    val = _to_float(s)
    return val

# ---- Endpoints ----

@csrf_exempt
def health(request):
    return JsonResponse({"ok": True})

@csrf_exempt
def geocode_search(request):
    """
    POST /api/geocode
    Body (todos os campos opcionais exceto q):
    {
      "q": "texto de busca",                 # obrigatório
      "limit": 5,                            # default=5
      "country": "BR",                       # default="BR" (BR inteiro). Envie "" para sem filtro.
      "lang": "pt",                          # default="pt"
      "focus_lat": -23.5, "focus_lng": -46.6,# prioriza perto de um ponto (não restringe)
      "rect_north": -22.0, "rect_south": -24.0, "rect_east": -43.0, "rect_west": -46.0  # restringe aos bounds
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
        country = body.get("country")
        if country is None:
            country = "BR"
        lang = (body.get("lang") or "pt").strip()

        params = {
            "api_key": ORS_KEY,
            "text": q,
            "size": size,
            "lang": lang,
        }

        if isinstance(country, str) and country.strip():
            params["boundary.country"] = country.strip()

        if body.get("focus_lat") is not None and body.get("focus_lng") is not None:
            params["focus.point.lat"] = float(body["focus_lat"])
            params["focus.point.lon"] = float(body["focus_lng"])

        rect_keys = ["rect_north", "rect_south", "rect_east", "rect_west"]
        if all(k in body for k in rect_keys):
            params["boundary.rect.min_lat"] = float(body["rect_south"])
            params["boundary.rect.min_lon"] = float(body["rect_west"])
            params["boundary.rect.max_lat"] = float(body["rect_north"])
            params["boundary.rect.max_lon"] = float(body["rect_east"])

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

@csrf_exempt
def obstaculos_altura(request):
    """
    POST /api/obstaculos-altura
    Body:
    {
      "bbox": { "south": -25.60, "west": -54.65, "north": -25.45, "east": -54.50 },
      "limit": 500,                 # opcional (default=500)
      "vehicle_height_m": 3.8       # opcional: filtra só obstáculos < altura do veículo
    }

    Retorna:
    {
      "features": [
        { "lat":..., "lng":..., "maxheight": "3.5 m", "maxheight_m": 3.5, "kind": "bridge|tunnel|way", "osm_id": 123 },
        ...
      ],
      "filtered_by_height": true|false
    }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8"))
        bbox = body.get("bbox") or {}
        south = float(bbox["south"]); west = float(bbox["west"])
        north = float(bbox["north"]); east = float(bbox["east"])
        limit = int(body.get("limit") or 500)
        vehicle_height_m = body.get("vehicle_height_m")
        vehicle_height_m = float(vehicle_height_m) if vehicle_height_m is not None else None
    except Exception:
        return JsonResponse({"error": "bbox ou parâmetros inválidos"}, status=400)

    overpass_url = "https://overpass-api.de/api/interpreter"
    # Nós e vias com maxheight / maxheight:physical dentro do bbox
    query = f"""
    [out:json][timeout:25];
    (
      node["maxheight"]({south},{west},{north},{east});
      way["maxheight"]({south},{west},{north},{east});
      node["maxheight:physical"]({south},{west},{north},{east});
      way["maxheight:physical"]({south},{west},{north},{east});
    );
    out tags center {limit};
    """

    try:
        r = _session.post(overpass_url, data={"data": query}, timeout=DEFAULT_TIMEOUT)
        data = r.json()
    except Exception as e:
        return JsonResponse({"error": f"Falha Overpass: {e}"}, status=502)

    feats = []
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        raw = tags.get("maxheight") or tags.get("maxheight:physical")
        if not raw:
            continue

        mh_m = _parse_maxheight_to_meters(raw)
        # coordenadas
        if el.get("type") == "node":
            lat = el.get("lat"); lon = el.get("lon")
        else:
            center = el.get("center") or {}
            lat = center.get("lat"); lon = center.get("lon")
            if lat is None or lon is None:
                continue

        kind = "way"
        if tags.get("bridge") == "yes": kind = "bridge"
        if tags.get("tunnel") == "yes": kind = "tunnel"

        feats.append({
            "lat": lat, "lng": lon, "maxheight": raw, "maxheight_m": mh_m,
            "kind": kind, "osm_id": el.get("id")
        })

    # filtro por altura do veículo (se passado)
    filtered = False
    if vehicle_height_m is not None:
        filtered = True
        feats = [f for f in feats if (f.get("maxheight_m") is not None and f["maxheight_m"] < vehicle_height_m)]

    # aplica limite final, se necessário
    if limit and len(feats) > limit:
        feats = feats[:limit]

    return JsonResponse({"features": feats, "filtered_by_height": filtered}, status=200)
