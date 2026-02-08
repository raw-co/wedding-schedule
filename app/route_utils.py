import json
import os
import urllib.parse
import urllib.request
from typing import Optional, Tuple

# ✅ Kakao 버전 (운영용으로 가장 현실적인 구성)
# - 지오코딩: Kakao Local (주소 검색)
# - 경로/시간: KakaoMobility 길찾기 (자동차 길찾기)
#
# 필요한 환경변수:
#   KAKAO_REST_API_KEY=...   (Kakao Developers REST API Key)
#
# 참고 문서:
# - Kakao Local 주소 검색: Authorization: KakaoAK {REST_API_KEY}
# - KakaoMobility 자동차 길찾기: GET https://apis-navi.kakaomobility.com/v1/directions
#
# ⚠️ 주의
# - 키는 소스에 하드코딩하지 말고 .env / 환경변수로 관리하세요.
# - 이 구현은 프로토타입(서버사이드 호출)용입니다.

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()

def _http_get_json(url: str, headers: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def geocode_kakao(address: str) -> Optional[Tuple[float, float]]:
    """주소 -> (lat, lon)"""
    address = (address or "").strip()
    if not address or not KAKAO_REST_API_KEY:
        return None

    q = urllib.parse.quote(address)
    url = f"https://dapi.kakao.com/v2/local/search/address.json?query={q}&size=1"
    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
        "Accept": "application/json",
    }
    data = _http_get_json(url, headers=headers)

    docs = data.get("documents") or []
    if not docs:
        return None

    # Kakao: x=longitude, y=latitude (문자열)
    try:
        lon = float(docs[0]["x"])
        lat = float(docs[0]["y"])
        return lat, lon
    except Exception:
        return None

def route_minutes_kakaomobility(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> Optional[int]:
    """(lat,lon) -> minutes"""
    if not KAKAO_REST_API_KEY:
        return None

    # KakaoMobility: origin/destination은 "x,y" (lon,lat 아님! 문서에서 X좌표, Y좌표)
    origin = f"{origin_lon},{origin_lat}"
    dest = f"{dest_lon},{dest_lat}"

    params = {
        "origin": origin,
        "destination": dest,
        "summary": "false",
        "priority": "RECOMMEND",
        "alternatives": "false",
        "road_details": "false",
    }
    url = "https://apis-navi.kakaomobility.com/v1/directions?" + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = _http_get_json(url, headers=headers)

    routes = data.get("routes") or []
    if not routes:
        return None

    summary = routes[0].get("summary") or {}
    duration = summary.get("duration")  # seconds
    if duration is None:
        return None

    try:
        minutes = int(round(float(duration) / 60.0))
        return max(1, minutes)
    except Exception:
        return None

def estimate_travel_minutes(origin_address: str, dest_address: str) -> Optional[int]:
    """주소 -> 이동시간(분). 키가 없거나 실패하면 None."""
    o = geocode_kakao(origin_address)
    d = geocode_kakao(dest_address)
    if not o or not d:
        return None
    return route_minutes_kakaomobility(o[0], o[1], d[0], d[1])
