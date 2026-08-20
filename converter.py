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

    async def build_zip_package(
        self,
        content_tree: List[Dict[str, Any]],
        attachment_downloader: Optional[Callable[[str, str, str], Any]] = None,
        progress_callback: Optional[Callable[[str, float], Any]] = None,
    ) -> bytes:
        """
        Processes content tree, creates files/directories in a zip archive in-memory,
        and returns the raw zip bytes.
        """
        zip_buffer = io.BytesIO()

        total_nodes = self._count_nodes(content_tree)
        processed_nodes = 0

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write a README index file at root
            readme_content = f"# {self.course_name}\n\n"
            readme_content += f"**Course ID:** {self.course_id}\n\n"
            readme_content += "Extracted automatically via **NTULearn Extractor Tool**.\n\n"
            readme_content += "## Table of Contents\n\n"

            for node in content_tree:
                readme_content += f"- [{node['title']}](./{sanitize_filename(node['title'])})\n"

            zf.writestr(f"{self.root_folder_name}/README.md", readme_content)

            # Process tree recursively
            await self._process_node_list(
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

    def _count_nodes(self, nodes: List[Dict[str, Any]]) -> int:
        count = 0
        for node in nodes:
            count += 1
            if node.get("children"):
                count += self._count_nodes(node["children"])
        return count

    async def _process_node_list(
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
                await progress_callback(
                    f"Processing content item: {node.get('title', 'Untitled')}", pct
                )

            title = sanitize_filename(node.get("title", "Untitled"))
            is_folder = node.get("isFolder", False)

            if is_folder:
                folder_path = f"{current_dir}/{title}"
                # Process attachments for folder if any
                attachment_map = await self._handle_attachments(
                    node, folder_path, zf, attachment_downloader
                )

                # Convert description/body if present
                body = node.get("body") or node.get("description", "")
                if body and body.strip():
                    md_text = self.convert_html_to_markdown(body, attachment_map)
                    md_filename = f"{folder_path}/index.md"
                    zf.writestr(md_filename, f"# {node.get('title')}\n\n{md_text}")

                # Process children
                children = node.get("children", [])
                if children:
                    await self._process_node_list(
                        nodes=children,
                        current_dir=folder_path,
                        zf=zf,
                        attachment_downloader=attachment_downloader,
                        progress_callback=progress_callback,
                        processed_count=processed_count,
                        total_nodes=total_nodes,
                    )
            else:
                # Document / File / WebLink
                attachments_dir = f"{current_dir}/attachments"
                attachment_map = await self._handle_attachments(
                    node, attachments_dir, zf, attachment_downloader
                )

                body = node.get("body") or node.get("description", "")
                md_text = self.convert_html_to_markdown(body, attachment_map)

                full_md = f"# {node.get('title')}\n\n"
                if md_text:
                    full_md += f"{md_text}\n\n"

                # Add link list for attached files at the bottom
                if node.get("attachments"):
                    full_md += "### Attached Files\n\n"
                    for att in node["attachments"]:
                        att_name = sanitize_filename(att.get("fileName", "attachment"))
                        rel_link = f"./attachments/{att_name}"
                        full_md += f"- [{att.get('fileName', 'attachment')}]({rel_link})\n"

                file_path = f"{current_dir}/{title}.md"
                zf.writestr(file_path, full_md)

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

            if downloader and content_id and att_id:
                try:
                    data = await downloader(self.course_id, content_id, att_id)
                    if data:
                        zf.writestr(zip_target_path, data)
                except Exception as e:
                    logger.error(f"Failed to download attachment {filename}: {e}")

        return mapping
