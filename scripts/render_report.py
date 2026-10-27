#!/usr/bin/env python3
"""Validate per-hunk annotations and render an offline side-by-side diff report."""
import argparse
import base64
import hashlib
from difflib import SequenceMatcher
from html import escape
from itertools import zip_longest
import json
from pathlib import Path
import re
import sys

from collect_diff import DiffError, fingerprint, parse_patch


REASONS = {"documented": "有直接依据", "inferred": "根据代码推断", "unknown": "原因待确认"}
STATES = {"passed": "已通过", "failed": "未通过", "not_run": "未执行"}


def text_value(value, location):
    if not isinstance(value, str) or not value.strip():
        raise DiffError(f"{location}: nonempty text required")


def text_list(value, location):
    if not isinstance(value, list):
        raise DiffError(f"{location}: list required")
    for item in value:
        text_value(item, location)


def check_reason(value, location):
    if not isinstance(value, dict) or value.get("status") not in REASONS:
        raise DiffError(f"{location}: reason.status must be documented, inferred, or unknown")
    text_value(value.get("text"), location + ".text")
    text_list(value.get("evidence"), location + ".evidence")
    if value["status"] != "unknown" and not value["evidence"]:
        raise DiffError(f"{location}: evidence required for {value['status']}")


def validate(bundle, annotations):
    if bundle.get("schema_version") != 1:
        raise DiffError("Unsupported diff schema_version")
    if annotations.get("schema_version") != 2:
        raise DiffError("Annotation schema_version 2 required: use one hunk_id note per diff block")
    if bundle.get("bundle_id") != fingerprint(bundle):
        raise DiffError("Diff fingerprint mismatch; do not edit the collected snapshot")
    if annotations.get("bundle_id") != bundle["bundle_id"]:
        raise DiffError("Annotations belong to a different diff snapshot")
    for key in ("title", "summary", "impact_summary"):
        text_value(annotations.get(key), key)
    text_list(annotations.get("risks"), "risks")
    if not isinstance(annotations.get("verification"), list):
        raise DiffError("verification: list required")
    for item in annotations["verification"]:
        text_value(item.get("description"), "verification.description")
        text_value(item.get("evidence"), "verification.evidence")
        if item.get("status") not in STATES:
            raise DiffError("verification.status must be passed, failed, or not_run")
    expected = {}
    for scope in bundle["scopes"]:
        if not re.fullmatch(r"s[1-9]\d*", scope["id"]):
            raise DiffError("Invalid scope ID")
        for file in scope["files"]:
            if not re.fullmatch(r"s[1-9]\d*-f[1-9]\d*", file["id"]) or file["id"] in expected:
                raise DiffError("Invalid or duplicate file ID")
            hunks, metadata = parse_patch(file["patch"], file["id"])
            if hunks != file["hunks"] or metadata != file["metadata"]:
                raise DiffError(f"{file['id']}: parsed rows do not match raw patch")
            expected[file["id"]] = {
                hunk["id"] for hunk in file["hunks"]
                if any(row["kind"] in ("add", "delete") for row in hunk["lines"])
            }
    supplied, all_notes = {}, {}
    if not isinstance(annotations.get("files"), list):
        raise DiffError("files: list required")
    for file in annotations["files"]:
        file_id = file.get("file_id")
        if file_id not in expected or file_id in supplied:
            raise DiffError(f"Unknown or duplicate file_id: {file_id}")
        for key in ("summary", "impact"):
            text_value(file.get(key), f"{file_id}.{key}")
        check_reason(file.get("reason"), f"{file_id}.reason")
        if not isinstance(file.get("notes"), list):
            raise DiffError(f"{file_id}.notes: list required")
        supplied[file_id] = file
        seen = set()
        for note in file["notes"]:
            hunk_id = note.get("hunk_id")
            if "line_id" in note:
                raise DiffError("Line annotations are no longer supported; explain the entire hunk")
            if hunk_id not in expected[file_id] or hunk_id in seen:
                raise DiffError(f"Unknown or duplicate diff block: {hunk_id}")
            seen.add(hunk_id)
            for key in ("change", "impact"):
                text_value(note.get(key), f"{hunk_id}.{key}")
            for key in ("risk", "verification"):
                if key in note:
                    text_value(note[key], f"{hunk_id}.{key}")
            check_reason(note.get("reason"), f"{hunk_id}.reason")
            all_notes[hunk_id] = note
        missing = expected[file_id] - seen
        if missing:
            raise DiffError(f"{file_id}: {len(missing)} unexplained diff block(s): "
                            + ", ".join(sorted(missing)[:10]))
    missing_files = expected.keys() - supplied.keys()
    if missing_files:
        raise DiffError("Missing file annotations: " + ", ".join(sorted(missing_files)))
    return supplied, all_notes


def e(value):
    return escape(str(value), quote=True)


def paragraph(label, value, cls=""):
    return f'<p class="{e(cls)}"><strong>{e(label)}</strong><span>{e(value)}</span></p>'


def reason_html(reason):
    evidence = "".join(f"<li>{e(value)}</li>" for value in reason["evidence"])
    evidence_block = f'<details class="evidence"><summary>依据</summary><ul>{evidence}</ul></details>' if evidence else ""
    return (f'<div class="reason"><span class="badge {reason["status"]}">{REASONS[reason["status"]]}</span>'
            + paragraph("原因", reason["text"]) + evidence_block + "</div>")


def path_label(file):
    before, after = file["old_path"], file["new_path"]
    return f"{before} → {after}" if before and after and before != after else (after or before)


def split_rows(hunk):
    """Align context and pair each contiguous edit run without reordering either side."""
    pairs, removed, added = [], [], []

    def flush():
        pairs.extend(zip_longest(removed, added))
        removed.clear()
        added.clear()

    for row in hunk["lines"]:
        if row["kind"] == "context":
            flush()
            pairs.append((row, row))
        elif row["kind"] == "delete":
            removed.append(row)
        else:
            added.append(row)
    flush()
    return pairs


def highlighted_pair(before, after):
    """Return escaped code with character highlights; cap work on long generated lines."""
    if len(before) + len(after) > 8000:
        return e(before), e(after)
    old_parts, new_parts = [], []
    for tag, i, j, k, l in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        old, new = e(before[i:j]), e(after[k:l])
        if tag == "equal":
            old_parts.append(old)
            new_parts.append(new)
        else:
            if old:
                old_parts.append(f'<mark class="intraline delete">{old}</mark>')
            if new:
                new_parts.append(f'<mark class="intraline add">{new}</mark>')
    return "".join(old_parts), "".join(new_parts)


def side_cells(row, side, highlighted=None):
    if row is None:
        return (f'<td class="ln placeholder {side}" aria-hidden="true"></td>'
                f'<td class="code placeholder {side}" aria-label="此侧无对应行"></td>')
    number = row["old_line"] if side == "old" else row["new_line"]
    # Context is shown on both sides, but the canonical line ID occurs only once.
    anchor = row["id"] + ("-new" if side == "new" and row["kind"] == "context" else "")
    symbol = {"add": "+", "delete": "−", "context": " "}[row["kind"]]
    no_newline = '<span class="no-newline">文件末尾无换行</span>' if row["no_newline"] else ""
    code_text = row["text"]
    if code_text.endswith("\r"):
        code_text = code_text[:-1]
        no_newline += '<span class="no-newline">CRLF 行尾</span>'
    code = e(code_text) if highlighted is None else highlighted
    return (f'<td class="ln {side} {row["kind"]}"><a href="#{e(anchor)}">{number}</a></td>'
            f'<td id="{e(anchor)}" class="code {side} {row["kind"]}"'
            f' data-source-id="{e(row["id"])}" data-side="{side}" data-line="{number}">'
            f'<span class="sign" aria-label="{e(row["kind"])}">{symbol}</span>'
            f'<pre>{code}</pre>{no_newline}</td>')


def split_row_html(before, after):
    context = before is not None and after is not None and before["kind"] == after["kind"] == "context"
    old_code = new_code = None
    if before is not None and after is not None and not context:
        old_code, new_code = highlighted_pair(before["text"].removesuffix("\r"),
                                              after["text"].removesuffix("\r"))
    return (f'<tr class="diff-row {"context" if context else "changed"}">'
            + side_cells(before, "old", old_code) + side_cells(after, "new", new_code) + "</tr>")


def hunk_html(hunk, file, note, number):
    before = file["old_path"] or "（文件不存在）"
    after = file["new_path"] or "（文件不存在）"
    body = (f'<section class="diff-block" id="{e(hunk["id"])}">'
            f'<div class="block-heading"><a href="#{e(hunk["id"])}">Diff 块 {number}</a>'
            f'<code>{e(hunk["header"])}</code></div>'
            '<div class="table-wrap"><table class="diff-table">'
            f'<caption class="sr-only">{e(path_label(file))} 的第 {number} 个差异块：左侧修改前，右侧修改后</caption>'
            '<colgroup><col class="line-col"><col class="code-col">'
            '<col class="line-col"><col class="code-col"></colgroup>'
            '<thead><tr><th colspan="2" scope="colgroup" class="before-heading">'
            f'<strong>修改前</strong><span>{e(before)}</span></th>'
            '<th colspan="2" scope="colgroup" class="after-heading">'
            f'<strong>修改后</strong><span>{e(after)}</span></th></tr></thead><tbody>')
    body += "".join(split_row_html(before, after) for before, after in split_rows(hunk))
    body += "</tbody></table></div>"
    if note:
        body += ('<div class="block-annotation"><div class="annotation-heading">本块解读</div>'
                 + paragraph("改动", note["change"]) + reason_html(note["reason"])
                 + paragraph("影响", note["impact"]))
        if note.get("risk"):
            body += paragraph("风险", note["risk"], "risk")
        if note.get("verification"):
            body += paragraph("验证", note["verification"])
        body += "</div>"
    return body + "</section>"


def file_html(file, annotation, notes, scope_label):
    kind = "二进制" if file["binary"] else "子模块" if file["submodule"] else "文本"
    mode = f'{file["old_mode"]} → {file["new_mode"]}'
    file_id = e(file["id"])
    body = (f'<details class="file" id="{file_id}" open><summary class="file-title">'
            f'<span class="file-path">{e(path_label(file))}</span><span class="file-count">'
            f'<b class="plus">+{file["additions"]}</b> <b class="minus">−{file["deletions"]}</b>'
            f' <span class="badge">{e(file["status"])}</span></span></summary>'
            f'<div class="file-overview"><div class="eyebrow">{e(scope_label)} · {kind} · 模式 {e(mode)}</div>'
            + paragraph("文件作用", annotation["summary"]) + reason_html(annotation["reason"])
            + paragraph("整体影响", annotation["impact"]) + "</div>")
    if file["binary"]:
        body += '<p class="notice">二进制内容无法以文本逐行比较；以上是文件级说明。</p>'
    if file["hunks"]:
        for number, hunk in enumerate(file["hunks"], 1):
            body += hunk_html(hunk, file, notes.get(hunk["id"]), number)
    else:
        body += '<p class="notice">此项没有文本增删行；重命名、文件模式或内容类型变化已在文件级说明中覆盖。</p>'
    body += ('<details class="raw"><summary>查看原始 patch</summary>'
             f'<pre>{e(file["patch"])}</pre></details></details>')
    return body


def render(bundle, annotations):
    annotated_files, notes = validate(bundle, annotations)
    files = [file for scope in bundle["scopes"] for file in scope["files"]]
    changed = len(notes)
    additions = sum(file["additions"] for file in files)
    deletions = sum(file["deletions"] for file in files)
    assets = Path(__file__).resolve().parent.parent / "assets"
    css = (assets / "report.css").read_text(encoding="utf-8")
    js = (assets / "report.js").read_text(encoding="utf-8")
    script_hash = base64.b64encode(hashlib.sha256(js.encode()).digest()).decode()
    csp = ("default-src 'none'; style-src 'unsafe-inline'; "
           f"script-src 'sha256-{script_hash}'; base-uri 'none'; form-action 'none'")
    nav, sections = [], []
    for scope in bundle["scopes"]:
        nav.append(f'<div class="nav-group">{e(scope["label"])}</div>')
        sections.append(f'<section class="scope" id="{e(scope["id"])}"><div class="scope-title">'
                        f'<h2>{e(scope["label"])}</h2><span>{len(scope["files"])} 个文件条目</span></div>'
                        f'<details class="provenance"><summary>比较基准与提交信息</summary>'
                        f'<dl><dt>旧版本</dt><dd><code>{e(scope["base"])}</code></dd>'
                        f'<dt>新版本</dt><dd><code>{e(scope["target"])}</code></dd></dl>')
        if scope.get("commit_message"):
            sections.append(f'<pre>{e(scope["commit_message"])}</pre>')
        sections.append("</details>")
        if not scope["files"]:
            sections.append('<p class="empty">该范围没有改动。</p>')
        for file in scope["files"]:
            nav.append(f'<a class="file-link" href="#{e(file["id"])}" data-target="{e(file["id"])}">'
                       f'<span>{e(path_label(file))}</span><small>+{file["additions"]} / −{file["deletions"]}</small></a>')
            sections.append(file_html(file, annotated_files[file["id"]], notes, scope["label"]))
        sections.append("</section>")
    risks = "".join(f"<li>{e(value)}</li>" for value in annotations["risks"])
    warnings = "".join(f"<li>{e(value)}</li>" for value in bundle["warnings"])
    checks = "".join(f'<li><span class="badge {e(item["status"])}">{STATES[item["status"]]}</span>'
                     f'<strong>{e(item["description"])}</strong><p>{e(item["evidence"])}</p></li>'
                     for item in annotations["verification"])
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{e(csp)}">'
            f'<title>{e(annotations["title"])}</title><style>{css}</style></head><body>'
            '<a class="skip-link" href="#main">跳至报告正文</a>'
            '<aside><a class="brand" href="#main"><span class="brand-icon">±</span> DIFF / 解读</a>'
            '<p class="nav-label">文件索引</p><nav aria-label="文件导航">'
            + "".join(nav) + '</nav><p class="aside-note">离线报告<br>Diff 块覆盖已校验</p></aside>'
            '<main id="main"><header><p class="eyebrow">CODE CHANGE EXPLAINED</p>'
            f'<h1>{e(annotations["title"])}</h1><p class="lead">{e(annotations["summary"])}</p>'
            '<div class="metrics">'
            f'<div><b>{len(files)}</b><span>文件条目</span></div>'
            f'<div><b class="plus">+{additions}</b><span>新增行</span></div>'
            f'<div><b class="minus">−{deletions}</b><span>删除行</span></div>'
            f'<div><b>{changed}/{changed}</b><span>已解释 Diff 块</span></div></div>'
            '<p class="coverage-note">每个 Diff 块均有完整说明。左侧为修改前，右侧为修改后；语义判断及推断仍需结合依据审阅。</p>'
            f'<div class="impact-summary"><span class="eyebrow">影响概览</span><p>{e(annotations["impact_summary"])}</p></div>'
            '<details class="provenance"><summary>快照信息</summary><dl>'
            f'<dt>仓库</dt><dd>{e(bundle["repository"])}</dd>'
            f'<dt>采集时间</dt><dd>{e(bundle["created_at"])}（UTC）</dd>'
            f'<dt>快照指纹</dt><dd><code>{e(bundle["bundle_id"])}</code></dd>'
            f'<dt>文本编码</dt><dd>{e(bundle["encoding"])}</dd>'
            f'<dt>范围</dt><dd>{e(", ".join(bundle["paths"]) or "仓库全部路径")}</dd></dl></details>'
            + (f'<ul class="warnings">{warnings}</ul>' if warnings else "")
            + '</header><div class="controls"><label for="search">搜索</label>'
            '<input id="search" type="search" placeholder="文件、代码或解释…" autocomplete="off">'
            '<label class="check"><input id="context-toggle" type="checkbox">隐藏上下文</label>'
            '<button type="button" id="expand-all">展开全部</button>'
            '<button type="button" id="collapse-all">折叠全部</button>'
            '<button type="button" id="print-report">打印 / PDF</button>'
            f'<span id="search-count" aria-live="polite">{len(files)} 个文件条目</span></div>'
            '<p id="no-results" class="empty" hidden>没有匹配的文件、代码或说明。</p>'
            + "".join(sections)
            + '<section class="review-notes"><h2>风险与验证</h2><div class="review-grid">'
            '<div><h3>风险与待确认项</h3>'
            + (f"<ul>{risks}</ul>" if risks else "<p>未列出风险项；不代表已排除所有风险。</p>")
            + '</div><div><h3>验证记录</h3>'
            + (f'<ul class="checks">{checks}</ul>' if checks else "<p>未记录已执行的验证。</p>")
            + '</div></div></section><footer>本报告对应采集时的快照。'
            '来源说明、代码事实与改动动机应分别核对。</footer></main>'
            f'<script>{js}</script></body></html>')


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--output")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not args.validate_only and not args.output:
        parser.error("--output is required unless --validate-only is used")
    try:
        bundle, annotations = load_json(args.diff), load_json(args.annotations)
        files, notes = validate(bundle, annotations)
        changed_lines = sum(file["additions"] + file["deletions"]
                            for scope in bundle["scopes"] for file in scope["files"])
        result = {"valid": True, "files": len(files), "explained_diff_blocks": len(notes),
                  "changed_lines": changed_lines, "coverage": 1.0, "bundle_id": bundle["bundle_id"]}
        if not args.validate_only:
            output = Path(args.output).resolve()
            if output in (Path(args.diff).resolve(), Path(args.annotations).resolve()):
                raise DiffError("HTML output must not overwrite either input")
            html = render(bundle, annotations)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(html, encoding="utf-8", newline="\n")
            result["output"] = str(output)
        print(json.dumps(result, ensure_ascii=True))
    except (DiffError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
