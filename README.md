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
-- Actor 모델 변경
ALTER TABLE actors ADD COLUMN IF NOT EXISTS thumbnail_s3_key VARCHAR;
ALTER TABLE actors ALTER COLUMN name DROP NOT NULL;

-- 갤러리(다중 exemplar) 구조: face_embedding(FLOAT8[]) → face_embeddings(JSONB list[list[float]])
ALTER TABLE actors ADD COLUMN IF NOT EXISTS face_embeddings JSONB;
-- 기존 단일 임베딩이 있으면 [embedding] 형태로 wrap해서 옮김
UPDATE actors
SET face_embeddings = jsonb_build_array(to_jsonb(face_embedding))
WHERE face_embedding IS NOT NULL AND face_embeddings IS NULL;
ALTER TABLE actors DROP COLUMN IF EXISTS face_embedding;

-- Video ↔ Actor 다대다 링크
CREATE TABLE IF NOT EXISTS video_actors (
  video_actor_id  SERIAL PRIMARY KEY,
  video_id        INTEGER NOT NULL REFERENCES videos(video_id)  ON DELETE CASCADE,
  actor_id        INTEGER NOT NULL REFERENCES actors(actor_id)  ON DELETE CASCADE,
  is_new_in_video BOOLEAN DEFAULT FALSE NOT NULL,
  UNIQUE(video_id, actor_id)
);
```

## Face-Analyzer Contract (Phase 4 + 5)

### S3 폴더 구조 (Phase 5 — 프로젝트 단위로 묶음)
```
reaction-video.capstone/
└── {project_id}/
    └── {session_id}/
        ├── video.{webm|mp4|mov}     ← BE가 multipart PUT (한 세션당 1개)
        ├── thumb-0.jpg              ← analyzer가 PUT
        ├── thumb-1.jpg
        └── thumb-N.jpg
```

- **버킷**: `reaction-buck` (region `ap-northeast-2`)
- **영상 키**: `{project_id}/{session_id}/video.{ext}` — BE가 생성 (재업로드 시 덮어씀)
- **썸네일 키**: `{project_id}/{session_id}/thumb-{actor_index}.jpg` — analyzer가 PUT
  - analyzer는 BE 호출 payload의 `thumbnail_dir` 값을 그대로 prefix로 사용 (하드코딩 X)
  - 즉 `{thumbnail_dir}thumb-{idx}.jpg` 형태로 PUT
- analyzer 측 IAM user 필요 권한: 같은 prefix 하위 `*` write

### BE → Analyzer 호출 payload
```jsonc
POST {ANALYZER_URL}/analyze
Headers: X-Analyzer-Secret: <env>
{
  "video_id": 27,
  "session_id": 12,
  "s3_key": "7/12/video.webm",                      // 영상 key (= {pid}/{sid}/video.ext)
  "s3_url": "https://.../7/12/video.webm",
  "callback_url": "https://be.example/api/v1/videos/analysis-callback",
  "thumbnail_dir": "7/12/",                         // ⭐ Phase 5: analyzer는 이 안에만 PUT
  "known_actors": [                                 // 같은 project의 기존 actors
    {
      "actor_id": 1,
      "face_templates": [                           // ⭐ 다중 exemplar (max-of-N 매칭)
        [0.12, ...],
        [0.08, ...]
      ]
    },
    { "actor_id": 2, "face_templates": [[0.34, ...]] }
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
    {
      "actor_id": 2,
      "thumbnail_s3_key": "7/12/thumb-0.jpg",
      "similarity": 0.82,
      "new_exemplars": [                     // ⭐ 이번 영상에서 새로 본 각도 (BE가 갤러리에 append)
        [0.55, ...],
        [0.61, ...]
      ]
    }
  ],

  "new_candidates": [                        // 새 얼굴
    {
      "temp_index": 0,
      "thumbnail_s3_key": "7/12/thumb-1.jpg",
      "face_embeddings": [                   // ⭐ 다중 exemplar 갤러리 (5~10개 권장, 512차원)
        [0.56, ...],
        [0.49, ...]
      ]
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
1. **갤러리 누적 정책** — actor당 `face_embeddings`는 다중 exemplar 리스트 (list[list[float]]).
   - `new_candidates` 수신 시: `face_embeddings` 그대로 저장 (analyzer가 within-video diversity 보장)
   - `matched` 수신 시: `new_exemplars`를 기존 갤러리에 append
   - **Cap**: actor당 최대 20개 (`GALLERY_CAP_PER_ACTOR`). 초과 시 가장 오래된 것부터 drop
   - 썸네일은 첫 등장 값 고정 (BE는 matched에 대해 갱신 안 함)
2. **배우 작명** — `name = "배우 " || actor_id` (SERIAL 활용, INSERT 직후 UPDATE)
3. **매칭 책임 분리** — analyzer가 cosine similarity 계산 (각 actor 갤러리에 대해 max-of-N). BE는 결과만 받아 저장
4. **재분석 시** — `POST /sessions/{id}/video/analyze`는 기존 VideoActor 링크를 모두 삭제 후 콜백에서 재구성. 콜백 끝에 고아 actor(=어떤 영상에도 안 묶인 actor) 정리
5. **사용자 머지 (`POST /api/v1/actors/{a}/merge-into`)** — UNIQUE(video_id, actor_id) 충돌 시 A 링크 삭제, B의 `is_new_in_video`는 유지

