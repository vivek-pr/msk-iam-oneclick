"""FastAPI application for MSK OneClick demo with long-polling operations."""

from __future__ import annotations

import configparser
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.sessions import SessionMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="MSK OneClick")
app.add_middleware(SessionMiddleware, secret_key="change-me")
TEMPLATES = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@dataclass
class Operation:
    """Track state for a long running operation."""

    status: str = "QUEUED"
    progress: int = 0
    logs: List[str] = field(default_factory=list)
    outputs: Dict[str, str] = field(default_factory=dict)
    error: Optional[Dict[str, str]] = None
    created: float = field(default_factory=time.time)


OPERATIONS: Dict[str, Operation] = {}
OPERATIONS_LOCK = threading.Lock()
TTL_SECONDS = 30 * 60


def _cleanup_operations() -> None:
    now = time.time()
    with OPERATIONS_LOCK:
        to_delete = [oid for oid, op in OPERATIONS.items() if now - op.created > TTL_SECONDS]
        for oid in to_delete:
            del OPERATIONS[oid]


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the main tabbed interface."""
    html = (TEMPLATES / "index.html").read_text()
    return HTMLResponse(content=html)


@app.get("/api/profiles")
def api_profiles() -> Dict[str, object]:
    """Return configured AWS profiles from credentials and config files."""
    cred_cfg = configparser.ConfigParser()
    cred_cfg.read(Path.home() / ".aws" / "credentials")
    conf_cfg = configparser.ConfigParser()
    conf_cfg.read(Path.home() / ".aws" / "config")

    profiles = set(cred_cfg.sections())
    for section in conf_cfg.sections():
        name = section.replace("profile ", "") if section.startswith("profile ") else section
        profiles.add(name)
    default = "default" if "default" in profiles else None
    return {"profiles": sorted(profiles), "default": default}


@app.get("/api/session")
def get_session(request: Request) -> Dict[str, Optional[str]]:
    """Return session information."""
    return {
        "profile": request.session.get("profile"),
        "region": request.session.get("region", "ap-south-1"),
        "stack_name": request.session.get("stack_name", "msk-iam-oneclick"),
    }


@app.post("/api/session")
async def set_session(request: Request) -> Dict[str, bool]:
    """Persist profile/region/stack in a server side session."""
    data = await request.json()
    profile = data.get("profile")
    region = data.get("region")
    stack = data.get("stack_name")
    if not profile:
        raise HTTPException(status_code=400, detail="profile required")
    request.session["profile"] = profile
    request.session["region"] = region or "ap-south-1"
    request.session["stack_name"] = stack or "msk-iam-oneclick"
    return {"ok": True}


def _register_operation(fn) -> str:
    op_id = str(uuid.uuid4())
    op = Operation(status="RUNNING", progress=0)
    with OPERATIONS_LOCK:
        OPERATIONS[op_id] = op
    thread = threading.Thread(target=fn, args=(op,), daemon=True)
    thread.start()
    return op_id


@app.post("/api/deploy")
async def api_deploy(request: Request) -> Dict[str, str]:
    """Simulate infrastructure deployment."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()

    def runner(op: Operation) -> None:
        try:
            steps = [
                ("Starting deployment", 5),
                ("Creating VPC", 25),
                ("Creating MSK cluster", 60),
                ("Launching EC2", 80),
                ("Finalising", 95),
            ]
            for msg, prog in steps:
                op.logs.append(msg)
                op.progress = prog
                time.sleep(1)
            op.outputs = {
                "ClusterArn": "arn:aws:kafka:region:acct:cluster/demo/123",
                "BootstrapBrokers": "b-1.example:9098,b-2.example:9098",
                "Ec2InstanceId": "i-0123456789abcdef0",
            }
            op.logs.append("Deployment complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:  # pragma: no cover - simulation should not fail
            op.logs.append(f"Error: {exc}")
            op.error = {"message": str(exc)}
            op.status = "FAILED"

    op_id = _register_operation(runner)
    return {"op_id": op_id}


@app.post("/api/test")
async def api_test(request: Request) -> Dict[str, str]:
    """Simulate produce/consume test."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()
    topic = data.get("TopicName", "poc-topic")

    def runner(op: Operation) -> None:
        try:
            steps = [
                ("Running setup", 20),
                ("Producing messages", 60),
                ("Consuming messages", 90),
            ]
            for msg, prog in steps:
                op.logs.append(msg)
                op.progress = prog
                time.sleep(1)
            messages = ["1", "2", "3", "4", "5"]
            op.outputs = {"messages": messages, "topic": topic}
            op.logs.append("Test complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:  # pragma: no cover
            op.logs.append(f"Error: {exc}")
            op.error = {"message": str(exc)}
            op.status = "FAILED"

    op_id = _register_operation(runner)
    return {"op_id": op_id}


@app.post("/api/teardown")
async def api_teardown(request: Request) -> Dict[str, str]:
    """Simulate stack deletion."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()

    def runner(op: Operation) -> None:
        try:
            steps = [
                ("Deleting SSM", 20),
                ("Deleting EC2", 50),
                ("Deleting MSK", 80),
                ("Deleting VPC", 95),
            ]
            for msg, prog in steps:
                op.logs.append(msg)
                op.progress = prog
                time.sleep(1)
            op.logs.append("Teardown complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:  # pragma: no cover
            op.logs.append(f"Error: {exc}")
            op.error = {"message": str(exc)}
            op.status = "FAILED"

    op_id = _register_operation(runner)
    return {"op_id": op_id}


@app.get("/api/op/{op_id}")
def api_operation(op_id: str, since: int = 0) -> Dict[str, object]:
    """Return operation status and new logs since the given cursor."""
    _cleanup_operations()
    with OPERATIONS_LOCK:
        op = OPERATIONS.get(op_id)
    if not op:
        raise HTTPException(status_code=404, detail="operation not found")
    logs = op.logs[since:]
    resp: Dict[str, object] = {
        "status": op.status,
        "progress": op.progress,
        "logs": logs,
        "cursor": since + len(logs),
    }
    if op.status == "SUCCEEDED" and op.outputs:
        resp["outputs"] = op.outputs
    if op.error:
        resp["error"] = op.error
    return resp

