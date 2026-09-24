"""
Dashboard Streamlit - 3 màn hình.

NGUYÊN TẮC SỐ MỘT: DASHBOARD KHÔNG TÍNH TOÁN.
Mọi con số hiển thị ở đây đều đến từ SQL (chủ yếu là VIEW v_video_growth).
Streamlit chỉ ĐỌC và VẼ.

Vì sao nghiêm ngặt thế? Vì nếu dashboard tự tính views_per_hour bằng Python,
ta sẽ có HAI công thức cho cùng một chỉ số: một trong SQL, một trong Python.
Chúng sẽ lệch nhau - và người xem không biết tin cái nào. Đây là lỗi kinh điển
khiến "báo cáo của phòng A khác báo cáo của phòng B" dù cùng một database.
MỘT chỉ số = MỘT định nghĩa = MỘT nơi.

NGUYÊN TẮC SỐ HAI: LUÔN HIỂN THỊ ĐỘ BAO PHỦ VÀ ĐỘ TƯƠI.
Bảng xếp hạng mà không nói "dựa trên bao nhiêu / tổng bao nhiêu video" là
bảng xếp hạng gây hiểu nhầm. Người xem phải biết mình đang nhìn bao nhiêu phần
của sự thật, và dữ liệu cũ đến mức nào.

CHẠY:
    streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

# Cho phép import package khi chạy streamlit từ thư mục gốc
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from youtube_intel.config import load_config          # noqa: E402
from youtube_intel import repository as repo          # noqa: E402

st.set_page_config(page_title="YouTube Content Intelligence",
                   page_icon="📊", layout="wide")


# =============================================================================
# TRUY CẬP DỮ LIỆU
# =============================================================================

@st.cache_resource
def get_db():
    """cache_resource: giữ MỘT đối tượng kết nối dùng chung cho mọi lần rerun.

    Streamlit chạy lại TOÀN BỘ file mỗi khi người dùng bấm gì đó. Không cache
    thì mỗi cú click là một lần đọc config + mở kết nối mới -> chậm và cạn slot
    Postgres. cache_resource dành cho thứ KHÔNG serialize được (connection,
    client); cache_data dành cho DataFrame.
    """
    cfg = load_config(require=("db",))     # dashboard chỉ đọc database
    s = cfg.secrets
    return repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                         user=s.db_user, password=s.db_password), cfg


@st.cache_data(ttl=60)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Chạy SQL, trả DataFrame. Cache 60 giây.

    ttl=60: dữ liệu chỉ cập nhật 1 lần/ngày nên không cần đọc lại database mỗi
    giây. Nhưng cũng không cache vĩnh viễn - người dùng bấm refresh phải thấy
    thay đổi trong vòng 1 phút.
    """
    db, _ = get_db()
    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute(sql, params)
            return pd.DataFrame([dict(r) for r in cur.fetchall()])


def age_text(ts) -> str:
    """Biến timestamp thành '3 giờ trước' - dễ đọc hơn ngày giờ tuyệt đối.

    Người vận hành quan tâm 'dữ liệu cũ bao lâu rồi', không phải 'lúc mấy giờ'.
    """
    if ts is None or pd.isna(ts):
        return "chưa có"
    delta = datetime.now(timezone.utc) - ts
    h = delta.total_seconds() / 3600
    if h < 1:
        return f"{int(delta.total_seconds() / 60)} phút trước"
    if h < 48:
        return f"{h:.1f} giờ trước"
    return f"{h / 24:.1f} ngày trước"


# =============================================================================
# THANH BÊN - trạng thái hệ thống, LUÔN hiển thị ở mọi màn hình
# =============================================================================

st.sidebar.title("📊 YouTube Intelligence")

try:
    _, CFG = get_db()
except Exception as e:
    st.error(f"Không kết nối được database: {e}")
    st.info("Kiểm tra: `docker compose ps` — postgres đã chạy chưa? "
            "Chạy ngoài Docker thì POSTGRES_CONN_HOST phải là `localhost`.")
    st.stop()

health = q("""
    SELECT (SELECT max(observed_at) FROM yti.video_observations)     AS last_obs,
           (SELECT count(*) FROM yti.video_observations)             AS n_obs,
           (SELECT count(*) FROM yti.videos_current)                 AS n_videos,
           (SELECT count(DISTINCT collection_id) FROM yti.video_observations) AS n_runs,
           (SELECT count(*) FROM yti.channel_batches
             WHERE quality_status = 'quarantined')                   AS n_quarantined
""").iloc[0]

last_obs_age = age_text(health["last_obs"])
is_stale = (health["last_obs"] is not None
            and (datetime.now(timezone.utc) - health["last_obs"]).total_seconds() > 30 * 3600)

# ⭐ ĐỘ TƯƠI LÀ THỨ ĐẦU TIÊN NGƯỜI XEM PHẢI THẤY.
# Một dashboard đẹp với dữ liệu 5 ngày tuổi còn tệ hơn không có dashboard:
# nó khiến người ta tin rằng mình đang nhìn tình hình hiện tại.
if is_stale:
    st.sidebar.error(f"⚠️ DỮ LIỆU CŨ\n\nQuan sát mới nhất: {last_obs_age}")
else:
    st.sidebar.success(f"✅ Dữ liệu mới\n\nCập nhật: {last_obs_age}")

st.sidebar.metric("Video theo dõi", int(health["n_videos"]))
st.sidebar.metric("Lượt quan sát", int(health["n_obs"]))
st.sidebar.metric("Số lần thu", int(health["n_runs"]))
if health["n_quarantined"]:
    st.sidebar.warning(f"🔒 {int(health['n_quarantined'])} batch bị cách ly")

page = st.sidebar.radio("Màn hình", [
    "1 · Nguồn & độ tươi",
    "2 · Video & khoảng quan sát",
    "3 · Bảng xếp hạng tăng trưởng",
])

st.sidebar.caption(
    "Mọi số liệu đến từ SQL (`yti.v_video_growth`). "
    "Dashboard KHÔNG tự tính toán."
)


# =============================================================================
# MÀN HÌNH 1 - NGUỒN & ĐỘ TƯƠI
# =============================================================================
if page.startswith("1"):
    st.title("Nguồn dữ liệu & trạng thái thu thập")
    st.caption("Trả lời: *hệ thống có đang sống không, và mỗi kênh cập nhật tới đâu?*")

    channels = q("""
        SELECT c.channel_id, c.title, c.group_code, c.enabled,
               count(DISTINCT v.video_id)        AS videos,
               count(o.observation_id)           AS observations,
               max(o.observed_at)                AS last_observed
          FROM yti.tracked_channels c
          LEFT JOIN yti.videos_current v USING (channel_id)
          LEFT JOIN yti.video_observations o ON o.video_id = v.video_id
         GROUP BY c.channel_id, c.title, c.group_code, c.enabled
         ORDER BY c.group_code, c.title;
    """)

    if channels.empty:
        st.info("Chưa có kênh nào. Chạy `cli collect` để bắt đầu.")
    else:
        channels["độ tươi"] = channels["last_observed"].apply(age_text)
        st.dataframe(
            channels[["title", "group_code", "videos", "observations", "độ tươi", "enabled"]]
            .rename(columns={"title": "Kênh", "group_code": "Nhóm", "videos": "Video",
                             "observations": "Quan sát", "enabled": "Đang bật"}),
            use_container_width=True, hide_index=True,
        )

    st.subheader("Các lần thu gần nhất")
    runs = q("""
        SELECT r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
               r.status, r.code_version,
               count(b.batch_id) FILTER (WHERE b.quality_status = 'passed')      AS passed,
               count(b.batch_id) FILTER (WHERE b.quality_status = 'quarantined') AS quarantined,
               sum(b.received_count) AS rows_received
          FROM yti.collection_runs r
          LEFT JOIN yti.channel_batches b USING (collection_id)
         GROUP BY r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
                  r.status, r.code_version
         ORDER BY r.started_at DESC LIMIT 15;
    """)
    if not runs.empty:
        # Hiện CẢ scheduled_for LẪN started_at: chênh lệch giữa chúng là ĐỘ TRỄ
        # của scheduler - số liệu vận hành quý mà nhiều dashboard bỏ qua.
        runs["trễ (phút)"] = (
            (runs["started_at"] - runs["scheduled_for"]).dt.total_seconds() / 60
        ).round(1)
        st.dataframe(
            runs[["scheduled_for", "started_at", "trễ (phút)", "status",
                  "passed", "quarantined", "rows_received", "code_version"]]
            .rename(columns={"scheduled_for": "Lịch hẹn", "started_at": "Thực chạy",
                             "status": "Trạng thái", "passed": "Pass",
                             "quarantined": "Cách ly", "rows_received": "Dòng",
                             "code_version": "Phiên bản code"}),
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "**Lịch hẹn** là ô lịch mà lần chạy đại diện; **Thực chạy** là lúc nó "
            "bắt đầu thật. Chênh lệch = độ trễ của scheduler."
        )


# =============================================================================
# MÀN HÌNH 2 - VIDEO & KHOẢNG QUAN SÁT
# =============================================================================
elif page.startswith("2"):
    st.title("Video & khoảng quan sát")
    st.caption("Trả lời: *mỗi video đã được quan sát bao nhiêu lần, và có đủ để tính tăng trưởng chưa?*")

    growth = q("SELECT * FROM yti.v_video_growth;")
    if growth.empty:
        st.info("Chưa có dữ liệu.")
        st.stop()

    # ⭐ ĐỘ BAO PHỦ TRƯỚC, DỮ LIỆU SAU.
    # Người xem phải biết mình đang nhìn bao nhiêu phần của sự thật TRƯỚC khi
    # nhìn vào con số, không phải sau.
    total = len(growth)
    rankable = int(growth["is_rankable"].sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Tổng video", total)
    c2.metric("Tính được tăng trưởng", rankable, f"{rankable/total:.0%}")
    c3.metric("Chưa tính được", total - rankable)

    st.subheader("Vì sao video không tính được tăng trưởng?")
    reasons = (growth["validity_reason"].fillna("chưa có quan sát")
               .value_counts().rename_axis("Lý do").reset_index(name="Số video"))
    GIAI_THICH = {
        "ok": "Đủ điều kiện xếp hạng",
        "insufficient_history": "Mới có 1 lần quan sát — cần đợi lần thu kế tiếp",
        "window_too_short": "Hai lần quan sát quá gần nhau (< 18 giờ)",
        "window_too_long": "Hai lần quan sát quá xa nhau (> 30 giờ) — có khoảng trống?",
        "missing_view_count": "YouTube không trả về view_count",
        "view_count_decreased": "View GIẢM — YouTube điều chỉnh/lọc view giả",
        "chưa có quan sát": "Video có trong danh mục nhưng chưa được thu lần nào",
    }
    reasons["Nghĩa là gì"] = reasons["Lý do"].map(GIAI_THICH).fillna("—")
    st.dataframe(reasons, use_container_width=True, hide_index=True)

    st.subheader("Chi tiết từng video")
    groups = ["(tất cả)"] + sorted(growth["group_code"].dropna().unique().tolist())
    sel = st.selectbox("Nhóm", groups)
    view = growth if sel == "(tất cả)" else growth[growth["group_code"] == sel]

    st.dataframe(
        view[["video_title", "channel_title", "format_label",
              "start_observed_at", "end_observed_at", "elapsed_hours",
              "start_view_count", "end_view_count", "delta_views",
              "views_per_hour", "validity_reason"]]
        .rename(columns={
            "video_title": "Video", "channel_title": "Kênh", "format_label": "Loại",
            "start_observed_at": "Quan sát đầu", "end_observed_at": "Quan sát cuối",
            "elapsed_hours": "Giờ", "start_view_count": "View đầu",
            "end_view_count": "View cuối", "delta_views": "Tăng",
            "views_per_hour": "View/giờ", "validity_reason": "Trạng thái"}),
        use_container_width=True, hide_index=True,
    )


# =============================================================================
# MÀN HÌNH 3 - BẢNG XẾP HẠNG
# =============================================================================
else:
    st.title("Bảng xếp hạng tăng trưởng")
    st.caption("Trả lời: *video nào đang tăng view nhanh nhất TRONG TẬP ĐANG THEO DÕI?*")

    # ⚠️ TUYÊN BỐ PHẠM VI - bắt buộc, không phải trang trí.
    # 200 video của 4 kênh KHÔNG đại diện cho YouTube. Bảng xếp hạng không nói
    # rõ phạm vi sẽ bị hiểu thành "video hot nhất YouTube" - một tuyên bố sai.
    st.warning(
        "**Phạm vi:** chỉ gồm video trong 4 kênh đang theo dõi, mỗi kênh tối đa "
        "50 video mới nhất. **Không** đại diện cho YouTube nói chung."
    )

    growth = q("SELECT * FROM yti.v_video_growth WHERE is_rankable = true;")
    all_rows = q("SELECT count(*) AS n FROM yti.v_video_growth;").iloc[0]["n"]

    if growth.empty:
        # Không có dữ liệu thì NÓI RÕ VÌ SAO, không hiện bảng trống.
        st.info(
            "Chưa video nào đủ điều kiện xếp hạng.\n\n"
            "Cần **2 lần quan sát cách nhau 18–30 giờ**. "
            "Lịch thu là 1 lần/ngày, nên hãy đợi lần chạy kế tiếp."
        )
        st.stop()

    c1, c2 = st.columns(2)
    c1.metric("Video trong bảng xếp hạng", len(growth))
    c2.metric("Độ bao phủ", f"{len(growth)/all_rows:.0%}",
              help="Tỉ lệ video đủ điều kiện trên tổng số video theo dõi")

    top_n = st.slider("Số video mỗi nhóm", 3, 20, 5)

    for group in sorted(growth["group_code"].unique()):
        sub = (growth[growth["group_code"] == group]
               .sort_values("views_per_hour", ascending=False)
               .head(top_n))
        st.subheader(f"Nhóm: {group}")
        st.dataframe(
            sub[["video_title", "channel_title", "views_per_hour", "delta_views",
                 "elapsed_hours", "end_view_count", "end_like_count", "format_label"]]
            .rename(columns={
                "video_title": "Video", "channel_title": "Kênh",
                "views_per_hour": "View/giờ", "delta_views": "Tăng",
                "elapsed_hours": "Trong (giờ)", "end_view_count": "Tổng view",
                "end_like_count": "Like", "format_label": "Loại"}),
            use_container_width=True, hide_index=True,
        )
        st.bar_chart(sub.set_index("video_title")["views_per_hour"])

    st.caption(
        "**Cách tính:** `views_per_hour = (view cuối − view đầu) / số giờ giữa hai "
        "lần quan sát`. Đây là **tốc độ trung bình giữa hai lần đo**, không phải "
        "lượt xem thật từng giờ. Video thiếu dữ liệu bị **loại khỏi bảng**, "
        "không bị gán 0."
    )
