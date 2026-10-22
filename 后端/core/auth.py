"""管理后台的口令登录。

后台能改密钥、改人设、改知识库、删图，比 C 端危险得多，
所以拆成独立进程之后**必须**有一道门 —— 只靠「监听 127.0.0.1」不够：
同一台机器上别人开个浏览器就能进，哪天有人加个 `--host 0.0.0.0` 就直接裸奔了。

设计上刻意做得轻，不引第三方、不做账号体系：

- 口令存 `config/secrets.json → admin_password`，**不存明文**：存 `salt:hash`；
- 没设口令时，第一次进后台要求设置一个（这时接口对内网开放，是唯一的时间窗）；
- 登录后发 HttpOnly + SameSite=Strict 的 cookie，浏览器自动带上，
  连 `<img>` 取图都不用前端拼 token（拼在 URL 里会被写进访问日志）；
- 令牌只在内存里，进程重启就全部失效；
- 连续输错会越来越慢，挡住在线暴破；
- 写操作再查一遍 Origin/Referer，防 CSRF（SameSite 挡的是跨站，挡不住同机跨端口）。
"""
import hashlib
import hmac
import json
import os
import secrets as _secrets
import threading
import time

from . import sync
from .config import ROOT

TOKEN_TTL = 12 * 3600        # 登录有效期：12 小时
MAX_FAIL = 8                 # 失败次数超过这个就开始拖慢
_MIN_LEN = 4

_TOKENS = {}                 # token -> 过期时间戳
_FAILS = {}                  # 口令错误计数（按进程内存，重启清零）
_LOCK = threading.Lock()


# ---------------- 口令存储 ----------------
def _secrets_path():
    return os.path.join(ROOT, 'config', 'secrets.json')


def _read_secrets():
    try:
        with open(_secrets_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_secrets(sec):
    """只写这一个文件，不走 Config.save_secrets —— 那个函数有密钥防清空逻辑，
    跟口令没关系，绕过去反而更直接。仍然是原子写。
    """
    p = _secrets_path()
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(sec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    sync.rev_bump('auth')


def hash_password(pw, salt=None):
    salt = salt or _secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'),
                            60000).hex()
    return '%s:%s' % (salt, h)


def has_password():
    return bool((_read_secrets().get('admin_password') or '').strip())


def set_password(pw):
    pw = (pw or '').strip()
    if len(pw) < _MIN_LEN:
        return {"ok": False, "error": "口令至少 %d 位" % _MIN_LEN}
    sec = _read_secrets()
    sec['admin_password'] = hash_password(pw)
    _write_secrets(sec)
    return {"ok": True, "note": "口令已设置"}


def change_password(old, new):
    if not verify(old):
        return {"ok": False, "error": "当前口令不对"}
    return set_password(new)


def verify(pw):
    """口令对不对。注意：**慢路径**（pbkdf2 6 万轮）本身就带防暴破效果。"""
    got = (_read_secrets().get('admin_password') or '').strip()
    if not got:
        return False
    try:
        salt, want = got.split(':', 1)
    except ValueError:
        return False
    mine = hash_password(pw or '', salt).split(':', 1)[1]
    return hmac.compare_digest(mine, want)


def clear_password():
    """把口令清掉（下次进后台重新设置）。只在真的需要时手工调。"""
    sec = _read_secrets()
    sec.pop('admin_password', None)
    _write_secrets(sec)
    return {"ok": True}


# ---------------- 登录 ----------------
def _penalty(key='local'):
    """按失败次数递增的等待。第 8 次之后每次多等 1.5 秒，够挡住脚本暴破。"""
    with _LOCK:
        n = _FAILS.get(key, 0)
    if n > MAX_FAIL:
        time.sleep(min(10.0, 1.5 * (n - MAX_FAIL)))


def login(pw):
    _penalty()
    if not has_password():
        return {"ok": False, "need_set": True, "error": "还没设置口令"}
    if not verify(pw):
        with _LOCK:
            _FAILS['local'] = _FAILS.get('local', 0) + 1
        return {"ok": False, "error": "口令不对"}
    with _LOCK:
        _FAILS['local'] = 0
        tok = _secrets.token_urlsafe(24)
        _TOKENS[tok] = time.time() + TOKEN_TTL
    return {"ok": True, "token": tok, "ttl": TOKEN_TTL}


def logout(token):
    with _LOCK:
        _TOKENS.pop(token or '', None)
    return {"ok": True}


def valid(token):
    if not token:
        return False
    with _LOCK:
        now = time.time()
        exp = _TOKENS.get(token)
        if exp is None:
            return False
        if exp < now:
            _TOKENS.pop(token, None)
            return False
        return True


def session():
    """前端一进来先问这个：要不要登录 / 要不要先设口令。"""
    return {"ok": True, "has_password": has_password()}


def sweep():
    """清掉过期令牌。请求进来顺手扫一次，别让字典无限长。"""
    with _LOCK:
        now = time.time()
        for t in [k for k, v in _TOKENS.items() if v < now]:
            _TOKENS.pop(t, None)
