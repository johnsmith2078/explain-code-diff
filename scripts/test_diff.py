"""Integration checks using disposable repositories; run with Python 3.10+."""
import argparse
import copy
from html.parser import HTMLParser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from collect_diff import DiffError, Git, annotation_template, build_bundle, parse_patch
from render_report import highlighted_pair, hunk_html, render, split_rows, validate


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class SourceCells(Tags):
    """Recover actual rendered source, ignoring line numbers, badges and raw patches."""
    def __init__(self):
        super().__init__()
        self.cells = []
        self.current = None
        self.in_pre = False
        self.content = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attributes = dict(attrs)
        if tag == "td":
            self.current = attributes if "data-source-id" in attributes else None
        elif tag == "pre" and self.current is not None:
            self.in_pre = True
            self.content = []

    def handle_data(self, data):
        if self.in_pre:
            self.content.append(data)

    def handle_endtag(self, tag):
        if tag == "pre" and self.in_pre:
            self.cells.append({**self.current, "text": "".join(self.content)})
            self.in_pre = False
        elif tag == "td":
            self.current = None


class DiffTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("DIFF_SKILL_TEST_ROOT")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="diff-skill-", dir=root)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Diff Skill Test")
        self.git("config", "user.email", "diff-skill@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        self.write("main.py", "def quantity(value):\n    return value or 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial quantity function")
        self.root_commit = self.git("rev-parse", "HEAD").decode().strip()

    def tearDown(self):
        # TemporaryDirectory uses native Python paths and removes only its own fixture.
        self.temp.cleanup()

    def git(self, *args, input_data=None, allowed=(0,)):
        proc = subprocess.run(["git", "-C", str(self.repo), *args], input=input_data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode not in allowed:
            raise AssertionError(proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)

    def collect(self, mode="all", **kwargs):
        values = dict(repo=str(self.repo), mode=mode, commit=None, parent=1,
                      base=None, head=None, path=[], context=3,
                      encoding="utf-8", include_untracked=False)
        values.update(kwargs)
        return build_bundle(argparse.Namespace(**values))

    @staticmethod
    def all_files(bundle):
        return [file for scope in bundle["scopes"] for file in scope["files"]]

    @staticmethod
    def changed_rows(file):
        return [row for hunk in file["hunks"] for row in hunk["lines"] if row["kind"] != "context"]

    def annotations(self, bundle):
        result = annotation_template(bundle)
        result.update(title="Fixture report", summary="Integration test fixture.",
                      impact_summary="Fixture-only changes, not production code.")
        for file in result["files"]:
            file.update(summary="Fixture file change.", impact="Fixture content changes.")
            file["reason"]["text"] = "Author intent is unknown in this test fixture."
            for note in file["notes"]:
                note.update(change="This fixture block replaces the old behavior with the new behavior.",
                            impact="The stored fixture content changes within this hunk.")
                note["reason"]["text"] = "Fixture intent is not documented."
        return result

    def test_separate_staged_and_unstaged_without_mutation(self):
        self.write("main.py", "def quantity(value):\n    return 1 if value is None else value\n")
        self.git("add", ".")
        self.write("main.py", "def quantity(value):\n    return 2 if value is None else value\n")
        index_before = (self.repo / ".git/index").read_bytes()
        work_before = (self.repo / "main.py").read_bytes()
        bundle = self.collect()
        self.assertEqual([s["mode"] for s in bundle["scopes"]], ["staged", "worktree"])
        staged, unstaged = [s["files"][0] for s in bundle["scopes"]]
        self.assertIn("+    return 1 if", staged["patch"])
        self.assertIn("-    return 1 if", unstaged["patch"])
        self.assertIn("+    return 2 if", unstaged["patch"])
        self.assertEqual(index_before, (self.repo / ".git/index").read_bytes())
        self.assertEqual(work_before, (self.repo / "main.py").read_bytes())

    def test_root_commit_and_range_ignore_current_worktree(self):
        self.write("main.py", "unrelated dirty content\n")
        root = self.collect("commit", commit=self.root_commit)
        self.assertIsNone(root["scopes"][0]["selected_parent"])
        file = self.all_files(root)[0]
        self.assertEqual((file["additions"], file["deletions"]), (2, 0))
        self.assertNotIn("dirty", file["patch"])
        same = self.collect("range", base=self.root_commit, head=self.root_commit)
        self.assertEqual(self.all_files(same), [])

    def test_committed_range_and_pinned_revision(self):
        self.write("main.py", "def quantity(value):\n    return value\n")
        self.git("commit", "-qam", "Preserve values")
        target = self.git("rev-parse", "HEAD").decode().strip()
        bundle = self.collect("range", base=self.root_commit, head="HEAD")
        self.assertEqual(bundle["scopes"][0]["target"], target)
        self.assertEqual(self.all_files(bundle)[0]["deletions"], 1)

    def test_rename_binary_delete_and_mode_change(self):
        self.write("old name.txt", "same text\n")
        self.write("binary.bin", b"\x00\x01old")
        self.write("deleted.txt", "remove me\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture files")
        self.git("mv", "old name.txt", "new name.txt")
        self.write("binary.bin", b"\x00\x02new")
        self.git("rm", "deleted.txt")
        self.git("add", "binary.bin")
        self.git("update-index", "--chmod=+x", "main.py")
        files = self.all_files(self.collect("staged"))
        by_path = {f["new_path"] or f["old_path"]: f for f in files}
        self.assertEqual(by_path["new name.txt"]["status"], "R100")
        self.assertEqual(by_path["new name.txt"]["hunks"], [])
        self.assertTrue(by_path["binary.bin"]["binary"])
        self.assertEqual(by_path["deleted.txt"]["deletions"], 1)
        self.assertEqual(by_path["main.py"]["new_mode"], "100755")
        self.assertEqual(len(files), 4)

    def test_unicode_and_literal_pathspec(self):
        for path in ("说明 [1].txt", "说明 1.txt"):
            self.write(path, "旧值\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Unicode paths")
        self.write("说明 [1].txt", "新值\n")
        self.write("说明 1.txt", "别的值\n")
        files = self.all_files(self.collect("worktree", path=["说明 [1].txt"]))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["new_path"], "说明 [1].txt")
        self.assertIn("+新值", files[0]["patch"])

    def test_untracked_empty_binary_and_no_newline(self):
        self.write("empty.txt", "")
        self.write("note.txt", "no final newline")
        self.write("image.bin", b"\x00image")
        files = self.all_files(self.collect("worktree", include_untracked=True))
        self.assertEqual(len(files), 3)
        by_path = {file["new_path"]: file for file in files}
        self.assertEqual(by_path["empty.txt"]["additions"], 0)
        self.assertTrue(by_path["image.bin"]["binary"])
        self.assertTrue(self.changed_rows(by_path["note.txt"])[0]["no_newline"])

    def test_empty_repo_with_staged_first_file(self):
        fresh = self.repo / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "-C", str(fresh), "init", "-q"], check=True)
        (fresh / "first.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(fresh), "add", "."], check=True)
        values = dict(repo=str(fresh), mode="staged", commit=None, parent=1,
                      base=None, head=None, path=[], context=3, encoding="utf-8",
                      include_untracked=False)
        bundle = build_bundle(argparse.Namespace(**values))
        self.assertIsNone(bundle["repository_head"])
        self.assertEqual(self.all_files(bundle)[0]["additions"], 1)

    def test_merge_parents(self):
        self.git("checkout", "-qb", "side")
        self.write("side.txt", "side\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Side")
        self.git("checkout", "-qb", "mainline", self.root_commit)
        self.write("mainline.txt", "mainline\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Mainline")
        self.git("merge", "--no-ff", "-m", "Merge side", "side")
        first = self.collect("commit", commit="HEAD")
        second = self.collect("commit", commit="HEAD", parent=2)
        self.assertEqual(self.all_files(first)[0]["new_path"], "side.txt")
        self.assertEqual(self.all_files(second)[0]["new_path"], "mainline.txt")
        self.assertEqual(len(first["scopes"][0]["parents"]), 2)
        with self.assertRaises(DiffError):
            self.collect("commit", commit="HEAD", parent=3)

    def test_conflicts_are_not_presented_as_ordinary_diffs(self):
        self.git("checkout", "-qb", "side")
        self.write("main.py", "side\n")
        self.git("commit", "-qam", "Side")
        self.git("checkout", "-qb", "mainline", self.root_commit)
        self.write("main.py", "mainline\n")
        self.git("commit", "-qam", "Mainline")
        self.git("merge", "side", allowed=(1,))
        with self.assertRaisesRegex(DiffError, "conflicts"):
            self.collect()

    def test_multi_hunk_zero_context_and_newline_marker(self):
        self.write("lines.txt", "".join(f"line {i}\n" for i in range(20)))
        self.git("add", ".")
        self.git("commit", "-qm", "Lines")
        self.write("lines.txt", "changed\n" + "".join(f"line {i}\n" for i in range(1, 19)) + "last")
        bundle = self.collect("worktree", context=0)
        file = self.all_files(bundle)[0]
        self.assertEqual(len(file["hunks"]), 2)
        self.assertEqual(file["additions"], 2)
        self.assertEqual(file["deletions"], 2)
        self.assertTrue(file["hunks"][-1]["lines"][-1]["no_newline"])
        self.assertEqual(file["hunks"][-1]["lines"][-1]["new_line"], 20)
        template = annotation_template(bundle)
        self.assertEqual(template["schema_version"], 2)
        self.assertEqual([note["hunk_id"] for note in template["files"][0]["notes"]],
                         [hunk["id"] for hunk in file["hunks"]])
        parsed = Tags()
        parsed.feed(render(bundle, self.annotations(bundle)))
        self.assertEqual(sum(attrs.get("class") == "block-annotation" for _, attrs in parsed.tags), 2)

    def test_binary_snapshot_hash_tracks_current_bytes(self):
        self.write("binary.bin", b"\x00original")
        self.git("add", ".")
        self.git("commit", "-qm", "Binary")
        self.write("binary.bin", b"\x00first")
        first = self.collect("worktree")
        self.write("binary.bin", b"\x00second")
        second = self.collect("worktree")
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])

    def test_encoding_requires_explicit_choice(self):
        self.write("旧编码.txt", "旧值\n".encode("gbk"))
        self.git("add", ".")
        self.git("commit", "-qm", "Legacy")
        self.write("旧编码.txt", "新值\n".encode("gbk"))
        with self.assertRaises(DiffError):
            self.collect("worktree")
        file = self.all_files(self.collect("worktree", encoding="gbk"))[0]
        self.assertIn("+新值", file["patch"])

    def test_file_type_change_keeps_both_patches(self):
        blob = self.git("hash-object", "-w", "--stdin", input_data=b"target.txt").decode().strip()
        self.git("update-index", "--cacheinfo", "120000", blob, "main.py")
        bundle = self.collect("staged")
        file = self.all_files(bundle)[0]
        self.assertEqual(file["status"], "T")
        self.assertEqual(file["new_mode"], "120000")
        self.assertEqual((file["additions"], file["deletions"]), (1, 2))
        validate(bundle, self.annotations(bundle))

    def test_shallow_boundary_is_not_misidentified_as_root(self):
        self.write("main.py", "second revision\n")
        self.git("commit", "-qam", "Second")
        current = self.git("rev-parse", "HEAD").decode().strip()
        (self.repo / ".git/shallow").write_text(current + "\n", encoding="ascii")
        bundle = self.collect("commit", commit="HEAD")
        self.assertEqual(bundle["scopes"][0]["parents"], [self.root_commit])
        self.assertEqual(bundle["scopes"][0]["base"], self.root_commit)

    def test_concurrent_changes_are_rejected(self):
        import collect_diff
        original = collect_diff.collect_scope
        self.write("main.py", "first\n")
        calls = 0

        def changing(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                self.write("main.py", "second\n")
            return result

        with patch.object(collect_diff, "collect_scope", changing):
            with self.assertRaisesRegex(DiffError, "changed during collection"):
                self.collect("worktree")

    def test_gitlink_pointer_change(self):
        self.write("other.txt", "second revision\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Second")
        second = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "160000", self.root_commit, "vendor/module")
        self.git("commit", "-qm", "Add gitlink")
        self.git("update-index", "--cacheinfo", "160000", second, "vendor/module")
        file = self.all_files(self.collect("staged"))[0]
        self.assertTrue(file["submodule"])
        self.assertEqual((file["additions"], file["deletions"]), (1, 1))

    def test_external_diff_is_disabled(self):
        self.git("config", "diff.external", "nonexistent-command-that-must-never-run")
        self.write("main.py", "changed\n")
        self.assertEqual(len(self.all_files(self.collect())), 1)

    def test_missing_duplicate_unknown_and_stale_annotations_rejected(self):
        self.write("main.py", "changed\n")
        bundle = self.collect()
        complete = self.annotations(bundle)
        validate(bundle, complete)
        for kind in ("missing", "duplicate", "unknown", "stale", "blank", "missing_file",
                     "line_annotation", "old_schema"):
            value = copy.deepcopy(complete)
            if kind == "missing":
                value["files"][0]["notes"].pop()
            elif kind == "duplicate":
                value["files"][0]["notes"].append(value["files"][0]["notes"][0])
            elif kind == "unknown":
                value["files"][0]["notes"][0]["hunk_id"] = "s999-f1-h1"
            elif kind == "stale":
                value["bundle_id"] = "stale"
            elif kind == "blank":
                value["files"][0]["notes"][0]["impact"] = ""
            elif kind == "line_annotation":
                value["files"][0]["notes"][0]["line_id"] = "s1-f1-h1-l1"
            elif kind == "old_schema":
                value["schema_version"] = 1
            else:
                value["files"] = []
            with self.subTest(kind=kind), self.assertRaises(DiffError):
                validate(bundle, value)

    def test_html_escapes_code_and_annotations(self):
        attack = '</script><img src="https://example.invalid" onerror="alert(1)">'
        self.write("main.py", attack + "\n")
        bundle = self.collect()
        annotations = self.annotations(bundle)
        annotations["summary"] = attack
        html = render(bundle, annotations)
        parsed = Tags()
        parsed.feed(html)
        self.assertNotIn("img", [tag for tag, attrs in parsed.tags])
        self.assertEqual([tag for tag, attrs in parsed.tags].count("script"), 1)
        self.assertIn("&lt;/script&gt;&lt;img", html)
        self.assertFalse(any("onerror" in attrs for tag, attrs in parsed.tags))
        self.assertIn("Content-Security-Policy", html)
        self.assertEqual(sum("data-source-id" in attrs for _, attrs in parsed.tags),
                         sum(2 if row["kind"] == "context" else 1
                             for file in self.all_files(bundle) for hunk in file["hunks"]
                             for row in hunk["lines"]))

    def test_side_by_side_html_preserves_each_version(self):
        self.write("main.py", "head\nold one\nold two\ntail\n")
        self.git("commit", "-qam", "Before replacement")
        self.write("main.py", "head\nnew one\nnew two\nnew three\ntail\n")
        bundle = self.collect("worktree")
        parsed = SourceCells()
        parsed.feed(render(bundle, self.annotations(bundle)))
        file = self.all_files(bundle)[0]
        for side, excluded, number in (("old", "add", "old_line"), ("new", "delete", "new_line")):
            expected = [(row[number], row["text"])
                        for hunk in file["hunks"] for row in hunk["lines"] if row["kind"] != excluded]
            actual = [(int(cell["data-line"]), cell["text"]) for cell in parsed.cells
                      if cell["data-side"] == side]
            self.assertEqual(actual, expected)
        self.assertEqual(sum(attrs.get("class") == "block-annotation" for _, attrs in parsed.tags), 1)
        self.assertTrue(any("placeholder old" in attrs.get("class", "") for _, attrs in parsed.tags))
        ids = [attrs["id"] for _, attrs in parsed.tags if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(any(tag == "td" and "annotation" in attrs.get("class", "")
                             for tag, attrs in parsed.tags))

    def test_empty_diff_renders_and_snapshot_stays_stable(self):
        first = self.collect()
        second = self.collect()
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        self.assertIn("该范围没有改动", render(first, self.annotations(first)))

    def test_malformed_hunk_rejected(self):
        with self.assertRaises(DiffError):
            parse_patch("@@ -1,2 +1,1 @@\n-old\n+new\n", "s1-f1")


class SplitViewTests(unittest.TestCase):
    @staticmethod
    def hunk(text):
        return parse_patch(text, "s1-f1")[0][0]

    def test_context_aligns_multiple_edit_runs(self):
        hunk = self.hunk("@@ -10,5 +20,6 @@\n keep\n-old one\n-old two\n+new one\n anchor\n+extra one\n+extra two\n tail\n")
        pairs = split_rows(hunk)
        self.assertEqual([(old["text"] if old else None, new["text"] if new else None)
                          for old, new in pairs],
                         [("keep", "keep"), ("old one", "new one"), ("old two", None),
                          ("anchor", "anchor"), (None, "extra one"), (None, "extra two"), ("tail", "tail")])
        self.assertEqual([old["old_line"] for old, _ in pairs if old], list(range(10, 15)))
        self.assertEqual([new["new_line"] for _, new in pairs if new], list(range(20, 26)))

    def test_pure_additions_and_deletions_have_empty_opposite_side(self):
        added = split_rows(self.hunk("@@ -0,0 +1,2 @@\n+one\n+two\n"))
        removed = split_rows(self.hunk("@@ -1,2 +0,0 @@\n-one\n-two\n"))
        self.assertTrue(all(old is None for old, _ in added))
        self.assertTrue(all(new is None for _, new in removed))
        self.assertEqual([new["new_line"] for _, new in added], [1, 2])
        self.assertEqual([old["old_line"] for old, _ in removed], [1, 2])

    def test_shorter_replacement_preserves_remaining_deletions(self):
        pairs = split_rows(self.hunk("@@ -1,3 +1 @@\n-old1\n-old2\n-old3\n+new1\n"))
        self.assertEqual(len(pairs), 3)
        self.assertIsNone(pairs[1][1])
        self.assertIsNone(pairs[2][1])
        self.assertEqual([old["text"] for old, _ in pairs], ["old1", "old2", "old3"])

    def test_inline_highlights_preserve_and_escape_source(self):
        before = 'return old < "<script>" & value'
        after = 'return new < "<img>" & value'
        old_html, new_html = highlighted_pair(before, after)
        for side, text, markup in (("old", before, old_html), ("new", after, new_html)):
            parsed = SourceCells()
            parsed.feed(f'<table><tr><td data-source-id="test" data-side="{side}" data-line="1"><pre>{markup}</pre></td></tr></table>')
            self.assertEqual(parsed.cells[0]["text"], text)
            self.assertTrue(any(tag == "mark" for tag, _ in parsed.tags))
            self.assertFalse(any(tag in ("script", "img") for tag, _ in parsed.tags))

    def test_long_lines_skip_character_matching_without_losing_text(self):
        before, after = "<" * 5000, ">" * 5001
        old, new = highlighted_pair(before, after)
        self.assertEqual(old, "&lt;" * 5000)
        self.assertEqual(new, "&gt;" * 5001)

    def test_newline_markers_and_context_ids_survive_split_rendering(self):
        hunk = self.hunk("@@ -1,2 +1,2 @@\n keep\r\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n")
        file = {"old_path": "old.txt", "new_path": "new.txt"}
        html = hunk_html(hunk, file, None, 1)
        parsed = SourceCells()
        parsed.feed(html)
        self.assertEqual([cell["text"] for cell in parsed.cells], ["keep", "keep", "old", "new"])
        self.assertEqual(html.count("文件末尾无换行"), 2)
        self.assertEqual(html.count("CRLF 行尾"), 2)
        ids = [attrs["id"] for _, attrs in parsed.tags if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
