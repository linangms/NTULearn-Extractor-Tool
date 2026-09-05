import asyncio
import ipaddress
import logging
import os
import socket
import tempfile
import urllib.parse
from typing import Any, Dict, List, Optional, Union
import httpx

logger = logging.getLogger("blackboard_client")
logging.basicConfig(level=logging.INFO)


class DiskFile:
    """
    Marks a downloaded payload that was streamed straight to a temp file on
    disk instead of being buffered fully in memory, because it grew past
    _stream_fetch_capped's in-memory spool threshold. A file this large is,
    in this app, always genuine binary media (a lecture video) - an HTML
    error/login page never gets anywhere near that size - so callers can
    treat its presence as already-validated binary content: write it into
    the zip with zf.write(path, ...) and delete the temp file afterwards,
    rather than re-reading it into memory to sniff or parse it.
    """
    def __init__(self, path: str):
        self.path = path


def _peek(content: Union[bytes, DiskFile], n: int = 64) -> bytes:
    """First n bytes of a downloaded payload, whether it's in memory or on disk."""
    if isinstance(content, DiskFile):
        try:
            with open(content.path, "rb") as f:
                return f.read(n)
        except OSError:
            return b""
    return content[:n]


def _content_len(content: Union[bytes, DiskFile]) -> int:
    if isinstance(content, DiskFile):
        try:
            return os.path.getsize(content.path)
        except OSError:
            return 0
    return len(content)


def _discard(content: Union[bytes, DiskFile]) -> None:
    """Deletes the backing temp file of a DiskFile that ended up not being used; a no-op for in-memory bytes."""
    if isinstance(content, DiskFile):
        try:
            os.remove(content.path)
        except OSError:
            pass


class FileTooLargeError(Exception):
    """
    Raised by _stream_fetch_capped when a file exceeds the size cap, so
    callers can surface a specific "skipped: too large" message to the user
    (with the file's actual/declared size) instead of a generic failure, and
    fall back to a lighter alternative (e.g. captions instead of the video
    itself) rather than just giving up on the whole item.
    """
    def __init__(self, size_bytes: int, declared: bool):
        self.size_bytes = size_bytes
        self.declared = declared  # True if known via Content-Length, False if hit mid-stream
        super().__init__(f"File exceeds size cap: {size_bytes} bytes ({'declared' if declared else 'detected mid-stream'})")


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

    async def _is_safe_public_url(self, url: str) -> bool:
        """
        SSRF guard: rejects a URL unless it's plain http(s) and every address
        its hostname resolves to is a normal public address - not loopback,
        link-local (this blocks cloud metadata endpoints like
        169.254.169.254), private, reserved, or multicast.

        Needed because attachment/video URLs elsewhere in this file can
        originate from instructor-authored course body HTML (e.g. an <a> tag
        disguised as a lecture PDF link), which this app fetches server-side
        and returns the content of, inside the student's downloaded package.
        Without this check, a malicious course item could make the server
        fetch and hand back internal/cloud-internal data.
        """
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            hostname = parsed.hostname
            if not hostname or hostname.lower() == "localhost":
                return False
            loop = asyncio.get_event_loop()
            infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
            if not infos:
                return False
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                    return False
            return True
        except Exception:
            return False

    async def _safe_fetch_external(self, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[httpx.Response]:
        """
        Fetches a URL that (unlike this file's own hardcoded Kaltura/Blackboard
        API calls) originates from instructor-authored course content, guarded
        against SSRF - see _is_safe_public_url. Also re-checks the final URL
        after any redirects, since a URL that starts out looking public could
        still redirect to an internal address.
        """
        if not await self._is_safe_public_url(url):
            logger.warning(f"Refusing to fetch potentially unsafe URL (SSRF guard): {url}")
            return None
        resp = await self._request_with_retry("GET", url, headers=headers or {}, follow_redirects=True)
        final_url = str(resp.url)
        if final_url != url and not await self._is_safe_public_url(final_url):
            logger.warning(f"Refusing to use response from unsafe redirect target (SSRF guard): {url} -> {final_url}")
            return None
        return resp

    # A real lecture video can be hundreds of MB to a few GB. _request_with_retry
    # (via httpx's default .content) buffers a whole response in memory before
    # we ever get to check its size - on a memory-constrained host that risks
    # an OOM kill (the process gets silently restarted mid-extraction, which
    # looks like a hung/dropped connection to the client). Video fetches use
    # this capped streaming path instead, aborting as soon as the size limit
    # is exceeded rather than after the whole file is already in RAM.
    MAX_VIDEO_DOWNLOAD_BYTES = 300 * 1024 * 1024  # 300MB

    # Even under the 300MB hard cap, fully buffering a file in memory is
    # risky on a host with ~512MB total RAM - a single ~200-300MB video
    # held as one bytes object, on top of everything else the process
    # already has resident, is enough to get OOM-killed mid-extraction
    # (looks like a hung/dropped connection to the client). Past this
    # threshold, _stream_fetch_capped spills the rest of the download
    # straight to a temp file instead of continuing to buffer it.
    SPOOL_TO_DISK_THRESHOLD_BYTES = 20 * 1024 * 1024  # 20MB

    async def _stream_fetch_capped(
        self, url: str, headers: Optional[Dict[str, str]] = None, max_bytes: Optional[int] = None
    ) -> Optional[Union[bytes, "DiskFile"]]:
        max_bytes = max_bytes or self.MAX_VIDEO_DOWNLOAD_BYTES
        client = self._client or httpx.AsyncClient(timeout=60.0)
        close_needed = self._client is None
        req_headers = {**self._get_headers(), **(headers or {})}
        tmp_path = None
        tmp_file = None
        try:
            async with client.stream("GET", url, headers=req_headers, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    return None
                final_url = str(resp.url)
                if final_url != url and not await self._is_safe_public_url(final_url):
                    logger.warning(f"Refusing to use response from unsafe redirect target (SSRF guard): {url} -> {final_url}")
                    return None
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    logger.warning(f"Skipping {url}: declared size {content_length} bytes exceeds {max_bytes}-byte cap")
                    raise FileTooLargeError(int(content_length), declared=True)

                buf = bytearray()
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning(f"Aborting download of {url}: exceeded {max_bytes}-byte cap mid-stream")
                        raise FileTooLargeError(total, declared=False)

                    if tmp_file is not None:
                        tmp_file.write(chunk)
                        continue

                    buf.extend(chunk)
                    if len(buf) > self.SPOOL_TO_DISK_THRESHOLD_BYTES:
                        fd, tmp_path = tempfile.mkstemp(suffix=".bin")
                        tmp_file = os.fdopen(fd, "wb")
                        tmp_file.write(bytes(buf))
                        buf = None

                if tmp_file is not None:
                    tmp_file.close()
                    tmp_file = None
                    logger.info(f"Streamed {total} bytes for {url} straight to disk (over {self.SPOOL_TO_DISK_THRESHOLD_BYTES}-byte in-memory threshold)")
                    return DiskFile(tmp_path)
                return bytes(buf)
        except FileTooLargeError:
            if tmp_file is not None:
                tmp_file.close()
                tmp_file = None
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        except Exception as e:
            logger.debug(f"Streaming fetch failed for {url}: {e}")
            return None
        finally:
            if tmp_file is not None:
                tmp_file.close()
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
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
            # Raw internal PKs must be passed directly (e.g., "_560_1") without prefixes
            return [f"_{pk}_1"]
        
        if not course_id_str.startswith("_") and not course_id_str.startswith("courseId:"):
            # Blackboard internal PKs are always purely numeric (e.g. "_560_1"),
            # so a non-numeric course_id (a course code like "CCE102") can never
            # resolve as "_CCE102_1" - that candidate is dead weight that always
            # 404s. courseId:<code> is Blackboard's documented way to reference
            # a course by its human-readable Course ID, and must come first since
            # _format_course_id() (used by attachment/content download) just
            # takes candidates[0] without trying the rest on failure.
            return [f"courseId:{course_id_str}", f"_{course_id_str}_1"]
        return [course_id_str]

    def _format_course_id(self, course_id: str) -> str:
        candidates = self._get_course_id_candidates(course_id)
        return candidates[0]

    async def get_course_details(self, course_id: str) -> Dict[str, Any]:
        candidates = self._get_course_id_candidates(course_id)
        for fmt_id in candidates:
            for api_version in ["v3", "v1"]:
                url = f"{self.base_url}/learn/api/public/{api_version}/courses/{fmt_id}"
                resp = await self._request_with_retry("GET", url)
                
                if resp.status_code == 200:
                    data = resp.json()
                    
                    # Direct GET /courses/{id}
                    if isinstance(data, dict) and "courseId" in data:
                        # Save internal ID for subsequent API calls
                        self._resolved_course_ids[str(course_id).strip()] = data.get("id", fmt_id)
                        return data
                    
                    # Search/Filter response {"results": [...]}
                    if isinstance(data, dict) and "results" in data and len(data["results"]) > 0:
                        course_data = data["results"][0]
                        self._resolved_course_ids[str(course_id).strip()] = course_data.get("id", fmt_id)
                        return course_data

        # Direct fallback if requests fail
        return {
            "id": course_id, 
            "name": f"Course {course_id}", 
            "courseId": course_id
        }

    async def get_top_level_items(self, course_id: str) -> List[Dict[str, Any]]:
        """
        Fetches just the shallow list of top-level content items (topics) for
        a course - no recursive expansion of children, attachments, or body
        text. Cheap: one API call. Pairs with build_content_node, which does
        the expensive recursive expansion for a single item at a time, so a
        caller can process a course topic-by-topic instead of needing the
        whole course's content tree in memory at once.
        """
        candidates = self._get_course_id_candidates(course_id)
        resp = None
        for fmt_id in candidates:
            top_url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents"
            resp = await self._request_with_retry("GET", top_url)
            if resp.status_code == 200:
                self._resolved_course_ids[str(course_id).strip()] = fmt_id
                break

        if not resp or resp.status_code != 200:
            logger.error(f"Failed to fetch top-level contents for course {course_id}: {resp.status_code if resp else 'No response'}")
            return []

        data = resp.json()
        return data.get("results", [])

    async def build_content_node(self, course_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Public wrapper for _build_content_node - builds ONE item's own data
        (title, body, attachments, Kaltura resolution) plus its direct
        children as raw, unexpanded API items. Call this again on any child
        to expand one more level, on demand, instead of eagerly recursing
        through the whole subtree.
        """
        return await self._build_content_node(course_id, item)

    async def _build_content_node_deep(self, course_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """Fully recursive version of build_content_node - expands every descendant eagerly. Used by get_contents_tree."""
        node = await self._build_content_node(course_id, item)
        node["children"] = [await self._build_content_node_deep(course_id, child) for child in node.get("children", [])]
        return node

    async def get_contents_tree(self, course_id: str) -> List[Dict[str, Any]]:
        """
        Recursively fetches the full content tree for a course, all at once -
        every topic and every nested folder fully expanded and held in memory
        together. Kept for callers that want that; get_top_level_items +
        build_content_node instead let a caller expand and process the course
        one topic (or even one folder) at a time, releasing each before
        fetching the next - see converter.py's streaming build methods.
        """
        items = await self.get_top_level_items(course_id)
        return [await self._build_content_node_deep(course_id, item) for item in items]

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
        import re
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
                for tag in soup.find_all(["video", "source", "audio", "iframe", "embed", "track"]):
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

                # 3. Parse Kaltura & Panopto & Media Embeds and Links
                import re
                kaltura_entries = set()

                for tag in soup.find_all(["a", "iframe", "embed", "video", "source"]):
                    val = tag.get("src") or tag.get("href") or ""
                    if any(k in val.lower() for k in ["kaltura", "panopto", "calt.ntu.edu.sg", "media.ntu.edu.sg", "video.ntu.edu.sg", "cdnapisec.kaltura.com"]):
                        e_match = re.search(r'(?:entry_id[=/]|entryId/|kaltura.*?/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b', val, re.I)
                        if e_match:
                            entry_id = e_match.group(1)
                            kaltura_entries.add((entry_id, val))
                        else:
                            idx += 1
                            full_url = val if val.startswith("http") else f"{self.base_url}{val}"
                            attachments.append({
                                "id": full_url,
                                "fileName": f"{title}_video_{idx}.mp4",
                                "downloadUrl": full_url,
                                "originalUrl": val,
                                "isKaltura": True,
                            })

                # Regex search entire body_html for any Kaltura entry_id strings (e.g. 1_abc12345 or 0_98765432)
                for e_match in re.finditer(r'(?:entry_id[=/]|entryId/|kaltura.*?/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b', body_html, re.I):
                    entry_id = e_match.group(1)
                    if not any(e[0] == entry_id for e in kaltura_entries):
                        kaltura_entries.add((entry_id, f"https://cdnapisec.kaltura.com/p/2342341/sp/234234100/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4"))

                for entry_id, orig_url in kaltura_entries:
                    idx += 1
                    p_match = re.search(r'/p/(\d+)', orig_url)
                    partner_id = p_match.group(1) if p_match else "2342341"
                    manifest_url = f"https://cdnapisec.kaltura.com/p/{partner_id}/sp/{partner_id}00/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4"

                    attachments.append({
                        "id": manifest_url,
                        "fileName": f"{title}.mp4" if idx == 1 else f"{title}_video_{idx}.mp4",
                        "downloadUrl": manifest_url,
                        "originalUrl": orig_url,
                        "isKaltura": True,
                        "entryId": entry_id,
                    })

            except Exception as e:
                logger.warning(f"Could not parse HTML embedded links/media for {content_id}: {e}")

        # 4. Check handler / links for Kaltura / LTI placement items
        handler_obj = item.get("contentHandler", {})
        handler_str = str(handler_obj).lower()
        is_kaltura_handler = "kaltura" in handler_str or "media" in handler_str or "blti" in handler_str or "video" in handler_str

        # Blackboard's contentHandler for an LTI-linked item can carry the tool's
        # custom launch parameters (e.g. customParameters) directly in this JSON -
        # if the Kaltura entry id is in there, we can use it without ever needing
        # the interactive (SSO-gated) launch link at all. Search the raw object,
        # not just its .id, and also the item's links array as a second source.
        handler_entry_id_match = re.search(
            r'(?:entry_id[=/"\':]|entryId/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b',
            str(handler_obj) + " " + str(item.get("links", [])),
            re.I,
        )
        if is_kaltura_handler:
            if handler_entry_id_match:
                logger.info(f"Kaltura entry id found directly in contentHandler/links for content {content_id}: {handler_entry_id_match.group(1)}")
            else:
                logger.info(f"No Kaltura entry id in contentHandler/links for content {content_id}. contentHandler={handler_obj!r} links={item.get('links', [])!r}")

        for lk in item.get("links", []):
            href = lk.get("href", "")
            if href and any(k in href.lower() for k in ["kaltura", "osstream-kaltura", "launchlink", "blti", "panopto", "calt.ntu.edu.sg", "media.ntu.edu.sg"]):
                full_lk_url = href if href.startswith("http") else f"{self.base_url}{href}"
                e_match = re.search(r'(?:entry_id[=/]|entryId/|kaltura.*?/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b', full_lk_url, re.I)
                entry_id = e_match.group(1) if e_match else ""
                
                if not any(att.get("originalUrl") == full_lk_url or (entry_id and att.get("entryId") == entry_id) for att in attachments):
                    if entry_id:
                        manifest_url = f"https://cdnapisec.kaltura.com/p/2342341/sp/234234100/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4"
                        attachments.append({
                            "id": manifest_url,
                            "fileName": f"{title}.mp4",
                            "downloadUrl": manifest_url,
                            "originalUrl": full_lk_url,
                            "isKaltura": True,
                            "entryId": entry_id,
                        })
                    else:
                        attachments.append({
                            "id": full_lk_url,
                            "fileName": f"{title}.mp4",
                            "downloadUrl": full_lk_url,
                            "originalUrl": full_lk_url,
                            "isKaltura": True,
                        })

        if is_kaltura_handler and not attachments:
            fmt_id = self._format_course_id(course_id)
            launch_url = f"{self.base_url}/webapps/blackboard/execute/blti/launchLink?course_id={fmt_id}&content_id={content_id}&from_ultra=true"
            if handler_entry_id_match:
                entry_id = handler_entry_id_match.group(1)
                manifest_url = f"https://api.sg.kaltura.com/p/137/sp/13700/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4"
                attachments.append({
                    "id": manifest_url,
                    "fileName": f"{title}.mp4",
                    "downloadUrl": manifest_url,
                    "originalUrl": launch_url,
                    "isKaltura": True,
                    "entryId": entry_id,
                })
            else:
                # No entry id anywhere in Blackboard's own data for this item - this
                # URL requires an active browser session to resolve (SSO-gated), so
                # it will only ever work as a "watch in browser" link, not a direct
                # download; download_attachment_bytes/converter still try to resolve
                # it server-side as a last resort.
                attachments.append({
                    "id": launch_url,
                    "fileName": f"{title}.mp4",
                    "downloadUrl": launch_url,
                    "originalUrl": launch_url,
                    "isKaltura": True,
                })

        node["attachments"] = attachments

        # Always check if children exist for this item (covers modules, lessons, folders).
        # Deliberately shallow: children are left as raw, un-built API items (no
        # attachments/body/Kaltura resolution done on them yet) rather than
        # recursed into here. A node with a deep folder tree - e.g. a single
        # topic containing many tutorial sub-folders full of PDFs and videos -
        # would otherwise force the ENTIRE subtree to be fetched and held in
        # memory before any of it could be processed, the same problem
        # topic-by-topic streaming solves one level up. build_content_node
        # (this method) is called again on each raw child, one at a time, as
        # a caller actually descends into it - see converter.py's
        # _ensure_expanded, which recognizes an unexpanded child by the
        # absence of the "attachments" key this method always sets.
        fmt_id = self._format_course_id(course_id)
        child_url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/children"
        child_resp = await self._request_with_retry("GET", child_url)
        if child_resp.status_code == 200:
            child_items = child_resp.json().get("results", [])
            if child_items:
                node["isFolder"] = True
                node["children"] = child_items

        return node

    async def get_content_attachments(self, course_id: str, content_id: str) -> List[Dict[str, Any]]:
        fmt_id = self._format_course_id(course_id)
        url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/attachments"
        resp = await self._request_with_retry("GET", url)
        if resp.status_code == 200:
            return resp.json().get("results", [])
        return []

    async def _try_download_kaltura_video(self, entry_id: str, orig_url: str) -> Optional[Union[bytes, DiskFile]]:
        """
        Attempts to fetch direct MP4 video bytes for a Kaltura entry_id across candidate partner IDs.
        """
        import re
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{self.base_url}/",
        }
        
        partner_candidates = []
        p_match = re.search(r'/p/(\d+)', orig_url)
        if p_match:
            partner_candidates.append(p_match.group(1))

        for p in ["137", "2342341", "2092301", "102", "103", "0"]:
            if p not in partner_candidates:
                partner_candidates.append(p)

        for pid in partner_candidates:
            urls = [
                f"https://api.sg.kaltura.com/p/{pid}/sp/{pid}00/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4",
                f"https://cdnapisec.kaltura.com/p/{pid}/sp/{pid}00/playManifest/entryId/{entry_id}/format/url/flavorParamId/0/video.mp4",
                f"https://cdn.kaltura.com/p/{pid}/sp/{pid}00/playManifest/entryId/{entry_id}/format/url/protocol/https/flavorParamId/0/video.mp4",
                f"https://cfvod.kaltura.com/pd/p/{pid}/sp/{pid}00/serveFlavor/entryId/{entry_id}/v/11/flavorId/0/name/video.mp4",
            ]
            for candidate_url in urls:
                try:
                    content = await self._stream_fetch_capped(candidate_url, headers=headers)
                    if content and _content_len(content) > 1000:
                        snippet = _peek(content)
                        looks_like_error = snippet.startswith(b"<") or snippet.startswith(b"{") or b"<?xml" in snippet or b"404 Not Found" in snippet
                        looks_like_video = b"ftyp" in snippet or b"moov" in snippet or snippet.startswith(b"\x00\x00\x00")
                        if not looks_like_error and looks_like_video:
                            logger.info(f"Successfully downloaded valid MP4 Kaltura video for {entry_id} ({_content_len(content)} bytes) via {candidate_url}")
                            return content
                    if content:
                        _discard(content)
                except FileTooLargeError:
                    # Every candidate URL serves the same underlying video, so
                    # if one mirror is too large they all will be - no point
                    # trying the rest. Propagate so the caller can report the
                    # actual size and fall back to captions-only.
                    raise
                except Exception as e:
                    logger.debug(f"Kaltura candidate URL failed ({candidate_url}): {e}")

        if orig_url and orig_url.startswith("http"):
            try:
                if await self._is_safe_public_url(orig_url):
                    content = await self._stream_fetch_capped(orig_url, headers=headers)
                    if content and _content_len(content) > 1000:
                        snippet = _peek(content)
                        looks_like_error = snippet.startswith(b"<") or snippet.startswith(b"{") or b"<?xml" in snippet or b"404 Not Found" in snippet
                        looks_like_video = b"ftyp" in snippet or b"moov" in snippet or snippet.startswith(b"\x00\x00\x00")
                        if not looks_like_error and looks_like_video:
                            return content
                    if content:
                        _discard(content)
                else:
                    logger.warning(f"Refusing to fetch potentially unsafe URL (SSRF guard): {orig_url}")
            except FileTooLargeError:
                raise
            except Exception as e:
                logger.debug(f"Kaltura orig_url failed ({orig_url}): {e}")

    # NTU's Kaltura instance is on the Singapore regional cluster (api.sg.kaltura.com)
    # under partner id 137, confirmed from a live embed iframe's src URL. That partner
    # id/host is tried first; the rest are kept as a fallback for other entries/tenants.
    KALTURA_API_HOSTS = ["api.sg.kaltura.com", "cdnapisec.kaltura.com"]
    KALTURA_PARTNER_IDS = ["137", "2342341", "2092301", "102", "103", "0"]

    async def _get_kaltura_ks_token(self, partner_id: str = "137", api_host: str = "api.sg.kaltura.com") -> Optional[str]:
        """
        Obtains an authenticated Kaltura Session (ks) token for the user/session.
        """
        session_url = f"https://{api_host}/api_v3/service/session/action/startWidgetSession?format=1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{self.base_url}/",
        }
        body = {"widgetId": f"_{partner_id}"}
        try:
            resp = await self._request_with_retry("POST", session_url, json=body, headers=headers, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("ks"):
                    return data["ks"]
        except Exception as e:
            logger.debug(f"Failed to start Kaltura widget session for partner {partner_id} on {api_host}: {e}")
        return None

    async def _try_download_kaltura_captions(self, entry_id: str) -> Optional[bytes]:
        """
        Queries Kaltura caption assets API using authenticated session token (ks) for attached closed captions/subtitles (.vtt / .srt) and downloads them.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{self.base_url}/",
        }
        for api_host in self.KALTURA_API_HOSTS:
            for partner_id in self.KALTURA_PARTNER_IDS:
                ks = await self._get_kaltura_ks_token(partner_id, api_host)
                ks_param = f"&ks={ks}" if ks else ""
                api_url = f"https://{api_host}/api_v3/service/caption_captionasset/action/list?filter:entryIdEqual={entry_id}&partnerId={partner_id}{ks_param}&format=1"
                try:
                    resp = await self._request_with_retry("GET", api_url, headers=headers, follow_redirects=True)
                    if resp.status_code == 200:
                        data = resp.json()
                        objects = data.get("objects", []) if isinstance(data, dict) else []
                        for obj in objects:
                            caption_id = obj.get("id")
                            if caption_id:
                                serve_urls = [
                                    f"https://{api_host}/api_v3/service/caption_captionasset/action/serve/captionAssetId/{caption_id}?partnerId={partner_id}{ks_param}",
                                    f"https://{api_host}/api_v3/service/caption_captionasset/action/servewebvtt/captionAssetId/{caption_id}?partnerId={partner_id}{ks_param}",
                                    f"https://{api_host}/api_v3/service/caption_captionasset/action/exportToSrt/id/{caption_id}?partnerId={partner_id}{ks_param}",
                                ]
                                for serve_url in serve_urls:
                                    try:
                                        cap_resp = await self._request_with_retry("GET", serve_url, headers=headers, follow_redirects=True)
                                        if cap_resp.status_code == 200 and len(cap_resp.content) > 10:
                                            if not cap_resp.content.startswith(b"<") and not cap_resp.content.startswith(b"{"):
                                                return cap_resp.content
                                    except Exception as e:
                                        logger.debug(f"Kaltura serve caption URL failed ({serve_url}): {e}")
                except Exception as e:
                    logger.debug(f"Could not fetch Kaltura captions for {entry_id} via partner {partner_id} on {api_host}: {e}")
        return None

    def _extract_kaltura_entry_id(self, text: str) -> Optional[str]:
        import re
        e_match = re.search(r'(?:entry_id[=/]|entryId/|kaltura.*?/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b', text, re.I)
        return e_match.group(1) if e_match else None

    async def _resolve_kaltura_url(self, text: str) -> Optional[str]:
        """
        Some video items (e.g. an LTI launch link like .../execute/blti/launchLink)
        never expose a Kaltura entry id in Blackboard's own API response - it only
        appears after the launch page navigates (often via client-side JavaScript,
        not an HTTP redirect) to the final Kaltura browseandembed/media-redirect
        URL. This follows any URL(s) found in `text`, and since a plain GET can't
        execute that JS navigation, also scans the fetched page's own HTML/JS body
        for an embedded Kaltura URL (e.g. in an iframe src or a window.location
        assignment) - the browser has to get that destination from somewhere in
        the page it received, so it should be present as plain text even when no
        real HTTP redirect occurs.

        Best-effort: an LTI launch link normally requires the user's active browser
        session, so this may simply fail to resolve past a login page when run
        server-side - callers must treat a None result as "couldn't resolve".
        """
        import re
        urls = re.findall(r'https?://[^\s"\'<>]+', text or "")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{self.base_url}/",
        }
        for url in urls[:5]:
            try:
                resp = await self._safe_fetch_external(url, headers=headers)
                if resp is None:
                    continue
                final_url = str(resp.url)
                entry_id = self._extract_kaltura_entry_id(final_url)
                body = resp.text if not entry_id else ""
                if not entry_id and body:
                    # No HTTP redirect happened - look for the actual destination
                    # URL embedded in the page's own HTML/JS (iframe src, a plain
                    # href, or a window.location assignment).
                    for candidate in re.findall(r'https?://[^\s"\'<>\\]+', body):
                        if self._extract_kaltura_entry_id(candidate):
                            entry_id = self._extract_kaltura_entry_id(candidate)
                            final_url = candidate
                            break
                logger.info(
                    f"Kaltura URL resolution: {url} -> status={resp.status_code} "
                    f"final_url={final_url} entry_id_found={bool(entry_id)} "
                    f"body_len={len(body)} body_snippet={body[:300]!r}"
                )
                if entry_id:
                    return final_url
            except Exception as e:
                logger.warning(f"Could not resolve Kaltura URL via redirect for {url}: {e}")
        return None

    async def resolve_kaltura_embed_url(self, text: str) -> Optional[str]:
        """
        Returns a direct, redirect-resolved Kaltura embed URL for `text` if one can
        be found, so callers (e.g. the raw-file HTML launcher) can link straight to
        it instead of an intermediate LTI launch link. Returns None if `text`
        already contains a resolvable entry id (nothing to improve) or resolution fails.
        """
        if self._extract_kaltura_entry_id(text):
            return None
        return await self._resolve_kaltura_url(text)

    async def _resolve_kaltura_entry_id(self, text: str) -> Optional[str]:
        entry_id = self._extract_kaltura_entry_id(text)
        if entry_id:
            return entry_id
        resolved_url = await self._resolve_kaltura_url(text)
        return self._extract_kaltura_entry_id(resolved_url) if resolved_url else None

    async def download_kaltura_caption_bytes(self, attachment_id: str) -> Optional[bytes]:
        """
        Fetches the real .srt/.vtt caption asset for a Kaltura video attachment,
        independent of whether the video itself downloads successfully.
        """
        entry_id = await self._resolve_kaltura_entry_id(attachment_id)
        if not entry_id:
            return None
        return await self._try_download_kaltura_captions(entry_id)

    async def download_kaltura_video_bytes(self, attachment_id: str) -> Optional[Union[bytes, DiskFile]]:
        """
        Fetches raw MP4 bytes for a Kaltura video entry embedded via body/description
        HTML (i.e. with no separate downloadable attachment entry pointing at it).
        """
        entry_id = await self._resolve_kaltura_entry_id(attachment_id)
        if not entry_id:
            return None
        return await self._try_download_kaltura_video(entry_id, attachment_id)

    async def download_attachment_bytes(self, course_id: str, content_id: str, attachment_id: str) -> Optional[Union[bytes, DiskFile]]:
        import re
        if "kaltura" in attachment_id.lower() or re.search(r'\b([01]_[a-zA-Z0-9]{8,12})\b', attachment_id):
            entry_id = self._extract_kaltura_entry_id(attachment_id)
            if entry_id:
                k_bytes = await self._try_download_kaltura_video(entry_id, attachment_id)
                if k_bytes:
                    return k_bytes
                cap_bytes = await self._try_download_kaltura_captions(entry_id)
                if cap_bytes:
                    return cap_bytes

        if attachment_id.startswith("http"):
            url = attachment_id
        elif attachment_id.startswith("/"):
            url = f"{self.base_url}{attachment_id}"
        else:
            fmt_id = self._format_course_id(course_id)
            url = f"{self.base_url}/learn/api/public/v1/courses/{fmt_id}/contents/{content_id}/attachments/{attachment_id}/download"
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{self.base_url}/",
        }
        if not await self._is_safe_public_url(url):
            logger.warning(f"Refusing to fetch potentially unsafe URL (SSRF guard): {url}")
            return None

        content = await self._stream_fetch_capped(url, headers=headers)
        if content is None:
            logger.error(f"Failed (or refused, or exceeded size cap) to download attachment {attachment_id} (URL: {url})")
            return None

        # A DiskFile only ever comes from content that grew past the in-memory
        # spool threshold (tens of MB) - an HTML launch page never gets
        # anywhere near that size, so there's nothing to scan; it's already
        # confirmed binary media.
        if isinstance(content, DiskFile):
            return content

        # If response is HTML launch page, scan for embedded Kaltura entry_id or .mp4 URL
        if content.startswith(b"<!DOCTYPE") or content.startswith(b"<html") or content.startswith(b"<?xml") or b"<iframe" in content:
            try:
                html_text = content.decode("utf-8", errors="ignore")
                e_match = re.search(r'(?:entry_id[=/]|entryId/|kaltura.*?/|entry_id=|\b)([01]_[a-zA-Z0-9]{8,12})\b', html_text, re.I)
                if e_match:
                    found_entry_id = e_match.group(1)
                    k_bytes = await self._try_download_kaltura_video(found_entry_id, url)
                    if k_bytes:
                        return k_bytes

                mp4_match = re.search(r'(https?://[^\s"\']+\.mp4(?:\?[^\s"\']*)?)', html_text, re.I)
                if mp4_match:
                    mp4_url = mp4_match.group(1)
                    if await self._is_safe_public_url(mp4_url):
                        mp4_content = await self._stream_fetch_capped(mp4_url, headers=headers)
                        if mp4_content and _content_len(mp4_content) > 10000:
                            return mp4_content
                        if mp4_content:
                            _discard(mp4_content)
            except Exception as e:
                logger.debug(f"Error parsing launch HTML page for video stream ({url}): {e}")

        return content
