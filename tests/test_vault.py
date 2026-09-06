"""vault.py 的单元测试 —— 对齐 bridge AsanaBackupService 的输出约定。"""

from __future__ import annotations

from app.vault import (
    build_markdown,
    replace_asset_urls,
    safe_attachment_filename,
    sanitize,
    task_markdown_filename,
    to_yaml_line,
)


class TestSanitize:
    def test_removes_unsafe_chars(self):
        assert sanitize('a/b\\c:d*e?f"g<h>i|j') == "a-b-c-d-e-f-g-h-i-j"

    def test_removes_parens_and_brackets(self):
        # bridge 注释：Markdown 链接 ![](...) 中未转义的 ) 会截断引用
        assert sanitize("Report (final) [v2]") == "Report -final- -v2-"

    def test_collapses_whitespace(self):
        assert sanitize("  a \n b\t c  ") == "a b c"


class TestToYaml:
    def test_null_and_bool(self):
        assert to_yaml_line("x", None) == "x: null"
        assert to_yaml_line("x", True) == "x: true"
        assert to_yaml_line("x", False) == "x: false"

    def test_array_uses_json(self):
        assert to_yaml_line("tags", ["asana", "backup"]) == 'tags: ["asana", "backup"]'

    def test_quotes_special_chars(self):
        assert to_yaml_line("k", 'say "hi": #ok') == 'k: "say \\"hi\\": #ok"'

    def test_plain_passthrough(self):
        assert to_yaml_line("k", "plain-value") == "k: plain-value"

    def test_iso_timestamp_gets_quoted(self):
        # bridge 行为：含 : 的值（如 ISO 时间戳）加引号
        assert to_yaml_line("k", "2026-09-06T10:00:00.000Z") == 'k: "2026-09-06T10:00:00.000Z"'


class TestReplaceAssetUrls:
    def test_replaces_known_asset(self):
        out = replace_asset_urls(
            "见 https://app.asana.com/app/asana/-/get_asset?asset_id=123456",
            {"123456": "999_photo.jpg"},
        )
        assert out == "见 ![](../attachments/999_photo.jpg)"

    def test_keeps_unknown_asset(self):
        text = "https://app.asana.com/app/asana/-/get_asset?asset_id=777"
        assert replace_asset_urls(text, {"123": "f.jpg"}) == text


class TestAttachmentFilename:
    def test_keeps_extension(self):
        att = {"name": "Fabric order (final).xlsx"}
        assert safe_attachment_filename(att, "1") == "Fabric order -final-.xlsx"

    def test_no_extension(self):
        att = {"name": "screenshot"}
        assert safe_attachment_filename(att, "1") == "screenshot"


class TestBuildMarkdown:
    TASK = {
        "name": "下单 660302 1600",
        "completed": False,
        "notes": "描述内容 https://app.asana.com/app/asana/-/get_asset?asset_id=111",
        "permalink_url": "https://app.asana.com/0/1/2",
        "created_by": {"name": "Fred", "email": "fred@example.com"},
        "assignee": {"name": "Agnes"},
        "due_on": "2026-09-10",
        "created_at": "2026-09-01T00:00:00.000Z",
        "modified_at": "2026-09-02T00:00:00.000Z",
        "completed_at": None,
        "projects": [{"gid": "1", "name": "IT"}],
        "memberships": [{"section": {"name": "To Do"}}],
        "custom_fields": [{"name": "Priority", "display_value": "High"}],
    }

    def _build(self, **kw):
        defaults = dict(
            task=self.TASK,
            comments=[
                {
                    "text": "评论带附件 https://app.asana.com/app/asana/-/get_asset?asset_id=111",
                    "created_by": {"name": "Pan"},
                    "created_at": "2026-09-03T00:00:00.000Z",
                }
            ],
            attachment_map={"111": "222_doc.pdf"},
            external_attachments=[
                {"name": "https://drive.google.com/file/x", "resource_subtype": "external", "host": "gdrive"},
            ],
            subtasks=[
                {
                    "gid": "9",
                    "name": "子任务A",
                    "completed": True,
                    "assignee": {"name": "Zoe"},
                    "due_on": "2026-09-05",
                    "permalink_url": "https://app.asana.com/0/1/9",
                    "notes": "子任务备注",
                    "children": [{"gid": "10", "name": "孙任务", "completed": False, "notes": ""}],
                }
            ],
            task_gid="222",
        )
        defaults.update(kw)
        return build_markdown(**defaults)

    def test_frontmatter_fields(self):
        md = self._build()
        assert md.startswith("---\n")
        assert 'tags: ["asana", "backup"]' in md
        assert "source: Asana" in md
        assert "asana_gid: 222" in md
        assert "project: IT" in md
        assert "section: To Do" in md
        assert "status: open" in md
        assert "assignee: Agnes" in md
        assert "Priority: High" in md
        assert "completed_at: null" in md
        assert "due_on: 2026-09-10" in md

    def test_body_sections(self):
        md = self._build()
        assert "# 下单 660302 1600" in md
        assert "## 📎 Attachments" in md
        assert "![](../attachments/222_doc.pdf)" in md
        assert "- [📎 Google Drive](https://drive.google.com/file/x)" in md
        assert "## 📋 Description" in md
        assert "## 💬 Comments" in md
        assert "### Pan · 2026-09-03T00:00:00.000Z" in md
        assert "🔗 [Open in Asana](https://app.asana.com/0/1/2)" in md

    def test_asset_url_replacement_in_notes_and_comments(self):
        md = self._build()
        assert "get_asset?asset_id=111" not in md

    def test_subtasks_rendering(self):
        md = self._build()
        assert "## 🌿 Subtasks" in md
        assert "- [x] **子任务A** — Zoe · 2026-09-05" in md
        assert "> 子任务备注" in md
        assert "  - [ ] **孙任务**" in md

    def test_empty_optional_parts(self):
        md = self._build(comments=[], attachment_map={}, external_attachments=[], subtasks=[])
        for absent in ("Attachments", "Comments", "Subtasks"):
            assert absent not in md
        assert "## 📋 Description" in md  # notes 非空仍渲染

    def test_null_valued_fields_do_not_crash(self):
        # 真实数据回归：Asana 会返回键存在但值为 null 的字段（assignee/memberships 等）
        task = {
            "name": "Null task",
            "completed": False,
            "notes": "",
            "permalink_url": "u",
            "assignee": None,
            "created_by": None,
            "projects": [None],
            "memberships": [None],
            "created_at": None,
            "modified_at": None,
            "completed_at": None,
            "due_on": None,
            "custom_fields": [{"name": "F", "display_value": None}],
        }
        md = build_markdown(
            task=task,
            comments=[],
            attachment_map={},
            external_attachments=[],
            subtasks=[
                {
                    "gid": "s",
                    "name": "sub",
                    "completed": False,
                    "assignee": None,
                    "due_on": None,
                    "notes": "n",
                    "permalink_url": None,
                }
            ],
            task_gid="1",
        )
        assert "project: " in md
        assert "- [ ] **sub**" in md


class TestTaskFilename:
    def test_gid_prefix_and_sanitize(self):
        task = {"name": "Task: (urgent)"}
        assert task_markdown_filename(task, "123", "") == "123 Task- -urgent-.md"

    def test_fallback_name(self):
        assert task_markdown_filename({}, "123", "原始名") == "123 原始名.md"
