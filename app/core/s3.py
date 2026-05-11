import boto3
from botocore.exceptions import ClientError

from app.core.config import settings

s3_client = boto3.client(
    "s3",
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_REGION,
)


async def upload_video(file_bytes: bytes, key: str, content_type: str = "video/webm") -> str:
    """S3에 영상 업로드 후 URL 반환"""
    try:
        s3_client.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
        )
        url = f"https://{settings.S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
        return url
    except ClientError as e:
        raise Exception(f"S3 업로드 실패: {e}")


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """S3 presigned URL 생성 (영상 재생용)"""
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )