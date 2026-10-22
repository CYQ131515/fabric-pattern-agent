"""花型生成引擎 —— 调 `算法/花型生成`（pattern_studio）。

为什么换
    老实现（core/imagegen.ImageGen）把「风格/元素/配色/场景」拼成一段提示词，
    直接调文生图模型出一张图：画面里元素随缘、排版随缘、接缝随缘。要拿去印花，
    还得再走一遍四方连续。
    新算法（`算法/花型生成`，pattern_studio）是**一条设计流水线**：
        需求解析 → 九宫格元素图 → 拆格去背 → 环面排版（不重叠）→ 自动拼版
        → 3×3 环绕合成 → 逐像素无缝自检 → AI 审核
    每一版输出本身就是**可平铺的无缝 tile**，元素数量/排布/大小各不相同。

为什么走子进程
    算法要 numpy + Pillow，而后端是刻意的零依赖（连 JPEG 解码都是自研的）。
    所以这里沿用 `core/algo.py` 那道墙：用算法自带的 uv 跑
    `python -m pattern_studio`，进程组/超时/终止都复用同一套实现。
    主程序不 import 算法代码，装坏了也只影响这一个功能。

对 skill handler 的契约（和 ImageGen 保持一致，handler 只换发动机）
    .enabled / .available / .cost / .variants / .describe()
    .generate(keywords, n=..., tag='gen') → {"files": [...], "cost": 点数, ...}

关键词从哪来
    handler 把需求卡 + 识图标签映射成算法认的中文关键词（见 build_keywords）。
    算法自己再做规则解析 + 大模型精修，英文提示词由它的模板生成，不由后端拼。
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import shutil
import tempfile
import time

from .algo import BACKEND_ROOT, Algo, AlgoError, _env_file_value
from .imagegen import ImageGen, ImageGenError

DEFAULT_CONF = {
    "enabled": True,
    "dir": "../算法/花型生成",
    # 留空 = 复用 settings.algo 的 uv（算法目录自带的 .runtime/uv）
    "uv": "",
    # 留空 = 自动用算法目录的 .venv/bin/python（numpy+Pillow 预装，零网络）。
    # 线上强烈建议保持留空：uv 每次联网解析依赖，外网一断就卡成
    # "退出码 2 / operation timed out"。
    "python": "",
    # 生图网关：apimart（生产）/ mock（离线自检）
    "provider": "apimart",
    # 点数口径沿用生图：每版 cost 点
    "cost": 60,
    # 一版 = 一张可直接平铺的 tile；默认 4 版（和前端 1/2/4 张按钮对齐）
    "variants": 4,
    "profile": "balanced",          # cheapest / balanced / fast / best
    "presets": "halfdrop,phyllotaxis,scatter,grid",
    "tile": 1024,
    "review": "auto",               # off / auto / strict
    "bg_mode": "auto",
    "element_scale": 1.0,
    "seed": 0,                      # 0 = 每次随机（「重新生成」才出得来新排布）
    "llm": True,                    # 需求解析用算法的对话模型精修（token 成本可忽略）
    "timeout": 900,
    # 研发单跑时可以让它读算法目录自己的 .env；产品默认只认后端 secrets，
    # 否则后台自检会把"本机残留的研发 key"误判成已配置。
    "use_algo_env": False,
}

# ---- 需求卡受控词 → 算法关键词 -------------------------------------------
# pattern_studio 的词典是按中文设计语义写的（国风/水彩/青花/牡丹/缠枝…），
# 这里把产品词表翻过去；翻不出来的自由词按原话透传，算法会当自由文本解析。
STYLE_KW = {
    "法式浪漫": "法式 水彩 小碎花",
    "新中式国风": "国风 工笔",
    "复古波普": "复古 手绘",
    "现代极简": "极简",
    "童趣插画": "手绘 动物",
    "波西米亚": "巴洛克 手绘",
    "Y2K未来": "几何 极简",
    "田园草木": "水彩 叶片 缠枝",
}
ELEMENT_KW = {
    "花卉": "牡丹 玫瑰",
    "叶片藤蔓": "缠枝 叶片",
    "几何": "几何",
    "动物纹": "蝴蝶 鸟",
    "条纹格纹": "几何",
    "波点": "极简 圆点",
    "抽象笔触": "水墨",
    "海洋贝壳": "水波",
}
COLOR_KW = {
    "莫兰迪灰调": "烟灰 月白",
    "马卡龙甜彩": "丁香 藕荷 月白",
    "黑白灰": "墨 烟灰 月白",
    "高饱和撞色": "",
    "大地色系": "赭石 茶 米白",
    "蓝白青花": "青花 蓝白",
    "粉紫梦幻": "藕荷 丁香",
    "暖橙焦糖": "杏 赭石",
}
SCENE_KW = {
    "女装连衣裙": "服装",
    "童装": "服装",
    "家居布艺": "家居 布艺",
    "丝巾配饰": "丝巾",
    "衬衫正装": "服装",
    "运动休闲": "服装 印花",
    "箱包鞋帽": "包装",
}
# 构图 → 排版预设；密度/尺度是算法原生参数，直接映射。
COMPOSITION_PRESET = {
    "散点": "scatter", "满地": "halfdrop", "定位": "grid",
    "条纹": "stripes", "边花": "radial",
}
DENSITY_ARG = {"疏": 0.26, "中": 0.40, "密": 0.55}
SCALE_ARG = {"小碎花": 0.82, "中花": 1.0, "大花": 1.45}


def _first(d, keys):
    for k in keys:
        v = (d or {}).get(k)
        if v:
            return v
    return None


def build_keywords(brief=None, extra=None, addon="", fallback="花卉 印花"):
    """需求卡 + 识图标签 + 用户原话 → 算法关键词串。

    顺序：用户本轮原话在前（权重最高，算法会先看见），再风格/元素/配色/场景。
    词表外的值原样透传 —— 算法有自由词兜底，不要在后端替用户改说法。
    """
    brief = brief or {}
    extra = extra or {}
    parts = []
    addon = (addon or "").strip()
    if addon:
        parts.append(addon)
    for dim, table in (("style", STYLE_KW), ("element", ELEMENT_KW),
                       ("color", COLOR_KW), ("scene", SCENE_KW)):
        val = brief.get(dim) or extra.get(dim)
        if not val:
            continue
        val = str(val).strip()
        mapped = table.get(val)
        if mapped:
            parts.append(mapped)
        elif dim == "color":
            # 自由色名（「淡红」「雾霾蓝」）算法词典也认一部分，认不出就原样当文本
            parts.append(val)
        elif dim != "scene":
            parts.append(val)
    text = " ".join(p for p in parts if p).strip()
    text = re.sub(r"\s+", " ", text)
    return text or fallback


def presets_for(extra=None, default=""):
    """识图给了构图就按构图选预设，否则用配置里的默认预设串。"""
    comp = _first(extra or {}, ("composition",))
    return COMPOSITION_PRESET.get(comp) or default or None


def density_for(extra=None):
    return DENSITY_ARG.get(_first(extra or {}, ("density",)))


def scale_for(extra=None):
    return SCALE_ARG.get(_first(extra or {}, ("scale",)))


class PatternEngine:
    """花型生成：关键词 → N 版无缝花型 tile。"""

    def __init__(self, config):
        conf = dict(DEFAULT_CONF)
        conf.update(config.settings.get("patgen") or {})
        self.conf = conf
        self.cfg = config
        self.enabled = bool(conf.get("enabled", True))
        self.root = os.path.normpath(os.path.join(BACKEND_ROOT, conf.get("dir") or "."))
        # 运行时/网关地址复用 algo 桥；**密钥以后端 secrets 为准**。
        # 算法目录自己的 .env 只是研发单跑时用的，默认不读（否则后台自检会
        # 把别人机器上的研发 key 当成"已配置"，配没配就看不出来了）。
        self.algo = Algo(config)
        self.uv = (str(conf.get("uv") or "")).strip() or self.algo.uv
        # 优先用算法目录自己的 .venv：装了 numpy+Pillow 后完全离线起进程，
        # 不再让 uv 在请求路径上去解析/下载依赖（PyPI 不通时会卡到超时）。
        self.python = (str(conf.get("python") or "")).strip() or os.path.join(
            self.root, ".venv", "bin", "python")
        if not os.path.exists(self.python):
            self.python = ""
        self.provider = str(conf.get("provider") or "apimart")
        self.use_algo_env = bool(conf.get("use_algo_env", False))
        self._cfg_key = (os.environ.get("PATTERNFLOW_PATGEN_KEY")
                         or config.secrets.get("seamless_api_key")
                         or os.environ.get("PATTERNFLOW_SEAMLESS_KEY")
                         or "").strip()
        self._file_key = (_env_file_value(os.path.join(self.root, ".env"),
                                         "PATTERN_STUDIO_APIMART_KEY")
                          or _env_file_value(os.path.join(self.root, ".env"),
                                             "APIMART_KEY"))
        self._file_base = (_env_file_value(os.path.join(self.root, ".env"),
                                          "PATTERN_STUDIO_APIMART_BASE")
                           or _env_file_value(os.path.join(self.root, ".env"),
                                              "APIMART_BASE"))
        self._cfg_base = str(conf.get("base_url") or "").strip()
        img = config.settings.get("image") or {}
        self.cost = int(conf.get("cost") or img.get("cost") or 60)
        self.variants = max(1, int(conf.get("variants") or 4))
        self.profile = str(conf.get("profile") or "balanced")
        self.presets = str(conf.get("presets") or "")
        self.tile = max(512, int(conf.get("tile") or 1024))
        self.review = str(conf.get("review") or "auto")
        self.bg_mode = str(conf.get("bg_mode") or "auto")
        self.element_scale = float(conf.get("element_scale") or 1.0)
        self.seed = int(conf.get("seed") or 0)
        self.llm = bool(conf.get("llm", True))
        self.timeout = max(120, int(conf.get("timeout") or 900))
        self._saver = None

    # ---------- 可用性 ----------
    @property
    def _forced_mock(self):
        """测试/演示：主程序把生图切到 mock 时，别真去调算法（会花钱/联网）。"""
        return (os.environ.get("PATTERNFLOW_IMAGE_PROVIDER") or "").strip().lower() == "mock" \
            or (os.environ.get("PATTERNFLOW_PATGEN") or "").strip().lower() in ("0", "off", "false")

    @property
    def installed(self):
        has_code = os.path.exists(os.path.join(self.root, "pattern_studio", "__main__.py"))
        has_runner = bool(self.python) or bool(self.uv and os.path.exists(self.uv))
        return bool(has_code and has_runner)

    @property
    def key(self):
        # 生产只认后端 secrets / 环境变量；算法目录 .env 要显式开 use_algo_env 才读。
        if self._cfg_key:
            return self._cfg_key
        return (self._file_key or "").strip() if self.use_algo_env else ""

    @property
    def base(self):
        if self._cfg_base:
            return self._cfg_base
        if self.use_algo_env and self._file_base:
            return self._file_base
        return (self.algo.base or "https://api.apimart.ai/v1").strip()

    @property
    def available(self):
        return bool(self.enabled and not self._forced_mock and self.installed
                    and (self.provider == "mock" or self.key))

    @property
    def missing_note(self):
        if not self.enabled:
            return "新版花型生成没启用（settings.json → patgen.enabled）。"
        if self._forced_mock:
            return "当前是 mock 生图模式，不调用花型生成算法。"
        if not os.path.isdir(self.root):
            return ("找不到花型生成算法目录（%s）。检查 settings.json → patgen.dir。"
                    % self.root)
        if not os.path.exists(os.path.join(self.root, "pattern_studio", "__main__.py")):
            return "花型生成算法不完整（缺 pattern_studio/__main__.py）。"
        if not self.python and not (self.uv and os.path.exists(self.uv)):
            return ("花型生成的 Python 运行时没装好。跑一下"
                    "「算法/花型生成/安装.command」建本地 .venv（numpy + Pillow）。")
        if not self.key and self.provider != "mock":
            return ("还没配 APIMART 的 key，花型生成出不了图。"
                    "到后台「模型与密钥」填 seamless_api_key（或环境变量 APIMART_KEY）。")
        return "花型生成引擎不可用，检查后台配置。"

    def describe(self):
        return {"enabled": self.enabled, "available": self.available,
                "root": self.root, "uv": self.uv, "python": self.python,
                "provider": self.provider,
                "profile": self.profile, "variants": self.variants,
                "cost": self.cost, "review": self.review,
                "tile": self.tile, "has_key": bool(self.key),
                "forced_mock": self._forced_mock,
                "installed": self.installed}

    def cancel(self):
        return self.algo.cancel()

    @property
    def running(self):
        return self.algo.running

    # ---------- 落盘 ----------
    @property
    def saver(self):
        """只借用 ImageGen 的落盘/地址逻辑（不调模型）。"""
        if self._saver is None:
            self._saver = ImageGen(self.cfg)
        return self._saver

    # ---------- 生成 ----------
    def generate(self, prompt, image=None, image_mime="image/png", n=1, tag="gen",
                 presets=None, density=None, element_scale=None):
        """出一批无缝花型 tile。

        prompt 是中文关键词串（不是老那种长提示词）；n = 版数。
        返回形状与 ImageGen.generate 对齐，handler 不用分叉。
        """
        if image:
            raise ImageGenError("花型生成引擎只做文生图；改图请走定向改图。")
        if not self.available:
            raise ImageGenError(self.missing_note)
        try:
            n = max(1, min(6, int(n or 1)))
        except (TypeError, ValueError):
            n = max(1, self.variants)
        keywords = (prompt or "").strip()
        if not keywords:
            raise ImageGenError("没有可用的需求关键词，先补一下风格/元素再生成。")

        work = tempfile.mkdtemp(prefix="patgen_")
        outdir = os.path.join(work, "out")
        os.makedirs(outdir, exist_ok=True)
        try:
            argv = ["run", keywords,
                    "--out", outdir,
                    "--prints", str(n),
                    "--profile", self.profile,
                    "--provider", self.provider,
                    "--tile", str(self.tile), str(self.tile),
                    "--review", self.review,
                    "--bg-mode", self.bg_mode,
                    "--seed", str(self._seed())]
            use_presets = presets or self.presets
            if use_presets:
                argv += ["--presets", str(use_presets)]
            dens = density if density is not None else None
            if dens is not None:
                argv += ["--density", str(float(dens))]
            scale = element_scale if element_scale is not None else self.element_scale
            if scale and float(scale) != 1.0:
                argv += ["--element-scale", str(float(scale))]
            if not self.llm:
                argv.append("--no-llm")

            env = {"PATTERN_STUDIO_APIMART_BASE": self.base,
                   "PATTERN_STUDIO_APIMART_KEY": self.key,
                   "APIMART_BASE": self.base,
                   "APIMART_KEY": self.key,
                   "PYTHONUNBUFFERED": "1"}
            try:
                code, out, err = self._run_algorithm(argv, env)
            except AlgoError as e:
                raise ImageGenError(self._friendly_failure(None, "", str(e)))

            tiles = self._collect_tiles(outdir)
            if code != 0 or not tiles:
                raise ImageGenError(self._friendly_failure(code, out, err))
            blobs = []
            for p in tiles:
                with open(p, "rb") as f:
                    blobs.append(f.read())
            meta = self._meta(outdir)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        try:
            saved = self.saver.save(blobs, tag=tag)
        except Exception as e:                           # noqa: BLE001
            raise ImageGenError("花型生成出图了，但产物没存下来（%s: %s）"
                                % (type(e).__name__, e))
        files = list(saved.get("files") or [])
        if not files:
            raise ImageGenError("花型生成没产出可用的图。")
        return {"files": files, "count": len(files),
                "cost": self.cost * len(files), "mock": False,
                "provider": "pattern-studio",
                "model": meta.get("models") or self.profile,
                "prints": len(files), "cost_usd": meta.get("cost_usd"),
                "verify": meta.get("verify") or []}

    # ---------- 内部 ----------
    def _run_algorithm(self, argv, env):
        """起算法进程：优先本地 .venv（零网络），没有才退回 uv。

        退回 uv 时先加 `--offline`：依赖在缓存里就直接起，**不会**因为外网断了
        卡在"解析依赖"上；只有离线确实缺依赖时，才联网再试一次（首次安装）。
        """
        if self.python:
            return self.algo.run_python(self.python, "pattern_studio", argv,
                                        timeout=self.timeout, cwd=self.root,
                                        env_extra=env)
        code, out, err = self.algo.run_py_module(
            "pattern_studio", argv, timeout=self.timeout,
            cwd=self.root, env_extra=env, offline=True)
        if code != 0 and self._looks_like_deps_network(err + "\n" + out):
            code, out, err = self.algo.run_py_module(
                "pattern_studio", argv, timeout=self.timeout,
                cwd=self.root, env_extra=env, offline=False)
        return code, out, err

    @staticmethod
    def _looks_like_deps_network(text):
        """uv 取依赖失败的特征（不是生图超时，别混为一谈）。"""
        low = (text or "").lower()
        marks = ("operation timed out", "failed to fetch", "failed to download",
                 "network was disabled", "no solution found when resolving",
                 "pypi.org", "registry")
        return any(m in low for m in marks)

    def _friendly_failure(self, code, out, err):
        """把算法/运行时的报错翻译成用户能照着做的一句话。"""
        blob = (err or "") + "\n" + (out or "")
        low = blob.lower()
        if code is None:
            return "花型生成没起来（%s）。" % (str(err).strip()[:200] or "未知原因")
        if self._looks_like_deps_network(blob):
            return ("花型生成的运行环境没准备好（取不到 numpy/Pillow，uv 连不上依赖源）。"
                    "跑一下「算法/花型生成/安装.command」建本地 .venv，再点一次确认生成。")
        if ("urlerror" in low or "urlopen" in low or "timed out" in low
                or "timeout" in low or "connection" in low):
            return ("花型生成调用云端超时：连不上 APIMART，检查网络/代理后重试。"
                    "（算法本身没问题，这一步必须联网。）")
        lines = [ln.strip() for ln in blob.strip().splitlines() if ln.strip()]
        tail = " ".join(lines[-2:])[:300]
        return "花型生成没跑通（退出码 %s）。%s" % (code, tail or "算法没有产出图")

    def _seed(self):
        """seed=0 → 每次随机：否则「重新生成」永远出同一套排布。"""
        return self.seed if self.seed else (int(time.time() * 1000) % 1000000
                                            or random.randint(1, 999999))

    @staticmethod
    def _collect_tiles(outdir):
        """取 drafts/draft_*_tile.png，按版号排序（不是字典序：draft_10 会在 draft_2 前）。"""
        def idx(p):
            m = re.search(r"draft_(\d+)_tile\.png$", p)
            return int(m.group(1)) if m else 0
        return sorted(glob.glob(os.path.join(outdir, "drafts", "draft_*_tile.png")), key=idx)

    @staticmethod
    def _meta(outdir):
        """成本 + 自检结论（缺文件也不影响出图）。"""
        cost_usd, models = None, ""
        try:
            with open(os.path.join(outdir, "cost.json"), encoding="utf-8") as f:
                cost = json.load(f)
            cost_usd = round(float(cost.get("total_usd") or 0), 5)
            names = []
            for c in (cost.get("calls") or []):
                m = str(c.get("model") or "").strip()
                if m and m not in names:
                    names.append(m)
            models = " / ".join(names[:3])
        except Exception:                                # noqa: BLE001
            pass
        verify = []
        try:
            with open(os.path.join(outdir, "verify.json"), encoding="utf-8") as f:
                verify = json.load(f) or []
        except Exception:                                # noqa: BLE001
            pass
        return {"cost_usd": cost_usd, "models": models, "verify": verify}
