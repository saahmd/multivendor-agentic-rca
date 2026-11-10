# RAN Timing Fault Remediation - Requirements

## Infrastructure

| Component | Required | Notes |
|-----------|----------|-------|
| OpenShift | Yes | Hosts the lab and the operators |
| Ansible Automation Platform | Yes | 2.7+, automation controller only, installed by its operator (see `deploy/base/aap/`) |
| Automation Orchestrator | Yes | 2026.8 add-on to AAP, installed by its operator |
| LLM endpoint | Yes | OpenAI-compatible; the lab's llm-d (RHOAI) gateway URL |
| multivendor-agentic-rca lab | Yes | `ran-sandbox-controller` (7098) and `nep-orchestrator` (7095) in `multivendor-rca` |

No MCP server and no mcp-gateway change: the AO agent has no tools.

## AAP setup (automated)

The `mvrca-aap` Argo CD Application runs `deploy/base/aap/bootstrap/bootstrap_aap.py` as a PostSync Job (`aap-bootstrap`) once AAP is up. It is idempotent, so it runs on every sync. It creates, in organization `Default`:

- project `RAN Remediation`: this repo from the in-cluster Gitea, branch `main` (change `SCM_URL` / `SCM_BRANCH` in `deploy/base/aap/bootstrap-job.yaml` for another source). **Push the repo to Gitea first**, or the project sync fails and the Job retries.
- inventory `RAN Localhost` with one host, `localhost`, local connection (skipped with a warning if AAP has no subscription).
- job templates, each allowing extra variables at launch:

| Job template name | Playbook |
|---|---|
| `RAN - Heal PTP Timing` | `ran_heal_ptp.yml` |
| `RAN - Close RCA` | `ran_close_rca.yml` |
| `RAN - Escalate` | `ran_escalate.yml` |
| `RAN - Record Rejection` | `ran_record_rejection.yml` |

The workflow refers to the templates by name, so these names must not change.

**AAP needs a subscription.** A fresh AAP has none (`license_info` is empty in `/api/controller/v2/config/`) and then refuses to create hosts or launch jobs (`License is missing`), so the heal job cannot run. Attach one in the AAP UI (Settings > Subscription), or give the bootstrap a Red Hat subscription manifest:
`oc create secret generic aap-subscription -n aap --from-literal=manifest="$(base64 < manifest.zip | tr -d '\n')"`, then re-sync `mvrca-aap`. Without a subscription the Job still creates the project and templates but exits non-zero on purpose, so the sync shows as failed until one is attached. Check the result with `oc logs job/aap-bootstrap -n aap`. The Job reads the admin password from Secret `aap-admin-password` (key `password`), which the AAP operator creates for the instance named `aap`. Without Argo CD, `oc apply -k deploy/overlays/aap` runs it as an ordinary Job.

## AO setup

Manual, in the AO UI, on each new cluster:

1. **Service account** named `nep-orchestrator`, with a credential (client credentials). Keep the client ID and secret: the secret is shown once. Create it **before** the workflow import, because AO rejects a webhook trigger that has no authorized service account (an absent `authorized_service_account_ids` and an empty list were both rejected by AO's workflow validator). A different name works if you set `AO_SERVICE_ACCOUNT_NAME` in `deploy/base/aap/ao-import-job.yaml`.
2. **LLM integration** (provider "Red Hat AI", OpenAI-compatible) pointing at the llm-d gateway URL.
3. **AAP integration** for the job template steps. In-cluster, set the base URL to **`http://aap.aap.svc`** and turn on the integration's allow-HTTP setting. Use exactly that short form: AO's SSRF check rejects any hostname ending in `.svc.cluster.local` as "Kubernetes internal DNS", even when it is in the allow-list and allow-HTTP is on, so `http://aap.aap.svc.cluster.local` always fails with `base_url must not resolve to a private, reserved, or cloud metadata address`. The short name resolves the same way from inside the cluster. The base URL is scheme and host only (no path), and the `aap` Service listens on port 80 only, so use `http://`, not `https://`. Alternatively use AAP's Route, `https://<host from oc get route aap -n aap>`, which needs neither allow-HTTP nor an allow-list.
4. **Allow-lists for internal URLs.** AO also blocks hosts that resolve to private IP ranges. Two separate lists, hostnames only, no scheme or port:
   - **Workflow HTTP requests** (the verification RCA step): `workflowHttpRequestAllowedHosts` in the AutomationOrchestrator resource, set to `nep-orchestrator.multivendor-rca.svc` in `deploy/base/aap/ao-instance.yaml`. Nothing to do by hand. Use the short `.svc` form here too, for the same `.svc.cluster.local` reason.
   - **Integrations** (the AAP integration): `APP_INTEGRATION_URL_ALLOWED_HOSTS`, a JSON array on the AO backend, e.g. `["aap.aap.svc"]`. The AutomationOrchestrator resource has no field for it on AO 2026.8, so it has to be set on the backend Deployment, which is not GitOps-managed. It is only needed when the cluster's Service network is in a private range. On a cluster whose Services are in `172.30.0.0/16` the check blocks `aap.aap.svc` without the entry; on one whose Service CIDR is outside RFC 1918 (the cluster this was developed on used `172.231.0.0/16`) it passes without it. Check with `oc get svc aap -n aap -o jsonpath='{.spec.clusterIP}'`.
5. **Open the imported workflow** (see below), select the LLM on the `Verdict Explainer` node, and **publish** it.
6. **The `ao-webhook` Secret** in `multivendor-rca`, then restart NEP (`oc rollout restart deploy/nep-orchestrator -n multivendor-rca`; the env is read at start):
   - `AO_CLIENT_ID`, `AO_CLIENT_SECRET`: the service account credential from step 1.
   - `AO_URL`: AO's Route, `https://<host from oc get route automation-orchestrator -n aap>` (no path). Its certificate is trusted by the system CAs, and the call goes through the router, which AO's policies allow. The in-cluster Services are not usable from `multivendor-rca`: the backend (8000) serves TLS and only accepts AO's own pods, and the UI only accepts the router.
   - `AO_WEBHOOK_PATH=ran-rca-concluded` (the trigger's `webhook_path`).

   AO's token endpoint is OAuth 2.0 `client_credentials` with a form-encoded body; NEP sends it that way.

### Referencing the agent's output

Despite AO's schema documenting an `output` field, an agent step's output at runtime is the raw agent result. With `response_schema` set, the fields sit at `${verdict_explainer.result.content.<field>}`. `${verdict_explainer.output.<field>}` fails with `Property "output" not found in step "verdict_explainer" output`. Observed on AO 2026.8.

The model sometimes wraps its answer in an extra key. On one run the fields were at `result.content.final.<field>` and on later runs, with the same model and settings, at `result.content.<field>`. The reference path cannot adapt, so a wrapped answer fails the approval step with `Property "final" not found ...`. The workflow reduces the risk by making all four fields `required`, setting `additionalProperties: false`, and telling the model to use no wrapper key. If it still happens, re-run; check an execution's stored step output to see the shape.

### Workflow import (GitOps)

The `mvrca-aap` Argo CD Application runs a second PostSync Job, `ao-workflow-import` (`deploy/base/aap/bootstrap/import_ao_workflow.py`). It logs in as the AO local admin (the operator's `automation-orchestrator-initial-admin-password` Secret; the username `admin` is an assumption) and:

- finds the service account from step 1 by name and writes its UUID into the webhook trigger. The repo JSON deliberately holds no UUID, since it differs on every cluster.
- fetches `ao-remediation/ao/ran-timing-remediation.json` from the Gitea repo and creates the workflow **as a draft** in the service account's AO project.
- skips with a `SKIPPED` log line and exit 0 if the service account does not exist yet. Create it, then **sync the application again** in Argo CD.
- never overwrites an existing workflow, so the LLM you select and any edits survive later syncs. To re-import after changing the JSON, delete the workflow in AO and sync again.
- does not publish (the LLM is chosen by hand, step 5).
- bakes in the service account's UUID at import time. **If you delete and recreate the service account afterwards** (for example to get a new secret; rotating the credential keeps the account and its UUID), the workflow still lists the old UUID and publishing fails with `Service account(s) not found in this project: <old uuid>`. Fix it in the workflow's webhook trigger by removing the old account and keeping the new one, or delete the workflow and sync again.

Check it with `oc logs job/ao-workflow-import -n aap`. A small NetworkPolicy (`ao-workflow-import-to-ui`) lets only that Job reach the AO UI Service, since AO's own policies admit only the OpenShift router. Without Argo CD, `oc apply -k deploy/overlays/aap` runs it as an ordinary Job. To import by hand instead, add the UUID to a copy of the file and import that:

```bash
SA_ID=<uuid of the service account>
jq --arg id "$SA_ID" '.triggers[0].parameters.authorized_service_account_ids = [$id]' \
   ao/ran-timing-remediation.json > /tmp/ran-timing-remediation.json
```

## Network

NetworkPolicy must allow ingress from namespace `aap` to `ran-sandbox-controller` and `nep-orchestrator`, and egress from `nep-orchestrator` to the AO Route or Service.

## Webhook payload

```json
{ "eventId": "evt-...", "traceId": "...", "verdict": "HOLD",
  "decision": "HOLD - below the bar, needs human approval",
  "faultClass": "OC-TimingDegraded", "signal": "ptp_not_locked",
  "corroboration": "2/3", "planes": ["ran","redhat"],
  "evidence": [], "llmNarrative": "...",
  "target": { "cell": "odu-1", "ptpBridge": "ptp-bridge:7091" } }
```

`verdict` is `APPLY`, `HOLD` or `NO_ACTION`. It is a new machine field added to NEP's event (implementation step 3).
