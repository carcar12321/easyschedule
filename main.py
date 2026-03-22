import os
import re
import random
import string
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import psycopg2
from fastapi import FastAPI, HTTPException, Depends, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel, validator

# ─────────────────────────────────────────────
# 앱 초기화 및 환경 변수
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Schedule Pro", version="4.0.0")
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "easytrip-secret-pro")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ─────────────────────────────────────────────
# Google Maps API 유틸리티
# ─────────────────────────────────────────────
def resolve_google_place(url: str):
    """URL을 분석하여 Place ID와 상호명, 좌표를 반환 (스크래핑 없음, 공식 API만 사용)"""
    if not GOOGLE_API_KEY:
        return None

    try:
        # 1. 단축 URL 해제 (Redirect 추적)
        if "goo.gl" in url or "maps.app.goo.gl" in url:
            response = requests.head(url, allow_redirects=True, timeout=5)
            url = response.url

        # 2. URL에서 Place ID 추출 시도
        place_id = None
        match = re.search(r'place_id:([^/&?]+)', url)
        if match:
            place_id = match.group(1)
        
        # 3. URL에 Place ID가 없다면 Text Search API로 검색
        # URL에서 상호명 부분 추출 (보통 /place/상호명/...)
        query = ""
        name_match = re.search(r'/place/([^/]+)', url)
        if name_match:
            query = requests.utils.unquote(name_match.group(1).replace('+', ' '))
        
        if not place_id and query:
            search_url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={GOOGLE_API_KEY}&language=ko"
            res = requests.get(search_url).json()
            if res.get("results"):
                place_id = res["results"][0]["place_id"]

        if place_id:
            # Place Details 호출 (상호명, 별점, 좌표, 영업정보)
            details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,geometry,opening_hours,business_status&key={GOOGLE_API_KEY}&language=ko"
            details = requests.get(details_url).json().get("result", {})
            return {
                "place_id": place_id,
                "name": details.get("name"),
                "lat": details.get("geometry", {}).get("location", {}).get("lat"),
                "lng": details.get("geometry", {}).get("location", {}).get("lng"),
                "rating": details.get("rating"),
                "open_now": details.get("opening_hours", {}).get("open_now")
            }
    except Exception as e:
        print(f"Google API Error: {e}")
    return None

# ─────────────────────────────────────────────
# DB 초기화 (새로운 컬럼 추가 포함)
# ─────────────────────────────────────────────
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # 기존 테이블 생성 및 마이그레이션
    c.execute("""
        CREATE TABLE IF NOT EXISTS room (
            room_id TEXT PRIMARY KEY, title TEXT NOT NULL, admin_pw TEXT NOT NULL, team_pw TEXT DEFAULT '',
            city TEXT DEFAULT '', currency TEXT DEFAULT 'JPY', member_count INTEGER DEFAULT 1,
            is_comment_enabled BOOLEAN DEFAULT FALSE, bookmark_name1 TEXT, bookmark_link1 TEXT,
            bookmark_name2 TEXT, bookmark_link2 TEXT, bookmark_name3 TEXT, bookmark_link3 TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, day_num INTEGER NOT NULL,
            start_time TEXT, end_time TEXT, content TEXT, author TEXT,
            google_map_url TEXT, tabelog_url TEXT, budget INTEGER, sort_order INTEGER DEFAULT 0,
            place_id TEXT, latitude FLOAT, longitude FLOAT, rating FLOAT
        )
    """)
    # (생략: flight, accommodation, suggestion, comment 테이블은 기존과 동일)
    
    # 컬럼 추가 마이그레이션 (기존 DB 대응)
    cols = [
        ("schedule", "place_id", "TEXT"),
        ("schedule", "latitude", "FLOAT"),
        ("schedule", "longitude", "FLOAT"),
        ("schedule", "rating", "FLOAT")
    ]
    for table, col, dtype in cols:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}")
        except: conn.rollback()

    conn.commit()
    c.close(); conn.close()

@app.on_event("startup")
def startup(): init_db()

# ─────────────────────────────────────────────
# 핵심 API: 일정 추가 (자동 상호명 완성)
# ─────────────────────────────────────────────
class ScheduleCreate(BaseModel):
    day_num: int; start_time: str; end_time: str; content: str
    google_map_url: Optional[str] = ""; budget: Optional[int] = None

@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    # 인증 로직 생략 (기존과 동일)
    nickname = "방장" # 예시
    
    place_data = None
    final_content = sch.content
    if sch.google_map_url:
        place_data = resolve_google_place(sch.google_map_url)
        if place_data and place_data.get("name"):
            # 입력한 내용이 없으면 상호명으로 대체, 있으면 '상호명 (입력내용)' 형식
            final_content = f"{place_data['name']} ({sch.content})" if sch.content else place_data['name']

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, budget, 
                             place_id, latitude, longitude, rating, sort_order)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
               (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))
    """, (room_id, sch.day_num, sch.start_time, sch.end_time, final_content, nickname, sch.google_map_url, sch.budget,
          place_data['place_id'] if place_data else None,
          place_data['lat'] if place_data else None,
          place_data['lng'] if place_data else None,
          place_data['rating'] if place_data else None,
          room_id, sch.day_num))
    conn.commit()
    c.close(); conn.close()
    return {"status": "ok", "auto_filled": True if place_data else False}

# ─────────────────────────────────────────────
# GPS 퀵 서치 & 이동 시간 계산 API
# ─────────────────────────────────────────────
@app.get("/api/nearby")
def get_nearby(lat: float, lng: float, type: str):
    """GPS 기반 주변 검색"""
    if not GOOGLE_API_KEY: return {"results": []}
    
    keywords = {
        "restaurant": "restaurant",
        "smoking": "smoking allowed cafe|smoking area",
        "convenience": "convenience_store"
    }
    
    search_url = (f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?"
                  f"location={lat},{lng}&radius=1000&type={keywords.get(type, 'point_of_interest')}"
                  f"&keyword={keywords.get(type, '')}&key={GOOGLE_API_KEY}&language=ko")
    
    res = requests.get(search_url).json()
    results = res.get("results", [])
    
    # 식당의 경우 별점 4.0 이상 필터링 + AI 추천 시뮬레이션
    if type == "restaurant":
        results = [r for r in results if r.get("rating", 0) >= 4.0]
        results = sorted(results, key=lambda x: x.get("rating", 0), reverse=True)[:3]
        for r in results:
            r["ai_desc"] = f"현지인 평점 {r['rating']}점으로 실패 없는 선택! 주변에서 가장 인기 있는 곳입니다."
    else:
        results = results[:3]

    return {"results": results}

@app.get("/api/travel_time")
def get_travel_time(origin: str, dest: str):
    """Distance Matrix API를 이용한 이동 시간 계산"""
    url = (f"https://maps.googleapis.com/maps/api/distancematrix/json?"
           f"origins=place_id:{origin}&destinations=place_id:{dest}&mode=transit&key={GOOGLE_API_KEY}&language=ko")
    res = requests.get(url).json()
    try:
        element = res['rows'][0]['elements'][0]
        return {"duration": element['duration']['text'], "distance": element['distance']['text']}
    except:
        return {"duration": None}

# (기존 나머지 API들 유지...)
