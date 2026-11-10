# RAN Timing Fault: Governed Remediation with Automation Orchestrator

The "act" layer of the multivendor agentic RCA lab. NEP's deterministic RCA decides the verdict. Automation Orchestrator (AO) governs and executes it: an LLM agent explains the verdict to a human approver, the approver decides, and AAP heals PTP timing and verifies the result.



## Flow

1. Console **Run RCA** → `nep-orchestrator /nep/rca/trigger`. When the verdict is not `NO_ACTION`, NEP posts the RCA event to the AO webhook (span 7, `Automation.AO.Handoff`).
2. **Verdict Explainer** (task agent, RHOAI llm-d, no tools) writes a plain-language brief from the webhook payload.
3. **Switch** on `${trigger.verdict}`: anything but `NO_ACTION` (that is `HOLD` or `APPLY`) goes to the approval, `NO_ACTION` ends.
4. **Approval**: approved runs `RAN - Heal PTP Timing`, rejected runs `RAN - Record Rejection`.
5. After the heal: wait 10 s, run a verification RCA (`?trigger=ao-verify`, which NEP does not hand back to AO), then `RAN - Close RCA` if the verdict is `NO_ACTION`, otherwise `RAN - Escalate`.


## Files

```
ao/ran-timing-remediation.json      AO workflow definition (schema_version 2.0.0)
aap/playbooks/
  ran_heal_ptp.yml                  POST ran-sandbox /api/ran/heal-ptp, wait for LOCKED + ACTIVE
  ran_close_rca.yml                 Reads the verification RCA and closes the RCA
  ran_escalate.yml                  Fails visibly when the fault is still present after the heal
  ran_record_rejection.yml          Records a rejected remediation
../deploy/base/aap/bootstrap/       Scripts run by GitOps Jobs: create the AAP project, inventory and job templates; import the AO workflow
REQUIREMENTS.md                     Prerequisites, job templates, integrations
```

`ran_heal_ptp.yml` makes the same call as the console's **Heal Timing** button, so the console log shows the same `PTP-BRIDGE` and `O1` lines.

## Assumption

The heal runs straight after approval and assumes the fault is still present. The demo guarantees that (the injected PTP fault does not clear by itself). Outside the demo, a stale approval could heal a healthy cell.

## Setup

GitOps (`mvrca-aap`) installs the AAP and AO operators and instances, creates the AAP project and job templates, and imports the workflow into AO as a draft. Still manual, in the AO UI: the service account (named `nep-orchestrator`, created before the import), the LLM and AAP integrations, the internal-URL allowlist, selecting the LLM on the workflow and publishing it, and the `ao-webhook` Secret for NEP. Details in [REQUIREMENTS.md](REQUIREMENTS.md).


