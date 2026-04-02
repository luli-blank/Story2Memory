from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from rag.bookSlice import CHAPTER_HEADING_PATTERN, SECTION_HEADING_PATTERN, slice_book_by_chapter


@dataclass
class EpubParseResult:
    title: str
    author: str
    description: str | None
    chapters: list[dict[str, Any]]
    total_words: int
    cover_bytes: bytes | None
    cover_extension: str | None


@dataclass
class _SpineDoc:
    path: str
    text: str
    headings: list[str]
    properties: str


class _HtmlTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._headings: list[str] = []
        self._capture_heading = False
        self._heading_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS and self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._capture_heading = True
            self._heading_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS and self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = "".join(self._heading_buffer).strip()
            if heading:
                self._headings.append(heading)
            self._capture_heading = False
            self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if not data:
            return
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed.strip():
            return
        self._parts.append(collapsed)
        if self._capture_heading:
            self._heading_buffer.append(collapsed)

    @property
    def headings(self) -> list[str]:
        return list(self._headings)

    def get_text(self) -> str:
        text = "".join(self._parts).replace("\xa0", " ")
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_zip_text(zf: zipfile.ZipFile, name: str) -> str:
    raw = zf.read(name)
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _resolve_href(base_path: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), href))


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(zf.read("META-INF/container.xml"))
    for elem in root.iter():
        if _local_name(elem.tag) == "rootfile":
            full_path = elem.attrib.get("full-path", "").strip()
            if full_path:
                return full_path
    raise RuntimeError("Failed to locate OPF package path.")


def _extract_html_text(text: str) -> tuple[str, list[str]]:
    parser = _HtmlTextExtractor()
    parser.feed(text)
    parser.close()
    return parser.get_text(), parser.headings


def _match_chapter_title(text: str) -> str | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    for limit in (1, 2, 3):
        candidate = " ".join(lines[:limit]).strip()
        if candidate and (
            CHAPTER_HEADING_PATTERN.fullmatch(candidate) or SECTION_HEADING_PATTERN.fullmatch(candidate)
        ):
            return candidate
    return None


def _count_words(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _guess_extension(media_type: str, path: str) -> str | None:
    by_media = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    if media_type in by_media:
        return by_media[media_type]
    suffix = Path(path).suffix.lower()
    return suffix or None


def _find_cover_image_path(
    zf: zipfile.ZipFile,
    opf_path: str,
    manifest_by_id: dict[str, dict[str, str]],
    cover_id: str,
) -> tuple[str, str] | tuple[None, None]:
    def is_image_item(item: dict[str, str]) -> bool:
        return item.get("media_type", "").startswith("image/")

    if cover_id and cover_id in manifest_by_id and is_image_item(manifest_by_id[cover_id]):
        item = manifest_by_id[cover_id]
        return _resolve_href(opf_path, item["href"]), item["media_type"]

    for item in manifest_by_id.values():
        if "cover-image" in item.get("properties", "") and is_image_item(item):
            return _resolve_href(opf_path, item["href"]), item["media_type"]

    cover_page_item = manifest_by_id.get(cover_id) if cover_id in manifest_by_id else None
    candidates = []
    if cover_page_item:
        candidates.append(cover_page_item)
    candidates.extend(
        item
        for item in manifest_by_id.values()
        if "cover" in item.get("href", "").lower() and item.get("media_type") == "application/xhtml+xml"
    )

    for item in candidates:
        page_path = _resolve_href(opf_path, item["href"])
        try:
            page_text = _read_zip_text(zf, page_path)
        except KeyError:
            continue
        matched = re.search(
            r"""(?:xlink:href|src)\s*=\s*['"]([^'"]+\.(?:jpg|jpeg|png|webp|gif|bmp))['"]""",
            page_text,
            flags=re.IGNORECASE,
        )
        if matched:
            image_href = matched.group(1).strip()
            image_path = _resolve_href(page_path, image_href)
            suffix = Path(image_path).suffix.lower()
            media_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
            }.get(suffix, "")
            return image_path, media_type

    for item_id, item in manifest_by_id.items():
        if is_image_item(item) and (
            "cover" in item_id.lower()
            or "cover" in item.get("href", "").lower()
        ):
            return _resolve_href(opf_path, item["href"]), item["media_type"]

    return None, None


def _should_skip_doc(path: str, text: str, properties: str) -> bool:
    normalized_text = text.strip()
    lower_path = path.lower()
    if not normalized_text:
        return True
    if " nav " in f" {properties} ":
        return True
    if lower_path.endswith("cover.xhtml") and len(normalized_text) <= 32:
        return True
    if normalized_text.lower() == "cover":
        return True
    return False


def _strip_leading_heading(text: str, heading: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not heading.strip():
        return normalized.strip()
    lines = normalized.splitlines()
    nonempty_indices = [idx for idx, line in enumerate(lines) if line.strip()]
    if not nonempty_indices:
        return normalized.strip()
    first_join = " ".join(lines[idx].strip() for idx in nonempty_indices[:2]).strip()
    if first_join == heading.strip() and len(nonempty_indices) >= 2:
        start_idx = nonempty_indices[1] + 1
        return "\n".join(lines[start_idx:]).strip()
    first_line = lines[nonempty_indices[0]].strip()
    if first_line == heading.strip():
        start_idx = nonempty_indices[0] + 1
        return "\n".join(lines[start_idx:]).strip()
    return normalized.strip()


def _docs_to_chapters(docs: list[_SpineDoc]) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    prefix_parts: list[str] = []
    started_chapters = False

    for doc in docs:
        title = _match_chapter_title(next((item for item in doc.headings if item.strip()), "") or doc.text) or ""
        content = doc.text.strip()
        if not content:
            continue
        if not title:
            if not started_chapters:
                prefix_parts.append(content)
            elif chapters:
                previous = chapters[-1]
                previous_content = str(previous.get("content") or "").strip()
                previous["content"] = f"{previous_content}\n\n{content}".strip()
                previous["word_count"] = _count_words(str(previous["content"]))
            else:
                prefix_parts.append(content)
            continue

        started_chapters = True
        chapters.append(
            {
                "chapter_index": len(chapters) + 1,
                "title": title,
                "content": _strip_leading_heading(content, title),
                "word_count": _count_words(_strip_leading_heading(content, title)),
            }
        )

    if prefix_parts:
        prefix_content = "\n\n".join(part for part in prefix_parts if part.strip()).strip()
        if prefix_content:
            chapters.insert(
                0,
                {
                    "chapter_index": 1,
                    "title": "前言",
                    "content": prefix_content,
                    "word_count": _count_words(prefix_content),
                },
            )
            for index, chapter in enumerate(chapters, start=1):
                chapter["chapter_index"] = index
    return chapters


def parse_epub_book(epub_path: str | Path) -> EpubParseResult:
    path = Path(epub_path)
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        opf_root = ET.fromstring(zf.read(opf_path))

        title = ""
        author = ""
        description = ""
        cover_id = ""
        manifest_by_id: dict[str, dict[str, str]] = {}

        for elem in opf_root.iter():
            local = _local_name(elem.tag)
            if local == "title" and not title:
                title = (elem.text or "").strip()
            elif local == "creator" and not author:
                author = (elem.text or "").strip()
            elif local == "description" and not description:
                description = (elem.text or "").strip()
            elif local == "meta":
                if elem.attrib.get("name", "").strip().lower() == "cover":
                    cover_id = elem.attrib.get("content", "").strip()
            elif local == "item":
                item_id = elem.attrib.get("id", "").strip()
                if item_id:
                    manifest_by_id[item_id] = {
                        "href": elem.attrib.get("href", "").strip(),
                        "media_type": elem.attrib.get("media-type", "").strip(),
                        "properties": elem.attrib.get("properties", "").strip(),
                    }

        spine_ids: list[str] = []
        for elem in opf_root.iter():
            if _local_name(elem.tag) == "itemref":
                itemref = elem.attrib.get("idref", "").strip()
                if itemref:
                    spine_ids.append(itemref)

        docs: list[_SpineDoc] = []
        for item_id in spine_ids:
            item = manifest_by_id.get(item_id)
            if not item:
                continue
            media_type = item.get("media_type", "")
            if media_type not in {"application/xhtml+xml", "text/html", "application/html+xml"}:
                continue
            content_path = _resolve_href(opf_path, item["href"])
            text, headings = _extract_html_text(_read_zip_text(zf, content_path))
            if _should_skip_doc(content_path, text, item.get("properties", "")):
                continue
            docs.append(
                _SpineDoc(
                    path=content_path,
                    text=text,
                    headings=headings,
                    properties=item.get("properties", ""),
                )
            )

        combined_text = "\n\n".join(doc.text for doc in docs if doc.text.strip())
        chapter_like_count = 0
        for doc in docs:
            first_heading = next((heading for heading in doc.headings if heading.strip()), "")
            if _match_chapter_title(first_heading or doc.text):
                chapter_like_count += 1
        if chapter_like_count >= max(3, len(docs) // 3):
            chapters = _docs_to_chapters(docs)
        else:
            chapters = slice_book_by_chapter(combined_text) if combined_text else []
            if len(chapters) <= 1:
                chapters = _docs_to_chapters(docs)

        cover_path, cover_media_type = _find_cover_image_path(zf, opf_path, manifest_by_id, cover_id)
        cover_bytes = zf.read(cover_path) if cover_path else None
        cover_extension = _guess_extension(cover_media_type or "", cover_path or "")

        total_words = sum(int(chapter.get("word_count") or 0) for chapter in chapters)
        return EpubParseResult(
            title=title or path.stem.strip() or "未命名书籍",
            author=author or "未知",
            description=description or None,
            chapters=chapters,
            total_words=total_words,
            cover_bytes=cover_bytes,
            cover_extension=cover_extension,
        )
