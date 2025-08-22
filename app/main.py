"""FastAPI application offering a basic session check and stack deployment UI."""

from pathlib import Path
import json
import time

import boto3
from botocore.exceptions import ProfileNotFound, NoCredentialsError, ClientError
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI()
TEMPLATES = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def read_root() -> HTMLResponse:
    """Return a simple HTML form for AWS session parameters."""
    html = (TEMPLATES / "session.html").read_text()
    return HTMLResponse(content=html)

@app.post("/", response_class=HTMLResponse)
async def create_session(
    profile: str = Form(...),
    region: str = Form(...),
    stack_name: str = Form(...),
    feature: bool = Form(False),
) -> HTMLResponse:
    """Create a boto3 session based on form input and display result."""
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        # Trigger a call to ensure session is valid
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        message = (
            f"Created session for {identity['Arn']}<br>"
            f"Stack: {stack_name}<br>"
            f"Feature enabled: {feature}"
        )
        return HTMLResponse(content=message)
    except ProfileNotFound:
        return HTMLResponse(content="Profile not found", status_code=400)
    except (NoCredentialsError, ClientError) as exc:
        return HTMLResponse(content=f"Credentials error: {exc}", status_code=400)
    except Exception as exc:  # pragma: no cover - unexpected errors
        return HTMLResponse(content=f"Unexpected error: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# One-click deployment UI and handlers


@app.get("/deploy", response_class=HTMLResponse)
async def deploy_form() -> HTMLResponse:
    """Render the stack deployment form using Bootstrap."""
    html = (TEMPLATES / "deploy.html").read_text()
    return HTMLResponse(content=html)


def _stack_exists(cf_client, name: str) -> bool:
    """Return True if the stack exists in CloudFormation."""
    try:
        cf_client.describe_stacks(StackName=name)
        return True
    except cf_client.exceptions.ClientError:
        return False


def _wait_for_stack(cf_client, name: str):
    """Yield stack events until the stack completes or fails."""
    seen = set()
    while True:
        events = cf_client.describe_stack_events(StackName=name)["StackEvents"]
        new_events = [e for e in events if e["EventId"] not in seen]
        for event in reversed(new_events):
            msg = f"{name} {event['ResourceStatus']} {event.get('LogicalResourceId', '')}"
            seen.add(event["EventId"])
            yield f"data: {msg}\n\n"
        status = cf_client.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        if status.endswith("_COMPLETE") or status.endswith("_FAILED"):
            break
        time.sleep(5)


def _deploy_stack(cf_client, name: str, template: str, parameters=None):
    """Create or update a CloudFormation stack and stream its events."""
    params = parameters or []
    capabilities = ["CAPABILITY_NAMED_IAM", "CAPABILITY_IAM"]
    if _stack_exists(cf_client, name):
        change_set = f"{name}-changes"
        yield f"data: Updating {name}\n\n"
        cf_client.create_change_set(
            StackName=name,
            ChangeSetName=change_set,
            TemplateBody=template,
            Parameters=params,
            Capabilities=capabilities,
        )
        while True:
            desc = cf_client.describe_change_set(StackName=name, ChangeSetName=change_set)
            status = desc["Status"]
            if status in ("CREATE_COMPLETE", "FAILED"):
                break
            time.sleep(5)
        if status == "FAILED":
            yield f"data: No changes for {name}\n\n"
            cf_client.delete_change_set(StackName=name, ChangeSetName=change_set)
            return
        cf_client.execute_change_set(StackName=name, ChangeSetName=change_set)
    else:
        yield f"data: Creating {name}\n\n"
        cf_client.create_stack(
            StackName=name,
            TemplateBody=template,
            Parameters=params,
            Capabilities=capabilities,
        )
    yield from _wait_for_stack(cf_client, name)


def _stack_outputs(cf_client, name: str) -> dict:
    """Return stack outputs as a simple dictionary."""
    resp = cf_client.describe_stacks(StackName=name)
    outputs = {}
    for out in resp["Stacks"][0].get("Outputs", []):
        outputs[out["OutputKey"]] = out["OutputValue"]
    return outputs


def _stream_ssm_command(ssm_client, instance_id: str, **kwargs):
    """Send an SSM command and yield its output as SSE data lines."""
    resp = ssm_client.send_command(InstanceIds=[instance_id], **kwargs)
    command_id = resp["Command"]["CommandId"]
    stdout_len = 0
    stderr_len = 0
    while True:
        time.sleep(2)
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
            PluginName="aws:runShellScript",
        )
        stdout = invocation.get("StandardOutputContent", "")
        stderr = invocation.get("StandardErrorContent", "")
        if stdout_len < len(stdout):
            for line in stdout[stdout_len:].splitlines():
                yield f"data: {line}\n\n"
            stdout_len = len(stdout)
        if stderr_len < len(stderr):
            for line in stderr[stderr_len:].splitlines():
                yield f"data: {line}\n\n"
            stderr_len = len(stderr)
        status = invocation["Status"]
        if status not in ("Pending", "InProgress", "Delayed"):
            yield f"data: {status}\n\n"
            break


@app.post("/deploy")
async def deploy(
    profile: str = Form(...),
    region: str = Form(...),
    stack_name: str = Form(...),
):
    """Deploy VPC, MSK, EC2, and SSM stacks and stream progress events."""
    session = boto3.Session(profile_name=profile, region_name=region)
    cf = session.client("cloudformation")

    vpc_template = Path("vpc.yml").read_text()
    msk_template = Path("msk.yml").read_text()
    ec2_template = Path("ec2.yml").read_text()
    ssm_template = Path("ssm.yml").read_text()

    def event_stream():
        # VPC stack
        vpc_stack = f"{stack_name}-vpc"
        yield from _deploy_stack(cf, vpc_stack, vpc_template)
        vpc_outputs = _stack_outputs(cf, vpc_stack)

        # MSK stack
        msk_stack = f"{stack_name}-msk"
        msk_params = [
            {"ParameterKey": "MskSubnetIds", "ParameterValue": vpc_outputs.get("MskSubnetIds", "")},
            {"ParameterKey": "MskSecurityGroupId", "ParameterValue": vpc_outputs.get("MskSecurityGroupId", "")},
        ]
        yield from _deploy_stack(cf, msk_stack, msk_template, msk_params)
        msk_outputs = _stack_outputs(cf, msk_stack)

        # EC2 stack
        ec2_stack = f"{stack_name}-ec2"
        ec2_params = [
            {"ParameterKey": "Ec2SubnetId", "ParameterValue": vpc_outputs.get("Ec2SubnetId", "")},
            {"ParameterKey": "Ec2SecurityGroupId", "ParameterValue": vpc_outputs.get("Ec2SecurityGroupId", "")},
            {"ParameterKey": "MskClusterArn", "ParameterValue": msk_outputs.get("MskClusterArn", "")},
        ]
        yield from _deploy_stack(cf, ec2_stack, ec2_template, ec2_params)
        ec2_outputs = _stack_outputs(cf, ec2_stack)

        # SSM document
        ssm_stack = f"{stack_name}-ssm"
        yield from _deploy_stack(cf, ssm_stack, ssm_template)

        outputs = {
            "VpcId": vpc_outputs.get("VpcId", ""),
            "MskClusterArn": msk_outputs.get("MskClusterArn", ""),
            "Ec2InstanceId": ec2_outputs.get("Ec2InstanceId", ""),
        }
        yield f"data: Outputs: {json.dumps(outputs)}\n\n"
        yield "data: Deployment complete\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/test", response_class=HTMLResponse)
async def test_form() -> HTMLResponse:
    """Render the produce/consume test form."""
    html = (TEMPLATES / "test.html").read_text()
    return HTMLResponse(content=html)


@app.post("/test")
async def test(
    profile: str = Form(...),
    region: str = Form(...),
    stack_name: str = Form(...),
    topic_name: str = Form(...),
):
    """Run a produce/consume test against the MSK cluster via SSM."""
    session = boto3.Session(profile_name=profile, region_name=region)
    cf = session.client("cloudformation")
    kafka_client = session.client("kafka")
    ssm_client = session.client("ssm")

    msk_stack = f"{stack_name}-msk"
    ec2_stack = f"{stack_name}-ec2"
    msk_outputs = _stack_outputs(cf, msk_stack)
    ec2_outputs = _stack_outputs(cf, ec2_stack)
    cluster_arn = msk_outputs.get("MskClusterArn", "")
    instance_id = ec2_outputs.get("Ec2InstanceId", "")

    def event_stream():
        yield "data: Running setup document\n\n"
        yield from _stream_ssm_command(
            ssm_client,
            instance_id,
            DocumentName="MskClientSetupDocument",
        )

        yield "data: Fetching bootstrap brokers\n\n"
        brokers = kafka_client.get_bootstrap_brokers(ClusterArn=cluster_arn)[
            "BootstrapBrokerStringSaslIam"
        ]
        yield f"data: Brokers: {brokers}\n\n"

        commands = [
            f"/opt/kafka/bin/kafka-topics.sh --bootstrap-server '{brokers}' --command-config /opt/msk/client.properties --create --if-not-exists --topic '{topic_name}'",
            f"printf '1\\n2\\n3\\n4\\n5\\n' | /opt/msk/produce.sh '{brokers}' '{topic_name}'",
            f"/opt/msk/consume.sh '{brokers}' '{topic_name}' --from-beginning --max-messages 5",
        ]
        yield from _stream_ssm_command(
            ssm_client,
            instance_id,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
        )
        yield "data: Test complete\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

