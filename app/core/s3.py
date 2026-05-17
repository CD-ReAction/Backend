import asyncio
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import settings

# Signature v4 + virtual-hosted-style → presigned PUT 호환성 안정적
s3_client = boto3.client(
    "s3",
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_REGION,
    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

_executor = ThreadPoolExecutor(max_workers=4)


async def _to_thread(fn, **kwargs):
    """boto3는 동기라 이벤트 루프 블록 방지용으로 executor에 던짐."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: fn(**kwargs))


def s3_object_url(key: str) -> str:
    return f"https://{settings.S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"


async def create_multipart_upload(key: str, content_type: str) -> str:
    """multipart 업로드 세션 시작. upload_id 반환."""
    try:
        res = await _to_thread(
            s3_client.create_multipart_upload,
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            ContentType=content_type,
        )
        return res["UploadId"]
    except ClientError as e:
        raise Exception(f"S3 multipart 시작 실패: {e}")


async def generate_part_upload_url(
    key: str, upload_id: str, part_number: int, expires_in: int = 3600
) -> str:
    """파트 업로드용 presigned PUT URL. 브라우저가 이 URL로 직접 PUT."""
    return await _to_thread(
        s3_client.generate_presigned_url,
        ClientMethod="upload_part",
        Params={
            "Bucket": settings.S3_BUCKET_NAME,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires_in,
    )


async def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> str:
    """모든 파트 조립. parts 형식: [{"PartNumber": int, "ETag": str}, ...]"""
    sorted_parts = sorted(parts, key=lambda p: p["PartNumber"])
    try:
        await _to_thread(
            s3_client.complete_multipart_upload,
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted_parts},
        )
        return s3_object_url(key)
    except ClientError as e:
        raise Exception(f"S3 multipart 완료 실패: {e}")


async def abort_multipart_upload(key: str, upload_id: str) -> None:
    """업로드 취소. S3에 남은 조각 정리."""
    try:
        await _to_thread(
            s3_client.abort_multipart_upload,
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
        )
    except ClientError as e:
        raise Exception(f"S3 multipart 취소 실패: {e}")


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """S3 presigned URL 생성 (영상 재생용)"""
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )
