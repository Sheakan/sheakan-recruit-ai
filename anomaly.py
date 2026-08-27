# Copyright (c) 2026 Sheakan. 保留所有权利。
# -*- coding: utf-8 -*-
"""招聘数据异常检测（规则引擎）：不依赖大模型，稳定可跑。

检测维度：
- 关键信息缺失（缺联系方式 / 缺阶段）
- 阶段长期停滞（非终态且时间早于阈值，提示 HR 跟进）
- 期望值异常（如薪资为 0 或格式异常，仅做轻量提示）
返回 [{type, msg, record_id}]，供看板红点与提示使用。
"""
import datetime
import re

TERMINAL_STAGES = {"入职", "已拒绝", "已淘汰"}


def _parse_date(s: str):
    if not s:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def detect(records: list, stale_days: int = 7):
    out = []
    today = datetime.date.today()
    for r in records:
        rid = r.get("id")
        name = r.get("candidate") or "未知候选人"
        # 1. 缺联系方式
        if not (r.get("phone") or r.get("email") or r.get("contact")):
            out.append({"type": "缺失联系方式", "msg": f"{name} 缺少手机号/邮箱，不便后续跟进", "record_id": rid})
        # 2. 缺阶段
        if not r.get("stage"):
            out.append({"type": "缺阶段", "msg": f"{name} 未填写招聘阶段", "record_id": rid})
        # 3. 阶段长期停滞
        stage = (r.get("stage") or "").strip()
        if stage and stage not in TERMINAL_STAGES:
            d = _parse_date(r.get("time") or "")
            if d and (today - d).days > stale_days:
                out.append({
                    "type": "阶段停滞",
                    "msg": f"{name}（{stage}）自 {d.isoformat()} 起超过 {stale_days} 天无推进，建议跟进",
                    "record_id": rid,
                })
        # 4. 薪资异常（轻量）
        sal = r.get("expectedSalary") or ""
        if sal:
            nums = re.findall(r"(\d+)", sal)
            if nums and int(nums[0]) == 0:
                out.append({"type": "薪资异常", "msg": f"{name} 期望薪资疑似为 0，请核实", "record_id": rid})
        # 5. 低置信度待复核（让抽取→置信度→看板形成闭环）
        conf = r.get("confidence")
        if conf is not None:
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = None
        if conf is not None and conf < 0.7:
            out.append({"type": "待复核", "msg": f"{name} AI 抽取置信度 {conf:.2f}，建议人工核对关键信息", "record_id": rid})
        # 6. 字段校验问题（由 validate.clean_record 写入 r["issues"]，error 级进红点）
        for iss in (r.get("issues") or []):
            if iss.get("level") == "error":
                out.append({"type": "字段校验", "msg": f"{name}：{iss.get('msg')}", "record_id": rid})
    return out
