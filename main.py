from fastapi.responses import FileResponse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import random
import string
import re

app = FastAPI(title="Trip-Sync API")

# CORS 설정 (나중에 프론트엔드랑 연결할 때 에러 안 나게 해줌)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. DB 초기 세팅 (테이블 3개 생성)
def init_db():
    conn = sqlite3.connect("trip.db")
    c = conn.cursor()
    
    # [방 테이블]
    c.execute('''CREATE TABLE IF NOT EXISTS room 
                 (room_id TEXT PRIMARY KEY, 
                  title TEXT, 
                  admin_pw TEXT, 
                  team_pw TEXT, 
                  is_private BOOLEAN, 
                  view_pw TEXT, 
                  is_suggestion_on BOOLEAN)''')
                  
    # [일정 테이블]
    c.execute('''CREATE TABLE IF NOT EXISTS schedule 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  room_id TEXT, 
                  day_num INTEGER, 
                  time_str TEXT, 
                  content TEXT, 
                  google_map_url TEXT, 
                  tabelog_url TEXT)''')
                  
    # [참견(추천) 테이블]
    c.execute('''CREATE TABLE IF NOT EXISTS suggestion 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  room_id TEXT, 
                  suggester_name TEXT, 
                  content TEXT, 
                  google_map_url TEXT, 
                  tabelog_url TEXT, 
                  good_cnt INTEGER DEFAULT 0, 
                  bad_cnt INTEGER DEFAULT 0, 
                  status TEXT DEFAULT '대기중')''')
    
    conn.commit()
    conn.close()

init_db()

# 12자리 난수 생성 함수 (방 고유 링크용)
def generate_room_id():
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(12))

# 클라이언트(프론트)에서 방 만들 때 보내야 하는 데이터 형식
class RoomCreate(BaseModel):
    title: str
    admin_pw: str
    team_pw: str = ""
    is_private: bool = False
    view_pw: str = ""
    is_suggestion_on: bool = True

    # 방장 비밀번호 빡세게 검증 (영문, 숫자, 특수문자 포함 8자 이상)
    @validator('admin_pw')
    def validate_admin_pw(cls, v):
        pattern = r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*#?&])[A-Za-z\d@$!%*#?&]{8,}$"
        if not re.match(pattern, v):
            raise ValueError('방장 비밀번호는 영문, 숫자, 특수문자를 포함해 최소 8자 이상이어야 합니다.')
        return v

# 2. 방 생성 API (POST 요청)
@app.post("/create_room")
def create_room(room: RoomCreate):
    room_id = generate_room_id()
    
    conn = sqlite3.connect("trip.db")
    c = conn.cursor()
    
    try:
        c.execute("INSERT INTO room (room_id, title, admin_pw, team_pw, is_private, view_pw, is_suggestion_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (room_id, room.title, room.admin_pw, room.team_pw, room.is_private, room.view_pw, room.is_suggestion_on))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail="DB 저장 중 에러가 발생했습니다.")
        
    conn.close()
    
    return {
        "message": "방 생성 성공!",
        "room_id": room_id,
        "link": f"http://localhost:8000/room/{room_id}"  # 나중에 프론트엔드 주소로 바뀔 예정
    }
# ==========================================
# 여기서부터 main.py 맨 밑에 추가로 복붙!
# ==========================================

# 클라이언트가 일정 추가할 때 보낼 데이터 형식 (비번 필수)
class ScheduleCreate(BaseModel):
    day_num: int
    time_str: str
    content: str
    google_map_url: str = ""
    tabelog_url: str = ""
    password: str  # 방장 또는 동행자 비밀번호 확인용

# 클라이언트가 참견(추천)할 때 보낼 데이터 형식 (비번 불필요)
class SuggestionCreate(BaseModel):
    suggester_name: str
    content: str
    google_map_url: str = ""
    tabelog_url: str = ""

# 3. 일정 추가 API (권한 검사 포함)
@app.post("/room/{room_id}/schedule")
def add_schedule(room_id: str, sch: ScheduleCreate):
    conn = sqlite3.connect("trip.db")
    c = conn.cursor()
    
    # DB에서 해당 방의 비번 정보 가져오기
    c.execute("SELECT admin_pw, team_pw FROM room WHERE room_id=?", (room_id,))
    room = c.fetchone()
    
    if not room:
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    
    admin_pw, team_pw = room
    
    # 비밀번호 검증 (방장 비번이거나 동행자 비번이어야 통과)
    if sch.password != admin_pw and (not team_pw or sch.password != team_pw):
        conn.close()
        raise HTTPException(status_code=401, detail="비밀번호가 틀렸습니다. 권한이 없습니다.")

    # 통과했으면 일정 테이블에 저장
    c.execute("INSERT INTO schedule (room_id, day_num, time_str, content, google_map_url, tabelog_url) VALUES (?, ?, ?, ?, ?, ?)",
              (room_id, sch.day_num, sch.time_str, sch.content, sch.google_map_url, sch.tabelog_url))
    conn.commit()
    conn.close()
    
    return {"message": "일정이 성공적으로 추가되었습니다!"}

# 4. 참견(추천) 추가 API (비번 없이 누구나 가능)
@app.post("/room/{room_id}/suggestion")
def add_suggestion(room_id: str, sug: SuggestionCreate):
    conn = sqlite3.connect("trip.db")
    c = conn.cursor()
    
    # 방이 존재하는지, 그리고 '참견 기능'이 켜져 있는지 확인
    c.execute("SELECT is_suggestion_on FROM room WHERE room_id=?", (room_id,))
    room = c.fetchone()
    
    if not room:
        conn.close()
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.")
    if not room[0]:  # is_suggestion_on이 False(0)일 경우
        conn.close()
        raise HTTPException(status_code=403, detail="방장이 참견 기능을 꺼두었습니다.")

    # 통과했으면 참견 테이블에 저장
    c.execute("INSERT INTO suggestion (room_id, suggester_name, content, google_map_url, tabelog_url) VALUES (?, ?, ?, ?, ?)",
              (room_id, sug.suggester_name, sug.content, sug.google_map_url, sug.tabelog_url))
    conn.commit()
    conn.close()
    
    return {"message": "추천(참견)이 등록되었습니다! 방장의 승인을 기다립니다."}

# 5. 방의 모든 데이터(일정 + 참견) 한 번에 불러오기 API
@app.get("/room/{room_id}/data")
def get_room_data(room_id: str):
    conn = sqlite3.connect("trip.db")
    c = conn.cursor()
    
    # 확정 일정 리스트 가져오기 (날짜, 시간순 정렬)
    c.execute("SELECT id, day_num, time_str, content, google_map_url, tabelog_url FROM schedule WHERE room_id=? ORDER BY day_num ASC, time_str ASC", (room_id,))
    schedules = [{"id": r[0], "day_num": r[1], "time_str": r[2], "content": r[3], "google_map_url": r[4], "tabelog_url": r[5]} for r in c.fetchall()]
    
    # 눈팅족 참견 리스트 가져오기 (개추 많이 받은 순 정렬)
    c.execute("SELECT id, suggester_name, content, google_map_url, tabelog_url, good_cnt, bad_cnt, status FROM suggestion WHERE room_id=? ORDER BY good_cnt DESC", (room_id,))
    suggestions = [{"id": r[0], "suggester_name": r[1], "content": r[2], "google_map_url": r[3], "tabelog_url": r[4], "good_cnt": r[5], "bad_cnt": r[6], "status": r[7]} for r in c.fetchall()]
    
    conn.close()
    
    return {
        "room_id": room_id,
        "schedules": schedules,
        "suggestions": suggestions
    }
# 첫 화면(도메인 주소) 접속 시 index.html 띄워주기
@app.get("/")
def serve_home():
    return FileResponse("index.html")