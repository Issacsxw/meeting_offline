# 可编辑 HTML 与图片交付

每场主要交付两个文件：简明 HTML 和其 PNG 图片。PNG 默认由脚本直接生成，不依赖用户点击。主题在上，时间与参会人置于主题下；概要总结整场方向，核心要点按议题展开，最后列主要 Todo。参会人和负责人只显示已确认的姓名或称呼（不带岗位），时间无依据时隐藏，编辑提示不进入正式内容。将完整逐人观点与引用留在底稿。只提供页面按钮而没有实际生成图片，不算完成双格式交付。

默认版式为用户已确认的暖白底、陶土色点缀、宋体标题与浅灰分隔线；不使用深色大块区域。Figma 母版只维护视觉，普通会议使用本地模板直接生成；无需将真实会议内容发送到 Figma。

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
        "evidence": [12, 13]
      }
    ],
    "todos": [
      {"action_index": 0, "task": "交付已确认的产品资料与卖点。"}
    ]
  }
}
```

- `summary` 概括整场目的、推进方向和关键约束；`summary_evidence` 保留所依据段落。`points` 按议题综合多人对话，不按人名逐条罗列。
- `attribution` 是同一来源的稳定说话人键，留在底稿用于追溯，不自动追加到议题标题。客观未决事项可用空列表；不同人的立场不能合成共识，必要归属由助手写入正文。
- `evidence` 是原始转写零基段落索引，只留在底稿，不在正文堆砌原话。程序检查索引与说话人存在，语义仍需助手核实。
- `todos` 按 `action_index` 缩短已有行动项的任务文字；不增加或覆盖负责人。默认保留全部 analysis 行动项，避免因取前几条漏掉关键任务。必要时可给 `deadline` 提供忠实的精简表述，不能改变期限或把未知日期换成当前日期。
- 时间取有依据的 `meeting_time` 或 `meeting_date`；均未提供时传空字符串并隐藏时间行。用户可用“会议信息”补填。不要猜日期，也不在正文加日期待确认之类的说明。

按现有 `workspace.py analysis --file ... --revision ...` 保存。`exports/latest.json.files.<source_id>.html` 指向新 HTML，`.png` 指向同时生成的图片。复核服务的 `/minutes/<source_id>?token=...` 可直接打开；导出入口仍可下载独立文件。用户离线打开下载的 HTML 也可编辑与导图。

没有 `reader` 时，导出器保留完整已写观点与结论作为回退，不会自动压缩或删减。交付前仍需助手补齐 `reader`，确保简洁。

### 显示名偏好

如需把某个已确认称呼在纪要里显示成别的写法（例如用户希望自己显示为“我”），由用户明确提出后写入初始化返回的 `preferences.json`：

```json
{"meeting_minutes": {"display_names": {"用户本人": "我"}}}
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

脚本把 HTML 交给本机 Chrome/Edge/Chromium 无头运行，调用页面自己的“导出图片”逻辑（`makePng`），所以和用户点按钮得到的图完全一致：920 宽、2 倍清晰度、不含编辑工具栏。浏览器用临时配置目录，并关闭后台联网，不上传任何内容。找不到浏览器时可设 `MEETING_MINUTES_BROWSER` 或 `--browser`；仍不可用就退回让用户在页面里点“导出图片”。内容过长（超出图片高度上限）会直接报错，需要精简或拆分正文。

## 用户修改后出新图

1. 用户在浏览器打开 HTML，点击文字编辑、增删要点/Todo、勾选完成状态。
2. “保存 HTML”下载一份包含修改的新 HTML（浏览器不能改写原文件），之后打开新文件继续改。
3. 出新图有两种方式，结果相同：
   - 用户在页面点“导出图片”→“下载 PNG”；
   - 用户把修改版 HTML 交给助手，助手运行 `render_png.py --html <修改版.html>`，需要放回会议文件夹时用 `--out <会议文件夹>/会议纪要.png`。
4. 不要因为用户改了 HTML 就从 analysis 重新生成正文覆盖用户改稿；改稿以用户保存的 HTML 为准。

## 验证与归档

1. 交付前打开 PNG 检查中文、长行、表格、末尾内容及负责人/期限；PNG 与 HTML 来自同一版本。
2. 工作区模式检查后运行 `workspace.py publish --workspace <ws>`，它把最新导出的 HTML/PNG 路径与 sha256 写入 `publication.json`。没有浏览器时，用户在页面导出后用 `--png <source_id>=<路径>` 指定。
3. 按 [归档方法](archive.md) 将 HTML、PNG 放入该会议的录音文件夹。用户编辑过的归档稿会保留，新生成版本另存。

HTML 中的编辑先形成用户修订稿，不自动改写原始转写、角色档案或术语库。用户明确纠正人物或术语时，再按已有确认流程积累。不要上传会议资料以完成截图或导图。
