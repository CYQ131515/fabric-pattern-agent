"""新算法桥接层 —— 后端和 `算法/描稿-四方连续-psd分层` 之间唯一的通道。

为什么要单独一层
    后端的三个出图功能（描稿 / 四方连续 / 分层 PSD）原来各接一个老算法工程，
    现在统一换成新算法（Node `.mjs` 步骤 + Python 步骤）。三处调用长得一样：
        「图写临时文件 → 起算法子进程 → 读回 JSON / 产物」
    所以把「起进程、注入环境、解析 JSON、超时、杀进程」收在这一层，三处共用。

运行时从哪来
    算法目录自带 `.runtime/`（node v24.21.0 + sharp + uv），由迁移时的环境备份
    拷入，**不依赖系统或 DSH 的 Node**。路径都能在 settings.json → algo 覆盖。

超时 / 终止
    子进程用 core.proc.spawn 起（自成进程组），终止时 kill_tree 连它自己的
    子进程一起收掉 —— Node 步骤里还可能再起子进程（sharp 线程、uv python），
    只杀最外层会留下孤儿继续烧云端费用。这条和 core/proc.py 顶部的说明一致。
"""
from __future__ import annotations

import json
import re
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request

from .proc import kill_tree, spawn

# 后端/ 的上一层 = 项目根（算法目录相对它配置）
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONF = {
    "dir": "../算法/描稿-四方连续-psd分层",
    # 算法自己的网关/key（APIMART）。留空 = 用 settings.seamless 的老配置，
    # 再没有就用算法目录的 .env —— 后台换了 key，这里能立刻生效。
    "base_url": "",
    "api_key_env": "PATTERNFLOW_ALGO_KEY",
    # 留空 = 依次尝试：算法目录自带 .runtime → PATH
    "node": "",
    "sharp": "",
    "uv": "",
    # 成品的默认尺寸：300dpi 下 9382px ≈ 79.4cm 门幅
    "height": 9382,
    # 色层数：k 个调色板色，其中一个作背景，所以色层 = k-1
    "k": 8,
    # 四方连续是否用 LLM planner 读经验库（false = 只用先验规则，零成本）
    "planner": False,
    "model_clean": "gpt-image-2.5-sunburst",
    "model_seam": "gpt-image-2.5-ext",
    "band": 140,
    "feather": 20,
    # 单个步骤 / 整次调用的上限（秒）
    "timeout_step": 600,
    "timeout_total": 1800,
}

# 头部快速读 .env 里的 APIMART 配置（只读 key 是否存在，不解析完整格式）
def _env_file_value(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _first_exec(paths):
    for p in paths:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def _first_dir(paths):
    for p in paths:
        if p and os.path.isdir(p):
            return p
    return ""


class AlgoError(Exception):
    """算法侧的失败（超时、没返回结果、步骤退出非零……），消息给用户看。"""


class Algo:
    def __init__(self, config):
        conf = dict(DEFAULT_CONF)
        conf.update(config.settings.get("algo") or {})
        self.conf = conf
        self.root = os.path.normpath(os.path.join(BACKEND_ROOT, conf.get("dir") or "."))
        seam = config.settings.get("seamless") or {}
        self.base_url = ((conf.get("base_url") or "").strip()
                         or (seam.get("base_url") or "").strip()
                         or "https://api.apimart.ai/v1").rstrip("/")
        # 后台「模型与密钥 → 织印师 → 四方连续」那把 key 就是 APIMART 的 key，
        # 三个功能共用（描稿/四方连续都要走 APIMART 的图像模型）。
        self._api_key = (os.environ.get(conf.get("api_key_env") or "PATTERNFLOW_ALGO_KEY")
                         or config.secrets.get("seamless_api_key")
                         or os.environ.get("PATTERNFLOW_SEAMLESS_KEY")
                         or "").strip()
        runtime = os.path.join(self.root, ".runtime")
        self.node = (conf.get("node") or "").strip() or _first_exec([
            os.path.join(runtime, "node", "bin", "node"),
            shutil.which("node") or "",
        ])
        self.sharp = (conf.get("sharp") or "").strip() or _first_dir([
            os.path.join(runtime, "node_modules", "sharp"),
        ])
        self.uv = (conf.get("uv") or "").strip() or _first_exec([
            os.path.join(runtime, "uv", "uv"),
            shutil.which("uv") or "",
            os.path.expanduser("~/.local/bin/uv"),
        ])
        self.height = int(conf.get("height") or 9382)
        self.k = max(2, int(conf.get("k") or 8))
        self.planner = bool(conf.get("planner"))
        self.model_clean = (conf.get("model_clean") or "").strip() or "gpt-image-2.5-sunburst"
        self.model_seam = (conf.get("model_seam") or "").strip() or "gpt-image-2.5-ext"
        self.band = int(conf.get("band") or 140)
        self.feather = int(conf.get("feather") or 20)
        self.timeout_step = int(conf.get("timeout_step") or 600)
        self.timeout_total = int(conf.get("timeout_total") or 1800)
        # 正在跑的那一次（给 progress / cancel 用）
        self._current = None

    # ---------- 可用性 ----------
    def step(self, name):
        return os.path.join(self.root, "steps", name)

    @property
    def installed(self):
        return bool(self.node and self.sharp and self.uv
                    and os.path.exists(self.step("01-clean.mjs"))
                    and os.path.exists(self.step("04-layers-color.py")))

    @property
    def missing_note(self):
        if not os.path.isdir(self.root):
            return ("找不到新算法目录（%s）。检查 settings.json → algo.dir 是否指向 "
                    "算法/描稿-四方连续-psd分层。" % self.root)
        if not self.node:
            return ("新算法缺少 Node 运行时（算法目录下 .runtime/node/bin/node），"
                    "检查 settings.json → algo.node。")
        if not self.sharp:
            return ("新算法缺少 sharp（算法目录下 .runtime/node_modules/sharp），"
                    "检查 settings.json → algo.sharp。")
        if not self.uv:
            return ("新算法缺少 uv（算法目录下 .runtime/uv/uv），"
                    "检查 settings.json → algo.uv。")
        return "新算法文件不完整（steps/ 下缺步骤文件），检查算法目录。"

    @property
    def key(self):
        """算法可用的 APIMART key：后台配置的 > 环境变量 > 算法目录 .env。"""
        return (self._api_key
                or os.environ.get("APIMART_KEY")
                or _env_file_value(os.path.join(self.root, ".env"), "APIMART_KEY")
                or "").strip()

    @property
    def base(self):
        return (os.environ.get("APIMART_BASE")
                or _env_file_value(os.path.join(self.root, ".env"), "APIMART_BASE")
                or self.base_url or "").strip()

    def describe(self):
        return {"root": self.root, "node": self.node, "sharp": self.sharp,
                "uv": self.uv, "height": self.height, "k": self.k,
                "planner": self.planner, "has_key": bool(self.key),
                "installed": self.installed}

    # ---------- 进程 ----------
    # ---------- 网络代理 ----------
    @staticmethod
    def _proxy_state():
        """返回 (proxy_url, alive)。

        代理来源：环境变量 + macOS 系统代理（urllib 会读 _scproxy）。
        alive 用 0.35s TCP 探测 —— 死代理比没代理更糟：会让本可直连
        （TUN/fake-IP）的算法请求全部超时。
        """
        try:
            proxies = urllib.request.getproxies()
        except Exception:                                # noqa: BLE001
            proxies = {}
        url = (proxies.get("https") or proxies.get("http") or "").strip()
        if not url:
            return "", False
        try:
            u = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
            host = u.hostname
            port = u.port or (443 if u.scheme == "https" else 80)
            if not host:
                return url, False
            with socket.create_connection((host, port), timeout=0.35):
                return url, True
        except Exception:                                # noqa: BLE001
            return url, False

    def _env(self):
        env = dict(os.environ)
        if self.sharp:
            env["SHARP_PATH"] = self.sharp
        if self.uv:
            env["PATH"] = os.path.dirname(self.uv) + os.pathsep + env.get("PATH", "")
        if self.base_url:
            env["APIMART_BASE"] = self.base_url
        if self._api_key:
            env["APIMART_KEY"] = self._api_key
        env.setdefault("PYTHONWARNINGS", "ignore")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # 代理：可用的系统代理显式注入（Python 直接用，Node 用 --use-env-proxy 读）；
        # 代理不可用就 NO_PROXY=* 强制直连，别让死代理把所有请求拖死。
        proxy, alive = self._proxy_state()
        if alive:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[k] = proxy
            extra = ["127.0.0.1", "localhost", "::1"]
            cur = (env.get("NO_PROXY") or env.get("no_proxy") or "")
            parts = [x.strip() for x in cur.split(",") if x.strip()] + extra
            env["NO_PROXY"] = env["no_proxy"] = ",".join(dict.fromkeys(parts))
        else:
            env["NO_PROXY"] = env["no_proxy"] = "*"
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.pop(k, None)
        return env

    def _run(self, argv, timeout, cwd=None, env_extra=None):
        if self._current is not None:
            raise AlgoError("算法正忙：上一步还没结束。")
        cur = {"argv": argv, "proc": None, "cancelled": False}
        self._current = cur
        env = self._env()
        if env_extra:
            env.update({str(k): str(v) for k, v in env_extra.items()})
        try:
            proc = spawn(argv, cwd=cwd or self.root, env=env)
        except Exception as exc:                         # noqa: BLE001
            self._current = None
            raise AlgoError("起算法进程失败（%s：%s）" % (type(exc).__name__, exc))
        cur["proc"] = proc
        if cur["cancelled"]:
            kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:                            # noqa: BLE001
                pass
            raise AlgoError("算法步骤超时（超过 %d 秒）。" % timeout)
        except AlgoError:
            raise
        except Exception as exc:                         # noqa: BLE001
            kill_tree(proc)
            raise AlgoError("跑算法时出错（%s：%s）" % (type(exc).__name__, exc))
        finally:
            if self._current is cur:
                self._current = None
        if cur["cancelled"]:
            raise AlgoError("这次已经停掉了。")
        return (proc.returncode,
                (out or b"").decode("utf-8", "ignore"),
                (err or b"").decode("utf-8", "ignore"))

    def cancel(self):
        cur = self._current
        if not cur:
            return False
        cur["cancelled"] = True
        kill_tree(cur.get("proc"))
        return True

    @property
    def running(self):
        return self._current is not None

    # ---------- 具体调用 ----------
    _node_ver_cache = {}

    def _node_supports_env_proxy(self):
        cached = Algo._node_ver_cache.get(self.node)
        if cached is not None:
            return cached
        ok = False
        try:
            r = subprocess.run([self.node, "-v"], capture_output=True, text=True, timeout=3)
            m = re.match(r"v(\d+)", (r.stdout or "").strip())
            ok = bool(m and int(m.group(1)) >= 24)
        except Exception:                                # noqa: BLE001
            ok = False
        Algo._node_ver_cache[self.node] = ok
        return ok

    def run_node(self, script, argv, timeout=None):
        """跑一个 Node 步骤（script 相对算法目录）。返回 (code, stdout, stderr)。

        script 传 `-e` 表示内联脚本（这时 argv[0] 是脚本正文，见 shrink）。
        """
        argv = [str(a) for a in argv]
        head = [self.node]
        # 系统代理可用时，让 Node 内置 fetch 也走代理（Node 默认不读环境代理）
        if self._proxy_state()[1] and self._node_supports_env_proxy():
            head.append("--use-env-proxy")
        if script == "-e":
            return self._run(head + ["-e"] + argv, timeout or self.timeout_step)
        path = script if os.path.isabs(script) else os.path.join(self.root, script)
        if not os.path.exists(path):
            raise AlgoError("算法步骤不存在：%s" % path)
        return self._run(head + [path] + argv, timeout or self.timeout_step)

    def run_py(self, script, argv, timeout=None):
        """用 uv 跑一个 Python 步骤（自动带上 psd-tools/pillow/numpy）。"""
        path = script if os.path.isabs(script) else os.path.join(self.root, script)
        if not os.path.exists(path):
            raise AlgoError("算法步骤不存在：%s" % path)
        if not self.uv:
            raise AlgoError("没有 uv，跑不了 %s" % os.path.basename(path))
        argv = ([self.uv, "run", "--quiet",
                 "--with", "psd-tools==1.17.4", "--with", "pillow", "--with", "numpy",
                 "python", path] + [str(a) for a in argv])
        return self._run(argv, timeout or self.timeout_step)

    def run_py_module(self, module, argv, timeout=None, cwd=None, env_extra=None,
                      with_pkgs=("numpy", "pillow"), offline=False):
        """用 uv 跑一个 `python -m <module>`（工作目录可另指，环境变量可追加）。

        给 `算法/花型生成`（pattern_studio）这种「自带 Python 包、但不是本算法目录
        的步骤文件」用：它只要 numpy + Pillow，不需要 psd-tools。进程组/取消/
        超时仍走本类的唯一实现，终止时能连 uv 拉起的 Python 一起收掉。

        offline=True 时加 `--offline`：依赖已经在 uv 缓存里就直接起，**绝不访问
        网络**。线上机器常年挂着代理/断外网，uv 每次联网解析依赖会卡到超时
        （表现为退出码 2 / operation timed out），这是刻意的默认行为。
        """
        if not self.uv:
            raise AlgoError("没有 uv，跑不了 %s" % module)
        cmd = [self.uv, "run", "--quiet"]
        if offline:
            cmd.append("--offline")

        for pkg in (with_pkgs or ()):
            cmd += ["--with", str(pkg)]
        cmd += ["python", "-m", str(module)] + [str(a) for a in argv]
        return self._run(cmd, timeout or self.timeout_total,
                         cwd=cwd or self.root, env_extra=env_extra)

    def run_python(self, python, module, argv, timeout=None, cwd=None, env_extra=None):
        """用指定解释器跑 `python -m <module>`（不走 uv、零网络）。

        花型生成会预先在算法目录建 `.venv`（numpy + Pillow 已装好）；
        线上直接用这个解释器起进程，连 uv 依赖解析都省掉。
        """
        python = str(python or "")
        if not python or not os.path.exists(python):
            raise AlgoError("Python 解释器不存在：%s" % (python or "(空)"))
        return self._run([python, "-m", str(module)] + [str(a) for a in argv],
                         timeout or self.timeout_total,
                         cwd=cwd or self.root, env_extra=env_extra)

    def shrink(self, src, out, max_width=1024, timeout=120):
        """用算法自带的 sharp 把图缩到长边 ≤ max_width（四方连续快速路径用）。"""
        code = ("const s=require(process.env.SHARP_PATH);"
                "(async()=>{const m=await s(process.argv[1]).metadata();"
                "await s(process.argv[1]).resize({width:Math.min(%d,m.width)})"
                ".png().toFile(process.argv[2]);})()"
                ".catch(e=>{console.error(e.message);process.exit(1);})" % int(max_width))
        return self.run_node("-e", [code, src, out], timeout=timeout)

    # ---------- 解析 ----------
    @staticmethod
    def error_summary(out, err, limit=300):
        """从步骤的 stdout/stderr 里抽一句**给人看**的错误。

        Node 未捕获异常时 stderr 长这样：
            file:///Users/.../%E8%8A%B1.../steps/01-clean.mjs:36
            const r = await generateImage(...)
                                ^
            TypeError: fetch failed
                at ...
              [cause]: Error: connect ETIMEDOUT ...
            Node.js v24.21.0
        以前后端只取最后 200 字符，用户看到的是一串 URL 编码的路径
        （`/ %E6%9C%8D...:36:11 Node.js`），真正的原因（fetch failed）反而丢了。
        这里优先挑真正的错误行，并把网络类错误翻译成一句能照着做的话。
        """
        raw = (err or "").strip()
        low = raw.lower()
        net_marks = ("fetch failed", "etimedout", "enotfound", "econnrefused",
                     "eai_again", "econnreset", "socket hang up",
                     "operation was aborted due to timeout",
                     "network was disabled", "failed to fetch")
        if any(m in low for m in net_marks):
            return ("连不上生图网关（APIMART）：网络或代理不通。"
                    "这一步需要联网，把代理打开后重试。")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        def _clean(line):
            return re.sub(r"^(Error|TypeError|RangeError|SyntaxError|"
                          r"UnhandledPromiseRejection):\s*", "", line)[:limit]

        for ln in lines:                       # 先认标准错误行（最干净）
            if ln.lower().startswith(("error:", "typeerror:", "rangeerror:",
                                      "syntaxerror:", "unhandledpromiserejection")):
                return _clean(ln)
        for ln in lines:                       # 再认中文业务错误
            l = ln.lower()
            if ("提交失败" in ln or "生成结果没有图片 url" in l
                    or "缺少参数" in ln):
                return _clean(ln)
        # 没有明确的 Error 行：丢掉堆栈/文件位置/Node 版本，取最后一条人话
        cand = [ln for ln in lines
                if not ln.startswith("at ") and not ln.startswith("Node.js ")
                and not ln.startswith("^") and "file:///" not in ln
                and not re.match(r"^[A-Za-z]:?/.*\.mjs:\d+", ln)
                and not ln.startswith("/Users/")]
        pick = cand[-1] if cand else (lines[-1] if lines else "")
        # Node 的 `Error: xxx` 前缀对人没信息量，去掉更顺口
        pick = re.sub(r"^(Error|TypeError|RangeError|SyntaxError):\s*", "", pick)
        return pick[:limit] or "算法没有输出错误信息"

    @staticmethod
    def last_json(text):
        """脚本一般最后一行打一个 JSON；取最后一行能解析的。"""
        lines = [l for l in (text or "").splitlines() if l.strip()]
        for line in reversed(lines):
            line = line.strip()
            if not (line.startswith("{") or line.startswith("[")):
                continue
            try:
                return json.loads(line)
            except Exception:                            # noqa: BLE001
                continue
        return None

    @staticmethod
    def meta(path):
        """步骤产物的 `<out>.meta.json` 侧车文件（成本/尺寸/指标都在里面）。"""
        try:
            with open(path + ".meta.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                # noqa: BLE001
            return {}

    def dry_run(self, blob, ext=".png", timeout=None):
        """零成本体检：features + 经验规则给出的候选顺序（不跑任何模型）。

        用的是算法自己的 `pipeline.mjs --dry-run`，产物只写 run-report.json，
        不会出图、不会归档。
        """
        work = tempfile.mkdtemp(prefix="algo_probe_")
        try:
            src = os.path.join(work, "input" + ext)
            with open(src, "wb") as f:
                f.write(blob)
            outdir = os.path.join(work, "out")
            os.makedirs(outdir, exist_ok=True)
            argv = ["--input", src, "--outdir", outdir, "--dry-run"]
            if not self.planner:
                argv.append("--no-planning")
            code, out, err = self.run_node("pipeline.mjs", argv, timeout=timeout or 180)
            report_path = os.path.join(outdir, "run-report.json")
            if os.path.exists(report_path):
                with open(report_path, encoding="utf-8") as f:
                    return json.load(f)
            tail = (err or out or "").strip()[-300:]
            raise AlgoError("体检没有返回结果（退出码 %s）。%s" % (code, tail))
        finally:
            shutil.rmtree(work, ignore_errors=True)
