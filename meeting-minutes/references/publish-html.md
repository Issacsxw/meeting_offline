# 可编辑 HTML 与图片交付

每场主要交付两个文件：简明 HTML 和其 PNG 图片。主题在上，时间与参会人置于主题下；概要总结整场方向，核心要点按议题展开，最后列主要 Todo。参会人只显示姓名，时间无依据时隐藏，编辑提示不进入正式内容。将完整逐人观点与引用留在底稿。只提供页面按钮而没有实际生成图片，不算完成双格式交付。

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

按现有 `workspace.py analysis --file ... --revision ...` 保存。`exports/latest.json.files.<source_id>.html` 指向新 HTML。复核服务的 `/minutes/<source_id>?token=...` 可直接打开；导出入口仍可下载独立文件。用户离线打开下载的 HTML 也可编辑与导图。

没有 `reader` 时，导出器保留完整已写观点与结论作为回退，不会自动压缩或删减。交付前仍需助手补齐 `reader`，确保简洁。

## 独立生成

已有可信的简明正文，也可以直接用渲染器：

```bash
python3 <skill-dir>/scripts/minutes_html.py --document /path/to/document.json --out /path/to/会议纪要.html
```

文档 JSON 的文本字段为 `topic`、`meeting_time`、`participants`，可选 `summary`；`participants` 只包含称呼。`points` 为含 `title`、`text` 的列表；`todos` 为含 `task`、`owner`、`deadline`、可选 `done` 的列表。所有值按文本处理，不插入可执行 HTML。无外部字体、图片、脚本或遥测依赖。

## 编辑、导图与验证

1. 用可用浏览器打开 HTML。直接点击文字编辑，可增删要点/Todo，也可勾选 Todo 完成状态。
2. “保存 HTML”下载当前内容的独立 HTML。浏览器不能直接改写原始磁盘文件，需打开新下载的文件继续编辑；验证重新打开后文字与完成状态保留。
3. “导出图片”读取当前页面内容，生成 2 倍清晰度 PNG 并展示预览；“下载 PNG”保存图片。图中不含编辑工具栏或增删按钮，长文本自动换行、按内容增高。
4. 交付前打开图片检查中文、长行、表格、末尾内容及负责人/期限；图与 HTML 应来自同一正文版本。用隔离样例验证编辑、保存与导图，不将测试内容写回真实会议或记忆库。
5. 按 [归档方法](archive.md) 将核对后的 HTML、PNG 放入该会议的录音文件夹。工作区模式需先保存 `publication.json`，记录当前 `exports/latest.json.version` 为 `export_version`，以及 `files.<source_id>.html/png` 的绝对 `path` 与 `sha256`，归档脚本据此检查版本和文件。用户编辑过的归档稿会保留，新生成版本另存。

HTML 中的编辑先形成用户修订稿，不自动改写原始转写、角色档案或术语库。用户明确纠正人物或术语时，再按已有确认流程积累。不要上传会议资料以完成截图或导图。
