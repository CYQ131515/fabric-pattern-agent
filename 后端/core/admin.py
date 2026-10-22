"""管理后台的服务端逻辑（B 端）。

和 C 端 API 的分工：
- C 端那套 `/api/chat`、`/api/library` 是**给用户的**，只做对话和取图；
- 这一套 `/api/admin/*` 是**给运营自己的**，能改配置、改人设、改知识库、改图库。

**这个模块能改密钥和人设文件，所以每条写入都必须**：
1. 原子写（`.tmp` + `os.replace`），写一半崩了不能留半个文件；
2. 改完立刻 `APP.reload()`，否则页面上看着改了、实际还是旧的人设；
3. 密钥回显一律脱敏，`未改动` 用占位符表示，不许把真 key 吐回前端再收回来。

后台是**独立进程、独立端口**（默认 127.0.0.1:8788），进页面要口令（core/auth.py）。
改完磁盘上的东西会 bump 变更号，C 端进程下次处理请求时自动重载，
所以运营在后台改了人设/知识库/图库，用户那边不用重启就能生效。
"""
import json
import os
import threading
import time

from . import sync
from .config import DEFAULT_STYLES, ROOT
from .kb import KnowledgeBase

MASK = '••••••'
KEEP = '（未改动）'          # 前端回传这个值 = 不要动这个密钥

# 允许在后台改写的密钥字段。**加新密钥时先改这里** ——
# 这个名单要在两个地方用到（真正写入、以及 clear_keys 显式清空），
# 早先两处各写一遍，漏掉一处就会出现"页面上填了、保存了、其实没生效"。
# config.save_secrets 里另有一份兜底名单（那份是防"被写成空"的第二道闸），
# 两边要同步加。
KEY_FIELDS = ('api_key', 'image_api_key', 'seamless_api_key',
              'seamless_analyze_key', 'runninghub_api_key', 'psdlayer_vlm_key',
              'trace_api_key')

# 不给扫的系统目录：这些地方文件以百万计，手一滑点下去能跑一整天。
# 注意**不要**把 /var、/private 加进来 —— 在 macOS 上用户的临时目录、
# 下载中转目录都在 /var/folders 下面，加了等于把正常用法一起封死（踩过）。
BLOCK_DIRS = ('/', '/System', '/Library', '/usr', '/bin', '/sbin', '/etc',
              '/opt', '/Applications', '/Network', '/cores', '/dev')


def mask_key(k):
    """只留最后 4 位。留太多等于把密钥贴在截图里。"""
    k = (k or '').strip()
    if not k:
        return ''
    return MASK + k[-4:] if len(k) > 8 else MASK * 6


def _read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ---------- 概览 ----------
def state(app):
    lib = app.library.stats()
    kb = app.kb.stats() if app.kb else {"enabled": False}
    sess_dir = app.cfg.path('sessions')
    n_sess = 0
    try:
        n_sess = len([f for f in os.listdir(sess_dir) if f.endswith('.jsonl')])
    except OSError:
        pass
    return {
        "ok": True,
        "model": {"base_url": app.cfg.base_url, "model": app.cfg.model,
                  "api_key": mask_key(app.cfg.api_key),
                  "image_api_key": mask_key(app.cfg.secrets.get('image_api_key')),
                  # 四方连续走的是第三方编辑模型（要能按 mask 局部重绘、且保形），
                  # 和生图那把 key 不是一回事，所以单独一个字段。
                  # 它分两条线：出图（要画得好）和诊断（只要能看图、便宜），
                  # 两家可以是不同的网关，所以是两把 key。
                  "seamless_api_key": mask_key(app.cfg.secrets.get('seamless_api_key')),
                  "seamless_analyze_key": mask_key(
                      app.cfg.secrets.get('seamless_analyze_key')),
                  # 分层 PSD 的两把：RunningHub 跑 SAM3.1 工作流（必填），
                  # 视觉那把可选（留空 = 复用上面那把对话 Key）。
                  "runninghub_api_key": mask_key(
                      app.cfg.secrets.get('runninghub_api_key')),
                  "psdlayer_vlm_key": mask_key(
                      app.cfg.secrets.get('psdlayer_vlm_key')),
                  # 描稿仿稿的独立 Key。留空 = 用上面那把生图 Key。
                  "trace_api_key": mask_key(
                      app.cfg.secrets.get('trace_api_key')),
                  # 识图模型：留空 = 跟对话模型同一个
                  "vision_model": (app.cfg.settings.get('vision') or {}).get('model') or '',
                  "llm_enabled": app.cfg.llm_enabled,
                  "temperature": app.cfg.settings['llm'].get('temperature'),
                  "max_tokens": app.cfg.settings['llm'].get('max_tokens')},
        "seamless": _seamless_info(app),
        "psdlayer": _psdlayer_info(app),
        "trace": _trace_info(app),
        # 花型生成的新发动机（关键词 → 可平铺无缝 tile），见 core/patgen.py。
        # 只回装没装好、档位、版数、点数；不回网关地址和密钥。
        "patgen": _patgen_info(app),
        # 所有对外接口的「配没配」清单。概览页的自检直接照着渲染 ——
        # 只回 ok 和一句说明，**不回地址也不回密钥**（那一页常被截图）。
        "apis": _api_checks(app),
        "image": {"provider": app.cfg.settings['image'].get('provider'),
                  "base_url": app.cfg.settings['image'].get('base_url'),
                  "model": app.cfg.settings['image'].get('model'),
                  "cost": app.cfg.settings['image'].get('cost'),
                  "variants": getattr(app.imagegen, 'variants', 1),
                  "enabled": app.imagegen.enabled},
        "library": lib,
        # 入库自动打标的实时状态：花型库页照着显示"引擎起没起"。
        # 只回 ready + 一句人话，不回端点地址（页面会被截图）。
        "atag": _atag_info(app),
        "kb": kb,
        "agents": sorted(app.cfg.settings['agents'].keys()),
        "personas": sorted(app.personas.keys()),
        "skills": [s['name'] for s in app.registry.describe()],
        "sessions": n_sess,
        "styles": {a: app.cfg.style_of(a) for a in app.cfg.settings['agents']},
        "presets": [{"key": k, "label": v[0]} for k, v in DEFAULT_STYLES.items()],
        "now": time.strftime('%Y-%m-%d %H:%M:%S'),
        # 后台和 C 端是两个进程两个端口，「打开对话页」得指到 C 端去，
        # 不能再用当前页面的 origin（那会把自己再开一遍）
        "client_url": os.environ.get('PATTERNFLOW_CLIENT_URL')
                      or ('http://127.0.0.1:%d/' % int(
                          (app.cfg.settings.get('server') or {}).get('port') or 8787)),
    }


# ---------- 配置 ----------
def _seamless_info(app):
    """四方连续引擎的自检信息。

    后台进程没有 runtime，所以在这里单独建一个引擎对象 —— 它只读配置、
    不 import 算法（算法是起子进程跑的），建起来零成本。
    后台最需要看到的就是 available 和 python 这两项：
    装没装好、指到哪个解释器，一眼就能判断问题出在哪。
    """
    try:
        from .seamless import SeamlessEngine
        return SeamlessEngine(app.cfg).describe()
    except Exception as e:                           # noqa: BLE001
        return {"enabled": False, "available": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def _psdlayer_info(app):
    """分层 PSD 引擎的自检信息。

    和 _seamless_info 一个道理：后台进程没有 runtime，这里单独建一个引擎对象。
    它只读配置、不起子进程（算法是真正调用那一刻才起的），建起来零成本。
    """
    try:
        from .psdlayer import PsdLayerEngine
        info = PsdLayerEngine(app.cfg).describe()
        # 视觉诊断那把 key：是单独配的，还是在复用通用那把？
        # 后台要能分辨 —— 页面上一句「沿用通用 Key」和「独立一把」含义完全不同。
        info["has_own_vlm_key"] = bool(
            (app.cfg.secrets.get('psdlayer_vlm_key') or '').strip())
        # 界面要区分两件事：**配置里写的**（可能留空）和**兜底之后实际用的**。
        # describe() 给的 vlm_base 已经是三级兜底之后的值，直接拿去回填输入框，
        # 用户会以为"后台本来就填了这个网关"，其实他一个字都没配过。
        pd = app.cfg.settings.get('psdlayer') or {}
        info["cfg"] = {"cost": pd.get('cost'), "max_layers": pd.get('max_layers'),
                       "vlm_base": pd.get('vlm_base') or '',
                       "vlm_model": pd.get('vlm_model') or ''}
        return info
    except Exception as e:                           # noqa: BLE001
        return {"enabled": False, "available": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def _trace_info(app):
    """描稿仿稿的配置自检。

    它**默认沿用生图那套**，所以页面要同时能看出两件事：
    ① 「我在后台填了什么」（`cfg`，可能整段是空的）；
    ② 「实际会用哪套」（`eff`，兜底/继承之后的结果）。
    只给一个的话，运营分不清"这是我配的"还是"系统默认继承来的"。
    """
    tr = app.cfg.settings.get('trace') or {}
    own_key = bool((app.cfg.secrets.get('trace_api_key') or '').strip())
    eff = {}
    try:
        from .imagegen import ImageGen
        eff = ImageGen(app.cfg, section='trace').describe()
    except Exception as e:                           # noqa: BLE001
        eff = {"error": "%s: %s" % (type(e).__name__, e)}
    return {
        "cfg": {"provider": tr.get('provider') or '',
                "base_url": tr.get('base_url') or '',
                "model": tr.get('model') or '',
                "cost": tr.get('cost')},
        "eff": {"provider": eff.get('provider'),
                "base_url": eff.get('base_url'),
                "model": eff.get('model'),
                "cost": eff.get('cost'),
                "has_key": eff.get('has_key')},
        "has_own_key": own_key,
        # 哪几项还在"继承生图"：页面上给个小标，一眼看出没单独配。
        "inherits": {"provider": not (tr.get('provider') or '').strip(),
                     "base_url": not (tr.get('base_url') or '').strip(),
                     "model": not (tr.get('model') or '').strip(),
                     "cost": tr.get('cost') in (None, '')},
    }


def _patgen_info(app):
    """花型生成（pattern_studio）的自检信息。

    后台进程没有 runtime，这里单独建一个引擎对象 —— 它只读配置、不起子进程，
    建起来零成本。页面要看的是「装没装好、一次几版、每版多少点」。
    """
    try:
        from .patgen import PatternEngine
        return PatternEngine(app.cfg).describe()
    except Exception as e:                           # noqa: BLE001
        return {"enabled": False, "available": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def _api_checks(app):
    """所有对外接口的「配没配」清单 —— 概览页的自检直接照着渲染。

    **只回 ok 和一句说明，不回网关地址、不回密钥**（连掩码都不回）。
    后台页经常被截图丢群里，地址和 Key 尾巴都不该出现在上面；
    要看具体值去「模型与密钥」页（那一页只在本地开着看）。
    也正因为如此，这里的 label 只能写「做什么用」，不能写「连的哪里」。
    """
    rows = []

    def add(group, name, ok, note, info=''):
        rows.append({"group": group, "name": name, "ok": bool(ok),
                     "note": note, "info": info})

    # ---------- 通用：三个机器人共用 ----------
    llm_ok = bool(app.cfg.llm_enabled)
    add('通用', '对话大模型', llm_ok,
        '已配置 · 问答、判断意图都走它' if llm_ok
        else '没配 Key，只能退回本地规则兜底',
        '模型 ' + (app.cfg.model or '未填'))

    vis = app.cfg.settings.get('vision') or {}
    vis_name = (vis.get('model') or '').strip() or (app.cfg.model or '未填')
    vis_ok = llm_ok and bool(vis.get('enabled', True))
    add('通用', '识图模型', vis_ok,
        '已配置 · 描稿体检、入库打标签、按图改图都用它' if vis_ok
        else '没配，看不了图',
        '模型 ' + vis_name)

    # ---------- 花型设计师 ----------
    # 生成已经换成新算法（九宫格元素 + 自动排版 + 无缝校验），改图仍走生图网关。
    # 两盏灯分开报：否则「生成好了但改不了」时运营不知道该查哪个配置。
    try:
        from .patgen import PatternEngine
        pg = PatternEngine(app.cfg)
        pgd = pg.describe()
    except Exception as e:                           # noqa: BLE001
        pgd = {"error": "%s: %s" % (type(e).__name__, e)}
    gen_ok = bool(pgd.get('available'))
    if gen_ok:
        gen_note = '已配置 · 新版花型生成（九宫格元素 + 自动排版 + 无缝校验）'
    elif pgd.get('forced_mock'):
        gen_note = '当前是 mock 模式，不调用真实生图'
    else:
        gen_note = '没配好，出不了图（%s）' % (
            '算法或密钥未就绪' if pgd.get('installed') else '算法没装好')
    add('花型设计师', '生图接口', gen_ok, gen_note,
        '一次 %d 版 · 每版 %s 点' % (pgd.get('variants') or 1, pgd.get('cost') or 0))
    # 定向改图（图生图）仍用老生图网关，密钥/模型在「模型与密钥」里配。
    img = app.imagegen
    img_ok = bool(img.enabled and img.api_key)
    add('花型设计师', '改图接口', img_ok,
        '已配置 · 在现有花型上定向修改' if img_ok else '没配，改不了图',
        '单张 %s 点' % getattr(img, 'cost', 0))

    # ---------- 织印师 ----------
    tr = app.tracegen
    tr_ok = bool(getattr(tr, 'enabled', False) and getattr(tr, 'api_key', ''))
    add('织印师', '描稿仿稿', tr_ok,
        '已配置 · 新算法 AI 清洗（保花型）' if tr_ok else '没配，描不了稿',
        '单张 %s 点 · 一次 1 张' % getattr(tr, 'cost', 0))

    try:
        from .seamless import SeamlessEngine
        se = SeamlessEngine(app.cfg).describe()
    except Exception as e:                           # noqa: BLE001
        se = {"error": "%s: %s" % (type(e).__name__, e)}
    se_ok = bool(se.get('available') and se.get('has_key'))
    if se_ok:
        se_note = '已配置 · 自动选路线 + 整图审查回退'
    elif se.get('available'):
        se_note = '缺生图 key：只能跑纯算法快速路径，多数图过不了审查'
    else:
        se_note = '没配，做不了四方连续'
    add('织印师', '四方连续 · 接缝', se_ok, se_note,
        '单张 %s 点' % se.get('cost'))
    add('织印师', '四方连续 · 审查', se_ok,
        '已配置 · A 级严格；全灭时按 B 级如实告知' if se_ok else se_note,
        '不过审自动换下一条路线')

    try:
        from .psdlayer import PsdLayerEngine
        ps = PsdLayerEngine(app.cfg).describe()
    except Exception as e:                           # noqa: BLE001
        ps = {"error": "%s: %s" % (type(e).__name__, e)}
    ps_ok = bool(ps.get('available'))
    add('织印师', '分层 PSD · 拆层', ps_ok,
        '已配置 · k-means 色层（本地）' if ps_ok else '没配，拆不了层',
        '最多 %s 层 · 单张 %s 点' % (ps.get('max_layers'), ps.get('cost')))
    add('织印师', '分层 PSD · 结构', ps_ok,
        '已配置 · Background + Colors 分组' if ps_ok else '没配，拆不了层',
        '300dpi / %spx 高' % ps.get('height'))

    # ---------- 入库自动打标（花型库导入闸门）----------
    # 只报"装没装"这种配置层面的事，不起子进程探活 —— 探活归花型库页的
    # 状态条（state.atag），那一页才是用这个功能的地方。
    try:
        from .atag import AtagEngine
        at = AtagEngine(app.cfg)
        at_desc = at.describe()
    except Exception as e:                               # noqa: BLE001
        at_desc = {"error": "%s: %s" % (type(e).__name__, e)}
    at_ok = bool(at_desc.get('available'))
    add('通用', '入库自动打标', at_ok,
        '已安装 · 后台导入的每张图都会先过它打标签' if at_ok
        else '没装好，后台导入会被拦下（去 算法/自动打标签/本地模型 装）',
        '本机推理 · 不按张计费' if at_ok else '装好才能导入')

    return rows


# ---------- 数据看板 ----------
DAY = 86400
DIM_LABEL = {'style': '风格', 'element': '元素', 'color': '配色', 'scene': '场景',
             'composition': '构图', 'density': '疏密', 'scale': '尺度'}
SKILL_LABEL = {'generate_pattern': '生成花型', 'edit_pattern': '定向改图',
               'trace_pattern': '描稿仿稿', 'seamless_pattern': '四方连续',
               'layer_psd': '分层 PSD', 'search_patterns': '检索花型库',
               'get_pattern_detail': '看花型详情', 'list_trends': '看趋势',
               'update_brief': '记需求卡'}

# 出图失败的归类。看板上必须能一眼看出"是密钥配错了、被限流了、还是网络问题"——
# 全都糊成一句"失败"的话，运营根本不知道该去动哪一块。
FAIL_LABEL = {'timeout': '超时（模型/云端没在时限内回来）',
              'rate_limit': '被限流（429）',
              'auth': '鉴权失败（Key 或权限不对）',
              'network': '网络不通（连不上网关）',
              # 标签会进 .brow 那一列（固定 82px、超出省略号），
              # 所以别写太长 —— 前面几个字才是真看得见的部分。
              'input': '输入不合规（缺参数或图太大）',
              'not_ready': '没配置或能力未就绪',
              'empty': '模型返回了空结果',
              'bad_result': '技能返回异常',
              'unknown': '其它',
              'other': '其它'}

# 会话结束方式的说明（和后端 runtime.END_LABEL 一套口径）
END_LABEL = {'done': '聊完走的', 'asking': '在等用户回话',
             'pending': '压着没确认的卡片', 'bounce': '进来就走',
             'unknown': '未知'}


def _pctile(vals, q):
    """给一堆耗时取分位数。样本为空给 0，不让页面出现 NaN。"""
    if not vals:
        return 0
    s = sorted(vals)
    i = int(round((len(s) - 1) * q))
    return s[max(0, min(len(s) - 1, i))]


def _scan_telemetry(cfg, days):
    """读埋点事件。目录不存在/文件坏了都只是「这段时间没有数据」，不是错误。

    目录解析走 `Telemetry.from_config` —— 跟写入端用同一套规则，
    所以 `PATTERNFLOW_TELEMETRY_DIR` 一设，写和读一起挪走（e2e 靠它隔离）。
    """
    try:
        from . import telemetry as telmod
        return telmod.load(telmod.Telemetry.from_config(cfg).dir, days)
    except Exception as e:                           # noqa: BLE001
        print('[admin] 读埋点失败：%s: %s' % (type(e).__name__, e))
        return []



def _daykey(ts):
    try:
        return time.strftime('%Y-%m-%d', time.localtime(float(ts or 0)))
    except (ValueError, OSError, OverflowError):
        return ''


def _scan_sessions(sess_dir):
    """把会话 jsonl 里的「轮次」和「需求卡」扫出来。

    只读、容错：单个文件坏了跳过，不让看板整体挂掉。
    后台是独立进程，这就是它唯一的"用户行为"数据源 —— 别的（注册/充值）
    后端根本没有，所以看板上那两块只能如实说明暂时没有。
    """
    turns, briefs = [], []
    try:
        names = [f for f in os.listdir(sess_dir) if f.endswith('.jsonl')]
    except OSError:
        return turns, briefs
    for f in names:
        try:
            with open(os.path.join(sess_dir, f), encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:                # noqa: BLE001
                        continue
                    kind = rec.get('type')
                    if kind == 'turn':
                        t = rec.get('turn') or {}
                        turns.append({"t": t.get('t') or 0,
                                      "agent": t.get('agent') or 'designer',
                                      "skills": t.get('skills') or [],
                                      "user": t.get('user') or '',
                                      "sid": f[:-6]})
                    elif kind == 'brief':
                        briefs.append(rec.get('brief') or {})
        except OSError:
            continue
    return turns, briefs


def _skill_costs(app):
    """每个技能「一次要多少点」——以配置里的点数为准，没配就退回 skill.json。

    生图那三个（生成/改图/描稿）走 background `image` / `trace` 段，
    运营在后台改过点数就得跟着变，不能读 skill.json 里写死的 60。
    """
    st = app.cfg.settings
    img_cost = int((st.get('image') or {}).get('cost') or 0)
    # 生成走新算法时，点数以 patgen.cost 为准（改图仍读 image.cost）
    pg_cost = int((st.get('patgen') or {}).get('cost') or 0) or img_cost
    tr = (st.get('trace') or {}).get('cost')
    tr_cost = int(tr) if tr not in (None, '') else img_cost
    se_cost = int((st.get('seamless') or {}).get('cost') or 0)
    psd_cost = int((st.get('psdlayer') or {}).get('cost') or 0)
    out = {}
    for s in app.registry.describe():
        out[s['name']] = int(s.get('cost') or 0)
    for name, c in (('generate_pattern', pg_cost), ('edit_pattern', img_cost),
                    ('trace_pattern', tr_cost), ('seamless_pattern', se_cost),
                    ('layer_psd', psd_cost)):
        if c:
            out[name] = c
    return out


def analytics(app, days=14):
    """后台「数据看板」的全部数据。

    只统计**磁盘上真有的东西**：会话记录、花型库索引、出图目录。
    注册人数、充值流水这类，后端根本没有账号和计费模块（前端那段是演示用的），
    所以 `accounts` 一律如实回 available=False，让页面明说"暂时没有"，
    而不是编一个 0 出来骗人。
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 14
    # 夹在 1~90 天：给 0 或负数就取 1，给超大值取 90（别让前端一句话把整盘数据拖出来）
    days = max(1, min(90, days))
    sess_dir = app.cfg.path('sessions')
    turns, briefs = _scan_sessions(sess_dir)
    lib = app.library
    dims = ('style', 'element', 'color', 'scene')

    # ---- 花型库画像 ----
    dim_top = {}
    for dim in dims + ('composition', 'density', 'scale'):
        c = {}
        for p in lib.items:
            v = p.get(dim)
            if v:
                c[v] = c.get(v, 0) + 1
        dim_top[dim] = [[k, v] for k, v in
                        sorted(c.items(), key=lambda x: (-x[1], x[0]))[:8]]
    colours = {}
    for p in lib.items:
        for c in (p.get('colors') or []):
            if c:
                colours[c] = colours.get(c, 0) + 1
    color_top = [[k, v] for k, v in sorted(colours.items(),
                                           key=lambda x: (-x[1], x[0]))[:10]]

    # ---- 出图目录（按文件修改时间归天）----
    outdir = getattr(app.imagegen, 'outdir', None)
    gen_by_day = {}
    gen_total = 0
    if outdir and os.path.isdir(outdir):
        try:
            for f in os.listdir(outdir):
                if f.startswith('.'):
                    continue
                d = _daykey(os.path.getmtime(os.path.join(outdir, f)))
                gen_by_day[d] = gen_by_day.get(d, 0) + 1
                gen_total += 1
        except OSError:
            pass

    # ---- 按天时间线（近 N 天）----
    now = time.time()
    turn_by_day, ing_by_day, se_by_day, psd_by_day = {}, {}, {}, {}
    for t in turns:
        k = _daykey(t['t'])
        turn_by_day[k] = turn_by_day.get(k, 0) + 1
        if 'seamless_pattern' in t['skills']:
            se_by_day[k] = se_by_day.get(k, 0) + 1
        if 'layer_psd' in t['skills']:
            psd_by_day[k] = psd_by_day.get(k, 0) + 1
    for p in lib.items:
        k = _daykey(p.get('added'))
        if k:
            ing_by_day[k] = ing_by_day.get(k, 0) + 1
    series = []
    for i in range(days - 1, -1, -1):
        k = _daykey(now - i * DAY)
        series.append({"d": k, "turns": turn_by_day.get(k, 0),
                       "gen": gen_by_day.get(k, 0),
                       "ingest": ing_by_day.get(k, 0),
                       "seamless": se_by_day.get(k, 0),
                       "psd": psd_by_day.get(k, 0)})
    # ---- 角色分布 ----
    ag = {}
    for t in turns:
        ag[t['agent']] = ag.get(t['agent'], 0) + 1
    agents = [[k, v] for k, v in sorted(ag.items(), key=lambda x: -x[1])]

    # ---- 技能调用 + 点数估算 ----
    costs = _skill_costs(app)
    sk = {}
    for t in turns:
        for s in t['skills']:
            sk[s] = sk.get(s, 0) + 1
    skills = [{"name": k, "label": SKILL_LABEL.get(k, k), "count": v,
               "cost": costs.get(k, 0), "points": costs.get(k, 0) * v}
              for k, v in sorted(sk.items(), key=lambda x: (-x[1], x[0]))]
    points_total = sum(s['points'] for s in skills)

    # ---- 用户选了什么（需求卡四维）----
    demand = {}
    for dim in dims:
        c = {}
        for b in briefs:
            v = b.get(dim)
            if v:
                c[v] = c.get(v, 0) + 1
        demand[dim] = [[k, v] for k, v in
                       sorted(c.items(), key=lambda x: (-x[1], x[0]))[:8]]

    # ---- 用户找了什么（检索时提到的标签词）----
    vocab = set()
    for p in lib.items:
        for dim in dims:
            if p.get(dim):
                vocab.add(p[dim])
    hits = {}
    for t in turns:
        if 'search_patterns' not in t['skills']:
            continue
        txt = t.get('user') or ''
        for w in vocab:
            if w in txt:
                hits[w] = hits.get(w, 0) + 1
    queries = [[k, v] for k, v in sorted(hits.items(),
                                         key=lambda x: (-x[1], x[0]))[:10]]

    # ---- 两个织印师能力的用量 ----
    def _usage(skill):
        c = sk.get(skill, 0)
        return {"count": c, "points": costs.get(skill, 0) * c}
    engine_usage = {"seamless": _usage('seamless_pattern'),
                    "psd": _usage('layer_psd'),
                    "trace": _usage('trace_pattern')}

    span_from = min([t['t'] for t in turns if t['t']] or [0])
    span_to = max([t['t'] for t in turns if t['t']] or [0])

    # ---- 环比：最近一半区间 vs 前一半（"这周比上周是热了还是凉了"）----
    half = max(1, days // 2)
    compare = {}
    for key in ('turns', 'gen'):
        cur = sum(r[key] for r in series[-half:])
        prev = sum(r[key] for r in series[-2 * half:-half])
        compare[key] = {"cur": cur, "prev": prev,
                        "pct": (round((cur - prev) * 100.0 / prev) if prev else None)}

    lib_stat = lib.stats()
    n_turns = len(turns)
    n_sess = len({t['sid'] for t in turns})

    # ================= 埋点：六块以前"没落盘"的指标 =================
    # 全部来自 数据/telemetry/*.jsonl，由运行时和前端上报。
    # 老数据（埋点上线之前）不会有这些事件 —— 那时显示 0 并写明"从 X 起才有数据"，
    # 不去拿会话记录硬凑一个近似值出来糊弄人。
    from . import telemetry as telmod
    events = _scan_telemetry(app.cfg, days)
    ev = telmod.by_kind(events)

    # ---- ① 出图成功率 + ④ 生图耗时与真实点数 ----
    gens = ev.get('gen') or []
    gen_ok = [g for g in gens if g.get('ok')]
    gen_bad = [g for g in gens if not g.get('ok')]
    durs = [int(g.get('ms') or 0) for g in gens if g.get('ms')]
    reason_cnt = {}
    for g in gen_bad:
        r = g.get('reason') or 'other'
        reason_cnt[r] = reason_cnt.get(r, 0) + 1
    per_skill = {}
    for g in gens:
        s = per_skill.setdefault(g.get('skill') or '?', {"n": 0, "ok": 0, "ms": []})
        s["n"] += 1
        if g.get('ok'):
            s["ok"] += 1
        if g.get('ms'):
            s["ms"].append(int(g['ms']))
    gen_by_skill = [{"name": k, "label": SKILL_LABEL.get(k, k),
                     "attempts": v['n'], "ok": v['ok'], "fail": v['n'] - v['ok'],
                     "rate": round(v['ok'] * 100.0 / v['n'], 1) if v['n'] else 0,
                     "avg_ms": (sum(v['ms']) // len(v['ms'])) if v['ms'] else 0}
                    for k, v in sorted(per_skill.items(), key=lambda x: -x[1]['n'])]
    # 真正扣掉的点数：以出图事件里技能回执的 cost 为准（那是这一次实际报价）
    gen_points = sum(int(g.get('points') or 0) for g in gens)
    gen = {"attempts": len(gens), "ok": len(gen_ok), "fail": len(gen_bad),
           "rate": (round(len(gen_ok) * 100.0 / len(gens), 1) if gens else None),
           "avg_ms": (sum(durs) // len(durs)) if durs else 0,
           "p50_ms": _pctile(durs, 0.5), "p90_ms": _pctile(durs, 0.9),
           "points": gen_points, "mock": sum(1 for g in gens if g.get('mock')),
           "by_skill": gen_by_skill,
           "by_reason": [[k, FAIL_LABEL.get(k, k), v] for k, v in
                         sorted(reason_cnt.items(), key=lambda x: (-x[1], x[0]))]}

    # ---- ② 提案 → 确认转化率 ----
    # proposals = 挂出来的确认卡片数；confirmed = 其中被点掉并真跑起来的次数。
    # 「按 pid 对上」才算转化：界面上直通车按钮（没先看卡片）产生的执行
    # 单列成 direct，不然转化率会莫名其妙超过 100%。
    props = ev.get('proposal') or []
    prop_pids = {}
    for p in props:
        prop_pids[p.get('pid')] = p
    fired_pids = {g.get('pid') for g in gens if g.get('pid')}
    conv_ok = [p for pid, p in prop_pids.items() if pid in fired_pids]
    unconfirmed = [p for pid, p in prop_pids.items() if pid not in fired_pids]
    f_skill = {}
    for p in props:
        e = f_skill.setdefault(p.get('skill') or '?',
                               {"proposals": 0, "confirmed": 0})
        e['proposals'] += 1
        if p.get('pid') in fired_pids:
            e['confirmed'] += 1
    funnel_skill = [{"name": k, "label": SKILL_LABEL.get(k, k),
                     "proposals": v['proposals'], "confirmed": v['confirmed'],
                     "conv": round(v['confirmed'] * 100.0 / v['proposals'], 1)
                     if v['proposals'] else 0}
                    for k, v in sorted(f_skill.items(), key=lambda x: -x[1]['proposals'])]
    # 没被确认的那些卡片是什么价、卡在哪 —— 运营最该看的一栏
    stuck_cost = sum(int(p.get('cost') or 0) for p in unconfirmed)
    funnel = {"proposals": len(props), "confirmed": len(conv_ok),
              "unconfirmed": len(unconfirmed), "stuck_points": stuck_cost,
              "conv": (round(len(conv_ok) * 100.0 / len(props), 1) if props else None),
              "direct": len([g for g in gens if not g.get('pid')]),
              "by_skill": funnel_skill}

    # ---- ③ 知识库命中率 ----
    kbs = ev.get('kb') or []
    kb_hit = [e for e in kbs if e.get('hit')]
    miss_q = {}
    for e in kbs:
        if not e.get('hit') and e.get('q'):
            miss_q[e['q']] = miss_q.get(e['q'], 0) + 1
    kbstat = {"lookups": len(kbs), "hits": len(kb_hit), "miss": len(kbs) - len(kb_hit),
              "rate": (round(len(kb_hit) * 100.0 / len(kbs), 1) if kbs else None),
              "docs": len(app.kb.docs) if app.kb else 0,
              "qa": len(app.kb.qa) if app.kb else 0,
              "top_miss": [[k, v] for k, v in
                           sorted(miss_q.items(), key=lambda x: (-x[1], x[0]))[:10]]}

    # ---- ⑤ 前端埋点：来路、停留、点了哪些按钮 ----
    visits = ev.get('visit') or []
    clicks = ev.get('click') or []
    leaves = ev.get('leave') or []
    visits_sid = {v.get('sid') for v in visits if v.get('sid')}
    ref_cnt, page_cnt, act_cnt, size_cnt = {}, {}, {}, {}
    for v in visits:
        # 再来一道 host 归一：写入时已经砍成域名了，但老数据 / 别的写入方
        # 可能塞的是完整 URL —— 这一页常被截图，绝不把带参数的地址摆上去。
        r = telmod.ref_host(v.get('ref')) or '直接打开 / 收藏夹'
        ref_cnt[r] = ref_cnt.get(r, 0) + 1
        if v.get('page'):
            page_cnt[v['page']] = page_cnt.get(v['page'], 0) + 1
        if v.get('w'):
            size_cnt['%dx%d' % (v.get('w') or 0, v.get('h') or 0)] = \
                size_cnt.get('%dx%d' % (v.get('w') or 0, v.get('h') or 0), 0) + 1
    for c in clicks:
        if c.get('act'):
            act_cnt[c['act']] = act_cnt.get(c['act'], 0) + 1
    durs = [int(l.get('dwell') or 0) for l in leaves if l.get('dwell') is not None]
    frontend = {
        "visits": len(visits), "visitors": len(visits_sid),
        "clicks": len(clicks),
        "avg_dwell": (sum(durs) // len(durs)) if durs else 0,
        "refs": [[k, v] for k, v in sorted(ref_cnt.items(), key=lambda x: (-x[1], x[0]))[:6]],
        "pages": [[k, v] for k, v in sorted(page_cnt.items(), key=lambda x: -x[1])[:6]],
        "acts": [[k, v] for k, v in sorted(act_cnt.items(), key=lambda x: -x[1])[:10]],
        "sizes": [[k, v] for k, v in sorted(size_cnt.items(), key=lambda x: -x[1])[:4]],
        # 只点了按钮但一句话没说过的人：说明"想干点什么、但没聊起来"
        "silent": len([s for s in visits_sid if s not in {t['sid'] for t in turns}]),
    }

    # ---- ⑥ 会话结束方式 ----
    end_cnt, end_turns = {}, {}
    for l in leaves:
        r = l.get('reason') or 'unknown'
        end_cnt[r] = end_cnt.get(r, 0) + 1
        if l.get('turns') is not None:
            end_turns.setdefault(r, []).append(int(l['turns']))
    ending = {"total": len(leaves),
              "items": [[k, END_LABEL.get(k, k), v] for k, v in
                        sorted(end_cnt.items(), key=lambda x: -x[1])],
              "avg_turns": {k: round(sum(v) * 1.0 / len(v), 1)
                            for k, v in end_turns.items() if v}}

    # 埋点是从哪个时间点开始有的 —— 页面上要写清楚，免得把"没上线"看成"没人用"
    first_t = min([e.get('t') or 0 for e in events] or [0])
    coverage = {"events": len(events), "since": _daykey(first_t) if first_t else "",
                "dir": telmod.Telemetry.from_config(app.cfg).dir}

    # 把「成功 / 失败 / 来访」按天挂回时间线，给看板上那两张趋势图用
    ok_day, bad_day, vis_day = {}, {}, {}
    for g in gens:
        k = _daykey(g.get('t'))
        if not k:
            continue
        if g.get('ok'):
            ok_day[k] = ok_day.get(k, 0) + 1
        else:
            bad_day[k] = bad_day.get(k, 0) + 1
    for v in visits:
        k = _daykey(v.get('t'))
        if k:
            vis_day[k] = vis_day.get(k, 0) + 1
    for r in series:
        r['ok'] = ok_day.get(r['d'], 0)
        r['fail'] = bad_day.get(r['d'], 0)
        r['visit'] = vis_day.get(r['d'], 0)

    return {
        "ok": True,
        "now": time.strftime('%Y-%m-%d %H:%M:%S'),
        "days": days,
        "kpi": {
            "sessions": n_sess,
            "turns": n_turns,
            "agents": len(ag),
            "library": lib_stat.get('count', 0),
            "generated": gen_total,
            "points": points_total,
            "skills": len(app.registry.skills),
            "kb_docs": len(app.kb.docs) if app.kb else 0,
            "kb_qa": len(app.kb.qa) if app.kb else 0,
            # 会话深度：轮次 ÷ 会话。1.0 上下说明用户基本问一句就走，
            # 2~3 才是"真的在来回聊"。这是判断人设/流程好不好用的第一手信号。
            "avg_turns": round(n_turns * 1.0 / n_sess, 1) if n_sess else 0,
            # 运营待办：没打上标签的图在检索里等于不存在
            "untagged": (lib_stat.get('by_status') or {}).get('untagged', 0),
            "span_from": _daykey(span_from),
            "span_to": _daykey(span_to),
        },
        "compare": compare,
        "series": series,
        "agents": agents,
        "skills": skills,
        "demand": demand,
        "dim_label": DIM_LABEL,
        "queries": queries,
        "library": {"count": lib_stat.get('count', 0),
                    "by_source": lib_stat.get('by_source') or {},
                    "by_status": lib_stat.get('by_status') or {},
                    "dims": dim_top, "colors": color_top},
        "engine_usage": engine_usage,
        # ---- 六块原来"没落盘"的指标（见上面埋点段）----
        "gen": gen,            # ① 出图成功率 + ④ 耗时与真实点数
        "funnel": funnel,      # ② 提案 → 确认转化率
        "kb_stat": kbstat,     # ③ 知识库命中率
        "frontend": frontend,  # ⑤ 前端埋点：来路 / 停留 / 点击
        "ending": ending,      # ⑥ 会话结束方式
        "coverage": coverage,  # 埋点从哪天开始有数据
        # 没有账号体系和计费后端，这两块数据客观上不存在 —— 如实说明，
        # 不编数字。要做真的，得先有登录/钱包（那是另一个大工程）。
        "accounts": {
            "available": False,
            "reason": "后端还没有账号与计费模块（前台的充值/注册是演示用的），"
                      "接上之后这里才会有真实曲线。"},
    }


def save_config(app, payload):
    """改 settings + 密钥。密钥传 KEEP 表示不动。"""
    st = dict(app.cfg.settings)
    sec = dict(app.cfg.secrets)
    notes = []          # 被拒绝/跳过的项，回给页面提示用

    llm = payload.get('llm') or {}
    for k in ('base_url', 'model'):
        if k in llm and str(llm[k]).strip():
            st.setdefault('llm', {})[k] = str(llm[k]).strip()
    for k in ('temperature', 'max_tokens'):
        if k in llm and llm[k] not in (None, ''):
            try:
                st.setdefault('llm', {})[k] = float(llm[k]) if k == 'temperature' \
                    else int(llm[k])
            except (TypeError, ValueError):
                pass

    img = payload.get('image') or {}
    for k in ('base_url', 'model', 'provider'):
        if k in img and str(img[k]).strip():
            st.setdefault('image', {})[k] = str(img[k]).strip()
    if 'cost' in img and img['cost'] not in (None, ''):
        try:
            st.setdefault('image', {})['cost'] = int(img['cost'])
        except (TypeError, ValueError):
            pass
    # 一次出几张：每张都是一次独立的并发请求，给个上限 ——
    # 后台顺手填个 20，用户那边就得同时开 20 条连接，网关多半先扛不住。
    if img.get('variants') not in (None, ''):
        try:
            st.setdefault('image', {})['variants'] = max(1, min(8, int(img['variants'])))
            # 生成已换新引擎：后台这个「出图张数」也要同步驱动 patgen 的版数，
            # 否则页面改了张数、实际生成还是旧值（最容易被当成"没生效"）。
            st.setdefault('patgen', {})['variants'] = max(
                1, min(6, st['image']['variants']))
        except (TypeError, ValueError):
            pass

    # 识图用的模型名。它不是密钥，空值是合法的（留空 = 跟对话模型同一个），
    # 所以这里跟网关那几项不一样：**允许写空**，不按"留空=不改"处理。
    vis = payload.get('vision')
    if isinstance(vis, dict) and 'model' in vis:
        st.setdefault('vision', {})
        st['vision']['model'] = str(vis.get('model') or '').strip()

    # 描稿仿稿：**默认沿用生图那套**，所以这里空值的语义跟别处相反 ——
    # 留空 = "继承生图"，要真的写进去（把之前的覆盖清掉），而不是"这项不改"。
    # 否则运营填过一次模型，就再也退不回"跟花型设计师共用"的状态了。
    tr = payload.get('trace')
    if isinstance(tr, dict) and tr:
        st.setdefault('trace', {})
        for k in ('provider', 'base_url', 'model'):
            if k in tr:
                st['trace'][k] = str(tr.get(k) or '').strip()
        if 'cost' in tr:
            cv = str(tr.get('cost') or '').strip()
            if not cv:
                st['trace'].pop('cost', None)       # 留空 = 继承生图点数
            else:
                try:
                    st['trace']['cost'] = max(0, int(float(cv)))
                except (TypeError, ValueError):
                    pass

    se = payload.get('seamless') or {}
    if se:
        st.setdefault('seamless', {})
        if se.get('enabled') is not None:
            st['seamless']['enabled'] = bool(se['enabled'])
        if se.get('cost') not in (None, ''):
            try:
                st['seamless']['cost'] = int(se['cost'])
            except (TypeError, ValueError):
                pass
        # 候选数直接决定等待时间（模型不可复现，跑几个挑最好），
        # 所以给个上限：后台顺手填个 10，用户就得等十分钟
        if se.get('candidates') not in (None, ''):
            try:
                st['seamless']['candidates'] = max(1, min(4, int(se['candidates'])))
            except (TypeError, ValueError):
                pass
        # 网关和模型名都是纯文本，允许留空表示"用代码里的默认值"。
        # 但**留空 ≠ 清空**：只有真的填了才写，避免运营顺手一保存就把默认值抹了。
        for k in ('base_url', 'analyze_base', 'image_model', 'analyze_model'):
            if se.get(k) not in (None, ''):
                st['seamless'][k] = str(se[k]).strip()

    # 分层 PSD：点数、最多分几层，以及**视觉诊断**走哪家。
    # 和四方连续同一个套路 —— 网关/模型留空 = 沿用代码里的默认（通用那套），
    # 不写空值，免得上手一保存就把默认抹了。
    psd = payload.get('psdlayer') or {}
    if psd:
        st.setdefault('psdlayer', {})
        if psd.get('enabled') is not None:
            st['psdlayer']['enabled'] = bool(psd['enabled'])
        if psd.get('cost') not in (None, ''):
            try:
                st['psdlayer']['cost'] = max(0, int(psd['cost']))
            except (TypeError, ValueError):
                pass
        # 层数上限跟着点数走：多一层 = 多跑一次云端工作流，钱和时间都翻倍。
        # 超过 8 层在真花型上已经没有意义了（元素类型没那么多）。
        if psd.get('max_layers') not in (None, ''):
            try:
                st['psdlayer']['max_layers'] = max(1, min(8, int(psd['max_layers'])))
            except (TypeError, ValueError):
                pass
        for k in ('vlm_base', 'vlm_model'):
            if psd.get(k) not in (None, ''):
                st['psdlayer'][k] = str(psd[k]).strip()

    kb = payload.get('kb') or {}
    if kb:
        st.setdefault('kb', {})
        # 只有明确给了布尔值才改；null/缺省一律当"这项没动"
        if kb.get('enabled') is not None:
            st['kb']['enabled'] = bool(kb['enabled'])
        if 'agents' in kb:
            picked = [a for a in (kb['agents'] or [])
                      if a in app.cfg.settings['agents']]
            # 一个角色都没选，几乎都是"页面状态还没加载好就顺手点了保存"，
            # 不是真想让谁都不吃资料 —— 那件事该用 enabled 那个开关表达。
            # 真写成空数组的后果很脏：页面下次依然全不亮，运营怎么点都修不回来
            # （真发生过：知识库被静默关掉，机器人不再按资料回答）。
            if picked:
                st['kb']['agents'] = picked
            else:
                notes.append('知识库的启用角色不能全空，这一项保持原样；'
                             '要停用请关掉「启用知识库严格模式」')
        if 'score_min' in kb and kb['score_min'] not in (None, ''):
            try:
                st['kb']['score_min'] = float(kb['score_min'])
            except (TypeError, ValueError):
                pass

    libc = payload.get('library') or {}
    if libc:
        st.setdefault('library', {})
        for k in ('enabled', 'auto_ingest'):
            if libc.get(k) is not None:
                st['library'][k] = bool(libc[k])
        if 'thumb_px' in libc and libc['thumb_px']:
            try:
                st['library']['thumb_px'] = int(libc['thumb_px'])
            except (TypeError, ValueError):
                pass

    # 密钥：只有**真的填了新值**才改。
    # 空串/占位符一律当作"没动" —— 否则运营打开页面顺手点保存，密钥就被清成空了，
    # 服务当场降级到规则模式，还查不出为什么。要清空得显式给 clear_keys。
    changed = []
    cleared = []
    for field in KEY_FIELDS:
        v = payload.get(field)
        if v is None:
            continue
        v = str(v).strip()
        if not v or v == KEEP or v == MASK or v.startswith(MASK):
            continue
        sec[field] = v
        changed.append(field)
    for field in (payload.get('clear_keys') or []):
        if field in KEY_FIELDS and field in sec:
            sec[field] = ''
            changed.append(field + '(已清空)')
            cleared.append(field)

    app.cfg.save_settings(st)
    if changed:
        # 有显式清空时要把 force 传下去：config.save_secrets 的兜底是
        # 「新的空值当误操作、救回旧值」，它防的是**手滑**；
        # 而 clear_keys 是用户明确点的清空，得让它真的清掉。
        # 不带 force 的后果是静默失效：页面提示"已清空"、日志也写了，
        # 可密钥还在 —— 最难查的那一类。
        app.cfg.save_secrets(sec, force=bool(cleared))
    app.reload()
    return {"ok": True, "changed_keys": changed, "notes": notes,
            "note": "已保存并重新加载"}


# ---------- 人设与风格 ----------
def persona_list(app):
    out = []
    for agent, conf in app.cfg.settings['agents'].items():
        p = app.personas.get(agent) or {}
        out.append({"agent": agent, "file": conf.get('persona'),
                    "name": p.get('name') or agent,
                    "chars": len(p.get('body') or ''),
                    "body": p.get('body') or '',
                    "style": app.cfg.style_of(agent),
                    "skills": conf.get('skills') or []})
    return {"ok": True, "items": out,
            "presets": [{"key": k, "label": v[0], "body": v[1]}
                        for k, v in DEFAULT_STYLES.items()]}


def save_persona(app, payload):
    """存人设全文 + 说话风格。两件事分开写，一个失败不影响另一个。"""
    agent = payload.get('agent')
    conf = app.cfg.settings['agents'].get(agent)
    if not conf:
        return {"ok": False, "error": "没有这个机器人：%s" % agent}
    did = []
    body = payload.get('body')
    if body is not None:
        fn = conf.get('persona') or (agent + '.md')
        p = os.path.join(app.cfg.path('personas'), os.path.basename(fn))
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(body)
        os.replace(tmp, p)
        did.append('人设')
        sync.rev_bump('persona')
    style = payload.get('style')
    if style is not None:
        styles = dict(app.cfg.styles or {})
        got = dict(styles.get(agent) or {})
        if 'preset' in style:
            got['preset'] = style['preset']
        if 'body' in style:
            got['body'] = style['body']
        styles[agent] = got
        app.cfg.save_styles(styles)
        did.append('说话风格')
    if not did:
        return {"ok": False, "error": "没有要保存的内容"}
    app.reload()
    return {"ok": True, "saved": did, "agent": agent}


# ---------- 知识库 ----------
def kb_list(app):
    if not app.kb:
        return {"ok": False, "error": "知识库未启用"}
    return {"ok": True, "stats": app.kb.stats(), "docs": app.kb.list_docs(),
            "qa": app.kb.list_qa()}


def kb_save(app, payload):
    """op: add_doc / del_doc / add_qa / update_qa / del_qa / lookup"""
    if not app.kb:
        return {"ok": False, "error": "知识库未启用"}
    op = payload.get('op')
    if op == 'add_doc':
        r = app.kb.add_doc(payload.get('text'), payload.get('title'),
                           source=payload.get('source') or 'admin',
                           keywords=payload.get('keywords'))
        return r
    if op == 'del_doc':
        return app.kb.remove_doc(payload.get('id'))
    if op == 'add_qa':
        return app.kb.add_qa(payload.get('q'), payload.get('a'), payload.get('tags'))
    if op == 'update_qa':
        return app.kb.update_qa(payload.get('id'), payload.get('fields') or {})
    if op == 'del_qa':
        return app.kb.remove_qa(payload.get('id'))
    if op == 'lookup':
        # 后台「检索测试」：要看得到模型将看到什么，不然调阈值全靠猜
        res = app.kb.lookup(payload.get('q'), agent=payload.get('agent'))
        res['prompt'] = app.kb.render_for_prompt(res)
        return res
    return {"ok": False, "error": "未知操作：%s" % op}


def kb_doc(app, did):
    if not app.kb:
        return {"ok": False, "error": "知识库未启用"}
    text = app.kb.doc_text(did)
    if text is None:
        return {"ok": False, "error": "没有这份资料"}
    d = app.kb.docs[did]
    return {"ok": True, "id": did, "title": d.get('title'),
            "keywords": d.get('keywords') or [], "text": text}


# ---------- 图库：扫目录 ----------
def _data_root(app):
    """数据根目录：出图/图库/会话/知识库 都在它下面（项目根的「数据/」）。

    正常这三个目录同属一个根，取谁的上级都一样；但测试会用环境变量
    把图库/出图挪到临时目录，所以多取几个候选、挑一个真能对上目录的，
    别因为图库被隔离就找不到 generated。
    """
    cands = []
    for getter in (lambda: app.library.root,
                   lambda: app.cfg.path('sessions'),
                   lambda: app.imagegen.outdir):
        try:
            cands.append(os.path.dirname(os.path.abspath(getter())))
        except Exception:
            pass
    for d in cands:
        if (os.path.isdir(os.path.join(d, 'generated'))
                or os.path.isdir(os.path.join(d, 'library'))):
            return d
    return cands[0] if cands else None


def _safe_dir(path, app=None):
    """把运营填的路径解析成一个可扫的安全目录。

    相对路径按两个基准找：先按进程当前目录，再按数据根 ——
    代码和数据分家之后（「后端/」和「数据/」各一个文件夹），
    后台进程的 cwd 是「后台/」，而运营顺手填的 'generated' / 'library'
    其实躺在项目根的「数据/」下，只按 cwd 找会误报"目录不存在"。
    """
    raw = os.path.expanduser(str(path or '').strip())
    if not raw:
        return None, '目录不存在：%s' % raw
    cands = [os.path.abspath(raw)]
    if not os.path.isabs(raw):
        dr = _data_root(app)
        if dr:
            cands.append(os.path.normpath(os.path.join(dr, raw)))
    p = next((c for c in cands if os.path.isdir(c)), cands[0])
    if not os.path.isdir(p):
        return None, '目录不存在：%s' % p
    for b in BLOCK_DIRS:
        if p == b or p.startswith(b + os.sep):
            return None, '这个目录不让扫（%s）' % p
    return p, None


SCAN_STATE = {"running": False, "done": 0, "total": 0, "stats": {},
              "started": 0, "log": []}
SCAN_LOCK = threading.Lock()


# ---------- 导入闸门：先过自动打标签，过了才准入库 ----------
def _atag_engine(app):
    """建一个打标引擎。只读配置、不起子进程，建起来零成本（同 _psdlayer_info）。"""
    from .atag import AtagEngine
    return AtagEngine(app.cfg)


def import_via_atag(app, blob, mime='image/png', src_name=None, name=None,
                    source='upload', engine=None, ready=None):
    """后台导入的**闸门**：每张图先过自动打标签算法，过了才落库。

    闸门本体在 core/atag.py 的 ingest_blob（全库共用一处实现，
    出图产物自动入库也走它）；这里只负责把 app 拆成引擎和图库。

    ready=None 时自己探一次引擎（单张导入走这条）；
    扫描批量传 True —— 开跑前已经探过了，没必要每张再探一遍。
    """
    from . import atag as _atag
    eng = engine or _atag_engine(app)
    return _atag.ingest_blob(eng, app.library, blob, mime=mime,
                             src_name=src_name, name=name, source=source,
                             ready=ready)


def import_file_via_atag(app, path, source='import', move=False, engine=None,
                         ready=None):
    """扫目录用的同一条闸门（文件版）。move=True 时入库成功才删源文件，
    被拒的不删 —— 那是客户的资料，不能替他做主。"""
    from . import atag as _atag
    eng = engine or _atag_engine(app)
    r = _atag.ingest_file(eng, app.library, path, source=source, ready=ready)
    if r.get('ok') and not r.get('dup') and move:
        try:
            os.remove(path)
        except Exception:
            pass
    return r


def _atag_info(app):
    """入库自动打标的实时状态（花型库页的状态条用）。

    探一次引擎起没起（20 秒内重复问走缓存，刷页面不会每下都起个子进程）。
    只回 ready + 一句人话，**不回端点地址** —— 这个字段会上后台页面，
    而那页面经常被截图丢群里。
    """
    try:
        eng = _atag_engine(app)
        d = eng.describe()
        if not d.get('available'):
            return {"installed": bool(d.get('installed')), "ready": False,
                    "note": d.get('note') or ''}
        pr = eng.probe()
        return {"installed": True, "ready": bool(pr.get('ready')),
                "note": pr.get('note') or ''}
    except Exception as e:                               # noqa: BLE001
        return {"installed": False, "ready": False,
                "note": "状态没查出来：%s: %s" % (type(e).__name__, e)}


def library_scan(app, payload):
    """后台扫目录入库。要几十秒到几分钟，所以异步跑 + 可轮询进度。"""
    path, err = _safe_dir(payload.get('path'), app)
    if err:
        return {"ok": False, "error": err}
    with SCAN_LOCK:
        if SCAN_STATE['running']:
            return {"ok": False, "error": "上一批还没导完，等一会儿再点"}
    if payload.get('probe'):
        files = _scan_files(path, payload.get('limit'), payload.get('grep'))
        return {"ok": True, "probe": True, "count": len(files),
                "sample": [os.path.basename(f) for f in files[:8]]}

    files = _scan_files(path, payload.get('limit'), payload.get('grep'))
    if not files:
        return {"ok": False, "error": "这个目录里没扫到图片"}

    # 闸门前置：算法没就绪就别开始。边扫边失败的话，运营要盯着进度条
    # 跑完才发现一张都没进来，白等几分钟。
    eng = _atag_engine(app)
    if not eng.available:
        return {"ok": False, "error": eng.missing_note}
    pr = eng.probe(fresh=True)
    if not pr.get('ready'):
        return {"ok": False,
                "error": "自动打标还没就绪：%s" % (pr.get('note') or '引擎没起')}

    workers = max(1, min(8, int(payload.get('workers') or 3)))
    source = payload.get('source') or 'import'
    move = bool(payload.get('move'))
    # any_image / no_tag 两个老开关就此退休：导入必须过算法是硬要求
    # （见 core/atag.py 顶部说明），不再给"不过算法直接收"的口子。

    with SCAN_LOCK:
        SCAN_STATE.update({"running": True, "done": 0, "total": len(files),
                           "stats": {"ok": 0, "dup": 0, "skip": 0, "fail": 0},
                           "started": time.time(), "log": []})

    def work(p):
        try:
            r = import_file_via_atag(app, p, source=source, move=move,
                                     engine=eng, ready=True)
        except Exception as e:
            r = {"ok": False, "error": '%s: %s' % (type(e).__name__, e)}
        with SCAN_LOCK:
            SCAN_STATE['done'] += 1
            s = SCAN_STATE['stats']
            if r.get('dup'):
                s['dup'] += 1
            elif r.get('not_pattern'):
                s['skip'] += 1
                SCAN_STATE['log'].append('不是花型：%s' % os.path.basename(p))
            elif not r.get('ok'):
                s['fail'] += 1
                SCAN_STATE['log'].append('失败：%s（%s）'
                                         % (os.path.basename(p), r.get('error')))
            else:
                s['ok'] += 1
        return r

    def run():
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(work, files))
        finally:
            with SCAN_LOCK:
                SCAN_STATE['running'] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(files), "workers": workers}


def scan_progress(app):
    with SCAN_LOCK:
        s = dict(SCAN_STATE)
        s['stats'] = dict(SCAN_STATE['stats'])
        s['log'] = list(SCAN_STATE['log'][-10:])
    s['ok'] = True
    return s


def _scan_files(path, limit, grep):
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            if not f.lower().endswith(exts) or f.startswith('.'):
                continue
            full = os.path.join(root, f)
            if os.sep + 'library' + os.sep in full + os.sep:
                continue
            if grep and grep not in f:
                continue
            out.append(full)
    out.sort()
    try:
        n = int(limit or 0)
    except (TypeError, ValueError):
        n = 0
    return out[:n] if n else out


# ---------- 会话 ----------
def session_list(app, limit=20, q='', day=''):
    """会话列表。q 按会话 ID 模糊搜，day 按最后活动日期（YYYY-MM-DD）筛。

    两个筛选都在**全部**会话上做、筛完再截断 —— 若跟在 limit 后面筛，
    列表只带着最近 20 条，搜上周某一天会一条都搜不到。
    """
    d = app.cfg.path('sessions')
    rows = []
    try:
        for f in sorted(os.listdir(d), reverse=True):
            if not f.endswith('.jsonl'):
                continue
            p = os.path.join(d, f)
            turns = 0
            last = ''
            try:
                with open(p, encoding='utf-8') as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get('type') == 'turn':
                            turns += 1
                            # 轮次里存的是 Unix 时间 `t`（早先这里读成了不存在的
                            # `ts`，于是"最后活跃"永远是空的，列表排不出先后）。
                            t = (rec.get('turn') or {}).get('t')
                            if t:
                                last = time.strftime('%Y-%m-%d %H:%M',
                                                     time.localtime(float(t)))
            except OSError:
                pass
            # 按最后活动筛日期；一句话都没说过的会话退回文件时间，
            # 否则「筛今天」会把今天刚建、还没聊起来的会话漏掉
            d0 = (last or '')[:10]
            if not d0:
                try:
                    d0 = time.strftime('%Y-%m-%d',
                                       time.localtime(os.path.getmtime(p)))
                except OSError:
                    d0 = ''
            rows.append({"id": f[:-6], "turns": turns, "last": last,
                         "day": d0, "bytes": os.path.getsize(p)})
    except OSError:
        pass
    rows.sort(key=lambda x: (x.get('last') or ''), reverse=True)
    total = len(rows)
    q_raw = (q or '').strip()
    q_low = q_raw.lower()
    day = (day or '').strip()
    if q_low:
        rows = [r for r in rows if q_low in (r.get('id') or '').lower()]
    if day:
        rows = [r for r in rows if r.get('day') == day]
    cap = 500 if (q_raw or day) else limit      # 筛了就别再截成 20 条
    return {"ok": True, "items": rows[:cap], "total": total, "matched": len(rows),
            "q": q_raw, "day": day, "dir": d, "root": ROOT}
