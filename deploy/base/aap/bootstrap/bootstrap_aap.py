#!/usr/bin/env python3
"""Create the AAP objects the Automation Orchestrator workflow launches. Idempotent: every object is
looked up by name and created or updated, so re-running (Argo CD runs it after every sync) is safe.

Creates, in organization Default:
  project         RAN Remediation          this repo, from the in-cluster Gitea (public repo, no credential)
  inventory       RAN Localhost            one host, localhost, local connection (the playbooks only call HTTP APIs)
  job templates   RAN - Heal PTP Timing / Close RCA / Escalate / Record Rejection

The AO workflow (ao-remediation/ao/ran-timing-remediation.json) refers to these templates by name, so the
names here and in the workflow must match. Stdlib only. Config comes from environment variables.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

AAP_URL = os.environ.get("AAP_URL", "http://aap.aap.svc.cluster.local").rstrip("/")
API = AAP_URL + "/api/controller/v2"
ADMIN_USER = os.environ.get("AAP_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ["AAP_ADMIN_PASSWORD"]
SCM_URL = os.environ.get("SCM_URL", "http://gitea-http.gitea.svc.cluster.local:3000/gitadmin/multivendor-agentic-rca.git")
SCM_BRANCH = os.environ.get("SCM_BRANCH", "main")
ORG = os.environ.get("AAP_ORGANIZATION", "Default")
WAIT_READY = float(os.environ.get("WAIT_READY_SECONDS", "2400"))     # AAP takes a while to come up
WAIT_PROJECT = float(os.environ.get("WAIT_PROJECT_SECONDS", "300"))
PLAYBOOK_DIR = "ao-remediation/aap/playbooks"

TEMPLATES = {
    "RAN - Heal PTP Timing": "ran_heal_ptp.yml",
    "RAN - Close RCA": "ran_close_rca.yml",
    "RAN - Escalate": "ran_escalate.yml",
    "RAN - Record Rejection": "ran_record_rejection.yml",
}

_AUTH = "Basic " + base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()


def log(msg):
    print(msg, flush=True)


def call(method, path, body=None, timeout=30):
    """Returns (status, parsed JSON or {}). Transport failures come back as status 0."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": _AUTH, "Content-Type": "application/json"})
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


def wait_for_aap():
    log(f"waiting for AAP at {API} (up to {WAIT_READY:.0f}s)")
    deadline = time.time() + WAIT_READY
    last = None
    while time.time() < deadline:
        code, body = call("GET", "/ping/", timeout=10)
        if code == 200:
            code, _ = call("GET", "/organizations/?page_size=1")
            if code == 200:
                log("AAP controller is up and the admin login works")
                return
            last = f"ping ok but admin login returned HTTP {code}"
        else:
            last = f"HTTP {code}: {body.get('detail', '')}"
        time.sleep(15)
    sys.exit(f"AAP not ready after {WAIT_READY:.0f}s ({last})")


def find(kind, name, **filters):
    q = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in {"name": name, **filters}.items())
    code, body = call("GET", f"/{kind}/?{q}")
    if code != 200:
        sys.exit(f"lookup {kind} '{name}' failed: HTTP {code} {body}")
    return (body.get("results") or [None])[0]


def ensure(kind, name, payload, **filters):
    """Create the object, or PATCH it if it already exists. Returns the object."""
    existing = find(kind, name, **filters)
    if existing:
        code, body = call("PATCH", f"/{kind}/{existing['id']}/", payload)
        verb = "updated"
    else:
        code, body = call("POST", f"/{kind}/", {"name": name, **payload})
        verb = "created"
    if code not in (200, 201):
        sys.exit(f"{kind} '{name}' could not be {verb}: HTTP {code} {body}")
    log(f"{kind} '{name}' {verb} (id {body['id']})")
    return body


def wait_for_project(project):
    log(f"waiting for project '{project['name']}' to sync from {SCM_URL} (up to {WAIT_PROJECT:.0f}s)")
    deadline = time.time() + WAIT_PROJECT
    status = None
    while time.time() < deadline:
        code, body = call("GET", f"/projects/{project['id']}/")
        status = body.get("status")
        if status == "successful":
            log("project synced")
            return
        if status in ("failed", "error", "canceled"):
            sys.exit(f"project sync {status}: check that {SCM_URL} exists and branch '{SCM_BRANCH}' has "
                     f"{PLAYBOOK_DIR}/ (push this repo to Gitea first)")
        time.sleep(5)
    sys.exit(f"project still '{status}' after {WAIT_PROJECT:.0f}s")


def main():
    wait_for_aap()
    org = find("organizations", ORG)
    if not org:
        sys.exit(f"organization '{ORG}' not found")

    project = ensure("projects", "RAN Remediation",
                     {"organization": org["id"], "scm_type": "git", "scm_url": SCM_URL, "scm_branch": SCM_BRANCH,
                      "scm_update_on_launch": True, "description": "Playbooks for the AO RAN timing remediation workflow"},
                     organization=org["id"])
    wait_for_project(project)

    inv = ensure("inventories", "RAN Localhost", {"organization": org["id"],
                 "description": "The remediation playbooks only call in-cluster HTTP APIs from the controller"},
                 organization=org["id"])
    if not find("hosts", "localhost", inventory=inv["id"]):
        code, body = call("POST", f"/inventories/{inv['id']}/hosts/",
                          {"name": "localhost", "variables": "ansible_connection: local\nansible_python_interpreter: \"{{ ansible_playbook_python }}\""})
        if code not in (200, 201):
            sys.exit(f"host 'localhost' could not be created: HTTP {code} {body}")
        log("host 'localhost' created")

    for name, playbook in TEMPLATES.items():
        ensure("job_templates", name,
               {"organization": org["id"], "project": project["id"], "inventory": inv["id"],
                "playbook": f"{PLAYBOOK_DIR}/{playbook}", "job_type": "run", "ask_variables_on_launch": True,
                "description": "Launched by the Automation Orchestrator workflow 'RAN Timing Fault - Governed Remediation'"},
               organization=org["id"])

    log("AAP side of the RAN remediation is ready. The AO side (integrations, service account, workflow import) "
        "is manual: see ao-remediation/REQUIREMENTS.md")


if __name__ == "__main__":
    main()
