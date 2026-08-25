import json     #Dùng để xử lý dữ liệu JSON. 
import logging    #Dùng để ghi log
from datetime import date, timedelta # Dùng để lấy ngày hôm nay, phục vụ đặt tên file:
 # timedelta dùng để cấu hình thời gian retry của Airflow task.
import boto3 #Dùng để kết nối với S3 hoặc hệ tương thích S3 như MinIO.
import requests  # Dùng để gọi API qua internet
from tenacity import (
    retry,  #Decorator để bọc function.
    stop_after_attempt, # Thử tối đa x lan
    wait_exponential,   #Chờ tăng dần giữa các lần retry.
    retry_if_exception_type,   #Chỉ retry nếu lỗi thuộc nhóm request/network/API error của requests
)   # Đây là thư viện giúp retry function. Nếu gọi API lỗi tạm thời, code tự thử lại
from airflow.decorators import task  # Biến function Python thành Airflow task.
from airflow.models import Variable   #Lấy config từ Airflow Variables, ví dụ API key, channel handle, MinIO credent
 
logger = logging.getLogger(__name__)  #tạo logger cho file hiện tại.
 
API_KEY = Variable.get("API_KEY")
CHANNEL_HANDLE = Variable.get("CHANNEL_HANDLE")
MAX_RESULTS = 50

@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def call_api(url: str) -> dict:
#     Gọi API
# → có timeout
# → nếu lỗi thì retry
# → nếu thành công thì trả JSON
    response = requests.get(url, timeout=15) #ếu YouTube API không phản hồi sau 15 giây thì dừng, không đợi mãi.
    response.raise_for_status()
    return response.json() #Dòng này chuyển JSON thành Python dict/list.
# còn ở trên là retry cho call api 
# Tham số retry cho mỗi task (Airflow tự chạy lại nếu cả task ngã)
TASK_ARGS = dict(
    retries=3,
    retry_delay=timedelta(seconds=30),
    retry_exponential_backoff=True,
)

@task(**TASK_ARGS)
# Mỗi YouTube channel có một “playlist đặc biệt” tên là uploads.
# Playlist này tự động chứa tất cả video mà kênh đó đã đăng.
def get_playlist_id() -> str:
    url = (
        "https://youtube.googleapis.com/youtube/v3/channels"
        f"?part=contentDetails&forHandle={CHANNEL_HANDLE}&key={API_KEY}"
    )
    data = call_api(url)
    playlist_id = data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    # trả về ID của uploads playlist. Hàm này trả về 1 chuỗi — id của playlist
    logger.info("Đã lấy playlist uploads: %s", playlist_id)
    return playlist_id

@task(**TASK_ARGS)
# Cầm id playlist ở trên, bạn duyệt qua nó để lấy id của từng video. 
# Nhưng playlist của MrBeast rất dài, YouTube không đưa hết một lúc mà chia thành từng trang 50 cái. 
# Đó là lý do có vòng while True
# Hàm trả về một danh sách dài toàn bộ video id
def get_video_ids(playlist_id: str) -> list[str]:
    video_ids: list[str] = []
    page_token = None
    while True:
        url = (
            "https://youtube.googleapis.com/youtube/v3/playlistItems"
            f"?part=contentDetails&maxResults={MAX_RESULTS}"
            f"&playlistId={playlist_id}&key={API_KEY}"
        )
        if page_token:
            url += f"&pageToken={page_token}"
 
        data = call_api(url)
        for item in data.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
 
        page_token = data.get("nextPageToken")      # trang tiep theo o dau 
        if not page_token:
            break           # het trang dung
 
    logger.info("Tổng số video lấy được: %d", len(video_ids))
    return video_ids

@task(**TASK_ARGS)
# Giờ bạn có danh sách id, bạn hỏi chi tiết từng video (endpoint videos). 
# Nhưng API chỉ cho hỏi tối đa 50 id mỗi lần, 
# nên batch_list cắt danh sách thành từng lô 50
# Với mỗi video, bạn moi ra đúng 7 trường (title, publishedAt, duration, view/like/comment) rồi gom vào extracted_data. 
# Hàm trả về một danh sách các dict, mỗi dict là một video.
def extract_video_data(video_ids: list[str]) -> list[dict]:
    def batch_list(items, size):
        for i in range(0, len(items), size):
            yield items[i : i + size]
 
    extracted_data: list[dict] = []
    for batch in batch_list(video_ids, MAX_RESULTS):
        video_ids_str = ",".join(batch)
        url = (
            "https://youtube.googleapis.com/youtube/v3/videos"
            f"?part=snippet,contentDetails,statistics&id={video_ids_str}&key={API_KEY}"
        )
        data = call_api(url)
        for item in data.get("items", []):
            extracted_data.append({
                "video_id": item["id"],
                "title": item["snippet"]["title"],
                "publishedAt": item["snippet"]["publishedAt"],
                "duration": item["contentDetails"]["duration"],
                "viewCount": item["statistics"].get("viewCount", None),
                "likeCount": item["statistics"].get("likeCount", None),
                "commentCount": item["statistics"].get("commentCount", None),
            })
 
    logger.info("Đã trích xuất chi tiết %d video", len(extracted_data))
    return extracted_data

# @task
# # Đây là "raw JSON" — dữ liệu thô, chưa xử lý gì.

# def save_to_json(extracted_data):

#     file_path = f"./data/YT_data_{date.today()}.json"

#     with open(file_path, "w", encoding="utf-8") as json_outfile:
#         json.dump(extracted_data, json_outfile, indent=4, ensure_ascii=False)

@task(**TASK_ARGS)
def save_to_minio(extracted_data: list[dict]) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=Variable.get("MINIO_ENDPOINT"),       # http://minio:9000
        aws_access_key_id=Variable.get("MINIO_ACCESS_KEY"),
        aws_secret_access_key=Variable.get("MINIO_SECRET_KEY"),
    )
    bucket = "youtube-raw"
    if bucket not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=bucket)
 
    key = f"videos/date={date.today()}/data.json"          # partition theo ngày
    body = json.dumps(extracted_data, indent=4, ensure_ascii=False)
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))
 
    logger.info("Đã đẩy %d video lên s3://%s/%s", len(extracted_data), bucket, key)
    return key