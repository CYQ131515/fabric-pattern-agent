# 花型图 → PSD 分层（SAM 3.1 · RunningHub 云端工作流）

把一张平铺花型图拆成带中文命名的多层 PSD。

```
花型图 ──► 视觉模型(VLM) 看图 → 决定分几层、每层提示词
            │
            └─► 每层跑一次 RunningHub 上的 SAM3.1 工作流 → 该层透明底 PNG
                          │
                          └─► 底布（挖掉花型后按图案周期拼贴补全）
                                        │
                                        └─► 合成 PSD + 回读自检
```

## 依赖安装

现役把分割丢给云端（RunningHub 的 SAM 3.1），本地只管看图诊断、拼底布、写 PSD ——
**不需要 torch，也不用下载模型权重**，装 4 个包就够：

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

- `numpy` — 图像数组运算
- `pillow` — 读写 PNG、图层合成
- `psd-tools` — 写分层 PSD
- `scipy` — 底布补全的距离变换（用到底布补全时才触发）

> 早期方案是本地跑 sam2 蒙版精修，要 864 MB 权重 + 583 MB torch，那套已封进
> `_归档_旧算法/`。要复现就装 `requirements-legacy.txt`，权重从
> https://hf-mirror.com/facebook/sam2.1-hiera-large/resolve/main/sam2.1_hiera_large.pt
> 下载到 `models/sam2/` 。

## 快速开始

```bash
export RUNNINGHUB_API_KEY=你的key      # RunningHub 控制台右上角
export DEEPSEEK_API_KEY=sk-xxx         # 分层诊断用（不给就跳过诊断）

python run.py -i 花型.jpg -o out/
```

默认走 **DeepSeek 官网**：`https://api.deepseek.com/v1` + `deepseek-flash`。
注意模型 id 是 **`deepseek-flash`**（显示名 DeepSeek-V4.1-Flash），它的
`input_modalities` 含 `image`，是真能看图的；`deepseek-v4-pro` 反而只有 `text`。
实测 508×500 的花型图约 **2 秒**出分层方案。

诊断用的视觉模型是**可插拔**的，任意 OpenAI 兼容的 `/chat/completions` 都行：

```bash
# 先看哪些网关现在能连上
python -m rhpsd.analyze

# 用预设（deepseek / dashscope / bigmodel / siliconflow / moonshot / volc / zai）
python run.py -i 花型.jpg --vlm-provider dashscope --vlm-key sk-xxx

# 或者任意兼容地址
python run.py -i 花型.jpg --vlm-base-url https://xxx/v1 \
                          --vlm-model 某视觉模型 --vlm-key sk-xxx
```

> `api.apimart.ai`（原来借它调 deepseek 的第三方网关）**已不可达**：代理
> CONNECT 直接 502，直连 443 端口关闭，6 分钟 12 次探测全部失败。
> 现在直接走 DeepSeek 官网，不再经过第三方中转。

## 网络出口：强制直连，不走代理 ⚠️

**这个算法的所有出网请求都绕开代理**，无论电脑开不开 VPN。

原因很实在：RunningHub 和视觉网关**走代理一律网络错误**。而这台开发机上
就装着一个本地代理（`HTTP_PROXY=http://127.0.0.1:1082/`），`urllib` 会
自动读它 —— 报出来的错还特别像"key 不对""网关挂了"，能白查半天。

两条线一起收紧：

| 位置 | 做法 |
|---|---|
| `rhpsd/net.py` | 所有请求走 `net.urlopen()`：`ProxyHandler({})` 关掉一切代理，并做 IPv4 优先 |
| `后端/core/psdlayer.py` | 起子进程时把 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（大小写）从环境里摘掉 |

**别改回裸 `urllib.request.urlopen`** —— 那会自己读环境变量和 macOS 的
系统代理设置（`scutil --proxy`），代理一开就 Connection refused。

自检（不花钱、不跑云端任务）：

```bash
python -m rhpsd.net --check      # 看代理现状 + 真连一次 RunningHub
python -m rhpsd.net --selftest   # 现场植入死代理做两组对照，证明请求不受影响
```

`--selftest` 会做两组对照（都只改本进程内存，不动系统设置）：

```
=== 对照一：环境变量里的代理（植入 http://127.0.0.1:1/）===
  ① 老写法（读环境代理）：不通  URLError: <urlopen error [Errno 61] Connection refused>（0.00s）
  ② 本模块出口（强制直连）：通  HTTP 200，0.45s

=== 对照二：macOS 系统设置里的代理（模拟成 http://127.0.0.1:1/）===
  ① 老写法（读系统代理）：不通  URLError: <urlopen error [Errno 61] Connection refused>（0.01s）
  ② 本模块出口（强制直连）：通  HTTP 200，0.34s
➜ 结论：代理确实会拦死请求，而本模块两条路都不受影响 —— 直连生效 ✓
```

**对照二为什么必须做**：VPN 客户端（ClashX / Surge 之类）改的是 macOS
**系统网络设置**，不是环境变量 —— 只测环境变量会漏掉最常见的那种情况。

> 顺带一个 urllib 的隐蔽行为，排错时很容易被它骗：
> `getproxies()` 的实现是 `getproxies_environment() or getproxies_macosx_sysconf()`。
> 而 `getproxies_environment()` 会把 **`NO_PROXY` 也收进去**，所以哪怕只写了
> `NO_PROXY=localhost`，返回的字典也是**非空的（真值）**，`or` 直接短路，
> **系统代理那条路永远走不到**。所以"明明开了系统代理，脚本却像没看见"
> 是正常的 —— 环境里有个 `NO_PROXY` 就够。
> 本模块不受影响：它压根不调 `getproxies()`。

顺带做了 **IPv4 优先**（同 `算法/四方连续`）：结果图 CDN 常把 AAAA 排在 A
前面，而 urllib 没有 Happy Eyeballs，会老实连第一个；某些网络下 IPv6 是
"连得上但极慢"，不报错、状态码也正常，只看通不通发现不了。只排序不禁用，
IPv4 全失败仍会退到 IPv6。

手动指定层（不诊断）：

```bash
python run.py -i 花型.jpg -o out/ --no-analyze \
    --layer "主体花=花朵:24" --layer "叶片=叶子:20"
```

产出：

| 文件 | 内容 |
|---|---|
| `分层.psd` | 最终交付，打开就是分层文件，中文图层名 |
| `layers/NN_名字.png` | 每层单独的 RGBA PNG |
| `composite.png` | 合成结果（**和原图逐像素相同**） |
| `ground.png` | 补好的底布 |
| `report.json` | 每层提示词、面积、指标、日志 |

## 为什么这个方案是无损的

**SAM3.1 是分割模型，不重画像素。** 它只回答"哪些像素属于这个概念"，
输出的 RGB 就是原图那几个像素。

所以每层内容直接取原图，于是无论 alpha 长什么样：

```
合成 ≡ 原图
```

**恒等式由构造直接成立**，不需要任何收敛修复，也不需要模型去"想象"
被遮挡的内容。实测 `stack_max_diff = 0.0` 灰阶、PSD 回读 `0.0` 灰阶。

底布层挖掉的洞用**图案自身的重复周期**去借真实纹理填（这是平铺图），
借不满的残余用最近邻扩散兜底。洞里的像素在任何图层组合下都不会露出来，
所以补得再随意也不影响合成；而关掉花型层时，看到的是带真实纹理的底布，
不是一团糊。

## 工作流节点（`2109041802081820673`）

| 节点 | 类型 | 作用 |
|---|---|---|
| 431 | LoadImage | **图片输入**，字段 `image` |
| 434 | CR Text | **文本输入**，字段 `text` |
| 435 | RH_Translator | force_english，所以中文提示词也能用 |
| 430 | SAM3_Detect | threshold 0.5 / refine 2 / individual_masks False |
| **432** | SaveImage | **目标层**（透明底）← 要这个 |
| 440 | SaveImage | 抠掉目标后剩下的背景 |

⚠️ **432 是目标层，440 才是反相** —— 和节点的视觉位置是反的。
`SAM3_Detect` 吐出来的原始蒙版本身是反的，经过 433「Mask Invert」之后
才变成"目标 = 不透明"。别按命名猜，`tools/probe_repeat.py` 的覆盖率才是准的。

## 提示词规范（由 deepseek 自动填）

- 多个对象用**英文逗号**分隔，中文逗号无效
- 用**英文冒号 + 数字**指定数量：`花朵:12`
- 可加描述性词定位：`中间的瓶子:2`
- 单次标记总数 ≤ 32，但**别顶格**：实测 `花朵:32` 容易退化成"整幅全选"，
  建议 8~24

## 实测踩出来的坑

1. **`/task/openapi/status` 的 `data` 是纯字符串**（`"RUNNING"`），
   不是包了 status 字段的对象 —— 按对象解直接 `AttributeError`。
2. **结果 URL 约 24 小时过期**，拿到必须立刻落盘。
3. **443 认证两处都要给**：body 的 `apiKey` ＋ 头 `Authorization: Bearer`。
4. **`nodeInfoList` 里值是 `[...]` 的字段是连线**，不能通过 API 改。
   （例外：实测硬改节点 436 的 `text` 也能生效，可以绕过翻译节点。）
5. **SAM3.1 同一个提示词逐次结果会抖**。同一张图上 `花朵:24` 出现过
   30.2% 和 100% 两种结果。所以 `pipeline.py` 有退化判据
   （>85% 视为"整幅全选"，<0.4% 视为"几乎为空"）+ 同提示词重试。
6. **重试要防止层间重复**。第 3 层所有提示词都退化时，最后退到默认提示词，
   结果和「主体花」一模一样（都是 30.2%），PSD 里凭空多一个重复图层。
   现在有 IoU 闸门（>0.6 判重），判重就继续换提示词，换不出来就**跳过这层**。
7. **`str.format()` 会让 JSON 示例里的花括号炸掉**。`analyze.py` 的 SYSTEM
   里有 `{"layers":[...]}`，用 `.format(max_layers=...)` 会
   `KeyError: '"layers"'`。改用占位符替换。

## 代码结构

```
run.py              命令行入口
rhpsd/
  client.py         RunningHub 接口客户端（纯标准库，手工拼 multipart）
  analyze.py        视觉模型看图 → 分层规划 + SAM3 提示词（网关可插拔 + 探活）
  pipeline.py       编排：上传 → 规划 → 逐层 → 底布 → 组装
  fill.py           底布补全：按图案周期拼贴 + 最近邻扩散兜底
  psd.py            写 PSD（utf-8）+ 回读自检
tools/
  probe_text.py     批量试提示词写法，看哪种真检出东西
  probe_repeat.py   测提示词可重复性 + 绕翻译直喂 436
```

## 已接入 PatternFlow 产品（织印师「分层 PSD」）

产品侧不 import 这里的代码，走**子进程**调 `rhpsd.service`（主程序刻意零依赖）：

```
织印师 → skills/layer_psd → 后端/core/psdlayer.py → 本文件 rhpsd/service.py
```

| 子命令 | 作用 | 代价 |
|---|---|---|
| `plan` | 只调视觉模型看图，输出"打算分几层" | 3 秒、几分钱、**不跑工作流** |
| `run` | 完整执行，产出 PSD + 合成预览 + 每层 PNG | 1~2 分钟 |

`plan` 存在的意义是让产品能**先提案后执行**：先把分层方案给用户看，
他点确认才扣 45 点。改动这两个子命令的**入参和返回字段名要向后兼容**，
否则产品那边会断。

## 已知边界

- **底布不是真底布**。它是从图案别处借来的纹理，不是"本来就该在那里的"
  底布。被花型盖住的地方本来就无从得知，这是信息层面的上限。
- **层数不在多**。语义上分不开的层，硬分只会得到两个高度重叠的图层。
  1~3 层通常比 4~5 层更有用。
- **视觉模型诊断可插拔**。跑之前先探活，网关连不上会直接报清楚并列出
  当前可用的网关，不会白花 RunningHub 的任务费。诊断失败时退回默认
  单层，`report.json` 的 log 里会写明原因。
- 成本 = 1 次诊断 + 每层 1 次工作流调用（退化重试会多花）。单次任务约 10~15 秒。
