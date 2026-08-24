import io
import os
import re
import zipfile
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
import markdownify

logger = logging.getLogger("converter")

def sanitize_filename(name: str) -> str:
    """
    Sanitizes string for cross-platform safe file and folder names.
    """
    if not name:
        return "Untitled"
    # Replace illegal characters with underscore
    sanitized = re.sub(r'[\\/*?:"<>|]', "_", name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "Untitled"


def clean_vtt_srt_transcript(text: str) -> str:
    """
    Cleans WebVTT (.vtt) and SubRip (.srt) subtitle content into readable plain text transcript.
    """
    if not text:
        return ""
    # Remove WEBVTT header
    text = re.sub(r'^WEBVTT.*?\n', '', text, flags=re.IGNORECASE)
    # Remove timestamp lines (e.g. 00:00:01.000 --> 00:00:04.000 or 00:00:01,000 --> 00:00:04,000)
    text = re.sub(r'\d{2}:\d{2}:\d{2}[\.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[\.,]\d{3}.*?\n', '', text)
    text = re.sub(r'\d{2}:\d{2}[\.,]\d{3}\s*-->\s*\d{2}:\d{2}[\.,]\d{3}.*?\n', '', text)
    # Remove standalone cue numbers
    text = re.sub(r'^\d+\s*$', '', text, flags=re.MULTILINE)
    # Remove HTML/XML cue tags like <v Speaker> or <c>
    text = re.sub(r'<[^>]+>', '', text)
    
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dedup_lines = []
    for line in lines:
        if not dedup_lines or line != dedup_lines[-1]:
            dedup_lines.append(line)
    return "\n\n".join(dedup_lines)


def extract_text_from_file_bytes(file_bytes: bytes, filename: str) -> str:
    """
    Extracts readable text content from PDF, PPTX, DOCX, TXT, VTT, SRT, or HTML attachment files.
    """
    if not file_bytes:
        return ""

    lower_name = filename.lower()

    # 1. PDF extraction
    if lower_name.endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            pages_text = []
            for idx, page in enumerate(reader.pages, 1):
                t = page.extract_text() or ""
                if t.strip():
                    pages_text.append(f"#### Page {idx}\n\n{t.strip()}")
            if pages_text:
                return "\n\n".join(pages_text)
        except Exception as e:
            logger.warning(f"Could not extract text from PDF {filename}: {e}")

    # 2. PowerPoint (.pptx) extraction
    elif lower_name.endswith(".pptx"):
        try:
            import pptx
            prs = pptx.Presentation(io.BytesIO(file_bytes))
            slides_text = []
            for idx, slide in enumerate(prs.slides, 1):
                slide_content = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text and shape.text.strip():
                        slide_content.append(shape.text.strip())
                if slide_content:
                    slides_text.append(f"#### Slide {idx}\n\n" + "\n\n".join(slide_content))
            if slides_text:
                return "\n\n".join(slides_text)
        except Exception as e:
            logger.warning(f"Could not extract text from PPTX {filename}: {e}")

    # 3. Word (.docx) extraction
    elif lower_name.endswith(".docx"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
            if paras:
                return "\n\n".join(paras)
        except Exception as e:
            logger.warning(f"Could not extract text from DOCX {filename}: {e}")

    # 4. WebVTT / SRT Subtitle Transcript extraction
    elif any(lower_name.endswith(ext) for ext in [".vtt", ".srt"]):
        try:
            raw_text = file_bytes.decode("utf-8", errors="ignore")
            return clean_vtt_srt_transcript(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse VTT/SRT transcript {filename}: {e}")

    # 5. Plain Text / HTML / Markdown / XML
    elif any(lower_name.endswith(ext) for ext in [".txt", ".html", ".htm", ".md", ".csv", ".json", ".xml"]):
        try:
            raw_text = file_bytes.decode("utf-8", errors="ignore")
            if lower_name.endswith(".html") or lower_name.endswith(".htm"):
                return markdownify.markdownify(raw_text)
            return raw_text
        except Exception as e:
            logger.warning(f"Could not decode text file {filename}: {e}")

    return ""


class CourseMarkdownConverter:
    """
    Parses Blackboard content trees, converts HTML into Markdown,
    downloads and places attachments into relative asset directories,
    rewrites links, and builds a downloadable ZIP file structure.
    """

    def __init__(self, course_name: str, course_id: str):
        self.course_name = course_name or course_id or "Course Materials"
        self.course_id = course_id
        clean_name = re.sub(r'\s*-\s*Course Materials$', '', self.course_name, flags=re.IGNORECASE).strip()
        self.clean_course_name = clean_name or self.course_name
        self.root_folder_name = sanitize_filename(f"{self.clean_course_name}_Markdown")

    def convert_html_to_markdown(self, html_content: str, attachment_map: Optional[Dict[str, str]] = None) -> str:
        if not html_content or not html_content.strip():
            return ""

        soup = BeautifulSoup(html_content, "html.parser")

        if attachment_map:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                for orig_url, rel_path in attachment_map.items():
                    if orig_url in href or href in orig_url:
                        a_tag["href"] = rel_path

            for img_tag in soup.find_all("img", src=True):
                src = img_tag["src"]
                for orig_url, rel_path in attachment_map.items():
                    if orig_url in src or src in orig_url:
                        img_tag["src"] = rel_path

        md = markdownify.markdownify(str(soup), heading_style="ATX")
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        return md

    def _flatten_tree(
        self,
        nodes: List[Dict[str, Any]],
        flat_list: List[Dict[str, Any]],
        folder_path: str = "",
    ):
        for node in nodes:
            title = node.get("title", "Untitled")
            current_path = f"{folder_path} / {title}" if folder_path else title

            body = (
                node.get("body")
                or node.get("description")
                or node.get("instructions")
                or node.get("summary")
                or node.get("formattedBody")
                or ""
            )
            has_content = bool(body and body.strip()) or bool(node.get("attachments"))

            children = node.get("children", [])
            if children:
                if has_content:
                    node_copy = dict(node)
                    node_copy["folder_path"] = folder_path
                    flat_list.append(node_copy)
                self._flatten_tree(children, flat_list, folder_path=current_path)
            else:
                node_copy = dict(node)
                node_copy["folder_path"] = folder_path
                flat_list.append(node_copy)

    async def build_zip_package(
        self,
        content_tree: List[Dict[str, Any]],
        attachment_downloader: Optional[Callable[[str, str, str], Any]] = None,
        progress_callback: Optional[Callable[[str, float], Any]] = None,
    ) -> bytes:
        """
        Processes content tree, creates all converted .md files in ONE single folder inside the zip archive.
        """
        zip_buffer = io.BytesIO()

        flat_items = []
        self._flatten_tree(content_tree, flat_items)
        total_nodes = len(flat_items)

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            out_folder = self.root_folder_name

            readme_content = f"# {self.course_name}\n\n"
            readme_content += f"**Course ID:** {self.course_id}\n\n"
            readme_content += "Extracted & converted to Markdown via **NTULearn Extractor Tool**.\n\n"
            readme_content += "## Course Content Index\n\n"

            for idx, item in enumerate(flat_items, 1):
                clean_title = sanitize_filename(item.get("title", f"Item_{idx}"))
                filename = f"{idx:02d}_{clean_title}.md"
                mod_path = f"*(Module: `{item.get('folder_path')}`)* " if item.get("folder_path") else ""
                readme_content += f"- [{item.get('title')}]({filename}) {mod_path}\n"

            zf.writestr(f"{out_folder}/00_README.md", readme_content)

            for idx, item in enumerate(flat_items, 1):
                clean_title = sanitize_filename(item.get("title", f"Item_{idx}"))
                filename = f"{idx:02d}_{clean_title}.md"
                file_path = f"{out_folder}/{filename}"

                if progress_callback:
                    pct = (idx / max(1, total_nodes)) * 100
                    await progress_callback(f"Converting Markdown for: {item.get('title')}", pct)

                attachments_dir = f"{out_folder}/attachments"
                attachment_map, doc_text_map = await self._handle_attachments(
                    item, attachments_dir, zf, attachment_downloader
                )

                body = (
                    item.get("body")
                    or item.get("description")
                    or item.get("instructions")
                    or item.get("summary")
                    or item.get("formattedBody")
                    or ""
                )

                handler = item.get("contentHandler", {})
                handler_id = handler.get("id", "") if isinstance(handler, dict) else str(handler)
                is_video_item = (
                    "video" in handler_id.lower()
                    or "kaltura" in handler_id.lower()
                    or "panopto" in handler_id.lower()
                    or "media" in handler_id.lower()
                    or any(ext in body.lower() for ext in [".mp4", ".mov", ".m4v", ".webm"])
                )

                # If this is a video item with description/transcript text, write a dedicated .txt transcript file!
                if is_video_item:
                    clean_item_title = sanitize_filename(item.get("title", "Video"))
                    txt_filename = f"{clean_item_title}_transcript.txt"
                    if txt_filename not in doc_text_map:
                        clean_body_text = BeautifulSoup(body, "html.parser").get_text(separator="\n\n", strip=True) if body else ""
                        if clean_body_text and len(clean_body_text) > 10:
                            zf.writestr(f"{out_folder}/{txt_filename}", clean_body_text)
                            doc_text_map[txt_filename] = clean_body_text

                md_text = self.convert_html_to_markdown(body, attachment_map)

                full_md = f"# {item.get('title')}\n\n"
                if item.get("folder_path"):
                    full_md += f"**Module / Path:** `{item.get('folder_path')}`\n\n"

                if md_text and md_text.strip():
                    full_md += f"{md_text.strip()}\n\n"

                # Append text extracted directly from attached PDF / PPTX / DOCX / TXT / Video Transcripts!
                if doc_text_map:
                    for att_file, doc_text in doc_text_map.items():
                        section_heading = "Video Transcript" if "transcript" in att_file.lower() or att_file.endswith(".txt") else "Extracted Content from Document"
                        full_md += f"## {section_heading} (`{att_file}`)\n\n"
                        full_md += f"{doc_text}\n\n"

                if item.get("attachments"):
                    full_md += "### Attached Files & Resources\n\n"
                    for att in item["attachments"]:
                        att_name = sanitize_filename(att.get("fileName", "attachment"))
                        rel_link = f"./attachments/{att_name}"
                        full_md += f"- 📎 [{att.get('fileName', 'attachment')}]({rel_link})\n"

                zf.writestr(file_path, full_md)

        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    async def _handle_attachments(
        self,
        node: Dict[str, Any],
        attachments_dir: str,
        zf: zipfile.ZipFile,
        downloader: Optional[Callable],
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        mapping = {}
        doc_text_map = {}
        attachments = node.get("attachments", [])
        content_id = node.get("id")

        for att in attachments:
            att_id = att.get("id")
            filename = sanitize_filename(att.get("fileName", f"file_{att_id}"))
            zip_target_path = f"{attachments_dir}/{filename}"

            # Calculate relative path from document to attachment
            rel_path = f"./attachments/{filename}"

            original_url = att.get("originalUrl") or att.get("downloadUrl") or att_id
            mapping[original_url] = rel_path

            if downloader and content_id:
                download_key = att.get("downloadUrl") or att_id
                if download_key:
                    try:
                        data = await downloader(self.course_id, content_id, download_key)
                        if data:
                            zf.writestr(zip_target_path, data)
                            extracted_text = extract_text_from_file_bytes(data, filename)
                            if extracted_text and extracted_text.strip():
                                doc_text_map[filename] = extracted_text

                                # If this attachment is a VTT / SRT subtitle file, also save a dedicated .txt transcript file!
                                lower_fn = filename.lower()
                                if any(lower_fn.endswith(ext) for ext in [".vtt", ".srt"]):
                                    clean_node_title = sanitize_filename(node.get("title", "Video"))
                                    txt_filename = f"{clean_node_title}_transcript.txt"
                                    txt_target_path = f"{self.root_folder_name}/{txt_filename}"
                                    zf.writestr(txt_target_path, extracted_text)
                                    doc_text_map[txt_filename] = extracted_text
                    except Exception as e:
                        logger.error(f"Failed to download attachment {filename}: {e}")

        return mapping, doc_text_map

    def _count_nodes(self, nodes: List[Dict[str, Any]]) -> int:
        count = 0
        for node in nodes:
            count += 1
            if node.get("children"):
                count += self._count_nodes(node["children"])
        return count

    async def build_raw_zip_package(
        self,
        content_tree: List[Dict[str, Any]],
        attachment_downloader: Optional[Callable[[str, str, str], Any]] = None,
        progress_callback: Optional[Callable[[str, float], Any]] = None,
    ) -> bytes:
        """
        Builds a ZIP package containing ONLY raw course files, PDFs, slides, and documents.
        Bypasses all Markdown conversion completely.
        """
        zip_buffer = io.BytesIO()
        total_nodes = self._count_nodes(content_tree)
        safe_name = sanitize_filename(self.clean_course_name)
        root_dir = f"{safe_name}_RawFiles"

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            await self._process_raw_node_list(
                nodes=content_tree,
                current_dir=root_dir,
                zf=zf,
                attachment_downloader=attachment_downloader,
                progress_callback=progress_callback,
                processed_count=[0],
                total_nodes=total_nodes,
            )

        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    async def _process_raw_node_list(
        self,
        nodes: List[Dict[str, Any]],
        current_dir: str,
        zf: zipfile.ZipFile,
        attachment_downloader: Optional[Callable],
        progress_callback: Optional[Callable],
        processed_count: List[int],
        total_nodes: int,
    ):
        for node in nodes:
            processed_count[0] += 1
            pct = (processed_count[0] / max(1, total_nodes)) * 100
            if progress_callback:
                await progress_callback(f"Downloading raw files for: {node.get('title', 'Untitled')}", pct)

            title = sanitize_filename(node.get("title", "Untitled"))
            is_folder = node.get("isFolder", False)

            attachments = node.get("attachments", [])
            content_id = node.get("id")

            if is_folder:
                folder_path = f"{current_dir}/{title}"
                # Process attachments on the folder itself
                if attachments:
                    for att in attachments:
                        att_id = att.get("downloadUrl") or att.get("id")
                        filename = sanitize_filename(att.get("fileName", f"file_{att.get('id')}"))
                        zip_target_path = f"{folder_path}/{filename}"
                        if attachment_downloader and content_id and att_id:
                            if progress_callback:
                                await progress_callback(f"Downloading file: {filename}...", pct)
                            try:
                                data = await attachment_downloader(self.course_id, content_id, att_id)
                                if data:
                                    zf.writestr(zip_target_path, data)
                                    if progress_callback:
                                        await progress_callback(f"Downloaded file: {filename} ({len(data)} bytes)", pct)
                            except Exception as e:
                                logger.error(f"Failed to download folder attachment {filename}: {e}")

                children = node.get("children", [])
                if children:
                    await self._process_raw_node_list(
                        nodes=children,
                        current_dir=folder_path,
                        zf=zf,
                        attachment_downloader=attachment_downloader,
                        progress_callback=progress_callback,
                        processed_count=processed_count,
                        total_nodes=total_nodes,
                    )
            else:
                # Raw attachments placed directly into current_dir
                downloaded_any = False
                if attachments:
                    for att in attachments:
                        att_id = att.get("downloadUrl") or att.get("id")
                        filename = sanitize_filename(att.get("fileName", f"file_{att.get('id')}"))
                        zip_target_path = f"{current_dir}/{filename}"

                        if attachment_downloader and content_id and att_id:
                            if progress_callback:
                                await progress_callback(f"Downloading file: {filename}...", pct)
                            try:
                                data = await attachment_downloader(self.course_id, content_id, att_id)
                                if data and len(data) > 100:
                                    lower_fn = filename.lower()
                                    is_video_file = any(lower_fn.endswith(ext) for ext in [".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"])
                                    
                                    # Validate binary video headers to prevent saving HTML/XML 404 error pages as .mp4
                                    valid_video = True
                                    if is_video_file:
                                        snippet = data[:64]
                                        if len(data) < 1000 or snippet.startswith(b"<") or snippet.startswith(b"{") or b"<?xml" in snippet or b"404 Not Found" in snippet:
                                            valid_video = False
                                        elif not (b"ftyp" in snippet or b"moov" in snippet or snippet.startswith(b"\x00\x00\x00") or b"matroska" in snippet):
                                            valid_video = False

                                    if valid_video:
                                        zf.writestr(zip_target_path, data)
                                        downloaded_any = True
                                        if progress_callback:
                                            await progress_callback(f"Downloaded file: {filename} ({len(data)} bytes)", pct)

                                        # If attachment is an SRT or VTT subtitle file, convert to plain text .txt transcript!
                                        if any(lower_fn.endswith(ext) for ext in [".srt", ".vtt"]):
                                            try:
                                                raw_text = data.decode("utf-8", errors="ignore")
                                                clean_txt = clean_vtt_srt_transcript(raw_text)
                                                if clean_txt and clean_txt.strip():
                                                    clean_node_title = sanitize_filename(node.get("title", "Video"))
                                                    txt_filename = f"{clean_node_title}_transcript.txt"
                                                    txt_target_path = f"{current_dir}/{txt_filename}"
                                                    zf.writestr(txt_target_path, clean_txt)
                                                    if progress_callback:
                                                        await progress_callback(f"Converted subtitle: {txt_filename}", pct)
                                            except Exception as e:
                                                logger.warning(f"Could not convert subtitle {filename} to TXT: {e}")
                                    else:
                                        logger.warning(f"Attachment {filename} returned non-video/HTML error data ({len(data)} bytes). Skipping saving invalid video file.")
                            except Exception as e:
                                logger.error(f"Failed to download raw attachment {filename}: {e}")

                # Check if this item is a Kaltura / Panopto / Video item
                body = node.get("body") or node.get("description") or node.get("instructions") or ""
                handler = str(node.get("contentHandler", "")).lower()
                is_kaltura_or_video = (
                    "kaltura" in handler
                    or "video" in handler
                    or "panopto" in handler
                    or "kaltura" in body.lower()
                    or "panopto" in body.lower()
                    or any(att.get("isKaltura") for att in attachments)
                )

                if is_kaltura_or_video:
                    # Write interactive HTML video launcher, URL shortcut & README for Kaltura/video items
                    embed_url = ""
                    for att in attachments:
                        if att.get("originalUrl"):
                            embed_url = att.get("originalUrl")
                            break
                        elif att.get("downloadUrl"):
                            embed_url = att.get("downloadUrl")
                            break
                    if not embed_url:
                        import re
                        m = re.search(r'href=["\']([^"\']+)["\']|src=["\']([^"\']+)["\']', body)
                        if m:
                            embed_url = m.group(1) or m.group(2) or ""
                    if not embed_url and content_id:
                        embed_url = f"https://ntulearn.ntu.edu.sg/webapps/blackboard/content/launchLink.jsp?course_id={self.course_id}&content_id={content_id}"

                    if embed_url:
                        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title} - Video</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #0f172a; color: white; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 90vh; }}
        h1 {{ margin-bottom: 20px; font-size: 1.5rem; text-align: center; }}
        .video-container {{ width: 100%; max-width: 960px; aspect-ratio: 16/9; background: #000; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
        iframe {{ width: 100%; height: 100%; border: none; }}
        .btn {{ margin-top: 20px; padding: 12px 24px; background: #2563eb; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; display: inline-block; }}
        .btn:hover {{ background: #1d4ed8; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <div class="video-container">
        <iframe src="{embed_url}" allowfullscreen allow="autoplay; fullscreen; encrypted-media"></iframe>
    </div>
    <a class="btn" href="{embed_url}" target="_blank">Open Video in Browser</a>
</body>
</html>"""
                        zf.writestr(f"{current_dir}/{title}_Kaltura_Video.html", html_content)
                        zf.writestr(f"{current_dir}/{title}_Kaltura_Link.url", f"[InternetShortcut]\nURL={embed_url}\n")
                        
                        readme_txt = f"""Kaltura Video: {node.get('title')}

Direct MP4 video file download is protected by NTU's Kaltura media server policies (requires active NTULearn login session).

How to watch this video:
1. Double-click '{title}_Kaltura_Video.html' to open and watch the embedded video in any web browser.
2. Or double-click '{title}_Kaltura_Link.url' to open the original video directly on NTULearn.
"""
                        zf.writestr(f"{current_dir}/{title}_README.txt", readme_txt)

                    # Always write dedicated {title}_transcript.txt for video items
                    clean_node_title = sanitize_filename(node.get("title", "Video"))
                    txt_filename = f"{clean_node_title}_transcript.txt"
                    txt_target_path = f"{current_dir}/{txt_filename}"

                    clean_body_text = BeautifulSoup(body, "html.parser").get_text(separator="\n\n", strip=True) if body else ""
                    if clean_body_text and len(clean_body_text) > 10:
                        zf.writestr(txt_target_path, clean_body_text)
                    else:
                        no_transcript_msg = f"""Title: {node.get('title')}
Course ID: {self.course_id}

Transcript Status: No subtitle/caption transcript file (.srt / .vtt) or text transcript was attached for this video by the instructor on NTULearn.

To watch the video with player controls in your browser:
1. Double-click '{title}_Kaltura_Video.html' to open and play the video.
2. Or double-click '{title}_Kaltura_Link.url' to view it directly on NTULearn.
"""
                        zf.writestr(txt_target_path, no_transcript_msg)

                elif not downloaded_any:
                    # Fallback for general content items with no attachments: save body HTML if present
                    if body and body.strip():
                        zf.writestr(f"{current_dir}/{title}.html", f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title></head><body><h1>{node.get('title')}</h1>{body}</body></html>")
