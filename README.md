# clean-code · 整洁代码技能

把 Robert C. Martin《代码整洁之道》(Clean Code) 变成 AI 编码代理**可以执行**的规则：写新代码、做 code review、重构遗留代码时自动生效。

这不是一段提示词，而是一个完整的 [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)：`SKILL.md` 负责决策流程，`references/` 提供逐章规则与 86 条坏味道清单，`scripts/smell_scan.py` 提供可运行的机械线索扫描器。

**核心立场**：代码首先是写给人读的。判断标准是「下一个改它的人能不能快速看懂、放心修改」——而不是主观审美。所以这个技能同样明确写了**什么时候不该套用这些规则**（一次性脚本、语言惯用法、性能关键路径、生成代码……）。

---

## 它能做什么

技能会先判断你处于哪种场景，再走对应流程：

| 你的意图 | 代理的行为 | 加载的参考 |
|---|---|---|
| 写新功能 / 新代码 | 先读周围代码保持项目一致，命名先于实现，不做假想抽象，写完自检 | `references/principles.md` |
| 代码审查 / PR review | 通读上下文 → 扫描器取线索 → 输出**分级**问题清单（Blocker/Major/Minor/Nit），每条含位置、危害、最小改法 | `references/smells.md` |
| 重构既有代码 | 先建安全网（特征测试）→ 一次一种手法 → 每步验证 → **行为不变**，不与功能变更混合 | `references/refactoring-playbook.md` |
| 只是问"这段代码怎么样" | 只讲最痛的 3–5 个问题，而不是倒出 86 条清单 | `references/smells.md` |

覆盖范围：命名、函数、注释、格式、对象与数据结构、错误处理、边界、单元测试、类、系统、简单设计四原则、并发，以及第 17 章的坏味道与启发清单。

---

## 安装

本仓库根目录**就是**技能目录（`SKILL.md` 在第一层），所以直接克隆到你的技能目录即可：

```bash
# 通用（SKILL.md 约定，例如 DeepSeek Harness）
git clone https://github.com/<你的用户名>/clean-code-skill.git ~/.agents/skills/clean-code

# Claude Code
git clone https://github.com/<你的用户名>/clean-code-skill.git ~/.claude/skills/clean-code
```

只要你的工具支持 `SKILL.md` 约定，把它放到任意技能目录下都能被识别；目录名建议保持 `clean-code`，与 frontmatter 里的 `name` 一致。

无需安装依赖：正文是 Markdown，扫描器只用 Python 3.8+ 标准库。

---

## 目录结构

```
clean-code/
├── SKILL.md                          # 主文件：模式判断、底线规则速查、流程与输出模板
├── README.md
├── LICENSE                           # MIT
├── references/
│   ├── principles.md                 # 第 1–13 章逐章规则、判定标准与反例
│   ├── smells.md                     # 第 17 章坏味道清单（8 类 / 86 条，含严重度与改法）
│   └── refactoring-playbook.md       # 安全网、味道→手法映射、22 种手法、遗留代码策略
├── scripts/
│   └── smell_scan.py                 # 语言无关的启发式坏味道扫描器
└── evals/
    └── evals.json                    # 3 个真实场景的测试用例与 22 条断言
```

代理实际只需要加载 `SKILL.md` 与它指向的文件，README / LICENSE / evals 对运行没有影响。

渐进式加载：`SKILL.md` 只有 155 行，细节都在 `references/` 里按需读取，不会一次性塞满上下文。

---

## 使用示例

### 1. 代码审查

> 帮我看看 `legacy_report.py`，这是我从上一个项目接手的老代码，下个月要在上面加功能，我有点担心。

代理会先通读文件、跑一遍扫描器、必要时写复现脚本验证可疑行为，然后输出分级报告（以下为真实输出的节选）：

```markdown
## 总体判断
这个文件只有 43 行，但浓缩了遗留代码的典型病：一个静默吞掉异常、会丢数据的 save()，
一个会在字段缺失时崩溃、还会重复计数的 process_order()……

### [Blocker] save() 裸 except + pass：写库失败被完全静默吞掉
- 位置：`_scan_test/legacy_report.py:16-20`
- 坏味道：吞掉异常（空 except 分支）+ 裸 except
- 为什么有害：保存失败时调用方收到的信号和成功时完全一样……已复现：传入必然抛 IOError
  的 db，save() 返回 None，无异常、无日志。
- 建议改法：<最小改动代码片段>

## 建议的修改顺序
1. 先建安全网：纳入版本控制 → 特征测试记录现状
2. 修 save()（行为变更，单独提交）
...

## 可以保持的地方
- 依赖是靠参数传入的（db、order 都是参数）……这是一个现成的接缝
```

### 2. 重构

> 我要给 `process_order` 加一条折扣规则，但这函数我现在完全不敢动。帮我整理一下，让以后改起来安全一些。

代理不会直接动手改行为，而是：先补**特征测试**记录当前真实行为（包括已知的可疑行为）→ 小步重构并每步验证 → 明确告知**哪些是结构改动（行为不变）、哪些是行为变更**→ 给出折扣规则的精确插入点并询问业务口径。

### 3. 写新代码

> 帮我写一个小模块：读订单列表，按月份汇总金额并输出 CSV。数据可能来自脏 CSV，字段可能缺失或格式不对，出错时要说清楚是哪一行。

代理会边写边应用规则（职责单一、命名达意、金额用 Decimal、错误信息带行号），并在交付前跑自检清单，而不是事后补一份"最佳实践"说明。

---

## 内置扫描器

给审查和重构提供起点，可脱离代理单独使用：

```bash
python3 scripts/smell_scan.py src/                     # 扫描目录
python3 scripts/smell_scan.py src/ --ext .py,.ts       # 限定扩展名
python3 scripts/smell_scan.py src/ --max-func-lines 30 --top 20
python3 scripts/smell_scan.py src/ --json              # 供程序处理
```

输出示例：

```
扫描文件 6 个，有效代码约 108 行。
注意：以下是启发式线索，不是结论——每一项都要结合上下文判断是否真的有害。

类别统计：参数过多 8，深层嵌套 4，吞掉异常 3，长函数 2，注释掉的代码 1，遗留标记 1
严重度统计：Major 18，Minor 3

── legacy_report.py
  [Major] 参数过多:4 — 函数 process_order 有 6 个参数（阈值 3）
      参数顺序易传错、职责过多；考虑参数对象或拆分函数
  [Major] 注释掉的代码:34 — 第 34-40 行有 7 行疑似被注释的代码
      版本控制已保留历史；注释掉的代码会随代码漂移，是纯负债
```

检查项：长文件、长函数、深层嵌套、参数过多、重复代码块、注释掉的代码、过长代码行、空 `catch` / 裸 `except` / `except: pass`、魔法数字、TODO/FIXME。

阈值都可以用命令行参数调整；扫描器会跳过 `node_modules`、`dist`、`.venv` 等目录，只读不写。

> ⚠️ **它只给线索，不做判断。** 启发式扫描必然有误报，技能正文也要求代理逐条人工复核后再下结论（自测中就出现过代理正确识别并说明扫描器误报的情况）。

---

## 设计原则

1. **规则服务于可读性，而不是教条。** 与语言惯用法、性能、一次性脚本的现实需求冲突时，明确说明破例原因——而不是机械遵守。
2. **模式分流。** 写代码时不要主动甩一份审查报告；做审查时不要顺手重写整个文件。先给判断，再问是否动手。
3. **严重度分级 + 最小改法。** 只说"这里不好"没有价值；20 条 Minor 淹没 1 条 Blocker 是最差的审查方式。
4. **重构的前提是不改变行为。** 没有测试就先建特征测试；重构与功能变更分开提交。
5. **工具输出是线索，不是结论。** 扫描器负责不漏类别，判断交给读代码的人。

---

## 自测与评测

`evals/evals.json` 收录了 3 个真实场景（审查遗留代码、重构遗留代码、写新模块）和 22 条断言。发布前用带技能 / 不带技能两组子代理各跑了一遍：

| 场景 | 带技能 | 不带技能 |
|---|---|---|
| 审查遗留代码 | 模板化分级报告，3 Blocker + 6 Major，含修改顺序与"可以保持的地方" | 同样准确，P0/P1/P2 结构 |
| 重构遗留代码 | 43 个特征测试，严格行为不变，折扣只给插入点 | 50 个测试 + 差分模糊测试，但顺手改了 `save()` 行为 |
| 写新模块 | 289 行模块 / 43 用例，并复核出扫描器 2 处误报 | 455 行模块 / 26 用例 |

结论是：强模型两边都能交付可用结果，**技能的增量主要在纪律性与一致性**——固定的严重度分级与报告结构、"重构不改行为/不混功能变更"、工具线索需人工复核、更克制的设计规模。自测暴露的扫描器续行误报问题已修复并回归验证。

---

## 已知限制

- 扫描器是启发式的：跨行表达式、数据文件、测试 fixture 都可能触发误报，需人工判断。
- 技能正文与示例为中文；用于英文项目时规则依然适用，但输出语言取决于你的指令。
- 它是书里思想的**执行版整理**，不是书的替代品——想系统学习仍然建议读原书。

---

## 许可

本项目采用 [MIT 许可证](LICENSE)，可自由使用、修改、商用，只需保留版权声明与许可证原文。

需要注意：许可证只覆盖本仓库的原创内容。书中的概念、原文与示例代码版权归 Robert C. Martin 及出版社所有，使用本技能不代表获得原书内容的任何授权。

---

## 致谢与声明

内容基于 Robert C. Martin 的 *Clean Code: A Handbook of Agile Software Craftsmanship*（中译《代码整洁之道》）整理与重写，本项目与作者及出版社无任何关联。所有规则表述均为面向 AI 代理的原创整理，不含书中原文长段引用。

## English

**clean-code** is an agent skill that turns *Clean Code* (Robert C. Martin) into executable rules for AI coding agents: it routes between writing, reviewing, and refactoring, ships an 86-item code-smell catalog and a safe-refactoring playbook, and includes a dependency-free heuristic scanner (`scripts/smell_scan.py`). Drop the repo into your skills directory (`~/.agents/skills/clean-code` or `~/.claude/skills/clean-code`) and it activates whenever you write, review, or refactor code. Skill content is written in Chinese; the scanner and rules are language-agnostic for the source code under inspection.
