from airflow.providers.postgres.hooks.postgres import PostgresHook
from psycopg2.extras import RealDictCursor

table = "yt_api"


def get_conn_cursor():
    # Nối Postgres qua Connection "postgres_db_yt_elt" đã khai trong docker-compose
    # -> không hardcode host/user/password, Airflow tự tra.
    hook = PostgresHook(postgres_conn_id="postgres_db_yt_elt", database="elt_db")
    conn = hook.get_conn()
    # RealDictCursor: mỗi dòng trả về dạng dict -> đọc row["Video_ID"] cho dễ
    cur = conn.cursor(cursor_factory=RealDictCursor)
    return conn, cur


def close_conn_cursor(conn, cur):
    # Luôn đóng kết nối, tránh rò slot của Postgres
    cur.close()
    conn.close()


def create_schema(schema):
    conn, cur = get_conn_cursor()

    # Schema = "thư mục" chứa bảng. Khóa tạo 2 schema: staging và production.
    schema_sql = f"CREATE SCHEMA IF NOT EXISTS {schema};"

    cur.execute(schema_sql)
    conn.commit()
    close_conn_cursor(conn, cur)


def create_table(schema):
    conn, cur = get_conn_cursor()

    if schema == "staging":
        # Bảng STAGING: giữ dữ liệu gần-raw (Duration vẫn là chuỗi PT26M10S)
        table_sql = f"""
                CREATE TABLE IF NOT EXISTS {schema}.{table} (
                    "Video_ID" VARCHAR(11) NOT NULL,
                    "Snapshot_Date" DATE NOT NULL,           -- ### [NÂNG CẤP] ngày chụp số liệu (= ngày partition trong MinIO)
                    "Video_Title" TEXT NOT NULL,
                    "Upload_Date" TIMESTAMP NOT NULL,
                    "Duration" VARCHAR(20) NOT NULL,
                    "Video_Views" BIGINT,                    -- ### [NÂNG CẤP] INT -> BIGINT (video >2 tỷ view sẽ tràn INT)
                    "Likes_Count" BIGINT,                    -- ### [NÂNG CẤP]
                    "Comments_Count" BIGINT,                 -- ### [NÂNG CẤP]
                    PRIMARY KEY ("Video_ID", "Snapshot_Date") -- ### [NÂNG CẤP] khóa kép: mỗi video MỖI NGÀY một dòng
                );
            """
    else:
        # Bảng PRODUCTION (final): đã làm sạch (Duration ép sang TIME, thêm Video_Type)
        table_sql = f"""
                  CREATE TABLE IF NOT EXISTS {schema}.{table} (
                      "Video_ID" VARCHAR(11) NOT NULL,
                      "Snapshot_Date" DATE NOT NULL,          -- ### [NÂNG CẤP]
                      "Video_Title" TEXT NOT NULL,
                      "Upload_Date" TIMESTAMP NOT NULL,
                      "Duration" TIME NOT NULL,
                      "Video_Type" VARCHAR(10) NOT NULL,
                      "Video_Views" BIGINT,                   -- ### [NÂNG CẤP]
                      "Likes_Count" BIGINT,                   -- ### [NÂNG CẤP]
                      "Comments_Count" BIGINT,                -- ### [NÂNG CẤP]
                      PRIMARY KEY ("Video_ID", "Snapshot_Date") -- ### [NÂNG CẤP] khóa kép
                  );
              """

    cur.execute(table_sql)
    conn.commit()
    close_conn_cursor(conn, cur)


def get_video_ids(cur, schema):
    # ### [NÂNG CẤP] DISTINCT: giờ mỗi video có nhiều dòng (nhiều ngày) nên phải khử trùng
    cur.execute(f"""SELECT DISTINCT "Video_ID" FROM {schema}.{table};""")
    ids = cur.fetchall()

    video_ids = [row["Video_ID"] for row in ids]

    return video_ids