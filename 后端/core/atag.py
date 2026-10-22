"""自动打标签引擎客户端 —— 主程序和「算法/自动打标签」之间的那道墙。

为什么要走子进程（和四方连续、PSD 分层同一个道理）
    后端是**刻意零依赖**的：只用标准库，连 JPEG 解码都是自研的（imgio.py）。
    这个算法要 mlx-vlm + mlx（几个 GB 的轮子），直接 import 等于把
    "要不要装第三方包"从算法层扩散到整个服务 —— 一次装坏，产品起不来。

    所以这里划一道墙：主程序不 import 任何算法代码，只做
        「图写成临时文件 → 起算法自己的 .venv 跑子进程 → 结果 JSON 读回来」
    顺带两个好处：算法环境坏了只影响入库这一个功能；本地模型首次加载
    要几十秒，也不会堵住 HTTP 服务（服务是多线程的，子进程阻塞不影响别的请求）。

它在产品里的位置：**花型库的入库闸门**（全库一道，不分入口）
    凡是要进花型库的图，都要先过这个算法再落库：
        · 后台导入（上传图片 / 选文件夹 / 扫目录）—— admin.import_via_atag
        · 出图产物自动入库（描稿仿稿 / 四方连续的成品）—— runtime._auto_ingest
    两边都走本文件底部的 ingest_blob / ingest_file，"过算法才准存"
    只有一处实现。三个分支都不放行"没打过标就存进去"：
        · 算法说"不是花型"（截图/人物/实景）→ 不进库。库是花型库，不是相册
        · 算法没跑通（引擎没起、模型没装）→ 不进库，告诉运营先去起引擎
        · 跑通了 → 带着四维标签（风格/元素/配色/适用场景）入库
    "都要过一下算法再储存"是硬要求，所以上面三个分支没有一个会放行
    "没打过标就存进去"。想跳过闸门只有一条路：把 atag.enabled 关掉
    （那是明知故犯，运营自己的决定）。

契约（对应 算法/自动打标签/atag/service.py 顶部的说明）
    probe   自检：引擎起没起、模型在不在。只回 ok + 一句人话，
            **不回端点地址、不回密钥** —— 这个结果会上了后台页面被截图。
    one     单张打标。结果 JSON 里带完整记录（四维标签/主色/关键词/描述）。

词表：默认把主程序的 后端/data/vocab.json 传给算法（--vocab）。
    两边同源，标签才能落进库的检索体系里 —— 客户说"蓝白青花"，
    库里存的也得是"蓝白青花"这个词，而不是算法自己的一套说法。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

from .imagegen import sniff

# 项目根（后端/ 的上一层）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 代理环境变量：大小写两份都摘。算法连的是本机引擎（127.0.0.1），
# 本来就不该走代理；万一以后把 base_url 指到云端，也保持全项目一致的直连策略。
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy")

DEFAULT_CONF = {
    "enabled": True,
    "dir": "../算法/自动打标签",   # 相对 后端/
    "python": "",                   # 留空 = 自动找 dir 下的 .venv
    "base_url": "",                 # 留空 = 用算法自己的默认（本机 11435）
    "model": "",                    # 留空 = 用算法自己的默认
    "timeout": 180,                 # 单张超时秒数（本地模型一张约 5~15 秒）
    "max_side": 1280,               # 传给模型的图最长边，够看又不浪费
    "vocab": "",                    # 留空 = 自动用 后端/data/vocab.json
}

# probe 的结果缓存（给页面状态 chip 用，避免每次刷状态都起一个子进程）。
# 闸门自己 probe 时传 fresh=True，不看缓存 —— 放行前的判断必须是真的。
_PROBE_CACHE = {"key": None, "ts": 0.0, "result": None}
_PROBE_TTL = 20.0


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class AtagError(Exception):
    pass


class AtagEngine:
    """给一张图打标签。模型/引擎不可用时 available=False，话见 missing_note。"""

    def __init__(self, config):
        conf = dict(DEFAULT_CONF)
        conf.update((config.settings.get("atag") or {}) if config is not None else {})
        self.conf = conf
        self.enabled = bool(conf.get("enabled"))
        self.root = os.path.normpath(os.path.join(ROOT, conf.get("dir") or "."))
        self.python = (conf.get("python") or "").strip() or self._auto_python()
        self.base_url = (conf.get("base_url") or "").strip()
        self.model = (conf.get("model") or "").strip()
        self.timeout = max(30, int(conf.get("timeout") or 180))
        self.max_side = max(320, int(conf.get("max_side") or 1280))
        self.vocab = (conf.get("vocab") or "").strip() or self._auto_vocab()

    # ---------- 装没装 ----------
    def _auto_python(self):
        for rel in ("bin/python", "Scripts/python.exe"):
            p = os.path.join(self.root, ".venv", rel)
            if os.path.exists(p):
                return p
        return ""

    def _auto_vocab(self):
        p = os.path.join(ROOT, "data", "vocab.json")
        return p if os.path.exists(p) else ""

    @property
    def installed(self):
        return bool(self.python and os.path.exists(self.python)
                    and os.path.exists(os.path.join(self.root, "atag", "service.py")))

    @property
    def available(self):
        """配置层面能不能试（装没装、启没启用）。引擎起没起要看 probe。"""
        return self.enabled and self.installed

    @property
    def missing_note(self):
        """没装好时给运营看的话。要能照着做，别甩一句"环境异常"。"""
        if not self.enabled:
            return "入库自动打标被关掉了（settings.json → atag.enabled=false）。"
        if not os.path.exists(os.path.join(self.root, "atag", "service.py")):
            return ("找不到自动打标签算法的代码（%s）。"
                    "检查 settings.json → atag.dir 是否指向 算法/自动打标签。" % self.root)
        if not self.python:
            return ("自动打标签的算法环境还没装好：到 算法/自动打标签/本地模型/ "
                    "双击「安装.command」（要装 mlx-vlm，几个 GB，装一次就好）。")
        return "自动打标引擎不可用，检查后台配置。"

    # ---------- 自检 ----------
    def probe(self, fresh=False):
        """问一句"引擎起没起、模型在不在"。返回 {ok, ready, note}。

        只回 ok + 一句人话，**不回端点地址、不回模型名** —— 这个结果会
        出现在后台页面上，而后台页面经常被截图丢群里。
        """
        if not self.available:
            return {"ok": False, "ready": False, "note": self.missing_note}
        key = (self.root, self.python, self.base_url, self.model)
        now = time.time()
        if (not fresh and _PROBE_CACHE["key"] == key
                and now - _PROBE_CACHE["ts"] < _PROBE_TTL):
            return dict(_PROBE_CACHE["result"])

        out = {"ok": False, "ready": False, "note": ""}
        workdir = tempfile.mkdtemp(prefix="pf_atag_probe_")
        try:
            out_json = os.path.join(workdir, "probe.json")
            argv = [self.python, "-m", "atag.service", "probe",
                    "--json-out", out_json, "--redact"]
            argv += self._endpoint_args()
            proc = subprocess.run(argv, cwd=self.root, env=self._env(),
                                  timeout=60, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            data = _read_json(out_json) or {}
            ready = bool(data.get("available")) and bool(data.get("model_ready"))
            out = {"ok": bool(data.get("ok")) and ready, "ready": ready,
                   "note": self._ready_note(ready, data, proc)}
        except subprocess.TimeoutExpired:
            out["note"] = "自检等超时了，引擎可能刚在加载模型，过几秒再试。"
        except Exception as e:                           # noqa: BLE001
            out["note"] = "自检没跑起来（%s：%s）" % (type(e).__name__, e)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        _PROBE_CACHE.update({"key": key, "ts": now, "result": dict(out)})
        return out

    def _ready_note(self, ready, data, proc):
        if ready:
            return "就绪：每张导入图都会先过它打标签。"
        if not data:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").strip()[-200:]
            return ("自检没返回结果（退出码 %s）。%s"
                    % (proc.returncode, tail or "没有更多信息"))
        note = data.get("note") or ""
        if not note:
            note = "引擎还没就绪。"
        # 算法自己的话里经常已经指了「启动本地模型.command」，别重复贴一遍
        if "启动本地模型" not in note:
            note += "（起引擎：算法/自动打标签/本地模型/启动本地模型.command）"
        return note

    # ---------- 打标 ----------
    def tag_blob(self, blob, ext=None):
        """给一张图打标签。返回归一化结果，绝不抛异常（调用方要照着分支）。"""
        if not self.available:
            return {"ok": False, "not_pattern": False, "error": self.missing_note}
        if not blob:
            return {"ok": False, "not_pattern": False, "error": "没有图片数据"}
        ext = ext or sniff(blob)[0] or ".png"
        workdir = tempfile.mkdtemp(prefix="pf_atag_")
        try:
            src = os.path.join(workdir, "input" + ext)
            with open(src, "wb") as f:
                f.write(blob)
            out_json = os.path.join(workdir, "result.json")
            argv = [self.python, "-m", "atag.service", "one",
                    "-i", src, "--outdir", os.path.join(workdir, "store"),
                    "--json-out", out_json, "--max-side", str(self.max_side)]
            argv += self._endpoint_args()
            if self.vocab:
                argv += ["--vocab", self.vocab]
            try:
                proc = subprocess.run(argv, cwd=self.root, env=self._env(),
                                      timeout=self.timeout,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.TimeoutExpired:
                return {"ok": False, "not_pattern": False,
                        "error": "打标等超时了（超过 %d 秒）。引擎可能正在加载"
                                 "模型，过一会儿再导入。" % self.timeout}
            except Exception as e:                       # noqa: BLE001
                return {"ok": False, "not_pattern": False,
                        "error": "起不动打标引擎（%s：%s）" % (type(e).__name__, e)}
            data = _read_json(out_json)
            if data is None:
                tail = (proc.stderr or b"").decode("utf-8", "ignore").strip()[-300:]
                return {"ok": False, "not_pattern": False,
                        "error": "打标引擎没返回结果（退出码 %s）。%s"
                                 % (proc.returncode, tail or "没有更多信息")}
            return self._normalize(data)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _normalize(data):
        """算法的记录 → 主程序能直接用的形状。

        四维在算法那边是**列表**（一维可以多条），在花型库这边是**单值**
        （检索按精确匹配打分，见 library.search）。所以这里取每条的第一个
        当主标签，完整列表和词表外原话一起放进 raw —— 详情弹层和排查问题时
        看得见全貌，检索体系不受影响。
        """
        rec = data.get("record") or {}
        status = rec.get("status") or ""
        out = {
            "ok": False, "not_pattern": False, "empty": False,
            "status": status, "error": "",
            "style": [], "element": [], "color": [], "scene": [],
            "free": {}, "colors": [], "keywords": [],
            "desc": "", "reason": "", "confidence": None,
            "elapsed_ms": rec.get("elapsed_ms"),
            "prompt_version": rec.get("prompt_version") or "",
            "model": rec.get("model") or "",
            "raw": rec,
        }
        if not data.get("ok"):
            out["error"] = data.get("error") or "打标没跑通。"
            return out
        if status == "not_pattern":
            out["not_pattern"] = True
            out["error"] = ("这张图看起来不是面料花型（%s）。"
                            % (rec.get("reason") or "画面与花型无关"))
            return out
        if status == "failed":
            out["error"] = rec.get("error") or "这次没打出标签，稍后再试。"
            return out
        for d in ("style", "element", "color", "scene"):
            vals = [str(v).strip() for v in (rec.get(d) or []) if str(v).strip()]
            out[d] = vals
        out["free"] = {d: list((rec.get("free") or {}).get(d) or [])
                       for d in ("style", "element", "color", "scene")}
        out["colors"] = list(rec.get("colors") or [])
        out["keywords"] = list(rec.get("keywords") or [])
        out["desc"] = rec.get("desc") or ""
        out["reason"] = rec.get("reason") or ""
        out["confidence"] = rec.get("confidence")
        out["empty"] = (status == "empty")
        out["ok"] = True
        return out

    @staticmethod
    def to_library_tags(res):
        """归一化结果 → 花型库条目要的标签形状（单值维度）。

        跟 core/tagger.py 的输出保持同构，这样 _store/_build_item 那边
        不用区分"标签是谁打的"。纯函数：不碰 self，测试里直接拿着用。
        """
        if not res or not res.get("ok"):
            return None
        tags = {"ok": True, "name": "", "desc": res.get("desc") or "",
                "fabric": "", "reason": res.get("reason") or "",
                "colors": res.get("colors") or [],
                "keywords": res.get("keywords") or [],
                "raw": res.get("raw") or {}, "rejected": {},
                "tag_engine": "atag"}
        for d in ("style", "element", "color", "scene"):
            vals = res.get(d) or []
            tags[d] = vals[0] if vals else None
        # 词表外原话：库里有 rejected 这个字段（"AI 说的是什么"），
        # 四维各留第一条，别把详情弹层撑爆
        for d, vals in (res.get("free") or {}).items():
            if vals:
                tags["rejected"][d] = str(vals[0])[:12]
        # 一维多标的其余几条并进关键词 —— 客户真会那么说，丢了可惜
        extra = []
        for d in ("style", "element", "color", "scene"):
            extra.extend((res.get(d) or [])[1:])
        for d, vals in (res.get("free") or {}).items():
            extra.extend(vals)
        for w in extra:
            w = str(w).strip()
            if w and w not in tags["keywords"] and len(tags["keywords"]) < 8:
                tags["keywords"].append(w)
        return tags

    # ----------  plumbing ----------
    def _endpoint_args(self):
        argv = []
        if self.base_url:
            argv += ["--base-url", self.base_url]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def _env(self):
        env = dict(os.environ)
        for k in PROXY_ENV_KEYS:
            env.pop(k, None)
        env["PYTHONPATH"] = self.root + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PYTHONWARNINGS", "ignore")
        return env

    def describe(self):
        """给后台页面用的自检信息。**不含端点地址和密钥**（页面会被截图）。"""
        return {"enabled": self.enabled, "installed": self.installed,
                "available": self.available,
                # available 时 note 留空 —— missing_note 是"没装好时怎么说"，
                # 可用还返回一句"不可用"会被人当成 bug 报上来
                "note": "" if self.available else self.missing_note,
                "python": self.python,
                "max_side": self.max_side,
                "own_base_url": bool(self.base_url),
                "own_model": bool(self.model),
                "use_main_vocab": bool(self.vocab)}


# ---------- 闸门：所有进花型库的图都走这一道 ----------
def _stem(src_name):
    """文件名去扩展名当花型名。atag 不起名（那是设计师的活），
    而"未命名-0031"对运营毫无意义 —— 原图叫什么，库里就先叫什么，
    不满意详情弹层里改。"""
    base = os.path.basename(str(src_name or '').strip())
    stem = os.path.splitext(base)[0].strip()
    return stem or None


def ingest_blob(engine, lib, blob, mime='image/png', src_name=None, name=None,
                source='import', ready=None):
    """闸门 + 入库。三个分支都不放行"没打过标就存进去"：
        算法没就绪 → 不入库，告诉运营去起引擎
        不是花型   → 不入库（库是花型库，不是相册）
        跑通了     → 带着四维标签入库

    后台导入（admin.import_via_atag）和出图产物自动入库
    （runtime._auto_ingest 里的 trace/seamless）共用这一条路 ——
    "入库的图都要过算法"是全库级规矩，只许有一处实现。

    ready=None 自己探一次引擎（单张走这条）；
    批量传 True —— 开跑前已经探过了，没必要每张再探一遍。
    """
    if engine is None or not engine.available:
        return {"ok": False,
                "error": engine.missing_note if engine else '打标引擎没装'}
    if ready is None:
        pr = engine.probe(fresh=True)
        if not pr.get('ready'):
            return {"ok": False,
                    "error": "自动打标还没就绪：%s" % (pr.get('note') or '引擎没起')}
    elif not ready:
        return {"ok": False, "error": engine.missing_note}

    res = engine.tag_blob(blob)
    if res.get('not_pattern'):
        return {"ok": False, "not_pattern": True, "error": res.get('error')}
    if not res.get('ok'):
        return {"ok": False,
                "error": "自动打标没跑通，没有入库：%s" % (res.get('error') or '未知原因')}
    tags = engine.to_library_tags(res)
    return lib.ingest_bytes(blob, source=source, src_name=src_name,
                            name=(name or '').strip() or _stem(src_name),
                            tag=False, check_pattern=False,
                            preset_tags=tags)


def ingest_file(engine, lib, path, source='import', ready=None):
    """文件版闸门（后台扫目录 / 产物自动入库共用）。"""
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": "读不了这个文件：%s" % e}
    return ingest_blob(engine, lib, blob, src_name=os.path.basename(path),
                       source=source, ready=ready)
