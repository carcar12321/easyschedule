import os
import re
import json
import base64
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
# 앱 초기화 (이름 변경: Easy Schedule Pro)
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Schedule Pro API", version="4.5.0")

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

# 72바이트 제한 없는 pbkdf2_sha256 적용
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

# ─────────────────────────────────────────────
# DB 설정
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ─────────────────────────────────────────────
# Google Maps API 유틸리티
# ─────────────────────────────────────────────
def get_google_place_info(url: str, api_key: str):
    """구글맵 URL에서 장소 정보를 가져오는 공식 API 로직"""
    if not api_key or not url:
        return None
    try:
        # 1. 단축 URL 해제
        if "goo.gl" in url or "maps.app.goo.gl" in url:
            res = requests.head(url, allow_redirects=True, timeout=5)
            url = res.url
        
        # 2. URL에서 장소명 추출 (/place/장소명/...)
        name_match = re.search(r'/place/([^/]+)', url)
        if name_match:
            query = requests.utils.unquote(name_match.group(1).replace('+', ' '))
            
            # 3. Text Search API로 Place ID 확보
            search_res = requests.get(
                f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={api_key}&language=ko"
            ).json()
            
            if search_res.get("results"):
                place_id = search_res["results"][0]["place_id"]
                
                # 4. Details API로 상세 정보 확보
                details_res = requests.get(
                    f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,geometry,opening_hours&key={api_key}&language=ko"
                ).json().get("result", {})
                
                return {
                    "place_id": place_id,
                    "name": details_res.get("name"),
                    "rating": details_res.get("rating"),
                    "lat": details_res.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": details_res.get("geometry", {}).get("location", {}).get("lng")
                }
    except Exception as e:
        print(f"Google API Error: {e}")
    return None

# ─────────────────────────────────────────────
# 도시 ↔ 통화 매핑
# ─────────────────────────────────────────────
CITY_CURRENCY_MAP = {
    "도쿄": "JPY", "오사카": "JPY", "나고야": "JPY", "후쿠오카": "JPY",
    "구마모토": "JPY", "가고시마": "JPY", "삿포로": "JPY", "오키나와": "JPY",
    "타이베이": "TWD", "가오슝": "TWD",
}

CURRENCY_SYMBOL_MAP = {
    "JPY": "¥",
    "TWD": "NT$",
    "KRW": "₩",
    "USD": "$",
    "EUR": "€",
    "OTHER": "",
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

    # 1. 방 테이블
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
            bookmark_link3   TEXT DEFAULT ''
        )
    """)

    # 2. 일정 테이블
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
            rating          FLOAT
        )
    """)

    # 3. 항공편 테이블
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

    # 4. 숙박 테이블
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

    # 5. 훈수(추천) 테이블
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

    # 6. 댓글 테이블
    c.execute("""
        CREATE TABLE IF NOT EXISTS comment (
            id          SERIAL PRIMARY KEY,
            schedule_id INTEGER NOT NULL,
            writer_name TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # 기존 DB 안전 업데이트 (마이그레이션)
    migrations =[
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

# 비밀번호 검증
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
# Pydantic 데이터 모델 (AI & Import 모델 추가)
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

# ---- 신규: AI 스케줄러 DTO ----
class AiScheduleRequest(BaseModel):
    city: str
    days: int

# ---- 신규: 내보내기/가져오기 DTO ----
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
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (req.room_id,))
    row = c.fetchone()
    c.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    admin_pw_hash, team_pw_hash = row
    
    if verify_pw(req.password, admin_pw_hash):
        role = "admin"
        nickname = "방장"
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
                   (room_id, title, admin_pw, team_pw, city, currency, member_count, is_comment_enabled)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (rid, room.title, hash_pw(room.admin_pw), hash_pw(room.team_pw) if room.team_pw else "",
             room.city or "", currency, room.member_count or 1, room.is_comment_enabled)
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

    bookmarks =[]
    if b_n1 and b_l1: bookmarks.append({"name": b_n1, "url": b_l1})
    if b_n2 and b_l2: bookmarks.append({"name": b_n2, "url": b_l2})
    if b_n3 and b_l3: bookmarks.append({"name": b_n3, "url": b_l3})

    # 항공권 조회
    c.execute("SELECT id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo FROM flight WHERE room_id=%s ORDER BY id ASC", (room_id,))
    flights = [{"id": r[0], "flight_type": r[1], "airport": r[2], "flight_num": r[3], "terminal": r[4], "departure_time": r[5], "arrival_time": r[6], "memo": r[7]} for r in c.fetchall()]

    # 숙박 조회
    c.execute("SELECT id, days_applied, hotel_name, google_map_url, has_breakfast, budget FROM accommodation WHERE room_id=%s ORDER BY id ASC", (room_id,))
    accommodations = [{"id": r[0], "days_applied": [int(x) for x in r[1].split(',') if x], "hotel_name": r[2], "google_map_url": r[3], "has_breakfast": r[4], "budget": r[5]} for r in c.fetchall()]

    # 일정 및 댓글 조회 (구글 정보 포함)
    c.execute(
        """SELECT id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, rating
           FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC, start_time ASC""",
        (room_id,)
    )
    schedules =[
        {"id": r[0], "day_num": r[1], "start_time": r[2], "end_time": r[3], "content": r[4], 
         "author": r[5], "google_map_url": r[6] or "", "tabelog_url": r[7] or "", "budget": r[8],
         "place_id": r[9], "rating": r[10]}
        for r in c.fetchall()
    ]

    schedule_ids = [s["id"] for s in schedules]
    comments_map: dict = {s["id"]:[] for s in schedules}
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

    # 훈수(추천) 조회
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
# 방 설정 및 북마크 수정 (방장)
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
    
    fields =[]
    values =[]
    
    if req.title is not None:
        fields.append("title=%s")
        values.append(req.title)
    if req.team_pw is not None:
        fields.append("team_pw=%s")
        values.append(hash_pw(req.team_pw) if req.team_pw else "")
    if req.member_count is not None:
        fields.append("member_count=%s")
        values.append(req.member_count)
    if req.is_comment_enabled is not None:
        fields.append("is_comment_enabled=%s")
        values.append(req.is_comment_enabled)
    
    # 북마크 업데이트
    if req.bookmark_name1 is not None:
        fields.append("bookmark_name1=%s")
        values.append(req.bookmark_name1)
    if req.bookmark_link1 is not None:
        fields.append("bookmark_link1=%s")
        values.append(req.bookmark_link1)
    if req.bookmark_name2 is not None:
        fields.append("bookmark_name2=%s")
        values.append(req.bookmark_name2)
    if req.bookmark_link2 is not None:
        fields.append("bookmark_link2=%s")
        values.append(req.bookmark_link2)
    if req.bookmark_name3 is not None:
        fields.append("bookmark_name3=%s")
        values.append(req.bookmark_name3)
    if req.bookmark_link3 is not None:
        fields.append("bookmark_link3=%s")
        values.append(req.bookmark_link3)

    if fields:
        values.append(room_id)
        c.execute(f"UPDATE room SET {', '.join(fields)} WHERE room_id=%s", values)
        conn.commit()
        
    c.close()
    conn.close()
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 신규 요구사항 4 & 6: 여행 코스 데이터 Export/Import
# ─────────────────────────────────────────────
@app.get("/room/{room_id}/export")
def export_room_data(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # 해당 방의 데이터 수집 (항공/숙박/일정)
    c.execute("SELECT flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo FROM flight WHERE room_id=%s", (room_id,))
    flights = [{"flight_type": r[0], "airport": r[1], "flight_num": r[2], "terminal": r[3], "departure_time": r[4], "arrival_time": r[5], "memo": r[6]} for r in c.fetchall()]
    
    c.execute("SELECT days_applied, hotel_name, google_map_url, has_breakfast, budget FROM accommodation WHERE room_id=%s", (room_id,))
    accommodations = [{"days_applied": [int(x) for x in r[0].split(',') if x], "hotel_name": r[1], "google_map_url": r[2], "has_breakfast": r[3], "budget": r[4]} for r in c.fetchall()]
    
    c.execute("SELECT day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC", (room_id,))
    schedules = [{"day_num": r[0], "start_time": r[1], "end_time": r[2], "content": r[3], "author": r[4], "google_map_url": r[5], "tabelog_url": r[6], "budget": r[7], "place_id": r[8], "latitude": r[9], "longitude": r[10], "rating": r[11], "sort_order": r[12]} for r in c.fetchall()]
    
    c.close()
    conn.close()
    
    # 딕셔너리를 JSON 문자열 변환 -> utf-8 인코딩 -> Base64로 직렬화하여 숨김(암호화 아님, 직렬화)
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
        
    for f in data.get("flights",[]):
        c.execute(
            """INSERT INTO flight (room_id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (room_id, f.get("flight_type"), f.get("airport"), f.get("flight_num"), f.get("terminal"), f.get("departure_time"), f.get("arrival_time"), f.get("memo"))
        )
    
    for a in data.get("accommodations",[]):
        days_str = ",".join(map(str, a.get("days_applied",[])))
        c.execute(
            """INSERT INTO accommodation (room_id, days_applied, hotel_name, google_map_url, has_breakfast, budget)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (room_id, days_str, a.get("hotel_name"), a.get("google_map_url"), a.get("has_breakfast"), a.get("budget"))
        )
    
    for s in data.get("schedules",[]):
        c.execute(
            """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (room_id, s.get("day_num"), s.get("start_time"), s.get("end_time"), s.get("content"), s.get("author", "방장"), 
             s.get("google_map_url", ""), s.get("tabelog_url", ""), s.get("budget"), s.get("place_id"), s.get("latitude"), 
             s.get("longitude"), s.get("rating"), s.get("sort_order", 0))
        )
                  
    conn.commit()
    c.close()
    conn.close()
    
    return {"status": "ok"}

# ─────────────────────────────────────────────
# 신규 요구사항 5: LLM 기반 AI 자동 스케줄 
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/ai_schedule", status_code=201)
def generate_ai_schedule(room_id: str, req: AiScheduleRequest, x_llm_api_key: Optional[str] = Header(None), credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
        
    if not x_llm_api_key:
        raise HTTPException(status_code=400, detail="LLM API 키(Gemini 키)가 헤더로 전달되지 않았습니다.")
        
    # Gemini API 직접 호출 로직
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={x_llm_api_key}"
    prompt = f"""당신은 10년차 전문 여행 플래너입니다. 사용자가 방문하는 목적지 또는 도시는 '{req.city}' 이며, 총 여행 기간은 '{req.days}' 일입니다.
매일 아침 09:00부터 저녁 시간대까지 식사, 유명 관광지, 적절한 휴식을 배분하여 현지 상황에 맞는 최적의 동선 일정표를 짜주세요.
**가장 중요한 제약사항:** 반드시 마크다운(예: ```json 등) 텍스트 포맷이나 당신의 친절한 부연설명 없이 순수한 JSON 배열 포맷으로만 응답을 시작하고 끝내세요. 그래야 코드에서 JSON 파싱 에러가 안납니다.

배열 내 각 JSON 객체는 아래 5개 필드만 포함해야 합니다:
- "day_num": 정수값 (1 부터 {req.days} 까지의 일차 번호)
- "start_time": 문자열 ("HH:MM" 형태의 시작시간, 예: "10:30")
- "end_time": 문자열 ("HH:MM" 형태의 종료시간)
- "content": 문자열 (구체적인 추천 식당, 추천 메뉴 혹은 관광지 명과 그곳에서 할 일 짧게 설명)
- "budget": 현지 화폐 단위의 숫자형 데이터 (식비, 교통비, 입장료 등). 공짜거나 알 수 없다면 0을 넣어주세요."""
    
    try:
        # LLM 호출
        res = requests.post(url, json={"contents": [{"parts":[{"text": prompt}]}], "generationConfig": {"temperature": 0.5}}, timeout=30)
        
        if not res.ok:
            raise Exception("API 호출이 실패했거나 API Key가 유효하지 않습니다.")
            
        resp_json = res.json()
        text_content = resp_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # 혹시나 포함될 수 있는 마크다운 코드 블록( ```json ) 정규식으로 안전하게 제거
        text_content = re.sub(r"^```(json)?\n?|```$", "", text_content, flags=re.IGNORECASE).strip()
        
        # 응답 문자열을 실제 리스트 구조로 파싱
        ai_data = json.loads(text_content)
        
        conn = get_db_connection()
        c = conn.cursor()
        
        inserted_count = 0
        for item in ai_data:
            c.execute(
                """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, sort_order, budget) 
                   VALUES (%s, %s, %s, %s, %s, %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s), %s)""",
                (room_id, item.get("day_num", 1), item.get("start_time", "09:00"), item.get("end_time", "10:00"), 
                 item.get("content", "자동 생성된 추천 일정"), "🤖 AI 로봇", room_id, item.get("day_num", 1), item.get("budget", 0))
            )
            inserted_count += 1
            
        conn.commit()
        c.close()
        conn.close()
        
        return {"status": "ok", "generated_count": inserted_count}
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI가 정해진 규칙(순수 JSON)대로 응답하지 못했습니다. 버튼을 다시 한 번 눌러 재시도해주세요.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 스케줄 생성 중 시스템 오류 발생: {str(e)}")


# ─────────────────────────────────────────────
# 항공편(Flight) 및 숙박(Accommodation) API (기존 로직 보존)
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
# 일정 추가 (구글맵 연동 - 기존 로직 보존)
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/schedule", status_code=201)
def add_schedule(room_id: str, sch: ScheduleCreate, x_google_api_key: Optional[str] = Header(None), credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    
    # 구글 API 호출 (헤더의 키 사용)
    g = get_google_place_info(sch.google_map_url, x_google_api_key)
    
    # 구글 상호명이 있으면 "상호명 (원래내용)" 으로 저장
    final_content = f"{g['name']} ({sch.content})" if (g and g.get("name")) else sch.content

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
        (room_id, sch.day_num, sch.start_time, sch.end_time, final_content, nickname, sch.google_map_url, sch.tabelog_url, sch.budget,
         g['place_id'] if g else None, g['lat'] if g else None, g['lng'] if g else None, g['rating'] if g else None, room_id, sch.day_num)
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
# 훈수, 댓글 관리 API (기존 로직 보존)
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
# Google API (GPS 주변 검색 및 이동 시간 계산 - 기존 로직 보존)
# ─────────────────────────────────────────────
@app.get("/api/nearby")
def get_nearby(lat: float, lng: float, type: str, x_google_api_key: Optional[str] = Header(None)):
    """GPS 좌표 기반 주변 검색 (식당, 흡연실, 편의점)"""
    if not x_google_api_key:
        return {"results":[]}
    
    keywords = {
        "restaurant": "restaurant",
        "smoking": "smoking area|smoking allowed cafe",
        "convenience": "convenience_store"
    }
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius=1000&keyword={keywords.get(type)}&key={x_google_api_key}&language=ko"
    
    res = requests.get(url).json().get("results",[])
    
    if type == "restaurant":
        res =[r for r in res if r.get("rating", 0) >= 4.0][:3]
        for r in res:
            r["ai_desc"] = f"현지 평점 {r.get('rating')}점 맛집입니다. 실패 없는 선택!"
    else:
        res = res[:3]
        
    return {"results": res}

@app.get("/api/travel_time")
def get_travel_time(origin: str, dest: str, x_google_api_key: Optional[str] = Header(None)):
    """두 장소 사이의 대중교통 이동 시간 계산"""
    if not x_google_api_key:
        return {"duration": None}
        
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins=place_id:{origin}&destinations=place_id:{dest}&mode=transit&key={x_google_api_key}&language=ko"
    res = requests.get(url).json()
    
    try:
        element = res['rows'][0]['elements'][0]
        return {
            "duration": element['duration']['text'],
            "distance": element['distance']['text']
        }
    except Exception:
        return {"duration": None}

# ─────────────────────────────────────────────
# 정산 및 예산 로직 (기존 로직 보존)
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
