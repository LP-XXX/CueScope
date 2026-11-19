"""本机模拟飞书接收器：仅供自动化验证，不转发任何消息。"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = json.dumps(payload, ensure_ascii=False)
            event_id = text.split("事件编号：", 1)[1].split("\\n", 1)[0][:100]
        except Exception:
            self.send_json(400, {"code": 400, "msg": "invalid json"})
            return
        logging.info("mock accepted event_id=%s path=%s", event_id, self.path)
        if self.path == "/business-error":
            self.send_json(200, {"code": 19001, "msg": "simulated invalid sign"})
        elif self.path == "/http-500":
            self.send_json(500, {"code": 500, "msg": "simulated error"})
        elif self.path == "/rate-limit":
            self.send_json(200, {"code": 99991663, "msg": "simulated rate limit"})
        else:
            self.send_json(200, {"code": 0, "msg": "success"})

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="仅绑定 127.0.0.1 的模拟飞书接收器")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock webhook: http://127.0.0.1:{args.port}/ok")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
