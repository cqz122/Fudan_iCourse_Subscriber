import re
import smtplib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from io import BytesIO
from PIL import Image
from collections import OrderedDict
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from html import escape
from email.utils import formataddr
from urllib.parse import quote

import markdown
from pygments.formatters import HtmlFormatter

from src.runtime import config

_MD_EXTENSIONS= ["tables", "fenced_code", "nl2br", "sane_lists", "codehilite"]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "linenums": False,
        "css_class": "highlight",
    }
}

_PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".highlight")

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f8f8f8;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[str, tuple] = {}


def _fetch_latex_image(url: str, dpi: int = 300) -> tuple:
    """Fetch rendered LaTeX image, return (width, height, png_bytes).

    Width/height are logical display pixels (DPI-adjusted).
    Returns (None, None, None) on failure.
    """
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]

    try:
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))

        result = (logical_width, logical_height, response.content)
        _IMAGE_CACHE[url] = result
        return result
    except Exception as e:
        print(f"[LaTeX Render] Image fetch failed: {e}")
        return None, None, None


def _prefetch_latex_images(urls: list[str], dpi: int = 300) -> None:
    """Pre-fetch multiple LaTeX images concurrently.

    Results are stored in ``_IMAGE_CACHE`` so that subsequent calls to
    ``_fetch_latex_image`` become instant cache hits.
    """
    uncached = [u for u in urls if u not in _IMAGE_CACHE]
    if not uncached:
        return
    with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
        futures = {pool.submit(_fetch_latex_image, u, dpi): u for u in uncached}
        for future in as_completed(futures):
            future.result()  # trigger any exception logging inside _fetch_latex_image


def _md_to_html(md_text: str, cid_images: dict | None = None) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Args:
        md_text: Markdown source text.
        cid_images: When provided (dict), download images and embed via CID
                    references instead of external URLs.  The dict is populated
                    with {cid_name: png_bytes} entries for the caller to attach
                    to the MIME message.
    """
    latex_map: dict[str, str] = {}
    counter = 0

    def _stash(match):
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$$" + match.group(1) + "$$"
        return key

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = "$" + match.group(1) + "$"
        return key

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )

    # Build URL list and pre-fetch all LaTeX images concurrently
    # Each entry: (url, latex_content, is_block)
    latex_info: dict[str, tuple[str, str, bool]] = {}
    for key, original in latex_map.items():
        is_block = original.startswith("$$")
        latex_content = original[2:-2] if is_block else original[1:-1]
        prefix = r"\dpi{300}\bg{white}" if is_block else r"\dpi{300}\bg{white}\inline"
        url = f"https://latex.codecogs.com/png.latex?{prefix}%20{quote(latex_content)}"
        latex_info[key] = (url, latex_content, is_block)

    _prefetch_latex_images([info[0] for info in latex_info.values()])

    for key, (url, latex_content, is_block) in latex_info.items():
        w, h, img_data = _fetch_latex_image(url)

        if is_block:
            if w and h:
                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:middle;border:none;display:inline-block;">'
                    f'</div>'
                )
            else:
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<code>{escape(latex_content)}</code></div>'
                )
        else:
            if w and h:
                # Enforce minimum height so formulas aren't smaller than text
                if h < _MIN_INLINE_HEIGHT:
                    scale = _MIN_INLINE_HEIGHT / h
                    w = max(1, int(w * scale))
                    h = _MIN_INLINE_HEIGHT

                src = _resolve_src(url, img_data, cid_images)
                img_tag = (
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:-3px;border:none;margin:0 2px;">'
                )
            else:
                img_tag = f'<code>{escape(latex_content)}</code>'

        html = html.replace(key, img_tag)

    return html


def _resolve_src(url: str, img_data: bytes | None,
                 cid_images: dict | None) -> str:
    """Return a CID reference if embedding, otherwise the original URL."""
    if cid_images is not None and img_data:
        cid = f"latex-{uuid.uuid4().hex[:12]}"
        cid_images[cid] = img_data
        return f"cid:{cid}"
    return url


class Emailer:
    """Send course summary emails via QQ SMTP SSL.

    正文只列课程名 + 课次清单，每门课的 Markdown 笔记单独作为 ``.md``
    附件发送（一门课一个附件，文件名带课程名和日期）。
    """

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

    # ------------------------------------------------------------------
    # 内容构建
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_filename(title: str, fallback: str = "course") -> str:
        """把课程名清洗成合法的文件名（去掉 / \\ : * ? " < > | 等）。"""
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title or "")
        cleaned = cleaned.strip(" ._")
        return (cleaned or fallback)[:80]

    @staticmethod
    def _demote_headings(md_text: str, levels: int = 2) -> str:
        """把摘要里的标题整体降级，避免和「# 课程 / ## 课次」冲突。

        围栏代码块（``` / ~~~）里的内容原样保留，防止注释行被误改。
        """
        if not md_text:
            return ""
        out: list[str] = []
        in_fence = False
        for line in md_text.splitlines():
            if re.match(r"^\s*(```|~~~)", line):
                in_fence = not in_fence
                out.append(line)
                continue
            if not in_fence:
                m = re.match(r"^(#{1,6})(\s+)", line)
                if m:
                    hashes = "#" * min(6, len(m.group(1)) + levels)
                    line = hashes + line[len(m.group(1)):]
            out.append(line)
        return "\n".join(out)

    @classmethod
    def _build_course_markdown(cls, course_title: str, lectures: list[dict]) -> str:
        """一门课的全部课次拼成一个 Markdown 文件。"""
        stamp = time.strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# {course_title}",
            "",
            f"> 本次共 {len(lectures)} 个课次 · 生成于 {stamp}",
            "",
        ]
        for lec in lectures:
            tag = "（更新）" if lec.get("is_update") else ""
            lines.append(f"## {tag}{lec.get('sub_title', '')}（{lec.get('date', '')}）")
            lines.append("")
            lines.append(cls._demote_headings((lec.get("summary") or "").strip()))
            lines.append("")
            lines.append("---")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _build_plain_body(courses: "OrderedDict[str, list[dict]]",
                          files: dict[str, str]) -> str:
        """纯文本正文：只写课程名、课次和附件名。"""
        n_course = len(courses)
        n_lec = sum(len(v) for v in courses.values())
        out = [
            "您好：",
            "",
            f"本次 iCourse 检查发现 {n_course} 门课程、{n_lec} 个课次的新笔记，"
            "已作为 Markdown 附件随信发送。",
            "",
        ]
        for course_title, lectures in courses.items():
            out.append(f"【{course_title}】{len(lectures)} 个课次")
            out.append(f"    附件：{files.get(course_title, '')}")
            for lec in lectures:
                tag = "[更新] " if lec.get("is_update") else ""
                out.append(
                    f"    · {tag}{lec.get('sub_title', '')}"
                    f"（{lec.get('date', '')}）"
                )
            out.append("")
        out.append(f"共 {len(files)} 个 Markdown 附件。")
        return "\n".join(out)

    @staticmethod
    def _build_html_body(courses: "OrderedDict[str, list[dict]]",
                         files: dict[str, str]) -> str:
        """HTML 正文：卡片式列出课程名 + 课次，并标注对应附件名。"""
        n_course = len(courses)
        n_lec = sum(len(v) for v in courses.values())

        badge = (
            '<span style="background:#ff9800;color:#ffffff;border-radius:3px;'
            'font-size:11px;padding:1px 6px;margin-right:6px;'
            'vertical-align:middle;">更新</span>'
        )

        blocks = []
        for i, (course_title, lectures) in enumerate(courses.items(), start=1):
            items = "".join(
                "<li style=\"margin:3px 0;\">{badge}{sub}"
                "<span style=\"color:#95a5a6;\">（{date}）</span></li>".format(
                    badge=badge if lec.get("is_update") else "",
                    sub=escape(str(lec.get("sub_title", ""))),
                    date=escape(str(lec.get("date", ""))),
                )
                for lec in lectures
            )
            blocks.append(
                '<div style="border:1px solid #e6e9ef;border-radius:8px;'
                'padding:14px 18px;margin-bottom:14px;">'
                '<div style="font-size:17px;font-weight:600;color:#2c3e50;">'
                f"{i}. {escape(course_title)}"
                '<span style="font-weight:400;font-size:13px;color:#7f8c8d;">'
                f" · {len(lectures)} 个课次</span></div>"
                '<ul style="margin:8px 0 10px;padding-left:20px;color:#34495e;">'
                f"{items}</ul>"
                '<div style="font-size:13px;color:#3498db;">'
                f"📎 {escape(files.get(course_title, ''))}</div>"
                "</div>"
            )

        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
            "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "Roboto,'Helvetica Neue',Arial,sans-serif;font-size:15px;"
            "line-height:1.7;color:#1a1a1a;max-width:760px;margin:0 auto;"
            "padding:20px;\">"
            '<h2 style="color:#2c3e50;border-bottom:2px solid #3498db;'
            'padding-bottom:8px;margin:0 0 18px;">课程笔记</h2>'
            '<p style="color:#555;margin:0 0 18px;">本次发现 '
            f"<strong>{n_course}</strong> 门课程、<strong>{n_lec}</strong> "
            "个课次的新笔记，Markdown 笔记见下方附件。</p>"
            + "".join(blocks)
            + '<p style="color:#95a5a6;font-size:12px;margin-top:24px;">'
            "由 iCourse Subscriber 自动发送，仅供个人学习使用，请勿转发。"
            "</p></body></html>"
        )

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send(self, items: list[dict]) -> bool:
        """Send a single email containing all course summaries as .md files.

        Args:
            items: List of dicts, each with keys:
                   course_title, sub_title, date, summary
                   Optional key:
                   is_update — bool, True for re-summarized lectures.

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not items:
            return True

        any_update = any(item.get("is_update") for item in items)

        # Group by course (preserve insertion order)
        courses: "OrderedDict[str, list[dict]]" = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        # Subject
        parts = [f"{ct} ({len(lecs)})" for ct, lecs in courses.items()]
        subject = f"[FiCS] {', '.join(parts)}"
        if any_update:
            subject += "（含 PPT 识别·更新）"

        # One Markdown attachment per course
        stamp = time.strftime("%Y%m%d")
        attachments: list[tuple[str, str]] = []
        files: dict[str, str] = {}
        used: set[str] = set()
        for course_title, lectures in courses.items():
            base = self._safe_filename(course_title)
            filename = f"{base}_{stamp}.md"
            suffix = 2
            while filename in used:
                filename = f"{base}_{stamp}_{suffix}.md"
                suffix += 1
            used.add(filename)
            files[course_title] = filename
            attachments.append(
                (filename, self._build_course_markdown(course_title, lectures))
            )

        plain = self._build_plain_body(courses, files)
        html = self._build_html_body(courses, files)

        # MIME: mixed > alternative > (plain, html) + .md attachments
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver

        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
        msg_alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(msg_alt)

        for filename, content in attachments:
            part = MIMEBase("text", "markdown")
            part.set_payload(content.encode("utf-8"))
            encoders.encode_base64(part)
            # RFC 2231 编码，中文文件名在 QQ / Outlook / Gmail 都不会乱码
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", filename))
            msg.attach(part)

        # Retry with exponential backoff
        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL(self.host, self.port) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receiver, msg.as_string())
                print(f"[Emailer] Sent: {subject} "
                      f"({len(attachments)} md attachment(s))")
                return True
            except Exception as e:
                print(f"[Emailer] Attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Emailer] All send attempts failed.")
        return False
