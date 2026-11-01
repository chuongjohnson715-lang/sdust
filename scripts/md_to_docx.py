"""Markdown → Word(.docx) 转换器（本机 Word COM，不装任何新依赖）。

用法：
  python scripts/md_to_docx.py <a.md> [<b.md> ...]
  python scripts/md_to_docx.py --html-only <a.md>   # 只产 HTML 便于检查

实现：先渲染成带内联样式的 HTML（表格、引用块、列表、行内代码都保留），
再用 WINWORD.EXE 的 COM 接口「打开 HTML → 另存为 .docx」。
"""
import html as H
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CSS = """
body { font-family: "等线", "Microsoft YaHei", sans-serif; font-size: 10.5pt; line-height: 1.5; }
h1 { font-size: 18pt; font-weight: bold; margin: 12pt 0 8pt; }
h2 { font-size: 14pt; font-weight: bold; margin: 12pt 0 6pt; border-bottom: 1px solid #bbb; }
h3 { font-size: 12pt; font-weight: bold; margin: 10pt 0 4pt; }
h4 { font-size: 11pt; font-weight: bold; margin: 8pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; }
th, td { border: 1px solid #a0a0a0; padding: 3pt 5pt; vertical-align: top; font-size: 10pt; }
th { background: #f0f0f0; font-weight: bold; }
blockquote { margin: 6pt 0; padding: 4pt 10pt; border-left: 3px solid #bbb;
             color: #444; background: #fafafa; }
code { font-family: Consolas, monospace; font-size: 9.5pt; background: #f2f2f2; }
hr { border: 0; border-top: 1px solid #ccc; margin: 10pt 0; }
ul, ol { margin: 4pt 0 4pt 18pt; }
"""


def inline(text):
    """行内语法：先转义，再还原行内代码/链接/粗体/斜体/原生 HTML 标签。"""
    keep = []

    def stash(fragment):
        keep.append(fragment)
        return f"\x00{len(keep) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{H.escape(m.group(1))}</code>"), text)
    text = re.sub(r"<br\s*/?>", lambda m: stash("<br/>"), text, flags=re.I)
    text = H.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def split_row(line):
    cells = line.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [c.strip() for c in cells.split("|")]


SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def convert(md_text):
    lines = md_text.replace("\r\n", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            out.append("<hr/>")
            i += 1
            continue

        if line.lstrip().startswith("|") and i + 1 < len(lines) and SEP.match(lines[i + 1].strip()):
            head = split_row(line)
            aligns = []
            for spec in split_row(lines[i + 1]):
                spec = spec.strip()
                if spec.startswith(":") and spec.endswith(":"):
                    aligns.append("center")
                elif spec.endswith(":"):
                    aligns.append("right")
                else:
                    aligns.append("left")
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            parts = ["<table><tr>"]
            for k, c in enumerate(head):
                al = aligns[k] if k < len(aligns) else "left"
                parts.append(f'<th style="text-align:{al}">{inline(c)}</th>')
            parts.append("</tr>")
            for r in rows:
                parts.append("<tr>")
                for k, c in enumerate(r):
                    al = aligns[k] if k < len(aligns) else "left"
                    parts.append(f'<td style="text-align:{al}">{inline(c)}</td>')
                parts.append("</tr>")
            parts.append("</table>")
            out.append("".join(parts))
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + "<br/>".join(inline(b) for b in buf if b.strip()) + "</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            ordered = bool(re.match(r"\d", m.group(2)))
            tag = "ol" if ordered else "ul"
            items = []
            depth_base = len(m.group(1))
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not mm:
                    break
                indent = max(0, (len(mm.group(1)) - depth_base) // 2)
                items.append((indent, mm.group(3)))
                i += 1
            html_items, level = [], 0
            for indent, text in items:
                while level < indent:
                    html_items.append(f"<{tag}>")
                    level += 1
                while level > indent:
                    html_items.append(f"</{tag}>")
                    level -= 1
                html_items.append(f"<li>{inline(text)}</li>")
            while level > 0:
                html_items.append(f"</{tag}>")
                level -= 1
            block = "".join(html_items)
            if not block.startswith(f"<{tag}>"):
                block = f"<{tag}>" + block
            if not block.endswith(f"</{tag}>"):
                block += f"</{tag}>"
            out.append(block)
            continue

        out.append(f"<p>{inline(line)}</p>")
        i += 1

    return ("<html><head><meta charset=\"utf-8\"/><style>" + CSS + "</style></head><body>"
            + "\n".join(out) + "</body></html>")


PS1 = r"""
param([string]$In, [string]$Out)
$ErrorActionPreference = "Stop"
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
  $doc = $word.Documents.Open($In, $false, $true)
  # HTML 默认落成「Web 版式」，改成页面视图并按 A4 排版
  $doc.PageSetup.PageWidth = 595.3
  $doc.PageSetup.PageHeight = 841.9
  $doc.PageSetup.TopMargin = 56.7
  $doc.PageSetup.BottomMargin = 56.7
  $doc.PageSetup.LeftMargin = 56.7
  $doc.PageSetup.RightMargin = 56.7
  try { $doc.ActiveWindow.View.Type = 3 } catch {}
  $doc.SaveAs2($Out, 16)
  $doc.Close($false)
  Write-Output "ok"
} finally {
  $word.Quit()
}
"""


def to_docx(md_path: Path):
    md_text = md_path.read_text(encoding="utf-8")
    docx_path = md_path.with_suffix(".docx")
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / "page.html"
        html_path.write_text(convert(md_text), encoding="utf-8")
        ps1 = Path(td) / "md2docx.ps1"
        ps1.write_text(PS1, encoding="utf-8-sig")
        if docx_path.exists():
            docx_path.unlink()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
             "-In", str(html_path), "-Out", str(docx_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    if proc.returncode != 0 or not docx_path.exists():
        raise SystemExit(f"[fail] {md_path}\n{proc.stdout}\n{proc.stderr}")
    return docx_path


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    html_only = "--html-only" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    for a in args:
        p = Path(a).resolve()
        if html_only:
            out = p.with_suffix(".preview.html")
            out.write_text(convert(p.read_text(encoding="utf-8")), encoding="utf-8")
            print(f"[html] {out}")
        else:
            out = to_docx(p)
            print(f"[docx] {out.name} {os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main()
