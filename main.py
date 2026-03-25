import os
import re
import json
import base64
import random
import string
import math
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
# 앱 초기화
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Schedule Pro API", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# 보안 설정
# ─────────────────────────────────────────────
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "easytrip-default-secret-change-in-production")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7   # 7일
TEST_ADMIN_PASSWORD = os.environ.get("EASY_TEST_ADMIN_PASSWORD")

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

# ─────────────────────────────────────────────
# DB 설정
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ─────────────────────────────────────────────
# Gemini 모델 매핑 (최신 Gemini 3.1 Pro 및 3 Flash 모델 직접 호출)
# ─────────────────────────────────────────────
GEMINI_MODEL_MAP = {
    "gemini-3-flash": "gemini-3-flash",
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.0-flash": "gemini-2.0-flash",
    "gemini-1.5-flash": "gemini-1.5-flash",
    "gemini-1.5-pro": "gemini-1.5-pro",
}

# ─────────────────────────────────────────────
# Google Maps API 유틸리티
# ─────────────────────────────────────────────
def get_google_place_info(url: str, api_key: str):
    """구글맵 URL에서 장소 정보를 가져오는 공식 API 로직"""
    if not api_key or not url:
        return None
    try:
        if "goo.gl" in url or "maps.app.goo.gl" in url:
            res = requests.head(url, allow_redirects=True, timeout=5)
            url = res.url

        name_match = re.search(r'/place/([^/]+)', url)
        if name_match:
            query = requests.utils.unquote(name_match.group(1).replace('+', ' '))
            search_res = requests.get(
                f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={api_key}&language=ko",
                timeout=5
            ).json()

            if search_res.get("results"):
                place_id = search_res["results"][0]["place_id"]
                details_res = requests.get(
                    f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,geometry,opening_hours&key={api_key}&language=ko",
                    timeout=5
                ).json().get("result", {})

                return {
                    "place_id": place_id,
                    "name": details_res.get("name"),
                    "rating": details_res.get("rating"),
                    "lat": details_res.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": details_res.get("geometry", {}).get("location", {}).get("lng"),
                }
    except Exception as e:
        print(f"Google API Error: {e}")
    return None


def get_google_place_info_by_name(name: str, api_key: str):
    """
    장소명(문자열)으로 구글 Text Search API를 호출하여
    place_id, 위도, 경도, 구글맵 URL을 반환합니다.
    AI 자동 생성 일정의 장소 정보 자동 첨부에 사용됩니다.
    """
    if not api_key or not name:
        return None
    try:
        search_res = requests.get(
            f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={requests.utils.quote(name)}&key={api_key}&language=ko",
            timeout=5
        ).json()

        if search_res.get("results"):
            place = search_res["results"][0]
            place_id = place.get("place_id", "")
            return {
                "place_id": place_id,
                "name": place.get("name"),
                "rating": place.get("rating"),
                "lat": place.get("geometry", {}).get("location", {}).get("lat"),
                "lng": place.get("geometry", {}).get("location", {}).get("lng"),
                "map_url": f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else "",
            }
    except Exception as e:
        print(f"Google Place Name Search Error: {e}")
    return None

def calc_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def google_places_nearby(api_key: str, lat: float, lng: float, radius: int = 900, keyword: str = "", place_type: str = "", language: str = "ko"):
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "key": api_key,
        "language": language
    }
    if keyword:
        params["keyword"] = keyword
    if place_type:
        params["type"] = place_type
    res = requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params=params,
        timeout=8
    ).json()
    return res.get("status", "UNKNOWN_ERROR"), res.get("results", [])

def google_places_text_search(api_key: str, query: str, lat: float, lng: float, radius: int = 1200, language: str = "ko"):
    res = requests.get(
        "https://maps.googleapis.com/maps/api/place/textsearch/json",
        params={
            "query": query,
            "location": f"{lat},{lng}",
            "radius": radius,
            "key": api_key,
            "language": language
        },
        timeout=8
    ).json()
    return res.get("status", "UNKNOWN_ERROR"), res.get("results", [])

def google_distance_matrix_walking(api_key: str, origin_lat: float, origin_lng: float, destinations: List[dict]):
    if not destinations:
        return {}
    dests = "|".join([f"{d['lat']},{d['lng']}" for d in destinations if d.get("lat") is not None and d.get("lng") is not None])
    if not dests:
        return {}
    res = requests.get(
        "https://maps.googleapis.com/maps/api/distancematrix/json",
        params={
            "origins": f"{origin_lat},{origin_lng}",
            "destinations": dests,
            "mode": "walking",
            "key": api_key,
            "language": "ko"
        },
        timeout=8
    ).json()
    data = {}
    rows = res.get("rows", [])
    if not rows:
        return data
    elements = rows[0].get("elements", [])
    valid_dests = [d for d in destinations if d.get("lat") is not None and d.get("lng") is not None]
    for i, el in enumerate(elements):
        if i >= len(valid_dests):
            continue
        item = valid_dests[i]
        if el.get("status") == "OK":
            data[item["place_id"]] = {
                "distance_text": el.get("distance", {}).get("text", ""),
                "distance_value": el.get("distance", {}).get("value"),
                "walking_time_text": el.get("duration", {}).get("text", ""),
                "walking_time_value": el.get("duration", {}).get("value")
            }
    return data

def parse_food_preferences_with_gemini(user_text: str, llm_key: str):
    if not llm_key or not user_text.strip():
        return {}
    prompt = f"""다음 사용자 문장을 식당 추천용 JSON으로 정규화해줘.
문장: "{user_text}"
키는 반드시 menu_keywords(배열), mood_keywords(배열), solo_ok(불리언 또는 null), exclude_keywords(배열), priority(문자열)만 사용.
설명 없이 JSON 한 개만 출력."""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={llm_key}",
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}},
            timeout=12
        )
        if not res.ok:
            return {}
        text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```(json)?\n?|```$", "", text, flags=re.IGNORECASE).strip()
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}

def build_google_search_url(query: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(query)}"

def is_meal_slot(content_text: str) -> bool:
    t = (content_text or "").lower()
    keywords = ["점심", "저녁", "브런치", "아침", "식사", "lunch", "dinner", "breakfast", "meal"]
    return any(k in t for k in keywords)

def fetch_meal_candidates(api_key: str, city: str, content_text: str, lat: Optional[float], lng: Optional[float], limit: int = 3):
    if not api_key:
        return []
    candidates = []
    try:
        if lat is not None and lng is not None:
            _, nearby = google_places_nearby(api_key, lat, lng, radius=1300, place_type="restaurant", language="ko")
            candidates.extend(nearby[:10])
        query = f"{city} {content_text} 맛집".strip()
        _, ts = google_places_text_search(api_key, query, lat or 0, lng or 0, radius=2500, language="ko")
        candidates.extend(ts[:10])
    except Exception:
        pass
    seen = set()
    picked = []
    for p in candidates:
        pid = p.get("place_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        rating = p.get("rating")
        picked.append({
            "name": p.get("name", "이름 없음"),
            "rating": rating,
            "map_url": f"https://www.google.com/maps/place/?q=place_id:{pid}",
            "reason": f"평점 {rating}점 및 접근성이 비교적 좋아 보여 추천합니다." if rating else "주변 동선 기준으로 접근성이 좋아 보여 추천합니다."
        })
        if len(picked) >= limit:
            break
    return picked

def parse_route_query(message: str):
    text = (message or "").strip()
    m = re.search(r"(.+?)에서\s*(.+?)까지", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None

# ───────────────────────────────────────────
# 도시 ↔ 통화 매핑
# ─────────────────────────────────────────────
CITY_CURRENCY_MAP = {
    "도쿄": "JPY", "오사카": "JPY", "나고야": "JPY", "후쿠오카": "JPY",
    "구마모토": "JPY", "가고시마": "JPY", "삿포로": "JPY", "오키나와": "JPY",
    "타이베이": "TWD", "가오슝": "TWD",
}

CURRENCY_SYMBOL_MAP = {
    "JPY": "¥", "TWD": "NT$", "KRW": "₩", "USD": "$", "EUR": "€", "OTHER": "",
}

def resolve_currency(city: str, currency_override: Optional[str] = None) -> str:
    if currency_override:
        return currency_override
    return CITY_CURRENCY_MAP.get(city, "OTHER")

# ─────────────────────────────────────────────
# DB 초기화 및 마이그레이션
# ─────────────────────────────────────────────
def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS room (
            room_id          TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            admin_pw         TEXT NOT NULL,
            team_pw          TEXT DEFAULT '',
            city             TEXT DEFAULT '',
            currency         TEXT DEFAULT 'JPY',
            member_count     INTEGER DEFAULT 1,
            is_comment_enabled BOOLEAN DEFAULT FALSE,
            bookmark_name1   TEXT DEFAULT '',
            bookmark_link1   TEXT DEFAULT '',
            bookmark_name2   TEXT DEFAULT '',
            bookmark_link2   TEXT DEFAULT '',
            bookmark_name3   TEXT DEFAULT '',
            bookmark_link3   TEXT DEFAULT '',
            test_admin_pw    TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id              SERIAL PRIMARY KEY,
            room_id         TEXT NOT NULL,
            day_num         INTEGER NOT NULL,
            start_time      TEXT NOT NULL,
            end_time        TEXT NOT NULL,
            content         TEXT NOT NULL,
            author          TEXT DEFAULT '방장',
            google_map_url  TEXT DEFAULT '',
            tabelog_url     TEXT DEFAULT '',
            budget          INTEGER,
            sort_order      INTEGER DEFAULT 0,
            place_id        TEXT,
            latitude        FLOAT,
            longitude       FLOAT,
            rating          FLOAT,
            ai_options_json TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS flight (
            id              SERIAL PRIMARY KEY,
            room_id         TEXT NOT NULL,
            flight_type     TEXT NOT NULL,
            airport         TEXT NOT NULL,
            flight_num      TEXT NOT NULL,
            terminal        TEXT DEFAULT '',
            departure_time  TEXT NOT NULL,
            arrival_time    TEXT NOT NULL,
            memo            TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS accommodation (
            id              SERIAL PRIMARY KEY,
            room_id         TEXT NOT NULL,
            days_applied    TEXT NOT NULL,
            hotel_name      TEXT NOT NULL,
            google_map_url  TEXT DEFAULT '',
            has_breakfast   BOOLEAN DEFAULT FALSE,
            budget          INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS suggestion (
            id              SERIAL PRIMARY KEY,
            room_id         TEXT NOT NULL,
            suggester_name  TEXT NOT NULL,
            content         TEXT NOT NULL,
            google_map_url  TEXT DEFAULT '',
            tabelog_url     TEXT DEFAULT '',
            good_cnt        INTEGER DEFAULT 0,
            bad_cnt         INTEGER DEFAULT 0,
            status          TEXT DEFAULT '대기중'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS comment (
            id          SERIAL PRIMARY KEY,
            schedule_id INTEGER NOT NULL,
            writer_name TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    migrations = [
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS city TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'JPY'",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS member_count INTEGER DEFAULT 1",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS is_comment_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_name1 TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_link1 TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_name2 TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_link2 TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_name3 TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS bookmark_link3 TEXT DEFAULT ''",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS budget INTEGER",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS start_time TEXT DEFAULT ''",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS end_time TEXT DEFAULT ''",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS author TEXT DEFAULT '방장'",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS place_id TEXT",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS latitude FLOAT",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS longitude FLOAT",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS rating FLOAT",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS ai_options_json TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS test_admin_pw TEXT DEFAULT ''",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            conn.rollback()

    conn.commit()
    c.close()
    conn.close()

@app.on_event("startup")
def startup_event():
    init_db()
    # ─────────────────────────────────────────────
# JWT 유틸 및 인증 로직
# ─────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

def get_current_user_info(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not credentials:
        return "guest", "훈수꾼"
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("room_id") != room_id:
        return "guest", "훈수꾼"
    return payload.get("role", "guest"), payload.get("nickname", "훈수꾼")

ADMIN_PW_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{6,}$")

def validate_admin_pw(pw: str) -> bool:
    return bool(ADMIN_PW_PATTERN.match(pw))

def hash_pw(pw: str) -> str:
    return pwd_context.hash(pw)

def verify_pw(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    return pwd_context.verify(plain, hashed)

# ─────────────────────────────────────────────
# Pydantic 데이터 모델
# ─────────────────────────────────────────────
class RoomCreate(BaseModel):
    title: str
    admin_pw: str
    team_pw: Optional[str] = ""
    city: Optional[str] = ""
    currency: Optional[str] = ""
    member_count: Optional[int] = 1
    is_comment_enabled: Optional[bool] = False

    @validator("admin_pw")
    def validate_pw(cls, v):
        if not validate_admin_pw(v):
            raise ValueError("비밀번호는 영문과 숫자를 포함하여 6자 이상이어야 합니다.")
        return v

class RoomUpdate(BaseModel):
    password: str
    title: Optional[str] = None
    team_pw: Optional[str] = None
    city: Optional[str] = None
    currency: Optional[str] = None
    member_count: Optional[int] = None
    is_comment_enabled: Optional[bool] = None
    bookmark_name1: Optional[str] = None
    bookmark_link1: Optional[str] = None
    bookmark_name2: Optional[str] = None
    bookmark_link2: Optional[str] = None
    bookmark_name3: Optional[str] = None
    bookmark_link3: Optional[str] = None

class LoginRequest(BaseModel):
    room_id: str
    password: str
    nickname: Optional[str] = ""

class ScheduleCreate(BaseModel):
    day_num: int
    start_time: str
    end_time: str
    content: str
    google_map_url: Optional[str] = ""
    tabelog_url: Optional[str] = ""
    budget: Optional[int] = None

# [패치 #7/#8] 일정 수정 DTO
class ScheduleUpdate(BaseModel):
    day_num: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    content: Optional[str] = None
    google_map_url: Optional[str] = None
    tabelog_url: Optional[str] = None
    budget: Optional[int] = None

# [패치 #1/#3] AI 스케줄 고도화 DTO
class AiScheduleRequest(BaseModel):
    city: str
    days: int
    model: Optional[str] = "gemini-2.5-flash"
    keep_existing: Optional[bool] = False
    feedback: Optional[str] = ""
    target_days: Optional[List[int]] = None

class AiScheduleEditRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-2.5-flash"
    target_days: Optional[List[int]] = None

class OmniAssistantRequest(BaseModel):
    message: str
    lat: Optional[float] = None
    lng: Optional[float] = None

class ImportRequest(BaseModel):
    export_code: str
    clear_existing: bool = False

class FlightCreate(BaseModel):
    flight_type: str
    airport: str
    flight_num: str
    terminal: Optional[str] = ""
    departure_time: str
    arrival_time: str
    memo: Optional[str] = ""

class AccommodationCreate(BaseModel):
    days_applied: List[int]
    hotel_name: str
    google_map_url: Optional[str] = ""
    has_breakfast: bool = False
    budget: Optional[int] = None

class ReorderRequest(BaseModel):
    new_order: List[int]

class SuggestionCreate(BaseModel):
    suggester_name: str
    content: str
    google_map_url: Optional[str] = ""
    tabelog_url: Optional[str] = ""

class ApproveRequest(BaseModel):
    day_num: int
    start_time: str
    end_time: str

class CommentCreate(BaseModel):
    writer_name: str
    content: str

class WhatToEatRequest(BaseModel):
    lat: float
    lng: float
    user_text: Optional[str] = ""

# ─────────────────────────────────────────────
# 정적 파일 & 로그인 API
# ─────────────────────────────────────────────
@app.get("/")
@app.get("/{room_id}")
def serve_frontend(room_id: str = None):
    return FileResponse("index.html")

@app.post("/auth/login")
def login(req: LoginRequest):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw, test_admin_pw FROM room WHERE room_id=%s", (req.room_id,))
    row = c.fetchone()

    if not row:
        c.close()
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    admin_pw_hash, team_pw_hash, test_admin_pw_hash = row
    c.close()
    conn.close()

    if verify_pw(req.password, admin_pw_hash):
        role = "admin"
        nickname = "방장"
    elif test_admin_pw_hash and verify_pw(req.password, test_admin_pw_hash):
        role = "admin"
        nickname = "방장(테스트)"
    elif team_pw_hash and verify_pw(req.password, team_pw_hash):
        role = "team"
        if not req.nickname.strip():
            raise HTTPException(status_code=400, detail="동행자는 닉네임을 반드시 입력해야 합니다.")
        nickname = f"동행자 {req.nickname.strip()[:8]}"
    else:
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")

    token = create_access_token({"sub": req.room_id, "room_id": req.room_id, "role": role, "nickname": nickname})
    return {"access_token": token, "token_type": "bearer", "role": role, "nickname": nickname}

@app.post("/create_room", status_code=201)
def create_room(room: RoomCreate):
    try:
        rid = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        currency = resolve_currency(room.city or "", room.currency or "")
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """INSERT INTO room
                   (room_id, title, admin_pw, team_pw, city, currency, member_count, is_comment_enabled, test_admin_pw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (rid, room.title, hash_pw(room.admin_pw), hash_pw(room.team_pw) if room.team_pw else "",
             room.city or "", currency, room.member_count or 1, room.is_comment_enabled,
             hash_pw(TEST_ADMIN_PASSWORD) if TEST_ADMIN_PASSWORD else "")
        )
        conn.commit()
        c.close()
        conn.close()
        return {"room_id": rid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"서버 내부 오류: {str(e)}")

# ─────────────────────────────────────────────
# 메인 데이터 로드 API
# ─────────────────────────────────────────────
@app.get("/room/{room_id}/data")
def get_room_data(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT title, city, currency, member_count, is_comment_enabled,
               bookmark_name1, bookmark_link1, bookmark_name2, bookmark_link2, bookmark_name3, bookmark_link3
        FROM room WHERE room_id=%s
    """, (room_id,))
    room_row = c.fetchone()
    if not room_row:
        c.close()
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    (title, city, currency, member_count, is_comment_enabled,
     b_n1, b_l1, b_n2, b_l2, b_n3, b_l3) = room_row

    currency_symbol = CURRENCY_SYMBOL_MAP.get(currency, "")
    bookmarks = []
    if b_n1 and b_l1: bookmarks.append({"name": b_n1, "url": b_l1})
    if b_n2 and b_l2: bookmarks.append({"name": b_n2, "url": b_l2})
    if b_n3 and b_l3: bookmarks.append({"name": b_n3, "url": b_l3})

    c.execute("SELECT id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo FROM flight WHERE room_id=%s ORDER BY id ASC", (room_id,))
    flights = [{"id": r[0], "flight_type": r[1], "airport": r[2], "flight_num": r[3], "terminal": r[4], "departure_time": r[5], "arrival_time": r[6], "memo": r[7]} for r in c.fetchall()]

    c.execute("SELECT id, days_applied, hotel_name, google_map_url, has_breakfast, budget FROM accommodation WHERE room_id=%s ORDER BY id ASC", (room_id,))
    accommodations = [{"id": r[0], "days_applied": [int(x) for x in r[1].split(',') if x], "hotel_name": r[2], "google_map_url": r[3], "has_breakfast": r[4], "budget": r[5]} for r in c.fetchall()]

    c.execute(
        """SELECT id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, rating, ai_options_json
           FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC, start_time ASC""",
        (room_id,)
    )
    schedules = [
        {"id": r[0], "day_num": r[1], "start_time": r[2], "end_time": r[3], "content": r[4],
         "author": r[5], "google_map_url": r[6] or "", "tabelog_url": r[7] or "", "budget": r[8],
         "place_id": r[9], "rating": r[10], "ai_options_json": r[11] or ""}
        for r in c.fetchall()
    ]

    schedule_ids = [s["id"] for s in schedules]
    comments_map: dict = {s["id"]: [] for s in schedules}
    if schedule_ids:
        c.execute(
            """SELECT id, schedule_id, writer_name, content, to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI')
               FROM comment WHERE schedule_id = ANY(%s) ORDER BY created_at ASC""",
            (schedule_ids,)
        )
        for row in c.fetchall():
            comments_map[row[1]].append({"id": row[0], "writer_name": row[2], "content": row[3], "created_at": row[4]})
    for s in schedules:
        s["comments"] = comments_map[s["id"]]

    c.execute(
        """SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status
           FROM suggestion WHERE room_id=%s ORDER BY id DESC""", (room_id,)
    )
    suggestions = [{"id": r[0], "suggester_name": r[1], "content": r[2], "google_map_url": r[3] or "",
                    "tabelog_url": r[4] or "", "good_cnt": r[5], "bad_cnt": r[6], "status": r[7]} for r in c.fetchall()]

    c.close()
    conn.close()

    return {
        "room_id": room_id, "title": title, "city": city, "currency": currency,
        "currency_symbol": currency_symbol, "member_count": member_count,
        "is_comment_enabled": is_comment_enabled, "role": role, "nickname": nickname,
        "bookmarks": bookmarks, "flights": flights, "accommodations": accommodations,
        "schedules": schedules, "suggestions": suggestions
    }

# ─────────────────────────────────────────────
# 방 설정 및 북마크 수정
# ─────────────────────────────────────────────
@app.patch("/room/{room_id}/settings")
def update_room_settings(room_id: str, req: RoomUpdate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    row = c.fetchone()

    if not row or not verify_pw(req.password, row[0]):
        c.close()
        conn.close()
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 올바르지 않습니다.")

    fields = []
    values = []

    if req.title is not None: fields.append("title=%s"); values.append(req.title)
    if req.team_pw is not None: fields.append("team_pw=%s"); values.append(hash_pw(req.team_pw) if req.team_pw else "")
    if req.member_count is not None: fields.append("member_count=%s"); values.append(req.member_count)
    if req.is_comment_enabled is not None: fields.append("is_comment_enabled=%s"); values.append(req.is_comment_enabled)
    if req.bookmark_name1 is not None: fields.append("bookmark_name1=%s"); values.append(req.bookmark_name1)
    if req.bookmark_link1 is not None: fields.append("bookmark_link1=%s"); values.append(req.bookmark_link1)
    if req.bookmark_name2 is not None: fields.append("bookmark_name2=%s"); values.append(req.bookmark_name2)
    if req.bookmark_link2 is not None: fields.append("bookmark_link2=%s"); values.append(req.bookmark_link2)
    if req.bookmark_name3 is not None: fields.append("bookmark_name3=%s"); values.append(req.bookmark_name3)
    if req.bookmark_link3 is not None: fields.append("bookmark_link3=%s"); values.append(req.bookmark_link3)

    if fields:
        values.append(room_id)
        c.execute(f"UPDATE room SET {', '.join(fields)} WHERE room_id=%s", values)
        conn.commit()

    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 여행 코스 Export / Import
# ─────────────────────────────────────────────
@app.get("/room/{room_id}/export")
def export_room_data(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo FROM flight WHERE room_id=%s", (room_id,))
    flights = [{"flight_type": r[0], "airport": r[1], "flight_num": r[2], "terminal": r[3], "departure_time": r[4], "arrival_time": r[5], "memo": r[6]} for r in c.fetchall()]

    c.execute("SELECT days_applied, hotel_name, google_map_url, has_breakfast, budget FROM accommodation WHERE room_id=%s", (room_id,))
    accommodations = [{"days_applied": [int(x) for x in r[0].split(',') if x], "hotel_name": r[1], "google_map_url": r[2], "has_breakfast": r[3], "budget": r[4]} for r in c.fetchall()]

    c.execute("SELECT day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order, ai_options_json FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC", (room_id,))
    schedules = [{"day_num": r[0], "start_time": r[1], "end_time": r[2], "content": r[3], "author": r[4], "google_map_url": r[5], "tabelog_url": r[6], "budget": r[7], "place_id": r[8], "latitude": r[9], "longitude": r[10], "rating": r[11], "sort_order": r[12], "ai_options_json": r[13] or ""} for r in c.fetchall()]

    c.close()
    conn.close()

    data = {"flights": flights, "accommodations": accommodations, "schedules": schedules}
    json_str = json.dumps(data, ensure_ascii=False)
    b64_str = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')

    return {"export_code": b64_str}

@app.post("/room/{room_id}/import")
def import_room_data(room_id: str, req: ImportRequest, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    try:
        json_str = base64.b64decode(req.export_code).decode('utf-8')
        data = json.loads(json_str)
    except Exception:
        raise HTTPException(status_code=400, detail="유효하지 않은 내보내기 코드입니다.")

    conn = get_db_connection()
    c = conn.cursor()

    if req.clear_existing:
        c.execute("DELETE FROM flight WHERE room_id=%s", (room_id,))
        c.execute("DELETE FROM accommodation WHERE room_id=%s", (room_id,))
        c.execute("DELETE FROM comment WHERE schedule_id IN (SELECT id FROM schedule WHERE room_id=%s)", (room_id,))
        c.execute("DELETE FROM schedule WHERE room_id=%s", (room_id,))

    for f in data.get("flights", []):
        c.execute(
            """INSERT INTO flight (room_id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (room_id, f.get("flight_type"), f.get("airport"), f.get("flight_num"), f.get("terminal"), f.get("departure_time"), f.get("arrival_time"), f.get("memo"))
        )

    for a in data.get("accommodations", []):
        days_str = ",".join(map(str, a.get("days_applied", [])))
        c.execute(
            """INSERT INTO accommodation (room_id, days_applied, hotel_name, google_map_url, has_breakfast, budget)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (room_id, days_str, a.get("hotel_name"), a.get("google_map_url"), a.get("has_breakfast"), a.get("budget"))
        )

    for s in data.get("schedules", []):
        c.execute(
            """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order, ai_options_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (room_id, s.get("day_num"), s.get("start_time"), s.get("end_time"), s.get("content"), s.get("author", "방장"),
             s.get("google_map_url", ""), s.get("tabelog_url", ""), s.get("budget"), s.get("place_id"),
             s.get("latitude"), s.get("longitude"), s.get("rating"), s.get("sort_order", 0), s.get("ai_options_json", ""))
        )

    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# [패치 #1/#2/#3] LLM 기반 AI 자동 스케줄 (고도화)
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/ai_schedule", status_code=201)
def generate_ai_schedule(
    room_id: str,
    req: AiScheduleRequest,
    x_llm_api_key: Optional[str] = Header(None),
    x_google_api_key: Optional[str] = Header(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    role, nickname = get_current_user_info(room_id, credentials)
        if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    if not x_llm_api_key:
        raise HTTPException(status_code=400, detail="LLM API 키(Gemini 키)가 헤더로 전달되지 않았습니다.")

    # [패치 #1] 모델 선택
    model_id = GEMINI_MODEL_MAP.get(req.model or "gemini-2.5-flash", "gemini-2.5-flash")
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={x_llm_api_key}"

    # [패치 #3] 기존 일정 읽어서 프롬프트에 포함
    existing_schedule_text = ""
    if req.keep_existing:
        conn_read = get_db_connection()
        c_read = conn_read.cursor()
        c_read.execute(
            "SELECT day_num, start_time, end_time, content FROM schedule WHERE room_id=%s ORDER BY day_num, sort_order, start_time",
            (room_id,)
        )
        existing_rows = c_read.fetchall()
        c_read.close()
        conn_read.close()

        if existing_rows:
            lines = [f"  - {row[0]}일차 {row[1]}~{row[2]}: {row[3]}" for row in existing_rows]
            existing_schedule_text = (
                "\n\n[현재 등록된 기존 일정 - 절대 수정/삭제 불가]\n"
                + "\n".join(lines)
                + "\n\n위 기존 일정들의 시간대와 절대 겹치지 않도록, 빈 시간대만 새로운 일정으로 채워주세요."
            )

    target_days = sorted(list(set([d for d in (req.target_days or []) if isinstance(d, int) and 1 <= d <= max(1, req.days)])))
    if not target_days:
        target_days = list(range(1, req.days + 1))

    feedback_text = f"\n\n[사용자 추가 요청사항]: {req.feedback}" if req.feedback and req.feedback.strip() else ""
    target_days_text = ", ".join([f"{d}일차" for d in target_days])

    prompt = f"""당신은 10년차 전문 여행 플래너입니다. 사용자가 방문하는 목적지/도시는 '{req.city}'이며, 총 여행 기간은 {req.days}일입니다.
{existing_schedule_text}{feedback_text}
이번 생성 대상은 반드시 [{target_days_text}]만 해당합니다. 다른 일차는 생성하지 마세요.

매일 아침 09:00부터 저녁 시간대까지 식사, 유명 관광지, 적절한 휴식을 배분하여 현지 상황에 맞는 최적의 동선 일정표를 짜주세요.
각 일정의 'content' 필드에는 구글맵에서 검색 가능한 정확한 장소명(상호명)을 반드시 포함해야 합니다.
**가장 중요한 제약사항:** 반드시 마크다운(예: ```json 등) 없이 순수한 JSON 배열 포맷으로만 응답을 시작하고 끝내세요.

배열 내 각 JSON 객체는 아래 5개 필드만 포함:
- "day_num": 정수 (반드시 {target_days} 중 하나)
- "start_time": "HH:MM" 형태
- "end_time": "HH:MM" 형태
- "content": 구체적 장소명(검색 가능한 정확한 상호명 포함) + 짧은 설명
- "budget": 현지 화폐 단위 숫자 (모르면 0)"""

    try:
        res = requests.post(
            api_url,
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.5}},
            timeout=60
        )

        if not res.ok:
            raise Exception(f"Gemini API 호출 실패: {res.status_code} - {res.text[:200]}")

        resp_json = res.json()
        text_content = resp_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        text_content = re.sub(r"^```(json)?\n?|```$", "", text_content, flags=re.IGNORECASE).strip()
        ai_data = json.loads(text_content)

        conn = get_db_connection()
        c = conn.cursor()

        inserted_count = 0
        for item in ai_data:
            content_text = item.get("content", "자동 생성된 추천 일정")
            day = item.get("day_num", 1)
            if day not in target_days:
                continue

            # [패치 #2] content에서 장소명 추출하여 구글 Places API 호출
            place_info = None
            if x_google_api_key:
                place_info = get_google_place_info_by_name(content_text, x_google_api_key)

            map_url = place_info.get("map_url") if place_info and place_info.get("map_url") else build_google_search_url(content_text)
            lat     = place_info.get("lat") if place_info else None
            lng     = place_info.get("lng") if place_info else None
            rating  = place_info.get("rating") if place_info else None
            place_id = place_info.get("place_id") if place_info else None
            meal_options = []
            if is_meal_slot(content_text) and x_google_api_key:
                meal_options = fetch_meal_candidates(x_google_api_key, req.city, content_text, lat, lng, limit=3)

            c.execute(
                """INSERT INTO schedule
                       (room_id, day_num, start_time, end_time, content, author, google_map_url,
                        budget, place_id, latitude, longitude, rating, ai_options_json, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
                (room_id, day, item.get("start_time", "09:00"), item.get("end_time", "10:00"),
                 content_text, "🤖 AI", map_url, item.get("budget", 0),
                 place_id, lat, lng, rating, json.dumps(meal_options, ensure_ascii=False), room_id, day)
            )
            inserted_count += 1

        conn.commit()
        c.close()
        conn.close()

        return {"status": "ok", "generated_count": inserted_count}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI가 정해진 규칙(순수 JSON)대로 응답하지 못했습니다. 다시 시도해주세요.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 스케줄 생성 중 오류 발생: {str(e)}")
        @app.post("/room/{room_id}/ai_schedule_edit")
def edit_ai_schedule(
    room_id: str,
    req: AiScheduleEditRequest,
    x_llm_api_key: Optional[str] = Header(None),
    x_google_api_key: Optional[str] = Header(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    if not x_llm_api_key:
        raise HTTPException(status_code=400, detail="LLM API 키(Gemini 키)가 필요합니다.")
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="수정 요청 프롬프트를 입력해주세요.")

    conn = get_db_connection()
    c = conn.cursor()
    if req.target_days:
        c.execute(
            "SELECT id, day_num, start_time, end_time, content, budget FROM schedule WHERE room_id=%s AND author=%s AND day_num = ANY(%s) ORDER BY day_num, sort_order",
            (room_id, "🤖 AI", req.target_days)
        )
    else:
        c.execute(
            "SELECT id, day_num, start_time, end_time, content, budget FROM schedule WHERE room_id=%s AND author=%s ORDER BY day_num, sort_order",
            (room_id, "🤖 AI")
        )
    rows = c.fetchall()
    if not rows:
        c.close()
        conn.close()
        raise HTTPException(status_code=404, detail="수정 가능한 AI 일정이 없습니다.")

    current_items = [{"id": r[0], "day_num": r[1], "start_time": r[2], "end_time": r[3], "content": r[4], "budget": r[5] or 0} for r in rows]
    allowed_ids = {x["id"] for x in current_items}
    prompt = f"""아래 AI 일정만 수정해줘. id는 절대 바꾸지 마.
사용자 요청: {req.prompt}
출력은 마크다운 없이 JSON 배열로, 각 항목은 id,start_time,end_time,content,budget만 포함.
현재 일정:
{json.dumps(current_items, ensure_ascii=False)}"""
    model_id = GEMINI_MODEL_MAP.get(req.model or "gemini-2.5-flash", "gemini-2.5-flash")
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={x_llm_api_key}"

    try:
        res = requests.post(api_url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}}, timeout=50)
        if not res.ok:
            raise Exception(f"Gemini API 호출 실패: {res.status_code}")
        text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```(json)?\n?|```$", "", text, flags=re.IGNORECASE).strip()
        edited = json.loads(text)
        invalid_ids = []
        for item in edited:
            sid = item.get("id")
            if sid not in allowed_ids:
                invalid_ids.append(sid)
        if invalid_ids:
            raise HTTPException(status_code=400, detail=f"AI 수정 결과에 허용되지 않은 일정 ID가 포함되었습니다: {invalid_ids}")

        updated = 0
        for item in edited:
            sid = item.get("id")
            content_text = item.get("content", "")
            place_info = get_google_place_info_by_name(content_text, x_google_api_key) if (x_google_api_key and content_text) else None
            map_url = place_info["map_url"] if place_info and place_info.get("map_url") else build_google_search_url(content_text)
            day_num = next((x["day_num"] for x in current_items if x["id"] == sid), None)
            options = fetch_meal_candidates(x_google_api_key, "", content_text, place_info.get("lat") if place_info else None, place_info.get("lng") if place_info else None, 3) if (x_google_api_key and is_meal_slot(content_text)) else []
            c.execute(
                """UPDATE schedule
                   SET start_time=%s, end_time=%s, content=%s, budget=%s, google_map_url=%s, place_id=%s, latitude=%s, longitude=%s, rating=%s, ai_options_json=%s
                   WHERE id=%s AND room_id=%s AND author=%s""",
                (
                    item.get("start_time", "09:00"), item.get("end_time", "10:00"), content_text, item.get("budget", 0),
                    map_url, place_info.get("place_id") if place_info else None, place_info.get("lat") if place_info else None, place_info.get("lng") if place_info else None,
                    place_info.get("rating") if place_info else None, json.dumps(options, ensure_ascii=False), sid, room_id, "🤖 AI"
                )
            )
            updated += c.rowcount
        conn.commit()
        c.close()
        conn.close()
        return {"status": "ok", "updated_count": updated}
    except HTTPException:
        c.close()
        conn.close()
        raise
    except Exception as e:
        c.close()
        conn.close()
        raise HTTPException(status_code=500, detail=f"AI 수정 실패: {str(e)}")

# ─────────────────────────────────────────────
# 항공편 API
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/flight", status_code=201)
def add_flight(room_id: str, fl: FlightCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO flight (room_id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (room_id, fl.flight_type, fl.airport, fl.flight_num, fl.terminal, fl.departure_time, fl.arrival_time, fl.memo)
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/flight/{flight_id}")
def delete_flight(room_id: str, flight_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM flight WHERE id=%s AND room_id=%s", (flight_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 숙박 API
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/accommodation", status_code=201)
def add_accommodation(room_id: str, acc: AccommodationCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    days_str = ",".join(map(str, sorted(acc.days_applied)))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO accommodation (room_id, days_applied, hotel_name, google_map_url, has_breakfast, budget)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (room_id, days_str, acc.hotel_name, acc.google_map_url, acc.has_breakfast, acc.budget)
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/accommodation/{acc_id}")
def delete_accommodation(room_id: str, acc_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM accommodation WHERE id=%s AND room_id=%s", (acc_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 일정 추가 / 삭제 / 수정 API
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/schedule", status_code=201)
def add_schedule(room_id: str, sch: ScheduleCreate, x_google_api_key: Optional[str] = Header(None), credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")

    g = get_google_place_info(sch.google_map_url, x_google_api_key)
    final_content = f"{g.get('name')} ({sch.content})" if (g and g.get("name")) else sch.content

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, ai_options_json, sort_order)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
               (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
        (room_id, sch.day_num, sch.start_time, sch.end_time, final_content, nickname,
         sch.google_map_url, sch.tabelog_url, sch.budget,
         g.get('place_id') if g else None, g.get('lat') if g else None, g.get('lng') if g else None, g.get('rating') if g else None, "",
         room_id, sch.day_num)
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/schedule/{sch_id}")
def delete_schedule(room_id: str, sch_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM comment WHERE schedule_id=%s", (sch_id,))
    c.execute("DELETE FROM schedule WHERE id=%s AND room_id=%s", (sch_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# [패치 #8] 일정 수정 PATCH API
@app.patch("/room/{room_id}/schedule/{sch_id}")
def update_schedule(
    room_id: str,
    sch_id: int,
    req: ScheduleUpdate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="방장 또는 동행자 권한이 필요합니다.")

    fields = []
    values = []

    if req.day_num is not None:       fields.append("day_num=%s");      values.append(req.day_num)
    if req.start_time is not None:    fields.append("start_time=%s");   values.append(req.start_time)
    if req.end_time is not None:      fields.append("end_time=%s");     values.append(req.end_time)
    if req.content is not None:       fields.append("content=%s");      values.append(req.content)
    if req.google_map_url is not None: fields.append("google_map_url=%s"); values.append(req.google_map_url)
    if req.tabelog_url is not None:   fields.append("tabelog_url=%s");  values.append(req.tabelog_url)
    if req.budget is not None:        fields.append("budget=%s");       values.append(req.budget)

    if not fields:
        return {"status": "no_changes"}

    values.extend([sch_id, room_id])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"UPDATE schedule SET {', '.join(fields)} WHERE id=%s AND room_id=%s", values)
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/reorder")
def reorder_schedule(room_id: str, req: ReorderRequest, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403)

    conn = get_db_connection()
    c = conn.cursor()
    for idx, sch_id in enumerate(req.new_order):
        c.execute("UPDATE schedule SET sort_order=%s WHERE id=%s AND room_id=%s", (idx, sch_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 훈수(추천) 및 댓글 관리 API
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/suggestion", status_code=201)
def add_suggestion(room_id: str, sug: SuggestionCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url) VALUES (%s, %s, %s, %s, %s)",
        (room_id, f"훈수꾼 {sug.suggester_name[:4]}", sug.content, sug.google_map_url, sug.tabelog_url)
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sug_id}/vote")
def vote_suggestion(room_id: str, sug_id: int, type: str):
    column = "good_cnt" if type == "good" else "bad_cnt"
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"UPDATE suggestion SET {column} = {column} + 1 WHERE id=%s AND room_id=%s", (sug_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sug_id}/approve")
def approve_suggestion(room_id: str, sug_id: int, req: ApproveRequest, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT content, google_map_url, tabelog_url FROM suggestion WHERE id=%s AND room_id=%s", (sug_id, room_id))
    row = c.fetchone()
    if row:
        c.execute(
            """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
            (room_id, req.day_num, req.start_time, req.end_time, row[0], nickname, row[1], row[2], room_id, req.day_num)
        )
        c.execute("UPDATE suggestion SET status='승인됨' WHERE id=%s", (sug_id,))
        conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/suggestion/{sug_id}")
def delete_suggestion(room_id: str, sug_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM suggestion WHERE id=%s AND room_id=%s", (sug_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/schedule/{sch_id}/comment", status_code=201)
def add_comment(room_id: str, sch_id: int, cm: CommentCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT is_comment_enabled FROM room WHERE room_id=%s", (room_id,))
    row = c.fetchone()
    if not row or not row[0]:
        c.close()
        conn.close()
        raise HTTPException(status_code=403, detail="댓글 기능 비활성")

    writer_name = nickname if role in ("admin", "team") else f"훈수꾼 {cm.writer_name[:4]}"
    c.execute("INSERT INTO comment (schedule_id, writer_name, content) VALUES (%s, %s, %s)", (sch_id, writer_name, cm.content))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/schedule/{sch_id}/comment/{comment_id}")
def delete_comment(room_id: str, sch_id: int, comment_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM comment WHERE id=%s AND schedule_id=%s", (comment_id, sch_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# Google API (GPS 주변 검색 / 이동시간 / 장소명 텍스트 검색)
# ─────────────────────────────────────────────
@app.get("/api/nearby")
def get_nearby(lat: float, lng: float, type: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key:
        return {"results": [], "error": "Google API 키가 누락되었습니다."}

    if type not in ("restaurant", "smoking", "convenience"):
        return {"results": [], "error": "지원하지 않는 검색 타입입니다."}

    try:
        seen = {}
        candidates = []
        if type == "restaurant":
            st, rs = google_places_nearby(x_google_api_key, lat, lng, radius=1500, place_type="restaurant", language="ko")
            candidates.extend(rs[:20])
            if st != "OK" and not candidates:
                return {"results": [], "error": f"Google Places 호출 실패: {st}"}
        elif type == "convenience":
            _, rs = google_places_nearby(x_google_api_key, lat, lng, radius=1500, place_type="convenience_store", language="ko")
            candidates.extend(rs[:20])
            for kw in ["편의점", "convenience store", "7-Eleven", "FamilyMart", "CU", "GS25", "Lawson"]:
                _, kw_rs = google_places_text_search(x_google_api_key, kw, lat, lng, radius=1800, language="ko")
                candidates.extend(kw_rs[:7])
        else:
            smoking_queries = ["흡연구역", "흡연 부스", "smoking area", "smoking zone", "喫煙所"]
            for q in smoking_queries:
                _, rs = google_places_text_search(x_google_api_key, q, lat, lng, radius=2000, language="ko")
                candidates.extend(rs[:8])

        for p in candidates:
            pid = p.get("place_id")
            if not pid or pid in seen:
                continue
            loc = p.get("geometry", {}).get("location", {})
            d_m = calc_distance_m(lat, lng, loc.get("lat", lat), loc.get("lng", lng))
            score = 0.0
            rating = p.get("rating", 0) or 0
            user_ratings_total = p.get("user_ratings_total", 0) or 0
            is_open = p.get("opening_hours", {}).get("open_now")

            if type == "restaurant":
                score = (rating * 18) + min(user_ratings_total, 500) * 0.05 - d_m * 0.01 + (7 if is_open else 0)
                reason = f"평점 {rating}점, 리뷰 {user_ratings_total}개이며 현재 위치에서 비교적 가깝습니다."
            elif type == "convenience":
                score = 120 - d_m * 0.02 + (5 if is_open else 0) + (rating * 5)
                reason = "현재 위치에서 접근성이 좋아 보이고 간단한 물품 구매에 적합해 보입니다."
            else:
                score = 100 - d_m * 0.02 + (rating * 4)
                reason = "흡연 관련 키워드로 검색된 장소이며 현재 위치에서 비교적 이동이 쉬워 보입니다."

            map_url = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            seen[pid] = {
                "name": p.get("name", "이름 없음"),
                "rating": rating if rating else None,
                "address": p.get("vicinity") or p.get("formatted_address") or "주소 정보 없음",
                "map_url": map_url,
                "place_id": pid,
                "distance_m": d_m,
                "distance_text": f"약 {d_m}m",
                "reason": reason,
                "score": score
            }

        sorted_results = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:5]
        for r in sorted_results:
            r.pop("score", None)
        if not sorted_results:
            return {"results": [], "error": "반경 내 후보가 부족합니다. 반경을 넓히거나 키워드를 바꿔주세요."}
        return {"results": sorted_results}
    except Exception as e:
        return {"results": [], "error": f"주변 검색 처리 중 오류: {str(e)}"}

@app.post("/api/what_to_eat")
def what_to_eat(req: WhatToEatRequest, x_google_api_key: Optional[str] = Header(None), x_llm_api_key: Optional[str] = Header(None)):
    if not x_google_api_key:
        return {"results": [], "error": "Google API 키가 누락되었습니다.", "debug": {"used_llm": False, "candidate_count": 0}}

    try:
        used_llm = bool(x_llm_api_key and (req.user_text or "").strip())
        pref = parse_food_preferences_with_gemini(req.user_text or "", x_llm_api_key) if used_llm else {}
        menu_keywords = pref.get("menu_keywords") or []
        mood_keywords = pref.get("mood_keywords") or []
        exclude_keywords = pref.get("exclude_keywords") or []
        solo_ok = pref.get("solo_ok")

        candidates = []
        _, nearby_res = google_places_nearby(x_google_api_key, req.lat, req.lng, radius=1200, place_type="restaurant", language="ko")
        candidates.extend(nearby_res[:20])

        text_queries = []
        if not (req.user_text or "").strip():
            text_queries = ["맛집", "restaurant"]
        else:
            text_queries = [req.user_text] + [f"{k} 맛집" for k in menu_keywords[:3]] + [f"{k} 식당" for k in mood_keywords[:2]]
        for q in text_queries[:5]:
            _, t_res = google_places_text_search(x_google_api_key, q, req.lat, req.lng, radius=1800, language="ko")
            candidates.extend(t_res[:8])

        uniq = {}
        for p in candidates:
            pid = p.get("place_id")
            if not pid or pid in uniq:
                continue
            loc = p.get("geometry", {}).get("location", {})
            uniq[pid] = {
                "name": p.get("name", "이름 없음"),
                "rating": p.get("rating"),
                "user_ratings_total": p.get("user_ratings_total", 0) or 0,
                "address": p.get("vicinity") or p.get("formatted_address") or "주소 정보 없음",
                "place_id": pid,
                "lat": loc.get("lat"),
                "lng": loc.get("lng"),
                "types": p.get("types", []),
                "open_now": p.get("opening_hours", {}).get("open_now"),
            }

        if not uniq:
            return {"results": [], "error": "주변 식당 후보를 찾지 못했습니다.", "debug": {"used_llm": used_llm, "candidate_count": 0}}

        walk_info = google_distance_matrix_walking(
            x_google_api_key,
            req.lat,
            req.lng,
            [{"place_id": v["place_id"], "lat": v["lat"], "lng": v["lng"]} for v in uniq.values()]
        )

        scored = []
        user_text_l = (req.user_text or "").lower()
        for item in uniq.values():
            pid = item["place_id"]
            dm = walk_info.get(pid, {})
            distance_value = dm.get("distance_value") or calc_distance_m(req.lat, req.lng, item["lat"] or req.lat, item["lng"] or req.lng)
            walking_time_value = dm.get("walking_time_value")
            walking_time_text = dm.get("walking_time_text") or f"도보 약 {max(3, int(distance_value / 70))}분"
            distance_text = dm.get("distance_text") or f"약 {distance_value}m"

            rating = item.get("rating") or 0
            reviews = item.get("user_ratings_total", 0)
            text_score = 0
            hay = f"{item['name']} {item['address']}".lower()
            if user_text_l:
                for tk in (menu_keywords + mood_keywords + [user_text_l]):
                    if tk and tk.lower() in hay:
                        text_score += 7
            for ex in exclude_keywords:
                if ex and ex.lower() in hay:
                    text_score -= 12

            score = (rating * 16) + min(reviews, 600) * 0.04 - distance_value * 0.012 + text_score + (6 if item.get("open_now") else 0)
            if walking_time_value and walking_time_value <= 600:
                score += 8

            reasons = [f"평점 {rating}점(리뷰 {reviews}개)이며 {distance_text} 거리입니다."]
            if walking_time_text:
                reasons.append(f"{walking_time_text} 정도로 이동 가능합니다.")
            if menu_keywords:
                reasons.append(f"요청하신 조건(예: {', '.join(menu_keywords[:2])})과의 연관성이 추정됩니다.")
            reason = " ".join(reasons[:2])
            solo_hint = "혼밥에 비교적 무난해 보입니다." if solo_ok is True else ("혼밥 적합 여부는 현장 좌석 구성 확인을 권장합니다." if solo_ok is False else "분위기/혼밥 적합성은 리뷰 기준 추정입니다.")

            scored.append({
                "name": item["name"],
                "rating": item.get("rating"),
                "address": item["address"],
                "map_url": f"https://www.google.com/maps/place/?q=place_id:{pid}",
                "distance_text": distance_text,
                "walking_time_text": walking_time_text,
                "reason": reason,
                "solo_hint": solo_hint,
                "place_id": pid,
                "score": score
            })

        filtered = []
        for x in scored:
            d_text = x.get("distance_text", "")
            if "km" in d_text:
                filtered.append(x)
                continue
            m = re.search(r"(\d+)", d_text)
            if not m or int(m.group(1)) <= 1400:
                filtered.append(x)
        top = sorted(filtered, key=lambda x: x["score"], reverse=True)[:5]
        for t in top:
            t.pop("score", None)
        if not top:
            return {"results": [], "error": "반경 내 후보가 부족합니다.", "debug": {"used_llm": used_llm, "candidate_count": len(scored)}}
        return {"results": top, "debug": {"used_llm": used_llm, "candidate_count": len(scored)}}
    except Exception as e:
        return {"results": [], "error": f"추천 처리 실패: {str(e)}", "debug": {"used_llm": False, "candidate_count": 0}}

@app.post("/api/omni_assistant")
def omni_assistant(req: OmniAssistantRequest, x_google_api_key: Optional[str] = Header(None), x_llm_api_key: Optional[str] = Header(None)):
    if not x_google_api_key:
        return {"intent": "unknown", "answer": "Google API 키가 없어 처리할 수 없습니다.", "maps_url": "", "routes": [], "places": []}
    msg = (req.message or "").strip()
    if not msg:
        return {"intent": "unknown", "answer": "질문을 입력해주세요.", "maps_url": "", "routes": [], "places": []}

    try:
        origin_txt, dest_txt = parse_route_query(msg)
        route_signal = bool(origin_txt and dest_txt) or ("얼마나 걸" in msg) or ("가고 싶" in msg)
        if route_signal:
            using_here = any(k in msg for k in ["여기서", "지금 위치", "현재 위치"])
            origin_candidates = []
            if using_here and req.lat is not None and req.lng is not None:
                origin = {"name": "현재 위치", "place_id": "", "lat": req.lat, "lng": req.lng}
            else:
                oq = origin_txt or "현재 위치"
                _, origin_candidates = google_places_text_search(x_google_api_key, oq, req.lat or 0, req.lng or 0, 4000, "ko")
                origin = origin_candidates[0] if origin_candidates else None

            _, dest_candidates = google_places_text_search(x_google_api_key, dest_txt or msg, req.lat or 0, req.lng or 0, 5000, "ko")
            dest = dest_candidates[0] if dest_candidates else None

            if not origin or not dest:
                return {
                    "intent": "route",
                    "answer": "출발지/도착지를 충분히 해석하지 못했습니다. 장소명을 더 구체적으로 입력해주세요.",
                    "maps_url": "",
                    "routes": [],
                    "places": [{"name": p.get("name"), "address": p.get("formatted_address"), "map_url": f"https://www.google.com/maps/place/?q=place_id:{p.get('place_id')}"} for p in dest_candidates[:3]]
                }

            def matrix(mode: str):
                origin_str = f"{origin.get('lat')},{origin.get('lng')}" if origin.get("name") == "현재 위치" else f"place_id:{origin.get('place_id')}"
                res = requests.get(
                    "https://maps.googleapis.com/maps/api/distancematrix/json",
                    params={"origins": origin_str, "destinations": f"place_id:{dest.get('place_id')}", "mode": mode, "key": x_google_api_key, "language": "ko"},
                    timeout=8
                ).json()
                el = (res.get("rows") or [{}])[0].get("elements", [{}])[0]
                if el.get("status") != "OK":
                    return None
                return {"mode": mode, "duration": el.get("duration", {}).get("text"), "distance": el.get("distance", {}).get("text")}

            routes = [x for x in [matrix("driving"), matrix("transit"), matrix("walking")] if x]
            origin_name = origin.get("name") or "출발지"
            dest_name = dest.get("name") or "도착지"
            origin_q = f"{origin.get('lat')},{origin.get('lng')}" if origin.get("name") == "현재 위치" else f"place_id:{origin.get('place_id')}"
            maps_url = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origin_q)}&destination={requests.utils.quote('place_id:'+dest.get('place_id',''))}"
            return {
                "intent": "route",
                "answer": f"{origin_name} → {dest_name} 이동 정보를 찾았습니다.",
                "maps_url": maps_url,
                "routes": routes,
                "places": [
                    {"name": origin_name, "address": origin.get("formatted_address") or "현재 위치", "map_url": maps_url},
                    {"name": dest_name, "address": dest.get("formatted_address") or "", "map_url": f"https://www.google.com/maps/place/?q=place_id:{dest.get('place_id')}"}
                ]
            }

        keyword = "온천" if "온천" in msg else ("쇼핑" if "쇼핑" in msg else msg)
        _, recs = google_places_text_search(x_google_api_key, keyword, req.lat or 0, req.lng or 0, 5000, "ko")
        places = []
        for p in recs[:5]:
            pid = p.get("place_id")
            places.append({
                "name": p.get("name"),
                "rating": p.get("rating"),
                "address": p.get("formatted_address") or p.get("vicinity"),
                "map_url": f"https://www.google.com/maps/place/?q=place_id:{pid}",
                "reason": f"평점 {p.get('rating')}점 및 주변 접근성이 좋아 보여 추천합니다." if p.get("rating") else "질문 키워드와 연관성이 높아 보입니다."
            })
        if not places:
            return {"intent": "recommendation", "answer": "추천 가능한 장소를 찾지 못했습니다. 지역명이나 키워드를 더 구체적으로 입력해주세요.", "maps_url": "", "routes": [], "places": []}
        return {"intent": "recommendation", "answer": f"'{keyword}' 관련 추천 장소입니다.", "maps_url": places[0]["map_url"], "routes": [], "places": places}
    except Exception as e:
        return {"intent": "unknown", "answer": f"처리 중 오류가 발생했습니다: {str(e)}", "maps_url": "", "routes": [], "places": []}

@app.get("/api/travel_time")
def get_travel_time(origin: str, dest: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key:
        return {"duration": None}

    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins=place_id:{origin}&destinations=place_id:{dest}&mode=transit&key={x_google_api_key}&language=ko"
    res = requests.get(url).json()

    try:
        element = res['rows'][0]['elements'][0]
        return {"duration": element['duration']['text'], "distance": element['distance']['text']}
    except Exception:
        return {"duration": None}

# [패치 #5] 장소명 텍스트 검색 API (수동 일정 추가 시 자동완성용)
@app.get("/api/place_text_search")
def place_text_search(query: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key or not query:
        return {"results": []}
    try:
        res = requests.get(
            f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={requests.utils.quote(query)}&key={x_google_api_key}&language=ko",
            timeout=5
        ).json()

        results = []
        for place in res.get("results", [])[:5]:
            pid = place.get("place_id", "")
            results.append({
                "name": place.get("name", ""),
                "address": place.get("formatted_address", ""),
                "rating": place.get("rating"),
                "place_id": pid,
                "map_url": f"https://www.google.com/maps/place/?q=place_id:{pid}" if pid else ""
            })
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}

# ─────────────────────────────────────────────
# 정산 및 예산 로직
# ─────────────────────────────────────────────
@app.get("/room/{room_id}/budget_summary")
def budget_summary(room_id: str, exchange_rate: Optional[float] = None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT member_count, currency FROM room WHERE room_id=%s", (room_id,))
    room_row = c.fetchone()

    if not room_row:
        c.close()
        conn.close()
        return {}

    c.execute("SELECT COALESCE(SUM(budget), 0) FROM schedule WHERE room_id=%s", (room_id,))
    total_sch_budget = int(c.fetchone()[0])

    c.execute("SELECT COALESCE(SUM(budget), 0) FROM accommodation WHERE room_id=%s", (room_id,))
    total_acc_budget = int(c.fetchone()[0])

    total_local = total_sch_budget + total_acc_budget
    per_person_local = total_local // room_row[0] if room_row[0] else 0
    per_person_krw = round(per_person_local / exchange_rate * 1000) if exchange_rate and exchange_rate > 0 else None

    c.close()
    conn.close()

    return {
        "currency": room_row[1],
        "currency_symbol": CURRENCY_SYMBOL_MAP.get(room_row[1], ""),
        "member_count": room_row[0],
        "total_local": total_local,
        "per_person_local": per_person_local,
        "per_person_krw": per_person_krw,
        "exchange_rate_per_1000krw": exchange_rate
    }
