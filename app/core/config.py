from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # JWT
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # AWS S3
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "ap-northeast-2"
    S3_BUCKET_NAME: str

    # File Upload (multipart)
    MAX_VIDEO_SIZE_MB: int = 8192          # 1시간 1080p@4Mbps ≈ 1.8GB, 여유 두고 8GB
    UPLOAD_PART_SIZE_MB: int = 16          # S3 최소 5MB. 16MB로 HTTP 오버헤드 감소
    UPLOAD_MAX_PARTS: int = 1024           # 16MB × 1024 = 16GB까지
    UPLOAD_URL_EXPIRES_SECONDS: int = 3600 # 파트별 presigned URL 유효시간

    # Face Analyzer service (RunPod Serverless)
    RUNPOD_ENDPOINT_URL: str | None = None
    RUNPOD_API_KEY: str | None = None
    FACE_ANALYZER_SECRET: str | None = None
    PUBLIC_API_BASE_URL: str | None = None

    ANALYZER_SECRET: str = ""
    CALLBACK_SECRET: str = ""
    ANTHROPIC_API_KEY: str

    class Config:
        env_file = ".env"


settings = Settings()
