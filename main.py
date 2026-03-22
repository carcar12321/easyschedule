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

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # 방 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS room 
                 (room_id TEXT PRIMARY KEY, title TEXT, admin_pw TEXT, team_pw TEXT, 
                  is_private BOOLEAN, view_pw TEXT, is_suggestion_on BOOLEAN)''')
    # 일정 테이블 (sort_order 컬럼 추가)
    c.execute('''CREATE TABLE IF NOT EXISTS schedule 
                 (id SERIAL PRIMARY KEY, room_id TEXT, day_num INTEGER, time_str TEXT, 
                  content TEXT, google_map_url TEXT, tabelog_url TEXT, sort_order INTEGER DEFAULT 0)''')
    # 참견 테이블
    c.execute('''CREATE TABLE IF NOT EXISTS suggestion 
                 (id SERIAL PRIMARY KEY, room_id TEXT, suggester_name TEXT, content TEXT, 
                  google_map_url TEXT, tabelog_url TEXT, good_cnt INTEGER DEFAULT 0, 
                  bad_cnt INTEGER DEFAULT 0, status TEXT DEFAULT '대기중')''')
    
    # [패치] 기존 DB에 sort_order 컬럼이 없을 경우를 대비해 강제 추가
    try:
        c.execute("ALTER TABLE schedule ADD COLUMN sort_order INTEGER DEFAULT 0")
    except:
        conn.rollback() # 이미 있으면 에러나므로 롤백
    
    conn.commit()
    c.close()
    conn.close()

@app.on_event("startup")
def startup_event():
    init_db()

# --- 모델 ---
class RoomCreate(BaseModel):
    title: str
    admin_pw: str
    team_pw: str = ""; is_private: bool = False; view_pw: str = ""; is_suggestion_on: bool = True
    @validator('admin_pw')
    def validate_admin_pw(cls, v):
        if not re.match(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*#?&])[A-Za-z\d@$!%*#?&]{8,}$", v):
            raise ValueError('비밀번호 규칙 위반')
        return v

class ScheduleCreate(BaseModel):
    day_num: int; time_str: str; content: str; google_map_url: str = ""; tabelog_url: str = ""; password: str

class ReorderRequest(BaseModel):
    password: str
    new_order: list # [id1, id2, id3...] 순서

class ApproveRequest(BaseModel):
    password: str; day_num: int; time_str: str

# --- API ---
@app.get("/")
def serve_home(): return FileResponse("index.html")

@app.post("/create_room")
def create_room(room: RoomCreate):
    rid = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO room VALUES (%s, %s, %s, %s, %s, %s, %s)", (rid, room.title, room.admin_pw, room.team_pw, room.is_private, room.view_pw, room.is_suggestion_on))
    conn.commit(); c.close(); conn.close()
    return {"room_id": rid}

@app.get("/room/{room_id}/data")
def get_room_data(room_id: str):
    conn = get_db_connection(); c = conn.cursor()
    # sort_order 순으로 가져옴
    c.execute("SELECT id, day_num, time_str, content, google_map_url, tabelog_url FROM schedule WHERE room_id=%s ORDER BY day_num ASC, sort_order ASC, time_str ASC", (room_id,))
    schs = [{"id":r[0],"day_num":r[1],"time_str":r[2],"content":r[3],"google_map_url":r[4],"tabelog_url":r[5]} for r in c.fetchall()]
    c.execute("SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status FROM suggestion WHERE room_id=%s ORDER BY good_cnt DESC", (room_id,))
    sugs = [{"id":r[0],"suggester_name":r[1],"content":r[2],"google_map_url":r[3],"tabelog_url":r[4],"good_cnt":r[5],"bad_cnt":r[6],"status":r[7]} for r in c.fetchall()]
    c.close(); conn.close()
    return {"schedules": schs, "suggestions": sugs}

@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (room_id,))
    pw = c.fetchone()
    if not pw or (sch.password != pw[0] and sch.password != pw[1]): raise HTTPException(401)
    c.execute("INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url, sort_order) VALUES (%s, %s, %s, %s, %s, %s, (SELECT COALESCE(MAX(sort_order),0)+1 FROM schedule WHERE room_id=%s AND day_num=%s))", (room_id, sch.day_num, sch.time_str, sch.content, sch.google_map_url, sch.tabelog_url, room_id, sch.day_num))
    conn.commit(); c.close(); conn.close()
    return {"status": "ok"}

# [패치] 순서 변경 API
@app.post("/room/{room_id}/reorder")
def reorder_schedule(room_id: str, req: ReorderRequest):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=%s", (room_id,))
    pw = c.fetchone()
    if not pw or (req.password != pw[0] and req.password != pw[1]): raise HTTPException(401)
    for index, sch_id in enumerate(req.new_order):
        c.execute("UPDATE schedule SET sort_order=%s WHERE id=%s AND room_id=%s", (index, sch_id, room_id))
    conn.commit(); c.close(); conn.close()
    return {"status": "ok"}

# [패치] 추천 투표, 승인, 삭제 API (간소화)
@app.post("/room/{room_id}/suggestion/{sug_id}/vote")
def vote_sug(room_id: str, sug_id: int, type: str):
    conn = get_db_connection(); c = conn.cursor()
    col = "good_cnt" if type == "good" else "bad_cnt"
    c.execute(f"UPDATE suggestion SET {col} = {col} + 1 WHERE id=%s", (sug_id,))
    conn.commit(); c.close(); conn.close()
    return {"ok":True}

@app.post("/room/{room_id}/suggestion/{sug_id}/approve")
def approve_sug(room_id: str, sug_id: int, req: ApproveRequest):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    if req.password != c.fetchone()[0]: raise HTTPException(401)
    c.execute("SELECT content, google_map_url, tabelog_url FROM suggestion WHERE id=%s", (sug_id,))
    s = c.fetchone()
    c.execute("INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url) VALUES (%s, %s, %s, %s, %s, %s)", (room_id, req.day_num, req.time_str, s[0], s[1], s[2]))
    c.execute("UPDATE suggestion SET status='승인됨' WHERE id=%s", (sug_id,))
    conn.commit(); c.close(); conn.close()
    return {"ok":True}

@app.delete("/room/{room_id}/suggestion/{sug_id}")
def del_sug(room_id: str, sug_id: int, pw: str):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT admin_pw FROM room WHERE room_id=%s", (room_id,))
    if pw != c.fetchone()[0]: raise HTTPException(401)
    c.execute("DELETE FROM suggestion WHERE id=%s", (sug_id,))
    conn.commit(); c.close(); conn.close()
    return {"ok":True}
