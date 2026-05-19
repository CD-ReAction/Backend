## ⚒️ TECH STACK
 
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
pwa 파일이 변경되었을 때 ⭐️

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

## S3 CORS 설정 (영상 업로드 필수)

PWA가 S3에 **직접** multipart PUT을 하려면 버킷 CORS에서 PUT 허용 + `ETag` 헤더 노출이 필요해요.
S3 콘솔 → 버킷 → Permissions → CORS 에 아래 정책 적용:

```json
[
  {
    "AllowedOrigins": [
      "https://reaction-camera-connection.netlify.app",
      "http://localhost:8000",
      "http://localhost:5173"
    ],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

> `ExposeHeaders: ["ETag"]` 빠지면 브라우저가 ETag를 못 읽어서 `complete`가 실패해요.

추가로 **버킷 Lifecycle 규칙**으로 미완료 multipart 업로드를 N일 후 자동 정리 권장
(콘솔 → Management → Lifecycle rules → "Delete incomplete multipart uploads after 7 days").

## DB 마이그레이션 (Phase 4 — 얼굴 인식 / 배우 매핑)

Supabase SQL Editor에서 **한 번만** 실행:

```sql
-- Actor 모델 변경 (운영 데이터 없음 가정 — face_embedding 타입 변경 위해 DROP + ADD)
ALTER TABLE actors DROP COLUMN IF EXISTS face_embedding;
ALTER TABLE actors ADD COLUMN face_embedding FLOAT8[];
ALTER TABLE actors ADD COLUMN IF NOT EXISTS thumbnail_s3_key VARCHAR;
ALTER TABLE actors ALTER COLUMN name DROP NOT NULL;

-- Video ↔ Actor 다대다 링크
CREATE TABLE IF NOT EXISTS video_actors (
  video_actor_id  SERIAL PRIMARY KEY,
  video_id        INTEGER NOT NULL REFERENCES videos(video_id)  ON DELETE CASCADE,
  actor_id        INTEGER NOT NULL REFERENCES actors(actor_id)  ON DELETE CASCADE,
  is_new_in_video BOOLEAN DEFAULT FALSE NOT NULL,
  UNIQUE(video_id, actor_id)
);
```

## Face-Analyzer Contract (Phase 4)

### S3
- **버킷**: `reaction-video.capstone`-> `reaction-buck`  (region `ap-northeast-2`)
- **썸네일 키 컨벤션**: `thumbnails/{video_id}/{actor_index}.jpg` (JPEG, 200~400px 정사각 권장)
- analyzer 측에서 boto3로 직접 PUT (IAM user에 `thumbnails/*` write 권한 필요)

### BE → Analyzer 호출 payload
```jsonc
POST {ANALYZER_URL}/analyze
Headers: X-Analyzer-Secret: <env>
{
  "video_id": 27,
  "session_id": 12,
  "s3_key": "videos/12/abc.webm",
  "s3_url": "https://.../videos/12/abc.webm",
  "callback_url": "https://be.example/api/v1/videos/analysis-callback",
  "known_actors": [                          // 같은 project의 기존 actors
    { "actor_id": 1, "embedding": [0.12, ...] },
    { "actor_id": 2, "embedding": [0.34, ...] }
  ]
}
```

### Analyzer → BE 콜백 payload
```jsonc
POST {BE}/api/v1/videos/analysis-callback
Headers: X-Analyzer-Secret: <env>
{
  "video_id": 27,
  "analysis_status": "done",                 // "done" | "failed" | "processing"

  "matched": [                               // similarity >= 임계값 (analyzer가 판정)
    { "actor_id": 2, "thumbnail_s3_key": "thumbnails/27/0.jpg", "similarity": 0.82 }
  ],

  "new_candidates": [                        // 새 얼굴
    {
      "temp_index": 0,
      "thumbnail_s3_key": "thumbnails/27/1.jpg",
      "face_embedding": [0.56, ...]          // 512차원 권장 (ArcFace 등)
    }
  ],

  "analysis_result": {                       // 자유 형식, BE는 placeholder만 치환
    "appearances": [
      { "person_id": "actor:2", "start": 0.5,  "end": 12.3 },
      { "person_id": "new:0",   "start": 14.0, "end": 22.0 }   // BE가 "actor:{id}"로 치환
    ]
  },

  "error_message": null                      // analysis_status="failed"일 때만
}
```

### 결정 사항 (Phase 4 설계)
1. **임베딩/썸네일 고정** — 같은 actor가 재등장해도 첫 등장 값 유지 (BE는 matched에 대해 갱신 안 함)
2. **배우 작명** — `name = "배우 " || actor_id` (SERIAL 활용, INSERT 직후 UPDATE)
3. **매칭 책임 분리** — analyzer가 cosine similarity 계산. BE는 결과만 받아 저장
4. **재분석 시** — `POST /sessions/{id}/video/analyze`는 기존 VideoActor 링크를 모두 삭제 후 콜백에서 재구성. 콜백 끝에 고아 actor(=어떤 영상에도 안 묶인 actor) 정리
5. **사용자 머지 (`POST /api/v1/actors/{a}/merge-into`)** — UNIQUE(video_id, actor_id) 충돌 시 A 링크 삭제, B의 `is_new_in_video`는 유지

