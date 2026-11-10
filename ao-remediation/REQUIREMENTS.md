# RAN Timing Fault Remediation - Requirements

## Infrastructure

| Component | Required | Notes |
|-----------|----------|-------|
| OpenShift | Yes | Hosts the lab and the operators |
| Ansible Automation Platform | Yes | 2.7+, automation controller only, installed by its operator (see `deploy/base/aap/`) |
| Automation Orchestrator | Yes | 2026.8 add-on to AAP, installed by its operator |
| LLM endpoint | Yes | OpenAI-compatible; the lab's llm-d (RHOAI) gateway URL |
| multivendor-agentic-rca lab | Yes | `ran-sandbox-controller` (7098) and `nep-orchestrator` (7095) in `multivendor-rca` |


## AAP setup (automated)

| Job template name | Playbook |
|---|---|
| `RAN - Heal PTP Timing` | `ran_heal_ptp.yml` |
| `RAN - Close RCA` | `ran_close_rca.yml` |
| `RAN - Escalate` | `ran_escalate.yml` |
| `RAN - Record Rejection` | `ran_record_rejection.yml` |

The workflow refers to the templates by name, so these names must not change.


## AO setup

Manual, in the AO UI, on each new cluster:

1. **Service account** named `nep-orchestrator`, with a credential (client credentials). Keep the client ID and secret: the secret is shown once. Create it **before** the workflow import, because AO rejects a webhook trigger that has no authorized service account (an absent `authorized_service_account_ids` and an empty list were both rejected by AO's workflow validator). A different name works if you set `AO_SERVICE_ACCOUNT_NAME` in `deploy/base/aap/ao-import-job.yaml`.
2. **LLM integration** (provider "Red Hat AI", OpenAI-compatible) pointing at the llm-d gateway URL.
3. **AAP integration** for the job template steps. In-cluster, set the base URL to **`http://aap.aap.svc`** and turn on the integration's allow-HTTP setting. Use exactly that short form: AO's SSRF check rejects any hostname ending in `.svc.cluster.local` as "Kubernetes internal DNS", even when it is in the allow-list and allow-HTTP is on, so `http://aap.aap.svc.cluster.local` always fails with `base_url must not resolve to a private, reserved, or cloud metadata address`. The short name resolves the same way from inside the cluster. The base URL is scheme and host only (no path), and the `aap` Service listens on port 80 only, so use `http://`, not `https://`. Alternatively use AAP's Route, `https://<host from oc get route aap -n aap>`, which needs neither allow-HTTP nor an allow-list.
4. **Allow-lists for internal URLs.** AO also blocks hosts that resolve to private IP ranges. Two separate lists, hostnames only, no scheme or port:
   - **Workflow HTTP requests** (the verification RCA step): `workflowHttpRequestAllowedHosts` in the AutomationOrchestrator resource, set to `nep-orchestrator.multivendor-rca.svc` in `deploy/base/aap/ao-instance.yaml`. Nothing to do by hand. Use the short `.svc` form here too, for the same `.svc.cluster.local` reason.
   - **Integrations** (the AAP integration): `APP_INTEGRATION_URL_ALLOWED_HOSTS`, a JSON array, e.g. `["aap.aap.svc"]`, set on the `automation-orchestrator-backend`, `-worker` and `-background-worker` Deployments (all three; see `deploy/INSTALL.md` step 7). The AutomationOrchestrator resource has no field for it on AO 2026.8, so it is not GitOps-managed. It is only needed when the cluster's Service network is in a private range. On a cluster whose Services are in `172.30.0.0/16` the check blocks `aap.aap.svc` without the entry; on one whose Service CIDR is outside RFC 1918 (the cluster this was developed on used `172.231.0.0/16`) it passes without it. Check with `oc get svc aap -n aap -o jsonpath='{.spec.clusterIP}'`.
5. **Open the imported workflow** (see below), select the LLM on the `Verdict Explainer` node, and **publish** it.
6. **The `ao-webhook` Secret** in `multivendor-rca`, then restart NEP (`oc rollout restart deploy/nep-orchestrator -n multivendor-rca`; the env is read at start):
   - `AO_CLIENT_ID`, `AO_CLIENT_SECRET`: the service account credential from step 1.
   - `AO_URL`: AO's Route, `https://<host from oc get route automation-orchestrator -n aap>` (no path). Its certificate is trusted by the system CAs, and the call goes through the router, which AO's policies allow. The in-cluster Services are not usable from `multivendor-rca`: the backend (8000) serves TLS and only accepts AO's own pods, and the UI only accepts the router.
   - `AO_WEBHOOK_PATH=ran-rca-concluded` (the trigger's `webhook_path`).

   AO's token endpoint is OAuth 2.0 `client_credentials` with a form-encoded body; NEP sends it that way.


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
