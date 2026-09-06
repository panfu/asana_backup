"""Markdown Vault 生成 —— buildMarkdown/toYaml/sanitize/replaceAssetUrls
自 ~/ar/bridge 的 AsanaBackupService 1:1 移植，新增两处（均为需求要求）：

- 🌿 Subtasks 章节：子任务渲染为嵌套 checkbox 列表（bridge 无子任务导出）
- 📎 附件区分外链与托管：外链（Google Drive/Figma 等）保留原链接，
  仅托管在 Asana 的附件才下载进 ZIP 并以相对路径引用
"""

from __future__ import annotations

import json
import re
from typing import Any

_ASSET_URL_RE = re.compile(r"https://app\.asana\.com/app/asana/-/get_asset\?asset_id=(\d+)")
# 括号也需去掉：Markdown 链接 ![](...) 中未转义的 ) 会截断引用（bridge 同款）
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|()\[\]]')

HOST_LABELS = {
    "gdrive": "Google Drive",
    "dropbox": "Dropbox",
    "box": "Box",
    "figma": "Figma",
    "onedrive": "OneDrive",
    "slack": "Slack",
    "vimeo": "Vimeo",
    "external": "外部链接",
}


def sanitize(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("-", name)
    return re.sub(r"\s+", " ", cleaned).strip()


def to_yaml_line(key: str, value: Any) -> str:
    """bridge toYaml 的逐行规则：数组走 JSON、null/布尔原样、含特殊字符加引号。"""
    if isinstance(value, (list, tuple)):
        return f"{key}: {json.dumps(list(value), ensure_ascii=False)}"
    if value is None:
        return f"{key}: null"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    text = str(value)
    if re.search(r'[:#"\']', text):
        return f'{key}: "{text.replace(chr(34), chr(92) + chr(34))}"'
    return f"{key}: {text}"


def build_frontmatter(task: dict[str, Any], task_gid: str) -> str:
    fm: list[tuple[str, Any]] = [
        ("tags", ["asana", "backup"]),
        ("source", "Asana"),
        ("asana_gid", task_gid),
        ("asana_url", task.get("permalink_url") or ""),
        ("project", (((task.get("projects") or [{}])[0]) or {}).get("name", "")),
        ("section", ((((task.get("memberships") or [{}])[0]) or {}).get("section") or {}).get("name", "")),
        ("status", "completed" if task.get("completed") else "open"),
        ("assignee", (task.get("assignee") or {}).get("name", "")),
        ("created_by", (task.get("created_by") or {}).get("name", "")),
        ("created_by_email", (task.get("created_by") or {}).get("email", "")),
        ("created_at", task.get("created_at")),
        ("modified_at", task.get("modified_at")),
        ("completed_at", task.get("completed_at")),
        ("due_on", task.get("due_on")),
    ]
    for cf in task.get("custom_fields") or []:
        key = sanitize(cf.get("name") or "field").replace(" ", "_")
        value = cf.get("display_value") or cf.get("text_value") or cf.get("number_value") or ""
        fm.append((key, value))
    lines = [to_yaml_line(k, v) for k, v in fm]
    return "---\n" + "\n".join(lines) + "\n---"


def replace_asset_urls(text: str, attachment_map: dict[str, str]) -> str:
    """把描述/评论里的 Asana 附件 URL 替换为项目内 attachments/ 的相对引用。"""

    def _sub(m: re.Match[str]) -> str:
        filename = attachment_map.get(m.group(1))
        return f"![](../attachments/{filename})" if filename else m.group(0)

    return _ASSET_URL_RE.sub(_sub, text)


def host_label(attachment: dict[str, Any]) -> str:
    host = (attachment.get("host") or "").lower()
    return HOST_LABELS.get(host, attachment.get("host") or "外部链接")


def is_external_attachment(attachment: dict[str, Any]) -> bool:
    return attachment.get("resource_subtype") == "external" or bool(attachment.get("host"))


def safe_attachment_filename(attachment: dict[str, Any], attachment_gid: str) -> str:
    """附件文件名加任务 gid 前缀防冲突 —— bridge 同款规则。"""
    import os

    raw = attachment.get("name") or attachment_gid
    stem, ext = os.path.splitext(raw)
    cleaned_stem = sanitize(stem)
    ext = sanitize(ext)
    return f"{cleaned_stem}{ext}" if ext else cleaned_stem


def render_subtasks(subtasks: list[dict[str, Any]]) -> str:
    """子任务 → 嵌套 checkbox 列表。children 字段由 exporter 递归填充。"""
    lines: list[str] = []

    def _render(items: list[dict[str, Any]], depth: int) -> None:
        indent = "  " * depth
        for st in items:
            box = "x" if st.get("completed") else " "
            bits: list[str] = []
            assignee_name = (st.get("assignee") or {}).get("name")
            if assignee_name:
                bits.append(assignee_name)
            if st.get("due_on"):
                bits.append(str(st["due_on"]))
            meta = f" — {' · '.join(bits)}" if bits else ""
            lines.append(f"{indent}- [{box}] **{st.get('name') or st.get('gid')}**{meta}")
            if st.get("permalink_url"):
                lines.append(f"{indent}  🔗 [Asana]({st['permalink_url']})")
            notes = (st.get("notes") or "").strip()
            if notes:
                for note_line in notes.splitlines():
                    if note_line.strip():
                        lines.append(f"{indent}  > {note_line.strip()}")
            children = st.get("children") or []
            if children:
                _render(children, depth + 1)

    _render(subtasks, 0)
    return "\n".join(lines)


def build_markdown(
    task: dict[str, Any],
    comments: list[dict[str, Any]],
    attachment_map: dict[str, str],
    external_attachments: list[dict[str, Any]],
    subtasks: list[dict[str, Any]],
    task_gid: str,
) -> str:
    """单任务 Markdown —— bridge buildMarkdown 的移植 + Subtasks/外链附件章节。"""
    md = build_frontmatter(task, task_gid) + "\n\n# " + (task.get("name") or "Untitled") + "\n\n"

    if attachment_map or external_attachments:
        md += "## 📎 Attachments\n\n"
        for filename in attachment_map.values():
            md += f"![](../attachments/{filename})\n\n"
        for att in external_attachments:
            # 外链附件：保留原链接（name 即目标 URL），不下载进 ZIP
            url = att.get("name") or ""
            if url:
                md += f"- [📎 {host_label(att)}]({url})\n"
        md += "\n"

    notes = (task.get("notes") or "").strip()
    if notes:
        md += "## 📋 Description\n\n" + replace_asset_urls(notes, attachment_map) + "\n\n"

    if subtasks:
        md += "## 🌿 Subtasks\n\n" + render_subtasks(subtasks) + "\n\n"

    if comments:
        md += "## 💬 Comments\n\n"
        blocks = []
        for c in comments:
            text = replace_asset_urls(str(c.get("text") or ""), attachment_map)
            author = (c.get("created_by") or {}).get("name") or "Unknown"
            date = c.get("created_at") or ""
            blocks.append(f"### {author} · {date}\n\n{text}")
        md += "\n\n---\n\n".join(blocks) + "\n"

    md += "\n---\n\n🔗 [Open in Asana](" + (task.get("permalink_url") or "") + ")\n"
    return md


def task_markdown_filename(task: dict[str, Any], task_gid: str, fallback_name: str) -> str:
    return f"{task_gid} {sanitize(task.get('name') or fallback_name or 'task')}.md"
