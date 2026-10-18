# 深度洞察

给用户本人看的分析：每位已确认的参会人这场想要什么、背后可能的考虑、会上立场怎么变的、和他之前的会比有什么不同。结果是单独的 `深度洞察.md`，不进纪要正文、HTML、PNG 或 `会议纪要.md`，也不写进长期库。

## 什么时候做

- 用户要求时，例如“深度洞察”“分析一下每个人的想法”“他们背后怎么考虑的”。
- 用户说过“以后每场都做”时，把 `{"meeting_offline": {"insights": true}}` 写进初始化返回的 `preferences.json`，之后每场纪要交付后自动做。
- 其他时候，交付纪要的回复末尾可以用一句话提一次：“需要的话，我可以再写一份只给你看的深度洞察。”不追问。

纪要先交付，洞察不拖慢纪要。身份未确认、只保留为“身份未确认”的说话人不分析。

## 步骤

```bash
python3 <skill-dir>/scripts/insights.py history --workspace /path/to/workspace --out /path/to/history.json
python3 <skill-dir>/scripts/insights.py save --workspace /path/to/workspace --file /path/to/insights.json --revision N
```

1. `history` 列出每位已确认参会人在本机之前的会议（同一个用户确认的身份，最近 5 场）：主题、日期、归属他的要点、他负责的事项、上次的洞察和他的原话（带段落编号）。没有就是没有，不要说“首次参会”，只能说“本机没有他之前的会议记录”。
2. 通读本场完整转写，按人整理，不只看观点卡片。
3. 写 insights JSON（格式见下）后 `save`。它逐条检查依据，生成 `深度洞察.md`，之后 `workspace.py publish` 和归档会把它一起放进会议文件夹（设置了 Obsidian 库时就在库里，带 `私密` 标签）。
4. 交付时只把文件给用户，说明一句：这份只给你看，没有放进纪要和图片，转发会议文件夹前删掉它。

之后若改了谁是谁，旧洞察自动失效（导出里出现 `insight_error`），需要重写。

## 格式

```json
{"sources": {"4": {
  "people": [{
    "speaker": "4-A",
    "focus": [{"text": "希望守住 229 元首发价，可以接受有期限的降价。", "evidence": [3, 18]}],
    "considerations": [{"text": "可能担心降价后难以恢复原价、影响渠道。", "basis": "inference", "confidence": "medium", "evidence": [3]}],
    "shifts": [{"text": "从坚持 229 元转为接受 30 天限时 199 元。", "basis": "fact", "evidence": [3, 18]}],
    "history": [{"text": "上次明确反对打折，这次让步更大。", "basis": "inference", "confidence": "low", "evidence": [18],
                 "past": [{"meeting": "<history 里的 meeting>", "source_id": "4", "segment": 0}]}]
  }],
  "dynamics": [{"text": "价格分歧通过限时降价找到折中。", "basis": "inference", "confidence": "high", "evidence": [7, 18], "speakers": ["4-A", "4-B"]}]
}}}
```

| 部分 | 写什么 | 依据 |
| --- | --- | --- |
| `focus` 本场诉求（必填） | 他在会上明确要的东西、坚持的条件 | 事实，引用本人发言 |
| `considerations` 背后考虑 | 可能的利益、约束、顾虑，所代表的团队或岗位立场 | 一律是推断，标把握程度；有别的可能就写出来 |
| `shifts` 场内变化 | 前后发言里立场、条件、语气的变化，以及是什么让他变了 | 本人前后至少两段发言 |
| `history` 跨会议变化 | 和之前会议比：立场、关注点、说过要做的事 | 本场发言 + `past` 引用之前会议里他本人的原话 |
| `dynamics` 整体（可选） | 几个人之间的分歧、联盟、谁在推动 | 引用相关几个人的发言 |

`basis` 是 `fact`（会上说过的）或 `inference`（推断）；推断要有 `confidence`：`high`、`medium`、`low`。`evidence` 是本场转写段落编号，`focus`、`considerations`、`shifts`、`history` 的依据里至少一段是本人说的。

## 写法要求

- 只分析工作上的立场、利益、约束和决策方式。不评价性格、能力、人品、情绪状态或健康，不涉及家庭、年龄、性别、民族、宗教、政治等私人情况；`save` 会拒绝明显越界的词，但这不能代替判断。
- 不揣测恶意（如“想抢功”“故意拖延”）；会上没有依据的动机不写。
- 推断写成“可能”“看起来”，不写成定论；把握不足就不写，不凑满四部分。发言很少的人只写本场诉求。
- 之前会议的洞察本身也是推断，只能作为线索，不能当成事实再往上推。
- 不给操控、施压某人的建议；需要时可以写下次会议值得先确认的问题，放在对应条目的文字里。
- 原话只引用依据需要的那一句，不大段复制转写。
