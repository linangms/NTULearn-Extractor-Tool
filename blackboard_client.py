import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("blackboard_client")
logging.basicConfig(level=logging.INFO)

class BlackboardClient:
    """
    Async client for Blackboard Learn REST APIs.
    Handles OAuth authentication, recursive content tree extraction,
    attachment downloads, and automatic retry handling for rate-limiting (429).
    """

    def __init__(
        self,
        base_url: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._client: Optional[httpx.AsyncClient] = None
        self._resolved_course_ids: Dict[str, str] = {}

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def authenticate(self) -> str:
        """
        Authenticates via Client Credentials flow if no access_token is set.
        """
        if self.access_token:
            return self.access_token

        if not self.client_id or not self.client_secret:
            raise ValueError("Client ID and Client Secret required for OAuth2 authentication.")

        client_id = self.client_id.strip()
        client_secret = self.client_secret.strip()

        url = f"{self.base_url}/learn/api/public/v1/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials"}

        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await client.post(url, data=data, headers=headers, auth=(client_id, client_secret))
            if resp.status_code == 200:
                token_data = resp.json()
                self.access_token = token_data.get("access_token")
                logger.info("OAuth2 authentication successful.")
                return self.access_token
            else:
                logger.error(f"OAuth2 authentication failed with status {resp.status_code}: {resp.text}")
                raise RuntimeError(f"Authentication failed: {resp.status_code} {resp.text}")
        finally:
            if not self._client:
                await client.aclose()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> httpx.Response:
        """
        Executes an HTTP request with automatic retry handling for rate-limiting (429)
        and transient network errors.
        """
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_needed = self._client is None

        kwargs["headers"] = {**self._get_headers(), **kwargs.get("headers", {})}

        retry_count = 0
        delay = 1.0

        try:
            while True:
                try:
                    resp = await client.request(method, url, **kwargs)
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        sleep_time = float(retry_after) if retry_after else delay
                        logger.warning(f"Rate limited (429). Retrying after {sleep_time}s...")
                        await asyncio.sleep(sleep_time)
                        retry_count += 1
                        delay *= self.backoff_factor
                        continue
                    return resp
                except httpx.RequestError as exc:
                    retry_count += 1
                    if retry_count > self.max_retries:
                        logger.error(f"Request error failed after max retries: {exc}")
                        raise
                    await asyncio.sleep(delay)
                    delay *= self.backoff_factor
        finally:
            if close_needed:
                await client.aclose()

    def _get_course_id_candidates(self, course_id: str) -> List[str]:
        course_id_str = str(course_id).strip()
        if course_id_str in self._resolved_course_ids:
            return [self._resolved_course_ids[course_id_str]]
        
        import re
        m = re.search(r'^\_?(\d+)(?:\_1)?$', course_id_str)
        if m:
            pk = m.group(1)
            fmt = f"_{pk}_1"
            return [fmt, f"courseId:{pk}", f"courseId:{course_id_str}"]
        
        if not course_id_str.startswith("_") and not course_id_str.startswith("courseId:"):
            return [f"_{course_id_str}_1", f"courseId:{course_id_str}"]
        return [course_id_str]

    def _format_course_id(self, course_id: str) -> str:
        candidates = self._get_course_id_candidates(course_id)
        return candidates[0]

    async def get_course_details(self, course_id: str) -> Dict[str, Any]:
        """
        Retrieves basic details about a course via Blackboard v3/v1 API.
        """
        candidates = self._get_course_id_candidates(course_id)
        for fmt_id in candidates:
            for api_version in ["v3", "v1"]:
                url = f"{self.base_url}/learn/api/public/{api_version}/courses/{fmt_id}"
                resp = await self._request_with_retry("GET", url)
                if resp.status_code == 200:
                    self._resolved_course_ids[str(course_id).strip()] = fmt_id
                    data = resp.json()
                    if "results" in data and isinstance(data["results"], list) and len(data["results"]) > 0:
                        return data["results"][0]
        return {"id": course_id, "name": f"Course {course_id}", "courseId": course_id}

    async def get_contents_tree(self, course_id: str) -> List[Dict[str, Any]]:
        """
        Recursively fetches the full content tree for a course.
        """
        candidates = self._get_course_id_candidates(course_id)
        resp = None
        used_fmt_id = None
        for fmt_id in candidates:
            top_url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents"
            resp = await self._request_with_retry("GET", top_url)
            if resp.status_code == 200:
                used_fmt_id = fmt_id
                self._resolved_course_ids[str(course_id).strip()] = fmt_id
                break

        if not resp or resp.status_code != 200:
            logger.error(f"Failed to fetch top-level contents for course {course_id}: {resp.status_code if resp else 'No response'}")
            return []

        data = resp.json()
        items = data.get("results", [])

        tree = []
        for item in items:
            node = await self._build_content_node(course_id, item)
            tree.append(node)

        return tree

    async def get_content_detail(self, course_id: str, content_id: str) -> Dict[str, Any]:
        candidates = self._get_course_id_candidates(course_id)
        for fmt_id in candidates:
            url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}"
            resp = await self._request_with_retry("GET", url)
            if resp.status_code == 200:
                self._resolved_course_ids[str(course_id).strip()] = fmt_id
                return resp.json()
        return {}

    async def _build_content_node(self, course_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        content_id = item.get("id")
        
        # Fetch detailed item endpoint to ensure complete HTML body/instructions are present
        if content_id:
            detail = await self.get_content_detail(course_id, content_id)
            if detail:
                for k, v in detail.items():
                    if v and (not item.get(k) or k in ["body", "description", "instructions", "summary", "formattedBody"]):
                        item[k] = v

        title = item.get("title", "Untitled Content")
        handler = item.get("contentHandler", {}).get("id", "")
        is_folder = (
            item.get("folder", {}).get("isFolder", False)
            or item.get("hasChildren", False)
            or any(k in handler.lower() for k in ["folder", "module", "lesson", "chapter", "unit", "outline", "section"])
        )

        node = {
            "id": content_id,
            "title": title,
            "body": item.get("body") or item.get("formattedBody", ""),
            "description": item.get("description") or item.get("instructions") or item.get("summary", ""),
            "contentHandler": handler,
            "isFolder": is_folder,
            "created": item.get("created"),
            "modified": item.get("modified"),
            "children": [],
            "attachments": [],
            "links": item.get("links", []),
        }

        # Retrieve direct attachments for document / assignment / video items
        attachments = await self.get_content_attachments(course_id, content_id)

        MEDIA_EXTS = [
            ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".png", ".jpg", ".jpeg", ".svg",
            ".mp4", ".m4v", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".3gp", ".m4a", ".mp3", ".wav",
            ".vtt", ".srt", ".xml"
        ]
        
        # Also check item.links array for WebDAV / File / Video resources
        for lk in item.get("links", []):
            href = lk.get("href", "")
            if href and ("/bbcswebdav/" in href or "/files/" in href or any(href.lower().endswith(ext) for ext in MEDIA_EXTS)):
                title_name = lk.get("title") or title
                full_download_url = href if href.startswith("http") else f"{self.base_url}{href}"
                attachments.append({
                    "id": f"link_{content_id}_{len(attachments)+1}",
                    "fileName": title_name,
                    "downloadUrl": full_download_url,
                    "originalUrl": href,
                })

        # Also extract embedded video, audio, track (captions/transcripts) and file links from body/description HTML
        body_html = item.get("body") or item.get("description") or item.get("instructions") or ""
        if body_html:
            try:
                from bs4 import BeautifulSoup
                import urllib.parse
                soup = BeautifulSoup(body_html, "html.parser")
                idx = 0

                # 1. Parse <a> tags
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    link_text = a_tag.get_text(strip=True) or f"File_{idx+1}"
                    if "/bbcswebdav/" in href or "/files/" in href or any(href.lower().endswith(ext) for ext in MEDIA_EXTS):
                        idx += 1
                        filename = link_text
                        if not any(filename.lower().endswith(ext) for ext in MEDIA_EXTS):
                            parsed_path = urllib.parse.urlparse(href).path
                            url_filename = parsed_path.split("/")[-1]
                            if url_filename and "." in url_filename:
                                filename = url_filename
                        
                        full_download_url = href if href.startswith("http") else f"{self.base_url}{href}"
                        attachments.append({
                            "id": f"embedded_{content_id}_{idx}",
                            "fileName": filename,
                            "downloadUrl": full_download_url,
                            "originalUrl": href,
                        })

                # 2. Parse <video>, <source>, <audio>, <iframe src="...">, and <track src="...">
                for tag in soup.find_all(["video", "source", "audio", "iframe", "track"]):
                    src = tag.get("src") or tag.get("href")
                    if src and ("/bbcswebdav/" in src or "/files/" in src or any(src.lower().endswith(ext) for ext in MEDIA_EXTS)):
                        idx += 1
                        parsed_path = urllib.parse.urlparse(src).path
                        url_filename = parsed_path.split("/")[-1] or f"video_media_{idx}.mp4"
                        if not any(url_filename.lower().endswith(ext) for ext in MEDIA_EXTS):
                            url_filename = f"{title}_{idx}.mp4"

                        full_download_url = src if src.startswith("http") else f"{self.base_url}{src}"
                        attachments.append({
                            "id": f"media_{content_id}_{idx}",
                            "fileName": url_filename,
                            "downloadUrl": full_download_url,
                            "originalUrl": src,
                        })

            except Exception as e:
                logger.warning(f"Could not parse HTML embedded links/media for {content_id}: {e}")

        node["attachments"] = attachments

        # Always check if children exist for this item (covers modules, lessons, folders)
        fmt_id = self._format_course_id(course_id)
        child_url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/children"
        child_resp = await self._request_with_retry("GET", child_url)
        if child_resp.status_code == 200:
            child_data = child_resp.json()
            child_items = child_data.get("results", [])
            if child_items:
                node["isFolder"] = True
                for child_item in child_items:
                    child_node = await self._build_content_node(course_id, child_item)
                    node["children"].append(child_node)

        return node

    async def get_content_attachments(self, course_id: str, content_id: str) -> List[Dict[str, Any]]:
        fmt_id = self._format_course_id(course_id)
        url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/attachments"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.json().get("results", [])
        return []

    async def download_attachment_bytes(self, course_id: str, content_id: str, attachment_id: str) -> Optional[bytes]:
        if attachment_id.startswith("http"):
            url = attachment_id
        elif attachment_id.startswith("/"):
            url = f"{self.base_url}{attachment_id}"
        else:
            fmt_id = self._format_course_id(course_id)
            url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/attachments/{attachment_id}/download"
            
        resp = await self._request_with_retry("GET", url, follow_redirects=True)
        if resp.status_code == 200:
            return resp.content
        logger.error(f"Failed to download attachment {attachment_id} (URL: {url}): HTTP status {resp.status_code}")
        return None
