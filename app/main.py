from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from redis.asyncio import Redis
import json
import os
import asyncio
from dotenv import load_dotenv
from typing import Optional, AsyncGenerator
from sqlalchemy.orm import Session
from sqlalchemy.future import select
from models import User, Notification
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from models import Base
from icecream import ic
import re
ic.configureOutput(includeContext=True)

load_dotenv()

app = FastAPI()

# 템플릿과 정적 파일 설정
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Redis 연결 설정
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)

# Redis 클라이언트 설정
redis_client = None

DATABASE_URL = "sqlite:///./notifications.db"  # 또는 다른 데이터베이스 URL

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@app.on_event("startup")
async def startup_db_client():
    global redis_client
    redis_client = Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True
    )
    Base.metadata.create_all(bind=engine, checkfirst=True)

@app.on_event("shutdown")
async def shutdown_db_client():
    if redis_client:
        await redis_client.close()

async def get_redis() -> Redis:
    return redis_client

@app.get("/", response_class=HTMLResponse)
async def get_homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

async def event_generator(user_id: str) -> AsyncGenerator:
    """사용자별 이벤트 생성기"""
    channel_name = f"channel:{user_id}"
    subscription = redis_client.pubsub()
    await subscription.subscribe(channel_name)
    
    try:
        # 초기 연결 메시지
        yield {
            "event": "connect",
            "data": json.dumps({"status": "connected", "channel": channel_name})
        }
        
        # 이벤트 리스닝 루프
        while True:
            message = await subscription.get_message(ignore_subscribe_messages=True, timeout=1.0)
            ic(message)
            if message and message["type"] == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                
                try:
                    parsed_data = json.loads(data)
                    event_type = parsed_data.get("event", "message")
                    
                    yield {
                        "event": event_type,
                        "data": data
                    }
                except json.JSONDecodeError:
                    yield {
                        "event": "message",
                        "data": data
                    }
            
            # 연결 유지를 위한 주기적인 핑
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        await subscription.unsubscribe(channel_name)
        await subscription.close()
    finally:
        await subscription.unsubscribe(channel_name)
        await subscription.close()

@app.get("/events/{user_id}")
async def sse_endpoint(user_id: str):
    """SSE 이벤트 엔드포인트"""
    return EventSourceResponse(event_generator(user_id))

# 데이터베이스 의존성
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ISO 형식 날짜 문자열 처리 개선
def parse_iso_datetime(iso_string):
    if not isinstance(iso_string, str):
        return datetime.now()
    
    # 'Z' 표기를 '+00:00'으로 변환
    iso_string = iso_string.replace('Z', '+00:00')
    
    try:
        return datetime.fromisoformat(iso_string)
    except ValueError:
        # 파싱 실패 시 현재 시간 반환
        return datetime.now()

# 알림 발송 및 저장
@app.post("/notify/{user_id}")
async def send_notification(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis)
):
    data = await request.json()
    channel_name = f"channel:{user_id}"
    
    # 메시지 생성
    message = {
        "event": "notification",
        "title": data.get("title", "알림"),
        "message": data.get("message", "새로운 알림이 있습니다."),
        "icon": data.get("icon", "/static/notification-icon.png"),
        "timestamp": data.get("timestamp", datetime.now()),
        "id": None  # 저장 후 업데이트
    }
    ic(message['timestamp'])
    
    # 데이터베이스에 알림 저장
    new_notification = Notification(
        user_id=user_id,
        title=message["title"],
        message=message["message"],
        icon=message["icon"],
        created_at=parse_iso_datetime(message["timestamp"]),
        category=data.get("category"),
        priority=data.get("priority", 0)
    )
    
    db.add(new_notification)
    db.commit()
    db.refresh(new_notification)
    
    # 저장된 알림 ID를 메시지에 추가
    message["id"] = new_notification.id
    
    # 실시간 알림 발송
    await redis.publish(channel_name, json.dumps(message))
    
    return {"status": "success", "message": f"Notification sent to {user_id}", "notification_id": new_notification.id}

# 알림 목록 조회
@app.get("/notifications/{user_id}")
async def get_notifications(
    user_id: str,
    limit: int = 20,
    offset: int = 0,
    unread_only: bool = False,
    db: Session = Depends(get_db)
):
    query = select(Notification).where(Notification.user_id == user_id)
    
    if unread_only:
        query = query.where(Notification.is_read == False)
    
    query = query.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    results = db.execute(query).scalars().all()
    
    return [
        {
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "icon": notification.icon,
            "is_read": notification.is_read,
            "created_at": notification.created_at.isoformat(),
            "read_at": notification.read_at.isoformat() if notification.read_at else None,
            "category": notification.category,
            "priority": notification.priority
        }
        for notification in results
    ]

# 알림 읽음 상태 변경
@app.put("/notifications/{notification_id}/read")
async def mark_notification_as_read(
    notification_id: int,
    db: Session = Depends(get_db)
):
    notification = db.query(Notification).filter(Notification.id == notification_id).first()
    
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    notification.is_read = True
    notification.read_at = datetime.utcnow()
    db.commit()
    
    return {"status": "success", "message": "Notification marked as read"}

# 모든 알림 읽음 상태로 변경
@app.put("/notifications/{user_id}/read-all")
async def mark_all_notifications_as_read(
    user_id: str,
    db: Session = Depends(get_db)
):
    db.query(Notification).filter(
        Notification.user_id == user_id,
        Notification.is_read == False
    ).update({"is_read": True, "read_at": datetime.utcnow()})
    
    db.commit()
    
    return {"status": "success", "message": "All notifications marked as read"}

@app.post("/broadcast")
async def broadcast_notification(
    request: Request,
    redis: Redis = Depends(get_redis)
):
    """모든 사용자에게 브로드캐스트 알림 전송"""
    data = await request.json()
    users = data.get("users", [])
    
    message = {
        "event": "notification",
        "title": data.get("title", "알림"),
        "message": data.get("message", "새로운 알림이 있습니다."),
        "icon": data.get("icon", "/static/notification-icon.png"),
        "timestamp": data.get("timestamp", None)
    }
    
    message_json = json.dumps(message)
    
    # 특정 사용자 목록이 제공된 경우
    if users:
        for user_id in users:
            channel_name = f"channel:{user_id}"
            await redis.publish(channel_name, message_json)
        return {"status": "success", "message": f"Notification sent to {len(users)} users"}
    else:
        # 모든 채널 패턴 가져오기 (사용자 목록이 제공되지 않은 경우)
        channels = await redis.keys("channel:*")
        for channel in channels:
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8")
            await redis.publish(channel, message_json)
        return {"status": "success", "message": f"Notification broadcasted to all users ({len(channels)} channels)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 