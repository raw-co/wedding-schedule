from __future__ import annotations

from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import date, timedelta

import os
import tempfile
import uuid
import time

from .db import init_db, get_session
from .models import Photographer, Schedule, Checkin, RouteEstimate, Venue, WeddingHall
from .auth import hash_password, verify_password, set_session, clear_session, get_user_id_from_request
from .importer import load_schedules_from_excel, load_photographers_from_excel

app = FastAPI(title="Wedding Schedule App")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# 업로드(도착사진) 임시 저장 폴더: 운영에서는 환경변수로 바꿀 수 있음
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "uploads"))

def cleanup_uploads(ttl_hours: int = 6) -> None:
    """UPLOAD_DIR 내 사진을 ttl_hours 지난 것부터 자동 삭제"""
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except Exception:
        return
    ttl = ttl_hours * 3600
    now = time.time()
    try:
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            try:
                if os.path.isfile(path) and (now - os.path.getmtime(path)) > ttl:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
app.mount("/uploads", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "uploads")), name="uploads")

@app.on_event("startup")
def on_startup():
    init_db()

def get_current_user(request: Request, session: Session) -> Photographer | None:
    uid = get_user_id_from_request(request)
    if not uid:
        return None
    return session.get(Photographer, uid)



def upsert_wedding_hall(session: Session, name: str, address: str | None) -> "WeddingHall | None":
    name = (name or "").strip()
    if not name:
        return None
    hall = session.exec(select(WeddingHall).where(WeddingHall.name == name)).first()
    if hall is None:
        hall = WeddingHall(name=name, address=(address.strip() if address else None))
        session.add(hall)
        session.commit()
        session.refresh(hall)
        return hall
    # update address only if provided (non-empty)
    if address and address.strip():
        hall.address = address.strip()
        session.add(hall)
        session.commit()
        session.refresh(hall)
    return hall

def fill_schedule_address_from_hall(session: Session, schedule: "Schedule") -> None:
    if schedule.venue and (not schedule.venue_address or not schedule.venue_address.strip()):
        hall = session.exec(select(WeddingHall).where(WeddingHall.name == schedule.venue.strip())).first()
        if hall and hall.address:
            schedule.venue_address = hall.address
            session.add(schedule)
            session.commit()
            session.refresh(schedule)


def propagate_hall_address(session: Session, hall_name: str, address: str) -> int:
    """Update all schedules with same wedding hall name to have the given address.
    Returns number of schedules updated.
    """
    hall_name = (hall_name or "").strip()
    address = (address or "").strip()
    if not hall_name or not address:
        return 0
    schedules = session.exec(select(Schedule).where(Schedule.venue == hall_name)).all()
    updated = 0
    for s in schedules:
        if (s.venue_address or "").strip() != address:
            s.venue_address = address
            session.add(s)
            updated += 1
    if updated:
        # clear route estimates for affected schedules so they are recalculated
        try:
            for s in schedules:
                routes = session.exec(select(RouteEstimate).where(RouteEstimate.schedule_id == s.id)).all()
                for r in routes:
                    session.delete(r)
        except Exception:
            pass
        session.commit()
    return updated

def require_login(user: Photographer | None):
    if not user:
        return RedirectResponse("/login", status_code=302)

def week_range(today: date):
    # 월~일
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end

def ensure_admin(session: Session):
    admin = session.exec(select(Photographer).where(Photographer.username == "admin")).first()
    if not admin:
        admin = Photographer(
            name="관리자",
            username="admin",
            password_hash=hash_password("admin1234"),
            is_admin=True,
            status="활성",
        )
        session.add(admin)
        session.commit()


def get_or_create_checkin(session: Session, schedule_id: int, photographer_name: str) -> Checkin:
    chk = session.exec(select(Checkin).where(
        (Checkin.schedule_id == schedule_id) & (Checkin.photographer_name == photographer_name)
    )).first()
    if chk:
        return chk
    chk = Checkin(schedule_id=schedule_id, photographer_name=photographer_name)
    session.add(chk)
    session.commit()
    session.refresh(chk)
    return chk
def get_cached_route_minutes(session: Session, schedule: Schedule, photographer_name: str, photographer_address: str) -> int | None:
    # 1) 캐시(표) 확인
    cached = session.exec(
        select(RouteEstimate).where(
            (RouteEstimate.schedule_id == schedule.id) & (RouteEstimate.photographer_name == photographer_name)
        )
    ).first()
    if cached:
        return cached.minutes

    # 2) 스케줄에 수동 기본값이 있으면 그걸 사용 (계산 실패 대비)
    if schedule.travel_minutes_default is not None:
        return schedule.travel_minutes_default

    # 3) 주소가 둘 다 있으면 온라인으로 계산(프로토타입)
    if photographer_address and schedule.venue_address:
        try:
            mins = estimate_travel_minutes(photographer_address, schedule.venue_address)
        except Exception:
            mins = None
        if mins is not None:
            session.add(RouteEstimate(schedule_id=schedule.id, photographer_name=photographer_name, minutes=mins, provider="osrm"))
            session.commit()
            return mins

    return None

def compute_deadlines(schedule: Schedule, travel_minutes: int | None) -> dict:
    # 기준 도착목표시간: arrival_target_time 있으면 우선, 없으면 예식시간 - 2시간
    from datetime import datetime, timedelta
    if schedule.wedding_time is None:
        return {"arrival_target_dt": None, "wake_deadline": None, "depart_deadline": None}

    base_time = schedule.arrival_target_time or (datetime.combine(schedule.wedding_date, schedule.wedding_time) - timedelta(hours=2)).time()
    arrival_target_dt = datetime.combine(schedule.wedding_date, base_time)

    wake_deadline = arrival_target_dt - timedelta(hours=2)

    # 출발마감: 이동시간이 있으면 arrival_target - travel, 없으면 arrival_target - 60분
    if travel_minutes is None:
        depart_deadline = arrival_target_dt - timedelta(minutes=60)
    else:
        depart_deadline = arrival_target_dt - timedelta(minutes=travel_minutes)

    return {
        "arrival_target_dt": arrival_target_dt,
        "wake_deadline": wake_deadline,
        "depart_deadline": depart_deadline,
    }



@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    ensure_admin(session)
    user = get_current_user(request, session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.is_admin:
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/my", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
def login(
    request: Request,
    session: Session = Depends(get_session),
    username: str = Form(...),
    password: str = Form(...),
):
    user = session.exec(select(Photographer).where(Photographer.username == username)).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401)

    resp = RedirectResponse("/", status_code=302)
    set_session(resp, user.id)
    return resp

@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    clear_session(resp)
    return resp

@app.post("/check/wake")
def check_wake(
    request: Request,
    session: Session = Depends(get_session),
    schedule_id: int = Form(...),
):
    user = get_current_user(request, session)
    if not user or user.is_admin:
        return RedirectResponse("/login", status_code=302)

    s0 = session.get(Schedule, schedule_id)
    if not s0:
        return RedirectResponse("/my", status_code=302)

    # ✅ 하루에 한 번만: 같은 날짜의 내 모든 스케줄에 '기상'을 한 번에 찍기
    from datetime import datetime
    day = s0.wedding_date
    my_schedules = session.exec(
        select(Schedule).where(
            (Schedule.wedding_date == day)
            & ((Schedule.main_name == user.name) | (Schedule.sub_name == user.name))
        )
    ).all()

    now = datetime.now()
    for s in my_schedules:
        chk = get_or_create_checkin(session, s.id, user.name)
        if chk.wake_time is None:
            chk.wake_time = now
            chk.updated_at = datetime.utcnow()
            session.add(chk)
    session.commit()
    return RedirectResponse("/my", status_code=302)

@app.post("/check/depart")
def check_depart(
    request: Request,
    session: Session = Depends(get_session),
    schedule_id: int = Form(...),
):
    user = get_current_user(request, session)
    if not user or user.is_admin:
        return RedirectResponse("/login", status_code=302)

    s0 = session.get(Schedule, schedule_id)
    if not s0:
        return RedirectResponse("/my", status_code=302)

    from datetime import datetime
    now = datetime.now()

    # ✅ 같은 날 + 같은 웨딩홀(장소) 일정은 출발 1번으로 묶어서 처리
    day = s0.wedding_date

    my_same_venue_schedules = session.exec(
        select(Schedule).where(
            (Schedule.wedding_date == day)
            & (Schedule.venue == s0.venue)
            & ((Schedule.main_name == user.name) | (Schedule.sub_name == user.name))
        )
    ).all()

    for s in my_same_venue_schedules:
        chk = get_or_create_checkin(session, s.id, user.name)
        # 하루 기상은 v3.6에서 일괄 처리되지만, 혹시 비어있으면 안전하게 채움
        if chk.wake_time is None:
            chk.wake_time = now
        if chk.depart_time is None:
            chk.depart_time = now
            chk.updated_at = datetime.utcnow()
            session.add(chk)

    session.commit()
    return RedirectResponse("/my", status_code=302)


@app.post("/check/arrive")
async def check_arrive(
    request: Request,
    session: Session = Depends(get_session),
    schedule_id: int = Form(...),
    photo: UploadFile = File(...),
):
    user = get_current_user(request, session)
    if not user or user.is_admin:
        return RedirectResponse("/login", status_code=302)

    if not photo or not photo.filename:
        return RedirectResponse("/my", status_code=302)

    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        return RedirectResponse("/my", status_code=302)

    s0 = session.get(Schedule, schedule_id)
    if not s0:
        return RedirectResponse("/my", status_code=302)

    # 저장 경로
    upload_root = UPLOAD_DIR
    os.makedirs(upload_root, exist_ok=True)
    cleanup_uploads(ttl_hours=6)
    safe_name = f"{schedule_id}_{user.id}_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(upload_root, safe_name)

    contents = await photo.read()
    with open(save_path, "wb") as f:
        f.write(contents)

    from datetime import datetime
    now_local = datetime.now()

    # ✅ 같은 날 + 같은 웨딩홀(장소) 일정은 도착 1번으로 처리
    day = s0.wedding_date
    venue_key = (s0.venue or "").strip()

    my_same_venue_schedules = session.exec(
        select(Schedule).where(
            (Schedule.wedding_date == day)
            & (Schedule.venue == s0.venue)
            & ((Schedule.main_name == user.name) | (Schedule.sub_name == user.name))
        )
    ).all()

    for s in my_same_venue_schedules:
        chk = get_or_create_checkin(session, s.id, user.name)
        # 기상/출발이 비어 있으면 기본값으로 채움(흐름 보호)
        if chk.wake_time is None:
            chk.wake_time = now_local
        if chk.depart_time is None:
            chk.depart_time = now_local
        if chk.arrive_time is None:
            chk.arrive_time = now_local
        # 사진은 같은 장소 묶음에 공통 적용
        chk.arrive_photo_path = f"/uploads/{safe_name}"
        chk.updated_at = datetime.utcnow()
        session.add(chk)

    session.commit()
    return RedirectResponse("/my", status_code=302)

    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        return RedirectResponse("/my", status_code=302)

    chk = get_or_create_checkin(session, schedule_id, user.name)
    from datetime import datetime
    if chk.wake_time is None:
        chk.wake_time = datetime.now()
    if chk.depart_time is None:
        chk.depart_time = datetime.now()

    upload_root = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_root, exist_ok=True)
    safe_name = f"{schedule_id}_{user.id}_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(upload_root, safe_name)

    contents = await photo.read()
    with open(save_path, "wb") as f:
        f.write(contents)

    chk.arrive_time = chk.arrive_time or datetime.now()
    chk.arrive_photo_path = f"/uploads/{safe_name}"
    chk.updated_at = datetime.utcnow()
    session.add(chk)
    session.commit()

    return RedirectResponse("/my", status_code=302)



@app.get("/my", response_class=HTMLResponse)
def my_schedule(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user:
        return RedirectResponse("/login", status_code=302)

    cleanup_uploads(ttl_hours=6)

    today = date.today()
    start, end = week_range(today)

    q = select(Schedule).where(
        (Schedule.wedding_date >= start) & (Schedule.wedding_date <= end) &
        ((Schedule.main_name == user.name) | (Schedule.sub_name == user.name))
    ).order_by(Schedule.wedding_date, Schedule.wedding_time)

    schedules = session.exec(q).all()

    # 체크인(기상/출발/도착) 상태 불러오기
    schedule_ids = [s.id for s in schedules]
    if schedule_ids:
        chks = session.exec(select(Checkin).where(
            (Checkin.photographer_name == user.name) & (Checkin.schedule_id.in_(schedule_ids))
        )).all()
    else:
        chks = []
    checkins_map = {c.schedule_id: c for c in chks}

    day_woke_set = {c.wake_time.date() for c in chks if c.wake_time}
    arrived_venue_set = set()
    for s in schedules:
        chk = checkins_map.get(s.id)
        if chk and chk.arrive_time:
            arrived_venue_set.add((s.wedding_date, (s.venue or '').strip()))
    departed_venue_set = set()
    for s in schedules:
        chk = checkins_map.get(s.id)
        if chk and chk.depart_time:
            departed_venue_set.add((s.wedding_date, (s.venue or '').strip()))

    return templates.TemplateResponse("my.html", {
        "request": request,
        "user": user,
        "start": start,
        "end": end,
        "schedules": schedules,
        "checkins_map": checkins_map,
        "day_woke_set": day_woke_set,
        "arrived_venue_set": arrived_venue_set,
        "departed_venue_set": departed_venue_set
    })


@app.get("/health")
def health():
    # 헬스체크용(업타임로봇/ngrok/로드밸런서 등에서 주기적으로 호출)
    return {"ok": True}

@app.get("/api/keepalive_needed")
def keepalive_needed(session: Session = Depends(get_session)):
    # 오늘(한국시간) 예식이 있는 날 + 06:00~17:00(KST) 시간대에만 keepalive 필요
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now_kst.date()
    in_window = (now_kst.time() >= time(6, 0)) and (now_kst.time() <= time(17, 0))
    has_wedding = session.exec(select(Schedule).where(Schedule.wedding_date == today)).first() is not None
    return {
        "keepalive": bool(in_window and has_wedding),
        "in_window": bool(in_window),
        "has_wedding": bool(has_wedding),
        "today": str(today),
        "now_kst": now_kst.isoformat(),
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse("/my", status_code=302)
    return templates.TemplateResponse("admin_home.html", {"request": request, "user": user})

@app.get("/admin/alerts", response_class=HTMLResponse)
def admin_alerts(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    from datetime import datetime, timedelta, date
    now = datetime.now()
    # 오늘~7일 내 스케줄 모니터링
    start = date.fromordinal(now.date().toordinal() - 0)
    end = date.fromordinal(now.date().toordinal() + 7)

    schedules = session.exec(
        select(Schedule).where((Schedule.wedding_date >= start) & (Schedule.wedding_date <= end))
        .order_by(Schedule.wedding_date, Schedule.wedding_time)
    ).all()

    rows = []
    for s in schedules:
        for role, name in [("메인", s.main_name), ("서브", s.sub_name)]:
            if not name:
                continue
            p = session.exec(select(Photographer).where(Photographer.name == name)).first()
            p_addr = (p.address or "") if p else ""
            travel_mins = get_cached_route_minutes(session, s, name, p_addr)

            deadlines = compute_deadlines(s, travel_mins)
            arrival_target_dt = deadlines["arrival_target_dt"]
            wake_deadline = deadlines["wake_deadline"]
            depart_deadline = deadlines["depart_deadline"]

            chk = session.exec(select(Checkin).where((Checkin.schedule_id == s.id) & (Checkin.photographer_name == name))).first()

            # 상태 판단
            wake_ok = bool(chk and chk.wake_time)
            depart_ok = bool(chk and chk.depart_time)
            arrive_ok = bool(chk and chk.arrive_time)

            # 같은 날+같은 장소 묶음 도착/출발 처리된 경우도 도착/출발 OK로 간주
            if not arrive_ok:
                other_arr = session.exec(
                    select(Checkin, Schedule).join(Schedule, Schedule.id == Checkin.schedule_id).where(
                        (Checkin.photographer_name == name)
                        & (Schedule.wedding_date == s.wedding_date)
                        & (Schedule.venue == s.venue)
                        & (Checkin.arrive_time.is_not(None))
                    )
                ).first()
                if other_arr:
                    arrive_ok = True

            if not depart_ok:
                other_dep = session.exec(
                    select(Checkin, Schedule).join(Schedule, Schedule.id == Checkin.schedule_id).where(
                        (Checkin.photographer_name == name)
                        & (Schedule.wedding_date == s.wedding_date)
                        & (Schedule.venue == s.venue)
                        & (Checkin.depart_time.is_not(None))
                    )
                ).first()
                if other_dep:
                    depart_ok = True

            # 알림 플래그 (지금 시각 기준)
            wake_overdue = (wake_deadline is not None) and (now >= wake_deadline) and (not wake_ok)
            depart_overdue = (depart_deadline is not None) and (now >= depart_deadline) and (not depart_ok)
            arrive_overdue = (arrival_target_dt is not None) and (now >= arrival_target_dt) and (not arrive_ok)

            rows.append({
                "schedule": s,
                "role": role,
                "name": name,
                "venue_address": s.venue_address,
                "arrival_target_dt": arrival_target_dt,
                "wake_deadline": wake_deadline,
                "depart_deadline": depart_deadline,
                "travel_mins": travel_mins,
                "wake_ok": wake_ok,
                "depart_ok": depart_ok,
                "arrive_ok": arrive_ok,
                "wake_overdue": wake_overdue,
                "depart_overdue": depart_overdue,
                "arrive_overdue": arrive_overdue,
            })

    # 알림만 보기 (쿼리 ?only=1)
    only = request.query_params.get("only") == "1"
    if only:
        rows = [r for r in rows if (r["wake_overdue"] or r["depart_overdue"] or r["arrive_overdue"])]

    return templates.TemplateResponse("admin_alerts.html", {"request": request, "user": user, "rows": rows, "only": only})



@app.get("/admin/alerts/feed")
def admin_alerts_feed(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return {"ok": False}

    from datetime import datetime, date
    now = datetime.now()
    start = now.date()
    end = date.fromordinal(now.date().toordinal() + 7)

    schedules = session.exec(
        select(Schedule).where((Schedule.wedding_date >= start) & (Schedule.wedding_date <= end))
        .order_by(Schedule.wedding_date, Schedule.wedding_time)
    ).all()

    alerts = []
    for s in schedules:
        for role, name in [("메인", s.main_name), ("서브", s.sub_name)]:
            if not name:
                continue
            p = session.exec(select(Photographer).where(Photographer.name == name)).first()
            p_addr = (p.address or "") if p else ""
            travel_mins = get_cached_route_minutes(session, s, name, p_addr)
            d = compute_deadlines(s, travel_mins)

            arrival_target_dt = d["arrival_target_dt"]
            wake_deadline = d["wake_deadline"]
            depart_deadline = d["depart_deadline"]

            chk = session.exec(select(Checkin).where((Checkin.schedule_id == s.id) & (Checkin.photographer_name == name))).first()
            wake_ok = bool(chk and chk.wake_time)
            depart_ok = bool(chk and chk.depart_time)
            arrive_ok = bool(chk and chk.arrive_time)

            # grouped by same day+venue
            if not arrive_ok:
                other_arr = session.exec(
                    select(Checkin, Schedule).join(Schedule, Schedule.id == Checkin.schedule_id).where(
                        (Checkin.photographer_name == name)
                        & (Schedule.wedding_date == s.wedding_date)
                        & (Schedule.venue == s.venue)
                        & (Checkin.arrive_time.is_not(None))
                    )
                ).first()
                if other_arr:
                    arrive_ok = True

            if not depart_ok:
                other_dep = session.exec(
                    select(Checkin, Schedule).join(Schedule, Schedule.id == Checkin.schedule_id).where(
                        (Checkin.photographer_name == name)
                        & (Schedule.wedding_date == s.wedding_date)
                        & (Schedule.venue == s.venue)
                        & (Checkin.depart_time.is_not(None))
                    )
                ).first()
                if other_dep:
                    depart_ok = True

            wake_overdue = (wake_deadline is not None) and (now >= wake_deadline) and (not wake_ok)
            depart_overdue = (depart_deadline is not None) and (now >= depart_deadline) and (not depart_ok)
            arrive_overdue = (arrival_target_dt is not None) and (now >= arrival_target_dt) and (not arrive_ok)

            if wake_overdue or depart_overdue or arrive_overdue:
                key = f"{s.id}:{name}:{role}"
                alerts.append({
                    "key": key,
                    "schedule_id": s.id,
                    "date": str(s.wedding_date),
                    "venue": s.venue,
                    "wedding_time": s.wedding_time.strftime("%H:%M") if s.wedding_time else None,
                    "arrival_target": arrival_target_dt.strftime("%H:%M") if arrival_target_dt else None,
                    "name": name,
                    "role": role,
                    "wake_overdue": wake_overdue,
                    "depart_overdue": depart_overdue,
                    "arrive_overdue": arrive_overdue,
                    "travel_mins": travel_mins,
                })

    return {"ok": True, "now": now.isoformat(), "count": len(alerts), "alerts": alerts}

@app.get("/admin/photos", response_class=HTMLResponse)
def admin_photos(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    # 도착 사진이 있는 체크인만
    q = select(Checkin, Schedule).join(Schedule, Schedule.id == Checkin.schedule_id).where(Checkin.arrive_photo_path.is_not(None)).order_by(Schedule.wedding_date.desc())
    items = session.exec(q).all()

    return templates.TemplateResponse("admin_photos.html", {"request": request, "user": user, "items": items})


@app.get("/admin/photographers", response_class=HTMLResponse)
def admin_photographers(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    photographers = session.exec(select(Photographer).where(Photographer.is_admin == False).order_by(Photographer.id)).all()
    return templates.TemplateResponse("admin_photographers.html", {"request": request, "user": user, "photographers": photographers})


@app.get("/admin/wedding_halls", response_class=HTMLResponse)
def admin_wedding_halls(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    halls = session.exec(select(WeddingHall).order_by(WeddingHall.name)).all()
    return templates.TemplateResponse("admin_wedding_halls.html", {"request": request, "user": user, "halls": halls})


@app.post("/admin/wedding_halls")
async def admin_wedding_halls_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    session: Session = Depends(get_session),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    name = (name or "").strip()
    address = (address or "").strip()
    if name:
        hall = upsert_wedding_hall(session, name, address or None)
        if hall and hall.address:
            propagate_hall_address(session, hall.name, hall.address)
    return RedirectResponse("/admin/wedding_halls", status_code=302)


@app.get("/admin/wedding_halls/{hall_id}/edit", response_class=HTMLResponse)
def admin_wedding_halls_edit(request: Request, hall_id: int, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    hall = session.get(WeddingHall, hall_id)
    if not hall:
        return RedirectResponse("/admin/wedding_halls", status_code=302)
    return templates.TemplateResponse("admin_wedding_hall_edit.html", {"request": request, "user": user, "hall": hall})


@app.post("/admin/wedding_halls/{hall_id}/edit")
async def admin_wedding_halls_edit_save(
    request: Request,
    hall_id: int,
    name: str = Form(...),
    address: str = Form(""),
    session: Session = Depends(get_session),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    hall = session.get(WeddingHall, hall_id)
    if not hall:
        return RedirectResponse("/admin/wedding_halls", status_code=302)
    old_name = hall.name
    name = (name or "").strip()
    address = (address or "").strip()
    if name:
        hall.name = name
    hall.address = address or None
    session.add(hall)
    session.commit()
    session.refresh(hall)

    # If name changed, update schedules key as well
    if old_name != hall.name:
        schedules = session.exec(select(Schedule).where(Schedule.venue == old_name)).all()
        for s in schedules:
            s.venue = hall.name
            session.add(s)
        session.commit()

    if hall.address:
        propagate_hall_address(session, hall.name, hall.address)
    return RedirectResponse("/admin/wedding_halls", status_code=302)


@app.post("/admin/wedding_halls/{hall_id}/delete")
async def admin_wedding_halls_delete(
    request: Request,
    hall_id: int,
    session: Session = Depends(get_session),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    hall = session.get(WeddingHall, hall_id)
    if hall:
        session.delete(hall)
        session.commit()
    return RedirectResponse("/admin/wedding_halls", status_code=302)



@app.post("/admin/photographers/import")
async def admin_photographers_import(
    request: Request,
    session: Session = Depends(get_session),
    file: UploadFile = File(...),
):
    import uuid
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    os.makedirs("uploads", exist_ok=True)
    tmp_path = os.path.join("uploads", f"photographers_{uuid.uuid4().hex}_{file.filename}")
    content = await file.read()
    with open(tmp_path, "wb") as f:
        f.write(content)

    rows = load_photographers_from_excel(tmp_path)
    imported = 0
    updated = 0

    for r in rows:
        name = r.get("name")
        if not name:
            continue
        p = session.exec(select(Photographer).where(Photographer.name == name)).first()
        if p:
            # update
            p.phone = r.get("phone") or p.phone
            p.address = r.get("address") or p.address
            p.region = r.get("region") or p.region
            if r.get("has_car") is not None:
                p.has_car = r.get("has_car")
            if r.get("start_date") is not None:
                p.start_date = r.get("start_date")
            p.gender = r.get("gender") or p.gender
            p.role = r.get("role") or p.role
            updated += 1
        else:
            # create (default password 1234)
            base_username = name.strip()
            username = base_username
            suffix = 1
            while session.exec(select(Photographer).where(Photographer.username == username)).first():
                suffix += 1
                username = f"{base_username}{suffix}"

            p = Photographer(
                name=name.strip(),
                username=username,
                password_hash=hash_password("1234"),
                phone=r.get("phone"),
                address=r.get("address"),
                region=r.get("region"),
                has_car=r.get("has_car"),
                start_date=r.get("start_date"),
                status="활성",
                memo=None,
                gender=r.get("gender"),
                role=r.get("role"),
                is_admin=False,
            )
            session.add(p)
            imported += 1

    session.commit()
    return RedirectResponse(f"/admin/photographers?imported={imported}&updated={updated}", status_code=302)


@app.post("/admin/photographers/create")
def admin_create_photographer(
    request: Request,
    session: Session = Depends(get_session),
    name: str = Form(...),
    phone: str = Form(""),
    gender: str = Form(""),
    role: str = Form(""),
    address: str = Form(""),
    region: str = Form(""),
    has_car: str = Form(""),
    start_date: str = Form(""),
    status: str = Form("활성"),
    memo: str = Form(""),
    password: str = Form("1234"),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    has_car_bool = True if has_car == "보유" else (False if has_car == "미보유" else None)

    sd = None
    if start_date:
        try:
            sd = date.fromisoformat(start_date)
        except Exception:
            sd = None

    # username 기본: 이름(중복이면 숫자 붙임)
    base_username = name.strip()
    username = base_username
    suffix = 1
    while session.exec(select(Photographer).where(Photographer.username == username)).first():
        suffix += 1
        username = f"{base_username}{suffix}"

    p = Photographer(
        name=name.strip(),
        username=username,
        password_hash=hash_password(password),
        phone=phone or None,
        gender=gender or None,
        role=role or None,
        address=address or None,
        region=region or None,
        has_car=has_car_bool,
        start_date=sd,
        status=status,
        memo=memo or None,
        is_admin=False,
    )
    session.add(p)
    session.commit()
    return RedirectResponse("/admin/photographers", status_code=302)

@app.get("/admin/photographers/{pid}/edit", response_class=HTMLResponse)
def admin_edit_photographer_page(pid: int, request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    p = session.get(Photographer, pid)
    if not p or p.is_admin:
        return RedirectResponse("/admin/photographers", status_code=302)
    return templates.TemplateResponse("admin_photographer_edit.html", {"request": request, "user": user, "p": p, "error": None})

@app.post("/admin/photographers/{pid}/edit")
def admin_edit_photographer_save(
    pid: int,
    request: Request,
    session: Session = Depends(get_session),
    name: str = Form(...),
    phone: str = Form(""),
    gender: str = Form(""),
    role: str = Form(""),
    address: str = Form(""),
    region: str = Form(""),
    has_car: str = Form(""),
    start_date: str = Form(""),
    status: str = Form("활성"),
    memo: str = Form(""),
    new_password: str = Form(""),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    p = session.get(Photographer, pid)
    if not p or p.is_admin:
        return RedirectResponse("/admin/photographers", status_code=302)

    old_name = p.name

    p.name = name.strip()
    p.phone = phone.strip() or None
    p.address = address.strip() or None
    p.region = region.strip() or None

    if has_car == "보유":
        p.has_car = True
    elif has_car == "미보유":
        p.has_car = False
    else:
        p.has_car = None

    sd = None
    if start_date.strip():
        try:
            from datetime import date
            sd = date.fromisoformat(start_date.strip())
        except Exception:
            sd = None
    p.start_date = sd

    p.status = status
    p.memo = memo.strip() or None

    if new_password and new_password.strip():
        p.password_hash = hash_password(new_password.strip())

    session.add(p)
    session.commit()

    # 이름 변경 시 스케줄(main/sub)도 같이 변경
    if old_name != p.name:
        mains = session.exec(select(Schedule).where(Schedule.main_name == old_name)).all()
        for s in mains:
            s.main_name = p.name
            session.add(s)
        subs = session.exec(select(Schedule).where(Schedule.sub_name == old_name)).all()
        for s in subs:
            s.sub_name = p.name
            session.add(s)
        session.commit()

    return RedirectResponse("/admin/photographers", status_code=302)


@app.post("/admin/photographers/{pid}/delete")
def admin_delete_photographer(pid: int, request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    p = session.get(Photographer, pid)
    if not p or p.is_admin:
        return RedirectResponse("/admin/photographers", status_code=302)

    # 안전장치: 스케줄에 연결된 작가면 삭제 막기(원하면 강제 삭제로 바꿀 수 있음)
    used = session.exec(select(Schedule).where((Schedule.main_name == p.name) | (Schedule.sub_name == p.name))).first()
    if used:
        # 간단히 목록으로 되돌리기(추후 에러 메시지 페이지로 개선 가능)
        return RedirectResponse("/admin/photographers", status_code=302)

    # 체크인도 삭제
    chks = session.exec(select(Checkin).where(Checkin.photographer_name == p.name)).all()
    for c in chks:
        session.delete(c)

    session.delete(p)
    session.commit()
    return RedirectResponse("/admin/photographers", status_code=302)



    old_name = p.name

    p.name = name.strip()
    p.phone = phone or None
    p.address = address or None
    p.region = region or None
    p.has_car = True if has_car == "보유" else (False if has_car == "미보유" else None)

    sd = None
    if start_date:
        try:
            from datetime import date
            sd = date.fromisoformat(start_date)
        except Exception:
            sd = None
    p.start_date = sd
    p.status = status
    p.memo = memo or None

    # 비번 변경(선택)
    if new_password and new_password.strip():
        p.password_hash = hash_password(new_password.strip())

    session.add(p)
    session.commit()

    # 중요: 이름이 바뀌면 스케줄의 main/sub 문자열도 같이 변경
    if old_name != p.name:
        # main_name 변경
        mains = session.exec(select(Schedule).where(Schedule.main_name == old_name)).all()
        for s in mains:
            s.main_name = p.name
            session.add(s)
        subs = session.exec(select(Schedule).where(Schedule.sub_name == old_name)).all()
        for s in subs:
            s.sub_name = p.name
            session.add(s)
        session.commit()

    return RedirectResponse("/admin/photographers", status_code=302)



@app.get("/admin/schedules", response_class=HTMLResponse)
def admin_schedules(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    schedules = session.exec(select(Schedule).order_by(Schedule.wedding_date.desc(), Schedule.wedding_time)).all()
    return templates.TemplateResponse("admin_schedules.html", {"request": request, "user": user, "schedules": schedules})


@app.post("/admin/schedules/bulk_delete")
async def admin_schedules_bulk_delete(
    request: Request,
    schedule_ids: list[int] = Form(default=[]),
    session: Session = Depends(get_session),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    ids: list[int] = []
    for x in schedule_ids:
        try:
            ids.append(int(x))
        except Exception:
            pass
    if not ids:
        return RedirectResponse("/admin/schedules", status_code=302)

    for sid in ids:
        # Checkins + arrive photo files
        checkins = session.exec(select(Checkin).where(Checkin.schedule_id == sid)).all()
        for c in checkins:
            if c.arrive_photo_path:
                try:
                    if os.path.exists(c.arrive_photo_path):
                        os.remove(c.arrive_photo_path)
                except Exception:
                    pass
            session.delete(c)

        # route estimates
        try:
            routes = session.exec(select(RouteEstimate).where(RouteEstimate.schedule_id == sid)).all()
            for r in routes:
                session.delete(r)
        except Exception:
            pass

        s = session.get(Schedule, sid)
        if s:
            session.delete(s)

    session.commit()
    return RedirectResponse("/admin/schedules", status_code=302)

@app.get("/admin/schedules/{sid}/edit", response_class=HTMLResponse)
def admin_edit_schedule_page(sid: int, request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)
    s = session.get(Schedule, sid)
    if not s:
        return RedirectResponse("/admin/schedules", status_code=302)
    venues = session.exec(select(Venue).order_by(Venue.name)).all()
    return templates.TemplateResponse("admin_schedule_edit.html", {"request": request, "user": user, "s": s, "venues": venues, "error": None})

@app.post("/admin/schedules/{sid}/edit")
def admin_edit_schedule_save(
    sid: int,
    request: Request,
    session: Session = Depends(get_session),
    wedding_date: str = Form(...),
    wedding_time: str = Form(""),
    venue: str = Form(...),
    venue_address: str = Form(""),
    shoot_start_time: str = Form(""),
    arrival_target_time: str = Form(""),
    couple: str = Form(""),
    main_name: str = Form(""),
    sub_name: str = Form(""),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    s = session.get(Schedule, sid)
    if not s:
        return RedirectResponse("/admin/schedules", status_code=302)

    from datetime import date, datetime, timedelta

    def render_error(msg: str):
        venues = session.exec(select(Venue).order_by(Venue.name)).all()
        return templates.TemplateResponse(
            "admin_schedule_edit.html",
            {"request": request, "user": user, "s": s, "venues": venues, "error": msg},
            status_code=400,
        )

    # parse date
    try:
        s.wedding_date = date.fromisoformat(wedding_date)
    except Exception:
        return render_error("날짜 형식이 올바르지 않습니다(YYYY-MM-DD).")

    # parse wedding time
    if wedding_time.strip():
        try:
            s.wedding_time = datetime.strptime(wedding_time.strip(), "%H:%M").time()
        except Exception:
            return render_error("시간 형식이 올바르지 않습니다(HH:MM).")
    else:
        s.wedding_time = None

    # 촬영시작시간/도착목표시간 자동 규칙
    shoot_t = None
    if shoot_start_time.strip():
        try:
            shoot_t = datetime.strptime(shoot_start_time.strip(), "%H:%M").time()
        except Exception:
            return render_error("촬영시작시간 형식이 올바르지 않습니다(HH:MM).")
    else:
        # 비워두면 예식시간 1시간 전
        if s.wedding_time:
            shoot_t = (datetime.combine(s.wedding_date, s.wedding_time) - timedelta(hours=1)).time()
    s.shoot_start_time = shoot_t

    if arrival_target_time.strip():
        try:
            s.arrival_target_time = datetime.strptime(arrival_target_time.strip(), "%H:%M").time()
        except Exception:
            return render_error("도착목표시간 형식이 올바르지 않습니다(HH:MM).")
    else:
        # 비워두면 촬영시작 30분 전
        if s.shoot_start_time:
            s.arrival_target_time = (datetime.combine(s.wedding_date, s.shoot_start_time) - timedelta(minutes=30)).time()
        else:
            s.arrival_target_time = None

    s.venue = venue.strip()
    s.venue_address = venue_address.strip() or None
    s.couple = couple.strip() or None
    s.main_name = main_name.strip() or None
    s.sub_name = sub_name.strip() or None
    s.raw_photographers = " ".join([x for x in [s.main_name, s.sub_name] if x])

    # 웨딩홀 주소는 1회 입력 후 재사용: 입력이 비어있으면 Venue 테이블에서 찾아 채움
    v = session.exec(select(Venue).where(Venue.name == s.venue)).first()
    if s.venue_address:
        if v:
            v.address = s.venue_address
            v.updated_at = datetime.utcnow()
        else:
            v = Venue(name=s.venue, address=s.venue_address)
        session.add(v)
    else:
        if v and v.address:
            s.venue_address = v.address

    session.add(s)
    session.commit()

    return RedirectResponse(f"/admin/schedules?updated={sid}", status_code=302)


@app.post("/admin/schedules/{sid}/delete")

def admin_delete_schedule(sid: int, request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    s = session.get(Schedule, sid)
    if not s:
        return RedirectResponse("/admin/schedules", status_code=302)

    # 관련 체크인도 같이 삭제
    chks = session.exec(select(Checkin).where(Checkin.schedule_id == sid)).all()
    for c in chks:
        session.delete(c)

    session.delete(s)
    session.commit()
    return RedirectResponse("/admin/schedules", status_code=302)



@app.post("/admin/schedules/import")
async def admin_import_schedules(
    request: Request,
    session: Session = Depends(get_session),
    file: UploadFile = File(...),
):
    user = get_current_user(request, session)
    if not user or not user.is_admin:
        return RedirectResponse("/login", status_code=302)

    # 업로드 파일 임시 저장
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
        contents = await file.read()
        tmp.write(contents)
        tmp_path = tmp.name

    rows = load_schedules_from_excel(tmp_path)
    os.unlink(tmp_path)

    # 작가 자동 생성(스케줄에 나온 이름 기반) — 비번 1234
    for r in rows:
        for nm in [r.get("main_name"), r.get("sub_name")]:
            if not nm:
                continue
            existing = session.exec(select(Photographer).where(Photographer.name == nm)).first()
            if not existing:
                # username 기본 = 이름
                username = nm
                suffix = 1
                while session.exec(select(Photographer).where(Photographer.username == username)).first():
                    suffix += 1
                    username = f"{nm}{suffix}"
                p = Photographer(
                    name=nm,
                    username=username,
                    password_hash=hash_password("1234"),
                    status="활성",
                    is_admin=False,
                )
                session.add(p)
    session.commit()

    # 스케줄 insert(중복 방지: 날짜+시간+홀+커플+메인+서브)
    inserted = 0
    for r in rows:
        exists = session.exec(
            select(Schedule).where(
                (Schedule.wedding_date == r["wedding_date"]) &
                (Schedule.wedding_time == r["wedding_time"]) &
                (Schedule.venue == r["venue"]) &
                (Schedule.couple == r["couple"]) &
                (Schedule.main_name == r["main_name"]) &
                (Schedule.sub_name == r["sub_name"])
            )
        ).first()
        if exists:
            continue
        s = Schedule(**r)
        session.add(s)
        inserted += 1
    session.commit()

    return RedirectResponse(f"/admin/schedules?imported={inserted}", status_code=302)
