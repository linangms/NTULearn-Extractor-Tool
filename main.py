import asyncio
import json
import os
import re
import secrets
import time
import urllib.parse
import uuid
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
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
    error_ref = str(uuid.uuid4())[:8]
    logger.error(f"Unhandled exception [ref={error_ref}] on {request.method} {request.url}: {exc}", exc_info=True)
    # Never echo raw exception text to the client - it can reveal internal
    # paths, hostnames, or other implementation details. Log the full error
    # server-side under error_ref and show the user only that reference, so
    # a report ("issue ref abc12345") can be correlated with the logs.
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
                    The tool encountered an issue while processing this request.<br/>
                    If this persists, please report it with this reference:<br/>
                    <code class="text-xs bg-slate-100 p-2 rounded text-amber-800 font-mono block mt-2 text-left overflow-x-auto">{error_ref}</code>
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
BLACKBOARD_BASE_URL = os.environ.get("BLACKBOARD_BASE_URL", "https://ntulearntst.ntu.edu.sg")


# --- LTI 1.3 launch verification --------------------------------------------
# Blackboard Learn's LTI 1.3 platform endpoints are shared across all SaaS
# tenants - the issuer and OIDC/JWKS URLs below are Blackboard-wide constants,
# not specific to any one Blackboard instance. Only the client_id (your
# registered Application ID) and, optionally, deployment_id(s) are
# institution-specific.
LTI_ISSUER = "https://blackboard.com"
LTI_OIDC_AUTH_URL = "https://developer.blackboard.com/api/v1/gateway/oidcauth"
LTI_CLIENT_ID = os.environ.get("LTI_CLIENT_ID") or os.environ.get("BLACKBOARD_CLIENT_ID")
LTI_JWKS_URL = os.environ.get("LTI_JWKS_URL") or (
    f"https://developer.blackboard.com/api/v1/management/applications/{LTI_CLIENT_ID}/jwks.json"
    if LTI_CLIENT_ID else None
)
# Optional allow-list (comma-separated). If unset, deployment_id is logged but not enforced.
LTI_DEPLOYMENT_IDS = [d.strip() for d in os.environ.get("LTI_DEPLOYMENT_IDS", "").split(",") if d.strip()]

_LTI_STATE_TTL_SECONDS = 600
_lti_states: Dict[str, Dict[str, Any]] = {}
_lti_jwks_client = None


def _cleanup_lti_states() -> None:
    now = time.time()
    for state, entry in list(_lti_states.items()):
        if now - entry["created_at"] > _LTI_STATE_TTL_SECONDS:
            _lti_states.pop(state, None)


def _get_lti_jwks_client():
    """Lazily creates (and caches) the PyJWT client for Blackboard's platform JWKS."""
    global _lti_jwks_client
    if _lti_jwks_client is None and LTI_JWKS_URL:
        import jwt
        _lti_jwks_client = jwt.PyJWKClient(LTI_JWKS_URL)
    return _lti_jwks_client


def _extract_course_info_from_claims(claims: Dict[str, Any]) -> Dict[str, str]:
    """Derives course_id/course_name/user_role strictly from verified LTI claims."""
    context_claim = claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {}) or {}
    custom_claim = claims.get("https://purl.imsglobal.org/spec/lti/claim/custom", {}) or {}
    lis_claim = claims.get("https://purl.imsglobal.org/spec/lti/claim/lis", {}) or {}
    roles_claim = claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", []) or []

    course_id = (
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
    course_name = (
        context_claim.get("title")
        or context_claim.get("label")
        or custom_claim.get("course_name")
        or custom_claim.get("course_code")
        or custom_claim.get("course_title")
        or custom_claim.get("context_title")
    )
    user_role = "Instructor" if any(
        "Instructor" in r or "Administrator" in r or "ContentDeveloper" in r for r in roles_claim
    ) else "Student"

    course_id = clean_course_id_string(str(course_id)) if course_id else ""
    if not course_name:
        course_name = course_id or "Course Materials Extractor"

    return {"course_id": course_id, "course_name": str(course_name), "user_role": user_role}


def _verify_lti_launch(id_token: Optional[str], state: Optional[str]) -> Dict[str, Any]:
    """
    Verifies a Blackboard LTI 1.3 id_token: signature (via Blackboard's platform
    JWKS), issuer, audience, expiry, and nonce (tied to the state issued during
    /lti/login). Raises HTTPException on any failure - callers must never trust
    an unverified token's claims.
    """
    import jwt

    if not id_token:
        raise HTTPException(status_code=403, detail="Missing id_token - launch must go through /lti/login")
    if not state or state not in _lti_states:
        raise HTTPException(status_code=403, detail="Invalid or expired LTI login state - launch must start from /lti/login")

    state_entry = _lti_states.pop(state)
    if time.time() - state_entry["created_at"] > _LTI_STATE_TTL_SECONDS:
        raise HTTPException(status_code=403, detail="LTI login state expired")

    jwks_client = _get_lti_jwks_client()
    if not jwks_client:
        raise HTTPException(status_code=500, detail="LTI verification not configured - set LTI_CLIENT_ID (or BLACKBOARD_CLIENT_ID)")

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=LTI_CLIENT_ID,
            issuer=LTI_ISSUER,
            options={"require": ["exp", "iat"]},
        )
    except Exception as e:
        # Peek at the token's actual (unverified) claims purely to log what
        # Blackboard really sent, so a mismatch (aud, iss) is diagnosable from
        # server logs without needing to dig through Blackboard's admin UI.
        try:
            unverified = jwt.decode(id_token, options={"verify_signature": False})
            logger.warning(
                f"LTI id_token verification failed: {e} | "
                f"token aud={unverified.get('aud')!r} iss={unverified.get('iss')!r} "
                f"vs configured LTI_CLIENT_ID={LTI_CLIENT_ID!r} LTI_ISSUER={LTI_ISSUER!r}"
            )
        except Exception:
            logger.warning(f"LTI id_token verification failed: {e} (could not decode token for diagnostics)")
        raise HTTPException(status_code=403, detail=f"LTI id_token verification failed: {e}")

    if claims.get("nonce") != state_entry["nonce"]:
        raise HTTPException(status_code=403, detail="LTI id_token nonce mismatch")

    message_type = claims.get("https://purl.imsglobal.org/spec/lti/claim/message_type")
    if message_type not in ("LtiResourceLinkRequest", "LtiDeepLinkingRequest"):
        raise HTTPException(status_code=403, detail=f"Unexpected LTI message_type: {message_type}")

    deployment_id = claims.get("https://purl.imsglobal.org/spec/lti/claim/deployment_id")
    if LTI_DEPLOYMENT_IDS:
        if deployment_id not in LTI_DEPLOYMENT_IDS:
            raise HTTPException(status_code=403, detail=f"Unrecognized LTI deployment_id: {deployment_id}")
    else:
        logger.warning(f"LTI_DEPLOYMENT_IDS not configured - accepting deployment_id={deployment_id} without an allow-list check")

    return claims


_tool_rsa_private_key = None
_tool_jwk_cache: Optional[Dict[str, Any]] = None


def _get_tool_jwk() -> Optional[Dict[str, Any]]:
    """
    Generates (once, in memory) an RSA keypair for this tool and returns its
    public key as a JWK dict, served at /lti/jwks. Not persisted across
    restarts - fine since nothing here currently signs outgoing requests with
    it (no AGS/NRPS/Deep Linking calls); regenerating on restart is invisible
    to Blackboard, which only ever verifies our launch-independent JWKS if it
    decides to call back into a signed service, which this app doesn't use yet.
    """
    global _tool_rsa_private_key, _tool_jwk_cache
    if _tool_jwk_cache is not None:
        return _tool_jwk_cache
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        import base64

        if _tool_rsa_private_key is None:
            _tool_rsa_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        public_numbers = _tool_rsa_private_key.public_key().public_numbers()

        def _b64url_uint(value: int) -> str:
            byte_length = (value.bit_length() + 7) // 8 or 1
            return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")

        _tool_jwk_cache = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "ntulearn-extractor-1",
            "n": _b64url_uint(public_numbers.n),
            "e": _b64url_uint(public_numbers.e),
        }
    except Exception as e:
        logger.warning(f"Could not generate tool JWKS keypair: {e}")
        _tool_jwk_cache = None
    return _tool_jwk_cache


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


async def extract_lti_context(request: Request, require_verified: bool = False) -> Dict[str, str]:
    """
    Extracts LTI claims, course ID, course name, and user role from request payload and referer headers.

    require_verified=True (used by the actual /lti/launch handshake) requires a
    valid, signature-verified id_token tied to a state issued by /lti/login, and
    derives course_id/course_name/user_role strictly from those verified claims -
    no fallback to unauthenticated URL/referer heuristics. require_verified=False
    (used for cosmetic page rendering, e.g. the dashboard on a plain reload) keeps
    the lenient best-effort heuristics below, since it is not a security boundary.
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

    id_token = params.get("id_token") or form_data.get("id_token")

    if require_verified:
        state = params.get("state") or form_data.get("state")
        claims = _verify_lti_launch(id_token, state)
        deployment_id = claims.get("https://purl.imsglobal.org/spec/lti/claim/deployment_id")
        logger.info(f"Verified LTI launch: deployment_id={deployment_id}")
        return _extract_course_info_from_claims(claims)

    # Fallback regex extraction from raw unquoted URL string (handles course_id%253D_626_1 or course_id=_626_1)
    m_course = (
        re.search(r'course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'custom_course_id[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
        or re.search(r'course[=:=%253D%3D]+([^&?\s]+)', unquoted_url, re.IGNORECASE)
    )
    if m_course and "course_id" not in params:
        params["course_id"] = m_course.group(1)

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
        import os
        bb_client_id = os.environ.get("BLACKBOARD_CLIENT_ID")
        bb_client_secret = os.environ.get("BLACKBOARD_CLIENT_SECRET")
        bb_base_url = os.environ.get("BLACKBOARD_BASE_URL", BLACKBOARD_BASE_URL)
        try:
            async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as bb_client:
                if bb_client_id and bb_client_secret:
                    await bb_client.authenticate()
                details = await bb_client.get_course_details(course_id)
                resolved_name = details.get("courseId") or details.get("name")
                if resolved_name and not resolved_name.startswith("Course ") and resolved_name != course_id:
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


@app.get("/api/course_details")
async def api_course_details(course_id: str = Query(...)):
    clean_id = clean_course_id_string(course_id)
    import os
    bb_client_id = os.environ.get("BLACKBOARD_CLIENT_ID")
    bb_client_secret = os.environ.get("BLACKBOARD_CLIENT_SECRET")
    bb_base_url = os.environ.get("BLACKBOARD_BASE_URL", BLACKBOARD_BASE_URL)
    
    course_code = clean_id
    course_name = f"Course {clean_id}"
    
    try:
        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as bb_client:
            if bb_client_id and bb_client_secret:
                await bb_client.authenticate()
            details = await bb_client.get_course_details(clean_id)
            if details:
                course_code = details.get("courseId") or details.get("name") or clean_id
                course_name = details.get("name") or course_code
    except Exception as e:
        logger.warning(f"Error in api_course_details for {course_id}: {e}")

    return {
        "course_id": clean_id,
        "course_code": course_code,
        "course_name": course_name,
        "display_title": f"{course_code} Course Materials" if course_code and not course_code.endswith("Course Materials") else course_code
    }


@app.api_route("/lti/login", methods=["GET", "POST"])
async def lti_login(request: Request):
    """
    LTI 1.3 OIDC third-party initiated login. Redirects the browser to
    Blackboard's OIDC authorization endpoint per the LTI 1.3 / OIDC Core spec,
    with a freshly generated state+nonce that /lti/launch will require and
    verify - this is what makes the subsequent id_token verification meaningful,
    instead of the tool simply re-posting straight to itself.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: str(v) for k, v in form.items()})
        except Exception as e:
            logger.warning(f"Error parsing login form data: {e}")

    iss = params.get("iss")
    login_hint = params.get("login_hint")
    target_link_uri = params.get("target_link_uri") or str(request.url_for("lti_launch"))
    if request.headers.get("x-forwarded-proto") == "https":
        target_link_uri = target_link_uri.replace("http://", "https://", 1)
    client_id = params.get("client_id") or LTI_CLIENT_ID
    lti_message_hint = params.get("lti_message_hint")

    if iss != LTI_ISSUER:
        logger.warning(f"LTI login rejected: unexpected issuer '{iss}'")
        raise HTTPException(status_code=400, detail="Unrecognized LTI platform issuer")
    if not login_hint:
        raise HTTPException(status_code=400, detail="Missing login_hint")

    _cleanup_lti_states()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    _lti_states[state] = {"nonce": nonce, "target_link_uri": target_link_uri, "created_at": time.time()}

    auth_params = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": client_id,
        "redirect_uri": target_link_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if lti_message_hint:
        auth_params["lti_message_hint"] = lti_message_hint

    logger.info(f"LTI OIDC login: redirecting to platform auth endpoint for client_id={client_id}")
    redirect_url = f"{LTI_OIDC_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    return RedirectResponse(redirect_url, status_code=302)


@app.api_route("/lti/launch", methods=["GET", "POST"])
async def lti_launch(request: Request):
    """
    LTI 1.3 Launch handler endpoint. Verifies the platform-signed id_token
    (signature, issuer, audience, nonce, deployment_id) before trusting any of
    its claims - see extract_lti_context(require_verified=True).
    """
    session_id = str(uuid.uuid4())
    context = await extract_lti_context(request, require_verified=True)

    course_id = context["course_id"]
    course_name = context["course_name"]
    user_role = context["user_role"]

    sessions[session_id] = {
        "course_id": course_id,
        "course_name": course_name,
        "user_role": user_role,
        "created_at": time.time(),
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
    LTI 1.3 JWKS public keys endpoint. Publishes this tool's own public key -
    not used to verify incoming Blackboard launches (those are verified against
    Blackboard's own platform JWKS), but required by the LTI 1.3 spec for any
    future signed service calls (e.g. Deep Linking, AGS) the tool might make.
    """
    jwk_client = _get_tool_jwk()
    return {"keys": [jwk_client]} if jwk_client else {"keys": []}


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
    session_id: str = Query(...),
    mode: str = Query("markdown"),
    mock: bool = Query(False),
):
    """
    Server-Sent Events (SSE) endpoint to stream live extraction progress.

    Requires a session_id created by a verified LTI launch (/lti/launch).
    course_id is resolved from that server-side session, never accepted
    directly from the client - otherwise anyone with this URL could extract
    any course's content just by supplying its course_id, bypassing Blackboard
    and LTI entirely (this app authenticates to Blackboard's REST API with its
    own service credentials, not the visiting user's own permissions).
    """
    session_data = sessions.get(session_id)
    if not session_data or not session_data.get("course_id"):
        raise HTTPException(status_code=403, detail="Invalid or expired session - please relaunch the tool from Blackboard")

    course_id = session_data["course_id"]
    course_title = session_data.get("course_name")
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            yield f"data: {json.dumps({'stage': 1, 'progress': 10, 'message': f'Connecting to Blackboard REST API for course {course_id}...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            if not course_title or course_title == course_id or course_title.isdigit() or course_title.startswith("Course "):
                display_title = course_title or course_id
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
                        # Just the shallow topic list here (cheap) - each topic's
                        # full subtree (all its body HTML, attachments, etc.) is
                        # fetched one at a time inside topics_source() below, as
                        # it's processed, instead of holding every topic for the
                        # whole course in memory for the whole extraction.
                        top_items = await bb_client.get_top_level_items(course_id)
                    total_topics = len(top_items)

                    async def topics_source():
                        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as topic_client:
                            await topic_client.authenticate()
                            for item in top_items:
                                yield await topic_client.build_content_node(course_id, item)

                    yield f"data: {json.dumps({'stage': 1, 'progress': 35, 'message': f'Authenticated successfully! Fetching contents for {display_title}...', 'status': 'running', 'course_title': display_title})}\n\n"
                except Exception as api_err:
                    # Logged with full detail server-side only - never echoed to the
                    # client or, worse, baked into the mock package the user downloads,
                    # since it can reveal internal hostnames/credentials/config details.
                    logger.error(f"Blackboard REST API Auth Error [task_id={task_id}]: {api_err}", exc_info=True)
                    yield f"data: {json.dumps({'stage': 1, 'progress': 30, 'message': f'Could not authenticate with the live Blackboard REST API (ref: {task_id[:8]}). Generating a placeholder package for {course_id}...', 'status': 'running'})}\n\n"
                    tree = [
                        {
                            "id": f"{course_id}_overview",
                            "title": f"{course_id} - Course Overview & Syllabus",
                            "isFolder": False,
                            "body": f"<h2>Welcome to {display_title}</h2><p>Course content extracted for {course_id}. Note: the live Blackboard REST API could not be reached (ref: {task_id[:8]}).</p><p><a href='/bbcswebdav/xid-{course_id}_syllabus'>{course_id}_Syllabus_2026.pdf</a></p>",
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
                    total_topics = len(tree)

                    async def topics_source():
                        for node in tree:
                            yield node
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
                total_topics = len(tree)

                async def topics_source():
                    for node in tree:
                        yield node

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
                # Kaltura video/caption resolution tries many candidate URLs
                # (multiple partner ids x multiple hosts), each with its own
                # retries - a single unreachable candidate can otherwise stall
                # for minutes with zero progress, hanging the whole extraction
                # and eventually dropping the SSE connection client-side. A hard
                # per-file timeout guarantees a slow/broken file is skipped
                # instead of blocking every other file behind it.
                ATTACHMENT_FETCH_TIMEOUT_SECONDS = 60

                async def _with_timeout(coro, label: str):
                    try:
                        return await asyncio.wait_for(coro, timeout=ATTACHMENT_FETCH_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        logger.warning(f"Timed out after {ATTACHMENT_FETCH_TIMEOUT_SECONDS}s fetching {label} - skipping it")
                        return None

                async def real_downloader(c_id, content_id, att_id):
                    async def _do():
                        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as client:
                            await client.authenticate()
                            return await client.download_attachment_bytes(c_id, content_id, att_id)
                    return await _with_timeout(_do(), f"attachment {att_id}")
                downloader_func = real_downloader

                async def real_caption_downloader(c_id, content_id, att_id):
                    async def _do():
                        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as client:
                            await client.authenticate()
                            return await client.download_kaltura_caption_bytes(att_id)
                    return await _with_timeout(_do(), f"captions for {att_id}")
                caption_downloader_func = real_caption_downloader

                async def real_embed_url_resolver(text):
                    async def _do():
                        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as client:
                            await client.authenticate()
                            return await client.resolve_kaltura_embed_url(text)
                    return await _with_timeout(_do(), "embed URL resolution")
                embed_url_resolver_func = real_embed_url_resolver

                async def real_video_downloader(text):
                    async def _do():
                        async with BlackboardClient(bb_base_url, client_id=bb_client_id, client_secret=bb_client_secret) as client:
                            await client.authenticate()
                            return await client.download_kaltura_video_bytes(text)
                    return await _with_timeout(_do(), "Kaltura video")
                video_downloader_func = real_video_downloader
            else:
                async def mock_downloader(c_id, content_id, att_id):
                    return f"Simulated attachment binary content for {att_id}".encode("utf-8")
                downloader_func = mock_downloader

                async def mock_caption_downloader(c_id, content_id, att_id):
                    return None
                caption_downloader_func = mock_caption_downloader

                async def mock_embed_url_resolver(text):
                    return None
                embed_url_resolver_func = mock_embed_url_resolver

                async def mock_video_downloader(text):
                    return None
                video_downloader_func = mock_video_downloader

            converter = CourseMarkdownConverter(course_title, course_id, base_url=bb_base_url)
            progress_queue = asyncio.Queue()

            async def progress_cb(msg: str, pct: float):
                await progress_queue.put((msg, pct))

            async def run_packaging():
                try:
                    if mode == "raw":
                        res = await converter.build_raw_zip_package_streaming(
                            topics=topics_source(),
                            total_topics=total_topics,
                            attachment_downloader=downloader_func,
                            caption_downloader=caption_downloader_func,
                            embed_url_resolver=embed_url_resolver_func,
                            video_downloader=video_downloader_func,
                            progress_callback=progress_cb,
                        )
                    else:
                        res = await converter.build_zip_package_streaming(
                            topics=topics_source(),
                            total_topics=total_topics,
                            attachment_downloader=downloader_func,
                            caption_downloader=caption_downloader_func,
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

            zip_path = await pkg_task

            yield f"data: {json.dumps({'stage': 4, 'progress': 95, 'message': 'Finalizing Zip package archive...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            # Store the on-disk zip path in task storage (not the bytes - a large
            # course's archive is written straight to a temp file so it's never
            # fully buffered in RAM, which is what was crashing the process on
            # memory-constrained hosting for big courses).
            task_storage[task_id] = {
                "course_id": course_id,
                "course_title": display_title,
                "zip_path": zip_path,
                "mode": mode,
            }

            yield f"data: {json.dumps({'stage': 4, 'progress': 100, 'message': 'Extraction completed successfully!', 'status': 'completed', 'task_id': task_id})}\n\n"

        except Exception as e:
            # Log the real error server-side under task_id, but never echo raw
            # exception text to the client - it can reveal internal details
            # (paths, hostnames, Blackboard API responses).
            logger.error(f"Error during extraction stream [task_id={task_id}]: {e}", exc_info=True)
            yield f"data: {json.dumps({'stage': 0, 'progress': 0, 'message': f'Extraction failed (ref: {task_id[:8]}). Please try again or contact support.', 'status': 'error'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/download/{task_id}")
async def download_package(task_id: str):
    """
    Triggers download of the generated .zip package.
    """
    task = task_storage.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download package not found or expired.")

    zip_path = task["zip_path"]
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Download package not found or expired.")

    mode = task.get("mode", "markdown")
    course_name_raw = task.get("course_title") or task.get("course_id") or "Course"
    clean_name = re.sub(r'\s*-\s*Course Materials$', '', course_name_raw, flags=re.IGNORECASE).strip()
    safe_name = sanitize_filename(clean_name or course_name_raw)

    if mode == "raw":
        filename = f"{safe_name}_package.zip"
    else:
        filename = f"{safe_name}_markdown_package.zip"

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
    )
