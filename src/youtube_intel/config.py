"""
Cổng vào duy nhất của mọi cấu hình.

TRIẾT LÝ CỦA FILE NÀY - đọc kỹ trước khi đọc code:

1. MỘT NƠI DUY NHẤT ĐỌC os.environ.
   Cả project chỉ file này được gọi os.environ. Mọi module khác NHẬN một
   object Config làm tham số. Vì sao?
     - Test: muốn đổi config trong test thì tạo object khác, không phải
       vá biến môi trường toàn cục (thứ rò rỉ giữa các test).
     - Truy vết: cần biết "biến X đọc ở đâu" -> grep một file, không phải cả repo.
   Kỹ thuật này tên là DEPENDENCY INJECTION: thay vì hàm tự đi lấy thứ nó cần,
   ta ĐƯA cho nó.

2. FAIL FAST - sai thì gãy NGAY LÚC KHỞI ĐỘNG.
   Thiếu API_KEY thì hỏng ngay giây đầu tiên, kèm thông báo nói rõ thiếu gì.
   KHÔNG để pipeline chạy được 20 phút, gọi xong 40 request, rồi mới chết ở
   bước ghi MinIO vì thiếu MINIO_SECRET_KEY. Lỗi phát hiện càng sớm càng rẻ.

3. BẤT BIẾN (frozen dataclass).
   Config đọc xong là khóa lại, không module nào sửa được. Config bị sửa giữa
   chừng là loại bug kinh hoàng nhất: hành vi đổi mà không có dấu vết nào.

4. CÓ KIỂU (typed), không phải dict.
   cfg.settings.snapshot_hours -> IDE gợi ý được, gõ sai tên thì đỏ ngay.
   cfg["setting"]["snapshot_hour"] -> chỉ vỡ lúc chạy, giữa đêm.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Channel ID của YouTube: "UC" + đúng 22 ký tự base64url.
# Validate bằng regex để bắt lỗi gõ nhầm/thiếu ký tự NGAY, thay vì để API trả
# về items rỗng rồi mới đi dò ngược.
CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

# Đường đi mặc định tới file config, tính TƯƠNG ĐỐI so với vị trí file .py này.
# Không dùng đường dẫn tuyệt đối, cũng không dùng "thư mục hiện hành":
# cwd thay đổi tùy chỗ bạn gõ lệnh, còn __file__ thì không bao giờ đổi.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "channels.yaml"


class ConfigError(Exception):
    """Cấu hình sai. Lỗi RIÊNG để nơi gọi phân biệt được 'sai cấu hình'
    với 'lỗi mạng' hay 'lỗi database' - ba thứ này cần ba cách xử lý khác nhau."""


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    group: str
    title: str
    source_url: str
    enabled: bool


@dataclass(frozen=True)
class Settings:
    """Lựa chọn vận hành - đọc từ YAML, đi vào git."""
    name: str
    report_timezone: str
    snapshot_hours: int
    initial_video_limit_per_channel: int
    discovery_page_limit_per_channel: int
    request_timeout_seconds: int
    max_attempts: int
    derived_metrics_enabled: bool
    llm_enabled: bool


@dataclass(frozen=True)
class Secrets:
    """Bí mật - đọc từ biến môi trường, KHÔNG vào git.

    repr=False ở các trường nhạy cảm: nếu ai đó print(config) hoặc một thư viện
    log object này ra, mật khẩu KHÔNG bị in. Secret rò rỉ vào log là một trong
    những đường lộ dữ liệu phổ biến nhất - log thường được gom về nơi nhiều
    người đọc được hơn database rất nhiều.
    """
    youtube_api_key: str = None            # type: ignore[assignment]
    minio_endpoint: str = None             # type: ignore[assignment]
    minio_access_key: str = None           # type: ignore[assignment]
    minio_secret_key: str = None           # type: ignore[assignment]
    db_host: str = None                    # type: ignore[assignment]
    db_port: str = None                    # type: ignore[assignment]
    db_name: str = None                    # type: ignore[assignment]
    db_user: str = None                    # type: ignore[assignment]
    db_password: str = None                # type: ignore[assignment]

    def __repr__(self) -> str:             # chặn in secret ra log
        return "Secrets(<đã ẩn>)"


@dataclass(frozen=True)
class Config:
    settings: Settings
    channels: tuple[ChannelConfig, ...]
    secrets: Secrets

    def enabled_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(c for c in self.channels if c.enabled)

    def channels_in_group(self, group: str) -> tuple[ChannelConfig, ...]:
        return tuple(c for c in self.enabled_channels() if c.group == group)


# =============================================================================
# ĐỌC VÀ KIỂM TRA
# =============================================================================

def load_config(path: Path | str | None = None, *, require_secrets: bool = True) -> Config:
    """Đọc YAML + biến môi trường, kiểm tra, trả về Config bất biến.

    require_secrets=False dùng cho unit test: test logic parse config thì không
    cần API key thật. Đây là "escape hatch" có chủ đích, không phải lỗ hổng -
    mặc định vẫn là bắt buộc phải có secret.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(f"Không tìm thấy file config: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)     # safe_load, KHÔNG dùng load() - xem bài giảng

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} không phải một YAML dạng mapping.")

    settings = _parse_settings(raw.get("project"), path)
    channels = _parse_channels(raw.get("channels"), raw.get("groups"), path)
    secrets = _load_secrets(require=require_secrets)

    return Config(settings=settings, channels=channels, secrets=secrets)


def _parse_settings(block, path: Path) -> Settings:
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: thiếu khối 'project'.")

    def need(key, cast):
        if key not in block:
            raise ConfigError(f"{path}: thiếu project.{key}")
        try:
            return cast(block[key])
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{path}: project.{key} sai kiểu: {e}") from e

    settings = Settings(
        name=need("name", str),
        report_timezone=need("report_timezone", str),
        snapshot_hours=need("snapshot_hours", int),
        initial_video_limit_per_channel=need("initial_video_limit_per_channel", int),
        discovery_page_limit_per_channel=need("discovery_page_limit_per_channel", int),
        request_timeout_seconds=need("request_timeout_seconds", int),
        max_attempts=need("max_attempts", int),
        derived_metrics_enabled=need("derived_metrics_enabled", bool),
        llm_enabled=need("llm_enabled", bool),
    )

    # Kiểm tra MIỀN GIÁ TRỊ, không chỉ kiểu.
    # snapshot_hours = 0 đúng kiểu int nhưng vô nghĩa -> chia cho 0 ở bước tính metric.
    # Bắt ở đây rẻ hơn bắt ở production rất nhiều.
    if not 1 <= settings.snapshot_hours <= 24:
        raise ConfigError("project.snapshot_hours phải trong khoảng 1..24")
    if settings.initial_video_limit_per_channel < 1:
        raise ConfigError("project.initial_video_limit_per_channel phải >= 1")
    if settings.discovery_page_limit_per_channel < 1:
        raise ConfigError("project.discovery_page_limit_per_channel phải >= 1")
    if settings.max_attempts < 1:
        raise ConfigError("project.max_attempts phải >= 1")

    return settings


def _parse_channels(block, groups_block, path: Path) -> tuple[ChannelConfig, ...]:
    if not isinstance(block, list) or not block:
        raise ConfigError(f"{path}: 'channels' phải là danh sách không rỗng.")

    valid_groups = set(groups_block or [])
    if not valid_groups:
        raise ConfigError(f"{path}: thiếu danh sách 'groups'.")

    channels, seen = [], set()

    for i, item in enumerate(block):
        where = f"{path}: channels[{i}]"

        if not isinstance(item, dict):
            raise ConfigError(f"{where} phải là mapping.")

        channel_id = str(item.get("id", "")).strip()
        if not CHANNEL_ID_PATTERN.match(channel_id):
            raise ConfigError(
                f"{where}: channel id không hợp lệ: {channel_id!r}. "
                "Định dạng đúng: 'UC' + 22 ký tự (chữ, số, '-', '_')."
            )

        # Trùng ID -> mỗi collection sẽ thu kênh đó hai lần, tốn quota gấp đôi
        # và sinh ra hai batch cho cùng một kênh. Bắt ngay.
        if channel_id in seen:
            raise ConfigError(f"{where}: channel id bị lặp: {channel_id}")
        seen.add(channel_id)

        group = str(item.get("group", "")).strip()
        if group not in valid_groups:
            raise ConfigError(
                f"{where}: group {group!r} không nằm trong {sorted(valid_groups)}."
            )

        channels.append(
            ChannelConfig(
                channel_id=channel_id,
                group=group,
                title=str(item.get("title", "")).strip(),
                source_url=str(item.get("source_url", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
        )

    return tuple(channels)


def _load_secrets(*, require: bool) -> Secrets:
    """Đọc secret từ biến môi trường.

    Gom TẤT CẢ biến thiếu rồi báo MỘT LẦN. Không báo từng cái một bắt người
    dùng sửa - chạy - lại thiếu - sửa tiếp. Chi tiết vặt nhưng khác biệt lớn
    về trải nghiệm vận hành lúc 2 giờ sáng.
    """
    mapping = {
        "youtube_api_key": "API_KEY",
        "minio_endpoint": "MINIO_ENDPOINT",
        "minio_access_key": "MINIO_ACCESS_KEY",
        "minio_secret_key": "MINIO_SECRET_KEY",
        "db_host": "POSTGRES_CONN_HOST",
        "db_port": "POSTGRES_CONN_PORT",
        "db_name": "ELT_DATABASE_NAME",
        "db_user": "ELT_DATABASE_USERNAME",
        "db_password": "ELT_DATABASE_PASSWORD",
    }

    values = {field: os.environ.get(env_name) for field, env_name in mapping.items()}

    if require:
        missing = sorted(env for field, env in mapping.items() if not values[field])
        if missing:
            raise ConfigError(
                "Thiếu biến môi trường: " + ", ".join(missing) +
                ". Xem .env.example để biết cần những gì."
            )

    return Secrets(**values)
