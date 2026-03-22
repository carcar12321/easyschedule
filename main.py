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
# 앱 초기화
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Schedule Pro API", version="4.0.2")

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
                f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={query}&key={api_key}&language=ko"
            ).json()
            
            if search_res.get("results"):
                place_id = search_res["results"][0]["place_id"]
                details_res = requests.get(
                    f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,rating,geometry,opening_hours&key={api_key}&language=ko"
                ).json().get("result", {})
                
                return {
                    "place_id": place_id,
                    "name": details_res.get("name"),
                    "rating": details_res.get("rating"),
                    "lat": details_res.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": details_res.get("geometry", {}).get("location", {}).get("lng"),
                    "open_now": details_res.get("opening_hours", {}).get("open_now")
                }
    except Exception as e:
        print(f"Google API Error: {e}")
    return None

# ─────────────────────────────────────────────
# 도시 ↔ 통화 매핑 및 DB 초기화
# ─────────────────────────────────────────────
CITY_CURRENCY_MAP = {"도쿄": "JPY", "오사카": "JPY", "나고야": "JPY", "후쿠오카": "JPY", "구마모토": "JPY", "가고시마": "JPY", "삿포로": "JPY", "오키나와": "JPY", "타이베이": "TWD", "가오슝": "TWD"}
CURRENCY_SYMBOL_MAP = {"JPY": "¥", "TWD": "NT$", "KRW": "₩", "USD": "$", "EUR": "€", "OTHER": ""}

def resolve_currency(city: str, currency_override: Optional[str] = None) -> str:
    if currency_override: return currency_override
    return CITY_CURRENCY_MAP.get(city, "OTHER")

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS room (room_id TEXT PRIMARY KEY, title TEXT NOT NULL, admin_pw TEXT NOT NULL, team_pw TEXT DEFAULT '', city TEXT DEFAULT '', currency TEXT DEFAULT 'JPY', member_count INTEGER DEFAULT 1, is_comment_enabled BOOLEAN DEFAULT FALSE, bookmark_name1 TEXT DEFAULT '', bookmark_link1 TEXT DEFAULT '', bookmark_name2 TEXT DEFAULT '', bookmark_link2 TEXT DEFAULT '', bookmark_name3 TEXT DEFAULT '', bookmark_link3 TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS schedule (id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, day_num INTEGER NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, content TEXT NOT NULL, author TEXT DEFAULT '방장', google_map_url TEXT DEFAULT '', tabelog_url TEXT DEFAULT '', budget INTEGER, sort_order INTEGER DEFAULT 0, place_id TEXT, latitude FLOAT, longitude FLOAT, rating FLOAT)""")
    c.execute("CREATE TABLE IF NOT EXISTS flight (id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, flight_type TEXT NOT NULL, airport TEXT NOT NULL, flight_num TEXT NOT NULL, terminal TEXT DEFAULT '', departure_time TEXT NOT NULL, arrival_time TEXT NOT NULL, memo TEXT DEFAULT '')")
    c.execute("CREATE TABLE IF NOT EXISTS accommodation (id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, days_applied TEXT NOT NULL, hotel_name TEXT NOT NULL, google_map_url TEXT DEFAULT '', has_breakfast BOOLEAN DEFAULT FALSE, budget INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS suggestion (id SERIAL PRIMARY KEY, room_id TEXT NOT NULL, suggester_name TEXT NOT NULL, content TEXT NOT NULL, google_map_url TEXT DEFAULT '', tabelog_url TEXT DEFAULT '', good_cnt INTEGER DEFAULT 0, bad_cnt INTEGER DEFAULT 0, status TEXT DEFAULT '대기중')")
    c.execute("CREATE TABLE IF NOT EXISTS comment (id SERIAL PRIMARY KEY, schedule_id INTEGER NOT NULL, writer_name TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())")

    migrations = [
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS city TEXT DEFAULT ''", "ALTER TABLE room ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'JPY'", "ALTER TABLE room ADD COLUMN IF NOT EXISTS member_count INTEGER DEFAULT 1", "ALTER TABLE room ADD COLUMN IF NOT EXISTS is_comment_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS place_id TEXT", "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS latitude FLOAT", "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS longitude FLOAT", "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS rating FLOAT",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS budget INTEGER", "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0", "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS author TEXT DEFAULT '방장'"
    ]
    for sql in migrations:
        try: c.execute(sql)
        except: conn.rollback()
    conn.commit()
    c.close(); conn.close()

@app.on_event("startup")
def startup_event(): init_db()

# ─────────────────────────────────────────────
# 인증 로직
# ─────────────────────────────────────────────
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str):
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except: return None

def get_current_user_info(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not credentials: return "guest", "훈수꾼"
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("room_id") != room_id: return "guest", "훈수꾼"
    return payload.get("role", "guest"), payload.get("nickname", "훈수꾼")

def hash_pw(pw: str): return pwd_context.hash(pw)
def verify_pw(plain: str, hashed: str): return pwd_context.verify(plain, hashed) if hashed else False

# ─────────────────────────────────────────────
# 모델 정의
# ─────────────────────────────────────────────
class RoomCreate(BaseModel):
    title: str; admin_pw: str; team_pw: Optional[str] = ""; city: Optional[str] = ""; currency: Optional[str] = ""; member_count: Optional[int] = 1; is_comment_enabled: Optional[bool] = False
class RoomUpdate(BaseModel):
    password: str; title: Optional[str] = None; team_pw: Optional[str] = None; city: Optional[str] = None; currency: Optional[str] = None; member_count: Optional[int] = None; is_comment_enabled: Optional[bool] = None; bookmark_name1: Optional[str] = None; bookmark_link1: Optional[str] = None; bookmark_name2: Optional[str] = None; bookmark_link2: Optional[str] = None; bookmark_name3: Optional[str] = None; bookmark_link3: Optional[str] = None
class LoginRequest(BaseModel): room_id: str; password: str; nickname: Optional[str] = ""
class ScheduleCreate(BaseModel): day_num: int; start_time: str; end_time: str; content: str; google_map_url: Optional[str] = ""; tabelog_url: Optional[str] = ""; budget: Optional[int] = None
class FlightCreate(BaseModel): flight_type: str; airport: str; flight_num: str; terminal: Optional[str] = ""; departure_time: str; arrival_time: str; memo: Optional[str] = ""
class AccommodationCreate(BaseModel): days_applied: List[int]; hotel_name: str; google_map_url: Optional[str] = ""; has_breakfast: bool = False; budget: Optional[int] = None
class ReorderRequest(BaseModel): new_order: List[int]
class SuggestionCreate(BaseModel): suggester_name: str; content: str; google_map_url: Optional[str] = ""; tabelog_url: Optional[str] = ""
class ApproveRequest(BaseModel): day_num: int; start_time: str; end_time: str
class CommentCreate(BaseModel): writer_name: str; content: str

# ─────────────────────────────────────────────
# 기본 API (로그인, 방 생성, 로드)
# ─────────────────────────────────────────────
@app.get("/")
@app.get("/{room_id}")
def serve_frontend(room_id: str = None): return FileResponse("index.html")

@app.post("/auth/login")
def login(req: LoginRequest):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (req.room_id,))
    row = c.fetchone(); c.close(); conn.close()
    if not row: raise HTTPException(status_code=404, detail="방 없음")
    if verify_pw(req.password, row[0]): role, nick = "admin", "방장"
    elif row[1] and verify_pw(req.password, row[1]):
        if not req.nickname.strip(): raise HTTPException(status_code=400)
        role, nick = "team", f"동행자 {req.nickname.strip()[:8]}"
    else: raise HTTPException(status_code=401)
    token = create_access_token({"room_id": req.room_id, "role": role, "nickname": nick})
    return {"access_token": token, "role": role, "nickname": nick}

@app.post("/create_room", status_code=201)
def create_room(room: RoomCreate):
    rid = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    curr = resolve_currency(room.city or "", room.currency or "")
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO room (room_id, title, admin_pw, team_pw, city, currency, member_count, is_comment_enabled) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (rid, room.title, hash_pw(room.admin_pw), hash_pw(room.team_pw) if room.team_pw else "", room.city or "", curr, room.member_count or 1, room.is_comment_enabled))
    conn.commit(); c.close(); conn.close()
    return {"room_id": rid}

@app.get("/room/{room_id}/data")
def get_room_data(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT title, city, currency, member_count, is_comment_enabled, bookmark_name1, bookmark_link1, bookmark_name2, bookmark_link2, bookmark_name3, bookmark_link3 FROM room WHERE room_id=%s", (room_id,))
    room_row = c.fetchone()
    if not room_row: c.close(); conn.close(); raise HTTPException(status_code=404)

    c.execute("SELECT id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo FROM flight WHERE room_id=%s ORDER BY id ASC", (room_id,))
    flights = [{"id": r[0], "flight_type": r[1], "airport": r[2], "flight_num": r[3], "terminal": r[4], "departure_time": r[5], "arrival_time": r[6], "memo": r[7]} for r in c.fetchall()]
    c.execute("SELECT id, days_applied, hotel_name, google_map_url, has_breakfast, budget FROM accommodation WHERE room_id=%s ORDER BY id ASC", (room_id,))
    accommodations = [{"id": r[0], "days_applied": [int(x) for x in r[1].split(',') if x], "hotel_name": r[2], "google_map_url": r[3], "has_breakfast": r[4], "budget": r[5]} for r in c.fetchall()]
    c.execute("SELECT id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, rating FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC, start_time ASC", (room_id,))
    schedules = [{"id": r[0], "day_num": r[1], "start_time": r[2], "end_time": r[3], "content": r[4], "author": r[5], "google_map_url": r[6] or "", "tabelog_url": r[7] or "", "budget": r[8], "place_id": r[9], "rating": r[10]} for r in c.fetchall()]
    for s in schedules:
        c.execute("SELECT id, writer_name, content, to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') FROM comment WHERE schedule_id=%s ORDER BY created_at ASC", (s["id"],))
        s["comments"] = [{"id": row[0], "writer_name": row[1], "content": row[2], "created_at": row[3]} for row in c.fetchall()]
    c.execute("SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status FROM suggestion WHERE room_id=%s ORDER BY id DESC", (room_id,))
    suggestions = [{"id": r[0], "suggester_name": r[1], "content": r[2], "google_map_url": r[3] or "", "tabelog_url": r[4] or "", "good_cnt": r[5], "bad_cnt": r[6], "status": r[7]} for r in c.fetchall()]
    c.close(); conn.close()
    return {"room_id": room_id, "title": room_row[0], "city": room_row[1], "currency": room_row[2], "currency_symbol": CURRENCY_SYMBOL_MAP.get(room_row[2], ""), "member_count": room_row[3], "is_comment_enabled": room_row[4], "role": role, "nickname": nickname, "bookmarks": [{"name": room_row[5], "url": room_row[6]}, {"name": room_row[7], "url": room_row[8]}, {"name": room_row[9], "url": room_row[10]}], "flights": flights, "accommodations": accommodations, "schedules": schedules, "suggestions": suggestions}

# ─────────────────────────────────────────────
# 설정 관리
# ─────────────────────────────────────────────
@app.patch("/room/{room_id}/settings")
def update_room_settings(room_id: str, req: RoomUpdate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    row = c.fetchone()
    if not row or not verify_pw(req.password, row[0]):
        c.close(); conn.close(); raise HTTPException(status_code=401)
    fields, values = [], []
    if req.title is not None: fields.append("title=%s"); values.append(req.title)
    if req.team_pw is not None: fields.append("team_pw=%s"); values.append(hash_pw(req.team_pw) if req.team_pw else "")
    if req.member_count is not None: fields.append("member_count=%s"); values.append(req.member_count)
    if req.is_comment_enabled is not None: fields.append("is_comment_enabled=%s"); values.append(req.is_comment_enabled)
    for i in range(1, 4):
        name = getattr(req, f"bookmark_name{i}"); link = getattr(req, f"bookmark_link{i}")
        if name is not None: fields.append(f"bookmark_name{i}=%s"); values.append(name)
        if link is not None: fields.append(f"bookmark_link{i}=%s"); values.append(link)
    if fields:
        values.append(room_id); c.execute(f"UPDATE room SET {', '.join(fields)} WHERE room_id=%s", values); conn.commit()
    c.close(); conn.close(); return {"status": "ok"}

# ─────────────────────────────────────────────
# 일정/항공/숙박/훈수 API (핵심 수정 포함)
# ─────────────────────────────────────────────
@app.post("/room/{room_id}/schedule", status_code=201)
def add_schedule(room_id: str, sch: ScheduleCreate, x_google_api_key: Optional[str] = Header(None), credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403)
    g = get_google_place_info(sch.google_map_url, x_google_api_key)
    final_content = f"{g['name']} ({sch.content})" if (g and g.get("name")) else sch.content
    conn = get_db_connection(); c = conn.cursor()
    c.execute("""INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, budget, place_id, latitude, longitude, rating, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""", (room_id, sch.day_num, sch.start_time, sch.end_time, final_content, nickname, sch.google_map_url, sch.tabelog_url, sch.budget, g['place_id'] if g else None, g['lat'] if g else None, g['lng'] if g else None, g['rating'] if g else None, room_id, sch.day_num))
    conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.delete("/room/{room_id}/schedule/{sch_id}")
def delete_schedule(room_id: str, sch_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM comment WHERE schedule_id=%s", (sch_id,)); c.execute("DELETE FROM schedule WHERE id=%s AND room_id=%s", (sch_id, room_id)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/reorder")
def reorder_schedule(room_id: str, req: ReorderRequest, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor()
    for idx, sid in enumerate(req.new_order): c.execute("UPDATE schedule SET sort_order=%s WHERE id=%s AND room_id=%s", (idx, sid, room_id))
    conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/flight", status_code=201)
def add_flight(room_id: str, fl: FlightCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("INSERT INTO flight (room_id, flight_type, airport, flight_num, terminal, departure_time, arrival_time, memo) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (room_id, fl.flight_type, fl.airport, fl.flight_num, fl.terminal, fl.departure_time, fl.arrival_time, fl.memo)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.delete("/room/{room_id}/flight/{fid}")
def delete_flight(room_id: str, fid: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM flight WHERE id=%s AND room_id=%s", (fid, room_id)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/accommodation", status_code=201)
def add_acc(room_id: str, acc: AccommodationCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("INSERT INTO accommodation (room_id, days_applied, hotel_name, google_map_url, has_breakfast, budget) VALUES (%s,%s,%s,%s,%s,%s)", (room_id, ",".join(map(str, sorted(acc.days_applied))), acc.hotel_name, acc.google_map_url, acc.has_breakfast, acc.budget)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.delete("/room/{room_id}/accommodation/{aid}")
def delete_acc(room_id: str, aid: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM accommodation WHERE id=%s AND room_id=%s", (aid, room_id)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/suggestion", status_code=201)
def add_sug(room_id: str, sug: SuggestionCreate):
    conn = get_db_connection(); c = conn.cursor(); c.execute("INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url) VALUES (%s,%s,%s,%s,%s)", (room_id, f"훈수꾼 {sug.suggester_name[:4]}", sug.content, sug.google_map_url, sug.tabelog_url)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sid}/vote")
def vote_sug(room_id: str, sid: int, type: str):
    col = "good_cnt" if type == "good" else "bad_cnt"
    conn = get_db_connection(); c = conn.cursor(); c.execute(f"UPDATE suggestion SET {col} = {col} + 1 WHERE id=%s AND room_id=%s", (sid, room_id)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sid}/approve")
def approve_sug(room_id: str, sid: int, req: ApproveRequest, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT content, google_map_url, tabelog_url FROM suggestion WHERE id=%s AND room_id=%s", (sid, room_id))
    row = c.fetchone()
    if row:
        c.execute("INSERT INTO schedule (room_id, day_num, start_time, end_time, content, author, google_map_url, tabelog_url, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))", (room_id, req.day_num, req.start_time, req.end_time, row[0], nickname, row[1], row[2], room_id, req.day_num))
        c.execute("UPDATE suggestion SET status='승인됨' WHERE id=%s", (sid,))
        conn.commit()
    c.close(); conn.close(); return {"status": "ok"}

@app.delete("/room/{room_id}/suggestion/{sid}")
def del_sug(room_id: str, sid: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM suggestion WHERE id=%s AND room_id=%s", (sid, room_id)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.post("/room/{room_id}/schedule/{sid}/comment")
def add_cm(room_id: str, sid: int, cm: CommentCreate, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, nickname = get_current_user_info(room_id, credentials); conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT is_comment_enabled FROM room WHERE room_id=%s", (room_id,))
    row = c.fetchone(); if not row or not row[0]: c.close(); conn.close(); raise HTTPException(status_code=403)
    writer = nickname if role in ("admin", "team") else f"훈수꾼 {cm.writer_name[:4]}"
    c.execute("INSERT INTO comment (schedule_id, writer_name, content) VALUES (%s,%s,%s)", (sid, writer, cm.content)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

@app.delete("/room/{room_id}/schedule/{sid}/comment/{cid}")
def del_cm(room_id: str, sid: int, cid: int, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role, _ = get_current_user_info(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403)
    conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM comment WHERE id=%s AND schedule_id=%s", (cid, sid)); conn.commit(); c.close(); conn.close(); return {"status": "ok"}

# ─────────────────────────────────────────────
# Google API 및 정산
# ─────────────────────────────────────────────
@app.get("/api/nearby")
def get_nearby(lat: float, lng: float, type: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key: return {"results": []}
    kw = {"restaurant": "restaurant", "smoking": "smoking area|smoking allowed cafe", "convenience": "convenience_store"}
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius=1000&keyword={kw.get(type)}&key={x_google_api_key}&language=ko"
    res = requests.get(url).json().get("results", [])
    if type == "restaurant":
        res = [r for r in res if r.get("rating", 0) >= 4.0][:3]
        for r in res: r["ai_desc"] = f"현지 평점 {r.get('rating')}점 맛집입니다. 추천!"
    else: res = res[:3]
    return {"results": res}

@app.get("/api/travel_time")
def get_travel_time(origin: str, dest: str, x_google_api_key: Optional[str] = Header(None)):
    if not x_google_api_key: return {"duration": None}
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins=place_id:{origin}&destinations=place_id:{dest}&mode=transit&key={x_google_api_key}&language=ko"
    res = requests.get(url).json()
    try:
        element = res['rows'][0]['elements'][0]
        return {"duration": element['duration']['text'], "distance": element['distance']['text']}
    except: return {"duration": None}

@app.get("/room/{room_id}/budget_summary")
def budget_summary(room_id: str, exchange_rate: Optional[float] = None):
    conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT member_count, currency FROM room WHERE room_id=%s", (room_id,))
    room_row = c.fetchone(); if not room_row: c.close(); conn.close(); return {}
    c.execute("SELECT COALESCE(SUM(budget), 0) FROM schedule WHERE room_id=%s", (room_id,))
    s_b = int(c.fetchone()[0]); c.execute("SELECT COALESCE(SUM(budget), 0) FROM accommodation WHERE room_id=%s", (room_id,))
    a_b = int(c.fetchone()[0]); total = s_b + a_b; per = total // room_row[0] if room_row[0] else 0
    krw = round(per / exchange_rate * 1000) if (exchange_rate and exchange_rate > 0) else None
    c.close(); conn.close()
    return {"currency": room_row[1], "currency_symbol": CURRENCY_SYMBOL_MAP.get(room_row[1], ""), "member_count": room_row[0], "total_local": total, "per_person_local": per, "per_person_krw": krw}
