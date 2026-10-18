# Obsidian 库

用户用 Obsidian 时，会议文件夹直接归档进库里，并用库里的笔记标题、别名和标签纠正识别错的人名和专有名词。`scripts/obsidian.py` 只在本机读取 Markdown，不上传，也不改动已有笔记。

## 设置（一次）

```bash
python3 <skill-dir>/scripts/obsidian.py find                     # 列出本机的库
python3 <skill-dir>/scripts/obsidian.py use --vault "/path/to/库" --folder 会议纪要
python3 <skill-dir>/scripts/obsidian.py status
```

用户提到 Obsidian 时设置；或者第一次整理会议时 `status` 显示还没选过（`configured` 为空）而 `find` 找到了库，就问一次“要把会议归档进 Obsidian 库吗”。用户不要就运行 `obsidian.py off`，以后不再问。

`find` 读取 Obsidian 自己记录的库列表，并查看 iCloud Obsidian 文件夹和“文稿”。只有一个库时直接告诉用户并问是否用它；多个时让用户选。`--folder` 是库里的相对路径，默认 `会议纪要`。设置后 `device_context.py init` 返回的 `archive` 就指向 `库/会议纪要`，之后的会议都归档到那里。库被移走或改名时自动退回默认归档位置，`status` 会提示。

## 归档进库的内容

每场一个 `YYYY-MM-DD_主题` 文件夹：

| 文件 | 内容 |
| --- | --- |
| `会议纪要.md` | 属性（日期、参会人、标签、别名）、概要、核心要点、Todo 复选框，嵌入纪要图片和录音 |
| `逐字稿.md` | 按说话人分段、带时间戳的转写，术语已按确认的写法修正 |
| `会议纪要.png` / `会议纪要.html` | 和不用 Obsidian 时一样 |
| 原录音 | 移动进来，在笔记里可以直接播放 |

参会人在库里有同名笔记（标题或别名）时写成 `[[链接]]`，人物笔记的反向链接就能看到所有相关会议；没有笔记的人保持纯文本，不新建笔记。会议笔记的别名是“日期 主题”，在库里搜索或链接时用它。

用户在 Obsidian 里改过的笔记会保留，重新生成的版本另存为 `会议纪要-更新版-xxxxxxxx.md`，告诉用户两份的区别，不覆盖。多台电脑同步同一个库时，每台电脑用自己的归档索引（`.archive-index-<设备>.json`），互不干扰。

## 用库纠正识别错误

**转写前，作为热词**：

```bash
python3 <skill-dir>/scripts/obsidian.py hotwords --out /path/to/hotwords.json
```

输出已确认术语加库里的人名、笔记标题（最多 300 个）。本地识别直接传给 `transcribe_local.py --hotwords`，不出电脑。阿里云转写时这些词会随录音发给阿里云：先告诉用户“会把库里的约 N 个人名和标题作为热词一起发给阿里云”，用户同意才用；不同意就只做转写后纠正。

**转写后，批量确认**：工作区导入后、写纪要前运行

```bash
python3 <skill-dir>/scripts/obsidian.py suggest --workspace /path/to/workspace --out /path/to/obsidian-candidates.json
python3 <skill-dir>/scripts/workspace.py candidates --workspace /path/to/workspace --file /path/to/obsidian-candidates.json --revision N
```

`suggest` 找出转写里和库中词语读音相同、写法不同的词（例如“背联”和笔记《贝联》），跳过库里本来就有的词和已确认的术语；本技能自己归档的会议笔记不当作词库，避免把旧错误带回来。候选为空就不提这一步。有候选时在一条消息里列出，默认都按库里的写法改：

```
在你的 Obsidian 库里找到 2 处可能识别错的词，默认按库里的写法修正：
1. 背联 → 贝联（2 处，笔记《贝联》）：“这周背联的上线节奏要定下来”
2. 王武 → 王五（1 处，人物笔记《王五》）：“王武那边下周给报价”
回复“都对”全部修正；也可以说“第 2 个不改”或“第 1 个只改第一处”。
```

每条给一句原话，让用户能判断。用户回复后写确认文件并一次应用：

```json
[{"candidate_id": "obsidian-…", "decision": "confirm"},
 {"candidate_id": "obsidian-…", "decision": "different"}]
```

```bash
python3 <skill-dir>/scripts/workspace.py confirm-terms --workspace /path/to/workspace --file /path/to/choices.json --revision N
```

`decision` 是 `confirm`（按库里写法改）、`different`（不是同一个词）或 `defer`（暂不确定）。`segments` 默认 `"all"`，也可以给段落编号只改其中几处；全部修正时会记住对应关系，下次自动给出候选，只改部分时不记。任何一条出错，整批都不生效，报出是第几条。短会议快速通道里，这份清单和认领问题放在同一条消息里。

用户没回应这份清单就继续写纪要时，不应用任何修正；读音相同不等于写错，仍需用户确认。
