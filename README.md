# 웨딩 촬영 스케줄 웹앱 (작가 로그인 → 이번주 내 스케줄)

## 기능 (MVP)
- 엑셀 업로드(import)로 스케줄 등록
- F열(촬영자) 규칙으로 메인/서브 자동 분리
  - 이름이 1개면 메인
  - 이름이 2개 이상이면 첫번째가 메인, 두번째가 서브
- 작가 계정 로그인 (아이디=이름 기본 생성)
- 로그인하면 이번주 '본인(메인 또는 서브)' 스케줄만 표시
- 관리자: 작가 목록/스케줄 목록 확인, 엑셀 업로드

## 엑셀 컬럼 매핑
- G열: 예식일 (날짜)
- J열: 웨딩홀
- H열: 예식 시간
- C열: 신랑/신부 성함
- F열: 촬영자 (예: "신원식" 또는 "신원식 · 김서브")

> 주의: 엑셀 파일의 첫 시트 기준으로 읽습니다.

## 실행 방법
```bash
python -m venv .venv
# mac/linux
source .venv/bin/activate
# windows
# .venv\Scripts\activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

브라우저에서 http://127.0.0.1:8000 접속

## 초기 관리자 계정
- 아이디: admin
- 비번: admin1234
- (첫 실행 시 자동 생성)
실서비스 전에는 반드시 변경하세요.


### 참고
- macOS Python 3.13에서 bcrypt 호환 문제를 피하기 위해 pbkdf2_sha256 해시를 사용합니다.


## 체크(기상/출발/도착) 기능(v3)
- 도착은 사진 첨부가 있어야 확정됩니다.
- 기존 app.db가 이미 있는 경우(이전 버전 실행), MVP 단계에서는 프로젝트 폴더에서 `rm app.db` 후 재실행이 가장 간단합니다.


## v3.4 변경사항
- 스케줄: 웨딩홀 주소(venue_address), 도착목표시간(arrival_target_time) 필드 추가
- 관리자: 스케줄/작가 삭제 버튼 추가
- 기존 app.db가 있으면(이전 버전 실행), MVP 단계에서는 `rm app.db` 후 재실행을 권장합니다.


## v3.6 변경사항
- 작가 '기상'은 같은 날짜 기준 1번 누르면 해당 날짜의 모든 내 스케줄에 일괄 적용
- 같은 날 같은 웨딩홀(장소) 스케줄은 '도착(사진)' 1번으로 묶어서 처리


## v3.7 변경사항
- 같은 날 같은 웨딩홀(장소) 스케줄은 '출발'도 1번으로 묶어서 처리


## v3.9 변경사항
- 관리자 알림 모니터링 페이지(/admin/alerts): 기상/출발/도착 지연 상태를 표로 확인
- 관리자 도착 사진 확인 페이지(/admin/photos)
- 이동시간(분) 자동 추정(프로토타입): 작가 주소 + 웨딩홀 주소가 있으면 OSM(Nominatim)+OSRM로 계산 후 캐시
- 스케줄에 이동시간 기본값(travel_minutes_default) 추가(자동 계산 실패 시 수동 입력용)
- DB 스키마가 변경되었으니 기존 app.db가 있으면 `rm app.db` 후 재실행 권장


## v3.10 변경사항
- 관리자 페이지에서 지연 발생 시 자동 토스트 알림(60초 폴링, /admin/alerts/feed)


## Kakao 길찾기 설정(v3.11)
1) Kakao Developers에서 REST API 키 발급
2) 터미널에서 환경변수 설정 후 실행

macOS/Linux:
```bash
export KAKAO_REST_API_KEY="YOUR_KEY"
```
Windows PowerShell:
```powershell
setx KAKAO_REST_API_KEY "YOUR_KEY"
```

- 주소가 정확할수록 결과가 잘 나옵니다.
- 이동시간 계산이 실패하면 스케줄의 이동시간(분) 기본값(travel_minutes_default)을 수동으로 입력해도 됩니다.


## v3.12 변경사항
- 스케줄 업로드: 날짜 블록 형태(웨딩홀/시간/메인/서브) 엑셀도 자동 인식하여 입력
- 촬영시작시간 규칙: 비어있으면 예식시간-1시간, 도착목표시간은 촬영시작-30분 자동 계산
- 웨딩홀 주소 재사용: Venue 테이블 도입(한 번 입력하면 이후 같은 웨딩홀 선택 시 자동 채움)
- 스케줄 수정 화면에 촬영시작시간 입력 추가
- DB 스키마 변경: 기존 app.db가 있으면 `rm app.db` 권장


## v3.13 변경사항
- 작가 정보 엑셀 업로드 추가(예시 형식 지원)
- 작가 필드 추가: 성별(gender), 역할(role)
- DB 스키마 변경: 기존 app.db가 있으면 `rm app.db` 권장


## v3.13-fix
- main.py 문법 오류(SyntaxError: return outside function) 수정


## v3.14 변경사항
- 스케줄 업로드: 날짜 블록 엑셀을 header=None으로 읽어 날짜 행까지 정상 인식(기존 v3.13_fix에서 업로드 0건 문제 수정)


## v3.15 변경사항
- 작가 엑셀 업로드 오류 수정: time 모듈 import 누락(NameError) 해결


## v3.16 변경사항
- 스케줄 수정 오류 수정: Schedule 모델에 shoot_start_time 필드 추가
- DB 스키마 변경: 기존 app.db가 있으면 `rm app.db` 후 재시작 권장


## v3.17 변경사항
- 작가 엑셀 업로드 오류 재수정: main.py에 time import 보장
- v3.16의 shoot_start_time 필드 포함


## v3.18 변경사항
- 작가 엑셀 업로드 오류 수정: 파일명 생성에서 time 모듈 의존 제거(datetime.utcnow().timestamp() 사용)


## v3.19 변경사항
- 작가 엑셀 업로드 오류 수정: datetime import 누락(NameError) 해결


## v3.20 변경사항
- 작가 엑셀 업로드 오류 최종 수정: main.py 상단에 `from datetime import datetime` 강제 추가


## v3.21 변경사항
- 작가 엑셀 업로드 500 오류 수정: 파일명 생성에서 datetime/time 의존 제거(함수 내부 uuid 사용)


## v3.22 변경사항
- 작가 엑셀 업로드 500 오류 수정: admin_photographers_import에서 tmp_path 생성 로직을 uuid 기반으로 강제 교체( datetime/time 의존 완전 제거 )


## v3.23 변경사항
- 작가.xlsx 업로드 강화: 여러 시트 중 '이름' 컬럼 시트 자동 선택
- 시작일: 비어있음/년-월만/문자열/날짜 모두 처리
- 컬럼명이 조금 달라도 자동 매핑(전화번호/주소/차량보유 등)


## v3.25 변경사항
- 관리자 스케줄 목록에 체크박스 추가
- 선택삭제(일괄삭제) 기능 추가: 스케줄 + 체크인 + 도착사진 + 경로추정 데이터 함께 삭제


## v3.26 변경사항
- 웨딩홀 주소 '한번만 입력' 기능 구현 (웨딩홀명 기준 저장/재사용)
- 같은 웨딩홀 스케줄 편집 시 주소 자동 채움


## v3.28 변경사항
- 관리자 메뉴에 '웨딩홀 관리' 분리
- 웨딩홀 목록/추가/수정/삭제 페이지
- 웨딩홀 주소 저장 시 DB 전체 스케줄 주소 일괄 업데이트(같은 웨딩홀명 기준)


## v3.28.1 핫픽스
- 웨딩홀 관리 페이지 NameError(WeddingHall import) 수정


## v3.28.2 핫픽스
- 웨딩홀 관리 일괄반영 로직이 Schedule 필드명(venue/venue_address)과 불일치하던 오류 수정


## v3.28.3
- 도착사진 6시간 TTL 자동 삭제(cleanup_uploads)
- /health 헬스체크 엔드포인트 추가
- /api/keepalive_needed 추가: 오늘 예식 여부 + 06:00~17:00(KST) 체크
