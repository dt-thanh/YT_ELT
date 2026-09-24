"""
DAG integrity - test RẺ NHẤT mà GIÁ TRỊ NHẤT của mọi repo Airflow.

Bắt lỗi import/cú pháp TRƯỚC khi deploy. Không có nó, bạn chỉ biết DAG hỏng
khi mở UI thấy chữ đỏ - tức là SAU khi nó đã không chạy đêm qua.

Cần airflow -> chạy TRONG container:
    docker exec airflow-scheduler bash -lc "cd /opt/airflow && pytest tests/test_dags.py"
"""
import pytest

airflow = pytest.importorskip("airflow")      # máy dev không có airflow -> BỎ QUA, không fail
from airflow.models import DagBag            # noqa: E402

EXPECTED = {"yt_collect", "yt_quality"}


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder="/opt/airflow/dags", include_examples=False)


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}, dagbag.import_errors


def test_expected_dags_present(dagbag):
    # So TẬP HỢP, không đếm cứng số lượng: test cũ assert size()==3 nên vỡ ngay
    # khi thay DAG. Test phải kiểm tra ĐIỀU QUAN TRỌNG, không phải chi tiết vặt.
    assert EXPECTED <= set(dagbag.dag_ids)


def test_collect_has_one_task_per_enabled_channel(dagbag):
    from youtube_intel.config import load_config
    n = len(load_config(require=()).enabled_channels())
    dag = dagbag.get_dag("yt_collect")
    collect = [t for t in dag.task_ids if t.startswith("collect_")]
    assert len(collect) == n


def test_collect_never_catches_up(dagbag):
    # ⭐ catchup=True sẽ BỊA dữ liệu: YouTube không trả số liệu quá khứ
    assert dagbag.get_dag("yt_collect").catchup is False


def test_quality_runs_on_independent_schedule(dagbag):
    # ⭐ Monitor phải có nhịp RIÊNG, nếu không pipeline chết là monitor chết theo
    c = dagbag.get_dag("yt_collect").timetable.summary
    q = dagbag.get_dag("yt_quality").timetable.summary
    assert c != q
