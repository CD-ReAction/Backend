## TECH STACK
 
| 구분 | 기술 |
|------|------|
| 백엔드 | FastAPI, Python 3.12, SQLAlchemy, asyncpg |
| DB | PostgreSQL (Supabase) |
| 영상 저장 | AWS S3 |
| 백엔드 배포 | Railway |
| PWA | Vanilla JS, Netlify |
| 패키지 관리 | Poetry |

## PWA 실행 방법
### 배포된 서버 : `https://reaction-camera-connection.netlify.app`
1. `pwa/config.js` 에서 API_BASE_URL을 본인 로컬에서 돌리고 싶으면 **API_BASE_URL = "http://localhost:8000"** 으로 변경
2. `cd pwa && npx netlify deploy --prod`

### PWA 재배포 시점 (netlify)
pwa 파일이 변경되었을 때

```bash
cd ~/Desktop/Re-action/pwa
npx netlify deploy --prod
```

### 동작 흐름
```
1. 노트북에서 QR 생성 (index.html)
2. 핸드폰으로 QR 스캔 → 카메라 실행 (camera.html)
3. 영상 촬영 후 업로드 → S3 저장
4. 노트북에서 피드백 작성
5. face-analysis로 배우 자동 매핑 (개발 중)
6. 결과 확인 (영상 + 피드백 + 배우 매핑)
```

## 실행 방법 (default)
### 1. Docker Desktop 켜기

### 2. DB 시작
```bash
docker start reaction-db
```


### 3. Poetry 환경 진입
```bash
cd ~/Desktop/Re-action
source $(poetry env info --path)/bin/activate
```

### 4. 서버 실행
```bash
uvicorn app.main:app --reload
```
