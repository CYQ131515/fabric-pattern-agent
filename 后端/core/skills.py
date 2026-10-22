"""技能注册表：skills/<name>/skill.json + handler.py

skill.json 示例：
{
  "name": "search_patterns",
  "description": "按四维标签检索花型库",
  "parameters": {"type": "object", "properties": {...}, "required": []},
  "confirm": false,
  "cost": 0,
  "ingest": true,
  "handler": "handler.py"
}

`ingest` 管的是「这个技能出的图要不要自动进花型库」，默认 true（生图/改图/
描稿/四方连续的成品都进）。置 false 的技能（如 layer_psd）产物只落盘给用户
下载，不进库 —— 分层 PSD 交付的是一个文件，不是图。别用「产物长得像不像
花型」去猜，那是两回事。

入库前还要再过一道：core/atag.py 的自动打标签闸门（trace/seamless 的来源，
见 runtime.GATED_SOURCES）—— 没打过标不许存，和后台导入同一条规矩。

handler.py 只需定义 run(args, ctx) -> dict。ctx 含 patterns / trends / vocab / brief / session / config。
新增本地算法 = 在 skills/ 下建一个目录，服务重启即自动挂载，无需改主程序。
"""
import importlib.util
import json
import os
import traceback


class Skill:
    def __init__(self, directory, manifest):
        self.dir = directory
        self.name = manifest.get('name') or os.path.basename(directory)
        self.description = manifest.get('description', '')
        self.parameters = manifest.get('parameters') or {"type": "object", "properties": {}}
        self.confirm = bool(manifest.get('confirm'))
        self.cost = int(manifest.get('cost') or 0)
        # 产物要不要自动进花型库。默认进（生图/改图/描稿/四方连续的成品
        # 都是这个预期）；置 false 的（layer_psd）只落盘给用户下载 ——
        # 分层 PSD 交付的是一个文件，不是图。
        self.ingest = bool(manifest.get('ingest', True))
        self.version = manifest.get('version', '0.1.0')
        self._handler = os.path.join(directory, manifest.get('handler') or 'handler.py')
        self._module = None

    def _load(self):
        if self._module is None:
            spec = importlib.util.spec_from_file_location('skill_' + self.name, self._handler)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._module = mod
        return self._module

    def run(self, args, ctx):
        try:
            out = self._load().run(args or {}, ctx)
            if not isinstance(out, dict):
                out = {"result": out}
            return out
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-400:]}

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe(self):
        return {"name": self.name, "description": self.description,
                "version": self.version, "cost": self.cost, "confirm": self.confirm,
                "ingest": self.ingest}


class SkillRegistry:
    def __init__(self, root):
        self.root = root
        self.skills = {}
        self.load()

    def load(self):
        self.skills = {}
        if not os.path.isdir(self.root):
            return
        for name in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, name)
            mf = os.path.join(d, 'skill.json')
            if os.path.isdir(d) and os.path.exists(mf):
                try:
                    with open(mf, encoding='utf-8') as f:
                        s = Skill(d, json.load(f))
                    self.skills[s.name] = s
                except Exception as e:
                    print('[skills] 跳过 %s：%s' % (name, e))

    def get(self, name):
        return self.skills.get(name)

    def schemas(self, names):
        return [self.skills[n].schema() for n in names if n in self.skills]

    def run(self, name, args, ctx):
        s = self.get(name)
        if not s:
            return {"error": "未注册的技能：%s" % name}
        return s.run(args, ctx)

    def describe(self):
        return [s.describe() for s in self.skills.values()]
