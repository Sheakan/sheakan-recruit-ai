# Copyright (c) 2026 Sheakan. 保留所有权利。
# -*- coding: utf-8 -*-
"""招聘记录统一字段定义：抽取、智能表列、文档表头均由此派生，顺序即列顺序。

字段可在应用「配置」→「字段管理」中自定义（重命名 / 启用禁用 / 新增 / 删除 / 排序）。
自定义结果保存在 config.json 的 fields 段，运行时由本模块的 get_* 函数读取并贯穿到
网页表格、腾讯文档同步、以及大模型抽取提示词三处。
"""
import config_store

# 内置字段：(key, 中文显示名, LLM 抽取提示词)
BUILTIN_FIELDS = [
    ("candidate", "候选人", "候选人姓名，未知则空字符串"),
    ("position", "岗位", "应聘岗位，未知则空字符串"),
    ("department", "部门", "所属部门/团队，未知则空字符串"),
    ("stage", "阶段", "招聘阶段，只能是以下之一：投递 / 简历初筛 / 笔试 / 一面 / 二面 / 三面 / offer / 入职"),
    ("status", "状态", "该阶段下的具体状态，如 进行中/已通过/待定/已淘汰/已发offer/已接受/已拒绝；未知则空字符串"),
    ("interviewer", "面试官", "面试官姓名，未知则空字符串"),
    ("recruiter", "招聘负责人", "招聘负责人/HR姓名，未知则空字符串"),
    ("channel", "渠道", "来源渠道，如 内推/猎头/校招/社招/官网/招聘平台/企业微信群；未知则空字符串"),
    ("education", "学历", "学历，如 本科/硕士/博士，未知则空字符串"),
    ("experience", "工作年限", "工作年限，如 3年，未知则空字符串"),
    ("currentCompany", "当前公司", "当前所在公司，未知则空字符串"),
    ("expectedSalary", "期望薪资", "期望薪资，如 25k，未知则空字符串"),
    ("contact", "联系方式", "联系方式(手机或邮箱)，未知则空字符串"),
    ("time", "时间", "关键时间(面试/入职等)，格式 YYYY-MM-DD HH:mm，未知则空字符串"),
    ("remark", "备注", "其它备注信息，未知则空字符串"),
    ("gender", "性别", "候选人性别，如 男/女/未知，未知则空字符串"),
    ("age", "年龄", "候选人年龄(数字)，未知则空字符串"),
    ("phone", "手机号", "候选人手机号，未知则空字符串"),
    ("email", "邮箱", "候选人邮箱，未知则空字符串"),
    ("expectedCity", "期望城市", "期望工作城市，未知则空字符串"),
]

# 兼容旧引用（默认字段，未自定义时使用）
FIELDS = [(k, n) for k, n, _ in BUILTIN_FIELDS]
HEADERS_CN = [n for _, n in FIELDS]
KEYS = [k for k, _ in FIELDS]


def _load_cfg_fields():
    """读取用户已保存的字段配置；结构非法或为空时返回 None（回退内置）。"""
    try:
        f = config_store.load().get("fields")
    except Exception:
        f = None
    if isinstance(f, list) and f:
        return f
    return None


def get_full_fields():
    """返回完整字段列表（含 desc/builtin/enabled），用于大模型提示词生成。"""
    cf = _load_cfg_fields()
    if cf is not None:
        return cf
    return [{"key": k, "name": n, "desc": d, "builtin": True, "enabled": True}
            for k, n, d in BUILTIN_FIELDS]


def effective_fields():
    """返回 [(key, name)]，按配置顺序且仅包含启用项——贯穿表格/文档渲染。"""
    out = []
    for f in get_full_fields():
        if f.get("enabled", True):
            out.append((f["key"], f["name"]))
    return out


def get_headers():
    return [n for _, n in effective_fields()]


def get_keys():
    return [k for k, _ in effective_fields()]


def field_descriptions():
    """返回 [(key, name, desc)]，仅启用项，用于动态生成 SYSTEM_PROMPT。"""
    out = []
    for f in get_full_fields():
        if f.get("enabled", True):
            out.append((f["key"], f["name"], f.get("desc", "")))
    return out
