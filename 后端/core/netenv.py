"""启动体检：把「指向一个没在跑的本地代理」的环境变量摘掉。

为什么必须有这一步
    这台机器上反复出现同一种故障：代理软件的进程已经退出，但 HTTP_PROXY /
    HTTPS_PROXY 还指在 127.0.0.1:1082 上。标准库（urllib / requests）会老老实实
    走这个代理，于是**所有**出网请求瞬间失败 —— 大模型、识图、生图、云端工作流
    全哑火。用户的体感是「对话突然变笨了、生图报错」，而报错里只有一句"连不上"，
    很难联想到是代理。真踩过：改了半天代码，最后发现是本地 1082 没人监听。

为什么不做成"失败就重试直连"
    那要在每个调用点都塞兜底（llm / imagegen / 云端工作流各一套），容易漏。
    在进程启动时把死代理摘掉，一次解决全部 —— 子进程（PSD 分层、四方连续）是
    fork 出来的，会继承这份干净的环境。

边界
    只处理**本地地址**（localhost / 127.x / ::1 / 0.0.0.0）且**连不上**的。
    远端代理"连不上"和"今天有点慢"分不清，不该在启动时替用户拍板。
"""
import os
import socket
from urllib.parse import urlsplit

PROXY_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy')

_LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1', '0.0.0.0')
PROBE_TIMEOUT = 0.25


def _is_local(host):
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    return host.startswith('127.')


def _alive(url, timeout=PROBE_TIMEOUT):
    """代理地址能不能连上。解析不了就当作"能用"，交回给 urllib 自己处理。"""
    try:
        u = urlsplit(url if '://' in url else 'http://' + url)
    except Exception:                                   # noqa: BLE001
        return True
    host = u.hostname
    if not _is_local(host):
        return True                                     # 远端的不猜，直接放行
    port = u.port or (443 if u.scheme == 'https' else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def sanitize_proxies(verbose=True):
    """摘掉连不上的**本地**代理变量，返回被摘掉的变量名。"""
    dropped = []
    seen = []
    for k in PROXY_VARS:
        v = (os.environ.get(k) or '').strip()
        if not v or _alive(v):
            continue
        os.environ.pop(k, None)
        dropped.append(k)
        if v not in seen:
            seen.append(v)
    if dropped and verbose:
        print('[net] 本地代理连不上（%s），已摘掉 %s —— 这轮改走直连。'
              % ('、'.join(seen), '、'.join(dropped)))
    return dropped
