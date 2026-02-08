from __future__ import annotations
from typing import Optional
from datetime import date, time, datetime
from sqlmodel import SQLModel, Field, Relationship

class Photographer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    phone: Optional[str] = None
    gender: Optional[str] = None  # 남자/여자
    role: Optional[str] = None  # 메인/서브
    address: Optional[str] = None
    region: Optional[str] = None
    has_car: Optional[bool] = None
    start_date: Optional[date] = None
    status: str = Field(default="활성")  # 활성/비활성
    memo: Optional[str] = None

    # auth
    username: str = Field(index=True, unique=True)
    password_hash: str
    is_admin: bool = Field(default=False)



class WeddingHall(SQLModel, table=True):
    __tablename__ = "wedding_halls"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    address: str | None = None

class Schedule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    wedding_date: date = Field(index=True)
    wedding_time: Optional[time] = None
    shoot_start_time: Optional[time] = None  # 촬영시작시간
    venue: str = Field(index=True)
    venue_address: Optional[str] = None  # 웨딩홀 주소
    couple: Optional[str] = None

    arrival_target_time: Optional[time] = None  # 도착목표시간
    travel_minutes_default: Optional[int] = None  # 이동시간(분) - 수동/기본값
    main_name: Optional[str] = Field(index=True)
    sub_name: Optional[str] = Field(default=None, index=True)

    raw_photographers: Optional[str] = None  # F열 원본
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Checkin(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(index=True, foreign_key="schedule.id")
    photographer_name: str = Field(index=True)

    wake_time: Optional[datetime] = None
    depart_time: Optional[datetime] = None
    arrive_time: Optional[datetime] = None

    arrive_photo_path: Optional[str] = None  # 도착 증빙 사진(파일 경로)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Venue(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    address: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class RouteEstimate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(index=True, foreign_key="schedule.id")
    photographer_name: str = Field(index=True)

    minutes: int
    provider: str = "osrm"
    note: Optional[str] = None
    computed_at: datetime = Field(default_factory=datetime.utcnow)

