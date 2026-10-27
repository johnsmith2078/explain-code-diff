#!/usr/bin/env python3
"""Collect read-only Git snapshots and a fill-in annotation template."""
import argparse
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


class DiffError(Exception):
    pass


def fingerprint(data):
    payload = {k: v for k, v in data.items() if k not in ("bundle_id", "created_at")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Git:
    def __init__(self, repo):
        self.repo = str(Path(repo).resolve())

    def run(self, *args, input_data=None, allowed=(0,)):
        env = os.environ.copy()
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_NO_LAZY_FETCH": "1",
                    "GIT_LITERAL_PATHSPECS": "1", "LC_ALL": "C", "LANG": "C"})
        cmd = ["git", "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
               "-c", "color.ui=false", "-C", self.repo, *args]
        result = subprocess.run(cmd, input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)
        if result.returncode not in allowed:
            raise DiffError(result.stderr.decode("utf-8", "replace").strip()
                            or f"git {args[0]} failed ({result.returncode})")
        return result.stdout

    def resolve(self, ref):
        return self.run("rev-parse", "--verify", "--end-of-options",
                        ref + "^{commit}").decode("ascii").strip()

    def head(self):
        result = self.run("rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1))
        return result.decode("ascii").strip() or None

    def empty_tree(self):
        return self.run("hash-object", "-t", "tree", "--stdin", input_data=b"").decode().strip()


def parse_patch(patch, file_id):
    """Parse LF-delimited patch lines, preserving CR and no-newline markers."""
    hunks, metadata = [], []
    current = None
    old_line = new_line = 0
    old_used = new_used = 0

    def finish():
        if current and (old_used != current["old_count"] or new_used != current["new_count"]):
            raise DiffError(f"Malformed/truncated hunk in {file_id}")

    for raw in patch.split("\n"):
        if raw.startswith("diff --git "):
            # Git represents a file type change as a deletion plus an addition.
            finish()
            current = None
            metadata.append(raw)
        elif raw.startswith("@@"):
            finish()
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", raw)
            if not match:
                raise DiffError(f"Unsupported hunk header in {file_id}: {raw[:100]}")
            old_line, new_line = int(match[1]), int(match[3])
            current = {"id": f"{file_id}-h{len(hunks)+1}", "header": raw,
                       "old_start": old_line, "old_count": int(match[2] or 1),
                       "new_start": new_line, "new_count": int(match[4] or 1), "lines": []}
            hunks.append(current)
            old_used = new_used = 0
        elif current is None:
            if raw:
                metadata.append(raw)
        elif raw.startswith("\\ No newline at end of file"):
            if not current["lines"]:
                raise DiffError("No-newline marker without a preceding line")
            current["lines"][-1]["no_newline"] = True
        elif raw and raw[0] in " +-":
            kind = {" ": "context", "+": "add", "-": "delete"}[raw[0]]
            row = {"id": f'{current["id"]}-l{len(current["lines"])+1}', "kind": kind,
                   "old_line": old_line if kind != "add" else None,
                   "new_line": new_line if kind != "delete" else None,
                   "text": raw[1:], "no_newline": False}
            current["lines"].append(row)
            if kind != "add":
                old_line += 1
                old_used += 1
            if kind != "delete":
                new_line += 1
                new_used += 1
        elif raw:
            raise DiffError(f"Unexpected patch data in {file_id}: {raw[:100]}")
    finish()
    return hunks, metadata


def parse_raw(raw):
    fields, files, i = raw.split(b"\0"), [], 0
    while i < len(fields) and fields[i]:
        header = fields[i].decode("ascii").split()
        if len(header) != 5 or not header[0].startswith(":"):
            raise DiffError("Unsupported raw diff format (possibly an unresolved merge)")
        old_mode, new_mode, old_oid, new_oid, status = header
        i += 1
        path = fields[i].decode("utf-8", "strict")
        i += 1
        old_path = new_path = path
        if status[0] in "RC":
            new_path = fields[i].decode("utf-8", "strict")
            i += 1
        if status[0] == "A":
            old_path = None
        if status[0] == "D":
            new_path = None
        if status[0] in "UXB":
            raise DiffError(f"Unsupported Git status {status}: {path}")
        files.append({"status": status, "old_path": old_path, "new_path": new_path,
                      "old_mode": old_mode[1:], "new_mode": new_mode,
                      "old_oid": old_oid, "new_oid": new_oid})
    return files


def working_digest(root, path, mode):
    if path is None or mode == "000000":
        return None
    if mode == "160000":
        return "gitlink: nested working-tree contents are not included"
    target = Path(root) / path
    if target.is_symlink():
        content = os.fsencode(os.readlink(target))
    else:
        content = target.read_bytes()
    return hashlib.sha256(content).hexdigest()


def decode_patch(data, encoding):
    """Git headers use UTF-8 paths; source lines use the selected text encoding."""
    result, in_hunk = [], False
    for line in data.split(b"\n"):
        if line.startswith(b"diff --git "):
            in_hunk = False
        if line.startswith(b"@@"):
            in_hunk = True
            codec = encoding
        elif in_hunk and line[:1] in (b" ", b"+", b"-"):
            codec = encoding
        else:
            codec = "utf-8"
        result.append(line.decode(codec, "strict"))
    return "\n".join(result)


def finish_file(item, patch_bytes, file_id, encoding, root, local):
    try:
        # Paths come from the UTF-8 NUL-delimited raw records, not these headers.
        patch = decode_patch(patch_bytes, encoding)
    except UnicodeDecodeError as exc:
        raise DiffError(f"{item.get('new_path') or item.get('old_path')}: cannot decode patch "
                        f"as {encoding}; specify --encoding explicitly") from exc
    hunks, metadata = parse_patch(patch, file_id)
    item.update({"id": file_id, "patch": patch, "hunks": hunks, "metadata": metadata,
                 "binary": any(x.startswith("Binary files ") or x == "GIT binary patch"
                               for x in metadata),
                 "submodule": "160000" in (item["old_mode"], item["new_mode"])})
    rows = [row for hunk in hunks for row in hunk["lines"]]
    item["additions"] = sum(row["kind"] == "add" for row in rows)
    item["deletions"] = sum(row["kind"] == "delete" for row in rows)
    if local:
        item["worktree_sha256"] = working_digest(root, item["new_path"], item["new_mode"])
    return item


def common_options(context):
    return ["--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
            "--src-prefix=a/", "--dst-prefix=b/", "--no-abbrev", "--full-index",
            "--find-renames", "--submodule=short", "--ignore-submodules=none",
            "--diff-algorithm=myers", f"--unified={context}"]


def collect_scope(git, spec, index, args):
    scope_id = f"s{index}"
    data = git.run("diff", *common_options(args.context), "--raw", "-z", "--patch",
                   *spec["git_args"], "--", *args.path)
    files = []
    if data:
        raw, separator, patch_data = data.partition(b"\0\0")
        if not separator:
            raise DiffError("Missing raw/patch boundary in Git output")
        records = parse_raw(raw)
        patches = re.split(br"(?m)(?=^diff --git )", patch_data)
        patches = [p for p in patches if p]
        patch_counts = [2 if record["status"].startswith("T") else 1 for record in records]
        if sum(patch_counts) != len(patches):
            raise DiffError("File/patch count mismatch; refusing a partial report")
        cursor = 0
        for i, (record, count) in enumerate(zip(records, patch_counts), 1):
            patch = b"".join(patches[cursor:cursor + count])
            cursor += count
            files.append(finish_file(record, patch, f"{scope_id}-f{i}", args.encoding,
                                     git.repo, spec["mode"] == "worktree"))
    if args.include_untracked and spec["mode"] == "worktree":
        paths = git.run("ls-files", "--others", "--exclude-standard", "-z", "--", *args.path)
        for encoded in paths.split(b"\0"):
            if not encoded:
                continue
            path = encoded.decode("utf-8", "strict")
            target = Path(git.repo) / path
            if target.is_dir() and not target.is_symlink():
                raise DiffError(f"Untracked nested repository/directory {path}; collect it separately")
            patch = git.run("diff", "--no-index", "--no-ext-diff", "--no-textconv",
                            "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
                            "--full-index", f"--unified={args.context}", "--",
                            os.devnull, path, allowed=(0, 1))
            mode_match = re.search(br"(?m)^new file mode (\d+)$", patch)
            mode = mode_match[1].decode() if mode_match else "100644"
            record = {"status": "?", "old_path": None, "new_path": path,
                      "old_mode": "000000", "new_mode": mode, "old_oid": None, "new_oid": None}
            files.append(finish_file(record, patch, f"{scope_id}-f{len(files)+1}",
                                     args.encoding, git.repo, True))
    return {**spec, "id": scope_id, "files": files}


def reason_template():
    return {"status": "unknown", "text": "", "evidence": []}


def annotation_template(bundle):
    files = []
    for scope in bundle["scopes"]:
        for file in scope["files"]:
            notes = [{"hunk_id": hunk["id"], "change": "", "reason": reason_template(), "impact": ""}
                     for hunk in file["hunks"]
                     if any(row["kind"] != "context" for row in hunk["lines"])]
            files.append({"file_id": file["id"], "summary": "", "reason": reason_template(),
                          "impact": "", "notes": notes})
    return {"schema_version": 2, "bundle_id": bundle["bundle_id"], "title": "代码 Diff 分块解读",
            "summary": "", "impact_summary": "", "risks": [], "verification": [], "files": files}


def build_bundle(args):
    codecs.lookup(args.encoding)
    git = Git(args.repo)
    git.repo = git.run("rev-parse", "--show-toplevel").decode("utf-8").strip()
    head = git.head()
    if args.mode in ("all", "staged", "worktree") and git.run("ls-files", "--unmerged", "-z"):
        raise DiffError("Unresolved index conflicts exist; a two-sided diff cannot describe them")
    specs = []
    if args.mode in ("all", "staged"):
        base = head or git.empty_tree()
        specs.append({"mode": "staged", "label": "暂存区：HEAD → index" if head else "暂存区：空树 → index",
                      "base": base, "target": "index", "git_args": ["--cached", base]})
    if args.mode in ("all", "worktree"):
        specs.append({"mode": "worktree", "label": "工作区：index → worktree",
                      "base": "index", "target": "worktree", "git_args": []})
    if args.mode == "commit":
        commit = git.resolve(args.commit)
        header = git.run("cat-file", "-p", commit).split(b"\n\n", 1)[0]
        parents = [line[7:].decode("ascii") for line in header.split(b"\n")
                   if line.startswith(b"parent ")]
        if parents and not 1 <= args.parent <= len(parents):
            raise DiffError(f"Commit has {len(parents)} parents, cannot select parent {args.parent}")
        if not parents and args.parent != 1:
            raise DiffError("Root commit has no selectable parent")
        base = parents[args.parent - 1] if parents else git.empty_tree()
        message = git.run("show", "-s", "--format=%B", commit).decode("utf-8", "replace").strip()
        specs.append({"mode": "commit", "label": f"提交：父提交 {args.parent} → commit" if parents
                      else "根提交：空树 → commit", "base": base, "target": commit,
                      "parents": parents, "selected_parent": args.parent if parents else None,
                      "commit_message": message, "git_args": [base, commit]})
    if args.mode == "range":
        base, target = git.resolve(args.base), git.resolve(args.head)
        specs.append({"mode": "range", "label": "版本区间：base → head（端点比较）",
                      "base": base, "target": target, "git_args": [base, target]})
    scopes = [collect_scope(git, spec, i, args) for i, spec in enumerate(specs, 1)]
    if args.mode in ("all", "worktree", "staged"):
        again = [collect_scope(git, spec, i, args) for i, spec in enumerate(specs, 1)]
        if scopes != again or head != git.head():
            raise DiffError("Repository changed during collection; retry after edits finish")
    files = [file for scope in scopes for file in scope["files"]]
    warnings = []
    if not args.include_untracked and args.mode in ("all", "worktree"):
        warnings.append("未跟踪文件不在本次范围内；需要时使用 --include-untracked。")
    if any(file["binary"] for file in files):
        warnings.append("二进制文件只有文件级说明，不包含虚构的文本行差异。")
    if any(file["submodule"] for file in files):
        warnings.append("子模块只覆盖 Git 指针／dirty 标记；内部改动需在子模块中另行采集。")
    if any(len(scope.get("parents", [])) > 1 for scope in scopes):
        warnings.append("这是合并提交，报告只展示所选父提交与目标提交的差异。")
    bundle = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "repository": git.repo, "repository_head": head,
              "encoding": args.encoding, "context": args.context, "mode": args.mode,
              "paths": args.path, "include_untracked": args.include_untracked,
              "warnings": warnings, "scopes": scopes}
    bundle["bundle_id"] = fingerprint(bundle)
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--mode", choices=["all", "worktree", "staged", "commit", "range"], default="all")
    parser.add_argument("--commit")
    parser.add_argument("--parent", type=int, default=1)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--context", type=int, default=5)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--include-untracked", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.context < 0 or args.parent < 1:
        parser.error("--context must be nonnegative and --parent must be positive")
    if (args.mode == "commit") != (args.commit is not None):
        parser.error("--commit is required only for --mode commit")
    if args.mode == "range" and not (args.base and args.head):
        parser.error("--mode range requires --base and --head")
    if args.mode != "range" and (args.base or args.head):
        parser.error("--base / --head require --mode range")
    if args.mode != "commit" and args.parent != 1:
        parser.error("--parent requires --mode commit")
    if args.include_untracked and args.mode not in ("all", "worktree"):
        parser.error("--include-untracked requires --mode all or worktree")
    try:
        out = Path(args.out).resolve()
        paths = [out / "diff.json", out / "annotations.template.json"]
        if any(path.exists() for path in paths):
            raise DiffError("Output exists; use a new directory to preserve existing annotations")
        bundle = build_bundle(args)
        out.mkdir(parents=True, exist_ok=True)
        for path, value in zip(paths, (bundle, annotation_template(bundle))):
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        files = [file for scope in bundle["scopes"] for file in scope["files"]]
        print(json.dumps({"diff": str(paths[0]), "template": str(paths[1]),
                          "bundle_id": bundle["bundle_id"], "files": len(files),
                          "changed_lines": sum(f["additions"] + f["deletions"] for f in files),
                          "diff_blocks": sum(len(f["hunks"]) for f in files)},
                         ensure_ascii=True))
    except (DiffError, OSError, ValueError, LookupError, IndexError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
