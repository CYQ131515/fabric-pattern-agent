#!/usr/bin/env python3
"""假模型服务：只为验证「工具调用循环」是否跑通，不是真模型。

它按固定剧本返回：第一轮要求调用 update_brief 与 search_patterns，
第二轮基于工具结果给出最终回复。用它可以在没有 API Key 的情况下
完整验证 Agent 链路（人设 → 工具 → 回写需求卡 → 出候选）。

启动：
    python3 tools/mock_llm.py --port 8899
然后：
    PATTERNFLOW_BASE_URL=http://127.0.0.1:8899/v1 PATTERNFLOW_API_KEY=mock python3 server.py
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _json(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n).decode('utf-8'))
        msgs = body.get('messages') or []
        has_tool_result = any(m.get('role') == 'tool' for m in msgs)

        if not has_tool_result and body.get('tools'):
            user = next((m['content'] for m in reversed(msgs) if m.get('role') == 'user'), '')
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "update_brief",
                                  "arguments": json.dumps({"style": "法式浪漫", "element": "花卉",
                                                           "scene": "女装连衣裙"}, ensure_ascii=False)}},
                    {"id": "call_2", "type": "function",
                     "function": {"name": "search_patterns",
                                  "arguments": json.dumps({"limit": 4}, ensure_ascii=False)}},
                ],
            }
        else:
            names = [json.loads(m.get('content') or '{}') for m in msgs if m.get('role') == 'tool']
            tops = []
            for r in names:
                tops += [i['name'] for i in (r.get('items') or [])][:4]
            message = {"role": "assistant",
                       "content": "按法式浪漫 + 花卉 + 女装连衣裙，库里这几款最贴：%s。留白比例都偏高，春夏连衣裙上身显瘦。"
                                  % '、'.join(tops[:4] or ['暂无'])}

        self._json({"choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8899)
    args = ap.parse_args()
    print('假模型服务：http://127.0.0.1:%d/v1' % args.port)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
