import os
import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import random
import string
import re

app = FastAPI(title="Easy Trip API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 환경 변수에서 DB 연결 주소를 가져옵니다. (없으면 로컬 테스트용)
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    # Render의 PostgreSQL 주소를 사용하여 연결합니다.
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# DB 테이블 생성 (PostgreSQL 문법에 맞게 수정)
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # 방 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS room 
                 (room_id TEXT PRIMARY KEY, 
                  title TEXT, 
                  admin_pw TEXT, 
                  team_pw TEXT, 
                  is_private BOOLEAN, 
                  view_pw TEXT, 
                  is_suggestion_on BOOLEAN)''')
                  
    # 일정 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS schedule 
                 (id SERIAL PRIMARY KEY, 
                  room_id TEXT, 
                  day_num INTEGER, 
                  time_str TEXT, 
                  content TEXT, 
                  google_map_url TEXT, 
                  tabelog_url TEXT)''')
                  
    # 참견 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS suggestion 
                 (id SERIAL PRIMARY KEY, 
                  room_id TEXT, 
                  suggester_name TEXT, 
                  content TEXT, 
                  google_map_url TEXT, 
                  tabelog_url TEXT, 
                  good_cnt INTEGER DEFAULT 0, 
                  bad_cnt INTEGER DEFAULT 0, 
                  status TEXT DEFAULT '대기중')''')
    
    conn.commit()
    c.close()
    conn.close()

# 서버 시작 시 DB 초기화
@app.on_event("startup")
def startup_event():
    init_db()

# --- 데이터 모델 및 유틸리티 함수 ---
class RoomCreate(BaseModel):
    title: str
    admin_pw: str
    team_pw: str = ""
    is_private: bool = False
    view_pw: str = ""
    is_suggestion_on: bool = True

    @validator('admin_pw')
    def validate_admin_pw(cls, v):
        pattern = r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*#?&])[A-Za-z\d@$!%*#?&]{8,}$"
        if not re.match(pattern, v):
            raise ValueError('비밀번호 규칙을 확인하세요.')
        return v

class ScheduleCreate(BaseModel):
    day_num: int
    time_str: str
    content: str
    google_map_url: str = ""
    tabelog_url: str = ""
    password: str

class SuggestionCreate(BaseModel):
    suggester_name: str
    content: str
    google_map_url: str = ""
    tabelog_url: str = ""

def generate_room_id():
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))

# --- API 엔드포인트 ---

@app.get("/")
def serve_home():
    return FileResponse("index.html")

@app.post("/create_room")
def create_room(room: RoomCreate):
    room_id = generate_room_id()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO room (room_id, title, admin_pw, team_pw, is_private, view_pw, is_suggestion_on) VALUES (%s, %s, %s, %s, %s, %s, %s)",
              (room_id, room.title, room.admin_pw, room.team_pw, room.is_private, room.view_pw, room.is_suggestion_on))
    conn.commit()
    c.close()
    conn.close()
    return {"room_id": room_id}

@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (room_id,))
    res = c.fetchone()
    if not res or (sch.password != res[0] and sch.password != res[1]):
        c.close()
        conn.close()
        raise HTTPException(status_code=401)
    
    c.execute("INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url) VALUES (%s, %s, %s, %s, %s, %s)",
              (room_id, sch.day_num, sch.time_str, sch.content, sch.google_map_url, sch.tabelog_url))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.post("/room/{room_id}/suggestion")
def add_suggestion(room_id: str, sug: SuggestionCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url) VALUES (%s, %s, %s, %s, %s)",
              (room_id, sug.suggester_name, sug.content, sug.google_map_url, sug.tabelog_url))
    conn.commit()
    c.close()
    conn.close()
    return {"status": "ok"}

@app.get("/room/{room_id}/data")
def get_room_data(room_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, day_num, time_str, content, google_map_url, tabelog_url FROM schedule WHERE room_id=%s ORDER BY day_num, time_str", (room_id,))
    schedules = [{"id":r[0],"day_num":r[1],"time_str":r[2],"content":r[3],"google_map_url":r[4],"tabelog_url":r[5]} for r in c.fetchall()]
    
    c.execute("SELECT id, suggester_name, content, google_map_url, tabelog_url FROM suggestion WHERE room_id=%s", (room_id,))
    suggestions = [{"id":r[0],"suggester_name":r[1],"content":r[2],"google_map_url":r[3],"tabelog_url":r[4]} for r in c.fetchall()]
    
    c.close()
    conn.close()
    return {"room_id": room_id, "schedules": schedules, "suggestions": suggestions}
