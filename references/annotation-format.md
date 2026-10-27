# 注释格式与判定标准

复制采集生成的 `annotations.template.json` 到自己的注释文件并填充。每个 Git hunk（以 `@@ … @@` 开头的 diff 块）对应一个 note，解释显示在该块的左右代码下方。不要改变 `bundle_id`、`file_id` 或 `hunk_id`。所有 prose 字段都是纯文本，不是 HTML 或 Markdown；需要分点时用换行和普通文本序号。

```json
{
  "schema_version": 2,
  "bundle_id": "从 diff.json 原样复制",
  "title": "订单数量校验：分块改动说明",
  "summary": "说明此范围的主要变化和前后行为。",
  "impact_summary": "说明受到影响的调用方、边界条件和兼容性。",
  "risks": ["真实风险、局限或尚待验证的假设；无已识别风险时可为空数组"],
  "verification": [
    {"description": "数量为零的边界用例", "status": "not_run", "evidence": "阅读 tests/test_quantity.py；未执行测试。"}
  ],
  "files": [
    {
      "file_id": "s1-f1",
      "summary": "此文件在整体变化中的作用。",
      "reason": {
        "status": "inferred",
        "text": "根据新增边界用例，推测意图是保留显式传入的零。",
        "evidence": ["目标版本 tests/test_quantity.py:8，test_zero_is_preserved"]
      },
      "impact": "传入 0 时结果从默认数量改为 0；未传值仍使用默认数量。",
      "notes": [
        {
          "hunk_id": "s1-f1-h1",
          "change": "将 value or default 改为显式判断 None：缺省时返回 default，否则保留输入；同步更新函数说明。",
          "reason": {"status": "inferred", "text": "旧表达式将 0 与未提供值合并处理；新增零值测试表明本块意图是区分这两类输入。", "evidence": ["旧版本 quantity.py:2；目标版本的零值测试"]},
          "impact": "输入 0 从返回默认值变为返回 0；输入 None 仍使用默认值。说明文字不影响运行时行为。",
          "risk": "可选：本块特有的兼容性或边界风险。",
          "verification": "可选：具体测试证据或待检查案例，注明是否执行。"
        }
      ]
    }
  ]
}
```

每个含新增／删除行的 hunk 有且仅有一个 note，其 `hunk_id` 取自 `diff.json` 的 `hunks[].id`。不要给每一行单独写 note，也不要将多个 hunk 合并成一个 note。每个 note 的 `change`、`reason` 和 `impact` 必填，文件级 `summary`、`reason` 和 `impact` 必填。文件级摘要不能代替块说明。纯重命名等没有文本 hunk 的文件使用空 notes 数组。

注释格式使用 `schema_version: 2`，采集快照 `diff.json` 仍使用版本 1。旧版按行注释不能直接传给新版渲染器；应保留原文件，根据对应 hunk 的完整语义重新填写版本 2 注释，不要仅拼接旧逐行说明。

`reason.status` 仅允许：

| 值 | 展示 | 证据要求 |
| --- | --- | --- |
| documented | 有直接依据 | evidence 至少一条；来源确实直接描述动机，不只是展示变化 |
| inferred | 根据代码推断 | evidence 至少一条；写清从哪些代码、测试或调用关系推断 |
| unknown | 原因待确认 | 可无 evidence；text 明确无法确定的内容，不用肯定句猜测动机 |

`verification.status` 仅允许 `passed`、`failed`、`not_run`。未执行测试不得标 passed。只完成静态阅读时，写明阅读了什么，并保持相关测试状态为 not_run。风险列表可为空，不能自动把空列表表述为“没有风险”。

解释标准：

- 不合格：“新增 return；优化代码；提升性能”。这些没有说明语义或证据。
- 合格：“新增 `value is None` 判断，只对缺省值采用默认数量；传入 0 不再进入默认分支。原因根据目标版本的零值测试推断，尚无需求文档确认。”
- 块内包含删除与新增时，一起说明旧行为被移除、迁移还是替代，并给出新行为的具体条件与结果。
- 同一块内有多项独立变化时，用同一 note 中的分段或序号覆盖全部变化。空行、括号等机械性变化只需在相关逻辑说明中交代。

左右代码按上下文行对齐，连续增删区域按各自顺序并排显示，较短的替换行有行内高亮。占位空白不是源代码；视觉配对也不代表两行存在一一对应的语义关系。代码内容、两侧行号和 hunk 边界均以 Git 快照为准。

采集快照包含范围的完整 SHA、patch、hunk ID 和行 ID。`bundle_id` 校验把解释绑定到这份快照；它不证明代码语义判断正确，也不证明工作区之后未变化。
