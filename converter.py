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

class CourseMarkdownConverter:
    """
    Parses Blackboard content trees, converts HTML into Markdown,
    downloads and places attachments into relative asset directories,
    rewrites links, and builds a downloadable ZIP file structure.
    """

    def __init__(self, course_name: str, course_id: str):
        self.course_name = course_name
        self.course_id = course_id
        self.root_folder_name = sanitize_filename(f"{course_name} ({course_id})")

    def convert_html_to_markdown(self, html_content: str, attachment_mapping: Dict[str, str]) -> str:
        """
        Converts HTML string to Markdown, replacing Blackboard attachment links with relative paths.
        """
        if not html_content or not html_content.strip():
            return ""

        soup = BeautifulSoup(html_content, "html.parser")

        # Replace attachment links or embedded file URLs
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            for original_url, local_rel_path in attachment_mapping.items():
                if original_url in href or href in original_url:
                    a_tag["href"] = local_rel_path

        for img_tag in soup.find_all("img", src=True):
            src = img_tag["src"]
            for original_url, local_rel_path in attachment_mapping.items():
                if original_url in src or src in original_url:
                    img_tag["src"] = local_rel_path

        cleaned_html = str(soup)
        md = markdownify.markdownify(cleaned_html, heading_style="ATX")
        # Clean up excess whitespace lines
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
                attachment_map = await self._handle_attachments(
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

                md_text = self.convert_html_to_markdown(body, attachment_map)

                full_md = f"# {item.get('title')}\n\n"
                if item.get("folder_path"):
                    full_md += f"**Module / Path:** `{item.get('folder_path')}`\n\n"

                if md_text and md_text.strip():
                    full_md += f"{md_text.strip()}\n\n"

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
    ) -> Dict[str, str]:
        mapping = {}
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
                    except Exception as e:
                        logger.error(f"Failed to download attachment {filename}: {e}")

        return mapping

    async def build_raw_zip_package(
        self,
        content_tree: List[Dict[str, Any]],
        attachment_downloader: Optional[Callable[[str, str, str], Any]] = None,
        progress_callback: Optional[Callable[[str, float], Any]] = None,
    ) -> bytes:
        """
        Builds a ZIP package containing ONLY raw course files, PDFs, slides, and documents.
        """
        zip_buffer = io.BytesIO()
        total_nodes = self._count_nodes(content_tree)

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            await self._process_raw_node_list(
                nodes=content_tree,
                current_dir=self.root_folder_name,
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

            if is_folder:
                folder_path = f"{current_dir}/{title}"
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
                attachments = node.get("attachments", [])
                content_id = node.get("id")
                if attachments:
                    for att in attachments:
                        att_id = att.get("downloadUrl") or att.get("id")
                        filename = sanitize_filename(att.get("fileName", f"file_{att.get('id')}"))
                        zip_target_path = f"{current_dir}/{filename}"

                        if attachment_downloader and content_id and att_id:
                            try:
                                data = await attachment_downloader(self.course_id, content_id, att_id)
                                if data:
                                    zf.writestr(zip_target_path, data)
                            except Exception as e:
                                logger.error(f"Failed to download raw attachment {filename}: {e}")
