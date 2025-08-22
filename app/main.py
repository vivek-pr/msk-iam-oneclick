from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
import boto3
from botocore.exceptions import ProfileNotFound, NoCredentialsError, ClientError

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
