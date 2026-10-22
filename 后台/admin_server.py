#!/usr/bin/env python3
"""PatternFlow 管理后台服务（B 端）—— 独立进程，默认 127.0.0.1:8788。

和用户端（../后端/server.py，8787）**物理分开**：
- 后台能改密钥、改人设、改知识库、删图，不该和用户访问的地址挂在一起；
- 默认只监听 127.0.0.1，别人连不上；
- 进页面要口令（core/auth.py）；
- 后台挂了/重启，用户那边聊天不受影响。

改完磁盘上的东西会 bump 变更号，用户端下次请求自动重载，不用重启。

启动（在本目录「后台/」下跑，或直接双击项目根的「启动后台.sh」）：
    python3 admin_server.py
    python3 admin_server.py --port 8788 --open
    python3 admin_server.py --host 0.0.0.0     # 会警告：必须已设好强口令
"""
import argparse
import base64
import os
import sys
import webbrowser
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 本文件在项目根的「后台/」，而核心引擎（core/、config/、personas/…）在「后端/」。
# 两个服务共用同一套核心，所以把「后端/」也加进模块搜索路径 ——
# 不复制一份 core，免得两处逻辑各改各的。
BACKEND = os.path.normpath(os.path.join(HERE, '..', '后端'))
sys.path.insert(0, BACKEND)

from core import admin as adminapi                   # noqa: E402
from core import auth                                # noqa: E402
from core.app import App, facets, take_image         # noqa: E402
from core.config import ROOT                         # noqa: E402
from core.httpsrv import BaseHandler, query          # noqa: E402
from core.library import DIMS as LIB_DIMS            # noqa: E402

# 后台只有一份页面，就放在本文件旁边
PROTO_DIR = HERE
ADMIN_PAGE = 'PatternFlow管理后台v0.1.html'

# 不用登录就能调的接口：会话状态、登录、以及**首次设置口令**。
# 口令一旦设上，set-password 也要求已登录（防止谁都能改口令）。
OPEN_API = ('/api/health', '/api/admin/session', '/api/admin/login')

APP = App(who='admin', with_runtime=False)


class Handler(BaseHandler):
    cookie_name = 'pf_admin'

    # ---------- 鉴权 ----------
    def _deny(self):
        return self._json({"ok": False, "need_auth": True, "error": "请先登录"}, 401)

    def authed(self):
        tok = self.token()
        return auth.valid(tok)

    def _need(self):
        """除白名单外的接口一律要登录。返回 None 表示放行。"""
        p = self.path_only
        if p in OPEN_API:
            return None
        if not self.authed():
            return self._deny()
        # 写操作再查一次来源：SameSite 挡跨站，挡不住同机跨端口
        if not self.same_origin():
            return self._json({"ok": False, "error": "来源不对（CSRF 拦截）"}, 403)
        return None

    # ---------- GET ----------
    def do_GET(self):
        APP.maybe_reload()
        auth.sweep()
        path = self.path_only

        if path == '/api/health':
            return self._json(APP.health())
        if path == '/api/admin/session':
            return self._json(auth.session())

        # 页面本身不拦：它只是一段静态 HTML，不带任何数据。
        # 拦住接口就够了 —— 页面打开后由前端问 /api/admin/session，
        # 没登录就弹登录框。要是这里也 401，浏览器只会看到一坨 JSON。
        if not path.startswith('/api/'):
            return self._file(os.path.join(PROTO_DIR, ADMIN_PAGE))

        denied = self._need()
        if denied:
            return denied

        if path == '/api/admin/state':
            return self._json(adminapi.state(APP))
        if path == '/api/admin/analytics':
            # 数据看板：只聚合磁盘上真有的东西（会话/图库/出图目录），只读。
            try:
                days = int(query(self.path).get('days') or 14)
            except (TypeError, ValueError):
                days = 14
            return self._json(adminapi.analytics(APP, days))
        if path == '/api/admin/persona':
            return self._json(adminapi.persona_list(APP))
        if path == '/api/admin/kb':
            return self._json(adminapi.kb_list(APP))
        if path == '/api/admin/kb/doc':
            return self._json(adminapi.kb_doc(APP, query(self.path).get('id')))
        if path == '/api/admin/library/scan/progress':
            return self._json(adminapi.scan_progress(APP))
        if path == '/api/admin/sessions':
            # q=会话 ID 片段（模糊），day=最后活动日期 YYYY-MM-DD；都为空就返回最近一批
            qs = query(self.path)
            return self._json(adminapi.session_list(
                APP, q=qs.get('q') or '', day=qs.get('day') or ''))
        if path == '/api/library':
            # 后台的图库页面：读写都要，所以这份带 facets，和 C 端那份不冲突
            # （两个进程两个端口，各管各的）
            qs = query(self.path)
            brief = {k: qs.get(k) for k in LIB_DIMS if qs.get(k)}
            try:
                limit = int(qs.get('limit') or 60)
                offset = int(qs.get('offset') or 0)
            except (TypeError, ValueError):
                limit, offset = 60, 0
            res = APP.library.search(brief=brief, q=qs.get('q'), limit=limit,
                                     offset=offset, source=qs.get('source'),
                                     status=qs.get('status'), strict=True)
            res.update({"ok": True, "stats": APP.library.stats(),
                        "facets": facets(APP.library)})
            return self._json(res)
        if path.startswith('/api/library/file/'):
            pid = path[len('/api/library/file/'):].split('/')[0]
            if not pid or not all(ch.isalnum() or ch in '-_.' for ch in pid):
                return self._json({"ok": False, "error": "bad id"}, 400)
            item = APP.library.get(pid)
            if not item:
                return self._json({"ok": False, "error": "no such pattern"}, 404)
            thumb = 'thumb=1' in (self.path.split('?')[1] if '?' in self.path else '')
            target = APP.library.path_of(item, thumb)
            if not target or not os.path.exists(target):
                return self._json({"ok": False, "error": "file missing"}, 404)
            return self._file(target, cache=True)
        if path.startswith('/api/'):
            return self._json({"ok": False, "error": "unknown api"}, 404)

        # 后台只有一份页面：管理后台。不给它服务 C 端原型，免得混在一起
        return self._file(os.path.join(PROTO_DIR, ADMIN_PAGE))

    # ---------- POST ----------
    def do_POST(self):
        APP.maybe_reload()
        path = self.path_only
        payload = self.body_json()
        if payload is None:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)

        # 登录 / 首次设口令
        if path == '/api/admin/login':
            r = auth.login((payload or {}).get('password') or '')
            if r.get('ok'):
                # cookie 给浏览器（<img> 取图自动带上，不用往 URL 里拼 token）；
                # 正文里也回一份，脚本/命令行才能拿去放 X-Admin-Token 头
                return self._json({"ok": True, "token": r['token'], "ttl": r['ttl']},
                                  extra=self._set_cookie(r['token'], r['ttl']))
            return self._json(r, 401)
        if path == '/api/admin/set-password':
            # 已经设过口令的，必须先登录才能改
            if auth.has_password() and not self.authed():
                return self._deny()
            return self._json(auth.set_password((payload or {}).get('password') or ''))
        if path == '/api/admin/logout':
            auth.logout(self.token())
            return self._json({"ok": True}, extra=self._set_cookie('', 0))

        denied = self._need()
        if denied:
            return denied

        try:
            if path == '/api/admin/config':
                return self._json(adminapi.save_config(APP, payload))
            if path == '/api/admin/persona/save':
                return self._json(adminapi.save_persona(APP, payload))
            if path == '/api/admin/kb/save':
                return self._json(adminapi.kb_save(APP, payload))
            if path == '/api/admin/library/scan':
                return self._json(adminapi.library_scan(APP, payload))
            if path == '/api/admin/password':
                return self._json(auth.change_password(
                    (payload or {}).get('old') or '', (payload or {}).get('new') or ''))
            if path == '/api/library/ingest':
                image, mime = take_image(payload)
                if not image:
                    return self._json({"ok": False, "error": "没有收到图片"}, 400)
                # 导入闸门：先过自动打标签算法，过了才落库（见 core/atag.py）。
                # 不是花型 / 引擎没起，都会被拦在这儿，库里不会进没打过标的图。
                res = adminapi.import_via_atag(
                    APP, base64.b64decode(image), mime=mime,
                    src_name=payload.get('filename'), name=payload.get('name'),
                    source=payload.get('source') or 'upload')
                return self._json(res, 200 if res.get('ok') else 400)
            if path == '/api/library/update':
                return self._json(APP.library.update(payload.get('id'),
                                                     payload.get('fields') or {}))
            if path == '/api/library/delete':
                return self._json(APP.library.remove(payload.get('id'),
                                                     purge=bool(payload.get('purge', True))))
            if path == '/api/library/retag':
                return self._json(APP.library.retag(payload.get('id')))
            if path == '/api/reload':
                APP.reload()
                return self._json({"ok": True, "skills": APP.registry.describe()})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 413)
        except Exception as e:
            # 这些接口能改密钥和人设，出错一律如实回，别吞
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        return self._json({"ok": False, "error": "unknown api"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--open', action='store_true', help='启动后自动打开浏览器')
    args = ap.parse_args()

    adm = APP.cfg.settings.get('admin') or {}
    host = args.host or adm.get('host') or '127.0.0.1'
    port = args.port or adm.get('port') or 8788
    url = 'http://%s:%d/' % (host, port)
    cli = APP.cfg.settings.get('server') or {}

    print('PatternFlow 管理后台（B 端）')
    print('  地址：%s' % url)
    print('  登录：%s' % ('已设置口令' if auth.has_password() else '还没设口令，进页面先设一个'))
    print('  图库：%d 张   知识库：%d 篇 / %d 问答   会话：%s'
          % (len(APP.library.items), len(APP.kb.docs), len(APP.kb.qa),
             APP.cfg.path('sessions')))
    print('  用户端是另一个进程：http://%s:%d/  （python3 server.py）'
          % (cli.get('host', '127.0.0.1'), cli.get('port', 8787)))
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print('  ⚠ 监听的是 %s —— 后台没有登录之外的防护，确认口令够强、且在内网' % host)
    print('  页面：%s' % os.path.join(PROTO_DIR, ADMIN_PAGE))
    if args.open:
        webbrowser.open(url)

    srv = ThreadingHTTPServer((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        srv.server_close()


if __name__ == '__main__':
    main()
