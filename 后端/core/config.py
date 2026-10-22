"""配置加载：settings.json（结构与模型）+ secrets.json（密钥）。

优先级：环境变量 > secrets.json > settings.json 默认值。
环境变量：PATTERNFLOW_API_KEY / PATTERNFLOW_BASE_URL / PATTERNFLOW_MODEL
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bump(what):
    """通知另一个进程「这些配置变了」，让它下次请求时重新加载。

    **故意在函数里导入 sync**：config 几乎被所有模块依赖，把 `from . import sync`
    放顶部会绕成 config → sync → config 的循环导入（sync 要 `from .config import ROOT`，
    而那时 ROOT 还没定义）。延迟到调用时才导入就绕开了。
    """
    try:
        from . import sync
        sync.rev_bump(what)
    except Exception:
        pass          # 通知失败不该挡住保存本身，最坏是另一个进程晚一步才重载

# 说话风格预设。和人设文件分开存：人设写"这个角色干什么"，风格写"怎么说话"。
# 分开之后，后台切一个档位就能改三个机器人的语气，不用去动任何一份人设。
DEFAULT_STYLES = {
    "warm": ("热情亲切（推荐）",
             "说话像愿意帮忙的老师傅、搭子：先给结论，再讲要注意的事。\n"
             "口语化、别端着，能用一句说清就不写三句。\n"
             "亲切不等于啰嗦，也不等于松专业底线：质量差照实说，"
             "参数、点数、产出说全，不糊弄、不拍胸脯。\n"
             "禁止用 Markdown 大标题把一句闲聊撑成一份报告。"),
    "pro": ("专业克制",
            "语气平稳、克制，以事实和参数为主。\n"
            "先说结论和依据，再给建议；不主动寒暄，不用感叹号，不堆形容词。\n"
            "不确定的地方明确说不确定，不要用模棱两可的话带过去。"),
    "terse": ("简洁直接",
              "回答尽量短：一句话能说完就不写第二句。\n"
              "只给结果和必要参数，不讲背景、不复述用户的话。\n"
              "需要用户做选择时才展开，否则就给最短的答案。"),
    "sales": ("销售导向",
              "主动、积极，善于把话题往「下一步」带：给出方案后自然地问要不要继续。\n"
              "会主动提示可选的方向和加购项，但不夸大、不承诺做不到的交期和价格。\n"
              "涉及数字一律以知识库资料为准，不许自己估算。"),
}


DEFAULT_SETTINGS = {
    # C 端（用户对话）与 B 端（管理后台）是两个进程两个端口。
    # 后台默认只监听 127.0.0.1 —— 它能改密钥和知识库，不该对外。
    "server": {"host": "127.0.0.1", "port": 8787},
    "admin": {"host": "127.0.0.1", "port": 8788},
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0.6,
        "max_tokens": 900,
        "timeout": 60,
        "tools_enabled": True,
        "max_tool_rounds": 3,
    },
    "vision": {"enabled": True, "max_tokens": 32000, "timeout": 180, "temperature": 0,
               "retries": 1},
    "image": {"enabled": True, "provider": "volcengine",
              "base_url": "https://ark.cn-beijing.volces.com/api/v3",
              "model": "doubao-seedream-5-0-260128",
              "fallback_models": ["doubao-seedream-5-0-flash-260915",
                                  "doubao-seedream-5-0-pro-260628",
                                  "doubao-seedream-4-0-250828",
                                  "doubao-seedream-4-5-251128"],
              "size": "2048x2048",
              "watermark": True, "timeout": 180, "retry_429": 2, "retry_wait": 6,
              "cost": 60,
              "outdir": "../数据/generated", "extra": {}},
    # 描稿仿稿：**默认沿用生图那套**（上面 image 段）。
    # 这里留空 = "跟花型设计师用同一个模型/网关/Key"，属于零改动默认值；
    # 想单独换模型或走另一家网关，就在后台「织印师 → ① 描稿仿稿」里填。
    # 合并逻辑在 core/imagegen.py：只覆盖非空项，空值一律继承 image 段。
    # 它自己的 Key 是 secrets.trace_api_key（留空 = 用 image_api_key）。
    # 描稿：新算法 01-clean。unwarp=先「想象平铺还原」一遍再清洗（R1，扭曲面料必须）；
    # size_final=最终交付分辨率（2048²，印刷够用；1024 更快但细节少）。
    "trace": {"provider": "", "base_url": "", "model": "",
              "unwarp": True, "size_restore": "1:1",
              "size_final": "2048x2048"},
    "agents": {
        # 客服助理：**只读技能**。检索（search_patterns）与单款详情
        # （get_pattern_detail）都接上了真数据；出图/描稿/连续/PSD 一件不给
        # —— 那些是专业活，人设里写明转交花型设计师或织印师。
        # （2026-10-13 之前它只有 get_pattern_detail：客户得先报编号才查得动。）
        # **趋势（list_trends）2026-10-14 摘掉了**：用户说不希望客服对话里出现
        # 趋势推荐 —— 那是花型设计师挑风格时的工具，摆在客服这边只会让客户
        # 以为"客服也能给我定方向"。要趋势就找花型设计师。
        "assistant": {"persona": "assistant.md",
                      "skills": ["get_pattern_detail", "search_patterns"]},
    "designer": {"persona": "designer.md",
                 "skills": ["update_brief", "search_patterns", "list_trends",
                            "get_pattern_detail", "generate_pattern", "edit_pattern"]},
        "engineer": {"persona": "engineer.md",
                     "skills": ["get_pattern_detail", "search_patterns",
                                "trace_pattern", "seamless_pattern",
                                "layer_psd"]},
        "auto": {"persona": "router.md",
                 "skills": ["update_brief", "search_patterns", "list_trends"]},
    },
    "library": {"enabled": True, "dir": "../数据/library", "auto_ingest": True,
                "thumb_px": 480, "thumb_quality": 72},
    # 四方连续：算法在 算法/四方连续/ 下、自带 .venv（numpy + Pillow），
    # 主程序按子进程调用它，自己保持零依赖。详细契约见 core/seamless.py。
    # 默认**一个网关两用**：诊断和出图同一个 base、同一把 key（APIMart
    # 自己也代理 deepseek，一把 key 全包）。
    # 要把诊断拆出去走独立网关：同时配 analyze_key + analyze_base 两项 ——
    # 只配 analyze_base 不配 key 是无效的，见 core/seamless.py 的 __init__。
    # 新算法（描稿 / 四方连续 / 分层 PSD 共用）：算法/描稿-四方连续-psd分层。
    # 运行时在算法目录自带 .runtime/（node v24.21.0 + sharp + uv），
    # node/sharp/uv 留空 = 自动找 .runtime；见 core/algo.py。
    "algo": {"dir": "../算法/描稿-四方连续-psd分层",
             "base_url": "", "api_key_env": "PATTERNFLOW_ALGO_KEY",
             "node": "", "sharp": "", "uv": "",
             "height": 9382, "k": 8, "planner": True,
             "model_clean": "gpt-image-2.5-sunburst",
             "model_seam": "gpt-image-2.5-ext",
             "band": 140, "feather": 20,
             "timeout_step": 600, "timeout_total": 1800},
    # 花型生成（花型设计师的「生成」）：算法/花型生成（pattern_studio）。
    # 关键词 → 九宫格元素 → 环面排版 → 3×3 环绕合成 → 逐像素无缝自检 →（可选）AI 审核，
    # 每版产物本身就是可平铺的无缝 tile。见 core/patgen.py。
    #   profile   fast / balanced / cheapest / best（实测见算法 README）：
    #             fast ≈20~30s（z-image-turbo）｜balanced ≈100~150s（gpt-image-2，画质最好）
    #   variants  一次出几版（前端 1/2/4 张按钮对得上）
    #   presets   排版预设串；识图给了构图时按构图覆盖
    #   seed      0 = 每次随机（否则「重新生成」会出同一套排布）
    #   review    off/auto/strict；auto 会初审+终审+1 轮自动修（+60~90s）
    #             产品默认 fast+off：聊天场景先保证"点完能很快看到图"；
    #             要更高质量/质检就改 balanced+auto（或 best）。
    "patgen": {"enabled": True, "dir": "../算法/花型生成", "uv": "",
               "cost": 60, "variants": 4,
               "profile": "fast",
               "presets": "halfdrop,phyllotaxis,scatter,grid",
               "tile": 1024, "review": "off", "bg_mode": "auto",
               "element_scale": 1.0, "seed": 0, "llm": True, "timeout": 600},
    # 注意（2026-10-20）：四方连续已改走新算法（见上面 algo 段），
    # 这里的 dir/python 只在回滚到老引擎时才有意义；cost 仍然是扣费口径。
    "seamless": {"enabled": True, "dir": "../算法/四方连续", "python": "",
                 "cost": 30, "candidates": 2,
                 # 产品侧：最多试几条候选、要不要跑 tile-multi（4 次采样、很慢）、
                 # 全部候选都不过 A 级时是否按 B 级（接缝轻微可见）交付
                 "max_attempts": 3, "tile_multi": False, "allow_grade_b": True, "review": True,
                 # AI 补缝后跑带后处理（steps/09）；修缝前底图长边归一（算法按 1024 调参）
                 "ai_postfix": True, "base_max_side": 1024,
                 "postfix": {"k": 1.0, "border": 16, "band": 0, "rounds": 5,
                             "ring": 40, "gain": 1.0, "ramp": 20},
                 "base_url": "https://api.apimart.ai/v1",
                 "analyze_base": "https://api.apimart.ai/v1",
                 "image_model": "gpt-image-2.5-flare",
                 "analyze_model": "deepseek-v4.1-flash",
                 "timeout_probe": 180, "timeout_run": 900},
    # 分层 PSD：算法在 算法/psd分层/ 下、自带 .venv（numpy + Pillow + psd-tools），
    # 主程序按子进程调用它，自己保持零依赖。详细契约见 core/psdlayer.py。
    # 跑云端 SAM3.1 工作流要 RunningHub 的 key（secrets.runninghub_api_key）；
    # 看图定分层复用对话那个 DeepSeek key，没配也能跑（退回默认单层）。
    # 两个超时必须满足 timeout_run > task_timeout × (max_retry + 1)，
    # 否则客户端自己的单任务超时永远轮不到生效（真踩过，见 core/psdlayer.py 注释）。
    # 注意（2026-10-20）：分层 PSD 已改走新算法（见 algo 段），
    # dir/python/RunningHub 相关配置只在回滚老引擎时才有意义；cost 仍是扣费口径。
    "psdlayer": {"enabled": True, "dir": "../算法/psd分层", "python": "",
                 # max_layers = 色层数（不含根层 Background）。算法默认 k=8 →
                 # 7 个色层 + 1 个背景；4 层时量化 PSNR 只有 21.5dB，明显丢层次。
                 "cost": 45, "max_layers": 7,
                 "mode": "color",
                 # flood=本地区域生长；sam3=RunningHub SAM3.1 语义蒙版
                 "matte": "flood", "max_side": 2048,
                 "timeout_plan": 120, "task_timeout": 180, "max_retry": 3,
                 "timeout_run": 900,
                 # 结果缓存（core/psdcache.py）：同一张图（同层数）只真跑
                 # 一次，之后直接调出来。命中时按 40~60 秒随机伪装成还在跑
                 # （产品要求，见 skills/layer_psd/handler.py 顶部常数）。
                 # 目录可用环境变量 PATTERNFLOW_PSD_CACHE_DIR 覆盖（测试隔离）。
                 "cache_enabled": True, "cache_dir": "../数据/psd-cache"},
    # 自动打标签（入库闸门）：算法在 算法/自动打标签/ 下、自带 .venv
    # （mlx-vlm + mlx，跑在本机的 Qwen3.5-4B）。主程序按子进程调用它，
    # 自己保持零依赖。详细契约见 core/atag.py。
    # base_url / model 留空 = 用算法自己的默认（本机 127.0.0.1:11435）。
    # 从今往后后台花型库导入的每一张图都要先过它，见 core/atag.py 顶部说明。
    "atag": {"enabled": True, "dir": "../算法/自动打标签", "python": "",
             "base_url": "", "model": "", "timeout": 180, "max_side": 1280,
             "vocab": ""},
    "kb": {"enabled": True, "agents": ["assistant"], "top_k": 4, "score_min": 0.18},
    "fallback": {"enabled": True},
    # 路径都相对「后端/」解析；数据（出图/图库/会话/知识库）刻意放在项目根的
    # 「数据/」里，让代码和数据分开 —— 备份、迁移时各管各的。
    "paths": {"personas": "personas", "skills": "skills", "data": "data",
              "sessions": "../数据/sessions", "kb": "../数据/kb",
              # 埋点（出图成败/耗时、提案转化、知识库命中、前端点击与来路）。
              # 跟会话记录分开放：那是上下文，这是运营数据，寿命和读法都不同。
              "telemetry": "../数据/telemetry"},
}


def _load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self):
        self.settings = _merge(DEFAULT_SETTINGS,
                               _load_json(os.path.join(ROOT, 'config', 'settings.json')))
        self.secrets = _load_json(os.path.join(ROOT, 'config', 'secrets.json'))
        self.styles = _load_json(os.path.join(ROOT, 'config', 'styles.json'))
        self.api_key = (os.environ.get('PATTERNFLOW_API_KEY')
                        or self.secrets.get('api_key') or '').strip()
        self.base_url = (os.environ.get('PATTERNFLOW_BASE_URL')
                         or self.settings['llm'].get('base_url')).rstrip('/')
        self.model = (os.environ.get('PATTERNFLOW_MODEL')
                      or self.settings['llm'].get('model'))

    def path(self, key):
        rel = self.settings['paths'][key]
        # kb 目录可能还没建（第一次进后台才用），缺目录不能让服务起不来
        if key == 'kb':
            p = os.path.normpath(os.path.join(ROOT, rel))
            os.makedirs(p, exist_ok=True)
            return p
        return os.path.normpath(os.path.join(ROOT, rel))

    def agent_conf(self, agent):
        return self.settings['agents'].get(agent) or self.settings['agents']['designer']

    # ---------- 说话风格 ----------
    def style_of(self, agent):
        """返回 {preset, body}。没配过就用默认的「热情亲切」。"""
        got = self.styles.get(agent) or {}
        body = (got.get('body') or '').strip()
        if not body:
            body = DEFAULT_STYLES['warm'][1]
        return {"preset": got.get('preset') or 'warm', "body": body}

    def save_styles(self, styles):
        p = os.path.join(ROOT, 'config', 'styles.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(styles, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        self.styles = styles
        _bump('config')

    def save_settings(self, settings):
        p = os.path.join(ROOT, 'config', 'settings.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        self.settings = settings
        _bump('config')

    def save_secrets(self, secrets, force=False):
        """写密钥文件。**默认不许把已有的密钥写成空**。

        这是最后一道兜底：上层（admin.save_config）已经保证"留空=不改"，
        但那层逻辑一旦被改错，密钥就会被静默清空、服务当场降级到规则模式，
        而且查不出原因（真出过一次）。所以这里再挡一次：
        新值为空、旧值非空时保留旧值，除非显式 force。
        """
        if not force:
            old = _load_json(os.path.join(ROOT, 'config', 'secrets.json'))
            # 这份名单要和 admin.KEY_FIELDS 对齐（后台能改哪些密钥）。
            # 故意各写一份：这是两层独立的保障，不是重复代码 ——
            # 上层改错了，这一层还挡得住。
            for k in ('api_key', 'image_api_key', 'seamless_api_key',
                      'seamless_analyze_key', 'runninghub_api_key',
                      'psdlayer_vlm_key', 'trace_api_key'):
                if k in old and old[k] and not (secrets or {}).get(k):
                    print('[config] %s 新值为空，保留原值（要清空请显式传 force=True）' % k)
                    secrets = dict(secrets or {})
                    secrets[k] = old[k]
        p = os.path.join(ROOT, 'config', 'secrets.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(secrets, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        self.secrets = secrets
        _bump('config')

    @property
    def llm_enabled(self):
        return bool(self.api_key)

    def describe(self):
        return {
            "base_url": self.base_url,
            "model": self.model,
            "llm_enabled": self.llm_enabled,
            "tools_enabled": bool(self.settings['llm'].get('tools_enabled')),
            "fallback_enabled": bool(self.settings['fallback'].get('enabled')),
            "agents": sorted(self.settings['agents'].keys()),
            "image": dict(self.settings.get('image') or {}),
        }
