import asyncio
import io
import os
import zipfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from blackboard_client import BlackboardClient
from converter import CourseMarkdownConverter, sanitize_filename


def test_sanitize_filename():
    assert sanitize_filename("Folder / With : Illegal ? Chars") == "Folder _ With _ Illegal _ Chars"
    assert sanitize_filename("") == "Untitled"
    assert sanitize_filename("  Normal Title  ") == "Normal Title"


def test_blackboard_client_retry_on_429():
    """
    Tests that BlackboardClient retries on 429 rate limit responses.
    """
    async def _test():
        client = BlackboardClient("https://ntulearn.test", max_retries=2, backoff_factor=0.01)

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.headers = {"Retry-After": "0.01"}

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"results": [{"id": "c1", "title": "Test Folder", "folder": {"isFolder": True}}]}

        with patch.object(httpx.AsyncClient, "request", side_effect=[mock_resp_429, mock_resp_200]) as mock_req:
            resp = await client._request_with_retry("GET", "https://ntulearn.test/learn/api/public/v1/courses/1/contents")
            assert resp.status_code == 200
            assert mock_req.call_count == 2

    asyncio.run(_test())


def test_converter_html_to_markdown():
    """
    Tests HTML to Markdown conversion and link rewriting.
    """
    converter = CourseMarkdownConverter("Deep Learning", "CZ4042")
    html = "<h1>Overview</h1><p>Welcome! Download <a href='/bbcswebdav/xid-123'>Syllabus</a>.</p>"
    mapping = {"/bbcswebdav/xid-123": "./attachments/Syllabus.pdf"}

    md = converter.convert_html_to_markdown(html, mapping)
    assert "# Overview" in md
    assert "[Syllabus](./attachments/Syllabus.pdf)" in md


def test_converter_build_zip_package():
    """
    Tests building full course folder hierarchy and zip package.
    """
    async def _test():
        converter = CourseMarkdownConverter("Deep Learning", "CZ4042")

        content_tree = [
            {
                "id": "node_1",
                "title": "Module 1 - Neural Networks",
                "isFolder": True,
                "body": "<p>Introductory Module</p>",
                "children": [
                    {
                        "id": "node_1_1",
                        "title": "Lecture Notes 1",
                        "isFolder": False,
                        "body": "<p>Basic artificial neuron model.</p>",
                        "attachments": [
                            {"id": "att_1", "fileName": "Lecture1.pdf", "originalUrl": "/bbcswebdav/xid-999"}
                        ]
                    }
                ]
            }
        ]

        async def mock_downloader(course_id, content_id, att_id):
            return b"%PDF-1.4 Mock PDF Content"

        zip_path = await converter.build_zip_package(
            content_tree=content_tree,
            attachment_downloader=mock_downloader
        )

        try:
            assert os.path.getsize(zip_path) > 0

            # Verify Zip structure
            with zipfile.ZipFile(zip_path, "r") as zf:
                namelist = zf.namelist()

                # Check README.md
                assert any("README.md" in name for name in namelist)

                # Check folder index.md
                assert any("Module 1 - Neural Networks/index.md" in name for name in namelist)

                # Check document .md
                assert any("Lecture Notes 1.md" in name for name in namelist)

                # Check attachment download
                assert any("Lecture1.pdf" in name for name in namelist)
        finally:
            os.remove(zip_path)

    asyncio.run(_test())


if __name__ == "__main__":
    pytest.main(["-v", __file__])

