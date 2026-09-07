from __future__ import annotations

from pathlib import Path

from backend.config import FILES_DIR, settings


def _spaces_client():
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.spaces_region,
        endpoint_url=settings.spaces_endpoint,
        aws_access_key_id=settings.spaces_key,
        aws_secret_access_key=settings.spaces_secret,
    )


def save_file(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    if settings.uses_spaces:
        client = _spaces_client()
        client.put_object(
            Bucket=settings.spaces_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ACL="private",
        )
        return key
    path = FILES_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def read_file(key_or_path: str) -> bytes:
    if settings.uses_spaces:
        client = _spaces_client()
        response = client.get_object(Bucket=settings.spaces_bucket, Key=key_or_path)
        return response["Body"].read()
    path = Path(key_or_path)
    if not path.is_absolute():
        path = FILES_DIR / key_or_path
    return path.read_bytes()


def get_url(key_or_path: str, expires: int = 3600) -> str | None:
    if not settings.uses_spaces:
        return None
    client = _spaces_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.spaces_bucket, "Key": key_or_path},
        ExpiresIn=expires,
    )
