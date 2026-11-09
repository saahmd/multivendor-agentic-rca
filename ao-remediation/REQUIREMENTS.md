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

- **LLM integration** (provider "Red Hat AI", OpenAI-compatible) pointing at the llm-d gateway URL. Select it for the `Verdict Explainer` agent after import.
- **AAP integration** for the job template steps.
- **Internal URL allowlist** for the REST step: `http://nep-orchestrator.multivendor-rca.svc:7095`.
- **Service account** (client credentials) for NEP's webhook call. Put its UUID in the trigger's `authorized_service_account_ids` in `ao/ran-timing-remediation.json` (this lab uses service account `sa1`, UUID `8f631e2a-dd19-4190-8018-a09583bea5f9`, which exists only on this cluster). The webhook path is `ran-rca-concluded`, so NEP's `AO_WEBHOOK_PATH` is `ran-rca-concluded`. Its id and secret go into the `ao-webhook` Secret in `multivendor-rca` (`AO_CLIENT_ID`, `AO_CLIENT_SECRET`), together with `AO_URL` and `AO_WEBHOOK_PATH`.

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
