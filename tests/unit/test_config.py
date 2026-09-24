"""Config: fail-fast, một nguồn sự thật cho lịch chạy."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from youtube_intel.config import ConfigError, DEFAULT_CONFIG_PATH, load_config
from youtube_intel.pipeline import current_slot, deterministic_batch_id

VN = ZoneInfo("Asia/Ho_Chi_Minh")


@pytest.fixture
def cfg_path(tmp_path):
    """Bản sao config để phá thử mà không đụng file thật."""
    def make(mutate=None):
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        if mutate:
            mutate(raw)
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return p
    return make


def test_real_config_loads_without_secrets():
    assert load_config(require=()).enabled_channels()


@pytest.mark.parametrize("mutate", [
    lambda c: c["channels"][0].__setitem__("id", "UCsai"),
    lambda c: c["channels"][0].__setitem__("group", "book_learning"),
    lambda c: c["channels"][1].__setitem__("id", c["channels"][0]["id"]),
    lambda c: c["project"].__setitem__("snapshot_hours", 5),       # 24 % 5 != 0
    lambda c: c["project"].pop("max_attempts"),
])
def test_bad_config_fails_fast(cfg_path, mutate):
    with pytest.raises(ConfigError):
        load_config(cfg_path(mutate), require=())


def test_missing_secret_reported_all_at_once(monkeypatch):
    for k in ("API_KEY", "MINIO_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError) as e:
        load_config(require=("youtube", "minio"))
    assert "API_KEY" in str(e.value) and "MINIO_SECRET_KEY" in str(e.value)


def test_secrets_never_printed():
    assert "Secrets(<đã ẩn>)" == repr(load_config(require=()).secrets)


def test_cron_derived_from_config():
    # Lịch khai MỘT nơi (channels.yaml), DAG suy ra - không thể lệch nhau
    s = load_config(require=()).settings
    assert s.cron_expression() == "0 21 * * *"


@pytest.mark.parametrize("now, expected_day", [
    (datetime(2026, 9, 23, 20, 59, tzinfo=VN), 22),   # chưa tới mốc -> mốc hôm qua
    (datetime(2026, 9, 23, 21, 0, tzinfo=VN), 23),
    (datetime(2026, 9, 24, 8, 0, tzinfo=VN), 23),     # sáng hôm sau vẫn thuộc mốc tối qua
])
def test_current_slot_follows_report_timezone(now, expected_day):
    s = load_config(require=()).settings
    slot = current_slot(s, now=now).astimezone(VN)
    assert (slot.day, slot.hour) == (expected_day, 21)


def test_batch_id_is_deterministic():
    # Chạy lại cùng collection -> cùng batch_id -> không nhân đôi staging
    a = deterministic_batch_id("c1", "UCx")
    assert a == deterministic_batch_id("c1", "UCx")
    assert a != deterministic_batch_id("c2", "UCx")


def test_reference_names_loaded_from_config():
    chans = {c.title: c for c in load_config(require=()).channels}
    wolfoo = next(c for t, c in chans.items() if t.startswith("Wolfoo"))
    assert "Wolfoo" in wolfoo.reference_names
