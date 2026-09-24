ARG AIRFLOW_VERSION=2.9.2
ARG PYTHON_VERSION=3.11
FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}
ENV AIRFLOW_HOME=/opt/airflow

# ============================================================================
# THỨ TỰ LAYER CÓ CHỦ Ý - từ ÍT đổi nhất đến HAY đổi nhất.
# Docker cache theo layer: một layer đổi thì MỌI layer sau nó build lại.
#   requirements.txt  -> đổi vài tháng một lần   -> đặt TRƯỚC
#   src/              -> đổi mỗi ngày            -> đặt SAU
# Đảo ngược thứ tự thì mỗi lần sửa một dòng code là cài lại toàn bộ thư viện
# (5-10 phút) thay vì vài giây.
# ============================================================================

# ---- Layer 1: thư viện (ít đổi) --------------------------------------------
COPY requirements.txt /
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /requirements.txt

# ---- Layer 2: code của ta (hay đổi) ----------------------------------------
# ⭐ [Bước 13] COPY code VÀO image - trả nợ "src là bind mount".
# Trước đây src/ chỉ được mount lúc chạy -> image KHÔNG TỰ CHỨA code ->
# đem image sang máy khác là chạy không được. Image phải là ARTIFACT HOÀN CHỈNH.
#
# --no-deps: thư viện đã cài ở Layer 1. Để pip tự giải dependency lần nữa thì
# nó có thể nâng/hạ phiên bản thư viện mà Airflow đang ghim -> vỡ Airflow.
#
# Ở MÁY DEV, docker-compose vẫn mount ./src và đặt PYTHONPATH=/opt/airflow/src.
# PYTHONPATH được ưu tiên hơn site-packages -> code mount ĐÈ LÊN code trong
# image -> sửa code là thấy ngay, không phải build lại.
# Ở PRODUCTION, không mount gì -> dùng code đã nướng trong image.
# Cùng một image, hai chế độ - không cần hai Dockerfile.
COPY --chown=airflow:root pyproject.toml /opt/youtube_intel/
COPY --chown=airflow:root src/ /opt/youtube_intel/src/
RUN pip install --no-cache-dir --no-deps /opt/youtube_intel

# ---- Layer 3: phiên bản (đổi MỖI lần build) - đặt CUỐI ----------------------
# Vì sao cuối? ARG đổi giá trị là layer đó và mọi layer sau bị vô hiệu cache.
# GIT_SHA đổi mỗi commit -> đặt cuối để không phá cache của 2 layer trên.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV APP_VERSION=${GIT_SHA}
ENV APP_BUILD_TIME=${BUILD_TIME}
