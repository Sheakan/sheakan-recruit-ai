# -*- coding: utf-8 -*-
"""招聘记录统一字段定义：抽取、智能表列、文档表头均由此派生，顺序即列顺序。"""
FIELDS = [
    ("candidate", "候选人"),
    ("position", "岗位"),
    ("department", "部门"),
    ("stage", "阶段"),
    ("status", "状态"),
    ("interviewer", "面试官"),
    ("recruiter", "招聘负责人"),
    ("channel", "渠道"),
    ("education", "学历"),
    ("experience", "工作年限"),
    ("currentCompany", "当前公司"),
    ("expectedSalary", "期望薪资"),
    ("contact", "联系方式"),
    ("time", "时间"),
    ("remark", "备注"),
    ("source", "来源"),
    # 简历个人信息（扩充字段）
    ("gender", "性别"),
    ("age", "年龄"),
    ("phone", "手机号"),
    ("email", "邮箱"),
    ("expectedCity", "期望城市"),
]

# 中文表头列表 / 字段 key 列表，便于各适配器直接复用
HEADERS_CN = [cn for _, cn in FIELDS]
KEYS = [k for k, _ in FIELDS]
