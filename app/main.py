"""FastAPI application for MSK OneClick demo with long-polling operations."""

from __future__ import annotations

import configparser
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

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
        "region": request.session.get("region", "us-east-1"),
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
    request.session["region"] = region or "us-east-1"
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


def _poll_stack_events(cf, stack_name: str, op: Operation) -> None:
    """Stream stack events until the stack reaches a terminal state."""
    seen: set[str] = set()
    while True:
        events = cf.describe_stack_events(StackName=stack_name)["StackEvents"]
        for ev in reversed(events):
            eid = ev["EventId"]
            if eid in seen:
                continue
            seen.add(eid)
            reason = ev.get("ResourceStatusReason", "")
            msg = f"{ev['ResourceStatus']} {ev['LogicalResourceId']} {reason}".strip()
            op.logs.append(msg)
        stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
        status = stack["StackStatus"]
        if status.endswith("COMPLETE"):
            return
        if "FAILED" in status or "ROLLBACK" in status:
            raise Exception(f"{stack_name} {status}")
        time.sleep(5)


def _deploy_stack(cf, stack_name: str, template_body: str, parameters: List[Dict[str, str]], op: Operation) -> None:
    """Create or update a stack and stream its events."""
    exists = True
    try:
        cf.describe_stacks(StackName=stack_name)
    except ClientError as e:
        if "does not exist" in str(e):
            exists = False
        else:
            raise
    kwargs = {
        "StackName": stack_name,
        "TemplateBody": template_body,
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
    }
    if exists:
        try:
            cf.update_stack(**kwargs)
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                op.logs.append(f"No changes for {stack_name}")
                return
            raise
    else:
        cf.create_stack(**kwargs)
    _poll_stack_events(cf, stack_name, op)


def _delete_stack(cf, stack_name: str, op: Operation) -> None:
    try:
        cf.describe_stacks(StackName=stack_name)
    except ClientError as e:
        if "does not exist" in str(e):
            op.logs.append(f"Stack {stack_name} missing")
            return
        raise
    cf.delete_stack(StackName=stack_name)
    _poll_stack_events(cf, stack_name, op)


def _get_stack_outputs(cf, stack_name: str) -> Dict[str, str]:
    stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


@app.post("/api/deploy")
async def api_deploy(request: Request) -> Dict[str, str]:
    """Create or update the CloudFormation stacks and stream events."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()
    profile = data.get("profile") or request.session["profile"]
    region = request.session.get("region")
    stack_base = request.session.get("stack_name", "msk-iam-oneclick")
    create_nat = data.get("CreateNAT", False)
    existing_tgw = data.get("ExistingTransitGatewayId")

    def runner(op: Operation) -> None:
        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            cf = session.client("cloudformation")
            kafka = session.client("kafka")
            infra = Path(__file__).resolve().parent.parent / "infra"

            op.logs.append("Deploying network stack")
            net_params = [
                {
                    "ParameterKey": "CreateNAT",
                    "ParameterValue": "true" if create_nat else "false",
                }
            ]
            if existing_tgw:
                net_params.append(
                    {
                        "ParameterKey": "ExistingTransitGatewayId",
                        "ParameterValue": existing_tgw,
                    }
                )
            _deploy_stack(
                cf,
                f"{stack_base}-network",
                (infra / "network.yaml").read_text(),
                net_params,
                op,
            )
            op.progress = 25
            network_outputs = _get_stack_outputs(cf, f"{stack_base}-network")

            op.logs.append("Deploying MSK stack")
            _deploy_stack(
                cf,
                f"{stack_base}-msk",
                (infra / "msk-provisioned.yaml").read_text(),
                [
                    {"ParameterKey": "MskSubnetIds", "ParameterValue": network_outputs["MskSubnetIds"]},
                    {"ParameterKey": "MskSecurityGroupId", "ParameterValue": network_outputs["MskSecurityGroupId"]},
                ],
                op,
            )
            op.progress = 50
            msk_outputs = _get_stack_outputs(cf, f"{stack_base}-msk")
            cluster_arn = msk_outputs["MskClusterArn"]

            op.logs.append("Deploying EC2 stack")
            _deploy_stack(
                cf,
                f"{stack_base}-ec2",
                (infra / "ec2.yml").read_text(),
                [
                    {"ParameterKey": "Ec2SubnetId", "ParameterValue": network_outputs["Ec2SubnetId"]},
                    {"ParameterKey": "Ec2SecurityGroupId", "ParameterValue": network_outputs["Ec2SecurityGroupId"]},
                    {"ParameterKey": "MskClusterArn", "ParameterValue": cluster_arn},
                ],
                op,
            )
            op.progress = 75
            ec2_outputs = _get_stack_outputs(cf, f"{stack_base}-ec2")
            instance_id = ec2_outputs["Ec2InstanceId"]

            op.logs.append("Deploying SSM stack")
            _deploy_stack(
                cf,
                f"{stack_base}-ssm",
                (infra / "ssm.yml").read_text(),
                [],
                op,
            )
            op.progress = 90

            brokers = kafka.get_bootstrap_brokers(ClusterArn=cluster_arn).get(
                "BootstrapBrokerStringSaslIam", ""
            )
            op.outputs = {
                "ClusterArn": cluster_arn,
                "BootstrapBrokers": brokers,
                "Ec2InstanceId": instance_id,
            }
            op.logs.append("Deployment complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:
            op.logs.append(f"Error: {exc}")
            op.error = {"message": str(exc)}
            op.status = "FAILED"

    op_id = _register_operation(runner)
    return {"op_id": op_id}


@app.post("/api/test")
async def api_test(request: Request) -> Dict[str, str]:
    """Run produce/consume test on the EC2 client via SSM."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()
    profile = data.get("profile") or request.session["profile"]
    topic = data.get("TopicName", "poc-topic")
    region = request.session.get("region")
    stack_base = request.session.get("stack_name", "msk-iam-oneclick")

    def runner(op: Operation) -> None:
        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            cf = session.client("cloudformation")
            ssm = session.client("ssm")
            kafka = session.client("kafka")

            msk_outputs = _get_stack_outputs(cf, f"{stack_base}-msk")
            ec2_outputs = _get_stack_outputs(cf, f"{stack_base}-ec2")
            cluster_arn = msk_outputs["MskClusterArn"]
            instance_id = ec2_outputs["Ec2InstanceId"]
            brokers = kafka.get_bootstrap_brokers(ClusterArn=cluster_arn)[
                "BootstrapBrokerStringSaslIam"
            ]

            op.logs.append("Running test via SSM")
            cmd = (
                "set -e; "
                f"/opt/kafka/bin/kafka-topics.sh --bootstrap-server {brokers} --topic {topic} --create --if-not-exists; "
                f"printf '1\\n2\\n3\\n4\\n5\\n' | /opt/msk/produce.sh {brokers} {topic}; "
                f"/opt/msk/consume.sh {brokers} {topic} --max-messages 5 --timeout-ms 10000"
            )
            resp = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [cmd]},
            )
            cmd_id = resp["Command"]["CommandId"]

            while True:
                inv = ssm.get_command_invocation(
                    CommandId=cmd_id, InstanceId=instance_id
                )
                status = inv["Status"]
                if status in ("Pending", "InProgress", "Delayed"):
                    time.sleep(5)
                    continue
                if status != "Success":
                    raise Exception(inv.get("StandardErrorContent") or status)
                output = inv.get("StandardOutputContent", "")
                messages = [
                    line for line in output.splitlines() if line.strip()
                ][-5:]
                op.outputs = {"messages": messages, "topic": topic}
                break

            op.logs.append("Test complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:
            op.logs.append(f"Error: {exc}")
            op.error = {"message": str(exc)}
            op.status = "FAILED"

    op_id = _register_operation(runner)
    return {"op_id": op_id}


@app.post("/api/teardown")
async def api_teardown(request: Request) -> Dict[str, str]:
    """Delete the CloudFormation stacks in reverse order."""
    if "profile" not in request.session:
        raise HTTPException(status_code=400, detail="session not initialised")
    data = await request.json()
    profile = data.get("profile") or request.session["profile"]
    region = request.session.get("region")
    stack_base = request.session.get("stack_name", "msk-iam-oneclick")

    def runner(op: Operation) -> None:
        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            cf = session.client("cloudformation")
            steps = [
                (f"{stack_base}-ssm", 20),
                (f"{stack_base}-ec2", 50),
                (f"{stack_base}-msk", 80),
                (f"{stack_base}-network", 95),
            ]
            for name, prog in steps:
                op.logs.append(f"Deleting {name}")
                _delete_stack(cf, name, op)
                op.progress = prog
            op.logs.append("Teardown complete")
            op.progress = 100
            op.status = "SUCCEEDED"
        except Exception as exc:
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

