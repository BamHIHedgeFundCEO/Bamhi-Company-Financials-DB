"""Cloudflare R2 上傳（S3 相容 API）。未設定憑證時回 None，main.py 直接串流檔案。"""
import os


def upload(key: str, data: bytes) -> str | None:
    account = os.environ.get("R2_ACCOUNT_ID")
    ak = os.environ.get("R2_ACCESS_KEY_ID")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    public_base = os.environ.get("R2_PUBLIC_BASE_URL")
    if not all([account, ak, sk, bucket, public_base]):
        return None

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        region_name="auto",
    )
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return f"{public_base}/{key}"
