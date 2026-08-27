# Copyright (c) 2026 Sheakan. 保留所有权利。
# -*- coding: utf-8 -*-
"""企业微信智能表写入适配器：通过「接收外部数据」Webhook 推送记录（免 OAuth）。"""
import os
import requests
import config_store
from fields import FIELDS

ENV_WEBHOOK = os.environ.get("SMARTSHEET_WEBHOOK", "")

# 字段 key -> (智能表字段ID, 列类型)；列类型: text/select/user/number/date；字段ID留空则跳过。
# 这些为内置默认值（已对接好的 16 列）。用户可在界面「配置」里为任意字段覆盖/补充字段 ID，
# 写入 config.json 的 smartsheet_field_ids，无需改代码。
FIELD_ID_MAP = {
    "candidate": ("f04Gwj", "text"),      # 候选人
    "position": ("fIgwxB", "text"),       # 岗位
    "department": ("flp2lo", "text"),     # 部门
    "stage": ("foQla3", "text"),          # 阶段
    "status": ("fWFR1Y", "text"),         # 状态
    "interviewer": ("fn4roh", "text"),    # 面试官
    "recruiter": ("fFtFfx", "text"),      # 招聘负责人
    "channel": ("fPtAYH", "text"),        # 渠道
    "education": ("fJT7vg", "text"),      # 学历
    "experience": ("fzZXgK", "text"),     # 工作年限
    "currentCompany": ("fOPHC3", "text"), # 当前公司
    "expectedSalary": ("fmjGNn", "text"), # 期望薪资
    "contact": ("fdK7uB", "text"),        # 联系方式
    "time": ("fVkeOh", "text"),           # 时间
    "remark": ("fGhmuu", "text"),         # 备注
    "source": ("f0fFft", "text"),         # 来源
    # 以下为扩充字段，智能表加列后填入字段 ID 即可同步（也可在界面配置里填）
    "gender": ("", "text"),               # 性别
    "age": ("", "text"),                  # 年龄
    "phone": ("", "text"),                # 手机号
    "email": ("", "text"),                # 邮箱
    "expectedCity": ("", "text"),         # 期望城市
}


def get_effective_map():
    """合并内置映射与用户在界面配置的字段 ID 覆盖（config.json -> smartsheet_field_ids）。"""
    overrides = {}
    try:
        overrides = config_store.load().get("smartsheet_field_ids", {}) or {}
    except Exception:
        overrides = {}
    eff = {k: (fid, t) for k, (fid, t) in FIELD_ID_MAP.items()}
    for k, fid in overrides.items():
        if k in eff and fid:
            eff[k] = (fid, eff[k][1])
    return eff


def enabled(webhooks=None):
    whs = webhooks if webhooks is not None else ([ENV_WEBHOOK] if ENV_WEBHOOK else [])
    return len([w for w in whs if w]) > 0 and any(fid for fid, _ in get_effective_map().values())


def _fmt(col_type, value):
    """按列类型格式化单值；空值返回 None（跳过该字段）。"""
    if value is None or value == "":
        return None
    if col_type == "select":
        return [{"text": str(value)}]
    if col_type == "user":
        return [{"user_id": str(value)}]
    if col_type == "number":
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except Exception:
            return None
    if col_type == "date":
        try:
            return str(int(float(value)))
        except Exception:
            return None
    return str(value)  # text


def _build_add_records(records):
    active = {k: (fid, t) for k, (fid, t) in get_effective_map().items() if fid}
    add_records = []
    for r in records:
        values = {}
        for key, (fid, col_type) in active.items():
            v = _fmt(col_type, r.get(key))
            if v is not None:
                values[fid] = v
        if values:
            add_records.append({"values": values})
    return add_records


def push(records, webhooks=None):
    """
    批量写入一个或多个智能表（扇出同步）。返回 (成功条数, 错误信息列表)。
    webhooks 为空时回退到环境变量 SMARTSHEET_WEBHOOK（兼容旧部署）。
    失败不影响主流程（仅记录），保证解析入库不中断。
    """
    whs = webhooks if webhooks is not None else ([ENV_WEBHOOK] if ENV_WEBHOOK else [])
    whs = [w for w in whs if w]
    if not whs or not any(fid for fid, _ in FIELD_ID_MAP.values()):
        return 0, ["未配置智能表 WEBHOOK（请在界面「配置」中填写）"]

    add_records = _build_add_records(records)
    if not add_records:
        return 0, []

    ok_total = 0
    errors = []
    for wh in whs:
        try:
            resp = requests.post(wh, json={"add_records": add_records}, timeout=15)
            data = resp.json()
            if data.get("errcode", -1) == 0:
                ok_total += len(add_records)
            else:
                errors.append(f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        except Exception as e:
            errors.append(str(e))
    return ok_total, errors
