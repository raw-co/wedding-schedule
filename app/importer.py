from __future__ import annotations
import re
from datetime import datetime, date, time
from typing import Optional, Tuple, List
import pandas as pd

SEPARATORS = ["·", "/", ",", "&", "및", " and "]

def _clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def parse_photographers(f_value: str) -> Tuple[Optional[str], Optional[str]]:
    """F열 규칙:
    - 이름이 1개면 메인
    - 2개 이상이면 첫번째=메인, 두번째=서브
    """
    raw = _clean_name(str(f_value) if f_value is not None else "")
    if not raw:
        return None, None

    # 우선 분리 가능한 구분자로 split
    parts = [raw]
    for sep in SEPARATORS:
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep)]
            break

    # 구분자가 없는데도 '두 명'이 공백으로만 들어오는 케이스 대응(주의: 단일 이름에 공백이 들어갈 수 있어 최소화)
    if len(parts) == 1:
        # 예: "홍길동 김철수" 같은 케이스만 아주 보수적으로 처리
        maybe = [p for p in raw.split(" ") if p.strip()]
        if len(maybe) >= 2 and all(len(x) >= 2 for x in maybe[:2]):
            parts = maybe

    parts = [_clean_name(p) for p in parts if _clean_name(p)]
    if len(parts) == 0:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]

def parse_date(value) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # 지원: '26.02.01' / '2026-02-01' / '2026.02.01'
    for fmt in ("%y.%m.%d", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def parse_time(value) -> Optional[time]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    s = str(value).strip()
    if not s:
        return None
    # 지원: '11:00' '08:50'
    try:
        return datetime.strptime(s, "%H:%M").time()
    except Exception:
        return None

def load_schedules_from_excel(file_path: str) -> List[dict]:
    """엑셀에서 스케줄 리스트를 로드합니다.

    지원 포맷
    1) 날짜 블록 포맷(예시 업로드 파일)
       - 날짜 행: "26년 02월 08일 (일)" 같은 문자열이 첫 컬럼에 존재
       - 그 아래 헤더: 웨딩홀 / 시간 / 촬영자(메인) / 촬영자(서브) / 촬영시간(선택)
    2) 기존 표준 포맷(열 기반: G=예식일, H=예식시간, J=웨딩홀, C=커플, F=촬영자)

    규칙
    - 촬영시간(촬영시작시간)이 비어있으면: 예식시간 - 1시간
    - 도착목표시간은: 촬영시작시간 - 30분
    """
    import pandas as pd
    from datetime import datetime, date, timedelta
    import re

    def parse_ymd_kr(s: str) -> date | None:
        if s is None:
            return None
        s = str(s)
        m = re.search(r"(\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일", s)
        if not m:
            return None
        yyyy = 2000 + int(m.group(1))
        mm = int(m.group(2))
        dd = int(m.group(3))
        try:
            return date(yyyy, mm, dd)
        except Exception:
            return None

    def parse_time_hhmm(s: str):
        s = (s or "").strip()
        if not s:
            return None
        # excel time objects can come as datetime.time in pandas; handle outside
        try:
            return datetime.strptime(s, "%H:%M").time()
        except Exception:
            return None

    def compute_shoot_and_arrival(wedding_time, shoot_start):
        if shoot_start is None and wedding_time is not None:
            shoot_start = (datetime.combine(date.today(), wedding_time) - timedelta(hours=1)).time()
        arrival_target = None
        if shoot_start is not None:
            arrival_target = (datetime.combine(date.today(), shoot_start) - timedelta(minutes=30)).time()
        return shoot_start, arrival_target

    # ------------- 먼저 날짜 블록 포맷을 시도 (header=None으로 첫 줄 날짜 유지) -------------
    try:
        df_raw = pd.read_excel(file_path, header=None)
    except Exception:
        df_raw = None

    rows: List[dict] = []

    if df_raw is not None and len(df_raw.columns) >= 2:
        # 첫 20행 안에 날짜 패턴이 있으면 날짜 블록 포맷으로 간주
        has_date = False
        for i in range(min(20, len(df_raw))):
            d = parse_ymd_kr(df_raw.iloc[i, 0])
            if d:
                has_date = True
                break

        if has_date:
            current_date = None
            for i in range(len(df_raw)):
                c0 = df_raw.iloc[i, 0] if len(df_raw.columns) > 0 else None

                d = parse_ymd_kr(c0)
                if d:
                    current_date = d
                    continue

                # 헤더 스킵(병합/줄바꿈 때문에 공백이 섞일 수 있어 contains로 처리)
                if isinstance(c0, str) and "웨딩홀" in c0:
                    continue

                if current_date is None:
                    continue

                venue_cell = df_raw.iloc[i, 0] if len(df_raw.columns) > 0 else None
                time_cell  = df_raw.iloc[i, 1] if len(df_raw.columns) > 1 else None
                main_cell  = df_raw.iloc[i, 2] if len(df_raw.columns) > 2 else None
                sub_cell   = df_raw.iloc[i, 3] if len(df_raw.columns) > 3 else None
                shoot_cell = df_raw.iloc[i, 4] if len(df_raw.columns) > 4 else None  # 촬영시간(선택)

                if pd.isna(venue_cell) and pd.isna(time_cell) and pd.isna(main_cell) and pd.isna(sub_cell):
                    continue
                if pd.isna(venue_cell):
                    continue

                venue_raw = str(venue_cell).strip()
                if not venue_raw:
                    continue

                venue_name = venue_raw.split("\n")[0].strip()
                venue_addr = None
                maddr = re.search(r"\((.+)\)", venue_raw.replace("\n", " "))
                if maddr:
                    venue_addr = maddr.group(1).strip()

                wedding_time = None
                couple = None
                if not pd.isna(time_cell):
                    parts = str(time_cell).splitlines()
                    if parts:
                        wedding_time = parse_time_hhmm(parts[0].strip())
                        if len(parts) > 1:
                            couple = " ".join([p.strip() for p in parts[1:] if p.strip()]) or None

                main_name = (str(main_cell).strip() if not pd.isna(main_cell) else "") or None
                sub_name  = (str(sub_cell).strip() if not pd.isna(sub_cell) else "") or None

                # 촬영시작시간: 엑셀에서 time 객체로 들어올 수 있음
                shoot_start = None
                if shoot_cell is not None and not pd.isna(shoot_cell):
                    # pandas가 time/datetime로 읽어올 수 있음
                    if hasattr(shoot_cell, "hour") and hasattr(shoot_cell, "minute"):
                        try:
                            shoot_start = shoot_cell
                            # datetime.time인 경우 그대로 OK
                            if hasattr(shoot_start, "time"):
                                shoot_start = shoot_start.time()
                        except Exception:
                            shoot_start = None
                    else:
                        shoot_start = parse_time_hhmm(str(shoot_cell))

                shoot_start, arrival_target = compute_shoot_and_arrival(wedding_time, shoot_start)

                raw_photographers = " ".join([x for x in [main_name, sub_name] if x]) if (main_name or sub_name) else ""
                rows.append({
                    "wedding_date": current_date,
                    "wedding_time": wedding_time,
                    "shoot_start_time": shoot_start,
                    "arrival_target_time": arrival_target,
                    "venue": venue_name,
                    "venue_address": venue_addr,
                    "couple": couple,
                    "main_name": main_name,
                    "sub_name": sub_name,
                    "raw_photographers": raw_photographers,
                })
            return rows

    # ------------- 기존 포맷(열 기반) -------------
    df = pd.read_excel(file_path)
    for _, row in df.iterrows():
        wedding_date = row.iloc[6] if len(row) > 6 else None  # G
        wedding_time = row.iloc[7] if len(row) > 7 else None  # H
        couple = row.iloc[2] if len(row) > 2 else None        # C
        photographers_raw = row.iloc[5] if len(row) > 5 else None  # F
        venue = row.iloc[9] if len(row) > 9 else None         # J

        if pd.isna(venue) or pd.isna(wedding_date):
            continue

        wdate = None
        if hasattr(wedding_date, "date"):
            try:
                wdate = wedding_date.date()
            except Exception:
                wdate = None
        if not wdate:
            try:
                wdate = pd.to_datetime(wedding_date).date()
            except Exception:
                wdate = None
        if not wdate:
            continue

        wtime = None
        if hasattr(wedding_time, "time"):
            try:
                wtime = wedding_time.time()
            except Exception:
                wtime = None
        if wtime is None and wedding_time is not None and not pd.isna(wedding_time):
            wtime = parse_time_hhmm(str(wedding_time))

        venue_name = str(venue).strip()
        couple_str = None if pd.isna(couple) else str(couple).strip()

        raw = "" if photographers_raw is None or pd.isna(photographers_raw) else str(photographers_raw).strip()
        names = [x for x in re.split(r"[\s,]+", raw) if x]
        main_name = names[0] if len(names) >= 1 else None
        sub_name = names[1] if len(names) >= 2 else None

        shoot_start, arrival_target = compute_shoot_and_arrival(wtime, None)

        rows.append({
            "wedding_date": wdate,
            "wedding_time": wtime,
            "shoot_start_time": shoot_start,
            "arrival_target_time": arrival_target,
            "venue": venue_name,
            "couple": couple_str or None,
            "main_name": main_name,
            "sub_name": sub_name,
            "raw_photographers": raw,
        })

    return rows


def load_photographers_from_excel(file_path: str) -> List[dict]:
    """작가 엑셀 업로드(사용자 제공 형식 포함) 지원.

    지원 컬럼(예시):
      촬영 | 성별 | 이름 | 시작일 | 연락처 | 거주지 | 주 촬영 지역 | 차량유무
    - 'no' 컬럼이 없어도 됨
    - 시작일이 비어있어도 됨
    - 시작일이 '년/월'만 있어도 됨(예: 17년11월, 2021-06 등)
    - 파일에 여러 시트가 있으면, '이름' 컬럼이 있는 시트를 우선 사용
    """
    import pandas as pd
    from datetime import date, datetime
    import re

    xls = pd.ExcelFile(file_path)
    df = None
    # 우선: '이름' 컬럼이 있는 시트 찾기
    for sh in xls.sheet_names:
        try:
            tmp = pd.read_excel(file_path, sheet_name=sh, header=0)
        except Exception:
            continue
        cols = [str(c).strip() for c in tmp.columns]
        if any(c == "이름" for c in cols):
            df = tmp
            break
    if df is None:
        # fallback: 첫 시트
        df = pd.read_excel(file_path, sheet_name=xls.sheet_names[0], header=0)

    df.columns = [str(c).strip() for c in df.columns]

    # 컬럼 별칭 매핑(조금 달라도 인식)
    alias = {
        "촬영": ["촬영", "역할", "메인/서브", "구분"],
        "성별": ["성별", "남/여", "성", "젠더"],
        "이름": ["이름", "성명", "작가명", "촬영자", "작가"],
        "시작일": ["시작일", "입사일", "시작", "근무시작", "근무 시작일"],
        "연락처": ["연락처", "전화", "전화번호", "휴대폰", "핸드폰"],
        "거주지": ["거주지", "주소", "사는곳", "거주", "거주 지역"],
        "주 촬영 지역": ["주 촬영 지역", "촬영지역", "주지역", "주 촬영", "주촬영지역"],
        "차량유무": ["차량유무", "차량", "차량보유", "차량 보유", "차량 여부"],
    }

    def find_col(key: str):
        for cand in alias.get(key, []):
            if cand in df.columns:
                return cand
        return None

    col_role = find_col("촬영")
    col_gender = find_col("성별")
    col_name = find_col("이름")
    col_start = find_col("시작일")
    col_phone = find_col("연락처")
    col_addr = find_col("거주지")
    col_region = find_col("주 촬영 지역")
    col_car = find_col("차량유무")

    def is_blank(v):
        if v is None:
            return True
        try:
            import pandas as pd
            if pd.isna(v):
                return True
        except Exception:
            pass
        s = str(v).strip()
        return s == "" or s.lower() == "nan"

    def parse_bool(v):
        if is_blank(v):
            return None
        s = str(v).strip().upper()
        if s in ["O", "Y", "YES", "TRUE", "보유", "있음", "유"]:
            return True
        if s in ["X", "N", "NO", "FALSE", "미보유", "없음", "무"]:
            return False
        return None

    def parse_phone(v):
        if is_blank(v):
            return None
        s0 = str(v).strip()
        s = s0.replace(" ", "").replace("-", "")
        if re.fullmatch(r"0\d{9,10}", s):
            if len(s) == 11:
                return f"{s[:3]}-{s[3:7]}-{s[7:]}"
            if len(s) == 10:
                return f"{s[:3]}-{s[3:6]}-{s[6:]}"
        return s0

    def parse_start(v):
        if is_blank(v):
            return None
        if isinstance(v, datetime):
            return v.date()
        try:
            if hasattr(v, "to_pydatetime"):
                return v.to_pydatetime().date()
        except Exception:
            pass
        if isinstance(v, date):
            return v
        s = str(v).strip()
        if not s:
            return None

        m = re.search(r"(\d{2,4})년\s*(\d{1,2})월", s)
        if m:
            yy = int(m.group(1))
            yyyy = 2000 + yy if yy < 100 else yy
            mm = int(m.group(2))
            try:
                return date(yyyy, mm, 1)
            except Exception:
                return None

        m = re.search(r"^(\d{4})[\-/\.](\d{1,2})$", s)
        if m:
            yyyy = int(m.group(1))
            mm = int(m.group(2))
            try:
                return date(yyyy, mm, 1)
            except Exception:
                return None

        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]:
            try:
                return datetime.strptime(s[:10], fmt).date()
            except Exception:
                pass
        return None

    rows: List[dict] = []
    if col_name is None:
        return rows

    for _, r in df.iterrows():
        name = str(r.get(col_name, "")).strip() if col_name else ""
        if is_blank(name):
            continue

        role = str(r.get(col_role, "")).strip() if col_role else ""
        gender = str(r.get(col_gender, "")).strip() if col_gender else ""
        phone = parse_phone(r.get(col_phone, "")) if col_phone else None
        address = str(r.get(col_addr, "")).strip() if col_addr else ""
        region = str(r.get(col_region, "")).strip() if col_region else ""
        has_car = parse_bool(r.get(col_car, "")) if col_car else None
        start_date = parse_start(r.get(col_start, None)) if col_start else None

        rows.append({
            "name": name,
            "role": None if is_blank(role) else role,
            "gender": None if is_blank(gender) else gender,
            "phone": phone,
            "address": None if is_blank(address) else address,
            "region": None if is_blank(region) else region,
            "has_car": has_car,
            "start_date": start_date,
        })
    return rows
