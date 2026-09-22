"""youtube_intel - lõi nghiệp vụ của YouTube Content Intelligence.

Package này KHÔNG import airflow ở bất cứ đâu. Airflow chỉ là một trong nhiều
cách gọi nó (cách khác: cli.py, pytest, Streamlit). Giữ được ranh giới này thì:
  - test chạy trong mili giây, không cần dựng hạ tầng
  - đổi orchestrator không phải viết lại nghiệp vụ
"""
