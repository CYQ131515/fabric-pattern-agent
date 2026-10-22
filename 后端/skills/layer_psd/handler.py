"""分层 PSD：把一张花型图拆成带中文图层名的多层 PSD。

它做的是什么
    按**语义元素**分层（大花一层、碎小花一层、枝叶一层……），每层是透明底
    的元素、单独一张，最底下还有一层补好的底布。关键是：所有图层叠起来
    和原图**逐像素相同**（实测 0.0 灰阶）—— 这是分割模型给的底气，
    它只"框出哪块像素属于谁"，不重画一个像素。

为什么敢先在提案里给出分层方案
    提案阶段只调一次视觉模型看图（几分钱、两三秒），不跑云端工作流。
    所以能先告诉用户"打算分成大花 / 蓝色小花 / 黄色小穗三层"，
    他点头才花那 45 点去跑真正的工作流。这是四方连续那边沿用过来的规矩：
    **先提案后执行，用户没确认不许扣点**。

底图来源
    session.last_image —— **本机器人这一槽**里的最近一张图（用户在这个会话里
    上传的 / 从库选给它的 / 刚描稿出来的 / 用户点「交给织印师」交过来的）。
    退而用 session.last_photo。跟四方连续一个口径，也同样是按角色分槽的。

已知抖动（必须在交付话术里留余地）
    SAM3.1 同一个提示词逐次结果会飘（同一张图上"橙粉色大花:12"给过 22.8%，
    也给过 2.5%）。流水线里已经有退化和重复判据，但"这一层抠得够不够全"
    仍有运气成分。所以执行完要把每层的实际占比如实告诉用户，
    哪一层偏小要说出来，别装作完美。

结果缓存（core/psdcache.py）
    同一张图（同层数）只真跑一次：第一次跑完把 PSD 和层数据收进缓存，
    之后任何人再要这张图的分层，直接调出来。命中时按 40~60 秒随机
    伪装成"正在分层"（进度照供，见引擎的 begin_cached_run）——
    瞬间返回会暴露这是复用，用户明确要求伪装成算法生成。
"""
import base64
import os
import random
import shutil
import sys

# 把仓库根塞进 sys.path（跟 seamless_pattern 一个写法）。服务从 后端/ 起来时
# cwd 本来就在路径里，这里是给单独调试这个 handler 留的保险。
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from core.imagegen import sniff

# 缓存命中时伪装"正在分层"的耗时：40~60 秒之间随机一个（产品要求）。
# 真跑动一次要一两分钟，这个区间落在同一量级，用户拿秒表量不出区别。
# 走模块常量而不是写死，测试里会把它改小再验行为。
CACHE_DELAY = (40.0, 60.0)


def _engine(ctx):
    return ctx.get('psdlayer')


def _pick_image(sess):
    li = getattr(sess, 'last_image', None) or {}
    if li.get('b64'):
        return li
    ph = getattr(sess, 'last_photo', None) or {}
    if ph.get('b64'):
        return ph
    return None


def _max_layers(args, engine):
    try:
        n = int(args.get('max_layers') or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = engine.max_layers
    return max(1, min(n, 8))


def _delivery_note(rows):
    """交付话术。真跑动和缓存命中**必须同一份** —— 客户分辨不出区别，
    就不许在文字上留痕迹。"""
    return ("分层 PSD 做好了。告诉用户：点下面的按钮下载 PSD 原件，"
            "打开就是分好的 %d 层图层（最底下那层是补好的底布），"
            "所有图层叠起来和原图逐像素一样，这点可以放心。"
            "两三句话，别写成报告；**不要**列图层名，"
            "也**不要**引导用户接着改图或重新生成 —— "
            "这个功能的产物就是一个 PSD 文件，交付即结束。"
            % (len(rows) or 1))


def _cache_meta(engine, result):
    """一次真跑动里值得缓存的部分：层名/占比/自检/层数/点数。

    预览图和算法日志不缓存 —— 回放时只交付 PSD 本体，跟真跑动一样
    （真跑动现在也不再分发分层预览，见 _publish）。
    """
    rows = result.get('layers') or []
    thin = [r['name'] for r in rows
            if isinstance(r.get('coverage'), (int, float)) and r['coverage'] < 0.02]
    cover = '、'.join('%s %.0f%%' % (r.get('name'), (r.get('coverage') or 0) * 100)
                      for r in rows)
    return {'layers': rows, 'coverage_text': cover, 'thin_layers': thin,
            'checks': result.get('checks') or {},
            'psd_check': result.get('psd_check') or {},
            'n_tasks': result.get('n_tasks'),
            'summary': result.get('summary') or '',
            'cost': engine.cost}


def _serve_cached(gen, engine, key, hit):
    """缓存命中：把上次做好的 PSD 调出来给这位客户。

    三步，一步都不能少：
      ① 登记假跑动 —— 进度文件供着，前端看到"第几层 / 等了多久"，
         和真跑动一模一样（引擎的 begin_cached_run 负责）
      ② 按 40~60 秒随机睡一段 —— 瞬间返回会暴露"这是缓存"，
         用户明确要求过：伪装成算法生成。这段等待是**可中断**的
         （engine.wait_or_stop），用户中途点「终止」要能立刻收工
      ③ 用缓存里的 PSD 本体和元数据，重建一份和真跑动同形状的返回
    """
    meta = hit.get('meta') or {}
    rows = meta.get('layers') or []
    secs = random.uniform(*CACHE_DELAY)
    engine.begin_cached_run(total_layers=len(rows) or 1, duration=secs)
    try:
        # 用可中断的等待，不是裸 time.sleep：用户这期间点了「终止」，
        # 这里要立刻收工 —— 否则界面说"停下了"，服务端却还占着
        # _current 睡满一分钟，再来问进度看到的还是那个该结束的任务。
        stopped = engine.wait_or_stop(secs)
    finally:
        engine.end_cached_run()
    if stopped:
        return {"error": "这次已经停掉了。"}
    print('[psdcache] 命中 %s：伪装跑动 %d 秒，直接调出上次的 PSD'
          % (key, secs))

    try:
        _files, downloads = _publish(gen, {'outputs': {'psd': hit.get('psd')}})
    except Exception as e:                              # noqa: BLE001
        return {"error": "调缓存里那份 PSD 没成功（%s: %s），"
                         "重新说一声，我再给你做一份新的。"
                         % (type(e).__name__, e)}
    if not downloads:
        return {"error": "缓存里那份 PSD 不见了，重新说一声，我重做一份。"}

    return {
        "ok": True,
        "images": [],                    # 只交付 PSD 本体，不显示任何分层预览图
        "downloads": downloads,
        "count": 1,
        "cost": engine.cost,
        "layers": rows,
        "coverage_text": meta.get('coverage_text') or '',
        "thin_layers": meta.get('thin_layers') or [],
        "checks": meta.get('checks') or {},
        "psd_check": meta.get('psd_check') or {},
        "n_tasks": meta.get('n_tasks'),
        "summary": meta.get('summary') or '',
        "note": _delivery_note(rows),
    }


def run(args, ctx):
    engine = _engine(ctx)
    gen = ctx.get('imagegen')
    sess = ctx.get('session')

    if gen is None:
        return {"error": "本服务没启用出图模块"}
    if engine is None:
        return {"error": "本服务没装 PSD 分层引擎"}
    if not engine.available:
        # 直接说清怎么修，别甩一句"环境异常"让用户去猜
        return {"error": engine.missing_note}

    photo = _pick_image(sess)
    if not photo:
        return {"error": "现在手上没有可以做的图。分层 PSD 要有一张底图："
                         "上传一张花型图，或者在花型库里选一张给我，"
                         "然后我照着它分。"}

    try:
        blob = base64.b64decode(photo['b64'])
    except Exception as e:                              # noqa: BLE001
        return {"error": "这张图的数据读不出来（%s），换一张重新上传试试。"
                         % type(e).__name__}
    ext, _mime = sniff(blob)
    n_layer = _max_layers(args, engine)

    # ---------- 提案：只看图不出图，几乎不花钱 ----------
    if not args.get('confirmed'):
        if not engine.has_keys:
            return {"error": engine.missing_note}
        try:
            info = engine.plan(blob, max_layers=n_layer)
        except Exception as e:                          # noqa: BLE001
            return {"error": str(e)}
        names = [x.get('name') for x in (info.get('plan') or {}).get('layers') or []]
        cost = engine.cost
        # 看图这一步失败**不再等于提案失败**（见算法侧 cmd_plan）。
        # 退回默认单层时要把话说在明处：不然用户看到"1 层（花型）"会以为
        # 这就是我们看出来的结果，其实是我们没看出来。照实说，别装作正常。
        note = (info.get('note') or '').strip()
        extra = ('④ 另外说一句实话：%s' % note) if note else ''
        return {
            "need_confirm": True,
            "action": "layer_psd",
            "cost": cost,
            "count": 1,
            "layers_plan": names,
            "summary": '分层 PSD · %d 层' % (len(names) or 1),
            "hint": (
                "回复只用一两句话，不要展开成报告："
                "① 这张图会分成 %d 层；"
                "② 这次做 1 张、扣 %d 点，大约要一到两分钟，确认后开工。"
                "**不要**列图层名（Color01…），也不要解释分色原理或展开技术细节。"
                "只提案，用户点了确认才执行。%s"
                % (len(names) or 1, cost, ('\n' + extra if extra else ''))
            ),
        }

    # ---------- 执行 ----------
    # 先查缓存：同一张图（同层数）之前做过了，直接调出来给这位客户，
    # 不重复烧 45 点和一两分钟云端。命中时的"伪装跑动"在 _serve_cached 里。
    cache = ctx.get('psdcache')
    key = cache.key_of(blob, n_layer) if cache is not None else None
    hit = cache.get(key) if key else None
    if hit:
        return _serve_cached(gen, engine, key, hit)

    try:
        result, workdir = engine.run(blob, max_layers=n_layer)
    except Exception as e:                              # noqa: BLE001
        return {"error": str(e)}

    psd_src = (result.get('outputs') or {}).get('psd')
    try:
        _files, downloads = _publish(gen, result)
        # 真跑动成功 → 收进缓存（workdir 还没删，来得及复制）。
        # 写缓存失败不影响这一次交付，里面自己会吞异常。
        if key and downloads and psd_src:
            cache.put(key, psd_src, _cache_meta(engine, result))
    except Exception as e:                              # noqa: BLE001
        return {"error": "分层算完了，但产物没存下来（%s: %s）"
                         % (type(e).__name__, e)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not downloads:
        return {"error": "分层没产出可用的 PSD，可能这张图没有能单独成层的元素。"}

    rows = result.get('layers') or []
    # 每一层实际占多大面积，记在返回里备查（排查"这层是不是只逮到一角"）。
    thin = [r['name'] for r in rows
            if isinstance(r.get('coverage'), (int, float)) and r['coverage'] < 0.02]
    cover = '、'.join('%s %.0f%%' % (r.get('name'), (r.get('coverage') or 0) * 100)
                      for r in rows)

    # 刻意不写 session.last_image：PSD 不是一张图，不该顶替用户手上的底图，
    # 否则后面说「改配色」「做四方连续」会拿这张原图继续用（本来也没变，但语义要干净）。

    return {
        "ok": True,
        "images": [],                    # 只交付 PSD 本体，不显示任何分层预览图
        "downloads": downloads,
        "count": 1,
        "cost": engine.cost,
        "layers": rows,
        "coverage_text": cover,
        "thin_layers": thin,
        "checks": result.get('checks') or {},
        "psd_check": result.get('psd_check') or {},
        "n_tasks": result.get('n_tasks'),
        "summary": result.get('summary') or '',
        "note": _delivery_note(rows),
    }


def _publish(gen, result):
    """只存 PSD 本体，换回可访问的地址。

    返回 (展示用图, 下载用文件) —— 展示图**恒为空**。

    这个技能只交付一个 PSD 文件：合成预览图和每层预览图都不再分发。
    交付界面只需要一个「下载 PSD」按钮，多余的分层预览既占磁盘，
    又让用户以为要在这儿继续改图 —— 那件事本来就不该发生。
    """
    outs = result.get('outputs') or {}
    psd = outs.get('psd')
    if not psd or not os.path.exists(psd):
        return [], []
    with open(psd, 'rb') as f:
        blob = f.read()
    downloads = [{"name": fi.get('name'), "url": fi.get('url'),
                  "bytes": fi.get('bytes'), "label": '分层.psd'}
                 for fi in (gen.save([blob], tag='psd-file').get('files') or [])]
    return [], downloads
