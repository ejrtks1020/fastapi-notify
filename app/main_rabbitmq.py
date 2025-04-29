from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
import json
import os
import asyncio
from dotenv import load_dotenv
from typing import Optional, AsyncGenerator, Dict, Any
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
import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractConnection, AbstractChannel, AbstractExchange, AbstractQueue
from contextlib import asynccontextmanager

ic.configureOutput(includeContext=True)

load_dotenv()

app = FastAPI()

# 템플릿과 정적 파일 설정
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# RabbitMQ 연결 설정
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "/")

# RabbitMQ 클라이언트 설정
rabbitmq_connection: Optional[AbstractConnection] = None
rabbitmq_channel: Optional[AbstractChannel] = None
rabbitmq_exchange: Optional[AbstractExchange] = None

DATABASE_URL = "sqlite:///./notifications.db"  # 또는 다른 데이터베이스 URL

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@app.on_event("startup")
async def startup_db_client():
    global rabbitmq_connection, rabbitmq_channel, rabbitmq_exchange
    
    # RabbitMQ 연결
    rabbitmq_connection = await aio_pika.connect_robust(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        login=RABBITMQ_USER,
        password=RABBITMQ_PASS,
        virtualhost=RABBITMQ_VHOST
    )
    
    # 채널 및 교환기 설정
    rabbitmq_channel = await rabbitmq_connection.channel()
    rabbitmq_exchange = await rabbitmq_channel.declare_exchange(
        "notifications", 
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )
    
    # 데이터베이스 초기화
    Base.metadata.create_all(bind=engine, checkfirst=True)

@app.on_event("shutdown")
async def shutdown_db_client():
    global rabbitmq_connection
    if rabbitmq_connection:
        await rabbitmq_connection.close()

@asynccontextmanager
async def get_rabbitmq_channel() -> AbstractChannel:
    global rabbitmq_channel
    yield rabbitmq_channel

async def get_rabbitmq_exchange() -> AbstractExchange:
    global rabbitmq_exchange
    return rabbitmq_exchange

@app.get("/", response_class=HTMLResponse)
async def get_homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

async def event_generator(user_id: str) -> AsyncGenerator:
    """사용자별 이벤트 생성기"""
    routing_key = f"user.{user_id}"
    
    # 사용자별 큐 선언
    async with get_rabbitmq_channel() as channel:
        queue_name = f"notifications.{user_id}"
        queue = await channel.declare_queue(
            queue_name,
            durable=True,
            auto_delete=True
        )
        
        # 라우팅 키로 큐 바인딩
        await queue.bind(rabbitmq_exchange, routing_key=routing_key)
        
        # 초기 연결 메시지
        yield {
            "event": "connect",
            "data": json.dumps({"status": "connected", "routing_key": routing_key, "channel": user_id})
        }
        
        # 메시지 소비
        async with queue.iterator() as queue_iter:
            try:
                async for message in queue_iter:
                    async with message.process():
                        try:
                            body = message.body.decode()
                            data = json.loads(body)
                            event_type = data.get("event", "message")
                            
                            yield {
                                "event": event_type,
                                "data": body
                            }
                        except json.JSONDecodeError:
                            yield {
                                "event": "message",
                                "data": body
                            }
                        
                        # 연결 유지를 위한 주기적인 핑
                        await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                await queue.unbind(rabbitmq_exchange, routing_key)
                # queue.delete() 호출은 필요하지 않음 (auto_delete=True 설정으로 인해)

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
    exchange: AbstractExchange = Depends(get_rabbitmq_exchange)
):
    data = await request.json()
    routing_key = f"user.{user_id}"
    
    # 메시지 생성
    message = {
        "event": "notification",
        "title": data.get("title", "알림"),
        "message": data.get("message", "새로운 알림이 있습니다."),
        "icon": data.get("icon", "/static/notification-icon.png"),
        "timestamp": data.get("timestamp", datetime.now().isoformat()),
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
    await exchange.publish(
        aio_pika.Message(
            body=json.dumps(message).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        ),
        routing_key=routing_key
    )
    
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
    db: Session = Depends(get_db),
    exchange: AbstractExchange = Depends(get_rabbitmq_exchange)
):
    """모든 사용자에게 브로드캐스트 알림 전송"""
    data = await request.json()
    users = data.get("users", [])
    
    message = {
        "event": "notification",
        "title": data.get("title", "알림"),
        "message": data.get("message", "새로운 알림이 있습니다."),
        "icon": data.get("icon", "/static/notification-icon.png"),
        "timestamp": data.get("timestamp", datetime.now().isoformat())
    }
    
    message_json = json.dumps(message)
    message_body = message_json.encode()
    
    # 특정 사용자 목록이 제공된 경우
    if users:
        # 데이터베이스에 각 사용자별로 알림 저장
        for user_id in users:
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
            
            # 실시간 알림 발송
            routing_key = f"user.{user_id}"
            await exchange.publish(
                aio_pika.Message(
                    body=message_body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                ),
                routing_key=routing_key
            )
        
        db.commit()
        return {"status": "success", "message": f"Notification sent to {len(users)} users"}
    else:
        # 브로드캐스트 메시지 발송 (모든 사용자용 라우팅 키)
        await exchange.publish(
            aio_pika.Message(
                body=message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            ),
            routing_key="broadcast"
        )
        
        # 데이터베이스에 등록된 모든 사용자 조회
        users = db.query(User).all()
        for user in users:
            new_notification = Notification(
                user_id=user.id,
                title=message["title"],
                message=message["message"],
                icon=message["icon"],
                created_at=parse_iso_datetime(message["timestamp"]),
                category=data.get("category"),
                priority=data.get("priority", 0)
            )
            db.add(new_notification)
        
        db.commit()
        return {"status": "success", "message": f"Notification broadcasted to all users ({len(users)} users)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_rabbitmq:app", host="0.0.0.0", port=8000, reload=True) 