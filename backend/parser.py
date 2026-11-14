import re
from datetime import datetime

from dedup import FLAG

STATION = re.compile(r"^[A-Z]{2}\d{2}$")
FLAGS = {"A": "实测", "M": "插补", "B": "可疑"}


def _parse_stamp(stamp: str):
    for fmt in ("%Y%m%d%H%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(stamp, fmt)
        except ValueError:
            continue
    return None


def _row(status, code, reason, raw, station=None, observed=None, tide=None, flag=None):
    return {
        "status": status,
        "code": code,
        "reason": reason,
        "raw_line": raw,
        "station_code": station,
        "observed_at": observed,
        "tide_cm": tide,
        "quality_flag": flag,
        "flag_label": FLAGS.get(flag, flag) if flag else None,
    }


def parse_telegram_line(line: str, parts=None):
    """解析一行报文。

    返回 status：
        ok        可以参与去重与落库
        conflict  键里没有可用的站码或观测时刻（启用旗键时还含质量旗），进冲突队列
        reject    行本身不合格（列数、潮位），进未接纳

    键需要的字段缺失或不可用，这一行就拼不出键，进冲突队列等人工看；
    潮位坏了但键完好的行仍按行本身不合格处理。
    """
    parts = tuple(parts) if parts else ()
    raw = line.strip()
    segments = [part.strip() for part in raw.split("|")]
    if len(segments) != 4:
        return _row("reject", "format", "列数不是四段", raw)
    station, stamp, tide, flag = segments
    flag_value = flag if flag in FLAGS else None

    # 键里缺站码或时刻 -> 冲突队列。
    if not station and not stamp:
        return _row("conflict", "conflict", "键里缺站码和观测时刻", raw, flag=flag_value)
    if not station:
        return _row(
            "conflict", "conflict", "键里缺站码", raw,
            observed=_parse_stamp(stamp), flag=flag_value,
        )
    if not STATION.match(station):
        return _row(
            "conflict", "conflict", "站码不合法，键里没有可用的站码", raw,
            station=station, observed=_parse_stamp(stamp), flag=flag_value,
        )
    if not stamp:
        return _row("conflict", "conflict", "键里缺观测时刻", raw, station=station, flag=flag_value)
    observed = _parse_stamp(stamp)
    if observed is None:
        return _row(
            "conflict", "conflict", "观测时刻不合法，键里没有可用的时刻", raw,
            station=station, flag=flag_value,
        )

    if not re.fullmatch(r"-?\d+", tide):
        return _row("reject", "format", "潮位不是整数厘米", raw, station, observed)
    centimeters = int(tide)
    if centimeters < -500 or centimeters > 2000:
        return _row("reject", "format", "潮位超出量程", raw, station, observed)

    if FLAG in parts:
        # 键含质量旗时，缺旗或旗值不可用同样拼不出键 -> 冲突队列。
        if not flag:
            return _row("conflict", "conflict", "键里缺质量旗", raw, station, observed)
        if flag not in FLAGS:
            return _row(
                "conflict", "conflict", "质量旗不合法，键里没有可用的质量旗", raw,
                station, observed, flag=flag,
            )
    elif flag not in FLAGS:
        return _row("reject", "format", "质量旗不在 A、M、B", raw, station, observed)

    return _row("ok", None, None, raw, station, observed, centimeters, flag)
