# -*- coding: utf-8 -*-
"""字段校验 + 相对时间归一化（纯规则，不依赖大模型，稳定可跑）。

这两块对看板准确性影响很大：
- 相对时间（昨天 / 3天前 / 上周五 / 下周一 / 3月5号）若不归一成绝对日期，
  时间轴、阶段停滞天数都会算错；
- 字段（手机号 / 邮箱 / 阶段 / 状态）若格式非法，看板会展示脏数据、漏斗统计失真。

对外暴露：
- normalize_time(value, ref=None) -> 规范字符串或原值
- validate_record(r) -> [ {"field","msg","level"} ]
- clean_record(r, ref=None) -> 原地处理并返回（归一化 time + 写入 r["issues"]）
"""
import datetime
import re

STAGES = ["投递", "简历初筛", "笔试", "一面", "二面", "三面", "offer", "入职"]
STATUSES = ["进行中", "已通过", "待定", "已淘汰", "已发offer", "已接受", "已拒绝"]
GENDERS = ["男", "女", "未知"]
VALID_STAGES = set(STAGES)
VALID_STATUSES = set(STATUSES)

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "7": 6}


def _is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _add_months(d, n):
    """对日期 d 增减 n 个月，自动处理月末溢出（如 1月31日 +1月 -> 2月28/29日）。"""
    m = d.month + n
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    days_in_month = [31, 29 if _is_leap(y) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    day = min(d.day, days_in_month)
    return datetime.date(y, m, day)


def _apply_year(base, off):
    """对基准日期整体偏移年份（用于「去年/明年/前年」前缀作用于相对日）。"""
    if not base or off == 0:
        return base
    try:
        return datetime.date(base.year + off, base.month, base.day)
    except Exception:
        return base


def _parse_date(s):
    """识别 YYYY-MM-DD / YYYY/MM/DD，返回 date 或 None（供字段校验判断时间是否可解析）。"""
    if not s:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def _resolve_date(s, ref):
    """把文本中的日期部分解析为绝对日期；解析不到返回 None。"""
    if not s:
        return None

    # 年前缀（作用于后续相对日/周/月）
    year_off = 0
    if "前年" in s:
        year_off = -2
    elif "去年" in s:
        year_off = -1
    elif "明年" in s:
        year_off = 1

    # 1) 绝对：YYYY年M月D日 / YYYY-M-D / YYYY/M/D
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if not m:
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)) + year_off, int(m.group(2)), int(m.group(3)))
        except Exception:
            return None

    # 2) 中文「个月」+ 可选具体日（上个月15号 / 下个月1号 / 本月5号）
    mon_off = 0
    if "上个月" in s or "上月" in s:
        mon_off = -1
    elif "下个月" in s or "下月" in s:
        mon_off = 1
    day_m = re.search(r"(\d{1,2})\s*[日号]", s)
    if mon_off != 0 or (("本月" in s or "这个月" in s or "当月" in s) and day_m):
        base = _add_months(ref, mon_off)
        if day_m:
            d = int(day_m.group(1))
            try:
                base = datetime.date(base.year, base.month, d)
            except Exception:
                pass
        return _apply_year(base, year_off)

    # 3) 仅有 M月D日 / M月D号（缺年：补今年；已过去则视为明年计划）
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m:
        try:
            base = datetime.date(ref.year, int(m.group(1)), int(m.group(2)))
        except Exception:
            return None
        if year_off != 0:
            return datetime.date(ref.year + year_off, int(m.group(1)), int(m.group(2)))
        if base < ref:
            try:
                base = datetime.date(ref.year + 1, int(m.group(1)), int(m.group(2)))
            except Exception:
                pass
        return base

    # 4) 相对日（注意：「大前天/大后天」须先于「前天/后天」判断，避免子串误命中）
    if "大前天" in s:
        return _apply_year(ref - datetime.timedelta(days=3), year_off)
    if "前天" in s:
        return _apply_year(ref - datetime.timedelta(days=2), year_off)
    if "昨天" in s or "昨日" in s:
        return _apply_year(ref - datetime.timedelta(days=1), year_off)
    if "今天" in s or "今日" in s or "当天" in s or "当日" in s:
        return _apply_year(ref, year_off)
    if "明天" in s or "明日" in s:
        return _apply_year(ref + datetime.timedelta(days=1), year_off)
    if "大后天" in s:
        return _apply_year(ref + datetime.timedelta(days=3), year_off)
    if "后天" in s:
        return _apply_year(ref + datetime.timedelta(days=2), year_off)

    m = re.search(r"(\d+)\s*天[前]", s)
    if m:
        return _apply_year(ref - datetime.timedelta(days=int(m.group(1))), year_off)
    m = re.search(r"(\d+)\s*天[后]", s)
    if m:
        return _apply_year(ref + datetime.timedelta(days=int(m.group(1))), year_off)

    # 5) 周：上周 / 本周 / 下周，可带星期（以本周一为基准推算，避免「下周一」算成下下周）
    monday = ref - datetime.timedelta(days=ref.weekday())
    wd_m = re.search(r"[周星期]([一二三四五六日天7])", s)
    if "上周" in s or "上星期" in s:
        base = monday - datetime.timedelta(days=7)
        if wd_m:
            base = base + datetime.timedelta(days=_WEEKDAYS[wd_m.group(1)])
        return _apply_year(base, year_off)
    if "下周" in s or "下星期" in s:
        base = monday + datetime.timedelta(days=7)
        if wd_m:
            base = base + datetime.timedelta(days=_WEEKDAYS[wd_m.group(1)])
        return _apply_year(base, year_off)
    if "本周" in s or "这周" in s or "这星期" in s:
        base = monday
        if wd_m:
            base = base + datetime.timedelta(days=_WEEKDAYS[wd_m.group(1)])
        return _apply_year(base, year_off)
    if "本月" in s or "这个月" in s or "当月" in s:
        return _apply_year(ref, year_off)
    if wd_m:
        # 单独「周X」默认指本周的周X
        return _apply_year(monday + datetime.timedelta(days=_WEEKDAYS[wd_m.group(1)]), year_off)

    return None


def _resolve_time(s):
    """解析文本中的时刻部分，返回 (h, m) 或 None。"""
    if not s:
        return None
    # 1) 带时段词（上午/下午/中午…）：覆盖 3点 / 3点半 / 3点15分 / 3:30（须先于「已有时分」分支，否则 3:30 会被截走）
    m = re.search(r"(上午|早上|早晨|凌晨|中午|下午|傍晚|晚上|夜里)\s*(\d{1,2})(?:点(?:半|(\d{1,2})分)?|[:：](\d{1,2}))?", s)
    if m:
        period, hh, seg = m.group(1), int(m.group(2)), m.group(0)
        if "半" in seg:
            mm = 30
        else:
            mm = None
            for g in (m.group(3), m.group(4)):
                if g:
                    mm = int(g)
                    break
            mm = mm or 0
        if period in ("下午", "傍晚", "晚上", "夜里"):
            if hh != 12:
                hh += 12
        elif period in ("上午", "早上", "早晨", "凌晨"):
            if hh == 12:
                hh = 0
        elif period == "中午":
            hh = 12
        return hh, mm
    # 2) 已有时分（如 14:00 / 14：00）无时段词
    m = re.search(r"(\d{1,2}):(\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 3) 纯 X点[半/分] 无时段词
    m = re.search(r"(\d{1,2})\s*点\s*(半|(\d{1,2})\s*分)?", s)
    if m:
        hh = int(m.group(1))
        if m.group(2) == "半":
            mm = 30
        elif m.group(3):
            mm = int(m.group(3))
        else:
            mm = 0
        return hh, mm
    return None


def normalize_time(value, ref=None):
    """把任意时间文本归一化为「YYYY-MM-DD HH:mm」或「YYYY-MM-DD」；无法解析则原样返回。

    统一 ref（默认今天），保证同一「昨天」在不同调用日解析出不同绝对日期，且入库后稳定。
    """
    if not value or not str(value).strip():
        return value
    s = str(value).strip()
    ref = ref or datetime.date.today()
    d = _resolve_date(s, ref)
    t = _resolve_time(s)
    if d is None:
        # 只有时刻但缺日期（如纯「下午3点」）无法定位绝对日，保留原值交由校验标记
        return value
    if t:
        hh, mm = max(0, min(23, t[0])), max(0, min(59, t[1]))
        return "%s %02d:%02d" % (d.isoformat(), hh, mm)
    return d.isoformat()


def validate_record(r):
    """对单条记录做字段校验，返回 issues 列表（每项 {field, msg, level}）。

    level: error（硬错误，影响看板准确性，进红点）/ warn（建议性，前端明细展示）。
    """
    issues = []
    if r is None:
        return issues

    def add(field, msg, level="error"):
        issues.append({"field": field, "msg": msg, "level": level})

    # 手机号
    phone = (r.get("phone") or "").strip()
    if phone and not PHONE_RE.match(phone):
        add("phone", "手机号「%s」格式异常（应为 11 位、1 开头的号码）" % phone)

    # 邮箱
    email = (r.get("email") or "").strip()
    if email and not EMAIL_RE.match(email):
        add("email", "邮箱「%s」格式异常（缺少 @ 或域名）" % email)

    # 阶段：必须在标准枚举内（空由 anomaly 的「缺阶段」负责，这里不重复报）
    stage = (r.get("stage") or "").strip()
    if stage and stage not in VALID_STAGES:
        add("stage", "阶段「%s」非标准枚举（应为 %s）" % (stage, "/".join(STAGES)))

    # 状态：建议性（模型可能自由填，宽松标 warn）
    status = (r.get("status") or "").strip()
    if status and status not in VALID_STATUSES:
        add("status", "状态「%s」非标准（建议：%s）" % (status, "/".join(STATUSES)), "warn")

    # 性别：建议性
    gender = (r.get("gender") or "").strip()
    if gender and gender not in GENDERS:
        add("gender", "性别「%s」非标准（应为 男/女/未知）" % gender, "warn")

    # 时间：若归一化后仍无法解析绝对日期，标记（影响时间轴/停滞判断）
    time = (r.get("time") or "").strip()
    if time:
        norm = normalize_time(time)
        if norm == time and _parse_date(norm) is None:
            add("time", "时间「%s」无法识别，将影响时间轴与停滞判断" % time)

    return issues


def clean_record(r, ref=None):
    """原地处理一条记录：归一化 time + 清洗常见字段空白 + 写入 issues。返回该记录。"""
    if not isinstance(r, dict):
        return r
    if r.get("time"):
        r["time"] = normalize_time(r.get("time"), ref)
    for k in ("phone", "email", "stage", "status", "candidate", "position"):
        if isinstance(r.get(k), str):
            r[k] = r[k].strip()
    r["issues"] = validate_record(r)
    return r
