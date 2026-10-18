# 可编辑 HTML 与图片交付

每场主要交付两个文件：简明 HTML 和其 PNG 图片。PNG 默认由脚本直接生成，不依赖用户点击。主题在上，时间与参会人置于主题下；概要总结整场方向，核心要点按议题展开，最后列主要 Todo。参会人和负责人只显示已确认的姓名或称呼（不带岗位），时间无依据时隐藏，编辑提示不进入正式内容。将完整逐人观点与引用留在底稿。只提供页面按钮而没有实际生成图片，不算完成双格式交付。

默认版式为用户已确认的暖白底、陶土色点缀、宋体标题与浅灰分隔线。Figma 母版只维护视觉，普通会议使用本地模板直接生成；无需将真实会议内容发送到 Figma。

## 样式

纪要外观由两部分自由组合：**版式**决定排版结构，**配色**决定颜色和标题字体。

| 版式 | 名称 | 样子 | 适合 |
| --- | --- | --- | --- |
| `classic` | 经典（默认） | 大号议题编号、表格 Todo | 日常团队纪要 |
| `official` | 公文 | 居中红头、双线、“一、（一）”编号、全框表格 | 正式会议、报送领导 |
| `cards` | 卡片 | 色块标题栏、两列要点卡片、Todo 卡片 | 发群、手机上看 |
| `timeline` | 时间轴 | 议题沿时间轴展开，Todo 作为“下一步” | 项目推进、复盘 |
| `checklist` | 极简清单 | 只有文字、圆点和复选框，图片更窄 | 快速同步、短会 |

| 配色 | 名称 | 适合 |
| --- | --- | --- |
| `warm` | 暖白陶土（默认） | 日常团队纪要 |
| `business` | 商务蓝 | 发给客户或领导 |
| `mono` | 极简黑白 | 打印、存档 |
| `fresh` | 清新绿 | 轻松的内部会议 |
| `dark` | 深色 | 屏幕阅读、个人偏好 |

- 用户在 HTML 工具栏的“样式”（配色）和“版式”菜单里随时切换；保存 HTML 会记住所选组合，导出图片按当前组合生成。
- 用户说出想要的样式时（如“用公文版式”“卡片 + 商务蓝”），独立生成用 `minutes_html.py --layout official --theme business`；工作区模式把 `layout`、`theme` 写进初始化返回的 `preferences.json` 的 `meeting_offline` 下（如 `{"meeting_offline": {"layout": "official", "theme": "business"}}`），之后的纪要都用它，再重新 `workspace.py export`。只改这一份时不写偏好，直接对已生成的 HTML 切换后保存，再用 `render_png.py` 出图。
- 用户只说“换个样式”时，简短列出版式和配色让用户选，不替用户决定。
- 不要为了换样式重写正文；版式和配色只影响外观。

## 在工作区形成正文

先按来源阅读并核实 analysis。给相应 `analysis.sources.<source_id>` 添加 `reader`，由助手写精简内容而非机械截取长转写。下面为虚构示例：

```json
{
  "reader": {
    "topic": "主推产品与素材协作",
    "summary": "围绕产品资料交付和投放协作明确分工，先推进已确认内容，价格与参数补齐后再使用。",
    "summary_evidence": [12, 13],
    "points": [
      {
        "title": "素材范围",
        "text": "先提交已确认的产品事实；未定价格与参数保留待核。",
        "attribution": ["4-A"],
        "evidence": [12, 13],
        "status": "direction"
      }
    ],
    "todos": [
      {"action_index": 0, "task": "交付已确认的产品资料与卖点。"}
    ]
  }
}
```

- `summary` 概括整场目的、推进方向和关键约束；`summary_evidence` 保留所依据段落。`points` 按议题综合多人对话，不按人名逐条罗列。
- `status` 必填，表明这条议题定到了哪一步：`decided`（已决定：会上明确拍板）、`direction`（推进方向：大方向一致、细节未定）、`proposal`（建议：有人提出，没有结论）、`open`（讨论中：还在争论或待定）。纪要里每条要点旁会显示这个标签，用户点标签可以改。拿不准时往低处标，不把“考虑一下”写成已决定。
- `attribution` 是同一来源的稳定说话人键，留在底稿用于追溯，不自动追加到议题标题。客观未决事项可用空列表；不同人的立场不能合成共识，必要归属由助手写入正文。
- `evidence` 是原始转写零基段落索引，只留在底稿，不在正文堆砌原话。程序检查索引与说话人存在，语义仍需助手核实。
- `todos` 按 `action_index` 缩短已有行动项的任务文字；不增加或覆盖负责人。默认保留全部 analysis 行动项，避免因取前几条漏掉关键任务。必要时可给 `deadline` 提供忠实的精简表述，不能改变期限或把未知日期换成当前日期。
- 时间取有依据的 `meeting_time` 或 `meeting_date`；均未提供时传空字符串并隐藏时间行。用户可用“会议信息”补填。不要猜日期，也不在正文加日期待确认之类的说明。

按现有 `workspace.py analysis --file ... --revision ...` 保存。`exports/latest.json.files.<source_id>.html` 指向新 HTML，`.png` 指向同时生成的图片。复核服务的 `/minutes/<source_id>?token=...` 可直接打开；导出入口仍可下载独立文件。用户离线打开下载的 HTML 也可编辑与导图。

没有 `reader` 时，导出器保留完整已写观点与结论作为回退，不会自动压缩或删减。交付前仍需助手补齐 `reader`，确保简洁。

### 显示名偏好

如需把某个已确认称呼在纪要里显示成别的写法（例如用户希望自己显示为“我”），由用户明确提出后写入初始化返回的 `preferences.json`：

```json
{"meeting_offline": {"display_names": {"用户本人": "我"}}}
```

键是角色库里的姓名或称呼，值是纪要中的显示名。它只影响 HTML/PNG 的参会人和负责人，不改角色库、转写或底稿；默认为空。修改后重新 `workspace.py export`。

## 独立生成

已有可信的简明正文，也可以直接用渲染器：

```bash
python3 <skill-dir>/scripts/minutes_html.py --document /path/to/document.json --out /path/to/会议纪要.html
```

默认同时生成同名 `会议纪要.png`；只要 HTML 时加 `--no-png`。

文档 JSON 的文本字段为 `topic`、`meeting_time`、`participants`，可选 `summary`；`participants` 只包含称呼。`points` 为含 `title`、`text` 的列表；`todos` 为含 `task`、`owner`、`deadline`、可选 `done` 的列表。所有值按文本处理，不插入可执行 HTML。无外部字体、图片、脚本或遥测依赖。

## 直接生成 PNG

```bash
python3 <skill-dir>/scripts/render_png.py --html /path/to/会议纪要.html [--out /path/to/会议纪要.png]
```

脚本把 HTML 交给本机 Chrome/Edge/Chromium 无头运行，调用页面自己的“导出图片”逻辑（`makePng`），所以和用户点按钮得到的图完全一致：920 宽（极简清单 720 宽）、2 倍清晰度、不含编辑工具栏。浏览器用临时配置目录，并关闭后台联网，不上传任何内容。找不到浏览器时可设 `MEETING_OFFLINE_BROWSER` 或 `--browser`；仍不可用就退回让用户在页面里点“导出图片”。内容过长（超出图片高度上限）会直接报错，需要精简或拆分正文。

## 用户修改后出新图

1. 用户在浏览器打开 HTML，点击文字编辑、增删要点/Todo、勾选完成状态。
2. “保存 HTML”下载一份包含修改的新 HTML（浏览器不能改写原文件），之后打开新文件继续改。
3. 出新图有两种方式，结果相同：
   - 用户在页面点“导出图片”→“下载 PNG”；
   - 用户把修改版 HTML 交给助手，助手运行 `render_png.py --html <修改版.html>`，需要放回会议文件夹时用 `--out <会议文件夹>/会议纪要.png`。
4. 不要因为用户改了 HTML 就从 analysis 重新生成正文覆盖用户改稿；改稿以用户保存的 HTML 为准。

## 内容核对（发布前必做）

```bash
python3 <skill-dir>/scripts/workspace.py audit --workspace <ws>
```

它把纪要正文（概要、要点、Todo 的事项和期限、决定）逐条和引用的原话对照，列出对不上的地方：

- **数字、金额、比例、日期、星期、时间说法**在原话里找不到。中文数字会换算后再比（“十六号”能对上“10 月 16 日”），但“周五”对不上“下周五”。
- **人名**不在原话里，也不是这条的发言人或负责人。
- **否定**对不上：原话“不打折”，纪要写成“打折”；或者纪要写了否定，原话没有。
- **状态**：标为已决定，原话却是“考虑/建议/再看看”；或者要点没写状态。

逐条处理，每条都要有结果：

1. 写错了就改 analysis，重新 `workspace.py analysis` 保存；改对的问题会自动消失。
2. 原话确实支持、只是说法不同（例如原话“二百二十九”，纪要“229 元”），登记 `supported`，`note` 写明依据。
3. 拿不准的问用户，用户确认后登记 `user_confirmed`，`note` 写用户怎么说的。不能替用户登记。

```json
{"<问题 id>": {"resolution": "user_confirmed", "note": "用户确认期限就是下周五"}}
```

```bash
python3 <skill-dir>/scripts/workspace.py audit --workspace <ws> --resolve resolutions.json --revision N
```

没有问题，或者每条都处理过，才算核对完。`publish` 会拒绝没核对完的纪要；之后再改角色、术语或正文，要重新运行一次 `audit`（已登记过、仍然适用的处理结果会保留，再用 `--resolve` 提交一个空的 `{}` 即可）。这是规则检查，能拦住数字和否定写错这类硬伤，不能代替通读：交付前仍要自己读一遍，确认结论和原话意思一致。

## 验证与归档

1. 交付前打开 PNG 检查中文、长行、表格、末尾内容及负责人/期限；PNG 与 HTML 来自同一版本。
2. 内容核对完成后运行 `workspace.py publish --workspace <ws>`，它把最新导出的 HTML/PNG 路径与 sha256 写入 `publication.json`。没有浏览器时，用户在页面导出后用 `--png <source_id>=<路径>` 指定。
3. 按 [归档方法](archive.md) 将 HTML、PNG 放入该会议的录音文件夹。用户编辑过的归档稿会保留，新生成版本另存。

HTML 中的编辑先形成用户修订稿，不自动改写原始转写、角色档案或术语库。用户明确纠正人物或术语时，再按已有确认流程积累。不要上传会议资料以完成截图或导图。
