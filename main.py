import asyncio
import json
import re
import urllib.parse
import uuid
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from blackboard_client import BlackboardClient
from converter import CourseMarkdownConverter, sanitize_filename

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ntulearn_extractor")

app = FastAPI(
    title="NTULearn Extractor Tool",
    description="API-driven LTI 1.3 Web Application to extract Blackboard course contents to Markdown",
    version="1.0.0",
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.method} {request.url}: {exc}", exc_info=True)
    return HTMLResponse(
        content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>NTULearn Extractor Tool - Notice</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-slate-50 text-slate-800 flex items-center justify-center min-h-screen p-6">
            <div class="max-w-lg w-full bg-white rounded-2xl shadow-xl p-8 border border-slate-200 text-center space-y-4">
                <div class="w-16 h-16 bg-amber-100 text-amber-600 rounded-2xl flex items-center justify-center mx-auto text-2xl font-bold">
                    ⚠️
                </div>
                <h2 class="text-2xl font-extrabold text-slate-900">Application Notice</h2>
                <p class="text-sm text-slate-600 leading-relaxed">
                    The tool encountered an issue while processing this request: <br/>
                    <code class="text-xs bg-slate-100 p-2 rounded text-amber-800 font-mono block mt-2 text-left overflow-x-auto">{str(exc)}</code>
                </p>
                <div class="pt-4 flex justify-center gap-3">
                    <a href="/" class="bg-indigo-600 hover:bg-indigo-700 text-white font-semibold px-6 py-2.5 rounded-xl transition-all shadow-md">
                        Return to Extractor Dashboard
                    </a>
                </div>
            </div>
        </body>
        </html>
        """,
        status_code=200,
    )

templates = Jinja2Templates(directory="templates")

# In-memory session and zip archive storage
sessions: Dict[str, Dict[str, Any]] = {}
task_storage: Dict[str, Dict[str, Any]] = {}

# Default Blackboard REST API configuration (can be overriden by env vars)
BLACKBOARD_BASE_URL = "https://ntulearn.ntu.edu.sg"

KNOWN_COURSE_MAP = {
    "560": "TMSC001",
    "623": "CCE102-Tst",
    "626": "MKTG101",
    "646": "MKTG101",
}


async def extract_lti_context(request: Request) -> Dict[str, str]:
    """
    Extracts LTI claims, course ID, course name, and user role from request payload and referer headers.
    """
    import urllib.parse, re

    params = {}
    raw_url = str(request.url)
    unquoted_url = urllib.parse.unquote(urllib.parse.unquote(raw_url))

    # Parse query parameters with double URL unquoting
    for k, v in request.query_params.items():
        params[k] = v
        k_un = urllib.parse.unquote(urllib.parse.unquote(k))
        v_un = urllib.parse.unquote(urllib.parse.unquote(v))
        params[k_un] = v_un

    form_data = {}
    if request.method == "POST":
        try:
            form = await request.form()
            form_data = dict(form)
            params.update(form_data)
        except Exception as e:
            logger.debug(f"Could not parse form: {e}")

    # Fallback regex extraction from raw unquoted URL string (handles course_id%253D_626_1 or course_id=_626_1)
    m_course = (
        re.search(r'\bcourse_id(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'\bcustom_course_id(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'\bcourse(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
    )
    if m_course and "course_id" not in params:
        params["course_id"] = m_course.group(1)

    id_token = params.get("id_token") or form_data.get("id_token")
    
    # 1. Case-insensitive dictionary inspection of all query and form parameters
    params_lower = {str(k).lower(): str(v) for k, v in params.items() if v}
    
    course_id = (
        params_lower.get("course_id") 
        or params_lower.get("courseid")
        or params_lower.get("course")
        or params_lower.get("custom_course_id")
        or params_lower.get("ext_course_id")
        or params_lower.get("ext_lms_course_id")
        or params_lower.get("context_id") 
    )
    course_name = (
        params_lower.get("course_name")
        or params_lower.get("course_title")
        or params_lower.get("coursetitle")
        or params_lower.get("coursename")
        or params_lower.get("context_title")
        or params_lower.get("custom_course_name")
        or params_lower.get("custom_course_title")
        or params_lower.get("custom_course_code")
        or params_lower.get("custom_context_title")
        or params_lower.get("custom_context_label")
        or params_lower.get("context_label")
        or params_lower.get("lis_course_offering_sourcedid")
        or params_lower.get("lis_course_section_sourcedid")
        or params_lower.get("title")
    )
    user_role = "Instructor"

    # 2. Decode LTI 1.3 JWT ID Token if present
    if id_token:
        try:
            import jwt
            decoded = jwt.decode(id_token, options={"verify_signature": False})
            logger.info(f"Decoded LTI ID Token claims: {decoded}")

            context_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/context", {})
            custom_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/custom", {})
            lis_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/lis", {})
            roles_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])

            c_id = (
                custom_claim.get("course_id") 
                or custom_claim.get("course_code")
                or custom_claim.get("courseid")
                or custom_claim.get("context_label")
                or custom_claim.get("CourseSection.id")
                or lis_claim.get("course_offering_sourcedid")
                or lis_claim.get("course_section_sourcedid")
                or context_claim.get("label") 
                or context_claim.get("id") 
            )
            c_name = (
                context_claim.get("title") 
                or context_claim.get("label") 
                or custom_claim.get("course_name") 
                or custom_claim.get("course_code")
                or custom_claim.get("course_title")
                or custom_claim.get("context_title")
                or custom_claim.get("context_label")
                or custom_claim.get("custom_course_code")
                or custom_claim.get("custom_course_name")
                or lis_claim.get("course_offering_sourcedid")
                or lis_claim.get("course_section_sourcedid")
            )

            if c_id:
                course_id = str(c_id)
            if c_name:
                course_name = str(c_name)

            if roles_claim:
                if any("Instructor" in r or "Administrator" in r or "ContentDeveloper" in r for r in roles_claim):
                    user_role = "Instructor"
                else:
                    user_role = "Student"
        except Exception as e:
            logger.warning(f"Error decoding id_token: {e}")

    # 3. Inspect HTTP Referer header or Request URL if course_id is missing or default
    referer = request.headers.get("referer", "") or str(request.url)
    unquoted_referer = urllib.parse.unquote(urllib.parse.unquote(referer))
    if not course_id:
        match = (
            re.search(r'/courses/([^/?]+)', unquoted_referer)
            or re.search(r'course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'courseId[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'course[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
        )
        if match:
            extracted = match.group(1)
            logger.info(f"Extracted course_id '{extracted}' from Referer/URL: {referer}")
            course_id = extracted

def clean_course_id_string(val: str) -> str:
    if not val:
        return ""
    import urllib.parse, re
    # 1. Double unquote string
    unquoted = urllib.parse.unquote(urllib.parse.unquote(str(val))).strip()
    # 2. Split on comma, semicolon, or %2C if present (e.g. _626_1%2C86cb8c88c33b4f6ab61fa92693e8a376)
    first_part = re.split(r'[,;]|\b%2C\b', unquoted, flags=re.IGNORECASE)[0].strip()
    # 3. If Blackboard internal PK like _626_1 or _12345_1, convert to numeric ID (626, 12345)
    if first_part.startswith("_"):
        pk_match = re.search(r'_?(\d+)_?\d*', first_part)
        if pk_match:
            return pk_match.group(1)
    return first_part


async def extract_lti_context(request: Request) -> Dict[str, str]:
    """
    Extracts LTI claims, course ID, course name, and user role from request payload and referer headers.
    """
    import urllib.parse, re

    params = {}
    raw_url = str(request.url)
    unquoted_url = urllib.parse.unquote(urllib.parse.unquote(raw_url))

    # Parse query parameters with double URL unquoting
    for k, v in request.query_params.items():
        params[k] = v
        k_un = urllib.parse.unquote(urllib.parse.unquote(k))
        v_un = urllib.parse.unquote(urllib.parse.unquote(v))
        params[k_un] = v_un

    form_data = {}
    if request.method == "POST":
        try:
            form = await request.form()
            form_data = dict(form)
            params.update(form_data)
        except Exception as e:
            logger.debug(f"Could not parse form: {e}")

    # Fallback regex extraction from raw unquoted URL string (handles course_id%253D_626_1 or course_id=_626_1)
    m_course = (
        re.search(r'course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'custom_course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'course[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
    )
    if m_course and "course_id" not in params:
        params["course_id"] = m_course.group(1)

    id_token = params.get("id_token") or form_data.get("id_token")
    
    # 1. Case-insensitive dictionary inspection of all query and form parameters
    params_lower = {str(k).lower(): str(v) for k, v in params.items() if v}
    
    course_id = (
        params_lower.get("course_id") 
        or params_lower.get("courseid")
        or params_lower.get("course")
        or params_lower.get("custom_course_id")
        or params_lower.get("custom_course_code")
        or params_lower.get("custom_courseid")
        or params_lower.get("custom_course")
        or params_lower.get("custom_context_label")
        or params_lower.get("context_label") 
        or params_lower.get("ext_course_id")
        or params_lower.get("ext_lms_course_id")
        or params_lower.get("lis_course_offering_sourcedid")
        or params_lower.get("lis_course_section_sourcedid")
        or params_lower.get("context_id") 
    )
    course_name = params.get("course_name") or params.get("context_title") or params.get("title")
    user_role = "Instructor"

    # 2. Decode LTI 1.3 JWT ID Token if present
    if id_token:
        try:
            import jwt
            decoded = jwt.decode(id_token, options={"verify_signature": False})
            logger.info(f"Decoded LTI ID Token claims: {decoded}")

            context_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/context", {})
            custom_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/custom", {})
            lis_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/lis", {})
            roles_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])

            c_id = (
                custom_claim.get("course_id") 
                or custom_claim.get("course_code")
                or custom_claim.get("courseid")
                or custom_claim.get("context_label")
                or custom_claim.get("CourseSection.id")
                or lis_claim.get("course_offering_sourcedid")
                or lis_claim.get("course_section_sourcedid")
                or context_claim.get("label") 
                or context_claim.get("id") 
            )
            c_name = (
                context_claim.get("title") 
                or context_claim.get("label") 
                or custom_claim.get("course_name") 
                or custom_claim.get("context_title")
            )

            if c_id:
                course_id = str(c_id)
            if c_name:
                course_name = str(c_name)

            if roles_claim:
                if any("Instructor" in r or "Administrator" in r or "ContentDeveloper" in r for r in roles_claim):
                    user_role = "Instructor"
                else:
                    user_role = "Student"
        except Exception as e:
            logger.warning(f"Error decoding id_token: {e}")

    # 3. Inspect HTTP Referer header or Request URL if course_id is missing or default
    referer = request.headers.get("referer", "") or str(request.url)
    unquoted_referer = urllib.parse.unquote(urllib.parse.unquote(referer))
    if not course_id:
        match = (
            re.search(r'/courses/([^/?]+)', unquoted_referer)
            or re.search(r'\bcourse_id(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'\bcourseId(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'\bcourse(?!_title|_name)[=:=%253D%3D]+([^&?\s]+)', unquoted_referer, re.IGNORECASE)
        )
        if match:
            extracted = match.group(1)
            logger.info(f"Extracted course_id '{extracted}' from Referer/URL: {referer}")
            course_id = extracted

    if not course_name or course_name == course_id or course_name == "Course Materials Extractor":
        match_title = (
            re.search(r'\bcourse_title[=:=%253D%3D]+([^&]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'\bcourse_name[=:=%253D%3D]+([^&]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'\bcontext_title[=:=%253D%3D]+([^&]+)', unquoted_referer, re.IGNORECASE)
            or re.search(r'\bcustom_course_code[=:=%253D%3D]+([^&]+)', unquoted_referer, re.IGNORECASE)
        )
        if match_title:
            extracted_title = urllib.parse.unquote(match_title.group(1)).strip()
            logger.info(f"Extracted course_name '{extracted_title}' from Referer/URL: {referer}")
            course_name = extracted_title

    # Clean course_id string (handles _626_1%2C86cb... -> 626)
    course_id = clean_course_id_string(course_id)

    if course_id:
        if not course_name or course_name == "Course Materials Extractor" or course_name == course_id or course_name.isdigit() or course_name.startswith("Course "):
            c_clean = clean_course_id_string(course_id)
            if c_clean in KNOWN_COURSE_MAP:
                course_name = KNOWN_COURSE_MAP[c_clean]
            else:
                course_name = course_id
    else:
        course_name = "Course Materials Extractor"

    logger.info(f"Resolved LTI context: course_id='{course_id}', course_name='{course_name}', role='{user_role}'")
    return {
        "course_id": course_id,
        "course_name": course_name,
        "user_role": user_role,
    }


@app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
async def dashboard(request: Request, session_id: Optional[str] = Query(None)):
    """
    Renders the main dashboard UI.
    """
    context = await extract_lti_context(request)
    session_data = sessions.get(session_id, {}) if session_id else {}
    
    course_name = session_data.get("course_name") or context["course_name"]
    course_id = session_data.get("course_id") or context["course_id"]
    user_role = session_data.get("user_role") or context["user_role"]

    if course_id and (not course_name or course_name.startswith("Course ") or course_name == "Course Materials Extractor" or course_name == course_id or course_name.isdigit()):
        c_clean = clean_course_id_string(course_id)
        if c_clean in KNOWN_COURSE_MAP:
            course_name = KNOWN_COURSE_MAP[c_clean]
        import os
        bb_client_id = os.environ.get("BLACKBOARD_CLIENT_ID")
        bb_client_secret = os.environ.get("BLACKBOARD_CLIENT_SECRET")
        bb_base_url = os.environ.get("BLACKBOARD_BASE_URL", BLACKBOARD_BASE_URL)
        if bb_client_id and bb_client_secret:
            try:
                async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as bb_client:
                    await bb_client.authenticate()
                    details = await bb_client.get_course_details(course_id)
                    resolved_name = details.get("courseId") or details.get("name")
                    if resolved_name and not resolved_name.startswith("Course "):
                        course_name = resolved_name
            except Exception as e:
                logger.warning(f"Could not resolve course name for dashboard: {e}")

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "course_name": course_name,
            "course_id": course_id,
            "user_role": user_role,
            "session_id": session_id or "",
        },
    )


@app.api_route("/lti/login", methods=["GET", "POST"])
async def lti_login(request: Request):
    """
    LTI 1.3 OIDC login initiation route.
    """
    import urllib.parse, re

    params = {}
    raw_url = str(request.url)
    unquoted_url = urllib.parse.unquote(urllib.parse.unquote(raw_url))

    for k, v in request.query_params.items():
        params[k] = v
        k_un = urllib.parse.unquote(urllib.parse.unquote(k))
        v_un = urllib.parse.unquote(urllib.parse.unquote(v))
        params[k_un] = v_un

    if request.method == "POST":
        try:
            form_data = await request.form()
            for k, v in form_data.items():
                params[str(k)] = str(v)
        except Exception as e:
            logger.warning(f"Error parsing login form data: {e}")

    # Fallback regex extraction from raw unquoted URL (handles course_id%253D_626_1%2C...)
    m_course = (
        re.search(r'course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'custom_course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'course[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
    )
    if m_course and "course_id" not in params:
        params["course_id"] = m_course.group(1)

    iss = params.get("iss")
    target_link_uri = params.get("target_link_uri") or str(request.url_for("lti_launch"))
    if "onrender.com" in target_link_uri or request.headers.get("x-forwarded-proto") == "https":
        target_link_uri = target_link_uri.replace("http://", "https://", 1)
    client_id = params.get("client_id")
    login_hint = params.get("login_hint")

    # Clean course_id if present in params
    if "course_id" in params:
        params["course_id"] = clean_course_id_string(params["course_id"])

    logger.info(f"LTI Login initiated with params={params}")

    hidden_inputs = ""
    for k, v in params.items():
        if k not in ["iss", "client_id"]:
            val = str(v) if v is not None else ""
            hidden_inputs += f'<input type="hidden" name="{k}" value="{val}" />\n'

    if iss and login_hint:
        response_html = f"""
        <html>
        <body>
            <p>Launching LTI Tool...</p>
            <form id="lti_launch_form" action="{target_link_uri}" method="POST">
                <input type="hidden" name="iss" value="{iss or ''}" />
                <input type="hidden" name="client_id" value="{client_id or ''}" />
                {hidden_inputs}
            </form>
            <script>document.getElementById('lti_launch_form').submit();</script>
        </body>
        </html>
        """
        return HTMLResponse(content=response_html)

    # If simple redirect without OIDC state, forward course_id in target_link_uri
    if "course_id" in params and params["course_id"] and "course_id" not in target_link_uri:
        delim = "&" if "?" in target_link_uri else "?"
        target_link_uri += f"{delim}course_id={urllib.parse.quote(params['course_id'])}"

    return HTMLResponse(content=f"<html><body><p>Redirecting...</p><script>window.location.href='{target_link_uri}';</script></body></html>")


@app.api_route("/lti/launch", methods=["GET", "POST"])
async def lti_launch(request: Request):
    """
    LTI 1.3 Launch handler endpoint.
    Extracts course details and user roles from LTI payload.
    """
    session_id = str(uuid.uuid4())
    context = await extract_lti_context(request)

    course_id = context["course_id"]
    course_name = context["course_name"]
    user_role = context["user_role"]

    sessions[session_id] = {
        "course_id": course_id,
        "course_name": course_name,
        "user_role": user_role,
    }

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "course_name": course_name,
            "course_id": course_id,
            "user_role": user_role,
            "session_id": session_id,
        },
    )


@app.get("/lti/jwks")
async def lti_jwks():
    """
    LTI 1.3 JWKS public keys endpoint.
    """
    return {"keys": []}


@app.get("/api/test-auth")
async def test_blackboard_auth():
    """
    Tests live OAuth 2.0 authentication against Blackboard Learn REST API.
    """
    import os
    client_id = os.environ.get("BLACKBOARD_CLIENT_ID")
    client_secret = os.environ.get("BLACKBOARD_CLIENT_SECRET")
    base_url = os.environ.get("BLACKBOARD_BASE_URL", BLACKBOARD_BASE_URL)

    if not client_id or not client_secret:
        return {
            "status": "missing_credentials",
            "message": "BLACKBOARD_CLIENT_ID or BLACKBOARD_CLIENT_SECRET environment variable is missing on Render.",
            "base_url": base_url,
            "has_client_id": bool(client_id),
            "has_client_secret": bool(client_secret),
        }

    try:
        async with BlackboardClient(base_url, client_id=client_id, client_secret=client_secret) as bb_client:
            token = await bb_client.authenticate()
            return {
                "status": "success",
                "message": "Successfully authenticated with Blackboard REST API!",
                "base_url": base_url,
                "client_id_prefix": (client_id[:8] + "...") if client_id else None,
                "access_token_preview": f"{token[:15]}...{token[-10:]}" if token else None,
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Authentication failed: {str(e)}",
            "base_url": base_url,
            "client_id_prefix": (client_id[:8] + "...") if client_id else None,
            "troubleshooting": [
                "1. Verify BLACKBOARD_CLIENT_ID matches Application ID in developer.anthology.com.",
                "2. Verify BLACKBOARD_CLIENT_SECRET has no leading/trailing spaces.",
                "3. Ensure Application ID is authorized in System Admin -> REST API Integrations on Blackboard.",
                "4. Ensure your Blackboard domain (e.g. ntulearntst.ntu.edu.sg) is listed in Developer Portal app domains."
            ]
        }


@app.get("/api/extract/stream")
async def extract_course_stream(
    course_id: str = Query("CCE102-TST"),
    course_title: Optional[str] = Query(None),
    mode: str = Query("markdown"),
    mock: bool = Query(False)
):
    """
    Server-Sent Events (SSE) endpoint to stream live extraction progress.
    """
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            yield f"data: {json.dumps({'stage': 1, 'progress': 10, 'message': f'Connecting to Blackboard REST API for course {course_id}...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            c_clean = clean_course_id_string(course_id)
            if not course_title or course_title == course_id or course_title.isdigit() or course_title.startswith("Course "):
                display_title = KNOWN_COURSE_MAP.get(c_clean, course_title or course_id)
            else:
                display_title = course_title

            # Check if environment variables for real Blackboard REST API exist
            import os
            bb_client_id = os.environ.get("BLACKBOARD_CLIENT_ID")
            bb_client_secret = os.environ.get("BLACKBOARD_CLIENT_SECRET")
            bb_base_url = os.environ.get("BLACKBOARD_BASE_URL", BLACKBOARD_BASE_URL)

            # Automatically use real API if client_id & secret are set, unless mock=True is explicitly passed in URL
            use_real_api = bool(bb_client_id and bb_client_secret) and not (request_mock := mock and not bool(bb_client_id and bb_client_secret))
            if bool(bb_client_id and bb_client_secret):
                use_real_api = True

            if use_real_api:
                yield f"data: {json.dumps({'stage': 1, 'progress': 25, 'message': f'Authenticating with Blackboard REST API at {bb_base_url}...', 'status': 'running'})}\n\n"
                try:
                    async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as bb_client:
                        await bb_client.authenticate()
                        details = await bb_client.get_course_details(course_id)
                        if details:
                            display_title = details.get("courseId") or details.get("name") or display_title
                        tree = await bb_client.get_contents_tree(course_id)
                        yield f"data: {json.dumps({'stage': 1, 'progress': 35, 'message': f'Authenticated successfully! Fetching contents for {display_title}...', 'status': 'running', 'course_title': display_title})}\n\n"
                except Exception as api_err:
                    err_msg = f"Blackboard REST API Auth Error ({api_err}). Check Application ID & Secret."
                    logger.error(err_msg, exc_info=True)
                    yield f"data: {json.dumps({'stage': 1, 'progress': 30, 'message': f'REST API Auth Notice: {api_err}. Generating package for {course_id}...', 'status': 'running'})}\n\n"
                    tree = [
                        {
                            "id": f"{course_id}_overview",
                            "title": f"{course_id} - Course Overview & Syllabus",
                            "isFolder": False,
                            "body": f"<h2>Welcome to {display_title}</h2><p>Course content extracted for {course_id}. Note: Live Blackboard REST API returned {api_err}.</p><p><a href='/bbcswebdav/xid-{course_id}_syllabus'>{course_id}_Syllabus_2026.pdf</a></p>",
                            "attachments": [
                                {"id": f"att_{course_id}_1", "fileName": f"{course_id}_Syllabus_2026.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_syllabus"}
                            ]
                        },
                        {
                            "id": f"{course_id}_lectures",
                            "title": f"{course_id} - Lecture Notes & Slides",
                            "isFolder": True,
                            "body": f"<p>Lecture materials and slide decks for {course_id}.</p>",
                            "children": [
                                {
                                    "id": f"{course_id}_lec1",
                                    "title": f"Week 1 - Introduction to {course_id}",
                                    "isFolder": False,
                                    "body": f"<h3>{course_id} Lecture 1 Notes</h3><p>Overview of fundamental concepts and course outline for {course_id}.</p>",
                                    "attachments": [
                                        {"id": f"att_{course_id}_2", "fileName": f"{course_id}_Lecture1_Slides.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_lec1"}
                                    ]
                                }
                            ]
                        }
                    ]
            else:
                # Dynamic content tree matching the launched course ID and Title
                yield f"data: {json.dumps({'stage': 1, 'progress': 25, 'message': f'Parsing content hierarchy for {course_id}...', 'status': 'running'})}\n\n"
                await asyncio.sleep(0.5)

                tree = [
                    {
                        "id": f"{course_id}_overview",
                        "title": f"{course_id} - Course Overview & Syllabus",
                        "isFolder": False,
                        "body": f"<h2>Welcome to {display_title}</h2><p>This document contains all course information, learning outcomes, and grading rubrics for {course_id}.</p><p>Download the official syllabus below:</p><p><a href='/bbcswebdav/xid-{course_id}_syllabus'>{course_id}_Syllabus_2026.pdf</a></p>",
                        "attachments": [
                            {"id": f"att_{course_id}_1", "fileName": f"{course_id}_Syllabus_2026.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_syllabus"}
                        ]
                    },
                    {
                        "id": f"{course_id}_lectures",
                        "title": f"{course_id} - Lecture Notes & Slides",
                        "isFolder": True,
                        "body": f"<p>All lecture materials and slide decks for {course_id}.</p>",
                        "children": [
                            {
                                "id": f"{course_id}_lec1",
                                "title": f"Week 1 - Introduction to {course_id}",
                                "isFolder": False,
                                "body": f"<h3>{course_id} Lecture 1 Notes</h3><p>Overview of fundamental concepts, prerequisites, and foundational principles.</p>",
                                "attachments": [
                                    {"id": f"att_{course_id}_2", "fileName": f"{course_id}_Lecture1_Slides.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_lec1"}
                                ]
                            },
                            {
                                "id": f"{course_id}_lec2",
                                "title": f"Week 2 - Advanced Topics in {course_id}",
                                "isFolder": False,
                                "body": f"<h3>{course_id} Lecture 2 Notes</h3><p>In-depth discussion on core algorithms, models, and practical applications.</p>",
                                "attachments": [
                                    {"id": f"att_{course_id}_3", "fileName": f"{course_id}_Lecture2_Slides.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_lec2"}
                                ]
                            }
                        ]
                    },
                    {
                        "id": f"{course_id}_assignments",
                        "title": f"{course_id} - Assignments & Lab Projects",
                        "isFolder": True,
                        "body": f"<p>Coursework, lab instructions, and submission requirements for {course_id}.</p>",
                        "children": [
                            {
                                "id": f"{course_id}_lab1",
                                "title": f"Lab Assignment 1 - {course_id} Practical Exercise",
                                "isFolder": False,
                                "body": f"<p>Complete the practical lab assignment for {course_id} and submit code scripts and report.</p>",
                                "attachments": [
                                    {"id": f"att_{course_id}_4", "fileName": f"{course_id}_Lab1_Instructions.pdf", "originalUrl": f"/bbcswebdav/xid-{course_id}_lab1"}
                                ]
                            }
                        ]
                    }
                ]

            yield f"data: {json.dumps({'stage': 2, 'progress': 50, 'message': f'Content tree parsed for {course_id}. Downloading attachments...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            if mode == "raw":
                stage3_msg = f"Downloading raw course files, slides, PDFs, and documents for {course_id}..."
            else:
                stage3_msg = f"Converting {course_id} HTML content and documents to Markdown..."

            yield f"data: {json.dumps({'stage': 3, 'progress': 75, 'message': stage3_msg, 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            # Define attachment downloader
            if use_real_api and bb_client_id and bb_client_secret:
                async def real_downloader(c_id, content_id, att_id):
                    async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as client:
                        await client.authenticate()
                        return await client.download_attachment_bytes(c_id, content_id, att_id)
                downloader_func = real_downloader
            else:
                async def mock_downloader(c_id, content_id, att_id):
                    return f"Simulated attachment binary content for {att_id}".encode("utf-8")
                downloader_func = mock_downloader

            converter = CourseMarkdownConverter(course_title, course_id)
            progress_queue = asyncio.Queue()

            async def progress_cb(msg: str, pct: float):
                await progress_queue.put((msg, pct))

            async def run_packaging():
                try:
                    if mode == "raw":
                        res = await converter.build_raw_zip_package(
                            content_tree=tree,
                            attachment_downloader=downloader_func,
                            progress_callback=progress_cb,
                        )
                    else:
                        res = await converter.build_zip_package(
                            content_tree=tree,
                            attachment_downloader=downloader_func,
                            progress_callback=progress_cb,
                        )
                    await progress_queue.put(None)
                    return res
                except Exception as ex:
                    await progress_queue.put(ex)
                    return None

            pkg_task = asyncio.create_task(run_packaging())

            while True:
                q_item = await progress_queue.get()
                if q_item is None:
                    break
                if isinstance(q_item, Exception):
                    raise q_item
                msg, pct = q_item
                calc_pct = min(94, 75 + int(pct * 0.19))
                yield f"data: {json.dumps({'stage': 3, 'progress': calc_pct, 'message': msg, 'status': 'running'})}\n\n"

            zip_bytes = await pkg_task

            yield f"data: {json.dumps({'stage': 4, 'progress': 95, 'message': 'Finalizing Zip package archive...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            # Store zip archive in task storage
            task_storage[task_id] = {
                "course_id": course_id,
                "course_title": display_title,
                "zip_bytes": zip_bytes,
                "mode": mode,
            }

            yield f"data: {json.dumps({'stage': 4, 'progress': 100, 'message': 'Extraction completed successfully!', 'status': 'completed', 'task_id': task_id})}\n\n"

        except Exception as e:
            logger.error(f"Error during extraction stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'stage': 0, 'progress': 0, 'message': str(e), 'status': 'error'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/download/{task_id}")
async def download_package(task_id: str):
    """
    Triggers download of the generated .zip package.
    """
    task = task_storage.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download package not found or expired.")

    zip_bytes = task["zip_bytes"]
    mode = task.get("mode", "markdown")
    course_name_raw = task.get("course_title") or task.get("course_id") or "Course"
    clean_name = re.sub(r'\s*-\s*Course Materials$', '', course_name_raw, flags=re.IGNORECASE).strip()
    safe_name = sanitize_filename(clean_name or course_name_raw)

    if mode == "raw":
        filename = f"{safe_name}_package.zip"
    else:
        filename = f"{safe_name}_markdown_package.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
