"""本地 Mock OpenAI 兼容服务（验证 LLM 模型管理链路，替代被公司网络屏蔽的外网调用）。

监听 127.0.0.1:8199，仅实现 POST /chat/completions：
- 请求体 model 字段决定返回的意图（mock-v1 / mock-v2 不同时间区间，用于验证切换生效）
- 其余一律 404

启动：python scripts/mock_llm_server.py
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

INTENTS = {
    "mock-v1": {"metric_code": None, "start": "2026-08-01", "end": "2026-08-31", "compare": "none"},
    "mock-v2": {"metric_code": None, "start": "2026-07-01", "end": "2026-07-31", "compare": "mom"},
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "")
        intent = INTENTS.get(model)
        content = json.dumps(intent if intent else {"metric_code": None})
        payload = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8199), Handler).serve_forever()
