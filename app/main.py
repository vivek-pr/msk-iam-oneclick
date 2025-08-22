"""FastAPI application offering a basic session check and stack deployment UI."""

from pathlib import Path
import json
import time

import boto3
from botocore.exceptions import ProfileNotFound, NoCredentialsError, ClientError
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI()

FORM_HTML = """
<!doctype html>
<html>
  <body>
    <h1>AWS Session Form</h1>
    <form method="post">
      <label>AWS Profile: <input type="text" name="profile" required></label><br>
      <label>Region: <input type="text" name="region" required></label><br>
      <label>Stack Name: <input type="text" name="stack_name" required></label><br>
      <label>Enable Feature: <input type="checkbox" name="feature"></label><br>
      <button type="submit">Submit</button>
    </form>
  </body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def read_root() -> HTMLResponse:
    """Return a simple HTML form for AWS session parameters."""
    return HTMLResponse(content=FORM_HTML)

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

DEPLOY_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>MSK IAM One-click Deploy</title>
    <link
      href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
      rel="stylesheet">
  </head>
  <body class="p-3">
    <h1 class="mb-4">Deploy stacks</h1>
    <form id="deploy-form" class="mb-3">
      <div class="mb-3">
        <label class="form-label">AWS Profile</label>
        <input class="form-control" name="profile" required>
      </div>
      <div class="mb-3">
        <label class="form-label">Region</label>
        <input class="form-control" name="region" required>
      </div>
      <div class="mb-3">
        <label class="form-label">Stack Name</label>
        <input class="form-control" name="stack_name" required>
      </div>
      <button class="btn btn-primary" type="submit">Deploy</button>
    </form>
    <pre id="events" class="border p-2 bg-light" style="height: 400px; overflow:auto;"></pre>
    <script>
    document.getElementById('deploy-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      const log = document.getElementById('events');
      log.textContent = '';
      const formData = new FormData(e.target);
      const response = await fetch('/deploy', {method: 'POST', body: formData});
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        for (const part of parts) {
          if (part.startsWith('data:')) {
            log.textContent += part.slice(5).trim() + '\n';
          }
        }
      }
    });
    </script>
  </body>
</html>
"""


@app.get("/deploy", response_class=HTMLResponse)
async def deploy_form() -> HTMLResponse:
    """Render the stack deployment form using Bootstrap."""
    return HTMLResponse(content=DEPLOY_HTML)


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

