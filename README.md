# explain-code-diff

一个用于 Codex 的 Git 代码差异解读 Skill：按 diff 块解释改动内容、原因和影响，并生成可离线打开的 HTML 报告。

报告采用左右并排的代码视图，左侧为修改前、右侧为修改后，保留各自的真实行号。删除用红色、新增用绿色；上下文对齐，纯增删在另一侧留空，较短替换行包含行内高亮。每个 diff 块下方集中展示说明。

## 功能

- 读取未暂存改动、暂存区、指定 commit，以及两个版本之间的差异。
- 同时分析本地改动时，分别展示 HEAD → 暂存区、暂存区 → 工作区。
- 以每个 `@@ … @@` hunk 为解释单位，校验全部 diff 块都有且仅有一份说明。
- 区分有直接依据的原因、根据代码的推断，以及无法确定的动机。
- 支持重命名、文件模式变化、二进制、子模块指针、根提交和合并提交的所选父提交。
- HTML 支持文件导航、搜索、折叠、隐藏上下文和打印；没有 CDN 或外部资源依赖。

采集和渲染脚本不会自行推断业务动机。Codex 需要阅读对应版本的代码、调用方和测试，再填写解释；覆盖率校验也不能代替语义审阅。

## 安装与调用

需要 Python 3.10+ 和 Git，无需安装第三方 Python 包。

将整个仓库放入你的 Codex skills 目录下，目录名保留为 `explain-code-diff`。例如，在目标目录尚不存在时：

```sh
git clone https://github.com/johnsmith2078/explain-code-diff.git "<你的 skills 目录>/explain-code-diff"
```

在目标代码仓库的 Codex 会话中调用：

```text
$explain-code-diff 按 diff 块解释当前工作区和暂存区改动，生成左右对比 HTML
$explain-code-diff 解释最近一次 commit，说明每个 diff 块的原因与影响
$explain-code-diff 比较 main 和 feature 分支，输出 HTML 报告
```

完整工作流程见 [SKILL.md](SKILL.md)。

## 脚本流程

在本仓库目录运行以下示例，将输出放在被分析仓库之外：

```sh
python -X utf8 scripts/collect_diff.py --repo /path/to/repository --mode all --out ../diff-analysis
```

支持的范围：

| 参数 | 比较内容 |
| --- | --- |
| `--mode all` | 分别采集暂存与未暂存改动 |
| `--mode worktree` | 暂存区 → 工作区 |
| `--mode staged` | HEAD → 暂存区 |
| `--mode commit --commit HEAD` | 所选提交的父提交 → 所选提交 |
| `--mode range --base main --head feature` | 两个端点直接比较 |

`all` / `worktree` 可加 `--include-untracked` 纳入未跟踪文件；`--path` 可重复指定字面路径；合并提交用 `--parent 2` 等选择其他父提交。

采集会生成 `diff.json` 与 `annotations.template.json`。根据 [注释格式](references/annotation-format.md) 填写独立的注释文件，再校验并渲染：

```sh
python -X utf8 scripts/render_report.py --diff ../diff-analysis/diff.json --annotations ../annotations.json --validate-only
python -X utf8 scripts/render_report.py --diff ../diff-analysis/diff.json --annotations ../annotations.json --output ../code-diff-explained.html
```

当前注释格式为 `schema_version: 2`，每个 note 使用 `hunk_id`。采集快照格式仍为版本 1。渲染器拒绝漏块、重复块、空解释、旧版逐行注释和快照指纹不匹配。

## 示例

[下载示例 HTML](examples/report.html)，保存到本地后用浏览器打开。示例来自专门构建的数量校验仓库，包含 2 个 diff 块、15 条增删行。

## 验证

```sh
python -X utf8 scripts/test_diff.py
```

随仓库提供 28 项自动测试，覆盖 Git 范围、特殊文件、块级注释校验、左右代码还原、行号对齐和 HTML 转义。测试使用临时仓库。

## 范围说明

二进制文件提供文件级解释。子模块只展示指针或 dirty 标记，内部改动需在子模块中另外采集。未解决的合并冲突会明确报错。报告对应采集时的快照，之后的工作区修改不会自动进入报告。
