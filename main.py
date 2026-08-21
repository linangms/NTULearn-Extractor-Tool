import asyncio
import json
import uuid
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from blackboard_client import BlackboardClient
from converter import CourseMarkdownConverter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ntulearn_extractor")

app = FastAPI(
    title="NTULearn Extractor Tool",
    description="API-driven LTI 1.3 Web Application to extract Blackboard course contents to Markdown",
    version="1.0.0",
)

templates = Jinja2Templates(directory="templates")

# In-memory session and zip archive storage
sessions: Dict[str, Dict[str, Any]] = {}
task_storage: Dict[str, Dict[str, Any]] = {}

# Default Blackboard REST API configuration (can be overriden by env vars)
BLACKBOARD_BASE_URL = "https://ntulearn.ntu.edu.sg"


@app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
async def dashboard(request: Request, session_id: Optional[str] = Query(None)):
    """
    Renders the main dashboard UI.
    """
    session_data = sessions.get(session_id, {}) if session_id else {}
    
    course_name = session_data.get("course_name", "CZ4042 - Neural Networks & Deep Learning")
    course_id = session_data.get("course_id", "NTU_CZ4042_2026_S1")
    user_role = session_data.get("user_role", "Instructor")

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
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form_data = await request.form()
            params.update(dict(form_data))
        except Exception as e:
            logger.warning(f"Error parsing login form data: {e}")

    iss = params.get("iss")
    target_link_uri = params.get("target_link_uri") or str(request.url_for("lti_launch"))
    client_id = params.get("client_id")
    login_hint = params.get("login_hint")
    lti_message_hint = params.get("lti_message_hint")

    logger.info(f"LTI Login initiated from iss={iss}, target={target_link_uri}, client_id={client_id}")

    # Build auth redirect if iss is provided, otherwise direct to target launch
    if iss and login_hint:
        # Construct Blackboard OIDC authorization redirect URL
        auth_url = f"{iss.rstrip('/')}/learn/api/public/v1/oauth2/authorizationcode"
        # Standard fallback to direct launch
        response_html = f"""
        <html>
        <body>
            <p>Launching LTI Tool...</p>
            <form id="lti_launch_form" action="{target_link_uri}" method="POST">
                <input type="hidden" name="iss" value="{iss or ''}" />
                <input type="hidden" name="client_id" value="{client_id or ''}" />
            </form>
            <script>document.getElementById('lti_launch_form').submit();</script>
        </body>
        </html>
        """
        return HTMLResponse(content=response_html)

    return HTMLResponse(content=f"<html><body><p>Redirecting...</p><script>window.location.href='{target_link_uri}';</script></body></html>")


@app.api_route("/lti/launch", methods=["GET", "POST"])
async def lti_launch(
    request: Request,
    id_token: Optional[str] = Form(None),
    state: Optional[str] = Form(None)
):
    """
    LTI 1.3 Launch handler endpoint.
    Extracts course details and user roles from LTI payload.
    """
    session_id = str(uuid.uuid4())
    
    # Try parsing form data directly if Form parameter was not populated
    if not id_token:
        try:
            form_data = await request.form()
            id_token = form_data.get("id_token")
        except Exception as e:
            logger.debug(f"Could not read request form data: {e}")

    # Fallback to query parameters
    if not id_token:
        id_token = request.query_params.get("id_token")

    course_id = "NTU_CZ4042_2026_S1"
    course_name = "CZ4042 - Neural Networks & Deep Learning"
    user_role = "Instructor"

    if id_token:
        try:
            import jwt
            decoded = jwt.decode(id_token, options={"verify_signature": False})
            logger.info(f"Decoded LTI ID Token claims: {decoded}")

            context_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/context", {})
            custom_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/custom", {})
            roles_claim = decoded.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])

            # Extract course ID
            if context_claim.get("id"):
                course_id = context_claim["id"]
            elif context_claim.get("label"):
                course_id = context_claim["label"]
            elif custom_claim.get("course_id"):
                course_id = custom_claim["course_id"]

            # Extract course Name / Title
            if context_claim.get("title"):
                course_name = context_claim["title"]
            elif context_claim.get("label"):
                course_name = context_claim["label"]
            elif custom_claim.get("course_name"):
                course_name = custom_claim["course_name"]

            # Extract User Role
            if roles_claim:
                if any("Instructor" in r or "Administrator" in r or "ContentDeveloper" in r for r in roles_claim):
                    user_role = "Instructor"
                else:
                    user_role = "Student"

        except Exception as e:
            logger.warning(f"Could not parse LTI ID Token claims: {e}", exc_info=True)
    else:
        logger.warning("No id_token found in LTI launch request.")

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


@app.get("/api/extract/stream")
async def extract_course_stream(
    course_id: str = Query("NTU_CZ4042_2026_S1"),
    mock: bool = Query(True)
):
    """
    Server-Sent Events (SSE) endpoint to stream live extraction progress.
    """
    task_id = str(uuid.uuid4())

    async def event_generator():
        try:
            yield f"data: {json.dumps({'stage': 1, 'progress': 10, 'message': 'Connecting to Blackboard REST API...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            course_title = "CZ4042 - Neural Networks & Deep Learning"

            if mock or course_id.startswith("NTU_") or course_id == "mock":
                # Simulated REST response content hierarchy
                yield f"data: {json.dumps({'stage': 1, 'progress': 25, 'message': 'Fetching top-level course contents...', 'status': 'running'})}\n\n"
                await asyncio.sleep(0.5)

                tree = [
                    {
                        "id": "item_1",
                        "title": "Course Overview & Syllabus",
                        "isFolder": False,
                        "body": "<h2>Welcome to CZ4042</h2><p>This course covers deep learning architectures, CNNs, RNNs, and Transformers.</p><p>Download the syllabus below:</p><p><a href='/bbcswebdav/xid-101_1'>Syllabus_2026.pdf</a></p>",
                        "attachments": [
                            {"id": "att_1", "fileName": "Syllabus_2026.pdf", "originalUrl": "/bbcswebdav/xid-101_1"}
                        ]
                    },
                    {
                        "id": "item_2",
                        "title": "Lectures & Slides",
                        "isFolder": True,
                        "body": "<p>All lecture notes for Sem 1.</p>",
                        "children": [
                            {
                                "id": "item_2_1",
                                "title": "Week 1 - Introduction to Perceptrons",
                                "isFolder": False,
                                "body": "<h3>Lecture 1 Notes</h3><p>Introduction to linear classifiers and activation functions.</p>",
                                "attachments": [
                                    {"id": "att_2_1", "fileName": "Lecture1_Slides.pdf", "originalUrl": "/bbcswebdav/xid-102_1"}
                                ]
                            },
                            {
                                "id": "item_2_2",
                                "title": "Week 2 - Backpropagation & Gradient Descent",
                                "isFolder": False,
                                "body": "<h3>Lecture 2 Notes</h3><p>Derivation of backpropagation algorithm with chain rule examples.</p>",
                                "attachments": [
                                    {"id": "att_2_2", "fileName": "Lecture2_Slides.pdf", "originalUrl": "/bbcswebdav/xid-103_1"}
                                ]
                            }
                        ]
                    },
                    {
                        "id": "item_3",
                        "title": "Assignments & Lab Projects",
                        "isFolder": True,
                        "body": "<p>Lab assignments and submission guidelines.</p>",
                        "children": [
                            {
                                "id": "item_3_1",
                                "title": "Lab 1 - PyTorch Basics & Image Classification",
                                "isFolder": False,
                                "body": "<p>Implement a ResNet model using PyTorch on CIFAR-10.</p>",
                                "attachments": [
                                    {"id": "att_3_1", "fileName": "Lab1_Instructions.pdf", "originalUrl": "/bbcswebdav/xid-104_1"}
                                ]
                            }
                        ]
                    }
                ]
            else:
                # Real API call with BlackboardClient
                async with BlackboardClient(BLACKBOARD_BASE_URL) as bb_client:
                    yield f"data: {json.dumps({'stage': 1, 'progress': 25, 'message': f'Requesting content tree for course {course_id}...', 'status': 'running'})}\n\n"
                    tree = await bb_client.get_contents_tree(course_id)
                    details = await bb_client.get_course_details(course_id)
                    course_title = details.get("name", course_title)

            yield f"data: {json.dumps({'stage': 2, 'progress': 50, 'message': 'Content tree parsed. Downloading attachments...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            yield f"data: {json.dumps({'stage': 3, 'progress': 75, 'message': 'Converting HTML content to Markdown...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            # Simulated mock attachment downloader
            async def mock_downloader(c_id, content_id, att_id):
                return f"Simulated attachment binary content for {att_id}".encode("utf-8")

            async def progress_cb(msg, pct):
                yield_msg = {
                    'stage': 3,
                    'progress': 75 + (pct * 0.15),
                    'message': msg,
                    'status': 'running'
                }
                logger.info(f"Progress update: {msg}")

            converter = CourseMarkdownConverter(course_title, course_id)
            zip_bytes = await converter.build_zip_package(
                content_tree=tree,
                attachment_downloader=mock_downloader,
            )

            yield f"data: {json.dumps({'stage': 4, 'progress': 95, 'message': 'Finalizing Zip package archive...', 'status': 'running'})}\n\n"
            await asyncio.sleep(0.5)

            # Store zip archive in task storage
            task_storage[task_id] = {
                "course_id": course_id,
                "course_title": course_title,
                "zip_bytes": zip_bytes,
            }

            yield f"data: {json.dumps({'stage': 4, 'progress': 100, 'message': 'Extraction completed successfully!', 'status': 'completed', 'task_id': task_id})}\n\n"

        except Exception as e:
            logger.error(f"Error during extraction stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'stage': 0, 'progress': 0, 'message': str(e), 'status': 'error'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/download/{task_id}")
async def download_package(task_id: str):
    """
    Triggers download of the generated Markdown .zip package.
    """
    task = task_storage.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download package not found or expired.")

    zip_bytes = task["zip_bytes"]
    filename = f"{task['course_id']}_markdown_package.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
