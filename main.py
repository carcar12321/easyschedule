import os
import psycopg2
import random
import string
import re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Optional

app = FastAPI(title="Easy Trip API")

# CORS 설정: 프론트엔드에서 API에 접근할 수 있도록 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Render 환경 변수에서 PostgreSQL 연결 주소를 가져옴
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """데이터베이스 연결 객체를 생성하여 반환합니다."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """서버 시작 시 필요한 테이블들을 생성하고 구조를 업데이트합니다."""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. 방(Room) 테이블 생성
    c.execute('''CREATE TABLE IF NOT EXISTS room 
                 (room_id TEXT PRIMARY KEY, 
                  title TEXT, 
                  admin_pw TEXT, 
                  team_pw TEXT DEFAULT '', 
                  is_private BOOLEAN DEFAULT FALSE, 
                  view_pw TEXT DEFAULT '', 
                  is_suggestion_on BOOLEAN DEFAULT TRUE)''')
                  
    # 2. 일정(Schedule) 테이블 생성
    c.execute('''CREATE TABLE IF NOT EXISTS schedule 
                 (id SERIAL PRIMARY KEY, 
                  room_id TEXT, 
                  day_num INTEGER, 
                  time_str TEXT, 
                  content TEXT, 
                  google_map_url TEXT DEFAULT '', 
                  tabelog_url TEXT DEFAULT '', 
                  sort_order INTEGER DEFAULT 0)''')
                  
    # 3. 참견(Suggestion) 테이블 생성
    c.execute('''CREATE TABLE IF NOT EXISTS suggestion 
                 (id SERIAL PRIMARY KEY, 
                  room_id TEXT, 
                  suggester_name TEXT, 
                  content TEXT, 
                  google_map_url TEXT DEFAULT '', 
                  tabelog_url TEXT DEFAULT '', 
                  good_cnt INTEGER DEFAULT 0, 
                  bad_cnt INTEGER DEFAULT 0, 
                  status TEXT DEFAULT '대기중')''')
    
    # [기존 데이터 보존용] 테이블 구조 업데이트 (컬럼이 없을 경우 추가)
    try:
        c.execute("ALTER TABLE room ADD COLUMN IF NOT EXISTS team_pw TEXT DEFAULT ''")
        c.execute("ALTER TABLE room ADD COLUMN IF NOT EXISTS is_suggestion_on BOOLEAN DEFAULT TRUE")
        c.execute("ALTER TABLE schedule ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
    except Exception as e:
        print(f"DB Update Warning: {e}")
        conn.rollback()
    
    conn.commit()
    c.close()
    conn.close()

@app.on_event("startup")
def startup_event():
    init_db()

# --- 데이터 모델 정의 ---

class RoomCreate(BaseModel):
    title: str
    admin_pw: str
    team_pw: Optional[str] = ""
    is_private: Optional[bool] = False
    view_pw: Optional[str] = ""
    is_suggestion_on: Optional[bool] = True

    @validator('admin_pw')
    def validate_admin_pw(cls, v):
        # 영문, 숫자, 특수문자 포함 8자 이상 정규식
        pattern = r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*#?&])[A-Za-z\d@$!%*#?&]{8,}$"
        if not re.match(pattern, v):
            raise ValueError('비밀번호는 영문, 숫자, 특수문자를 포함하여 8자 이상이어야 합니다.')
        return v

class ScheduleCreate(BaseModel):
    day_num: int
    time_str: str
    content: str
    google_map_url: Optional[str] = ""
    tabelog_url: Optional[str] = ""
    password: str

class ReorderRequest(BaseModel):
    password: str
    new_order: List[int] # [id1, id2, id3...] 순서

class SuggestionCreate(BaseModel):
    suggester_name: str
    content: str
    google_map_url: Optional[str] = ""
    tabelog_url: Optional[str] = ""

class ApproveRequest(BaseModel):
    password: str
    day_num: int
    time_str: str

# --- API 엔드포인트 ---

# 다이내믹 라우팅 지원: 어떤 경로로 들어오든 index.html을 반환
@app.get("/")
@app.get("/{room_id}")
def serve_home(room_id: str = None):
    return FileResponse("index.html")

@app.post("/create_room")
def create_room(room: RoomCreate):
    # 12자리 랜덤 방 ID 생성
    rid = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO room (room_id, title, admin_pw, team_pw, is_private, view_pw, is_suggestion_on) 
                 VALUES (%s, %s, %s, %s, %s, %s, %s)""", 
              (rid, room.title, room.admin_pw, room.team_pw, room.is_private, room.view_pw, room.is_suggestion_on))
    conn.commit()
    c.close()
    conn.close()
    return {"room_id": rid}

@app.get("/room/{room_id}/data")
def get_room_data(room_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    
    # 방 기본 정보 확인
    c.execute("SELECT is_suggestion_on, title FROM room WHERE room_id=%s", (room_id,))
    room_info = c.fetchone()
    if not room_info:
        c.close()
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")

    # 확정 일정 조회 (정렬 순서 반영)
    c.execute("""SELECT id, day_num, time_str, content, google_map_url, tabelog_url 
                 FROM schedule WHERE room_id=%s 
                 ORDER BY day_num ASC, sort_order ASC, time_str ASC""", (room_id,))
    schedules = [{"id":r[0],"day_num":r[1],"time_str":r[2],"content":r[3],"google_map_url":r[4],"tabelog_url":r[5]} for r in c.fetchall()]
    
    # 참견(추천) 조회
    c.execute("""SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status 
                 FROM suggestion WHERE room_id=%s ORDER BY id DESC""", (room_id,))
    suggestions = [{"id":r[0],"suggester_name":r[1],"content":r[2],"google_map_url":r[3],"tabelog_url":r[4],"good_cnt":r[5],"bad_cnt":r[6],"status":r[7]} for r in c.fetchall()]
    
    c.close()
    conn.close()
    return {
        "room_id": room_id, 
        "title": room_info[1],
        "is_suggestion_on": room_info[0],
        "schedules": schedules, 
        "suggestions": suggestions
    }

@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (room_id,))
    pw_res = c.fetchone()
    
    if not pw_res or (sch.password != pw_res[0] and sch.password != pw_res[1]):
        c.close()
        conn.close()
        raise HTTPException(status_code=401, detail="권한이 없습니다.")
    
    c.execute("""INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url, sort_order) 
                 VALUES (%s, %s, %s, %s, %s, %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM schedule WHERE room_id=%s))""", 
              (room_id, sch.day_num, sch.time_str, sch.content, sch.google_map_url, sch.tabelog_url, room_id))
    
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/reorder")
def reorder_schedule(room_id: str, req: ReorderRequest):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (room_id,))
    pw_res = c.fetchone()
    
    if not pw_res or (req.password != pw_res[0] and req.password != pw_res[1]):
        c.close()
        conn.close()
        raise HTTPException(status_code=401)
        
    for index, sch_id in enumerate(req.new_order):
        c.execute("UPDATE schedule SET sort_order=%s WHERE id=%s AND room_id=%s", (index, sch_id, room_id))
        
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion")
def add_suggestion(room_id: str, sug: SuggestionCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT is_suggestion_on FROM room WHERE room_id=%s", (room_id,))
    if not c.fetchone()[0]:
        c.close()
        conn.close()
        raise HTTPException(status_code=403, detail="이 방은 추천 기능이 비활성화되어 있습니다.")
        
    c.execute("""INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url) 
                 VALUES (%s, %s, %s, %s, %s)""", 
              (room_id, sug.suggester_name, sug.content, sug.google_map_url, sug.tabelog_url))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sug_id}/vote")
def vote_suggestion(room_id: str, sug_id: int, type: str):
    conn = get_db_connection()
    c = conn.cursor()
    column = "good_cnt" if type == "good" else "bad_cnt"
    c.execute(f"UPDATE suggestion SET {column} = {column} + 1 WHERE id=%s AND room_id=%s", (sug_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion/{sug_id}/approve")
def approve_suggestion(room_id: str, sug_id: int, req: ApproveRequest):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    if req.password != c.fetchone()[0]:
        c.close()
        conn.close()
        raise HTTPException(status_code=401)
        
    c.execute("SELECT content, google_map_url, tabelog_url FROM suggestion WHERE id=%s", (sug_id,))
    s = c.fetchone()
    c.execute("""INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url) 
                 VALUES (%s, %s, %s, %s, %s, %s)""", (room_id, req.day_num, req.time_str, s[0], s[1], s[2]))
    c.execute("UPDATE suggestion SET status='승인됨' WHERE id=%s", (sug_id,))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.delete("/room/{room_id}/suggestion/{sug_id}")
def delete_suggestion(room_id: str, sug_id: int, password: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    if password != c.fetchone()[0]:
        c.close()
        conn.close()
        raise HTTPException(status_code=401)
    c.execute("DELETE FROM suggestion WHERE id=%s AND room_id=%s", (sug_id, room_id))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}
