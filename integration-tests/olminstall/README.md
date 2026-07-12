# olminstall Integration Test Scenario

End-to-end Konflux integration test for [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh)/[RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) operator installation via [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm). Provisions an ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) cluster using Konflux [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) ([provisioning docs](https://konflux.pages.redhat.com/docs/users/testing/cluster-provisioning.html#methods)), installs the operator from the [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) catalog image in the Konflux [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), verifies the [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) reaches `Succeeded`, then runs [BVT](../../doc/contributing-konflux-testing-rhoai.md#bvt) (`opendatahub-tests` `cluster_health` and `operator_health` markers).

**Operational runbook** (EaaS vs external kubeconfig, tenant secrets, `maas_billing` prereqs, trigger/watch): [contributing guide](../../doc/contributing-konflux-testing-rhoai.md) and [Triggering](#triggering) below. This README focuses on pipeline architecture and in-tree files.

**Terms and abbreviations:** [BVT](../../doc/contributing-konflux-testing-rhoai.md#bvt), [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv), [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas), [FBC / FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf), [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift), [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms), [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm), [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), [full glossary](../../doc/contributing-konflux-testing-rhoai.md#terms-and-abbreviations) ([DBus](../../doc/contributing-konflux-testing-rhoai.md#dbus), [DSC](../../doc/contributing-konflux-testing-rhoai.md#dsc), [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco), [MCO](../../doc/contributing-konflux-testing-rhoai.md#mco), …).

## Test layers

| Layer | Path / entry | Runs on | Purpose |
|-------|----------------|---------|---------|
| **Konflux integration** | `integration-tests/olminstall/` (Tekton pipeline, `olm_pipeline.py`) | Live cluster (EaaS or external kubeconfig) | Operator install, BVT, per-component pytest gates |
| **Pipeline unit tests** | `unit_tests/` (`pytest.ini` → `testpaths = unit_tests`) | Developer machine / CI (no `oc`) | CLI parsing, Tekton step logic, catalog/plan helpers |
| **opendatahub-tests** | Cloned at runtime into test image | Cluster under test | Actual product pytest (`tests/workbenches/`, …) |

**Naming:** repo directories use **kebab-case** (`integration-tests/`); importable Python packages use **snake_case** (`unit_tests/`, `steps.*`, `runners.*`, `suite.*`). Do not rename `integration-tests/` to match pytest style - the names refer to different layers.

## Directory layout

| Path | Role |
|------|------|
| [`tekton/`](tekton/) | Tekton pipelines, tasks, ITS, shell scripts (`python -m steps.*` / `runners.*` from YAML) |
| [`steps/`](steps/) | Tekton step entrypoints (`write_pipeline_test_flags`, `summarize_test_output`, diagnostics, …) |
| [`runners/`](runners/) | Test orchestration (BVT, per-component pytest/golang/Cypress) and [`runners/cli/`](runners/cli/) for `olm_pipeline.py` |
| [`runners/report/`](runners/report/) | JUnit aggregation, Slack, PipelineRun summary, artifact URLs |
| [`install/`](install/) | OLM install, pull secret, dependency operators, DSC policy |
| [`suite/`](suite/) | Catalog, plan, phases, constants, tests-config parsing |
| [`k8s/`](k8s/) | `oc` helpers, external kubeconfig, cluster probes |
| [`components/`](components/) | Component-specific prereqs (e.g. `maas_billing/`) |
| [`config/`](config/) | Phase config, smoke catalog, DSC install map, example Snapshot/PipelineRun |

**Test artifacts:** JUnit and logs land in **`tests-payload/results/`** on the Tekton `tests-shared` workspace (created at runtime, not a source tree directory).

## Pipeline flow

```mermaid
flowchart TD
    classDef trigger fill:#3B82F6,stroke:#1D4ED8,color:#fff,font-weight:bold
    classDef infra   fill:#F97316,stroke:#C2410C,color:#fff,font-weight:bold
    classDef auth    fill:#8B5CF6,stroke:#5B21B6,color:#fff,font-weight:bold
    classDef hcco    fill:#06B6D4,stroke:#0E7490,color:#fff,font-weight:bold
    classDef olm     fill:#10B981,stroke:#065F46,color:#fff,font-weight:bold
    classDef pass    fill:#22C55E,stroke:#15803D,color:#fff,font-weight:bold
    classDef fail    fill:#EF4444,stroke:#B91C1C,color:#fff,font-weight:bold

    BUILD["🏗️ Snapshot ready → ITS creates PipelineRun"]:::trigger
    CLUSTER["☁️ Ephemeral HyperShift cluster (latest supported OCP version) + IDMS mirror"]:::infra
    AUTH["🔐 Three-level credential setup"]:::auth
    HCCO["🤖 HCCO syncs kubelet creds to all nodes"]:::hcco
    OLM["📦 OLM: CatalogSource + Subscription + bundle-unpack + CSV"]:::olm
    PASS["✅ CSV Succeeded - operator version recorded"]:::pass
    BVT_RESOLVE["🔎 Resolve opendatahub-tests image tag from CSV"]:::olm
    BVT_RUN["🧪 BVT pytest cluster_health + operator_health"]:::pass
    ALLPASS["✅ Install and BVT passed"]:::pass
    FAIL["❌ Failed  -  pod logs + RHOAI CR diagnostics"]:::fail

    BUILD -->|~20 min to provision| CLUSTER
    CLUSTER --> AUTH
    AUTH -.->|HCCO detects additional-pull-secret| HCCO
    AUTH -->|rhoai-quay-pull linked to SA| OLM
    HCCO -->|nodes synced before Subscription| OLM
    OLM --> PASS
    PASS --> BVT_RESOLVE
    BVT_RESOLVE --> BVT_RUN
    BVT_RUN --> ALLPASS
    OLM -.->|timeout / error| FAIL
    BVT_RESOLVE -.->|resolve failure| FAIL
    BVT_RUN -.->|pytest failure| FAIL
```

The `BUILD` node is the entry point for both **automatic** and **manual** runs (see [Triggering](#triggering) and the [contributing guide](../../doc/contributing-konflux-testing-rhoai.md)).

## What it does

1. **parse-pipeline-tests** - runs first; shallow-clones `SCRIPTS_REPO_*` into **`tests-shared/scripts-repo`** (once per PipelineRun), then runs [`steps/write_pipeline_test_flags.py`](steps/write_pipeline_test_flags.py) with params **`TESTS`** (default `bvt,smoke`), **`COMPONENTS`**, [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml), and [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml) to set Tekton results (`RUN_SMOKE`, `RUN_BVT`, `RUN_MINIMAL_DEPS`, `RUN_OPENDATAHUB_TESTS`, …). Downstream tasks read the same checkout from the PVC.
2. **extract-fbcf-image** - **`runAfter`** **`parse-pipeline-tests`**; reads scripts from **`tests-shared/scripts-repo`**. Extracts the [FBC](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) `containerImage` for `RHOAI_FBC_NAME` from the Konflux [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) (or writes `n/a` when **`PRODUCT=existing`**).
3. **provision-eaas-space** - reserves an [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) environment using the `provision-eaas-space` step action from [konflux-ci/build-definitions](https://github.com/konflux-ci/build-definitions) (`main`).
4. **install-ocp-cluster** - queries EaaS for supported versions via `konflux-ci/build-definitions` step actions, writes the version prefix with [`steps/resolve_ocp_prefix.py`](steps/resolve_ocp_prefix.py) (scripts from **`tests-shared/scripts-repo`**), resolves the latest patch, then installs an ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) OCP cluster (AWS, `m5.2xlarge` by default) via `eaas-create-ephemeral-cluster-hypershift-aws/0.1` with [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) `imageContentSources`: `registry.redhat.io/rhoai` → `quay.io/rhoai`. Stages ephemeral kubeconfig into **`tests-shared`** for **`install-dep-operators`** and pytest.
5. **install-dep-operators** _(when **`RUN_INSTALL_DEP_OPERATORS=true`** from `parse-pipeline-tests`)_ - unified EaaS + external task ([`tekton/tasks/task-install-dep-operators.yaml`](tekton/tasks/task-install-dep-operators.yaml)); on EaaS copies kubeconfig from **`tests-shared`** (staged by **`install-ocp-cluster`**), on external fetches from the tenant secret. Scripts from **`tests-shared/scripts-repo`**; clones **`OLMINSTALL_REPO_*`** into the task pod. Runs olminstall **`setup-dependencies.sh`** (`-M` by default), pins RHCL CSV from olminstall manifest, runs **`post-install-rhcl-operator.sh`**, then Serverless/Llama Stack sidecars when selected. With **`INSTALL_DEPENDENCIES=true`** (`--install-dependencies` on **`--product existing`**), also runs **`prepare-component-cluster`** (DSC, MaaS gateway, LDAP, dashboard route, …) in the same task. **Fails** when MaaS/smoke-selected deps cannot be recovered; on **product install** without MaaS smoke, partial dependency issues **warn and succeed** so **`install-rhoai`** / **`install-odh`** can run and **`verify-operator-ready`** gates dashboard readiness.
6. **install-operator** - unified EaaS + external task (`install-rhoai` / `install-odh`); **`runAfter`** **`install-dep-operators`** (skipped when **`RUN_INSTALL_DEP_OPERATORS=false`**). Scripts from **`tests-shared/scripts-repo`**; clones **`OLMINSTALL_REPO_*`**; patches pull secret, runs [`install/install_and_verify.py`](install/install_and_verify.py).
7. **verify-operator-ready** - Jenkins **`verifyDashboardRoute`** parity ([`tekton/tasks/task-verify-operator-ready.yaml`](tekton/tasks/task-verify-operator-ready.yaml)): wait for **all cluster Deployments** (`oc wait` parallel, 3 min), **`DashboardReady`**, and gateway HTTP preflight after **`install-rhoai`** / **`install-odh`** (whichever ran) or **`external-cluster-ready`**. Always runs when a workload cluster is available; no **`TEST_GATES`** / **`RUN_OPENDATAHUB_TESTS`** gate. Skips cleanly for **`PRODUCT=existing`** with no kubeconfig (snapshot-only). Stages **`odh-dashboard-url.txt`** for component prepare.
8. **opendatahub-tests-prepare** _(when `RUN_OPENDATAHUB_TESTS=true`)_ - fetch and stage kubeconfig (EaaS or external), `opendatahub-tests` image resolve; when **`RUN_COMPONENT_TESTS`** (smoke and/or tier1): component plan export and, unless **`RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS=true`**, cluster prereqs in **`prepare-components-prerequisites-*`** (reuses dashboard URL from **`verify-operator-ready`** when present). Scripts from **`tests-shared/scripts-repo`**. **`onError: continue`**.
9. **bvt-health-checks** _(when `RUN_BVT=true`)_ - `cluster_health` / `operator_health` only (not per-component). **`runAfter`** **`opendatahub-tests-prepare`**; must pass before component smoke (**no** `onError: continue`).
10. **test-<component>*** _(when `RUN_COMPONENT_TESTS=true`)_ - **one pipeline task per component** (separate Konflux DAG node each). **`runAfter`** serial catalog order: first selected smoke waits on **`opendatahub-tests-prepare`** + **`bvt-health-checks`**; each later catalog task waits on the previous one. With a **`COMPONENTS`** subset, unselected tasks are skipped via **`when:`** and do not block selected smokes. Konflux still lists every catalog **`test-*`** node; unselected ones appear grey/skipped. Each task runs **one pytest session** for all selected component phases (`smoke`, `tier1`, or both) via combined `-m` markers from the catalog, not separate Tekton cycles per phase. **`onError: continue`** so one failed component does not block the next. **`test-finalize`** **`runAfter`** the last catalog task only (also **`onError: continue`**); emits aggregate **`TEST_OUTPUT`**; **`publish-results`** uploads **`tests-payload/`** once.
11. **`publish-results`** - `finally` task (parallel with **`collect-diagnostics`**): OCI upload, Konflux UI summary, Slack, and pipeline `TEST_OUTPUT`.
12. **collect-diagnostics** _(when the target cluster was reachable)_ - RHOAI triage (status, events, since-window logs, issues summary), DSC/OLM dumps.

### Konflux UI: PipelineRun results, task logs, and files on the step pod

- **PipelineRun / TaskRun logs:** Open your application → **Pipeline runs** (or activity / pipelines for your tenant), select the run, then open individual **tasks** / **steps** to stream logs ([Konflux user flows vary slightly by deployment](https://konflux-ci.dev/docs/getting-started/)).
- **Tekton results surfaced in the UI:** Integration pipelines often expose standard **`TEST_OUTPUT`** and similar task results; Konflux [documents](https://konflux-ci.dev/docs/testing/integration/standardized-outputs/) that task-level results appear when you **click the task name** and inspect the details panel.
- **Pipeline-level results:** The **`PipelineRun` → Results / Summary** area shows values wired under `spec.results` (when emitted); some references are omitted when the source task was skipped.
- **publish-results task Results (Konflux):** Open **publish-results** → **Details** → **Results** for **`TEST_OUTPUT`**, **`ARTIFACTS_URL`**, **`RUN_SUMMARY`**, **`CLUSTER`**, and **`OPERATOR_VERSION`**.
- **JUnit in the shared artifact browser:** After tests complete, **`publish-results`** pushes matching JUnit and logs from **`tests-payload/results/`** to **`quay.io/opendatahub/odh-ci-artifacts`** under **`…/<PipelineRun>/test-payload-results/`** (patterns in [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml) **`artifactUpload`**). Tool binaries such as staged **`oc`** are not uploaded.

- **Large files on the pod filesystem:** Files written only inside the pod (**`/artifacts/*.xml`**, etc.) are **not automatically downloadable as blobs from the Konflux UI** unless a task publishes them as a Tekton **result** (size-limited) or copies them to external storage. **`collect-diagnostics`** publishes **`<product>-diagnostic-<datetime>.log`** via **`tests-payload/results/`** and OCI upload. In practice you rely on **step logs**, the artifact browser, or copy from the pod while it still exists. **Uncertainty:** Red Hat hosted Konflux builds evolve  -  confirm the exact panel labels (**Pipeline run**, **Task runs**, **Results**) in your tenant.

## Files

| File | Purpose |
|------|---------|
| [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml) | Tekton pipeline: setup → cluster/install → prepare → test gates → `finally` reporting |
| [`tekton/tasks/task-bvt-health-checks.yaml`](tekton/tasks/task-bvt-health-checks.yaml) | BVT `cluster_health` + `operator_health` pytest (or placeholder when `PRODUCT=existing`) |
| [`tekton/tasks/task-component-pytest.yaml`](tekton/tasks/task-component-pytest.yaml) | One component pytest step (`test-*` pipeline tasks; `component-test-plan.json`) |
| [`tekton/tasks/task-test-finalize.yaml`](tekton/tasks/task-test-finalize.yaml) | Aggregate smoke `TEST_OUTPUT` and component pytest exit check |
| [`tekton/tasks/task-install-operator.yaml`](tekton/tasks/task-install-operator.yaml) | Reusable Tekton Task for OLM install + CSV verify (`install-rhoai` / `install-odh`) |
| [`tekton/tasks/task-install-dep-operators.yaml`](tekton/tasks/task-install-dep-operators.yaml) | Reusable Tekton Task: `setup-dependencies.sh`, RHCL CSV pin + `post-install-rhcl-operator.sh` on EaaS or external clusters (`install-dep-operators`; fails pipeline on error) |
| [`install/rhcl_deps.py`](install/rhcl_deps.py) | RHCL CSV pin from olminstall manifest, `post-install-rhcl-operator.sh`, Authorino readiness (`ensure_maas_rhcl_dependency_stack`) |
| [`install/olminstall_checkout.py`](install/olminstall_checkout.py) | Shared olminstall clone path resolution for install and component prep |
| [`tekton/tasks/task-verify-operator-ready.yaml`](tekton/tasks/task-verify-operator-ready.yaml) | Post-install dashboard readiness gate (Jenkins `verifyDashboardRoute`); stages `odh-dashboard-url.txt` |
| [`runners/verify_operator_ready.py`](runners/verify_operator_ready.py) | Python entry for **`verify-operator-ready`** (dashboard wait + gateway curl) |
| [`unit_tests/suite/test_pipeline_catalog_consistency.py`](unit_tests/suite/test_pipeline_catalog_consistency.py) | Drift test: validates every catalog component has a matching `test-*` pipeline task and `RUN_SMOKE_*` result |
| [`config/olminstall-pipeline-snippets.yaml`](config/olminstall-pipeline-snippets.yaml) | Pattern reference for common step scripts (not a full pipeline mirror; see [Pipeline snippets](#pipeline-snippets-configolminstall-pipeline-snippetsyaml)) |

The canonical runnable pipeline is [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml). Konflux Tekton does **not** support nested `pipelineRef` pipelines; reusable **tasks** live under [`tekton/tasks/`](tekton/tasks/) and are referenced from the pipeline. Component tests run as a **chain of `test-*` pipeline tasks** (one DAG node per component) sharing the **`tests-shared`** workspace.

| [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml) | Declarative **phases** (ids, defaults, Tekton `RUN_*` mapping); read by `olm_pipeline.py` and by `parse-pipeline-tests` after cloning `SCRIPTS_REPO` |
| [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml) | Per-component catalog (`smoke` + `tier1` markers in `qualityGatesMap.default`); used when component phases are in **`TEST_GATES`** |
| [`steps/tekton_util.py`](steps/tekton_util.py) | Shared library: `require_env`, `write_result`, `git_clone` (with optional RH internal TLS workaround), `run`, `parse_junit_summary` |
| [`steps/resolve_ocp_prefix.py`](steps/resolve_ocp_prefix.py) | Tekton step: derive `OCP_VERSION_PREFIX` / default-minor prefix string for EaaS `pick-version` |
| [`steps/extract_fbcf_image.py`](steps/extract_fbcf_image.py) | Tekton step: extract FBCF container image from a Konflux `ApplicationSnapshot` JSON |
| [`steps/resolve_opendatahub_tests_image.py`](steps/resolve_opendatahub_tests_image.py) | Tekton step: maps installed CSV version to `opendatahub-tests` image tag (`skopeo` probe, `:latest` fallback) |
| [`runners/run_bvt_pytest.py`](runners/run_bvt_pytest.py) | BVT pytest runner: single marker via env, or `BVT_SUITE=health` for cluster + operator |
| [`steps/summarize_test_output.py`](steps/summarize_test_output.py) | Tekton step: parse JUnit XML files and write a Konflux-standardised `TEST_OUTPUT` result |
| [`steps/collect_diagnostics.py`](steps/collect_diagnostics.py) | Tekton step: RHOAI triage + DSC/DSCi; OLM dumps when install ran; optional `oc adm inspect` on install or pipeline failure |
| [`steps/rhoai_triage.py`](steps/rhoai_triage.py) | Status report, events, operator highlights, since-window pod logs, issues summary |
| [`runners/report/send_notification.py`](runners/report/send_notification.py) | Tekton step: Slack notification summarising pipeline run results |
| [`install/patch_cluster_pull_secret.py`](install/patch_cluster_pull_secret.py) | Tekton step: injects `quay.io/rhoai` credentials into the [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) cluster at all required levels |
| [`tekton/its/its-olminstall-open-data-hub-tenant.yaml`](tekton/its/its-olminstall-open-data-hub-tenant.yaml) | [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) `odh-olminstall` for [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) (`open-data-hub-tenant`, `odh-operator-catalog` component) |
| [`tekton/its/its-olminstall-testops-eaas.yaml`](tekton/its/its-olminstall-testops-eaas.yaml) | [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) `odh-olminstall-testops-eaas` — EaaS sandbox (`CLUSTER_SOURCE=EAAS`, `component_rhoai-fbc-fragment-ocp-421`) |
| [`tekton/its/its-olminstall-testops-rh-nightly.yaml`](tekton/its/its-olminstall-testops-rh-nightly.yaml) | Auto [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) on **`rhoai-fbc-fragment-ocp-420`** for external rh-nightly cluster (`optional: true`) |
| [`config/test-snapshot-rh-nightly.yaml`](config/test-snapshot-rh-nightly.yaml) | Offline FBC pin for `--run-now` when Konflux lookup fails (rh-nightly ITS) |
| [`suite/its_registry.py`](suite/its_registry.py) | Resolve in-tree ITS YAML by `metadata.name` for `--enable-its` / `--disable-its` |
| [`suite/tests_plan.py`](suite/tests_plan.py) | Validates/normalizes `TESTS` strings using [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml) (or `--tests-config`) |
| [`install/install_and_verify.py`](install/install_and_verify.py) | Tekton step: creates [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) resources, waits for [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) `Succeeded`, writes `INSTALL_STATUS` |
| [`olm_pipeline.py`](olm_pipeline.py) | Local CLI — apply/patch ITS for triggers, **`--enable-its`** / **`--disable-its`** for in-tree manifests, create a [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun), stream logs. Default **`--product existing`** injects **`PRODUCT=existing`** (skips EaaS/install); use **`--install-dependencies`** with external kubeconfig for cluster prep. **`--product rhoai`** / **`odh`** runs full install. **`TESTS`** is independent of product mode. |
| [`requirements.txt`](requirements.txt) | Python deps for unit tests (`pytest`, `pyyaml`, …); install via `make deps` or `pip install -r` |
| [`Makefile`](Makefile) | `make test`, `make test-cli`, `make deps` (local unit tests, no `oc`) |
| [`config/test-snapshot.yaml`](config/test-snapshot.yaml) | Example [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) for manual pipeline trigger |
| [`runners/report/prune_stale_testops_its.py`](runners/report/prune_stale_testops_its.py) | Optional: `oc delete` legacy `IntegrationTestScenario` names before raw `oc create -f config/test-snapshot.yaml` (same list as trigger-time prune) |
| [`config/test-pipelinerun.yaml`](config/test-pipelinerun.yaml) | Example [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) for local/manual execution |

### Pipeline snippets (`config/olminstall-pipeline-snippets.yaml`)

Konflux Tekton does **not** support nested `pipelineRef` pipelines. The runnable pipeline is [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml); shared tasks are under [`tekton/tasks/`](tekton/tasks/) (BVT, component pytest, finalize, install-operator).

[`config/olminstall-pipeline-snippets.yaml`](config/olminstall-pipeline-snippets.yaml) documents **recurring step patterns** (clone scripts repo, `python -m steps.<module>` or `python -m runners.<module>`, kubeconfig staging). It is not a full extract of the pipeline. When you change task behavior, update the monolithic pipeline and any referenced `tekton/tasks/*.yaml` first; refresh snippets only when the pattern itself changes.

| Pattern | Purpose |
|---------|---------|
| `clone-scripts-repo` | Shallow git fetch of `SCRIPTS_REPO_*` into **`tests-shared/scripts-repo`** (once in **`parse-pipeline-tests`** only; downstream tasks mount the PVC) |
| `run-olminstall-helper` | `python -m steps.<module>` or `python -m runners.<module>` from `OLMINSTALL_DIR` (e.g. `runners.run_component_pytest`) |
| `prepare-kubeconfig-eaas` / `prepare-kubeconfig-external` | Stage `/credentials/kubeconfig` |
| `verify-operator-ready` | Dashboard deployments + gateway HTTP preflight after install |
| `pipeline-run-summary-step` | Dispatch `steps.pipeline_run_summary_steps` |
| `write-konflux-task-summary-finally` | Per-task `TASK_MESSAGE` via `tekton/scripts/run_write_task_message.sh` (standalone Tasks use `finally:`; inline pipeline `taskSpec`s use a last step  -  Konflux rejects nested `taskSpec.finally`) |

ITS objects ([`its-olminstall-testops-eaas.yaml`](tekton/its/its-olminstall-testops-eaas.yaml), [`its-olminstall-open-data-hub-tenant.yaml`](tekton/its/its-olminstall-open-data-hub-tenant.yaml)) point at the monolithic pipeline via `resolverRef`, not at snippets.

### Konflux UI graph

- **PipelineRun DAG**  -  phase overview (setup → cluster/install → prepare → test gates → report). **`SCRIPTS_REPO`** is cloned once in **`parse-pipeline-tests`** into **`tests-shared/scripts-repo`**; other tasks read that PVC path. Expected shape:

```mermaid
flowchart TD
  subgraph setup["Setup"]
    parse["parse-pipeline-tests<br/><small>clone → tests-shared/scripts-repo</small>"]
    extract["extract-fbcf-image"]
    parse --> extract
  end

  subgraph eaas["EaaS  -  no external kubeconfig, PRODUCT ≠ existing"]
    eaasProv["provision-eaas-space"]
    ocp["install-ocp-cluster<br/><small>stages kubeconfig → tests-shared</small>"]
    eaasProv --> ocp
  end

  subgraph ext["External  -  CLUSTER_SOURCE is a tenant Secret name"]
    extReady["external-cluster-ready"]
    cleanup["cleanup-external<br/><small>optional</small>"]
    extReady --> cleanup
  end

  subgraph install["Install  -  when applicable"]
    deps["install-dep-operators<br/><small>EaaS + external</small>"]
    op["install-rhoai / install-odh"]
    verify["verify-operator-ready<br/><small>dashboard + gateway</small>"]
    deps --> op --> verify
  end

  subgraph tests["Tests  -  RUN_OPENDATAHUB_TESTS"]
    prepare["opendatahub-tests-prepare"]
    bvt["bvt-health-checks<br/><small>RUN_BVT; blocks smoke on fail</small>"]
    smoke1["test-workbenches"]
    smokeN["test-… → test-platform"]
    finalize["test-finalize"]
    verify --> prepare
    prepare --> bvt
    bvt --> smoke1 --> smokeN --> finalize
  end

  subgraph finally["Finally (parallel)"]
    diag["collect-diagnostics"]
    publish["publish-results<br/><small>OCI + UI summary + Slack</small>"]
  end

  extract --> eaasProv
  extract --> extReady
  parse --> extReady
  parse --> deps
  parse --> op
  ocp --> deps
  extReady --> deps
  cleanup --> deps
  ocp --> op
  extReady --> op
  cleanup --> op
  deps --> op
  ocp --> verify
  extReady --> verify
  cleanup --> verify
  finalize --> finally
```

- **Task runs** tab  -  each **`test-<component>`** is its own DAG node; unselected components still appear as skipped nodes when **`COMPONENTS`** is a subset. Inside that TaskRun, **`smoke`** and **`tier1`** (when both in `TEST_GATES`) run in **one pytest session** (`-m 'smoke or tier1'`), not as separate pipeline tasks. **`bvt-health-checks`** has no `onError: continue`  -  a BVT failure blocks all **`test-*`** tasks. Each **`test-*`** has **`onError: continue`**  -  one component failure does not stop the serial chain. **`test-finalize`** waits on the last catalog **`test-*`** task and emits aggregate **`TEST_OUTPUT`**; **`publish-results`** uploads **`tests-payload/`** to OCI.
- **publish-results → Results**  -  aggregated `TEST_OUTPUT` / `ARTIFACTS_URL` / `RUN_SUMMARY` / `CLUSTER` for pass/fail review without reading the full graph. Step **`patch-summary-annotations`** prints the human-readable run summary in the task log.
- Skipped tasks in the DAG (yellow/grey) are normal: mutually exclusive `when:` branches for EaaS vs external vs `PRODUCT=existing`.

## Tenant and application

[`its-olminstall-open-data-hub-tenant.yaml`](tekton/its/its-olminstall-open-data-hub-tenant.yaml) targets **`open-data-hub-tenant`**, application **`opendatahub-builds`**, context `component_odh-operator-catalog`, triggering on [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) builds.

[`its-olminstall-testops-eaas.yaml`](tekton/its/its-olminstall-testops-eaas.yaml) targets **`rhoai-tenant`**, application **`testops-playpen`**, used for development iteration and sandbox testing of the [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) FBC fragment builds (OCP 4.21 / EaaS).

**Why extra PipelineRuns appear:** A Konflux [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) for an **Application** starts **one `PipelineRun` per `IntegrationTestScenario`** that matches that app. Old scenarios still **on the cluster** (for example `rhoai-test` → `testops-e2e-test`) are **not** removed when you update git; they keep firing until deleted. **`testops-playpen-enterprise-contract-*`** runs are **Enterprise Contract** policy checks  -  separate from olminstall; tune or disable them in Konflux application / release / EC settings for your tenant, not via `tekton/pipelines/olminstall-pipeline.yaml`.

**`olm_pipeline.py` default:** For default **`--konflux-namespace rhoai-tenant`** and **`--konflux-app testops-playpen`**, before triggering the CLI runs **`oc delete integrationtestscenario`** for legacy names (`rhoai-test`, `testops-playpen-enterprise-contract`, …  -  see `STALE_TESTOPS_PLAYPEN_ITS_NAMES` in [`suite/constants.py`](suite/constants.py)) and applies the olminstall ITS. Trigger mode creates a **PipelineRun directly** (no Snapshot); component-build Snapshots still start runs via Integration Service. Raw **`oc create -f test-snapshot.yaml`** uses the Snapshot → ITS path instead  -  see [`config/test-snapshot.yaml`](config/test-snapshot.yaml).

> **PipelineRun naming:** **`olm_pipeline.py`** CLI-direct runs use `generateName` `olminstall-cli-{user}-…` (e.g. `olminstall-cli-nmanos-bvt-smoke-7kx2p`). Integration Service runs use the ITS pipelinerun template (rh-nightly: `olminstall-its-rh-nightly-pm-bvt-smoke-*`). Rerun a manual run via **`olminstall.trigger-command`** or repeat the same CLI flags.

The pipeline also needs a tenant secret with quay credentials. Each ITS sets `QUAY_PULL_SECRET_NAME`:
- `its-olminstall-open-data-hub-tenant.yaml` uses `odh-quay-secret`
- `its-olminstall-testops-eaas.yaml` uses `rhoai-quay-secret`

Channel defaults:
- `its-olminstall-open-data-hub-tenant.yaml` sets `UPDATE_CHANNEL=odh-stable` for Konflux auto-triggered [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) runs
- `python3 …/olm_pipeline.py --product odh` auto-selects `odh-stable` unless `--channel` is explicitly provided

## Auth strategy for IDMS mirrors

The [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) operator bundle images are referenced in the [FBC](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) as `registry.redhat.io/rhoai/odh-operator-bundle@sha256:...` but are only accessible at `quay.io/rhoai/`. The pipeline configures an [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror at cluster provisioning to redirect `registry.redhat.io/rhoai` → `quay.io/rhoai`.

However, [OLM's](../../doc/contributing-konflux-testing-rhoai.md#olm) bundle-unpack job runs on a worker node via [CRI-O](../../doc/contributing-konflux-testing-rhoai.md#cri-o), and [CRI-O](../../doc/contributing-konflux-testing-rhoai.md#cri-o) < 1.34 (OCP ≤ 4.20) has a known bug ([cri-o/cri-o#4941](https://github.com/cri-o/cri-o/issues/4941)): **pod-level `imagePullSecrets` are not forwarded to [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror registry pulls**. OpenShift documentation explicitly states that for [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror registries, only the cluster-wide global pull secret is supported  -  not project or pod pull secrets.

In a standard cluster, updating the global pull secret propagates via the Machine Config Operator ([MCO](../../doc/contributing-konflux-testing-rhoai.md#mco)). In [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift), [MCO](../../doc/contributing-konflux-testing-rhoai.md#mco) changes trigger **node replacement** (not in-place update), which takes 15-30 minutes  -  too slow for an ephemeral integration test.

**Solution:** `patch_cluster_pull_secret.py` creates a secret named `additional-pull-secret` in `kube-system`. [HyperShift's](../../doc/contributing-konflux-testing-rhoai.md#hypershift) **Hosted Cluster Config Operator ([HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco))** automatically detects this secret and deploys a `global-pull-secret-syncer` DaemonSet in `kube-system` that:
- Merges credentials into `/var/lib/kubelet/config.json` on each node
- Restarts kubelet via systemd [DBus](../../doc/contributing-konflux-testing-rhoai.md#dbus)

This is the **official [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) mechanism** for propagating pull-secret changes without node replacement. `install_and_verify.py` waits for the syncer to complete on all nodes before creating the Subscription.

> **Note:** Use namespace-specific credential keys (e.g. `quay.io/rhoai`) rather than bare `quay.io` in `additional-pull-secret`. [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco) applies original-pull-secret entries with higher precedence on conflict, so namespace-specific keys avoid being overridden.

## Triggering

- **Automatic (Konflux CI):** New [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) → matching [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) → [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun). Example ITS: [`its-olminstall-open-data-hub-tenant.yaml`](tekton/its/its-olminstall-open-data-hub-tenant.yaml), [`its-olminstall-testops-eaas.yaml`](tekton/its/its-olminstall-testops-eaas.yaml), [`its-olminstall-testops-rh-nightly.yaml`](tekton/its/its-olminstall-testops-rh-nightly.yaml).
- **Manual (CLI):** [`olm_pipeline.py`](olm_pipeline.py) applies or overrides the sandbox [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), resolves an image when needed, creates a [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) directly (or via Snapshot when using raw `oc create -f`), and streams logs.
- **Manual (`oc` only):** After [logging in](../../doc/contributing-konflux-testing-rhoai.md#log-in-and-pick-a-namespace) to the tenant namespace, apply an [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), then create a [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) (pinned file or latest image for your app label). Example for the [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) sandbox [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) and `rhoai-fbc-fragment-ocp-421`:

```bash
oc apply -n rhoai-tenant -f integration-tests/olminstall/tekton/its/its-olminstall-testops-eaas.yaml
oc create -n rhoai-tenant -f integration-tests/olminstall/config/test-snapshot.yaml
# Or substitute the latest snapshot image (adjust -l / jsonpath for your application):
LATEST=$(oc get snapshots -n rhoai-tenant \
  --sort-by=.metadata.creationTimestamp \
  -l appstudio.openshift.io/application=rhoai-fbc-fragment-ocp-421 \
  -o jsonpath='{.items[-1].spec.components[0].containerImage}')
sed "s|containerImage:.*|containerImage: $LATEST|" \
  integration-tests/olminstall/config/test-snapshot.yaml | oc create -n rhoai-tenant -f -
oc get pipelinerun -n rhoai-tenant
python3 integration-tests/olminstall/olm_pipeline.py -w --konflux-namespace rhoai-tenant --konflux-app testops-playpen
```

For generic Konflux testing (login, namespaces, [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) vs [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot)/[ITS](../../doc/contributing-konflux-testing-rhoai.md#its); [manual vs ITS vs PAC](../../doc/contributing-konflux-testing-rhoai.md#how-integration-runs-are-triggered)), see [contributing guide](../../doc/contributing-konflux-testing-rhoai.md#terms-and-abbreviations).

### IntegrationTestScenario admin (`--enable-its` / `--disable-its` / `--run-now`)

Rh-nightly and EaaS olminstall ITS live on the **FBC fragment Applications** (native Konflux auto-trigger on component build Snapshots), not on `testops-playpen`.

| ITS name | Konflux Application | Cluster | `CLUSTER_SOURCE` | Auto-trigger context | ITS PipelineRun prefix |
|----------|---------------------|---------|------------------|----------------------|-------------------------|
| `odh-olminstall-testops-rh-nightly` | **`rhoai-fbc-fragment-ocp-420`** | rh-nightly-pm (external) | `olminstall-kubeconfig-rh-nightly-pm` | `component_rhoai-fbc-fragment-ocp-420` | `olminstall-its-rh-nightly-pm-bvt-smoke-*` |
| `odh-olminstall-testops-eaas` | `testops-playpen` (→ **421** later) | EaaS (ephemeral) | `EAAS` | `component_rhoai-fbc-fragment-ocp-421` | `olminstall-its-eaas-bvt-smoke-*` |

Use [`olm_pipeline.py`](olm_pipeline.py) to apply or remove in-tree [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) manifests by `metadata.name`. **`--enable-its`** applies the ITS only (launch-and-forget). **`--run-now`** creates a direct CLI PipelineRun (`olminstall-cli-*`, Konflux **Incoming**) without applying ITS.

**Steady state (rh-nightly on ocp-420 FBC app):**

```bash
python3 integration-tests/olminstall/olm_pipeline.py \
  --enable-its odh-olminstall-testops-rh-nightly \
  --konflux-repo https://github.com/<you>/odh-konflux-central.git \
  --konflux-branch olminstall_smoke \
  --konflux-namespace rhoai-tenant --konflux-app rhoai-fbc-fragment-ocp-420
python3 integration-tests/olminstall/olm_pipeline.py \
  --enable-its odh-olminstall-testops-eaas \
  --konflux-namespace rhoai-tenant --konflux-app testops-playpen
```

Tenant secret **`olminstall-kubeconfig-rh-nightly-pm`** must exist before rh-nightly runs. Both ITS use `optional: true` so failures do not block FBC release.

**Autonomous external login (RHOAIENG-57718):** store durable htpasswd credentials in tenant Secret **`olminstall-external-rh-nightly-pm-credentials`** (`HTPASSWD_USER`, `HTPASSWD_PASS`, `API_SERVER`). The pipeline step **`refresh-external-kubeconfig`** (in **`external-cluster-ready`**) logs in with those credentials, refreshes the bearer token, writes the kubeconfig back to **`CLUSTER_SOURCE`** (step fails if write-back fails), and stages it for downstream tasks.

**Shared external cluster:** unlike EaaS (one cluster per run), each physical external cluster allows only **one active olminstall PipelineRun** at a time (matched by `olminstall.cluster` / `CLUSTER_SOURCE`). The CLI **waits** before manual FBC resolution / trigger and **refuses** a second trigger when your owned run still holds the same cluster (pass **`--force-cluster-run`** to override). The pipeline task **`external-cluster-ready`** polls in **`assert-external-cluster-idle`** before install (covers Integration Service / PAC auto-triggers). EAAS is never serialized.

| Goal | Command |
|------|---------|
| Enable rh-nightly ITS (FBC app, auto on upstream builds) | `--enable-its odh-olminstall-testops-rh-nightly --konflux-app rhoai-fbc-fragment-ocp-420` |
| Enable EaaS ITS (421 component builds) | `--enable-its odh-olminstall-testops-eaas` |
| Run rh-nightly now (direct CLI PR) | `--enable-its odh-olminstall-testops-rh-nightly --run-now` |
| Disable a profile | `--disable-its NAME` |

**How to tell how a PipelineRun was started** (Konflux Activity **Trigger** is always **Push** for Snapshot-driven runs — use name + annotations instead):

| Path | PipelineRun name prefix | Konflux **Trigger** | `olminstall.trigger-type` |
|------|-------------------------|---------------------|----------------------------|
| **CLI direct** (`--run-now`, default trigger without ITS) | `olminstall-cli-<user>-*` | **Incoming** | `manual` |
| **Integration Service** (upstream FBC build Snapshot) | `olminstall-its-rh-nightly-pm-bvt-smoke-*` or `olminstall-its-eaas-bvt-smoke-*` | **Push** | *(absent)* |

**`parse-pipeline-tests`** step **`print-run-context`** logs trigger/FBC details and writes **separate Results rows** (`TRIGGER`, `KONFLUX_EVENT`, `FBC`, `CLUSTER`, `RUN`, `TRIGGER_CMD`, …). **`olm_pipeline.py -w PIPELINERUN`** prints **Trigger context** (annotations) at the end of the watch summary.

**Example Results (upstream FBC build — Integration Service):**

| Result | Value |
|--------|-------|
| `TRIGGER` | Integration Service (upstream FBC build) |
| `KONFLUX_EVENT` | Push — Integration Service (Snapshot / ITS) |
| `SNAPSHOT` | rhoai-fbc-fragment-ocp-420-on-push-xyz |
| `FBC` | rhoai-fbc-fragment-ocp-420 @ sha256:ab0042e79c99… |
| `CLUSTER` | rh-nightly-pm |
| `RUN` | product=rhoai, tests=bvt,smoke |
| `TRIGGER_CMD` | (none — upstream component build; Integration Service only) |

**Example Results (CLI direct `--run-now`):**

| Result | Value |
|--------|-------|
| `TRIGGER` | CLI direct (manual trigger) |
| `KONFLUX_EVENT` | Incoming — CLI direct PipelineRun |
| `FBC` | rhoai-fbc-fragment-ocp-420 @ sha256:d9f54f26a526… |
| `CLUSTER` | rh-nightly-pm |
| `RUN` | product=rhoai, tests=bvt,smoke |
| `TRIGGER_CMD` | python3 integration-tests/olminstall/olm_pipeline.py … --run-now … |

**`--run-now`** creates a **direct PipelineRun** using params from the ITS manifest (`olminstall-cli-{user}-…` prefix; Konflux **Trigger: Incoming**). It resolves the **latest Konflux snapshot** for the ITS `RHOAI_FBC_NAME` (e.g. `rhoai-fbc-fragment-ocp-420` on rh-nightly); the offline snapshot YAML is a **fallback** only when lookup fails. It does **not** apply the ITS to the cluster or use Integration Service. Steady `--enable-its` uses Integration Service; rh-nightly runs get prefix `olminstall-its-rh-nightly-pm-bvt-smoke-*` via [`olminstall-pipelinerun-rh-nightly.yaml`](tekton/pipelines/olminstall-pipelinerun-rh-nightly.yaml); EaaS ITS runs use `olminstall-its-eaas-bvt-smoke-*` via [`olminstall-pipelinerun-eaas.yaml`](tekton/pipelines/olminstall-pipelinerun-eaas.yaml).

```bash
# One-shot verify via direct CLI PipelineRun (fork branch until merged to main)
python3 integration-tests/olminstall/olm_pipeline.py \
  --enable-its odh-olminstall-testops-rh-nightly --run-now \
  --konflux-namespace rhoai-tenant --konflux-app testops-playpen \
  --konflux-repo https://github.com/<you>/odh-konflux-central.git \
  --konflux-branch olminstall_smoke
```

Tooling for local debug commands in this section:
- `oc` (required)
- `python3` (required for [`olm_pipeline.py`](olm_pipeline.py); use **`-w`** to stream logs or replay from KubeArchive)
- `tkn` (optional; trigger mode uses it when installed, otherwise polls with `oc`)
- `skopeo` (optional; used by `--product odh` when Konflux snapshots are unavailable)
- `yq` (required for `olm_pipeline.py` trigger/apply: the CLI always patches ITS param **`PRODUCT`** to match **`--product`**, plus any of `--konflux-repo`, `--konflux-branch`, `--channel`, `--ocp-version`, non-default **`--tests`**, **`--slack-channel-id`**, or **`--product odh`** operator overrides)

Quick watch after triggering (newest olminstall run for the app; add a PipelineRun name after `-w` to target one run):

```bash
python3 integration-tests/olminstall/olm_pipeline.py -w --konflux-namespace rhoai-tenant --konflux-app testops-playpen
```

## Parameters (Pipeline)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RHOAI_FBC_NAME` | `odh-operator-catalog` | [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) component name for the catalog fragment. **`olm_pipeline.py`** sets **`rhoai-fbc-fragment-ocp-4XX`** when **`--product rhoai`** resolves by OCP minor (for example **`rhoai-fbc-fragment-ocp-421`** for **`4.21`**). Used by **`extract-fbcf-image`** to locate the image inside **`SNAPSHOT`**. |
| `RHOAI_FBC_IMAGE` | *(olm_pipeline sets on trigger)* | Informational FBC catalog pullspec for Konflux UI. Empty / **`unspecified (default)`** with **`PRODUCT=existing`**. Resolved pullspec (or exact ref with **`--image`**) for **`--product rhoai`** / **`odh`**. Install uses pipeline result **`FBCF_IMAGE`** (**`n/a`** when existing). |
| `OPERATOR_NAMESPACE` | `redhat-ods-operator` | Namespace for operator installation (must match [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) package expectations; `install_and_verify.py` adapts olminstall manifests to this namespace) |
| `OPERATOR_NAME` | `rhods-operator` | [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) package via olminstall `install-operator.sh`. [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) and **ODH `odh-stable`** (Konflux catalog) both use **`rhods-operator`** (same as Jenkins `odhTestConfigOperator`). Upstream ODH channels outside `odh-stable` / `odh-nightlies` may use `opendatahub-operator`  -  see Jenkins `generateTestConfigFile.groovy`. |
| `HYPERSHIFT_INSTANCE_TYPE` | `m5.2xlarge` | AWS worker instance type for the ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) cluster |
| `SCRIPTS_REPO_URL` | `https://github.com/opendatahub-io/odh-konflux-central.git` | Repo that provides `integration-tests/olminstall/` (`install/`, `steps/`, `runners/`, …) |
| `SCRIPTS_REPO_REVISION` | `main` | Branch/SHA of the scripts repo |
| `OLMINSTALL_REPO_URL` | `https://gitlab.cee.redhat.com/data-hub/olminstall.git` | olminstall repo with tested OLM manifests (`resources/install-rhods-operator.yaml`, `resources/install-rhcl-operator.yaml`) and `utils/` helpers |
| `OLMINSTALL_REPO_REVISION` | `main` | Branch/SHA of the olminstall repo |
| `OLMINSTALL_GITOPS_BRANCH` | *(empty)* | odh-gitops branch for `setup-dependencies.sh -b` (Jenkins **`GITOPS_REPO_BRANCH`** parity; empty = olminstall default `main`) |
| `SETUP_DEPENDENCIES_ARGS` | `-M` | Args to olminstall **`setup-dependencies.sh`**. **`-M`** skips observability operators only (cluster-observability, opentelemetry, tempo)  -  not a “minimal MaaS-only” profile; RHCL/Kuadrant still install via GitOps. |
| `RHCL_OPERATOR_STARTING_CSV` | from olminstall manifest | MaaS RHCL pin; default read from `resources/install-rhcl-operator.yaml`, override via env |
| `RHCL_OPERATOR_READY_TIMEOUT_SEC` | `600` | Wait for RHCL + authorino-operator CSVs after pin/upgrade |
| `OLMINSTALL_CATALOG_NAME` | `rhoai-catalog-dev` | CatalogSource name used by olminstall's `install-operator.sh` |
| `QUAY_PULL_SECRET_NAME` | `rhoai-quay-secret` | Tenant secret mounted for `quay.io/rhoai` credentials (`its-olminstall-open-data-hub-tenant.yaml` overrides to `odh-quay-secret`) |
| `PRODUCT` | `rhoai` (pipeline default); CLI default **`existing`** | **`existing`**: skip EaaS/install; use with **`--external-kubeconfig`** for smoke/BVT on a cluster where RHOAI/ODH is already installed. **`rhoai`** / **`odh`**: full install path when external secret is empty. **`verify-operator-ready`** runs whenever a workload cluster is reachable (not gated on **`TEST_GATES`**). |
| `CLUSTER_SOURCE` | `EAAS` (ITS default for install) or `""` | **`EAAS`**  -  provision ephemeral cluster in-pipeline. **Secret name** (e.g. `olminstall-kubeconfig-…`)  -  user-provided cluster (`--external-kubeconfig` / `--external-kubeconfig-secret`). **Empty** with `PRODUCT=existing`  -  no cluster. |
| `RHOAI_VERSION` | *(olm_pipeline sets on trigger)* | Informational target RHOAI version for Konflux UI. **`3.5`** when passed via **`--rhoai-version`**; **`3.5 (default)`** when inferred from app/channel; **`n/a`** for **`PRODUCT=existing`**. |
| `OCP_VERSION` | *(olm_pipeline sets on trigger)* | Informational OCP minor for Konflux UI. From **`--ocp-version`**, auto-detected from **`--external-kubeconfig`** when omitted (**`--product rhoai`** only), or **`latest (default)`** on EaaS when neither applies. **`OCP_VERSION_PREFIX`** uses the same resolved minor for EaaS **`pick-version`**. |
| `UPDATE_CHANNEL` | `stable` (pipeline default); ITS **`beta`** (rhoai sandbox); auto **`beta`** for `--rhoai-version 3.5` | [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) subscription channel (install value). **`olm_pipeline.py`** always patches the resolved channel on trigger. |
| `OPENDATAHUB_TESTS_REPO` | `quay.io/opendatahub/opendatahub-tests` | Image repository (no tag) for [BVT](../../doc/contributing-konflux-testing-rhoai.md#bvt); tag derived from installed CSV when install ran, else from **`resolve_opendatahub_tests_image.py`** (empty version → `latest`) |
| `TEST_GATES` | `bvt,smoke` | Comma-separated **gate ids** from [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml). **`bvt`** → **`bvt-health-checks`**; **`smoke`** / **`tier1`** → per-component **`test-*`** (both phases in one pytest run per component when selected together). Override via ITS or `olm_pipeline.py --tests`. |
| `COMPONENTS` | *(empty)* | When **`smoke`** or **`tier1`** is selected: empty runs **all** catalog ids; comma list restricts. Ignored when neither is in **`TEST_GATES`**.
| `COMPONENT_TEST_TIMEOUT` | *(empty)* | Per-component smoke timeout (for example `10m`, `90s`, `1h30m`). Set from CLI with `olm_pipeline.py --test-timeout`. On timeout, that component is terminated and marked failed while preserving/importing xUnit output. |
| `FAIL_FAST_DISABLED_COMPONENT` | `true` | When `true`, skip pytest for smoke components **Removed** in the cluster DSC and emit one failing JUnit test per such component (other components still run). Override via ITS param `FAIL_FAST_DISABLED_COMPONENT: "false"` to run full pytest instead. |

Sandbox development may override `SCRIPTS_*` / `OLMINSTALL_*` (and the ITS `resolverRef` URL/revision) so Konflux runs a pipeline revision that is not yet on `main`; see [`its-olminstall-testops-eaas.yaml`](tekton/its/its-olminstall-testops-eaas.yaml).

## Local CLI: `olm_pipeline.py`

**Defaults:** `--product existing` skips EaaS/install; use `--product rhoai` or `odh` for full install. **`--tests`** selects gates independently (`bvt`, `smoke`, `tier1`). With **`--product existing`** and no **`--external-kubeconfig`**, default **`bvt,smoke`** runs **placeholder BVT only** (smoke is skipped until a cluster secret is set).

From the repo root, invoke `python3 integration-tests/olminstall/olm_pipeline.py` (paths shown below assume that working directory). With **no arguments**, it prints the same usage as `--help`. Use it for local Konflux olminstall workflows (trigger a run, watch logs, list runs, or query supported OCP). It can:
- List latest PipelineRuns for the selected app (`-l [N]`, default `10`), including archived runs from KubeArchive
- Apply the [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) safely on repeated runs, or enable/disable in-tree ITS manifests (`--enable-its` / `--disable-its`)
- Resolve an image (explicit `--image`, **`--product rhoai`** with optional `--rhoai-version` and OCP-aware Konflux lookup via **`--ocp-version`** or external-kubeconfig auto-detect, **`--product odh`**, or omit for **`--product existing`** — no FBC snapshot; pipeline records **`n/a`**)
- Inject ITS overrides (`PRODUCT` from **`--product`**, `SCRIPTS_REPO_*`, `UPDATE_CHANNEL`, `--tests` → ITS param `TEST_GATES`)
- Watch your latest owned PipelineRun or a specific one (`-w [PIPELINERUN]`), with KubeArchive fallback for runs pruned from the cluster
- Stop incomplete live olminstall PipelineRuns (`--delete-pending-pipelines`; optional `--stop-owned-running`, `--include-unowned-stuck`, `--delete-pending-dry-run`)
- Create a [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) directly, stream logs, and print a Konflux URL summary

**Default `--product` is `existing`:** the ITS gets **`PRODUCT=existing`** - Konflux skips EaaS provisioning, operator install, and FBC snapshot parsing (`extract-fbcf-image` writes **`n/a`**; inline **`SNAPSHOT`** omits **`containerImage`** unless you pass **`--image`**). If **`TESTS`** includes **`bvt`**, **`bvt-health-checks`** still runs (placeholder JUnit when no cluster is available). Pass **`--product rhoai`** (or **`odh`**) for a full install + EaaS BVT; those resolve the catalog from Konflux (or **`--image`**). Use **`--external-kubeconfig`** for install/BVT/smoke on a cluster you already have (see [External kubeconfig](#external-kubeconfig)).

Examples:

```bash
# Show usage (same as no arguments or --help)
python3 integration-tests/olminstall/olm_pipeline.py --help

# Watch your latest owned olminstall PipelineRun
python3 integration-tests/olminstall/olm_pipeline.py -w

# Watch a specific existing PipelineRun
python3 integration-tests/olminstall/olm_pipeline.py -w olminstall-rhoai-3.5ea2-eaas-bvt-smoke-nmanos-xxxxx

# List latest PipelineRuns for selected app (default 10)
python3 integration-tests/olminstall/olm_pipeline.py -l

# List latest 20 PipelineRuns for selected app
python3 integration-tests/olminstall/olm_pipeline.py -l 20

# Apply, one-shot verify, or remove an in-tree IntegrationTestScenario
python3 integration-tests/olminstall/olm_pipeline.py \
  --enable-its odh-olminstall-testops-rh-nightly \
  --konflux-namespace rhoai-tenant --konflux-app testops-playpen
python3 integration-tests/olminstall/olm_pipeline.py \
  --enable-its odh-olminstall-testops-rh-nightly --run-now \
  --konflux-namespace rhoai-tenant --konflux-app testops-playpen
python3 integration-tests/olminstall/olm_pipeline.py \
  --disable-its odh-olminstall-testops-rh-nightly \
  --konflux-namespace rhoai-tenant --konflux-app testops-playpen

# Show usage/help
python3 integration-tests/olminstall/olm_pipeline.py --help

# Latest FBCF across rhoai-v* apps
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai

# Pin exact image
python3 integration-tests/olminstall/olm_pipeline.py \
  --image quay.io/rhoai/rhoai-fbc-fragment@sha256:<digest>

# Test scripts from a fork
python3 integration-tests/olminstall/olm_pipeline.py \
  --konflux-repo https://github.com/you/odh-konflux-central.git \
  --konflux-branch your-feature-branch

# Resolve latest FBCF from a specific RHOAI version stream (any fragment on newest rhoai-v3-5* snapshot)
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai --rhoai-version 3.5

# RHOAI 3.5 EA.2 + OCP 4.21: Konflux component rhoai-fbc-fragment-ocp-421 (no tracer)
python3 integration-tests/olminstall/olm_pipeline.py \
  --product rhoai --rhoai-version 3.5-ea.2 --ocp-version 4.21

# EaaS install: pin cluster minor; EaaS resolves latest patch; FBC matches OCP fragment
python3 integration-tests/olminstall/olm_pipeline.py \
  --product rhoai --rhoai-version 3.5-ea.2 --ocp-version 4.21 --tests bvt,smoke

# External cluster: auto-detect OCP from kubeconfig and pick matching FBC fragment
python3 integration-tests/olminstall/olm_pipeline.py \
  --product rhoai --rhoai-version 3.5-ea.2 \
  --external-kubeconfig ~/.kube/my-cluster --tests bvt,smoke

# Override OLM channel
python3 integration-tests/olminstall/olm_pipeline.py --channel beta

# OLM install + workbenches component smoke only (skip BVT and tier1)
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai --tests smoke --components workbenches

# Install + BVT health pytest only (omit smoke / tier1 tokens from TESTS)
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai --tests bvt

# Full default phases + tier1 no-op placeholder
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai --tests bvt,smoke,tier1

# Custom phases file (same schema as olminstall-tests-config.yaml; needs PyYAML or yq)
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai \
  --tests-config /path/to/my-olminstall-tests.yaml --tests bvt,smoke

# Trigger against ODH (uses sandbox ITS with ODH-specific pipeline params)
python3 integration-tests/olminstall/olm_pipeline.py --product odh

# Existing cluster: tests only (no EaaS, no install; cluster prep in opendatahub-tests-prepare)
python3 integration-tests/olminstall/olm_pipeline.py \
  --external-kubeconfig ~/.kube/my-cluster \
  --product existing --tests bvt,smoke

# Existing external cluster: run dependency operators + cluster prep in install-dep-operators
# (opt-in for pooled QE clusters  -  RHCL/setup-dependencies before smoke)
python3 integration-tests/olminstall/olm_pipeline.py \
  --external-kubeconfig ~/.kube/my-cluster \
  --product existing --install-dependencies \
  --tests bvt,smoke --components model_server

# Existing cluster: install from snapshot FBCF then BVT
python3 integration-tests/olminstall/olm_pipeline.py \
  --external-kubeconfig ~/.kube/my-cluster \
  --product rhoai --image quay.io/rhoai/rhoai-fbc-fragment@sha256:... --tests bvt

```

### OCP-aware RHOAI FBC resolution

For **`--product rhoai`**, **`olm_pipeline.py`** resolves the catalog from **Konflux Snapshots** (not [tracer](https://github.com/rhods-devops-infra/tree/main/tools/tracer)). When **`--rhoai-version`** is set and an OCP minor is known (**`--ocp-version`**, or auto-detected from **`--external-kubeconfig`**), the CLI:

1. Maps the minor to a snapshot component name (**`4.21`** → **`rhoai-fbc-fragment-ocp-421`**).
2. Finds matching **`rhoai-v*`** applications for the version prefix (for example **`3.5-ea.2`** → **`rhoai-v3-5-ea-2*`**; **`3.5`** → all **`rhoai-v3-5*`** apps).
3. Picks the **newest Snapshot** that lists that component’s **`containerImage`**.
4. Patches ITS **`RHOAI_FBC_NAME`**, inline **`SNAPSHOT`**, and **`OCP_VERSION`** / **`OCP_VERSION_PREFIX`**.

| Flags | FBC selection |
|-------|----------------|
| **`--rhoai-version 3.5-ea.2`** + OCP minor | Newest snapshot for **`rhoai-fbc-fragment-ocp-4XX`** on **`rhoai-v3-5-ea-2*`** apps |
| **`--rhoai-version 3.5`** + OCP minor | Same fragment name; newest snapshot across all **`rhoai-v3-5*`** apps (ea-1, ea-2, …) |
| **`--rhoai-version`** without OCP hint | Legacy: newest FBC image on matching version apps (any fragment) |
| No **`--rhoai-version`** | Highest **`rhoai-v*`** application line, then newest snapshot |
| **`--product existing`** | No FBC resolution (unchanged) |
| **`--image @sha256:…`** | Explicit digest wins |

**`--ocp-version`** with **`--external-kubeconfig`** is allowed for **`--product rhoai`** (optional override; omit to auto-detect). It is rejected for **`--product existing`** (no catalog install).

### External kubeconfig

**`--external-kubeconfig PATH`** uploads a local kubeconfig as a tenant Secret (key **`kubeconfig`**) and sets pipeline param **`CLUSTER_SOURCE`** to that Secret name. EaaS **`provision-eaas-space`** / **`install-ocp-cluster`** are skipped. With **`--product rhoai`** or **`odh`**, **`install-operator`** runs the same install steps against the mounted secret, then **`verify-operator-ready`** confirms the dashboard; with **`--product existing`**, install is skipped and **`verify-operator-ready`** runs against the existing cluster before BVT/smoke.

**`--install-dependencies`** ( **`--product existing`** only, requires **`--external-kubeconfig`** or **`--external-kubeconfig-secret`**, and **`--tests`** including **`smoke`** and/or **`tier1`**) sets **`INSTALL_DEPENDENCIES=true`**. The pipeline runs **`install-dep-operators`** ( **`setup-dependencies.sh`**, RHCL pin, Llama/Serverless when needed) and then **`prepare-component-cluster`** in that task. **`prepare-components-prerequisites-*`** in **`opendatahub-tests-prepare`** is skipped so prep is not duplicated. Default **`--product existing`** without this flag still prepares the cluster only in **`opendatahub-tests-prepare`** (no **`install-dep-operators`**).

**`--external-kubeconfig-secret NAME`** uses a Secret you created manually (mutually exclusive with **`--external-kubeconfig`**). Env: **`OLMINSTALL_EXTERNAL_KUBECONFIG`**, **`OLMINSTALL_EXTERNAL_KUBECONFIG_SECRET`**.

**EaaS triggers** (`--product rhoai` or `odh` without external kubeconfig) set **`CLUSTER_SOURCE=EAAS`**.

**`--cleanup`** sets pipeline param **`CLEANUP=true`**: Tekton task **`cleanup-external`** runs [olminstall `cleanup.sh`](https://gitlab.cee.redhat.com/data-hub/olminstall/-/blob/main/cleanup.sh) (`-t operator`) on the external cluster **before** **`install-operator`**. Requires **`--external-kubeconfig`** or **`--external-kubeconfig-secret`**. Olminstall source is cloned in the pipeline from **`OLMINSTALL_REPO_*`** params. **Destructive**  -  only on disposable test clusters.

Operational notes:

- TaskRun pods must reach the API URL in the kubeconfig (network/firewall from Konflux workers).
- The Integration ServiceAccount needs **`get`** on the Secret in the tenant namespace.
- Treat uploaded kubeconfigs as sensitive; the CLI deletes Secrets it created on normal exit (not on Ctrl-C detach).
- **`--ocp-version`** with **`--external-kubeconfig`**: optional for **`--product rhoai`** (FBC fragment selection + ITS display); rejected for **`--product existing`**. Omit **`--ocp-version`** on external **`rhoai`** installs to auto-detect the cluster minor from the kubeconfig.
- **External kubeconfig** with **`--product rhoai`** / **`odh`** runs **`install-dep-operators`** before **`install-operator`** (same **`setup-dependencies.sh`** as EaaS). **`collect-diagnostics`** runs after every completed pipeline when a target kubeconfig is available (EaaS or external).

A full `olm_pipeline.py` trigger or watch (including **`--tests bvt`**) will fail at **`oc whoami`** or **`ensure_konflux_cluster`** without a Konflux cluster  -  that is expected. **Local unit tests** (`make test` under `integration-tests/olminstall/`) run without `oc`. Use **`--external-kubeconfig PATH`** (or **`--external-kubeconfig-secret NAME`**) to run BVT/smoke against an existing cluster without EaaS provisioning (see [External kubeconfig](#external-kubeconfig) below).

Omit `--konflux-repo`/`--konflux-branch` to keep the ITS default Git source for the remote pipeline definition. If you see **`CouldntGetPipeline`** / **`tekton/pipelines/olminstall-pipeline.yaml`: file does not exist**, the default revision does not ship that path yet - use **`--konflux-repo`** + **`--konflux-branch`** on a fork/branch where `integration-tests/olminstall/tekton/pipelines/olminstall-pipeline.yaml` exists. Trigger mode prints a **WARN** when **`--konflux-repo` is set without `--konflux-branch`** (resolver revision may stay at the ITS YAML default, e.g. `main`); omitting both flags uses the committed ITS default with no such warning.

> **Concurrent runs:** The CLI does not take a cluster-side lock. If two users run the script simultaneously against the same namespace, both may create Snapshots and trigger separate PipelineRuns. **Trigger mode always starts a new run** (it does not attach to an already-running PipelineRun). If you still have a run in progress for the same app, the helper prints an **INFO** with a copy-pastable `-w <pipelinerun>` command so you can stream that run instead. Use `-w` (no name) to follow your newest owned olminstall run for `--konflux-app`, or `-w <pipelinerun>` for an explicit run. On normal exit the helper deletes your trigger Snapshot; **Ctrl-C while streaming logs only detaches locally**  -  the PipelineRun keeps running and the Snapshot is left in place. Deleting a Snapshot mid-run is non-fatal to the PipelineRun (which has already resolved the snapshot). To avoid confusion, coordinate with your team before triggering manually in a shared namespace.

If a freshly-created Snapshot takes time to trigger, `olm_pipeline.py` waits up to `PR_APPEAR_TIMEOUT_SECONDS` (default `600`) for the corresponding PipelineRun before failing. On this timeout path, it keeps the test Snapshot so a delayed run can still be followed with `-w` (or `-w <name>` once the PipelineRun name is visible in the Konflux UI).

> **Archived runs (KubeArchive):** Completed PipelineRuns are pruned from the live cluster by Tekton Results / cluster GC shortly after completion. `-l` and `-w` automatically fall back to the [KubeArchive](https://konflux-ci.dev/architecture/core/pipeline-service/) REST API to retrieve pruned runs and replay their logs. The `KA_HOST` environment variable can override the KubeArchive endpoint if needed. If KubeArchive is unreachable, the script degrades gracefully to live-only data.

> **`--delete-pending-pipelines` (live cluster only):** Stops incomplete olminstall PipelineRuns still on the apiserver: Kueue/resolver **pending**, your **owned** incomplete runs (PipelineRun or Snapshot `olminstall.run-owner`), and optionally unowned runs stuck with **no TaskRuns** (`--include-unowned-stuck`). Running owned runs with tasks are skipped unless you pass `--stop-owned-running` (uses `tkn pipelinerun cancel` when available). Use `--delete-pending-dry-run` to list targets without cancel/delete.
>
> **Konflux UI ghosts (archived, not live):** Some pruned runs are archived in KubeArchive with `deletionTimestamp` set, Tekton finalizers still present, no `completionTime`, and `Succeeded=Unknown` (for example `ResolvingTaskRef`). Konflux Activity may list these as **Pending/Running** indefinitely. `oc delete` and this CLI **cannot** remove or fix those archive records (tenant KubeArchive API is read-only). **Do not** recreate a PipelineRun by the same name to “unstick” the UI  -  that adds a second archive row and duplicates the ghost. Ignore the stale rows or escalate to platform admin. When no live targets match, the command may list incomplete KubeArchive records for awareness only.

```bash
# Dry-run: show what would be stopped on-cluster
python3 integration-tests/olminstall/olm_pipeline.py --delete-pending-pipelines --delete-pending-dry-run

# Also cancel+delete your actively running owned run (Konflux Stop/Cancel equivalent)
python3 integration-tests/olminstall/olm_pipeline.py --delete-pending-pipelines --stop-owned-running
```

For `--product rhoai`, use `--rhoai-version` in `x.y` form (for example `3.5`).

### Channel behavior for current `rhoai-v3-5-ea-1` FBCF

For the current fragment image (`quay.io/rhoai/rhoai-fbc-fragment@sha256:dc61ae73...`), OLM channel heads are:

| Channel | Latest operator |
|---------|------------------|
| `stable` | `rhods-operator.2.25.5` |
| `stable-3.x` | `rhods-operator.3.4.0` |
| `stable-3.4` | `rhods-operator.3.4.0` |
| `beta` | `rhods-operator.3.4.0-ea.1` |
| `fast-3.x` | `rhods-operator.3.3.1` |

`olm_pipeline.py` auto-selects **`beta`** for **`--rhoai-version 3.5`**, **`rhoai-v3-5*`**, and **`rhoai-v*-ea-*`** (Jenkins autotrigger-smoke parity: `UPDATE_CHANNEL=beta`, `setup.sh -u beta`). Other versions map to `stable-<x.y>` from the version or resolved `rhoai-v*` app; generic `rhoai-v3-*` falls back to `stable-3.x`.

**Important:** the bare pipeline default `UPDATE_CHANNEL` is still **`stable`** (2.25.x head on current FBCF). RHOAI sandbox ITS sets **`beta`**; use `olm_pipeline.py --product rhoai --rhoai-version 3.5` or set `UPDATE_CHANNEL` on the ITS when testing 3.x EA.

Examples:

```bash
# Default for --rhoai-version 3.5: auto channel beta (Jenkins parity)
python3 integration-tests/olminstall/olm_pipeline.py --product rhoai --rhoai-version 3.5

# Explicitly force stable-3.x (3.4 GA head on current FBCF)
python3 integration-tests/olminstall/olm_pipeline.py --channel stable-3.x

# Override EA channel explicitly
python3 integration-tests/olminstall/olm_pipeline.py --channel beta
```

## Test phases configuration (`config/olminstall-tests-config.yaml`)

Phase ids, defaults, and which Tekton results (`RUN_SMOKE`, `RUN_BVT`, `RUN_TIER1`, …) each phase toggles live in [`config/olminstall-tests-config.yaml`](config/olminstall-tests-config.yaml). **`parse-pipeline-tests`** clones `SCRIPTS_REPO` into **`tests-shared/scripts-repo`** and evaluates that file at runtime.

**`artifactUpload`** in the same file controls OCI publish: **`ociSubdir`** (browser folder, default **`test-payload-results`**) and **`includePatterns`** (fnmatch globs, default **`*.xml`**, **`*.log`**, **`*.console.log`**). Workspace JUnit/logs live under **`tests-payload/results/`**; staged **`oc`** stays under **`tests-payload/.tools/`** and is excluded from upload.

When you add a new phase that should drive pipeline `when:` branches, extend **both** the YAML (`setsPipelineResults`) and [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml) (new `results`, `parse-pipeline-tests` wiring, and tasks).

## Component smoke (`config/olminstall-components-smoke.yaml`)

When **`smoke`** and/or **`tier1`** is in **`TESTS`**, each **`test-<component>`** pipeline task runs **one** component test session (pytest, golang, Cypress, or Playwright per catalog `runner`). Each component remains its own Konflux DAG node; phases are not separate pipeline cycles. See [`suite/component_phases.py`](suite/component_phases.py), [`runners/run_component_pytest.py`](runners/run_component_pytest.py), and [`runners/run_component_golang.py`](runners/run_component_golang.py).

The catalog is **[`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml)** in this repo (`COMPONENTS_CONFIG` after `SCRIPTS_REPO` clone). Konflux **does not** clone the TestOps Jenkins repo at smoke runtime. Entries use **schemaVersion 2**: a **`component:`** block (manual mirror of shift-left `main.yaml` from TestOps Jenkins `resources/configs/components-testing/components/<name>/`) plus Konflux-only **`konflux:`** extensions. **ods-ci is not invoked** from Konflux olminstall.

**DSC at install:** [`config/olminstall-dsc-install.yaml`](config/olminstall-dsc-install.yaml) maps smoke catalog ids to DSC `spec.components` keys and version bands (Jenkins `generateTestConfigFile` / odhcluster parity). [`install/dsc_install_policy.py`](install/dsc_install_policy.py) resolves Managed vs Removed at `install-rhoai`; component prep enables Managed before pytest.

### Version → test container (dynamic + gates + pins)

| Rule | Behavior |
|------|----------|
| **Default** | Installed operator CSV → `opendatahub-tests` tag via [`steps/resolve_opendatahub_tests_image.py`](steps/resolve_opendatahub_tests_image.py) (mirrors Jenkins BVT). |
| **Gates** | `component.enablement.minRhoai` / `maxRhoai` enforced in [`steps/export_component_plan.py`](steps/export_component_plan.py) during **`opendatahub-tests-prepare`**; [`steps/refresh_component_smoke_flags.py`](steps/refresh_component_smoke_flags.py) runs in **`resolve-component-run-flags`** (after prepare) and publishes `RUN_SMOKE_<id>` so unselected or version-unsupported components are **grey-skipped** in the Konflux DAG. `test-*` PipelineTasks use `when: RUN_SMOKE_<id>` from resolve results (per catalog id, including granular `ai_safety_*` on RHOAI 3.5+). |
| **Pins** | `konflux.opendatahubTestsImage` (e.g. `llama_stack:3.4`) or `runner.image` for golang/mlflow images. |
| **ODH** | `PRODUCT=odh` resolves `opendatahub-tests:latest` (Jenkins `odh-stable.yaml`). |
| **Override** | `--tests-rhoai-version` → ITS param `OLMINSTALL_TESTS_VERSION_OVERRIDE` for external runs. |

Do **not** use `--konflux-branch` for test image selection (that only selects pipeline script revision).

### Upstream provenance convention

When tracing Konflux smoke back to TestOps Jenkins (GitLab **`ods/jenkins`**):

1. **`konflux.description`** in [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml)  -  upstream path (`components/<name>/main.yaml`) or job name (`job dashboard-e2e-tests`) only. No build numbers or Konflux runtime details.
2. **Konflux behavior**  -  `konflux:` block (runners, timeouts, vault keys, `cypress.gates`, pass rates) plus Tekton tasks and Python helpers.
3. **Full parity matrix**  -  tables in this README; do not duplicate in pipeline task YAML or generated descriptions beyond the auto-appended upstream pointer.

Per-component Konflux task descriptions are generated from the smoke catalog into `tekton/tasks/generated/component-<id>.yaml` (see `python3 -m suite.generate_component_tekton_tasks`). The pipeline `pathInRepo` for each `test-*` task points at those files so the Konflux Details panel shows framework, image, commands, and timeouts for that component.

**Regenerate** after editing `config/olminstall-components-smoke.yaml` or any base `tekton/tasks/task-component-*.yaml`, then commit `tekton/tasks/generated/` and any pipeline `pathInRepo` changes:

```bash
cd integration-tests/olminstall && python3 -m suite.generate_component_tekton_tasks
```

Drift tests: `unit_tests/suite/test_pipeline_catalog_consistency.py` and `unit_tests/suite/test_component_task_description.py` (includes regen-vs-committed check).

### Maintaining the catalog (manual copy from Jenkins)

The catalog is **not** generated or synced automatically. When TestOps Jenkins changes a component’s shift-left config:

1. Check out the TestOps Jenkins repo (GitLab **`ods/jenkins`**).
2. Open **`resources/configs/components-testing/components/<name>/main.yaml`** for that component.
3. Copy pytest-related fields into the matching **`component:`** block in [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml): `merge.metadata`, `merge.image.args`, `merge.qualityGatesMap`, `konfluxRepoMapKeys`, and related enablement fields.
4. Update **`konflux:`** only when Konflux pipeline behavior should change (timeouts, `minPassRateForSuccess`, `requiresShiftLeftEnv`, custom runners, etc.).

Optional drift check (local maintainer tool, requires a Jenkins checkout):

```bash
cd integration-tests/olminstall
JENKINS_REPO=/path/to/ods/jenkins python3 -m suite.verify_catalog_parity
```

Compares each catalog ``component:`` slice to ``resources/configs/components-testing/components/<name>/main.yaml``.

| Param / task | Role |
|--------------|------|
| `TESTS=smoke` or `tier1` or `smoke,tier1` | Enables **`opendatahub-tests-prepare`** + per-component **`test-*`** (one DAG task per component; phases combined per pytest run) |
| `COMPONENTS` | Empty = all catalog components; comma list = subset only |
| `install-dep-operators` | When **`RUN_INSTALL_DEP_OPERATORS`**: `setup-dependencies.sh`, RHCL CSV pin + `post-install-rhcl`, Serverless/Llama sidecars; on **`PRODUCT=existing`** only with **`--install-dependencies`** or **`PRODUCT=rhoai`/`odh`** reinstall; prepare probes MaaS deps and fails with retrigger hint if missing |

### Jenkins parity: `install-dep-operators` vs TestOps InstallDeps

| Topic | Jenkins (TestOps) | Konflux `install-dep-operators` |
|-------|-------------------|----------------------------------|
| Entry | ods-ci Robot **`InstallDeps`** during deploy; **`INSTALL_AUTHORINO_DEPENDENCY`** toggles standalone Authorino on 2.x jobs | Direct **`setup-dependencies.sh`** via [`install/install_minimal_deps.py`](install/install_minimal_deps.py) |
| RHOAI 3.x Authorino | Generated jobs set **`installAuthorino=false`** (RHCL bundle via gitops instead) | RHCL deps when **`RUN_INSTALL_DEP_OPERATORS`** (reinstall or **`--install-dependencies`** on existing) |
| GitOps branch | **`GITOPS_REPO_BRANCH`** job param | Pipeline param **`OLMINSTALL_GITOPS_BRANCH`** → `setup-dependencies.sh -b` |
| RHCL CSV | olminstall **`install-rhcl-operator.yaml`** (`startingCSV`) on direct install paths | Read from cloned olminstall manifest; patch Subscription when pooled cluster has unpinned RHCL |
| Post-install RHCL | **`post-install-rhcl-operator.sh`** after RHCL CSV (Kuadrant wait + Authorino TLS) | Same script from **`ensure_maas_rhcl_dependency_stack()`** in install-dep-operators (not deferred to prepare-only) |
| Failure policy | Deploy job fails when deps/install fail | MaaS/smoke-selected deps **fail** `install-dep-operators`; product install without MaaS smoke **warns** on partial deps issues and **`verify-operator-ready`** gates dashboard readiness |

Disconnected-cluster **`install-operator.sh rhcl-operator`** is out of scope for Konflux olminstall.
| `opendatahub-tests-prepare` | When BVT/smoke/tier1 selected: kubeconfig, resolve `opendatahub-tests` image, smoke plan + prereqs (scripts from **`tests-shared/scripts-repo`**) |

**Smoke catalog (shift-left parity; `cluster_health` / `operator_health` are BVT only):**

| Catalog id | TestOps Jenkins upstream | Konflux runner | Pass rate |
|------------|--------------------------|----------------|-----------|
| `workbenches` | `components/workbenches/main.yaml` | `tests/workbenches/` `-m smoke` | `minPassRateForSuccess` **90%** |
| `model_registry` | `components/ai-hub/main.yaml` | `tests/ai_hub/` `-m smoke` |  -  |
| `model_server` | `components/model-server/main.yaml` | `tests/model_serving/model_server/` `-m smoke` |  -  |
| `model_runtime` | `components/model-runtime/main.yaml` | `tests/model_serving/model_runtime/` `-m smoke` |  -  |
| `maas_billing` | `components/maas_billing/main.yaml` | `tests/model_serving/maas_billing/` `-m smoke` |  -  |
| `ai_pipelines` | `components/ai-pipelines/main.yaml` | `ds-pipelines-tests` `test-run.sh --label-filter=Smoke` |  -  |
| `kuberay` | `components/kuberay/main.yaml` | `kuberay-tests` `run-tests.sh -testTier=Smoke` |  -  |
| `mlflow` | `components/mlflow/main.yaml` | `mlflow-tests` `test-run.sh -m smoke` |  -  |
| `ogx` | `components/ogx/main.yaml` | `tests/ogx/` `-m smoke` |  -  |
| `ai_safety` | `components/model-explainability/main.yaml` | `tests/ai_safety/` `-m smoke` |  -  |
| `llama_stack` | `components/llama_stack/main.yaml` | `tests/llama_stack/` `-m smoke` (pin `:3.4`; gate ≤3.4) |  -  |
| `dashboard_cypress` | `job dashboard-e2e-tests` | `cypress-e2e-image` + clone `odh-dashboard`; `@SmokeSet1-5` | `minPassRateForSuccess` **90%**; vault `envfile-dashboard-cypress` |
| `trainer` | `components/trainer/main.yaml` | `distributed-workloads-tests` `./trainer` `-testTier=Smoke` | gate ≥3.2 |
| `distributed_workloads` | `components/distributed-workloads/main.yaml` | same image `./kfto` `-testTier=Smoke` |  -  |
| `spark_operator` | `components/spark-operator/main.yaml` | `opendatahub-operator-e2e` `--test-component=sparkoperator` | gate ≥3.4 |
| `workbench_images` | `job workbench-images-tests` | Playwright `--grep @smoke` |  -  |
| `codeflare_sdk` | `components/codeflare-sdk/main.yaml` | `codeflare-sdk-tests` `run-tests.sh -m smoke` | vault `envfile-codeflare-sdk` |
| `platform` | `components/platform/main.yaml` | `opendatahub-operator-e2e` `run_e2e_tests.sh` `--tag=Smoke` | gate ≥3.3; **`runOrder: last`** |

**Notes:**

- **`COMPONENTS` empty** (default): smoke runs for **every** catalog id (18 rows).
- **`--components workbenches,model_registry`**: smoke runs only for those ids (still uses each entry’s smoke marker).
- **`model_server`**, **`model_runtime`**, **`maas_billing`** may fail on pooled external clusters without ods-ci vault or MaaS - use **`--components`** to subset when needed.

### TestOps Jenkins shift-left parity

The **`component:`** slices in [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml) are a **manual** mirror of pytest-related fields from TestOps Jenkins (`resources/configs/components-testing/components/<name>/main.yaml`, internal GitLab `ods/jenkins`). Konflux olminstall covers the **opendatahub-tests** subset only; the full Jenkins matrix is broader.

| Legacy shift-left stage | Konflux olminstall smoke catalog |
|-------------------------|----------------------------------|
| validateHealth (BVT pytest) | **`bvt`** gate (`cluster_health` + `operator_health`, ~5 tests) |
| Robot operator checks (pre-pytest) | Not in Konflux (Robot / ods-ci) |
| ai-hub (model registry) pytest | **`model_registry`**: same `-m smoke` on `tests/ai_hub/` |
| ai-safety pytest | **`ai_safety`**: same `-m smoke` |
| workbenches pytest | **`workbenches`**: same `-m smoke` |
| model-server, model-runtime, maas_billing | Same catalog ids (when **`COMPONENTS`** empty) |
| ai-pipelines (`ds-pipelines-tests`) | **`ai_pipelines`** in smoke catalog (`tests/ai_pipelines/` `-m smoke`) |
| kuberay (`kuberay-tests`) | **`kuberay`** in smoke catalog (`run-tests.sh -testTier=Smoke`) |
| mlflow (`mlflow-tests`) | **`mlflow`** in smoke catalog (`test-run.sh -m smoke`) |
| ogx, llama_stack | **`ogx`** and **`llama_stack`** in smoke catalog (version gates) |
| trainer, distributed-workloads | **`trainer`**, **`distributed_workloads`**: `distributed-workloads-tests` golang |
| spark-operator, platform | **`spark_operator`**, **`platform`**: `opendatahub-operator-e2e` golang |
| workbench-images | **`workbench_images`**: Playwright ([`tekton/tasks/task-component-playwright.yaml`](tekton/tasks/task-component-playwright.yaml)) |
| codeflare-sdk | **`codeflare_sdk`**: `codeflare-sdk-tests` external-pytest |
| dashboard-e2e-tests | **`dashboard_cypress`**: runtime clone + `cypress-e2e-image`; vault `envfile-dashboard-cypress` |

Per-component **pytest** counts match the legacy runner when the same marker and path are used. Non-pytest catalog entries (golang, Cypress, Playwright) use dedicated `runner` images documented in the table above. Konflux runs look smaller when:

| Factor | Legacy shift-left (all enabled stages) | Typical Konflux run |
|--------|----------------------------------------|---------------------|
| Components | All enabled pytest stages (~16) | **`COMPONENTS`** empty = **18** catalog ids; **`--components a,b,c`** = subset only |
| Health gates | BVT before components | Only when **`--tests bvt,smoke`** (or ITS includes **`bvt`**) |
| Non-pytest tiers | Robot, golang, Playwright, component-specific images | Same runners in catalog (`runner.type` golang / cypress / playwright) |

Example: **`--tests smoke --components workbenches,model_registry,ai_safety`** runs a small subset, not the full legacy matrix. For catalog parity omit **`--components`** (all 18 ids).

**Infra-blocked verify (revisit only when external prerequisites land):** `maas_billing`, `mlflow`, `model_registry`, `kuberay` on pooled psi-23; `dashboard_cypress` needs vault `envfile-dashboard-cypress` and reachable dashboard gateway URL.

**Deferred (not in catalog):** feast, customer-workflows, *-upstream (Robot/make).

To add a component: extend [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml), then `--tests smoke --components <id>`.

## Rebasing on upstream `main`

[opendatahub-io/odh-konflux-central#362](https://github.com/opendatahub-io/odh-konflux-central/pull/362) is merged. For follow-up work, rebase (or branch) from current upstream `main`:

```bash
git fetch upstream
git rebase upstream/main
# or: git switch -c my-feature upstream/main
```

To base a branch on another open upstream PR, fetch its head by number (`N`):

```bash
git fetch upstream pull/N/head:pr-upstream-N
git switch -c my-follow-up pr-upstream-N
```

## BVT on an existing cluster (outside this CLI)

BVT pytest (`cluster_health` / `operator_health`) runs when `bvt` is included in `TESTS`. Task **`bvt-health-checks`** branches internally for EaaS, external, or placeholder paths. See [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml) for step-level `when:` rules.

## Failure diagnostics vs BVT logs

On every completed pipeline run after the cluster is ready, **collect-diagnostics** writes:

| Output | When |
|--------|------|
| **`/diag/triage/`** | Always (default)  -  status report, events, operator highlights; pod logs use **`oc logs --since-time=<PipelineRun start>`** |
| **`tests-payload/results/{product}-{version?}-{cluster?}-diagnostic-{datetime}.log`** | Merged triage + dependency install + pod logs (product/version from cluster CSV) |
| **`/diag/rhoai-cr-status/`** | Always  -  `DSC` / `DSCInitialization` get + describe |
| **`csv.yaml`**, **`subscription-describe.txt`**, **`marketplace-jobs-summary.txt`** | When **`PRODUCT`** is not `existing` (install path ran) |
| **`inspect-ns-operator/`** (`oc adm inspect`) | When **`install-operator`** **Failed** or overall pipeline **Failed** (override with **`DIAG_COLLECT_ADM_INSPECT`**) |

Env: **`PIPELINE_RUN_NAME`** (in-cluster API lookup for pod log `--since-time`), **`INSTALL_DEP_OPERATORS_STATUS`**, **`DIAG_POD_LOG_MAX_BYTES`**, **`DIAG_ISSUES_SUMMARY_MAX_LINES`**.

When RHOAI is not installed yet (dependency install failure), **dependency install status/events/pod logs** are collected from `kuadrant-system`, `cert-manager`, `openshift-keda`, OLM namespaces, etc. The same report is echoed to the **Tekton step log** (Konflux UI) and uploaded as the OCI artifact.

**`publish-results`** waits on **`.collect-diagnostics-done`** before OCI upload so the diagnostic log is not missed when finally tasks run in parallel.

A **small** `DIAGNOSTICS_MANIFEST` Tekton result leads with the **issues summary** and status snippets. Full triage (including pod logs since pipeline start) is in **`{product}-{version?}-{cluster?}-diagnostic-{datetime}.log`** in the OCI artifact browser (e.g. `rhoai-2.4.1-ods-qe-psi-07-diagnostic-2026-06-24T112510Z.log`).

## Slack notifications

The **`publish-results`** task posts to Slack (step **`send-notification`**) when **`SLACK_WEBHOOK_URL`** is set. The message uses aggregate pipeline task status (`Succeeded` when every non-finally task that ran has succeeded; optional phases skipped via `TESTS` do not count as failure). Create an optional Secret in the tenant namespace:

```text
Name: slack-webhook
Key:  webhook-url   (full Slack incoming webhook URL)
```

If the Secret is absent, the step logs the message and exits without failing the run.

## Unit tests (pipeline)

Pytest tests for olminstall **pipeline scripts and helpers** (CLI, Tekton step logic, YAML generation, orchestration). They are **not** the cluster integration runs: operator install, BVT, and RHOAI/ODH component smoke (`bvt-health-checks`, `test-*`, …) execute only in Konflux PipelineRuns against a live cluster.

From `integration-tests/olminstall/` (no `oc` required):

```bash
make deps       # once per venv
make test
make test-cli   # CLI tests only
```

Or from the repo root:

```bash
make -C integration-tests/olminstall deps
make -C integration-tests/olminstall test
make -C integration-tests/olminstall test-cli
```

Or use pytest directly:

```bash
pip install -r integration-tests/olminstall/requirements.txt
cd integration-tests/olminstall
pytest -q
pytest unit_tests/test_olm_cli.py -q
```

[`pytest.ini`](pytest.ini) sets `testpaths = unit_tests` and `pythonpath = .` so imports use `steps.*`, `runners.*`, `suite.*`, `install.*` without per-test `sys.path` hacks.

**Unit test imports:** prefer production package paths (`from runners.cli…`, `from suite…`). Use `from unit_tests._paths import REPO_ROOT` (or `OLMINSTALL_ROOT`) only when a test needs repo roots; do not call `ensure_olminstall_path()` in test modules.

**Automated PR review:** [`.coderabbit.yaml`](../../.coderabbit.yaml) excludes `unit_tests/**` from CodeRabbit path filters (wave 1 scope); rely on `pytest -q` in CI and local runs before merge. Re-enable review for this tree when the olminstall PR review cap allows wave 2.

### Python packages

Packages are top-level under `integration-tests/olminstall/` (Tekton invokes `python -m steps.<module>` or `python -m runners.<module>` from `OLMINSTALL_DIR`). See [Directory layout](#directory-layout).

| Package | Role |
|---------|------|
| [`steps/`](steps/) | Tekton step entrypoints (flags, kubeconfig, diagnostics, `TEST_OUTPUT`) |
| [`runners/`](runners/) | BVT and per-component test runners; [`runners/cli/`](runners/cli/) backs `olm_pipeline.py` |
| [`runners/report/`](runners/report/) | JUnit/UI summary, Slack, artifact URLs, ITS prune helper |
| [`suite/`](suite/) | Catalog, plan, phases, constants, tests-config parsing |
| [`install/`](install/) | OLM install, pull secret, dependency operators, DSC policy |
| [`k8s/`](k8s/) | `oc` utilities, external kubeconfig, cluster probes |
| [`components/`](components/) | Component-specific prereqs (e.g. [`maas_billing/`](components/maas_billing/)  -  gateway, DB, UWM, AuthPolicy; RHCL stack in [`install/rhcl_deps.py`](install/rhcl_deps.py)) |

Per-component catalog gates (**`smoke`**, **`tier1`**) share one pytest runner; **BVT** is separate.

| Module | Role |
|--------|------|
| [`runners/run_bvt_pytest.py`](runners/run_bvt_pytest.py) | Tekton: BVT (`cluster_health` / `operator_health`) |
| [`runners/run_component_pytest.py`](runners/run_component_pytest.py) | Tekton: per-component opendatahub-tests (smoke and/or tier1 markers) |
| [`suite/component_catalog.py`](suite/component_catalog.py) | Load [`config/olminstall-components-smoke.yaml`](config/olminstall-components-smoke.yaml) |
| [`suite/verify_catalog_parity.py`](suite/verify_catalog_parity.py) | Local maintainer check: catalog `component:` slices vs Jenkins `main.yaml` (`JENKINS_REPO`) |
| [`suite/component_plan.py`](suite/component_plan.py) | Validate/normalize `COMPONENTS` selection |
| [`suite/component_phases.py`](suite/component_phases.py) | Map `TESTS` phases to combined pytest `-m` expressions |
| [`runners/component_cluster_prep.py`](runners/component_cluster_prep.py) | Tekton: cluster prep before component pytest |
| [`suite/component_dsc_gate.py`](suite/component_dsc_gate.py) | DSC Removed check before component pytest |
| [`runners/orchestrator.py`](runners/orchestrator.py) | Prereq orchestration for catalog components |

## Maintenance

- **Image digest pins**  -  Some steps in [`tekton/pipelines/olminstall-pipeline.yaml`](tekton/pipelines/olminstall-pipeline.yaml) pin tool images by digest (e.g. `konflux-test:stable@sha256:…`) so runs stay reproducible; refresh those digests on whatever cadence your team uses and re-run the pipeline after each bump.
- **BVT image**  -  **`opendatahub-tests-prepare`** resolves a versioned or `:latest` [`opendatahub-tests`](https://quay.io/repository/opendatahub/opendatahub-tests) image. **`bvt-health-checks`** runs **`uv run pytest`** where a cluster is available.
