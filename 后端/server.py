#!/usr/bin/env python3
"""PatternFlow 用户端服务（C 端）—— 只管聊天和取图，默认 8787。

后台是另一个进程：../后台/admin_server.py（默认 127.0.0.1:8788）。
两边分开是因为后台能改密钥、改人设、删图，不该和用户访问的地址挂在一起；
后台挂了用户照样能聊，反过来也一样。

两边通过 config/.rev 对齐：后台改了配置/知识库/图库会 bump 变更号，
这边每个请求进来比对一次，不一样就自动重载。

启动（在本目录「后端/」下跑，或直接双击项目根的「启动后端.sh」）：
    python3 server.py            # 启动并打印地址
    python3 server.py --open     # 启动并自动打开浏览器

接口：
    GET  /api/health     服务与模型配置自检
    GET  /api/skills     已挂载的技能清单
    GET  /api/psd_progress  分层 PSD 当前跑到第几层（跑动时给界面用）
    GET  /api/patterns   花型库（?id=xxx 取单条）
    GET  /api/library    真图库（筛选 + 分页）
    GET  /api/library/file/<id>?thumb=1   库里的图
    POST /api/chat       对话
    POST /api/action     确认执行（生图/改图/描稿…）
    POST /api/export/list  按用户选中的花型导出一张 xlsx 清单
    POST /api/track      前端埋点（来路/停留/点击/离开），只记不答业务
    POST /api/reload     热重载
"""
import argparse
import base64
import os
import sys
import webbrowser
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.app import App, facets, take_image         # noqa: E402
from core.config import ROOT                         # noqa: E402
from core.httpsrv import BaseHandler, query          # noqa: E402
from core.library import DIMS as LIB_DIMS            # noqa: E402

# 页面默认在项目根的「前端/」里（ROOT 是「后端/」）。
# 用 PATTERNFLOW_PROTO_DIR / PATTERNFLOW_PAGE 可以另起一个实例去服务别的前端目录
# （新版前端就是这么独立跑的：同一个后端引擎，两个页面各起一个进程，互不干扰）。
def _proto_dir():
    p = os.environ.get('PATTERNFLOW_PROTO_DIR')
    if not p:
        return os.path.normpath(os.path.join(ROOT, '..', '前端'))
    return os.path.abspath(os.path.expanduser(p))


PROTO_DIR = _proto_dir()
DEFAULT_PAGE = os.environ.get('PATTERNFLOW_PAGE') or 'PatternFlow花型智造-交互原型v0.7.html'
FALLBACK_PAGE = os.path.join('历史版本', 'PatternFlow花型智造-交互原型v0.6.html')

APP = App(who='client', with_runtime=True)


class Handler(BaseHandler):
    # ---------- GET ----------
    def do_GET(self):
        # 后台可能刚改过配置/知识库/图库，先对齐再应答
        APP.maybe_reload()
        path = self.path_only

        if path == '/api/health':
            return self._json(APP.health())
        if path == '/api/skills':
            return self._json({"ok": True, "skills": APP.registry.describe()})
        if path == '/api/psd_progress':
            # 分层 PSD 逐层跑云端，几十秒到几分钟不等。界面靠这个回答
            # 「跑到第几层了、等了多久」—— 纯只读，不碰会话状态。
            # 四方连续也走这个出口（它没有逐层进度，只有一句"在做什么"）：
            # 界面上就一个秒表，不该为了两家引擎开两个轮询接口。
            # 同一时刻只可能有一个在跑，谁在跑就报谁。
            prog = APP.runtime.psdlayer.progress()
            if not prog.get('running'):
                alt = APP.runtime.seamless.progress()
                if alt.get('running'):
                    prog = alt
            return self._json(prog)
        if path == '/api/patterns':
            q = self.path.split('?')[1] if '?' in self.path else ''
            if 'id=' in q:
                pid = q.split('id=')[1].split('&')[0]
                for p in APP.data.get('patterns', []):
                    if p['id'].upper() == pid.upper():
                        return self._json({"ok": True, "pattern": p})
                return self._json({"ok": False, "error": "no such pattern"}, 404)
            return self._json({"ok": True, "count": len(APP.data.get('patterns', [])),
                               "items": APP.data.get('patterns', [])})
        if path == '/api/library':
            # 真图库：对话里检索命中了要能显示出来，所以 C 端也要能读。
            # 只给读，不给写 —— 改标签删图是后台的事。
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
            # 库里的图。id 已经归一化过，这里再挡一道目录穿越。
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
        if path.startswith('/api/file/'):
            # 取 generated/ 下的产出图。名字已在落盘时归一化，这里再挡一道目录穿越
            name = os.path.basename(path[len('/api/file/'):])
            if not name or not all(ch.isalnum() or ch in '-_.' for ch in name):
                return self._json({"ok": False, "error": "bad file name"}, 400)
            target = os.path.join(APP.runtime.imagegen.outdir, name)
            if not os.path.exists(target):
                return self._json({"ok": False, "error": "file not found"}, 404)
            return self._file(target, cache=True)
        if path.startswith('/api/'):
            return self._json({"ok": False, "error": "unknown api"}, 404)
        # /admin 不再由这个进程服务 —— 后台是独立进程，直接给人跳过去，
        # 免得有人在用户端地址上敲 /admin 拿到一份旧页面还以为能用
        if path.strip('/') in ('admin', 'admin/'):
            adm = APP.cfg.settings.get('admin') or {}
            to = 'http://%s:%d/' % (adm.get('host') or '127.0.0.1',
                                    adm.get('port') or 8788)
            self.send_response(302)
            self.send_header('Location', to)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        rel = os.path.basename(path.strip('/')) or DEFAULT_PAGE
        target = os.path.join(PROTO_DIR, rel)
        if not os.path.exists(target):
            target = os.path.join(PROTO_DIR, FALLBACK_PAGE)
        return self._file(target)

    # ---------- POST ----------
    def do_POST(self):
        APP.maybe_reload()
        path = self.path_only
        payload = self.body_json()
        if payload is None:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)

        if path == '/api/chat':
            try:
                return self._json(APP.chat(payload))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 413)
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        if path == '/api/vision':
            # 只识图、不走对话，方便调试和前端单独预览标签
            try:
                image, mime = take_image(payload)
                return self._json(APP.runtime.vision.analyze(image, mime))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 413)
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        if path == '/api/action':
            # 用户在界面上点「确认生成」/「改配色」等按钮 → 确定性执行，模型不参与
            try:
                sid = payload.get('session_id') or 'default'
                act = payload.get('action')
                # 哪个机器人页面上点的，就只动它自己那份待确认方案 ——
                # 三个机器人共用一个会话，不区分角色就会串台
                who = payload.get('agent') or 'designer'
                # 切还原度这类「只改参数」的动作：重跑提案但不执行、不扣点
                if act == 'set_fidelity':
                    return self._json(APP.runtime.retune_pending(
                        sid, {'fidelity': payload.get('fidelity')}, who))
                # 卡片上改「出几张」：同样是重跑提案，不执行、不扣点，报价跟着张数走
                if act == 'set_count':
                    try:
                        n = int(payload.get('n') or payload.get('count') or 0)
                    except (TypeError, ValueError):
                        n = 0
                    if n < 1:
                        return self._json({"ok": False, "error": "张数得是 1 以上的整数"})
                    return self._json(APP.runtime.retune_pending(sid, {'n': n}, who))
                # 卡片上换路线（纯算法 / 模型补缝）：照旧只重跑提案，不执行、不扣点。
                # 取值不在这里校验 —— 技能自己会把非法的夹回 auto，
                # 并把实际生效的路线回填到提案里，卡片照着它渲染就不会说谎。
                if act == 'set_engine':
                    return self._json(APP.runtime.retune_pending(
                        sid, {'engine': payload.get('engine') or 'auto'}, who))
                # 「以这张库图作底，改一版」：把库里的图读成底图塞进会话
                if act == 'pick_library':
                    return self._json(APP.runtime.pick_library(
                        sid, payload.get('id') or (payload.get('args') or {}).get('id'), who))
                # 界面上的「直通车」按钮（比如四方连续卡片上的「生成 PSD 文件」）：
                # 技能是确定的，没必要绕模型 —— 直接跑一遍提案，仍旧不执行、不扣点。
                if act == 'propose':
                    return self._json(APP.runtime.propose(
                        sid, payload.get('skill') or (payload.get('args') or {}).get('skill'),
                        payload.get('args') or {}, who))
                # 「终止」：把正在跑的算法子进程真的掐掉（不是只断前端那条 fetch）。
                # 前端 fire-and-forget 地发一次，不等结果 —— 用户点终止要的是
                # "立刻停"，不能因为这条请求慢就卡住界面。
                if act == 'cancel_run':
                    return self._json(APP.runtime.cancel_run(sid, who))
                # 「把这张交给织印师」：跨机器人传图**只能这么传** ——
                # 用户点一下，这张图才落进目标角色那一槽（会话记忆本身按角色隔离）
                if act == 'hand_off':
                    return self._json(APP.runtime.hand_off(
                        sid, payload.get('url') or (payload.get('args') or {}).get('url'),
                        payload.get('to') or (payload.get('args') or {}).get('to')
                        or 'engineer'))
                return self._json(APP.runtime.run_action(
                    sid, act, payload.get('args') or {}, who))
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        if path == '/api/export/list':
            # 「导出花型清单」：按会话里记着的"用户已选中花型"生成一张真 xlsx，
            # 回下载地址。确定性执行，不经过模型 —— 界面上点个按钮就该拿到文件，
            # 不该先被问一串问题。
            try:
                return self._json(APP.runtime.export_list(
                    payload.get('session_id') or 'default', payload.get('selected')))
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        if path == '/api/track':
            # 前端埋点：来路、停留时长、点了哪些按钮、什么时候离开。
            # **一律回 ok**，连异常都吞掉 —— 记一笔统计不该有能力让页面报错。
            try:
                return self._json(APP.runtime.track(payload))
            except Exception as e:                       # noqa: BLE001
                print('[track] 埋点失败（不影响用户）：%s: %s' % (type(e).__name__, e))
                return self._json({"ok": True})
        if path == '/api/reload':
            try:
                APP.reload()
                return self._json({"ok": True, "skills": APP.registry.describe()})
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        return self._json({"ok": False, "error": "unknown api"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--open', action='store_true', help='启动后自动打开浏览器')
    args = ap.parse_args()

    host = args.host or APP.cfg.settings['server']['host']
    port = args.port or APP.cfg.settings['server']['port']
    url = 'http://%s:%d/' % (host, port)
    adm = APP.cfg.settings.get('admin') or {}

    print('PatternFlow 用户端（C 端）')
    print('  地址：%s' % url)
    print('  模型：%s @ %s（%s）' % (APP.cfg.model, APP.cfg.base_url,
                                  '已配置 Key' if APP.cfg.llm_enabled else '未配置 Key → 本地规则模式'))
    print('  技能：%s' % ', '.join(s['name'] for s in APP.registry.describe()))
    print('  图库：%d 张   知识库：%d 篇 / %d 问答' % (len(APP.library.items),
                                                len(APP.kb.docs), len(APP.kb.qa)))
    print('  管理后台是另一个进程：http://%s:%d/  （python3 admin_server.py）'
          % (adm.get('host', '127.0.0.1'), adm.get('port', 8788)))
    print('  页面目录：%s' % PROTO_DIR)
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
