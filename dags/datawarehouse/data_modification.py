import logging

logger = logging.getLogger(__name__)
table = "yt_api"


# ### [NÂNG CẤP] Một hàm UPSERT thay cho cả insert_rows + update_rows + delete_rows.
# Vì đã chọn snapshot_date: mỗi video mỗi ngày một dòng.
#   - Ngày mới  -> ON CONFLICT không kích hoạt -> INSERT dòng mới (thêm lịch sử)
#   - Chạy lại cùng ngày -> ON CONFLICT kích hoạt -> UPDATE (idempotent, không trùng)
# KHÔNG còn delete: xóa dòng cũ = phá lịch sử.
def upsert_row(cur, conn, schema, row):

    try:
        if schema == "staging":
            # Key trong row là dạng raw từ extract: video_id, title, publishedAt...
            # row phải có thêm "snapshot_date" (do bước load thêm vào)
            video_id = row["video_id"]
            cur.execute(
                f"""
                INSERT INTO {schema}.{table}
                    ("Video_ID", "Snapshot_Date", "Video_Title", "Upload_Date",
                     "Duration", "Video_Views", "Likes_Count", "Comments_Count")
                VALUES
                    (%(video_id)s, %(snapshot_date)s, %(title)s, %(publishedAt)s,
                     %(duration)s, %(viewCount)s, %(likeCount)s, %(commentCount)s)
                ON CONFLICT ("Video_ID", "Snapshot_Date") DO UPDATE SET
                    "Video_Title"    = EXCLUDED."Video_Title",
                    "Video_Views"    = EXCLUDED."Video_Views",
                    "Likes_Count"    = EXCLUDED."Likes_Count",
                    "Comments_Count" = EXCLUDED."Comments_Count";
                """,
                row,
            )
        else:
            # Bảng production: key đã ở dạng TitleCase sau transform,
            # row phải có thêm "Snapshot_Date"
            video_id = row["Video_ID"]
            cur.execute(
                f"""
                INSERT INTO {schema}.{table}
                    ("Video_ID", "Snapshot_Date", "Video_Title", "Upload_Date",
                     "Duration", "Video_Type", "Video_Views", "Likes_Count", "Comments_Count")
                VALUES
                    (%(Video_ID)s, %(Snapshot_Date)s, %(Video_Title)s, %(Upload_Date)s,
                     %(Duration)s, %(Video_Type)s, %(Video_Views)s, %(Likes_Count)s, %(Comments_Count)s)
                ON CONFLICT ("Video_ID", "Snapshot_Date") DO UPDATE SET
                    "Video_Title"    = EXCLUDED."Video_Title",
                    "Video_Type"     = EXCLUDED."Video_Type",
                    "Video_Views"    = EXCLUDED."Video_Views",
                    "Likes_Count"    = EXCLUDED."Likes_Count",
                    "Comments_Count" = EXCLUDED."Comments_Count";
                """,
                row,
            )

        conn.commit()
        logger.info("Upserted row Video_ID=%s (snapshot)", video_id)

    except Exception as e:
        logger.error("Error upserting row: %s", e)
        raise