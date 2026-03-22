import os
import re
import random
import string
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import psycopg2
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel, validator

# ─────────────────────────────────────────────
# 앱 초기화
# ─────────────────────────────────────────────
app = FastAPI(title="Easy Trip API", version="2.0.0")

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

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

# ─────────────────────────────────────────────
# DB 설정
# ─────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


# ─────────────────────────────────────────────
# 도시 ↔ 통화 매핑
# ─────────────────────────────────────────────
CITY_CURRENCY_MAP = {
    "도쿄": "JPY", "오사카": "JPY", "후쿠오카": "JPY",
    "구마모토": "JPY", "삿포로": "JPY", "오키나와": "JPY",
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
# DB 초기화
# ─────────────────────────────────────────────
def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # room
    c.execute("""
        CREATE TABLE IF NOT EXISTS room (
            room_id          TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            admin_pw         TEXT NOT NULL,
            team_pw          TEXT DEFAULT '',
            city             TEXT DEFAULT '',
            currency         TEXT DEFAULT 'JPY',
            member_count     INTEGER DEFAULT 1,
            is_comment_enabled BOOLEAN DEFAULT FALSE
        )
    """)

    # schedule
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id              SERIAL PRIMARY KEY,
            room_id         TEXT NOT NULL,
            day_num         INTEGER NOT NULL,
            time_str        TEXT NOT NULL,
            content         TEXT NOT NULL,
            google_map_url  TEXT DEFAULT '',
            tabelog_url     TEXT DEFAULT '',
            budget          INTEGER,
            sort_order      INTEGER DEFAULT 0
        )
    """)

    # suggestion (훈수)
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

    # comment (일정별 댓글)
    c.execute("""
        CREATE TABLE IF NOT EXISTS comment (
            id          SERIAL PRIMARY KEY,
            schedule_id INTEGER NOT NULL,
            writer_name TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # 기존 DB 마이그레이션 (컬럼 없으면 추가)
    migrations = [
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS city TEXT DEFAULT ''",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'JPY'",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS member_count INTEGER DEFAULT 1",
        "ALTER TABLE room ADD COLUMN IF NOT EXISTS is_comment_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS budget INTEGER",
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception as e:
            print(f"Migration warning: {e}")
            conn.rollback()

    conn.commit()
    c.close()
    conn.close()


@app.on_event("startup")
def startup_event():
    init_db()


# ─────────────────────────────────────────────
# JWT 유틸
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


def get_current_role(
    room_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
) -> str:
    """
    JWT에서 role 추출.
    토큰 없음 → 'guest'
    토큰 있음 → 'admin' | 'team' | 'guest'
    """
    if not credentials:
        return "guest"
    payload = decode_token(credentials.credentials)
    if not payload:
        return "guest"
    if payload.get("room_id") != room_id:
        return "guest"
    return payload.get("role", "guest")


# ─────────────────────────────────────────────
# 비밀번호 검증
# ─────────────────────────────────────────────
ADMIN_PW_PATTERN = re.compile(
    r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*#?&])[A-Za-z\d@$!%*#?&]{8,}$"
)


def validate_admin_pw(pw: str) -> bool:
    return bool(ADMIN_PW_PATTERN.match(pw))


def hash_pw(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_pw(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    return pwd_context.verify(plain, hashed)


# ─────────────────────────────────────────────
# Pydantic 모델
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
            raise ValueError("비밀번호는 영문·숫자·특수문자를 포함하여 8자 이상이어야 합니다.")
        return v


class RoomUpdate(BaseModel):
    password: str
    title: Optional[str] = None
    team_pw: Optional[str] = None
    city: Optional[str] = None
    currency: Optional[str] = None
    member_count: Optional[int] = None
    is_comment_enabled: Optional[bool] = None


class LoginRequest(BaseModel):
    room_id: str
    password: str


class ScheduleCreate(BaseModel):
    day_num: int
    time_str: str
    content: str
    google_map_url: Optional[str] = ""
    tabelog_url: Optional[str] = ""
    budget: Optional[int] = None


class ScheduleUpdate(BaseModel):
    time_str: Optional[str] = None
    content: Optional[str] = None
    google_map_url: Optional[str] = None
    tabelog_url: Optional[str] = None
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
    time_str: str


class CommentCreate(BaseModel):
    writer_name: str
    content: str


class PasswordBody(BaseModel):
    password: str


# ─────────────────────────────────────────────
# 정적 파일 & 다이내믹 라우팅
# ─────────────────────────────────────────────

@app.get("/")
@app.get("/{room_id}")
def serve_frontend(room_id: str = None):
    return FileResponse("index.html")


# ─────────────────────────────────────────────
# 인증 (로그인 → JWT 발급)
# ─────────────────────────────────────────────

@app.post("/auth/login")
def login(req: LoginRequest):
    """
    비밀번호로 로그인. admin_pw 일치 → role='admin', team_pw 일치 → role='team'.
    JWT 토큰 반환.
    """
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
    elif team_pw_hash and verify_pw(req.password, team_pw_hash):
        role = "team"
    else:
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")

    token = create_access_token({"sub": req.room_id, "room_id": req.room_id, "role": role})
    return {"access_token": token, "token_type": "bearer", "role": role}


# ─────────────────────────────────────────────
# 방(Room) API
# ─────────────────────────────────────────────

@app.post("/create_room", status_code=201)
def create_room(room: RoomCreate):
    rid = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    currency = resolve_currency(room.city or "", room.currency or "")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO room
               (room_id, title, admin_pw, team_pw, city, currency, member_count, is_comment_enabled)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            rid,
            room.title,
            hash_pw(room.admin_pw),
            hash_pw(room.team_pw) if room.team_pw else "",
            room.city or "",
            currency,
            room.member_count or 1,
            room.is_comment_enabled,
        ),
    )
    conn.commit()
    c.close()
    conn.close()
    return {"room_id": rid}


@app.get("/room/{room_id}/data")
def get_room_data(room_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    role = get_current_role(room_id, credentials)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "SELECT title, city, currency, member_count, is_comment_enabled FROM room WHERE room_id=%s",
        (room_id,),
    )
    room_row = c.fetchone()
    if not room_row:
        c.close()
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    title, city, currency, member_count, is_comment_enabled = room_row
    currency_symbol = CURRENCY_SYMBOL_MAP.get(currency, "")

    # 확정 일정
    c.execute(
        """SELECT id, day_num, time_str, content, google_map_url, tabelog_url, budget
           FROM schedule
           WHERE room_id=%s
           ORDER BY day_num ASC, sort_order ASC, time_str ASC""",
        (room_id,),
    )
    schedules = [
        {
            "id": r[0], "day_num": r[1], "time_str": r[2], "content": r[3],
            "google_map_url": r[4] or "", "tabelog_url": r[5] or "",
            "budget": r[6],
        }
        for r in c.fetchall()
    ]

    # 댓글 (schedule_id별)
    schedule_ids = [s["id"] for s in schedules]
    comments_map: dict = {s["id"]: [] for s in schedules}
    if schedule_ids:
        c.execute(
            """SELECT id, schedule_id, writer_name, content,
                      to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS ts
               FROM comment
               WHERE schedule_id = ANY(%s)
               ORDER BY created_at ASC""",
            (schedule_ids,),
        )
        for row in c.fetchall():
            comments_map[row[1]].append(
                {"id": row[0], "writer_name": row[2], "content": row[3], "created_at": row[4]}
            )
    for s in schedules:
        s["comments"] = comments_map[s["id"]]

    # 훈수
    c.execute(
        """SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status
           FROM suggestion
           WHERE room_id=%s
           ORDER BY id DESC""",
        (room_id,),
    )
    suggestions = [
        {
            "id": r[0], "suggester_name": r[1], "content": r[2],
            "google_map_url": r[3] or "", "tabelog_url": r[4] or "",
            "good_cnt": r[5], "bad_cnt": r[6], "status": r[7],
        }
        for r in c.fetchall()
    ]

    # 예산 합계
    total_budget = sum(s["budget"] for s in schedules if s["budget"] is not None)

    c.close()
    conn.close()
    return {
        "room_id": room_id,
        "title": title,
        "city": city,
        "currency": currency,
        "currency_symbol": currency_symbol,
        "member_count": member_count,
        "is_comment_enabled": is_comment_enabled,
        "role": role,
        "schedules": schedules,
        "suggestions": suggestions,
        "total_budget": total_budget,
    }


@app.patch("/room/{room_id}/settings")
def update_room_settings(
    room_id: str,
    req: RoomUpdate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """관리자 전용: 방 설정 변경"""
    role = get_current_role(room_id, credentials)
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

    fields, values = [], []
    if req.title is not None:
        fields.append("title=%s"); values.append(req.title)
    if req.team_pw is not None:
        fields.append("team_pw=%s"); values.append(hash_pw(req.team_pw) if req.team_pw else "")
    if req.city is not None:
        currency = resolve_currency(req.city, req.currency)
        fields.append("city=%s"); values.append(req.city)
        fields.append("currency=%s"); values.append(currency)
    elif req.currency is not None:
        fields.append("currency=%s"); values.append(req.currency)
    if req.member_count is not None:
        fields.append("member_count=%s"); values.append(req.member_count)
    if req.is_comment_enabled is not None:
        fields.append("is_comment_enabled=%s"); values.append(req.is_comment_enabled)

    if fields:
        values.append(room_id)
        c.execute(f"UPDATE room SET {', '.join(fields)} WHERE room_id=%s", values)
        conn.commit()

    c.close()
    conn.close()
    return {"status": "ok"}


# ─────────────────────────────────────────────
# 일정(Schedule) API
# ─────────────────────────────────────────────

@app.post("/room/{room_id}/schedule", status_code=201)
def add_schedule(
    room_id: str,
    sch: ScheduleCreate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="동행자 이상의 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO schedule
               (room_id, day_num, time_str, content, google_map_url, tabelog_url, budget, sort_order)
           VALUES (%s, %s, %s, %s, %s, %s, %s,
                   (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
        (
            room_id, sch.day_num, sch.time_str, sch.content,
            sch.google_map_url, sch.tabelog_url, sch.budget,
            room_id, sch.day_num,
        ),
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


@app.patch("/room/{room_id}/schedule/{sch_id}")
def update_schedule(
    room_id: str,
    sch_id: int,
    upd: ScheduleUpdate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="동행자 이상의 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    fields, values = [], []
    if upd.time_str is not None:
        fields.append("time_str=%s"); values.append(upd.time_str)
    if upd.content is not None:
        fields.append("content=%s"); values.append(upd.content)
    if upd.google_map_url is not None:
        fields.append("google_map_url=%s"); values.append(upd.google_map_url)
    if upd.tabelog_url is not None:
        fields.append("tabelog_url=%s"); values.append(upd.tabelog_url)
    if upd.budget is not None:
        fields.append("budget=%s"); values.append(upd.budget)

    if fields:
        values += [sch_id, room_id]
        c.execute(f"UPDATE schedule SET {', '.join(fields)} WHERE id=%s AND room_id=%s", values)
        conn.commit()

    c.close()
    conn.close()
    return {"status": "ok"}


@app.delete("/room/{room_id}/schedule/{sch_id}")
def delete_schedule(
    room_id: str,
    sch_id: int,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
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
def reorder_schedule(
    room_id: str,
    req: ReorderRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role not in ("admin", "team"):
        raise HTTPException(status_code=403, detail="동행자 이상의 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    for idx, sch_id in enumerate(req.new_order):
        c.execute("UPDATE schedule SET sort_order=%s WHERE id=%s AND room_id=%s", (idx, sch_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


# ─────────────────────────────────────────────
# 훈수(Suggestion) API
# ─────────────────────────────────────────────

@app.post("/room/{room_id}/suggestion", status_code=201)
def add_suggestion(room_id: str, sug: SuggestionCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT room_id FROM room WHERE room_id=%s", (room_id,))
    if not c.fetchone():
        c.close(); conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    c.execute(
        """INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url)
           VALUES (%s, %s, %s, %s, %s)""",
        (room_id, sug.suggester_name, sug.content, sug.google_map_url, sug.tabelog_url),
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


@app.post("/room/{room_id}/suggestion/{sug_id}/vote")
def vote_suggestion(room_id: str, sug_id: int, type: str):
    if type not in ("good", "bad"):
        raise HTTPException(status_code=400, detail="type은 'good' 또는 'bad'이어야 합니다.")
    column = "good_cnt" if type == "good" else "bad_cnt"
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        f"UPDATE suggestion SET {column} = {column} + 1 WHERE id=%s AND room_id=%s",
        (sug_id, room_id),
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


@app.post("/room/{room_id}/suggestion/{sug_id}/approve")
def approve_suggestion(
    room_id: str,
    sug_id: int,
    req: ApproveRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT content, google_map_url, tabelog_url FROM suggestion WHERE id=%s AND room_id=%s", (sug_id, room_id))
    row = c.fetchone()
    if not row:
        c.close(); conn.close()
        raise HTTPException(status_code=404, detail="훈수를 찾을 수 없습니다.")

    content, gmap, tabelog = row
    c.execute(
        """INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url,
                                 sort_order)
           VALUES (%s, %s, %s, %s, %s, %s,
                   (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s AND day_num=%s))""",
        (room_id, req.day_num, req.time_str, content, gmap, tabelog, room_id, req.day_num),
    )
    c.execute("UPDATE suggestion SET status='승인됨' WHERE id=%s", (sug_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


@app.delete("/room/{room_id}/suggestion/{sug_id}")
def delete_suggestion(
    room_id: str,
    sug_id: int,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM suggestion WHERE id=%s AND room_id=%s", (sug_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


# ─────────────────────────────────────────────
# 댓글(Comment) API
# ─────────────────────────────────────────────

@app.get("/room/{room_id}/schedule/{sch_id}/comments")
def get_comments(room_id: str, sch_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT id, writer_name, content,
                  to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS ts
           FROM comment
           WHERE schedule_id=%s
           ORDER BY created_at ASC""",
        (sch_id,),
    )
    comments = [
        {"id": r[0], "writer_name": r[1], "content": r[2], "created_at": r[3]}
        for r in c.fetchall()
    ]
    c.close()
    conn.close()
    return comments


@app.post("/room/{room_id}/schedule/{sch_id}/comment", status_code=201)
def add_comment(room_id: str, sch_id: int, cm: CommentCreate):
    conn = get_db_connection()
    c = conn.cursor()

    # 댓글 허용 여부 확인
    c.execute("SELECT is_comment_enabled FROM room WHERE room_id=%s", (room_id,))
    row = c.fetchone()
    if not row:
        c.close(); conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    if not row[0]:
        c.close(); conn.close()
        raise HTTPException(status_code=403, detail="댓글 기능이 비활성화되어 있습니다.")

    # 해당 일정이 이 방에 속하는지 확인
    c.execute("SELECT id FROM schedule WHERE id=%s AND room_id=%s", (sch_id, room_id))
    if not c.fetchone():
        c.close(); conn.close()
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.")

    c.execute(
        "INSERT INTO comment (schedule_id, writer_name, content) VALUES (%s, %s, %s)",
        (sch_id, cm.writer_name, cm.content),
    )
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


@app.delete("/room/{room_id}/schedule/{sch_id}/comment/{comment_id}")
def delete_comment(
    room_id: str,
    sch_id: int,
    comment_id: int,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    role = get_current_role(room_id, credentials)
    if role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM comment WHERE id=%s AND schedule_id=%s", (comment_id, sch_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}


# ─────────────────────────────────────────────
# 편의 기능 API (정산)
# ─────────────────────────────────────────────

@app.get("/room/{room_id}/budget_summary")
def budget_summary(room_id: str, exchange_rate: Optional[float] = None):
    """
    exchange_rate: 1,000 KRW 당 현지 통화량 (수동 환율).
    예) 100 → 1,000원 = 100엔
    """
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT member_count, currency FROM room WHERE room_id=%s", (room_id,))
    room_row = c.fetchone()
    if not room_row:
        c.close(); conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    member_count, currency = room_row
    currency_symbol = CURRENCY_SYMBOL_MAP.get(currency, "")

    c.execute("SELECT COALESCE(SUM(budget), 0) FROM schedule WHERE room_id=%s", (room_id,))
    total_local = int(c.fetchone()[0])

    per_person_local = total_local // member_count if member_count else 0

    per_person_krw = None
    if exchange_rate and exchange_rate > 0:
        per_person_krw = round(per_person_local / exchange_rate * 1000)

    c.close()
    conn.close()
    return {
        "currency": currency,
        "currency_symbol": currency_symbol,
        "member_count": member_count,
        "total_local": total_local,
        "per_person_local": per_person_local,
        "per_person_krw": per_person_krw,
        "exchange_rate_per_1000krw": exchange_rate,
    }
