"""S3 MirrorBackend over boto3 (the optional ``[s3]`` extra; see ``core.mirror``).

boto3 is imported lazily, only when a real client is built. Bucket, region and key prefix come
from ``CULTURE_LIBRARY_S3_BUCKET`` / ``_REGION`` / ``_PREFIX``; credentials come from the standard
AWS environment variables. The backend only writes and reads metadata: it has no way to remove objects.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

SSE = "AES256"
_MISSING = {"404", "NoSuchKey", "NotFound"}


class S3Backend:
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self._region = region
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> Any:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "the S3 mirror backend needs boto3: install culture-library[s3]"
            ) from exc
        return boto3.client("s3", region_name=self._region) if self._region else boto3.client("s3")

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            ContentType=content_type,
            ServerSideEncryption=SSE,
            Metadata={"sha256": hashlib.sha256(data).hexdigest()},
        )

    def head(self, key: str) -> str | None:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # botocore.ClientError, without importing botocore
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in _MISSING:
                return None
            raise
        return (resp.get("Metadata") or {}).get("sha256")


def from_env(client: Any = None) -> S3Backend:
    bucket = os.environ.get("CULTURE_LIBRARY_S3_BUCKET", "")
    if not bucket:
        raise ValueError("CULTURE_LIBRARY_S3_BUCKET is not set")
    return S3Backend(
        bucket=bucket,
        prefix=os.environ.get("CULTURE_LIBRARY_S3_PREFIX", ""),
        region=os.environ.get("CULTURE_LIBRARY_S3_REGION") or None,
        client=client,
    )
