# Install

`oc` logged in. Work from the **repo root**. GitOps only — do not `oc patch`.

Argo CD syncs **Gitea**, not GitHub. A GitHub clone does not fill Gitea. After any later change, push `main` to Gitea again.

llm-d is **CPU or GPU**, not both. `deploy/overlays/multivendor/kustomization.yaml` currently includes `llama-31-8b-llmisvc-gpu.yaml`. For CPU, comment that line, uncomment `llama-31-8b-llmisvc-cpu.yaml`, commit, and push Gitea.

## 1. Install Gitea

```bash
oc apply -k deploy/gitea
```

Wait until it is up:

```bash
oc -n gitea rollout status deploy/gitea
oc -n gitea wait --for=condition=complete job/gitea-bootstrap --timeout=10m
```

## 2. Read Gitea connection details from the cluster

Hostname:

```bash
GITEA_HOST=$(oc get route gitea -n gitea -o jsonpath='{.spec.host}')
```

Username and password (Secret `gitea-admin-credentials` — do not hardcode):

```bash
GITEA_USER=$(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.username}' | base64 -d)
GITEA_PASS=$(oc get secret gitea-admin-credentials -n gitea -o jsonpath='{.data.password}' | base64 -d)
```

Encode the password for the git URL (`!` becomes `%21`; an unencoded `!` fails in zsh):

```bash
GITEA_PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$GITEA_PASS")
```

## 3. Push this repo into Gitea

> **NOTE:** If you are using a GPU with less than 24Gb of RAM, or need to use CPU Inference please see the [Troubleshooting](#troubleshooting) section below before continuing

Uses the variables from step 2.

```bash
GIT_SSL_NO_VERIFY=true git push \
  "https://${GITEA_USER}:${GITEA_PASS_ENC}@${GITEA_HOST}/${GITEA_USER}/multivendor-agentic-rca.git" \
  HEAD:main
```

## 4. Install Argo CD

Operator and `cluster-admin` for the GitOps controller (needed for cluster-scoped CRs such as `DataScienceCluster`):

```bash
oc apply -k deploy/argocd
```

Wait until the Application CRD exists (GitOps operator installing):

```bash
oc wait --for=condition=established crd/applications.argoproj.io --timeout=20m
oc -n openshift-gitops rollout status deploy/openshift-gitops-server --timeout=20m
```

Then create the AppProject and Applications (plain YAML, not a Job or ConfigMap):

```bash
oc apply -k deploy/argocd/apps
```

Argo still has to pull Gitea and sync. While the repo-server is coming up you may see `ComparisonError` / `connection refused` on port `8081`. That is temporary — wait and refresh.

Keep watching until `mvrca-sandbox` is **Synced** (OpenShift AI continues in the background and can take 15–20 minutes):

```bash
oc get applications.argoproj.io -n openshift-gitops
```

You should see `mvrca-platform`, `mvrca-operators`, `mvrca-gpu-operator`, `mvrca-connectivity-link`, `mvrca-aap`, and `mvrca-sandbox`. Do not go to step 5 until `mvrca-sandbox` is Synced — that is when the `mvrca` BuildConfig exists. NFD + NVIDIA GPU Operator come from `mvrca-gpu-operator` (needed before `llama-31-8b-llmisvc-gpu.yaml` can schedule).

## 5. Build the demo image

Do this after `mvrca-sandbox` is Synced (that creates the BuildConfig).

```bash
oc start-build mvrca -n multivendor-rca --from-dir=. --follow
oc rollout restart deployment -l app.kubernetes.io/part-of=multivendor-rca -n multivendor-rca
```

## 6. Open the UIs (this cluster)

Demo, Gitea, and GitOps are ready after step 5:

```bash
oc get route ran-sandbox -n multivendor-rca -o jsonpath='https://{.spec.host}{"\n"}'
oc get route gitea -n gitea -o jsonpath='https://{.spec.host}{"\n"}'
oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='https://{.spec.host}{"\n"}'
```

OpenShift AI is slower. There is no `rhods-dashboard` Route on RHOAI 3.x. Wait until `mvrca-operators` has created `default-dsc` and the DSC is Ready, then use the data-science gateway:

```bash
oc get applications.argoproj.io mvrca-operators -n openshift-gitops
oc wait --for=jsonpath='{.status.phase}'=Ready dsc/default-dsc --timeout=20m
oc get route data-science-gateway -n openshift-ingress -o jsonpath='https://{.spec.host}{"\n"}'
```

If `dsc/default-dsc` is NotFound, `mvrca-operators` is still installing — wait and retry the `oc wait`. The gateway Route can exist before the dashboard UI is Ready; wait for DSC Ready before opening it.

- **Demo** — no login.
- **Gitea** — Sign In with the username and password from step 2.
- **Argo CD** — Log in with OpenShift. Apps should be Synced / Healthy.
- **RHOAI** — Log in with OpenShift. Model `llama-31-8b` becomes Ready after llm-d starts.

`llama-31-8b-kserve-*` in `Init:0/1` is downloading `RedHatAI/Llama-3.1-8B-Instruct` (several minutes). Permission-denied messages on `/mnt/tmp_*` are harmless. After Init it pulls the vLLM CPU image (~5 GiB), then loads weights. `0/1 Running` with `/health` connection refused is normal until vLLM listens. CPU uses `--enforce-eager` so compile does not overrun the 10-minute startup probe. First Ready is often 8–12 minutes:

```bash
oc get llminferenceservice llama-31-8b -n multivendor-rca
oc get pods -n multivendor-rca | grep llama-31-8b
```

## 7. Governed remediation with Automation Orchestrator and Ansible Automation Platform (optional)

The `mvrca-aap` Argo CD app installs AAP and Automation Orchestrator (AO), then runs two Jobs:

- `aap-bootstrap` creates the AAP project and job templates.
- `ao-workflow-import` imports the workflow into AO as a **draft**.

Some things stay manual because they hold credentials or differ per cluster. Details: [ao-remediation/REQUIREMENTS.md](../ao-remediation/REQUIREMENTS.md).

Work through 7.1 to 7.8 **in order**.

### 7.1 Wait for AAP and AO

```bash
oc get ansibleautomationplatform,automationorchestrator -n aap
oc logs job/aap-bootstrap -n aap | tail -3
```

Get the AO URL and the `admin` password (you need them to log in to AO):

```bash
oc get route automation-orchestrator -n aap -o jsonpath='https://{.spec.host}{"\n"}'
oc get secret automation-orchestrator-initial-admin-password -n aap -o jsonpath='{.data.password}' | base64 -d; echo
```

### 7.2 Attach the AAP subscription

A fresh AAP has no license. Without one, `aap-bootstrap` cannot create the project and job templates.

1. Get the AAP URL and admin password:

   ```bash
   oc get route aap -n aap -o jsonpath='https://{.spec.host}{"\n"}'
   oc get secret aap-admin-password -n aap -o jsonpath='{.data.password}' | base64 -d; echo
   ```

2. Log in to AAP as `admin` and open *Subscription* (*Subscription settings*).
3. Choose **Username and password** and enter your Red Hat credentials.
4. Select a subscription and click **Next**.
5. Accept the analytics terms and click **Submit**.

Re-run the bootstrap Job and watch it finish:

```bash
oc delete job aap-bootstrap -n aap --ignore-not-found
oc apply -f deploy/base/aap/bootstrap-job.yaml
oc logs -f job/aap-bootstrap -n aap
```

### 7.3 Create the AO service account

In the AO UI, go to *System Administration → Access Management → Service Accounts → Create service account*.

- Project: `Default`
- Name: `nep-orchestrator`

Click **Create service account**, then **save the client ID and secret**. The secret is shown only once. You need both in 7.8.

### 7.4 Import the workflow

Sync `mvrca-aap` in Argo CD to run the `ao-workflow-import` Job, then read its log:

```bash
oc logs job/ao-workflow-import -n aap
```

- `created as a draft`: the import worked. In the AO UI open *Workflows* and check that **RAN Timing Fault - Governed Remediation** is listed.
- `SKIPPED`: the service account from 7.3 is missing. Create it, then sync again.

### 7.5 Add the LLM integration

In the AO UI, go to *Configuration → Integrations → Configure Integration* and set:

| Field | Value |
|---|---|
| Integration type | LLM provider |
| Name | any name you like |
| Provider type | Red Hat AI |
| API URL | your OpenAI-compatible LLM URL |

Click **Next**, then create a credential in the **default** project (any name, plus your API key).

### 7.6 Allow the AAP host in AO

Do this **before** adding the AAP integration. It restarts the AO pods. Set it on all three AO deployments, not only the backend:

```bash
oc set env \
  deployment/automation-orchestrator-backend \
  deployment/automation-orchestrator-worker \
  deployment/automation-orchestrator-background-worker \
  -n aap \
  'APP_INTEGRATION_URL_ALLOWED_HOSTS=["aap.aap.svc"]'
```

### 7.7 Add the AAP integration and publish the workflow

**Add the AAP integration.** In the AO UI, go to *Configuration → Integrations → Configure Integration* and set:

| Field | Value |
|---|---|
| Integration type | Ansible Automation Platform |
| Server name/ID | any name you like |
| API URL | `http://aap.aap.svc` (use `.svc`, not `.svc.cluster.local`, which AO blocks) |
| Security | turn **Allow HTTP** on |

Click **Next**, then create a credential in the **default** project:

- Auth method: **basic-auth**
- Username: `admin`
- Password: the output of this command:

  ```bash
  oc get secret aap-admin-password -n aap -o jsonpath='{.data.password}' | base64 -d; echo
  ```

**Wire up the workflow nodes.** Open **RAN Timing Fault - Governed Remediation**. For each node below, open it, choose the values shown, and click **Update**.

| Node | Integration and credential | Job template |
|---|---|---|
| Verdict Explainer | the LLM model and credential from 7.5 | n/a |
| RAN - Heal PTP Timing | the AAP integration and credential above | RAN - Heal PTP Timing |
| Record rejection / open incident | the AAP integration and credential above | RAN - Record Rejection |
| RAN - Close RCA | the AAP integration and credential above | RAN - Close RCA |
| RAN - Escalate | the AAP integration and credential above | RAN - Escalate |

Then click **Publish**.

### 7.8 Tell NEP where AO is

This Secret holds credentials, so it is not in Git. Use the client ID and secret you saved in 7.3:

```bash
oc create secret generic ao-webhook -n multivendor-rca \
  --from-literal=AO_URL="https://$(oc get route automation-orchestrator -n aap -o jsonpath='{.spec.host}')" \
  --from-literal=AO_WEBHOOK_PATH=ran-rca-concluded \
  --from-literal=AO_CLIENT_ID=<client id> \
  --from-literal=AO_CLIENT_SECRET=<client secret>
oc rollout restart deploy/nep-orchestrator -n multivendor-rca
```

**Quick test:** in the demo console, click Start RAN, Inject Fault, then Run RCA. The `Automation.AO.Handoff` span should show `DISPATCHED`, and an execution should be waiting for approval in AO. If it shows `ERROR`, the span's `ao.reason` says why.

## 8. Approve the remediation in the AO UI

Running the 4-plane agentic RCA in the console triggers the workflow in AO automatically.

1. Open the AO UI and go to *Workflow runs*.
2. Open the latest run. It is **waiting for approval**. The Verdict Explainer's brief tells you what failed and what approving will do.
3. Click **Approve**.

After approval, AO runs these AAP job templates in order:

1. **RAN - Heal PTP Timing** heals the PTP fault and unlocks the cell. The console log shows the same lines as the Heal Timing button.
2. **RAN - Close RCA** records the outcome. It does not change anything. It is there to show what a complete workflow looks like in a real environment.

If you **reject** instead, **RAN - Record Rejection** runs and nothing is healed.

## 9. Check

```bash
oc get pods -n multivendor-rca
oc get llminferenceservice -n multivendor-rca
python3 tests/smoke_route.py "https://$(oc get route ran-sandbox -n multivendor-rca -o jsonpath='{.spec.host}')"
```

## Troubleshooting

The defaults used in this repo will deploy on a GPU with a minimum of 24GB of ram. For scenarios with GPUs with less than 24GB of RAM, or no GPU available, use one of the following scenarios:

### GPU with 16GB of RAM

1. edit `/deploy/overlays/multivendor/kustomization.yaml`
2. uncomment the line `  # - llama-31-8b-llmisvc-gpu-small.yaml`
3. comment the line `  - llama-31-8b-llmisvc-gpu.yaml`
4. run `git add /deploy/overlays/multivendor/kustomization.yaml`
5. run `git commit -m "changing gpu settings"`
6. Return to the [Install Argo CD](#4-install-argo-cd) section to complete the install

If you have already deployed the application and you are modifying a running system, be sure to push your changes to the repo:
```bash
GIT_SSL_NO_VERIFY=true git push \
  "https://${GITEA_USER}:${GITEA_PASS_ENC}@${GITEA_HOST}/${GITEA_USER}/multivendor-agentic-rca.git" \
  HEAD:main
```

### CPU Only

This has also been tested using CPU only inference. This is a very slow process, but can be used in worse case scenarios. To enable CPU based inference

1. edit `/deploy/overlays/multivendor/kustomization.yaml`
2. uncomment the line `  # - llama-31-8b-llmisvc-cpu.yaml`
3. comment the line `  - llama-31-8b-llmisvc-gpu.yaml`
4. run `git add /deploy/overlays/multivendor/kustomization.yaml`
5. run `git commit -m "changing gpu settings"`
6. Return to the [Install Argo CD](#4-install-argo-cd) section to complete the install

If you have already deployed the application and you are modifying a running system, be sure to push your changes to the repo:
```bash
GIT_SSL_NO_VERIFY=true git push \
  "https://${GITEA_USER}:${GITEA_PASS_ENC}@${GITEA_HOST}/${GITEA_USER}/multivendor-agentic-rca.git" \
  HEAD:main
```
