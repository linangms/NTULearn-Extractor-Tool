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

        client = self._client or httpx.AsyncClient()
        try:
            # Attempt 1: HTTP Basic Auth with grant_type=client_credentials
            response = await client.post(
                url,
                data={"grant_type": "client_credentials"},
                headers=headers,
                auth=(client_id, client_secret),
            )

            # Attempt 2: Form Body parameters if Attempt 1 fails
            if response.status_code != 200:
                logger.info(f"Attempt 1 returned {response.status_code}, trying Form Body credentials...")
                response = await client.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                    headers=headers,
                )

            if response.status_code != 200:
                err_detail = response.text
                try:
                    err_json = response.json()
                    err_detail = err_json.get("error_description") or err_json.get("error") or response.text
                except Exception:
                    pass
                logger.error(f"Blackboard OAuth token request failed ({response.status_code}): {err_detail}")
                raise Exception(f"HTTP {response.status_code}: {err_detail}")

            token_data = response.json()
            self.access_token = token_data.get("access_token")
            logger.info("Successfully authenticated with Blackboard REST API.")
            return self.access_token
        finally:
            if not self._client:
                await client.aclose()

    async def _request_with_retry(
        self, method: str, url: str, **kwargs
    ) -> httpx.Response:
        """
        Executes HTTP request with exponential backoff for HTTP 429 rate limits.
        """
        client = self._client or httpx.AsyncClient()
        close_needed = self._client is None

        headers = self._get_headers()
        if "headers" in kwargs:
            headers.update(kwargs["headers"])
        kwargs["headers"] = headers

        retry_count = 0
        delay = 1.0

        try:
            while True:
                try:
                    response = await client.request(method, url, **kwargs)

                    if response.status_code == 429:
                        retry_count += 1
                        if retry_count > self.max_retries:
                            logger.error(f"Rate limit exceeded after {self.max_retries} retries for {url}")
                            response.raise_for_status()

                        # Respect Retry-After header if present
                        retry_after = response.headers.get("Retry-After")
                        wait_time = float(retry_after) if retry_after else delay
                        logger.warning(
                            f"HTTP 429 Rate Limit encountered. Retrying in {wait_time:.2f}s (Attempt {retry_count}/{self.max_retries})..."
                        )
                        await asyncio.sleep(wait_time)
                        delay *= self.backoff_factor
                        continue

                    if response.status_code in (401, 403):
                        logger.warning(
                            f"Access denied ({response.status_code}) for URL: {url}. Ensure adequate permissions."
                        )

                    return response

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

    def _format_course_id(self, course_id: str) -> str:
        if course_id.startswith("_") or course_id.startswith("courseId:"):
            return course_id
        return f"courseId:{course_id}"

    async def get_course_details(self, course_id: str) -> Dict[str, Any]:
        """
        Retrieves basic details about a course.
        """
        fmt_id = self._format_course_id(course_id)
        url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.json()
        return {"id": course_id, "name": f"Course {course_id}", "courseId": course_id}

    async def get_contents_tree(self, course_id: str) -> List[Dict[str, Any]]:
        """
        Recursively fetches the full content tree for a course.
        """
        fmt_id = self._format_course_id(course_id)
        top_url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents"
        resp = await self._request_with_retry("GET", top_url)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch top-level contents for course {course_id} ({fmt_id}): {resp.status_code}")
            return []

        data = resp.json()
        items = data.get("results", [])

        tree = []
        for item in items:
            node = await self._build_content_node(course_id, item)
            tree.append(node)

        return tree

    async def _build_content_node(self, course_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        content_id = item.get("id")
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
            "body": item.get("body", ""),
            "description": item.get("description", ""),
            "contentHandler": handler,
            "isFolder": is_folder,
            "created": item.get("created"),
            "modified": item.get("modified"),
            "children": [],
            "attachments": [],
            "links": item.get("links", []),
        }

        # Retrieve direct attachments for document / assignment items
        attachments = await self.get_content_attachments(course_id, content_id)
        
        # Also check item.links array for WebDAV / File resources
        for lk in item.get("links", []):
            href = lk.get("href", "")
            if href and ("/bbcswebdav/" in href or "/files/" in href or any(href.lower().endswith(ext) for ext in [".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip", ".png", ".jpg"])):
                title_name = lk.get("title") or title
                full_download_url = href if href.startswith("http") else f"{self.base_url}{href}"
                attachments.append({
                    "id": f"link_{content_id}_{len(attachments)+1}",
                    "fileName": title_name,
                    "downloadUrl": full_download_url,
                    "originalUrl": href,
                })

        # Also extract embedded file links from body/description HTML
        body_html = item.get("body") or item.get("description") or item.get("instructions") or ""
        if body_html:
            try:
                from bs4 import BeautifulSoup
                import urllib.parse
                soup = BeautifulSoup(body_html, "html.parser")
                idx = 0
                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    link_text = a_tag.get_text(strip=True) or f"File_{idx+1}"
                    # Check if link points to a webdav file or attachment document
                    if "/bbcswebdav/" in href or "/files/" in href or any(href.lower().endswith(ext) for ext in [".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip", ".png", ".jpg", ".jpeg", ".mp4", ".m4a"]):
                        idx += 1
                        filename = link_text
                        if not any(filename.lower().endswith(ext) for ext in [".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".zip", ".png", ".jpg", ".jpeg", ".mp4"]):
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
            except Exception as e:
                logger.warning(f"Could not parse HTML embedded links for {content_id}: {e}")

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
            
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.content
        return None
