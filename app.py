# -*- coding: utf-8 -*-
"""腾讯云函数 SCF 事件函数入口（与 scf_bootstrap 二选一）。
- Web 函数：用 scf_bootstrap 直接跑 server.py，无需本文件。
- 事件函数：SCF 收到 API 网关请求时调用本 handler，由 serverless_wsgi 把 Flask 应用适配为 SCF 事件。
"""
try:
    import serverless_wsgi
    import server

    def handler(event, context):
        return serverless_wsgi.lambda_handler(server.app, event, context)
except Exception as exc:  # 初始化失败时给出可读错误，避免静默 500
    def handler(event, context):
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": "SCF handler 初始化失败：%s" % exc,
        }
