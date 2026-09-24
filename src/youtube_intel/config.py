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
    # Tên thương hiệu / nhân vật của kênh THAM KHẢO. Gợi ý nội dung KHÔNG được
    # dùng lại (brief §10 - nội dung phải nguyên bản). Khai TƯỜNG MINH ở config
    # thay vì tự đoán tên riêng từ tiêu đề: đoán thì báo nhầm/lọt lưới, khai thì
    # kiểm tra được và sửa được mà không đụng code.
    reference_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    """Lựa chọn vận hành - đọc từ YAML, đi vào git."""
    name: str
    report_timezone: str
    snapshot_hours: int
    # Mốc lịch đầu tiên trong ngày, tính theo report_timezone.
    # Cùng với snapshot_hours, hai giá trị này là NGUỒN SỰ THẬT DUY NHẤT về lịch
    # chạy - DAG suy ra cron từ đây thay vì khai lại.
    snapshot_anchor_hour: int
    initial_video_limit_per_channel: int
    discovery_page_limit_per_channel: int
    request_timeout_seconds: int
    max_attempts: int
    derived_metrics_enabled: bool
    llm_enabled: bool
    # LLM - xem giải thích trong config/channels.yaml
    llm_model: str
    llm_max_output_tokens: int
    prompt_version: str
    report_top_n: int
    # Retention - xem giải thích trong config/channels.yaml
    staging_retention_days: int
    orphan_retention_hours: int
    raw_retention_days: int | None      # None = KHÔNG BAO GIỜ xóa raw

    def slot_hours(self) -> tuple[int, ...]:
        """Các giờ trong ngày có mốc thu thập, theo report_timezone.

        24h + anchor 21  ->  (21,)
        12h + anchor  0  ->  (0, 12)
         6h + anchor  3  ->  (3, 9, 15, 21)
        """
        return tuple(range(self.snapshot_anchor_hour, 24, self.snapshot_hours))

    def cron_expression(self) -> str:
        """Biểu thức cron cho Airflow, SUY RA từ config.

        ⭐ Nhờ hàm này, lịch chỉ được khai MỘT NƠI (channels.yaml).
        DAG không tự viết cron -> không thể lệch với snapshot_hours.
        Giờ ở đây là giờ ĐỊA PHƯƠNG: DAG phải khai tz=report_timezone thì cron
        mới được diễn giải đúng múi giờ.
        """
        return f"0 {','.join(str(h) for h in self.slot_hours())} * * *"


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
    openai_api_key: str = None             # type: ignore[assignment]

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

# Secret chia theo NHÓM CHỨC NĂNG. Mỗi lệnh chỉ đòi nhóm nó thực sự dùng.
#
# ⭐ VÌ SAO KHÔNG ĐÒI TẤT CẢ CHO GỌN?
# Vì `replay` KHÔNG gọi YouTube - nó đọc raw từ MinIO. Đòi API_KEY sẽ CHẶN một
# thao tác hoàn toàn hợp lệ, và buộc người vận hành đặt một key giả chỉ để chạy
# được. Thói quen đó làm hỏng kỷ luật bảo mật.
# Fail-fast là đúng, nhưng phải fail-fast về thứ MÌNH THỰC SỰ CẦN.
# Đây là nguyên tắc least privilege áp dụng cho cấu hình.
SECRET_GROUPS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube_api_key",),
    "minio": ("minio_endpoint", "minio_access_key", "minio_secret_key"),
    "db": ("db_host", "db_port", "db_name", "db_user", "db_password"),
    # Nhóm riêng: chỉ lệnh `report` với llm_enabled=true mới đòi key này.
    # Mọi lệnh khác chạy bình thường mà không cần OPENAI_API_KEY.
    "llm": ("openai_api_key",),
}

# Mặc định của load_config() = những nhóm LÕI PIPELINE cần.
# CỐ Ý KHÔNG gồm "llm": LLM là tính năng TÙY CHỌN. Nếu đưa vào đây thì mọi lệnh
# (kể cả `collect`) sẽ đòi OPENAI_API_KEY - tức bắt người dùng trả tiền cho thứ
# họ không dùng. Lệnh `report` tự khai require=("db","llm") khi cần.
ALL_SECRET_GROUPS = ("youtube", "minio", "db")


def load_config(
    path: Path | str | None = None,
    *,
    require: tuple[str, ...] | None = None,
    require_secrets: bool | None = None,
) -> Config:
    """Đọc YAML + biến môi trường, kiểm tra, trả về Config bất biến.

    require: nhóm secret BẮT BUỘC phải có. Ví dụ:
        load_config()                          -> đòi cả 3 nhóm (mặc định an toàn)
        load_config(require=("minio", "db"))   -> replay: không cần API key
        load_config(require=("db",))           -> status: chỉ cần database
        load_config(require=())                -> unit test: không đòi gì

    require_secrets: tham số CŨ, giữ để code cũ không vỡ (backward compatible).
        False tương đương require=().
    """
    if require is None:
        require = () if require_secrets is False else ALL_SECRET_GROUPS

    unknown = set(require) - set(SECRET_GROUPS)
    if unknown:
        raise ConfigError(f"Nhóm secret không tồn tại: {sorted(unknown)}")
    path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(f"Không tìm thấy file config: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)     # safe_load, KHÔNG dùng load() - xem bài giảng

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} không phải một YAML dạng mapping.")

    settings = _parse_settings(raw.get("project"), path)
    channels = _parse_channels(raw.get("channels"), raw.get("groups"), path)
    secrets = _load_secrets(require=require)

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
        snapshot_anchor_hour=need("snapshot_anchor_hour", int),
        initial_video_limit_per_channel=need("initial_video_limit_per_channel", int),
        discovery_page_limit_per_channel=need("discovery_page_limit_per_channel", int),
        request_timeout_seconds=need("request_timeout_seconds", int),
        max_attempts=need("max_attempts", int),
        derived_metrics_enabled=need("derived_metrics_enabled", bool),
        llm_enabled=need("llm_enabled", bool),
        llm_model=need("llm_model", str),
        llm_max_output_tokens=need("llm_max_output_tokens", int),
        prompt_version=need("prompt_version", str),
        report_top_n=need("report_top_n", int),
        staging_retention_days=need("staging_retention_days", int),
        orphan_retention_hours=need("orphan_retention_hours", int),
        # raw_retention_days ĐƯỢC PHÉP null -> không dùng need() (need bắt buộc có giá trị).
        # null ở đây là một LỰA CHỌN CÓ Ý NGHĨA ("không xóa"), không phải thiếu cấu hình.
        raw_retention_days=(
            None if block.get("raw_retention_days") is None
            else int(block["raw_retention_days"])
        ),
    )

    # Kiểm tra MIỀN GIÁ TRỊ, không chỉ kiểu.
    # snapshot_hours = 0 đúng kiểu int nhưng vô nghĩa -> chia cho 0 ở bước tính metric.
    # Bắt ở đây rẻ hơn bắt ở production rất nhiều.
    if not 1 <= settings.snapshot_hours <= 24:
        raise ConfigError("project.snapshot_hours phải trong khoảng 1..24")
    if not 0 <= settings.snapshot_anchor_hour <= 23:
        raise ConfigError("project.snapshot_anchor_hour phải trong khoảng 0..23")
    # 24 phải chia hết cho snapshot_hours, nếu không các mốc sẽ LỆCH DẦN qua
    # từng ngày (vd 5 giờ/lần: 0,5,10,15,20 rồi hôm sau 1,6,11... ) - lịch không
    # lặp lại được theo ngày, và cron cũng không biểu diễn nổi.
    if 24 % settings.snapshot_hours != 0:
        raise ConfigError(
            f"project.snapshot_hours={settings.snapshot_hours} phải là ước của 24 "
            "(1,2,3,4,6,8,12,24) để lịch lặp lại đều mỗi ngày."
        )
    if settings.snapshot_anchor_hour >= settings.snapshot_hours:
        raise ConfigError(
            f"project.snapshot_anchor_hour={settings.snapshot_anchor_hour} phải NHỎ HƠN "
            f"snapshot_hours={settings.snapshot_hours}; nếu không mốc đầu tiên sẽ trùng "
            "với một mốc khác trong cùng ngày."
        )
    if settings.initial_video_limit_per_channel < 1:
        raise ConfigError("project.initial_video_limit_per_channel phải >= 1")
    if settings.discovery_page_limit_per_channel < 1:
        raise ConfigError("project.discovery_page_limit_per_channel phải >= 1")
    if settings.max_attempts < 1:
        raise ConfigError("project.max_attempts phải >= 1")
    if not 1 <= settings.report_top_n <= 10:
        raise ConfigError("project.report_top_n phải trong khoảng 1..10")
    if settings.staging_retention_days < 1:
        raise ConfigError("project.staging_retention_days phải >= 1")
    if settings.orphan_retention_hours < 1:
        raise ConfigError("project.orphan_retention_hours phải >= 1")
    if settings.raw_retention_days is not None and settings.raw_retention_days < 1:
        raise ConfigError("project.raw_retention_days phải >= 1 hoặc để null (không xóa)")

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
                reference_names=tuple(
                    str(n).strip() for n in (item.get("reference_names") or []) if str(n).strip()
                ),
            )
        )

    return tuple(channels)


def _load_secrets(*, require: tuple[str, ...]) -> Secrets:
    """Đọc secret từ biến môi trường; chỉ BẮT BUỘC các nhóm được yêu cầu.

    Luôn ĐỌC hết mọi biến (để Config đầy đủ), nhưng chỉ KIỂM TRA nhóm cần thiết.

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
        "openai_api_key": "OPENAI_API_KEY",
    }

    values = {field: os.environ.get(env_name) for field, env_name in mapping.items()}

    required_fields = {f for group in require for f in SECRET_GROUPS[group]}
    missing = sorted(mapping[f] for f in required_fields if not values[f])
    if missing:
        raise ConfigError(
            "Thiếu biến môi trường: " + ", ".join(missing) +
            f" (cần cho: {', '.join(sorted(require))})."
            " Xem .env.example để biết cần những gì."
        )

    return Secrets(**values)
