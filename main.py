from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jose import jwt

load_dotenv()

MAGIC_LINK_SECRET = os.environ.get("MAGIC_LINK_SECRET", "sanaa-secret-2026-nsn")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", os.getenv("GITLAB_TOKEN", ""))
GITLAB_HOST = os.environ.get("GITLAB_HOST", "https://gitlab.nsn")
ASANA_TOKEN = os.environ.get("ASANA_TOKEN", os.getenv("ASANA_TOKEN", ""))
APP_URL = os.environ.get("APP_URL", "http://localhost:3000")

TOKEN_TTL_HOURS = 24

app = FastAPI(title="Sanaa")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://labnsn.com", "https://*.labnsn.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REPO_MAP = {
    "sportsmole": "sportsmole-main",
    "placar": "placar-apostas",
    "global": "core-wordpress",
    "ude": "umdoisesportes-main",
    "soccernet": "soccernet-main",
    "lb": "lakersbrasil-main",
    "uk": "sportsmole-main",
    "mundo": "mundodeportivo-main",
}

MS_MAP = {
    "users ms": "users-service",
    "websites ms": "websites-service",
    "invoices ms": "invoices-service",
}

ENRICHMENT_SYSTEM = """You are a senior developer at North Star Network (NSN), a sports media company with ~75 WordPress sites + custom microservices (Symfony/PHP on Kubernetes).
You enrich Asana tickets to make them easier to implement.

For each ticket, produce an improved version with:
- Clearer What/Where/Why/How sections
- Root cause hypothesis (mark as [hypothesis] if not confirmed)
- Specific file paths or code areas to investigate (based on repo context if provided)
- Concrete acceptance criteria (testable, specific)
- Estimated complexity: XS/S/M/L/XL

Be direct. No fluff. English only.
Mark anything you infer (not stated in the ticket) as [inferred]."""


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        payload = jwt.decode(token, MAGIC_LINK_SECRET, algorithms=["HS256"])
        return payload.get("email")
    except Exception:
        return None


def require_auth(request: Request) -> str:
    email = get_current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return email


# --- Auth routes ---


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/auth/request")
async def auth_request(email: str = Form(...)):
    if not email.endswith("@north-star.network"):
        raise HTTPException(status_code=400, detail="Only @north-star.network emails are allowed")

    payload = {
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, MAGIC_LINK_SECRET, algorithm="HS256")
    link = f"{APP_URL}/auth/verify?token={token}"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(
                "https://auth.labnsn.com/internal/send",
                json={
                    "to": email,
                    "subject": "Your Sanaa link",
                    "html": f'<a href="{link}">Sign in to Sanaa</a>',
                },
            )
        except Exception:
            pass  # best-effort send

    return JSONResponse({"ok": True, "message": "Check your email for the magic link"})


@app.get("/auth/verify")
async def auth_verify(token: str):
    try:
        payload = jwt.decode(token, MAGIC_LINK_SECRET, algorithms=["HS256"])
        email = payload["email"]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        max_age=TOKEN_TTL_HOURS * 3600,
        samesite="lax",
    )
    return response


@app.post("/auth/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Main routes ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    email = get_current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request, "email": email})


def identify_repo(task_name: str) -> str | None:
    bracket_match = re.search(r"\[([^\]]+)\]", task_name)
    if bracket_match:
        tag = bracket_match.group(1).strip().lower()
        if tag in REPO_MAP:
            return REPO_MAP[tag]

    name_lower = task_name.lower()
    for keyword, repo in MS_MAP.items():
        if keyword in name_lower:
            return repo

    return None


def extract_gid(input_str: str) -> str:
    input_str = input_str.strip()
    match = re.search(r"(\d{16,})", input_str)
    if match:
        return match.group(1)
    raise ValueError("Could not extract task GID from input")


@app.post("/enrich")
async def enrich(request: Request):
    email = require_auth(request)
    body = await request.json()
    task_input = body.get("task", "").strip()
    if not task_input:
        raise HTTPException(status_code=400, detail="Task URL or GID required")

    gid = extract_gid(task_input)

    # Fetch from Asana
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://app.asana.com/api/1.0/tasks/{gid}",
            params={"opt_fields": "name,notes,assignee.name"},
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch Asana task")
        task_data = resp.json()["data"]

    task_name = task_data.get("name", "")
    task_notes = task_data.get("notes", "")
    assignee = task_data.get("assignee", {})
    assignee_name = assignee.get("name", "Unassigned") if assignee else "Unassigned"

    # Identify repo and fetch context
    repo = identify_repo(task_name)
    repo_context = ""
    if repo:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
            try:
                # Search for project
                search_resp = await client.get(
                    f"{GITLAB_HOST}/api/v4/projects",
                    params={"search": repo, "per_page": 1},
                    headers=headers,
                )
                if search_resp.status_code == 200 and search_resp.json():
                    project = search_resp.json()[0]
                    project_id = project["id"]

                    # Fetch file tree
                    tree_resp = await client.get(
                        f"{GITLAB_HOST}/api/v4/projects/{project_id}/repository/tree",
                        params={"per_page": 50},
                        headers=headers,
                    )
                    if tree_resp.status_code == 200:
                        tree = tree_resp.json()
                        file_list = "\n".join(f"{'📁' if f['type'] == 'tree' else '📄'} {f['name']}" for f in tree)
                        repo_context += f"\n\nRepo: {repo}\nTop-level files:\n{file_list}"

                    # Fetch README
                    readme_resp = await client.get(
                        f"{GITLAB_HOST}/api/v4/projects/{project_id}/repository/files/README.md/raw",
                        params={"ref": "main"},
                        headers=headers,
                    )
                    if readme_resp.status_code == 200:
                        readme_text = readme_resp.text[:3000]
                        repo_context += f"\n\nREADME.md:\n{readme_text}"
            except Exception:
                pass  # GitLab unreachable — continue without repo context

    # Call Claude
    user_message = f"Task: {task_name}\nAssignee: {assignee_name}\n\nNotes:\n{task_notes}"
    if repo_context:
        user_message += f"\n\n--- Repo context ---{repo_context}"

    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    completion = claude.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=2000,
        system=ENRICHMENT_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    enriched_text = completion.content[0].text

    return JSONResponse({
        "original": {"name": task_name, "notes": task_notes, "assignee": assignee_name},
        "enriched": enriched_text,
        "repo": repo,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
