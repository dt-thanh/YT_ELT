"""
Raw object storage + manifest - TẦNG LƯU TRỮ BỀN.

MỤC ĐÍCH: ghi dữ liệu thô NGUYÊN XI xuống object storage TRƯỚC khi làm bất cứ gì
khác. Vì YouTube KHÔNG trả về số liệu quá khứ, raw là thứ DUY NHẤT không thể tạo
lại. Mọi thứ khác (staging, published, báo cáo) đều dựng lại được TỪ raw.

HAI KHÁI NIỆM CỐT LÕI CỦA FILE NÀY:

1. MANIFEST LÀ COMMIT MARKER.
   Nhìn vào bucket KHÔNG cho biết một batch đã xong hay đang dở. Nên:
       ghi mọi batch  ->  ghi manifest CUỐI CÙNG
   Sự TỒN TẠI của manifest = "batch này HOÀN CHỈNH". Không manifest = chưa xong,
   bất kể có bao nhiêu file nằm đó.
   (Hadoop/Spark/Hive dùng file rỗng `_SUCCESS` cho đúng mục đích này. Ta đi xa
   hơn một bước: manifest chứa cả siêu dữ liệu để đối soát.)

2. DUAL WRITE PROBLEM - MinIO và Postgres KHÔNG cùng transaction.
   Thứ tự ghi là quyết định thiết kế: RAW TRƯỚC, DATABASE SAU.
   Vì raw không thể tạo lại, còn database thì luôn dựng lại được từ raw.
   LUÔN BẢO VỆ THỨ KHÔNG THỂ TẠO LẠI TRƯỚC.

S3-AGNOSTIC: endpoint/credential đọc từ tham số (do config.py cấp), không hardcode
"minio" ở bất cứ đâu. Chuyển sang AWS S3 thật = đổi .env, KHÔNG sửa code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Phiên bản cấu trúc manifest. Khi đổi định dạng manifest, tăng số này ->
# code đọc biết mình đang đọc định dạng nào. Không có nó, đổi định dạng là
# phá vỡ mọi manifest cũ đã ghi (mà chúng thì không sửa lại được).
MANIFEST_SCHEMA_VERSION = 1

MANIFEST_FILENAME = "_manifest.json"


class StorageError(Exception):
    """Lỗi tầng lưu trữ. Lớp riêng để nơi gọi phân biệt được với lỗi API/DB."""


class ManifestNotFound(StorageError):
    """Không có manifest -> batch CHƯA HOÀN CHỈNH (hoặc chưa từng chạy).
    Đây KHÔNG phải lỗi bất thường: replay dùng nó để biết cần thu lại."""


@dataclass(frozen=True)
class RawObject:
    """Một file raw đã ghi thành công."""
    key: str
    size_bytes: int
    item_count: int
    observed_at: str          # ISO-8601 UTC - thời điểm GỌI API, không phải lúc ghi file


@dataclass(frozen=True)
class BatchManifest:
    """Tờ khai của MỘT kênh trong MỘT lần thu.

    Đây là "hợp đồng" cho phép replay: đọc manifest là biết đủ mọi thứ để nạp lại
    vào database mà KHÔNG cần gọi API.
    """
    schema_version: int
    collection_id: str
    channel_id: str
    attempt: int
    resource: str

    # Thời điểm - ba mốc khác nhau, đừng gộp
    scheduled_for: str        # ô lịch
    started_at: str           # thực sự bắt đầu
    completed_at: str         # ghi xong manifest

    # Đối soát: kỳ vọng vs thực nhận. Đây là thứ biến "silent data loss" thành
    # con số nhìn thấy được.
    expected_count: int
    received_count: int
    missing_ids: list[str]
    discovery_total_reported: int | None
    discovery_truncated: bool

    # Quota đã tiêu cho batch này
    request_count: int
    estimated_units: int

    # Danh sách file raw thuộc batch này (đường dẫn + kích cỡ + số item)
    objects: list[dict]

    # Tham số request ĐÃ LOẠI BỎ API KEY.
    # ⚠️ Manifest được đọc bởi nhiều người/công cụ và có thể bị copy đi nơi khác.
    # Ghi secret vào đây là rò rỉ vĩnh viễn - file raw gần như không bao giờ bị xóa.
    request_params: dict

    code_version: str | None = None


class RawStore:
    """Bọc object storage tương thích S3 (MinIO ở dev, AWS S3 ở production).

    Dùng boto3 - thư viện S3 chính thức. MinIO nói đúng giao thức S3, nên code
    này chạy y nguyên trên AWS: chỉ bỏ endpoint_url là xong.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str = "youtube-raw",
        prefix: str = "youtube",
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                # path-style: http://minio:9000/BUCKET/key
                # virtual-host style (mặc định của AWS): http://BUCKET.s3.../key
                # MinIO ở local KHÔNG có DNS wildcard cho từng bucket -> phải path.
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                # Retry của chính boto3, độc lập với tenacity ở tầng API.
                # 'standard' = có exponential backoff + jitter sẵn.
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    # ------------------------------------------------------------- bucket --

    def ensure_bucket(self) -> None:
        """Tạo bucket nếu chưa có. IDEMPOTENT - gọi bao nhiêu lần cũng được.

        Dùng head_bucket để KIỂM TRA thay vì list_buckets rồi lọc:
        head_bucket chỉ hỏi về đúng một bucket -> nhanh hơn, và không cần quyền
        liệt kê toàn bộ bucket (nguyên tắc least privilege).
        """
        try:
            self._s3.head_bucket(Bucket=self.bucket)
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            # '404'/'NoSuchBucket' = chưa có -> tạo. Mã khác = lỗi thật -> ném lên.
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise StorageError(f"Không kiểm tra được bucket {self.bucket}: {e}") from e

        try:
            self._s3.create_bucket(Bucket=self.bucket)
            logger.info("Đã tạo bucket %s", self.bucket)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            # Hai tiến trình cùng tạo một lúc -> một cái thắng, cái kia gặp lỗi
            # "đã tồn tại". Đó KHÔNG phải lỗi: kết quả cuối cùng vẫn đúng ý ta.
            # Xử lý được tình huống này mới thực sự là idempotent.
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            raise StorageError(f"Không tạo được bucket {self.bucket}: {e}") from e

    def bucket_exists(self) -> bool:
        """Bucket đã tồn tại chưa? KHÔNG tạo mới (khác ensure_bucket).

        Chỉ coi "không tồn tại" là False khi MinIO/S3 nói rõ 404/NoSuchBucket.
        Lỗi khác (sai mật khẩu, không có quyền, mất mạng) -> NÉM LÊN, không trả
        False: nếu gộp chung, một lỗi quyền truy cập sẽ bị hiểu nhầm thành
        "chưa có dữ liệu" - đúng kiểu lỗi im lặng cần tránh.
        """
        try:
            self._s3.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket", "NotFound"):
                return False
            raise StorageError(f"Không kiểm tra được bucket {self.bucket}: {e}") from e

    # ---------------------------------------------------------------- key --

    def batch_prefix(self, *, resource: str, channel_id: str, collection_id: str, attempt: int) -> str:
        """Dựng đường dẫn thư mục cho một batch.

        Định dạng key=value là HIVE-STYLE PARTITIONING, không phải để cho đẹp:
        Spark/Athena/Trino/DuckDB TỰ HIỂU cấu trúc này và chỉ đọc đúng phân vùng
        cần thiết (partition pruning) thay vì quét cả bucket.
        """
        return (
            f"{self.prefix}/resource={resource}"
            f"/channel_id={channel_id}"
            f"/collection_id={collection_id}"
            f"/attempt={attempt}"
        )

    # -------------------------------------------------------------- write --

    def put_batch(
        self,
        *,
        resource: str,
        channel_id: str,
        collection_id: str,
        attempt: int,
        batch_index: int,
        payload: list[dict] | dict,
        observed_at: datetime,
    ) -> RawObject:
        """Ghi MỘT lô dữ liệu thô.

        batch_index được đệm 0 thành 4 chữ số (0001, 0002...) -> sắp xếp theo
        tên file cũng ra đúng thứ tự. Nếu để '1','2','10' thì sort chuỗi cho ra
        '1','10','2' - sai thứ tự. Chi tiết nhỏ nhưng gây lỗi khó thấy về sau.
        """
        key = f"{self.batch_prefix(resource=resource, channel_id=channel_id, collection_id=collection_id, attempt=attempt)}/batch={batch_index:04d}.json"

        # ensure_ascii=False: giữ nguyên tiếng Việt và emoji trong tiêu đề video
        # thay vì biến thành ạ... Raw phải ĐỌC ĐƯỢC BẰNG MẮT khi điều tra.
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
                # Metadata của object: dùng được để lọc mà không cần mở file.
                # Giá trị metadata S3 phải là chuỗi ASCII.
                Metadata={
                    "collection-id": collection_id,
                    "channel-id": channel_id,
                    "observed-at": observed_at.astimezone(timezone.utc).isoformat(),
                },
            )
        except ClientError as e:
            raise StorageError(f"Ghi thất bại {key}: {e}") from e

        count = len(payload) if isinstance(payload, list) else 1
        logger.info("Đã ghi raw s3://%s/%s (%d item, %d byte)", self.bucket, key, count, len(body))

        return RawObject(
            key=key,
            size_bytes=len(body),
            item_count=count,
            observed_at=observed_at.astimezone(timezone.utc).isoformat(),
        )

    def put_manifest(self, manifest: BatchManifest) -> str:
        """Ghi manifest - PHẢI LÀ VIỆC CUỐI CÙNG của batch.

        ⭐ Sự tồn tại của file này = "batch HOÀN CHỈNH". Đây là commit marker.
        Gọi hàm này TRƯỚC khi ghi xong hết batch là phá vỡ toàn bộ cơ chế:
        replay sẽ tin là đủ trong khi thực tế còn thiếu file.
        """
        key = (
            self.batch_prefix(
                resource=manifest.resource,
                channel_id=manifest.channel_id,
                collection_id=manifest.collection_id,
                attempt=manifest.attempt,
            )
            + f"/{MANIFEST_FILENAME}"
        )

        body = json.dumps(asdict(manifest), ensure_ascii=False, indent=2).encode("utf-8")

        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
        except ClientError as e:
            raise StorageError(f"Ghi manifest thất bại {key}: {e}") from e

        logger.info(
            "✅ MANIFEST -> s3://%s/%s  (%d/%d bản ghi, %d file)",
            self.bucket, key, manifest.received_count, manifest.expected_count,
            len(manifest.objects),
        )
        return key

    # --------------------------------------------------------------- read --

    def manifest_exists(self, *, resource: str, channel_id: str, collection_id: str, attempt: int) -> bool:
        """Batch này đã hoàn chỉnh chưa?

        Dùng head_object: chỉ lấy metadata, KHÔNG tải nội dung. Rẻ hơn get_object
        rất nhiều khi chỉ cần biết "có tồn tại không".
        """
        key = self.batch_prefix(resource=resource, channel_id=channel_id, collection_id=collection_id, attempt=attempt) + f"/{MANIFEST_FILENAME}"
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(f"Không kiểm tra được manifest {key}: {e}") from e

    def read_manifest(self, *, resource: str, channel_id: str, collection_id: str, attempt: int) -> dict:
        """Đọc manifest. Đây là ĐIỂM VÀO CỦA REPLAY."""
        key = self.batch_prefix(resource=resource, channel_id=channel_id, collection_id=collection_id, attempt=attempt) + f"/{MANIFEST_FILENAME}"
        return self._read_json(key)

    def read_batch(self, key: str) -> list[dict]:
        """Đọc một file raw đã ghi."""
        data = self._read_json(key)
        return data if isinstance(data, list) else [data]

    def _read_json(self, key: str) -> dict | list:
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise ManifestNotFound(f"Không có object: s3://{self.bucket}/{key}") from e
            raise StorageError(f"Đọc thất bại {key}: {e}") from e

        try:
            return json.loads(response["Body"].read().decode("utf-8"))
        except json.JSONDecodeError as e:
            # File tồn tại nhưng nội dung hỏng -> lỗi KHÁC với "không có file".
            # Phân biệt hai loại này giúp chẩn đoán: hỏng = có thể do ghi dở.
            raise StorageError(f"JSON hỏng trong {key}: {e}") from e

    def list_objects(self, prefix: str) -> list[dict]:
        """Liệt kê object kèm SIÊU DỮ LIỆU (kích cỡ, thời điểm sửa cuối).

        Retention cần `last_modified` để biết object nào đã quá hạn, và `size`
        để báo cáo sẽ giải phóng bao nhiêu dung lượng. `list_keys` chỉ trả tên
        nên không đủ.
        """
        out: list[dict] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                out.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    # boto3 trả về datetime CÓ múi giờ (UTC) - so sánh được ngay
                    # với utc_now(), không cần ép kiểu.
                    "last_modified": obj["LastModified"],
                })
        return out

    def delete_keys(self, keys: list[str]) -> int:
        """Xóa nhiều object. Trả về số object đã xóa.

        ⚠️ KHÔNG HOÀN TÁC ĐƯỢC. Nơi gọi phải chắc chắn trước khi gọi.

        Dùng delete_objects (xóa theo LÔ tối đa 1000 key/lần) thay vì gọi
        delete_object từng cái: 1000 object = 1 request thay vì 1000 request.
        Cùng tư duy với execute_values ở tầng database.
        """
        if not keys:
            return 0

        deleted = 0
        for i in range(0, len(keys), 1000):          # trần cứng của S3 API
            chunk = keys[i:i + 1000]
            try:
                resp = self._s3.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )
            except ClientError as e:
                raise StorageError(f"Xóa thất bại: {e}") from e

            # Xóa theo lô là "một phần thành công" được: phải ĐỌC danh sách lỗi,
            # không được giả định cả lô đều xong.
            errors = resp.get("Errors", [])
            if errors:
                logger.error("Có %d object không xóa được, ví dụ: %s",
                             len(errors), errors[:3])
            deleted += len(chunk) - len(errors)

        logger.info("Đã xóa %d object khỏi s3://%s", deleted, self.bucket)
        return deleted

    def list_keys(self, prefix: str) -> list[str]:
        """Liệt kê object theo prefix, CÓ XỬ LÝ PHÂN TRANG.

        ⚠️ list_objects_v2 chỉ trả tối đa 1000 key mỗi lần. Rất nhiều người quên
        điều này và code của họ chạy đúng trong lúc test (ít file) rồi âm thầm
        bỏ sót khi lên production (nhiều file). Paginator lo việc lặp trang.
        """
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys


def utc_now() -> datetime:
    """Thời điểm hiện tại CÓ múi giờ (aware), luôn UTC.

    KHÔNG dùng datetime.utcnow(): nó trả về datetime KHÔNG có tzinfo (naive) -
    tức một con số mà không ai biết thuộc múi giờ nào. Trộn naive và aware là
    nguồn lỗi thời gian phổ biến nhất trong Python, và utcnow() đã bị đánh dấu
    deprecated từ Python 3.12.
    """
    return datetime.now(timezone.utc)
