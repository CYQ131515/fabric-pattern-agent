"""HTTP 公共基础：C 端和后台两个服务共用这一套收发工具。

拆成两个进程之后，像「怎么写 JSON 响应」「怎么发文件」「怎么解 query」
这种东西不能复制两份——复制两份一定会在某个地方长歪。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon',
        '.txt': 'text/plain; charset=utf-8', '.md': 'text/markdown; charset=utf-8'}


def query(path):
    """解 query string → dict（同名只取第一个）。中文会自动 unquote。"""
    from urllib.parse import parse_qs, urlsplit
    try:
        return {k: v[0] for k, v in parse_qs(urlsplit(path).query).items() if v}
    except Exception:
        return {}


def cookie_of(headers):
    """从请求头里取 cookie → dict。只做最简单的一次 split，够用了。"""
    raw = headers.get('Cookie') or ''
    out = {}
    for part in raw.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, _, v = part.partition('=')
        out[k.strip()] = v.strip()
    return out


class BaseHandler(BaseHTTPRequestHandler):
    """两个服务共用的请求基类。子类只需要实现 do_GET / do_POST。"""
    protocol_version = 'HTTP/1.1'
    server_version = 'PatternFlow/1.0'
    # 后台会写 cookie；C 端不需要但可以留着，省得分叉
    cookie_name = 'pf_admin'
    cookie_path = '/'

    # ---------- 输出 ----------
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',
                         self.headers.get('Origin') or '*')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, Authorization, X-Admin-Token')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Vary', 'Origin')

    def _head(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self._cors()
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, code=200, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._head(code, 'application/json; charset=utf-8', len(raw), extra)
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, body, code=200, extra=None):
        raw = body.encode('utf-8')
        self._head(code, MIME['.html'], len(raw), extra)
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, path, cache=False):
        if not os.path.exists(path) or not os.path.isfile(path):
            self._json({"ok": False, "error": "not found: %s" % path}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError:
            self._json({"ok": False, "error": "read failed"}, 500)
            return
        extra = []
        if cache:
            extra.append(('Cache-Control', 'public, max-age=300'))
        self._head(200, MIME.get(ext, 'application/octet-stream'), len(raw), extra)
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _set_cookie(self, value, max_age=0):
        """max_age=0 表示删掉这个 cookie。"""
        part = '%s=%s; Path=%s; HttpOnly; SameSite=Strict' % (
            self.cookie_name, value, self.cookie_path)
        if max_age > 0:
            part += '; Max-Age=%d' % int(max_age)
        else:
            part += '; Max-Age=0'
        return [('Set-Cookie', part)]

    # ---------- 输入 ----------
    def body_json(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b'{}'
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    def cookie(self, name=None):
        return cookie_of(self.headers).get(name or self.cookie_name) or ''

    def token(self):
        """登录令牌：优先读头（测试/脚本方便），其次读 cookie（浏览器自动带）。"""
        t = self.headers.get('X-Admin-Token') or ''
        if not t:
            au = self.headers.get('Authorization') or ''
            if au.lower().startswith('bearer '):
                t = au[7:].strip()
        if not t:
            t = self.cookie()
        return t.strip()

    def same_origin(self):
        """写操作的 CSRF 检查：Origin / Referer 的 host 必须和 Host 一致。

        cookie 是 HttpOnly + SameSite=Strict 已经挡掉大半，但端口不同源时
        （8787 的页面去打 8788）SameSite 不生效，所以这里再补一刀。
        """
        # 比 host **带端口**：8787 的页面去打 8788 不算同源
        host = (self.headers.get('Host') or '')
        for key in ('Origin', 'Referer'):
            v = self.headers.get(key) or ''
            if not v:
                continue
            try:
                from urllib.parse import urlsplit
                net = urlsplit(v).netloc or ''
            except Exception:
                net = ''
            if net and net == host:
                return True
        # 两个头都没有（curl / 脚本直连）：本机后台场景放行
        return not (self.headers.get('Origin') or self.headers.get('Referer'))

    # ---------- 杂项 ----------
    @property
    def path_only(self):
        return self.path.split('?')[0]

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' % (self.log_date_time_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()
