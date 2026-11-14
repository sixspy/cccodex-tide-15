"""去重键的组成定义。

只支持两种组成：
    站码 + 观测时刻
    站码 + 观测时刻 + 质量旗

键的组成在当天保存后钉住，归集时携带的键必须与钉住的组成一致。
"""

from datetime import datetime, timedelta, timezone

STATION = "station_code"
OBSERVED = "observed_at"
FLAG = "quality_flag"

# 顺序即页面与存库中的展示顺序，不接受调换或增删。
OPTION_PARTS = (
    (STATION, OBSERVED),
    (STATION, OBSERVED, FLAG),
)

KEY_NAMES = {
    OPTION_PARTS[0]: "站时键",
    OPTION_PARTS[1]: "站时旗键",
}

PART_LABELS = {
    STATION: "站码",
    OBSERVED: "观测时刻",
    FLAG: "质量旗",
}

# 港务水文组按北京时间记日。
LOCAL_TZ = timezone(timedelta(hours=8))


def today_local():
    return datetime.now(LOCAL_TZ).date()


def normalize_parts(parts):
    """把外部传入的键组成归一成受支持的组合；不合法时返回 None。"""
    if not isinstance(parts, (list, tuple)):
        return None
    cleaned = tuple(str(part).strip() for part in parts)
    return cleaned if cleaned in OPTION_PARTS else None


def parts_label(parts):
    parts = tuple(parts)
    return " + ".join(PART_LABELS.get(part, str(part)) for part in parts)


def name_for_parts(parts):
    return KEY_NAMES.get(tuple(parts), parts_label(parts))


def key_signature(parts):
    """键组成的紧凑签名，用于比对携带键与键设置。"""
    return ":".join(tuple(parts))


def option_payload():
    return [
        {"parts": list(parts), "label": parts_label(parts), "name": name_for_parts(parts)}
        for parts in OPTION_PARTS
    ]


def key_from_parts(parts, values):
    """生成存库用的去重键字符串。

    带上组成的名称，避免不同组成恰好拼出相同字符串。
    """
    tokens = []
    for part in parts:
        if part == STATION:
            tokens.append(str(values["station_code"]))
        elif part == OBSERVED:
            observed = values["observed_at"]
            tokens.append(observed.strftime("%Y%m%d%H%M"))
        elif part == FLAG:
            flag = values.get("quality_flag")
            if not flag:
                return None
            tokens.append(str(flag))
        else:
            return None
    return f"{key_signature(parts)}=" + "|".join(tokens)


def _stamp(observed):
    return f"{observed:%Y-%m-%d %H:%M}"


def identity_label(parts, identity):
    """冲突队列里展示的“被挡住的组合”。"""
    station = identity.get("station_code")
    observed = identity.get("observed_at")
    flag = identity.get("quality_flag")
    if station and observed:
        label = f"{station} · {_stamp(observed)}"
        if FLAG in tuple(parts) and flag:
            label += f" · {flag}"
        return label
    if station:
        return f"{station} · 缺观测时刻"
    if observed:
        return f"缺站码 · {_stamp(observed)}"
    return "缺站码 · 缺观测时刻"
