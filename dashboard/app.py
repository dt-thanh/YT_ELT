"""
Dashboard Streamlit - 6 màn hình.

NGUYÊN TẮC SỐ MỘT: DASHBOARD KHÔNG TÍNH TOÁN.
Mọi con số hiển thị ở đây đều đến từ SQL (chủ yếu là VIEW v_video_growth và
yti.v_llm_daily) hoặc từ hàm lõi (trợ lý). Streamlit chỉ ĐỌC và VẼ.

Vì sao nghiêm ngặt thế? Vì nếu dashboard tự tính views_per_hour bằng Python,
ta sẽ có HAI công thức cho cùng một chỉ số: một trong SQL, một trong Python.
Chúng sẽ lệch nhau - và người xem không biết tin cái nào. Đây là lỗi kinh điển
khiến "báo cáo của phòng A khác báo cáo của phòng B" dù cùng một database.
MỘT chỉ số = MỘT định nghĩa = MỘT nơi.

NGUYÊN TẮC SỐ HAI: LUÔN HIỂN THỊ ĐỘ BAO PHỦ VÀ ĐỘ TƯƠI.

Every query lives in dashboard/queries.py (named functions), so tests can swap
in a fake module and run every screen without a database or an LLM.

CHẠY:
    streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

# Cho phép import package (src/) và queries.py khi chạy streamlit từ thư mục gốc
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import queries as Q                                    # noqa: E402
from youtube_intel.reporting import render_markdown   # noqa: E402

st.set_page_config(page_title="YouTube Content Intelligence",
                   page_icon="📊", layout="wide")


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
    CFG = Q.config()
    health = Q.health()
except Exception as e:
    st.error(f"Không kết nối được database: {e}")
    st.info("Kiểm tra: `docker compose ps` — postgres đã chạy chưa? "
            "Chạy ngoài Docker thì POSTGRES_CONN_HOST phải là `localhost`.")
    st.stop()

last_obs_age = age_text(health["last_obs"])
is_stale = (health["last_obs"] is not None and not pd.isna(health["last_obs"])
            and (datetime.now(timezone.utc) - health["last_obs"]).total_seconds() > 30 * 3600)

# ⭐ ĐỘ TƯƠI LÀ THỨ ĐẦU TIÊN NGƯỜI XEM PHẢI THẤY.
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
    "4 · Bản tin AI",
    "5 · Trợ lý AI",
    "6 · LLM Observability",
])

st.sidebar.caption(
    "Mọi số liệu đến từ SQL (`yti.v_video_growth`, `yti.v_llm_daily`). "
    "Dashboard KHÔNG tự tính toán."
)


# =============================================================================
# MÀN HÌNH 1 - NGUỒN & ĐỘ TƯƠI
# =============================================================================
if page.startswith("1"):
    st.title("Nguồn dữ liệu & trạng thái thu thập")
    st.caption("Trả lời: *hệ thống có đang sống không, và mỗi kênh cập nhật tới đâu?*")

    channels = Q.channels()

    if channels.empty:
        st.info("Chưa có kênh nào. Chạy `cli collect` để bắt đầu.")
    else:
        channels["độ tươi"] = channels["last_observed"].apply(age_text)
        st.dataframe(
            channels[["title", "group_code", "videos", "observations", "độ tươi", "enabled"]]
            .rename(columns={"title": "Kênh", "group_code": "Nhóm", "videos": "Video",
                             "observations": "Quan sát", "enabled": "Đang bật"}),
            width="stretch", hide_index=True,
        )

    st.subheader("Các lần thu gần nhất")
    runs = Q.runs()
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
            width="stretch", hide_index=True,
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

    growth = Q.growth()
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
    st.dataframe(reasons, width="stretch", hide_index=True)

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
        width="stretch", hide_index=True,
    )


# =============================================================================
# MÀN HÌNH 3 - BẢNG XẾP HẠNG
# =============================================================================
elif page.startswith("3"):
    st.title("Bảng xếp hạng tăng trưởng")
    st.caption("Trả lời: *video nào đang tăng view nhanh nhất TRONG TẬP ĐANG THEO DÕI?*")

    # ⚠️ TUYÊN BỐ PHẠM VI - bắt buộc, không phải trang trí.
    # 200 video của 4 kênh KHÔNG đại diện cho YouTube. Bảng xếp hạng không nói
    # rõ phạm vi sẽ bị hiểu thành "video hot nhất YouTube" - một tuyên bố sai.
    # Con số lấy từ CONFIG, không viết cứng: thêm kênh thì câu này vẫn đúng.
    st.warning(
        f"**Phạm vi:** chỉ gồm video trong {len(CFG.enabled_channels())} kênh đang "
        f"theo dõi, mỗi kênh tối đa {CFG.settings.initial_video_limit_per_channel} "
        "video mới nhất. **Không** đại diện cho YouTube nói chung."
    )

    growth = Q.growth_rankable()
    all_rows = Q.growth_count()

    if growth.empty:
        # Không có dữ liệu thì NÓI RÕ VÌ SAO, không hiện bảng trống.
        st.info(
            "Chưa video nào đủ điều kiện xếp hạng.\n\n"
            "Cần **2 lần quan sát cách nhau 18–30 giờ**. "
            f"Lịch thu là {24 // CFG.settings.snapshot_hours} lần/ngày, "
            "nên hãy đợi lần chạy kế tiếp."
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
            width="stretch", hide_index=True,
        )
        st.bar_chart(sub.set_index("video_title")["views_per_hour"])

    st.caption(
        "**Cách tính:** `views_per_hour = (view cuối − view đầu) / số giờ giữa hai "
        "lần quan sát`. Đây là **tốc độ trung bình giữa hai lần đo**, không phải "
        "lượt xem thật từng giờ. Video thiếu dữ liệu bị **loại khỏi bảng**, "
        "không bị gán 0."
    )


# =============================================================================
# MÀN HÌNH 4 - BẢN TIN AI
# =============================================================================
elif page.startswith("4"):
    st.title("Bản tin AI theo nhóm")
    st.caption("Trả lời: *bản tin nói gì, ai viết (LLM hay bản mẫu), tốn bao nhiêu, và dựa trên bằng chứng nào?*")

    reports = Q.reports()
    if reports.empty:
        st.info("Chưa có bản tin nào. DAG `yt_report` tự chạy sau mỗi lần thu, "
                "hoặc chạy tay: `youtube-intel report`.")
        st.stop()

    c1, c2 = st.columns(2)
    group = c1.selectbox("Nhóm", sorted(reports["group_code"].unique()))
    mine = reports[reports["group_code"] == group]
    labels = [f"{r.as_of:%Y-%m-%d %H:%M} UTC · {r.status}" for r in mine.itertuples()]
    pick = c2.selectbox("Ngày", range(len(labels)), format_func=lambda i: labels[i])
    r = mine.iloc[pick]

    attempts = Q.report_attempts(r["report_id"]) or {}
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Viết bởi", r["model_name"] or "bản mẫu")
    k2.metric("Prompt", r["prompt_version"] or "—")
    k3.metric("Token", f"{int(r['tokens_used']):,}" if pd.notna(r["tokens_used"]) else "—")
    k4.metric("Latency", f"{int(r['latency_ms']):,} ms" if pd.notna(r["latency_ms"]) else "—")
    k5.metric("Số lần gọi LLM", int(attempts.get("calls") or 0) or "—")
    if r["status"] == "validated":
        st.success("✅ Đã qua validator" + (f" · {attempts['validations']}" if attempts.get("validations") else ""))
    else:
        st.warning("📝 Bản mẫu (draft): LLM tắt, thiếu key, hoặc hỏng 2 lần rồi rơi về bản mẫu"
                   + (f" · {attempts['validations']}" if attempts.get("validations") else ""))

    detail = Q.report_detail(r["report_id"])
    if detail:
        # KHÔNG tính gì ở đây: số liệu render từ evidence đã lưu cùng bản tin,
        # bằng CHÍNH hàm mà lệnh `report --show` dùng.
        st.markdown(render_markdown(detail["validated_payload"], detail["evidence_bundle"]))
        with st.expander("Bằng chứng đã đưa cho LLM (evidence bundle)"):
            st.caption(f"input_hash: `{r['input_hash'] or '—'}`")
            st.json(detail["evidence_bundle"])


# =============================================================================
# MÀN HÌNH 5 - TRỢ LÝ AI
# =============================================================================
elif page.startswith("5"):
    st.title("Trợ lý AI")
    st.caption("Hỏi về các kênh đang theo dõi. Trợ lý chỉ dùng công cụ truy vấn **chỉ đọc**; "
               "mọi con số được **kiểm chứng** với kết quả công cụ trước khi hiển thị.")
    st.info(f"**Phạm vi:** chỉ {len(CFG.enabled_channels())} kênh đang theo dõi. "
            "Không dự đoán viral, không trả lời về YouTube nói chung.")

    assistant, why_not = Q.assistant()
    if assistant is None:
        st.warning("Trợ lý chưa được cấu hình.")
        st.markdown(
            "Cần hai biến môi trường trong `.env`, rồi `docker compose up -d`:\n"
            "- `OPENAI_API_KEY`\n"
            "- `ASSISTANT_DB_PASSWORD` (login chỉ đọc, đặt khi chạy migration 005)\n\n"
            f"Chi tiết: `{why_not}`")
        st.stop()

    state = st.session_state
    state.setdefault("chat", [])                       # [(question, AssistantAnswer)]
    state.setdefault("session_id", str(uuid.uuid4()))

    STATUS = {"answered": ("✅", "đã kiểm chứng"), "declined": ("🚫", "từ chối (ngoài phạm vi)"),
              "fallback": ("⚠️", "không qua kiểm chứng: hiển thị số liệu gốc"),
              "failed": ("❌", "lỗi")}

    def show(ans):
        st.markdown(ans.text)
        icon, label = STATUS.get(ans.status, ("", ans.status))
        if ans.status == "answered" and ans.repaired:
            label = "đã kiểm chứng (sau một lần sửa)"
        cost = f"~${ans.est_cost_usd:.5f}" if ans.est_cost_usd is not None else "chi phí: —"
        st.caption(f"{icon} {label} · dữ liệu tới {ans.data_as_of or '—'} · "
                   f"{(ans.input_tokens or 0) + (ans.output_tokens or 0):,} token · {cost} · "
                   f"{ans.latency_ms:,} ms")
        if ans.citations:
            st.markdown("**Nguồn:** " + " · ".join(
                f"[{c.title or c.video_id}]({c.url})" for c in ans.citations))
        if ans.data_gaps:
            st.markdown("**Thiếu dữ liệu:** " + "; ".join(ans.data_gaps))
        with st.expander("Trace"):
            if ans.tool_trace:
                st.dataframe(pd.DataFrame([{
                    "bước": t.step, "công cụ": t.tool,
                    "tham số": json.dumps(t.args, ensure_ascii=False),
                    "số dòng": t.row_count, "ms": t.latency_ms, "lỗi": t.error or ""}
                    for t in ans.tool_trace]), width="stretch", hide_index=True)
            else:
                st.caption("Không gọi công cụ nào.")
            for e in ans.validation_errors:
                st.caption(f"Kiểm chứng: {e}")

    for question, ans in state.chat:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            show(ans)

    samples = ["Video nào tăng view nhanh nhất nhóm kids_animation?",
               "So sánh các kênh nhóm books_learning theo trung vị view mỗi giờ",
               "Dữ liệu cập nhật lần cuối khi nào?",
               "Có video nào về thói quen hoặc quản lý thời gian không?",
               "Bản tin mới nhất của nhóm books_learning nói gì?"]
    clicked = None
    if not state.chat:
        st.caption("Gợi ý:")
        cols = st.columns(len(samples))
        for col, s_ in zip(cols, samples):
            if col.button(s_):
                clicked = s_

    question = st.chat_input("Hỏi về dữ liệu YouTube đang theo dõi...") or clicked
    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Đang tra cứu..."):
                history = [(q_, a.text) for q_, a in state.chat]
                ans = assistant.ask(question, history=history, session_id=state.session_id)
            show(ans)
        state.chat.append((question, ans))


# =============================================================================
# MÀN HÌNH 6 - LLM OBSERVABILITY
# =============================================================================
else:
    st.title("LLM Observability")
    st.caption("Trả lời: *gọi LLM bao nhiêu lần, tốn bao nhiêu, chậm cỡ nào, hỏng ở đâu?* "
               "Mọi tổng hợp tính bằng SQL trên `yti.llm_calls` / `yti.v_llm_daily`.")

    c1, c2 = st.columns([1, 2])
    days = c1.slider("Số ngày gần nhất", 1, 90, 14)
    all_features = ("report", "assistant", "embedding", "eval")
    features = tuple(c2.multiselect("Tính năng", all_features, default=list(all_features))) or all_features

    s = Q.llm_summary(days, features) or {}
    def pct(v):
        return "—" if v is None or pd.isna(v) else f"{float(v):.1%}"
    m = st.columns(6)
    m[0].metric("Lần gọi", int(s.get("calls") or 0))
    m[1].metric("Chi phí ước tính", "—" if s.get("est_cost_usd") is None or pd.isna(s.get("est_cost_usd"))
                else f"${float(s['est_cost_usd']):.4f}")
    m[2].metric("p95 latency", "—" if s.get("latency_p95_ms") is None or pd.isna(s.get("latency_p95_ms"))
                else f"{float(s['latency_p95_ms']):,.0f} ms")
    m[3].metric("Tỉ lệ lỗi", pct(s.get("error_rate")))
    m[4].metric("Tỉ lệ fallback", pct(s.get("fallback_rate")))
    m[5].metric("Validation thất bại", pct(s.get("validation_fail_rate")))

    daily = Q.llm_daily(days, features)
    if daily.empty:
        st.info("Chưa có lần gọi LLM nào trong khoảng này. Chạy `youtube-intel report` "
                "hoặc hỏi trợ lý để có dữ liệu.")
    else:
        t1, t2 = st.columns(2)
        t1.subheader("Token theo ngày")
        t1.line_chart(daily.pivot_table(index="day", columns="feature", values="tokens",
                                        aggfunc="sum").fillna(0))
        t2.subheader("Chi phí theo ngày (USD)")
        t2.line_chart(daily.pivot_table(index="day", columns="feature", values="est_cost_usd",
                                        aggfunc="sum").fillna(0))

    st.subheader("Theo model × prompt version")
    by_model = Q.llm_by_model(days, features)
    if by_model.empty:
        st.caption("Không có dữ liệu.")
    else:
        st.dataframe(by_model, width="stretch", hide_index=True)

    st.subheader("20 lỗi gần nhất (lỗi API, validation thất bại, fallback)")
    errors = Q.llm_errors(days, features)
    if errors.empty:
        st.caption("Không có lỗi nào trong khoảng này.")
    else:
        st.dataframe(errors, width="stretch", hide_index=True)

    st.subheader("Kết quả eval mới nhất mỗi phiên bản")
    st.caption("Đọc từ `evals/results/*.json` - con số do lệnh eval ghi lại, không tính lại ở đây. "
               "Eval chạy trên dữ liệu tổng hợp cố định.")
    evals = Q.eval_results()
    if evals.empty:
        st.caption("Chưa có file kết quả eval.")
    else:
        st.dataframe(evals, width="stretch", hide_index=True)
