import os
import re
import random
import string
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import psycopg2
from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel, validator

# ─────────────────────────────────────────────
# 앱 설정 및 보안
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Schedule Pro", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "easytrip-pro-secret")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ─────────────────────────────────────────────
# Google Maps API 유틸리티
# ─────────────────────────────────────────────
def resolve_google_place(url: str, api_key: str):
    """구글맵 URL을 분석하여 장소 정보를 가져옴"""
    if not api_key: return None
    try:
        # 단축 URL 처리
        if "goo.gl" in url or "maps.app.goo.gl" in url:
            response = requests.head(url, allow_redirects=True, timeout=5)
            url = response.url

        place_id = None
        # URL에서 place_id 추출 시도
        match = re.search(r'place_id:([^/&?]+)', url)
        if match: place_id = match.group(1)
        
        # 검색어로 추출
        if not place_id:
            name_match = re.search(r'/place/([^/]+)', url)
            if name_match:
                query = requests.utils.unquote(name_match.group(1).replace('+', ' '))
                search_url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={api_key}&language=ko"
                res = requests.get(search_url).json()
                if res.get("results"): place_id = res["results"][0]["place_id"]

        if place_id:
            details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,geometry,opening_hours&key={api_key}&language=ko"
            details = requests.get(details_url).json().get("result", {})
            return {
                "place_id": place_id,
                "name": details.get("name"),
                "lat": details.get("geometry", {}).get("location", {}).get("lat"),
                "lng": details.get("geometry", {}).get("location", {}).get("lng"),
                "rating": details.get("rating"),
                "open_now": details.get("opening_hours", {}).get("open_now")
            }
    except: pass
    return None

# ─────────────────────────────────────────────
# DB 초기화 및 마이그레이션
# ─────────────────────────────────────────────
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS room (
            room_id TEXT PRIMARY KEY, title TEXT NOT NULL, admin_pw TEXT NOT NULL, team_pw TEXT DEFAULT '',
            city TEXT DEFAULT '', currency TEXT DEFAULT 'JPY', member_count INTEGER DEFAULT 1,
            is_comment_enabled BOOLEAN DEFAULT FALSE, bookmark_name1 TEXT DEFAULT '', bookmark_link1 TEXT DEFAULT '',
            bookmark_name2 TEXT DEFAULT '', bookmark_link2 TEXT DEFAULT '', bookmark_name3 TEXT DEFAULT '', bookmark_link3 TEXT DEFAULT ''
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, day_num INTEGER NOT NULL,
            start_time TEXT, end_time TEXT, content TEXT, author TEXT,
            google_map_url TEXT, budget INTEGER, sort_order INTEGER DEFAULT 0,
            place_id TEXT, latitude FLOAT, longitude FLOAT, rating FLOAT
        )
    """)
    # (다른 테이블 생략 - 기존 구조 유지)
    
    # 마이그레이션: 필수 컬럼 추가
    for col, dtype in [("place_id", "TEXT"), ("latitude", "FLOAT"), ("longitude", "FLOAT"), ("rating", "FLOAT")]:
        try: c.execute(f"ALTER TABLE schedule ADD COLUMN IF NOT EXISTS {col} {dtype}")
        except: conn.rollback()
    conn.commit()
    c.close(); conn.close()

@app.on_event("startup")
def startup(): init_db()

# ─────────────────────────────────────────────
# API 엔드포인트
# ─────────────────────────────────────────────

@app.get("/")
@app.get("/{room_id}")
def serve_index(room_id: str = None):
    return FileResponse("index.html")

# [핵심] 일정 추가 API (헤더에서 API 키 받음)
class ScheduleCreate(BaseModel):
    day_num: int; start_time: str; end_time: str; content: str
    google_map_url: Optional[str] = ""; budget: Optional[int] = None

@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate, x_google_api_key: Optional[str] = Header(None), credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    # 로그인 여부 확인 및 닉네임 가져오기 (기존 로직 요약)
    author = "방장" 
    
    place_data = None
    final_content = sch.content
    if sch.google_map_url and x_google_api_key:
        place_data = resolve_google_place(sch.google_map_url, x_google_api_key)
        if place_data and place_data.get("name"):
            final_content = f"{place_data['name']} ({sch.content})" if sch.content else place_data['name']

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, budget, 
                             place_id, latitude, longitude, rating, sort_order)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
               (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))
    """, (room_id, sch.day_num, sch.start_time, sch.end_time, final_content, author, sch.google_map_url, sch.budget,
          place_data['place_id'] if place_data else None,
          place_data['lat'] if place_data else None,
          place_data['lng'] if place_data else None,
          place_data['rating'] if place_data else None,
          room_id, sch.day_num))
    conn.commit()
    c.close(); conn.close()
    return {"status": "ok"}

# [핵심] 주변 검색 API
@app.get("/api/nearby")
def get_nearby(lat: float, lng: float, type: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key: return {"results": []}
    keywords = {"restaurant": "restaurant", "smoking": "smoking allowed cafe|smoking area", "convenience": "convenience_store"}
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius=1000&keyword={keywords.get(type)}&key={x_google_api_key}&language=ko"
    res = requests.get(url).json()
    results = res.get("results", [])
    if type == "restaurant":
        results = [r for r in results if r.get("rating", 0) >= 4.0][:3]
        for r in results: r["ai_desc"] = "현지인 추천 맛집입니다!"
    return {"results": results[:3]}

# [핵심] 이동 시간 계산 API
@app.get("/api/travel_time")
def get_travel_time(origin: str, dest: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key: return {"duration": None}
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins=place_id:{origin}&destinations=place_id:{dest}&mode=transit&key={x_google_api_key}&language=ko"
    res = requests.get(url).json()
    try:
        element = res['rows'][0]['elements'][0]
        return {"duration": element['duration']['text'], "distance": element['distance']['text']}
    except: return {"duration": None}

# (방 생성, 로그인, 데이터 조회 등 기존 API 코드는 그대로 유지/포함시키세요)
# ... [이전 코드의 create_room, login, get_room_data 등 복사] ...
