#!/usr/bin/env python3
"""Import the RAN remediation workflow into Automation Orchestrator (AO). Run by a GitOps PostSync Job.

What it does:
  1. waits for AO, logs in as the AO local admin (password from the operator's initial-admin Secret)
  2. looks up the service account you created by hand in AO (default name: nep-orchestrator). If it does not
     exist yet it says so and exits 0: create it, then sync the app again. AO refuses a webhook trigger without
     an authorized service account, so the workflow cannot be imported before it exists.
  3. fetches ao-remediation/ao/ran-timing-remediation.json from the repo (Gitea raw URL), puts the service
     account's UUID into the webhook trigger, and creates the workflow in the service account's AO project.

What it deliberately does not do:
  - publish. The Verdict Explainer agent needs an LLM, which is chosen by hand in the AO UI; pick it and publish there.
  - overwrite. If a workflow with the same name exists it is left alone, so the LLM you picked and any edits made in
    the UI survive later syncs. To re-import after changing the JSON, delete the workflow in AO and sync again.

Stdlib only. Configuration comes from environment variables.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AO_URL = os.environ.get("AO_URL", "http://automation-orchestrator-ui.aap.svc.cluster.local").rstrip("/")
API = AO_URL + "/api/v1"
ADMIN_USER = os.environ.get("AO_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ["AO_ADMIN_PASSWORD"]
SERVICE_ACCOUNT = os.environ.get("AO_SERVICE_ACCOUNT_NAME", "nep-orchestrator")
WORKFLOW_URL = os.environ.get(
    "WORKFLOW_URL",
    "http://gitea-http.gitea.svc.cluster.local:3000/gitadmin/multivendor-agentic-rca/raw/branch/main/"
    "ao-remediation/ao/ran-timing-remediation.json")
WAIT_READY = float(os.environ.get("WAIT_READY_SECONDS", "2400"))


def log(msg):
    print(msg, flush=True)


def http(method, url, body=None, token=None, timeout=30):
    """Returns (status, parsed JSON or {}). Transport failures come back as status 0."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"detail": raw.decode(errors="replace")[:300]}
    except Exception as e:
        return 0, {"detail": str(e)}


def wait_for_ao():
    log(f"waiting for AO at {API} (up to {WAIT_READY:.0f}s)")
    deadline = time.time() + WAIT_READY
    last = None
    while time.time() < deadline:
        code, body = http("GET", API + "/auth/providers", timeout=10)     # public endpoint
        if code == 200:
            log("AO is up")
            return
        last = f"HTTP {code}: {body.get('detail', '')}"
        time.sleep(15)
    sys.exit(f"AO not ready after {WAIT_READY:.0f}s ({last})")


def login():
    code, body = http("POST", API + "/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    if code != 200 or not body.get("access_token"):
        sys.exit(f"AO login as '{ADMIN_USER}' failed: HTTP {code} {body.get('detail') or body.get('title') or body}")
    log(f"logged in to AO as '{ADMIN_USER}'")
    return body["access_token"]


def list_all(path, token, query=None):
    """All items of a cursor-paginated list endpoint."""
    items, cursor = [], None
    while True:
        q = {"limit": 100, **(query or {})}
        if cursor:
            q["cursor"] = cursor
        code, body = http("GET", f"{API}{path}?{urllib.parse.urlencode(q)}", token=token)
        if code != 200:
            sys.exit(f"GET {path} failed: HTTP {code} {body.get('detail') or body}")
        items += body.get("resources", [])
        cursor = body.get("next")
        if not cursor:
            return items


def fetch_workflow():
    req = urllib.request.Request(WORKFLOW_URL)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        sys.exit(f"could not fetch the workflow JSON from {WORKFLOW_URL}: {e} (push the repo to Gitea first)")


def main():
    wait_for_ao()
    token = login()

    accounts = [a for a in list_all("/service_accounts", token) if a.get("name") == SERVICE_ACCOUNT]
    if not accounts:
        log(f"SKIPPED: no AO service account named '{SERVICE_ACCOUNT}' yet. Create it in the AO UI (with a credential), "
            f"then sync this Argo CD application again to import the workflow.")
        return
    account = accounts[0]
    log(f"service account '{SERVICE_ACCOUNT}' found (id {account['id']}, project {account.get('project_name') or account['project_id']})")

    workflow = fetch_workflow()
    name = workflow["name"]
    existing = [w for w in list_all("/workflows", token) if w.get("name") == name]
    if existing:
        w = existing[0]
        log(f"workflow '{name}' already exists (id {w['id']}, version {w['current_version']}, published version "
            f"{w.get('published_version_number')}); leaving it untouched")
        return

    triggers = [t for t in workflow.get("triggers", []) if t.get("type") == "webhook_trigger"]
    if not triggers:
        sys.exit("the workflow JSON has no webhook_trigger")
    for t in triggers:
        t.setdefault("parameters", {})["authorized_service_account_ids"] = [account["id"]]

    code, body = http("POST", API + "/workflows", {
        "name": name, "description": workflow.get("description"),
        "workflow_definition": workflow, "project_id": account["project_id"], "is_import": True}, token=token)
    if code not in (200, 201):
        sys.exit(f"creating workflow '{name}' failed: HTTP {code} {body.get('detail') or body}")
    log(f"workflow '{name}' created as a draft (id {body['id']}, version {body['current_version']})")
    result = body.get("validation_result") or {}
    for f in (result.get("findings") or [])[:10]:
        log(f"  validation {f.get('severity', '')}: {f.get('message', f)}")
    log("NEXT: in the AO UI open the workflow, select the LLM on the 'Verdict Explainer' node, and publish it. "
        "This job does not publish and will not overwrite the workflow on later syncs.")


if __name__ == "__main__":
    main()
