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

        url = f"{self.base_url}/learn/api/public/v1/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials"}

        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                url,
                data=data,
                headers=headers,
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
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

    async def get_course_details(self, course_id: str) -> Dict[str, Any]:
        """
        Retrieves basic details about a course.
        """
        url = f"{self.base_url}/learn/api/public/v1/courses/{course_id}"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.json()
        return {"id": course_id, "name": f"Course {course_id}", "courseId": course_id}

    async def get_contents_tree(self, course_id: str) -> List[Dict[str, Any]]:
        """
        Recursively fetches the full content tree for a course.
        """
        top_url = f"{self.base_url}/learn/api/public/v1/courses/{course_id}/contents"
        resp = await self._request_with_retry("GET", top_url)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch top-level contents for course {course_id}: {resp.status_code}")
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
        is_folder = item.get("folder", {}).get("isFolder", False) or "folder" in handler.lower()

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

        # Retrieve attachments for document / assignment items
        attachments = await self.get_content_attachments(course_id, content_id)
        node["attachments"] = attachments

        # Recursively fetch children if folder
        if is_folder:
            child_url = f"{self.base_url}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/children"
            child_resp = await self._request_with_retry("GET", child_url)
            if child_resp.status_code == 200:
                child_data = child_resp.json()
                child_items = child_data.get("results", [])
                for child_item in child_items:
                    child_node = await self._build_content_node(course_id, child_item)
                    node["children"].append(child_node)

        return node

    async def get_content_attachments(self, course_id: str, content_id: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.json().get("results", [])
        return []

    async def download_attachment_bytes(self, course_id: str, content_id: str, attachment_id: str) -> Optional[bytes]:
        url = f"{self.base_url}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments/{attachment_id}/download"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.content
        return None
