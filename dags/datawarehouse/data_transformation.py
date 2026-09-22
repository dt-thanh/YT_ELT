from datetime import timedelta, datetime


def parse_duration(duration_str):

    duration_str = duration_str.replace("P", "").replace("T", "")

    components = ["D", "H", "M", "S"]
    values = {"D": 0, "H": 0, "M": 0, "S": 0}

    for component in components:
        if component in duration_str:
            value, duration_str = duration_str.split(component)
            values[component] = int(value)

    total_duration = timedelta(
        days=values["D"], hours=values["H"], minutes=values["M"], seconds=values["S"]
    )

    return total_duration


def transform_data(row):

    duration_td = parse_duration(row["Duration"])

    row["Duration"] = (datetime.min + duration_td).time()

    row["Video_Type"] = classify_video_type(duration_td)

    return row


def classify_video_type(duration_td):
    # YouTube trả "P0D" cho livestream đang phát / sắp phát: độ dài CHƯA XÁC ĐỊNH,
    # không phải "dài 0 giây". Phải tách nhánh này TRƯỚC, nếu không một livestream
    # 3 tiếng sẽ bị xếp nhầm vào "Shorts" và làm sai mọi phân tích Shorts vs Normal.
    total_seconds = duration_td.total_seconds()

    if total_seconds == 0:
        return "Live"

    # Shorts theo định nghĩa của YouTube: tối đa 60 giây.
    if total_seconds <= 60:
        return "Shorts"

    return "Normal"