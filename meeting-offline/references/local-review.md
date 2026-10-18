# 本地复核与可重建导出

本工作流使用 Python 标准库。原音频只保存路径与 SHA-256，不复制；原始转写保存为只读意义上的来源快照，用户修改另存为角色映射和术语补丁。导出会核对原始转写哈希。

本机页面用于辅助复核；每次会议的主交付始终是包含各角色观点、议题结论与 TODO 的纪要。用户完成选择后，助手读取复核结果、完善结构化 analysis 并交付更新后的纪要。词库和角色档案随之积累；它们的新增数量不代表纪要已完成，也不替代纪要正文。

对外主交付采用时间、主题、参会人、核心要点、主要 Todo 五项，生成可编辑 HTML 和 PNG；详细引文留在底稿。[可编辑 HTML 与图片交付](publish-html.md)说明如何填写精简正文、验证编辑与导出。

## 建立工作区

先用 `scripts/prepare_speakers.py` 准备 state，读完转写后填写各人的 focus、core_points 和 recognition_quotes。已有任务状态可直接沿用，不能清空已确认身份。

```bash
python3 <skill-dir>/scripts/workspace.py import \
  --state /path/to/meeting-state.json \
  --audio 4=/path/to/recording-4.m4a \
  --audio 5=/path/to/recording-5.m4a \
  --workspace <本机meetings目录>/<meeting-id>
```

`--audio` 可省略，但没有音频路径就不能回听。目录已存在时复用，不覆盖重建。不同文件可作为同批复核对象，导出仍分别保存，不推定它们同一场会议。

需要根据本地角色档案预填候选时，在相应说话人中写 `candidate_role: {profile_id, reason}`，并保留 `role_confirmed: false`。profile_id 必须来自当前相关的本地档案；reason 给可核对的内容依据。没有合理候选就保持空，不按首次发言顺序套人名。

## 打开复核页面

```bash
python3 <skill-dir>/scripts/review.py --workspace /path/to/workspace
```

命令输出带随机会话令牌的 `http://127.0.0.1:<port>/#token=...`。打开这个地址，用户可以看观点、原话、回听片段、选择或新建人物，明确保留未知身份，或填写并确认术语。不要把整个长期词库、录音或会话令牌分享出去。

页面仅绑定回环地址，流式读取原音频，支持 Range 定位；服务重启后需要使用新链接。它不是后台监控或自动录音服务。用户在页面点击确认属于其角色/术语确认依据；助手不能替用户点击一个未经用户提供的身份或标准词。已在对话确认过的内容可以直接同步。

术语选择卡片显示原文、相邻短对话、说话人、时间及回听入口。保存后仅修改勾选段落的校对层，不改原文，也不自动传播到其他会议。相同业务范围已有不同对应关系时拒绝覆盖。普通人名与产品名匹配仍要结合业务上下文。

## 术语选择与词库

助手阅读转写后，将疑似变体整理为小组。`forms` 必须确实出现于该录音原文；候选标准写法放在 `options`，注明来自原转写、相关已确认词库或语境推测。不要自行查一个近音品牌当成事实。示例使用虚构名称：

```json
[
  {
    "id": "product-name-review",
    "source_id": "4",
    "title": "主推产品名称",
    "category": "产品名",
    "scope": "示例团队的局改产品",
    "forms": ["示例普罗", "示例Pro"],
    "question": "这两个写法是否是同一产品，标准写法是哪一个？",
    "options": [
      {"text": "示例Pro", "basis": "原转写中出现，尚未确认"},
      {"text": "示例普罗", "basis": "原转写中出现，尚未确认"}
    ]
  }
]
```

```bash
python3 <skill-dir>/scripts/workspace.py candidates \
  --workspace /path/to/workspace --file /path/to/candidates.json --revision <当前revision>
```

它合并候选，不重置已有复核，也不修改长期词库。页面不预选标准词。每种写法会显示全部匹配原文，较多段落折叠展示；用户可取消某种写法或部分段落。确认前显示本次标准词、证据段落数、要记住的对应关系和适用范围。

- **确认标准词**：保存标准词，并只改用户勾选的段落；标准词等于原写法也可积累。原文和确认记录可追溯。
- **记住对应关系**：仅在该种写法的全部可用匹配段落均被选中时启用。部分段落选择会禁用此项，避免将同词异义推广到未来。后续仍按业务范围和语境使用，不能机械替换。
- **不同含义，不合并 / 暂不确定**：只记录本次复核结果；不新增词条、别名或替换。卡片可再次展开继续核对。

本地术语库在页面显示标准词、已确认对应关系和业务范围。未确认卡片独立保留。库中只留必要的段落引用，不塞入整段会议原文。手动纠正入口继续提供全文件精确匹配替换，适合已经明确的其他词。

## 形成结构化纪要

角色确认后，由助手阅读来源并生成 analysis JSON。不要声称脚本能自己理解整场会议。最小格式：

```json
{
  "sources": {
    "4": {
      "title": "本场会议主题",
      "meeting_date": null,
      "decisions": [
        {"text": "一项明确要求", "status": "direction", "proposer": "4-A", "evidence": [12, 13]}
      ],
      "actions": [
        {"task": "明确认领的下一步", "owner": "4-B", "owner_basis": "self_commitment", "deliverable": "交付物", "deadline_text": null, "evidence": [18]}
      ],
      "open_questions": [
        {"text": "尚未解决的问题", "evidence": [22]}
      ]
    }
  }
}
```

`evidence` 是该文件原始 `segments` 数组的零基索引。决策 status 只能取 direction（明确方向）、proposal（建议/倾向）、decided（确已决定）、open（未定）。没有明确认领或指派的任务将 owner 设为 null。已命名 owner 需要 owner_basis 为 self_commitment 或 explicit_assignment，并在证据中真正成立；不要只为通过结构检查而填写。

```bash
python3 <skill-dir>/scripts/workspace.py show --workspace /path/to/workspace
python3 <skill-dir>/scripts/workspace.py analysis \
  --workspace /path/to/workspace --file /path/to/analysis.json --revision <当前revision>
```

程序检查引用和人物标识存在，再从来源复制原话。它不能证明推断或责任归属在语义上正确，助手仍需检查否定、条件、提议与后续反转。未确认角色时不会导出完整议题和行动项。没有 analysis 时仅导出逐人观点，不能当作已经完成完整纪要。

## 重建与版本

```bash
python3 <skill-dir>/scripts/workspace.py export --workspace /path/to/workspace
```

角色/术语保存时自动重建：带角色转写、SRT、详细纪要底稿和结构化 JSON。角色已确认且有 analysis 时同时生成可编辑 HTML，并用本机浏览器无头导出同名 PNG（`files.<source_id>.png`；没有浏览器时记录 `png_error`，HTML 不受影响）；正文的精简程度由助手撰写的 `reader` 内容决定，脚本不自动总结。每条行动项带原始证据；角色归属由稳定说话人键解析，改名后执行者及要点归属随映射更新。原始转写、原话证据保持不变，校对文本保留 original_text。角色与术语变化不会触发 ASR、读取云端密钥或上传音频。正文不变时复用已有 PNG；正文变化时自动重新出图，图片与 HTML 始终同一版本。

历史状态在 `revisions/`，导出在 `exports/<内容版本>/`；`exports/latest.json` 指向最新结果。用户认为角色认错时，可重新选择正确角色并保存；先前快照和导出仍在。跨页面并发修改会拒绝过期 revision，需要刷新后重试。

## 为 Qwen 准备热词

只选择与这次会议有关、已获用户确认的标准词，不自动导出整个人物档案。

```bash
python3 <skill-dir>/scripts/workspace.py hotwords \
  --workspace /path/to/workspace --term "已确认的人名" --term "已确认产品名" \
  --weight 2 --out /path/to/hotwords.json
```

输出为标准词到权重的 JSON。交给当前转写工具支持的 `--hotwords` 参数，先 `--dry-run` 检查。短录音使用即时热词时，需显式选文件转写路线 `--filetrans`；不能悄悄切换收费或存储路线。上线参数和离线测试不代表已经验证识别效果；效果要在用户授权的后续录音上比较。
