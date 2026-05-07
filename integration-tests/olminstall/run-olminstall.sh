#!/bin/bash
# Trigger (or attach to) the olminstall smoke pipeline in Konflux.
#
# - Ensures the ITS CR is applied (idempotent).
# - If an olminstall PipelineRun is already running for the app, reattaches quickly
#   (prefers same-user run markers when available).
# - Otherwise creates a fresh Snapshot to trigger a new run, then watches it.
# - Defaults to the latest Konflux-built FBCF image across all RHOAI apps;
#   narrow with --version (for --product rhoai) or override with --image.
# - Cleans up the test Snapshot on exit.
#
# Usage:
#   ./run-olminstall.sh                                        # watch your latest owned olminstall PipelineRun
#   ./run-olminstall.sh --watch                                # same as above (explicit watch mode)
#   ./run-olminstall.sh --watch odh-olminstall-smoke-testops-abcde
#       # watch a specific PipelineRun (prints tail logs if already finished)
#   ./run-olminstall.sh --list-pipelines
#   ./run-olminstall.sh --list-pipelines 20
#       # list latest PipelineRuns for selected app (default: 10)
#   ./run-olminstall.sh --product rhoai                        # trigger/reattach flow with latest FBCF across RHOAI apps
#   ./run-olminstall.sh --image quay.io/rhoai/rhoai-fbc-fragment@sha256:abc123
#   ./run-olminstall.sh --app rhoai-fbc-fragment-ocp-421       # trigger on the real build app
#   ./run-olminstall.sh --konflux-repo https://github.com/you/fork.git --konflux-branch my-branch
#       # override Tekton clone of integration-test scripts (default: upstream main in pipeline)
#   ./run-olminstall.sh --channel beta
#       # override UPDATE_CHANNEL (default auto: odh-stable for ODH, stable-3.x for rhoai-v3*, else pipeline default)
#   ./run-olminstall.sh --product rhoai --version 3.5
#       # resolve latest FBCF image from the rhoai-v3-5* Konflux app (instead of ocp-421)

set -euo pipefail

NAMESPACE="rhoai-tenant"
APP="testops-playpen"
KONFLUX_UI="${KONFLUX_UI:-https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SNAPSHOT_FILE="${SCRIPT_DIR}/test-snapshot.yaml"
ITS_FILE="${SCRIPT_DIR}/its-olminstall-rhoai-tenant.yaml"
IMAGE=""          # empty = fetch latest automatically
SNAPSHOT_NAME=""  # tracked for cleanup
PIPELINE_EXIT=0
KONFLUX_REPO_OVERRIDE=""
KONFLUX_BRANCH_OVERRIDE=""
UPDATE_CHANNEL_OVERRIDE=""
PRODUCT="rhoai"
VERSION=""  # e.g. "3.5" → lookup rhoai-v3-5* apps for latest FBC snapshot
ITS_APPLY_TMP=""
LOG_FILE=""
RUN_OWNER=""
WATCH_MODE=""
WATCH_PIPELINERUN=""
PR=""
CLEANUP_SNAPSHOT_ON_EXIT="yes"
PR_APPEAR_TIMEOUT_SECONDS="${PR_APPEAR_TIMEOUT_SECONDS:-600}"
WATCH_COMPLETED=""
LIST_PIPELINES_MODE=""
LIST_PIPELINES_COUNT="10"
KA_HOST="${KA_HOST:-https://kubearchive-api-server-product-kubearchive.apps.stone-prod-p02.hjvn.p1.openshiftapps.com}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Trigger and watch the olminstall smoke pipeline in Konflux.
Default (no args): watch your latest owned olminstall PipelineRun.
Use trigger options (for example --product/--image) to start a new run when needed.

Options:
  --watch [PIPELINERUN]
      Watch mode. Falls back to KubeArchive for runs pruned from the cluster.
      - no value: watch your latest owned olminstall PipelineRun
      - with value: watch that specific PipelineRun
  --list-pipelines [N]
      List latest PipelineRuns for selected app (--app; default: testops-playpen)
      in namespace (default: 10). Includes archived runs from KubeArchive.
      Accepts optional positive integer N.

  --image IMAGE
      FBCF container image to test (default: auto-fetch latest)
  --app APP
      Konflux application to target (default: testops-playpen)
  --namespace NS, -n NS
      Konflux namespace (default: rhoai-tenant)
  --product NAME
      Product stream: rhoai|odh (default: rhoai)
  --version VER, --rhoai-version VER
      Resolve FBCF from a specific RHOAI stream (for example 3.5, 3.4, 3.4-ea.2)
      Valid only with --product rhoai; default is latest across all rhoai-v* apps.
  --channel NAME
      OLM UPDATE_CHANNEL override (for example stable, beta, fast-3.x, odh-stable)
      Auto default: odh-stable for --product odh; stable-3.x for rhoai-v3*.
  --konflux-repo URL
      Override scripts/pipeline git URL in ITS (requires yq)
  --konflux-branch REF
      Override scripts/pipeline revision in ITS (requires yq; use with --konflux-repo)

  --help, -h
      Show this help

Environment variables:
  KA_HOST   KubeArchive API URL (default: auto-detected from Konflux cluster)
  PR_APPEAR_TIMEOUT_SECONDS  Timeout waiting for PipelineRun to appear (default: 600)

Notes:
  - Value flags accept both '--flag value' and '--flag=value' forms.
  - Value-optional flags: '--watch', '--list-pipelines'.
  - Boolean flags: '--help'.
  - Archived PipelineRuns (pruned by cluster GC) are retrieved via KubeArchive.
EOF
}

require_arg_value() {
  if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
    echo "❌ Missing value for $1"
    usage
    exit 1
  fi
}

if [[ $# -eq 0 ]]; then
  WATCH_MODE="yes"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image=*)       IMAGE="${1#*=}"; [[ -z "${IMAGE}" ]] && { echo "❌ Missing value for --image"; usage; exit 1; }; shift 1 ;;
    --image)         require_arg_value "$1" "${2:-}"; IMAGE="$2"; shift 2 ;;
    --app=*)         APP="${1#*=}"; [[ -z "${APP}" ]] && { echo "❌ Missing value for --app"; usage; exit 1; }; shift 1 ;;
    --app)           require_arg_value "$1" "${2:-}"; APP="$2"; shift 2 ;;
    --namespace=*)   NAMESPACE="${1#*=}"; [[ -z "${NAMESPACE}" ]] && { echo "❌ Missing value for --namespace"; usage; exit 1; }; shift 1 ;;
    --namespace|-n)  require_arg_value "$1" "${2:-}"; NAMESPACE="$2"; shift 2 ;;
    -n=*)            NAMESPACE="${1#*=}"; [[ -z "${NAMESPACE}" ]] && { echo "❌ Missing value for -n"; usage; exit 1; }; shift 1 ;;
    --konflux-repo=*) KONFLUX_REPO_OVERRIDE="${1#*=}"; [[ -z "${KONFLUX_REPO_OVERRIDE}" ]] && { echo "❌ Missing value for --konflux-repo"; usage; exit 1; }; shift 1 ;;
    --konflux-repo)  require_arg_value "$1" "${2:-}"; KONFLUX_REPO_OVERRIDE="$2"; shift 2 ;;
    --konflux-branch=*) KONFLUX_BRANCH_OVERRIDE="${1#*=}"; [[ -z "${KONFLUX_BRANCH_OVERRIDE}" ]] && { echo "❌ Missing value for --konflux-branch"; usage; exit 1; }; shift 1 ;;
    --konflux-branch) require_arg_value "$1" "${2:-}"; KONFLUX_BRANCH_OVERRIDE="$2"; shift 2 ;;
    --channel=*)     UPDATE_CHANNEL_OVERRIDE="${1#*=}"; [[ -z "${UPDATE_CHANNEL_OVERRIDE}" ]] && { echo "❌ Missing value for --channel"; usage; exit 1; }; shift 1 ;;
    --channel)       require_arg_value "$1" "${2:-}"; UPDATE_CHANNEL_OVERRIDE="$2"; shift 2 ;;
    --watch=*)
      WATCH_MODE="yes"
      WATCH_PIPELINERUN="${1#*=}"
      [[ -z "${WATCH_PIPELINERUN}" ]] && { echo "❌ Missing value for --watch"; usage; exit 1; }
      shift 1
      ;;
    --watch)
      WATCH_MODE="yes"
      if [[ $# -ge 2 && -n "${2:-}" && "${2:-}" != --* ]]; then
        WATCH_PIPELINERUN="$2"
        shift 2
      else
        shift 1
      fi
      ;;
    --list-pipelines=*)
      LIST_PIPELINES_MODE="yes"
      LIST_PIPELINES_COUNT="${1#*=}"
      [[ -z "${LIST_PIPELINES_COUNT}" ]] && { echo "❌ Missing value for --list-pipelines"; usage; exit 1; }
      shift 1
      ;;
    --list-pipelines)
      LIST_PIPELINES_MODE="yes"
      if [[ $# -ge 2 && -n "${2:-}" && "${2:-}" != --* ]]; then
        LIST_PIPELINES_COUNT="$2"
        shift 2
      else
        shift 1
      fi
      ;;
    --product=*)     PRODUCT="${1#*=}"; [[ -z "${PRODUCT}" ]] && { echo "❌ Missing value for --product"; usage; exit 1; }; shift 1 ;;
    --product)       require_arg_value "$1" "${2:-}"; PRODUCT="$2"; shift 2 ;;
    --version=*|--rhoai-version=*) VERSION="${1#*=}"; [[ -z "${VERSION}" ]] && { echo "❌ Missing value for --version"; usage; exit 1; }; shift 1 ;;
    --version|--rhoai-version) require_arg_value "$1" "${2:-}"; VERSION="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ -n "${WATCH_PIPELINERUN}" && "${WATCH_PIPELINERUN}" == --* ]]; then
  echo "❌ Invalid --watch value: ${WATCH_PIPELINERUN}"
  usage
  exit 1
fi
if [[ -n "${LIST_PIPELINES_MODE}" && ! "${LIST_PIPELINES_COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ --list-pipelines expects a positive integer (got: ${LIST_PIPELINES_COUNT})"
  usage
  exit 1
fi

if [[ "${PRODUCT}" != "rhoai" && "${PRODUCT}" != "odh" ]]; then
  echo "❌ --product must be one of: rhoai, odh"
  exit 1
fi
if [[ -n "${VERSION}" && "${PRODUCT}" != "rhoai" ]]; then
  echo "--version is supported only with --product rhoai"
  exit 1
fi

# ── Apply product-specific pipeline params ────────────────────────────────────
# Both rhoai and odh use the rhoai-tenant/testops-playpen sandbox ITS for manual
# runs.  Product-specific params (operator name, namespace, component) are
# injected at ITS-apply time below.
ODH_PARAM_OVERRIDES=""
if [[ "${PRODUCT}" == "odh" ]]; then
  ODH_PARAM_OVERRIDES="yes"
fi
echo "Product: ${PRODUCT}  Namespace: ${NAMESPACE}  App: ${APP}"

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
  set +e
  CLEANUP_PRINTED=""
  if [[ "${CLEANUP_SNAPSHOT_ON_EXIT}" == "yes" && -n "${SNAPSHOT_NAME}" ]]; then
    [[ -z "${CLEANUP_PRINTED}" ]] && { echo ""; echo "── Cleaning up ──"; CLEANUP_PRINTED="yes"; }
    oc delete snapshot "${SNAPSHOT_NAME}" -n "${NAMESPACE}" --ignore-not-found &>/dev/null \
      && echo "  Deleted Snapshot ${SNAPSHOT_NAME}" || true
  elif [[ -n "${SNAPSHOT_NAME}" ]]; then
    [[ -z "${CLEANUP_PRINTED}" ]] && { echo ""; echo "── Cleaning up ──"; CLEANUP_PRINTED="yes"; }
    echo "  Keeping Snapshot ${SNAPSHOT_NAME} for delayed trigger/debug"
  fi
  if [[ -n "${ITS_APPLY_TMP}" && -f "${ITS_APPLY_TMP}" ]]; then
    rm -f "${ITS_APPLY_TMP}"
  fi
  if [[ -n "${LOG_FILE}" && -f "${LOG_FILE}" ]]; then
    rm -f "${LOG_FILE}"
  fi
  true
}
trap cleanup EXIT

# Tekton v1 PipelineRun: never use status.conditions[0] — order is not stable.
# Read the condition with type=="Succeeded" (status True/False/Unknown).
pr_succeeded_state() {
  oc get pipelinerun "${1}" -n "${2}" -o json 2>/dev/null | jq -r '
    ((.status.conditions // []) | map(select(.type=="Succeeded")) | first) as $c
    | if $c == null then "Unknown\t"
      else ($c.status // "Unknown") + "\t" + ($c.reason // "")
      end
  '
}

# KubeArchive REST helper — archives PipelineRuns/TaskRuns/pods after cluster GC.
# Returns empty string on failure so callers can fall through gracefully.
KA_AVAILABLE=""
ka_get() {
  local path="$1"
  curl -sf -H "Authorization: Bearer $(oc whoami -t)" "${KA_HOST}${path}" 2>/dev/null || true
}
ka_check() {
  if [[ -z "${KA_AVAILABLE}" ]]; then
    if ka_get "/livez" | jq -e '.code == 200' &>/dev/null; then
      KA_AVAILABLE="yes"
    else
      KA_AVAILABLE="no"
      echo "⚠️  KubeArchive API unreachable (${KA_HOST}); archived runs will not be shown."
    fi
  fi
  [[ "${KA_AVAILABLE}" == "yes" ]]
}

# Replay logs for an archived PipelineRun via KubeArchive REST API.
# Fetches child TaskRuns → pods → container logs, printed task-by-task.
ka_replay_logs() {
  local pr_name="$1" ns="$2"
  local pr_json task_refs
  pr_json=$(ka_get "/apis/tekton.dev/v1/namespaces/${ns}/pipelineruns/${pr_name}")
  [[ -z "${pr_json}" ]] && { echo "❌ Could not fetch PipelineRun '${pr_name}' from KubeArchive."; return 1; }

  task_refs=$(echo "${pr_json}" | jq -r '(.status.childReferences // [])[] | [.name, .pipelineTaskName] | @tsv')
  if [[ -z "${task_refs}" ]]; then
    echo "  (no child TaskRuns found in archived PipelineRun)"
    return 0
  fi

  while IFS=$'\t' read -r tr_name task_name; do
    [[ -z "${tr_name}" ]] && continue
    local pod_name
    pod_name=$(ka_get "/api/v1/namespaces/${ns}/pods?labelSelector=tekton.dev/taskRun=${tr_name}" \
      | jq -r '.items[0].metadata.name // empty' 2>/dev/null)
    if [[ -z "${pod_name}" ]]; then
      echo "[${task_name}] (no pod found)"
      continue
    fi

    local containers
    containers=$(ka_get "/api/v1/namespaces/${ns}/pods/${pod_name}" \
      | jq -r '[(.spec.initContainers // [])[], (.spec.containers // [])[]] | .[].name' 2>/dev/null)
    for ctr in ${containers}; do
      [[ "${ctr}" == "prepare" || "${ctr}" == "place-scripts" || "${ctr}" == "place-tools" ]] && continue
      echo ""
      echo "[${task_name} : ${ctr}]"
      ka_get "/api/v1/namespaces/${ns}/pods/${pod_name}/log?container=${ctr}"
    done
  done <<< "${task_refs}"
}

# ── Check login ────────────────────────────────────────────────────────────────
if ! oc whoami &>/dev/null; then
  echo "❌ Not logged in. Run: oc login --server=<api-url> --web"
  exit 1
fi
echo "✓ Logged in as $(oc whoami)"
RUN_OWNER="$(oc whoami)"

if [[ -n "${LIST_PIPELINES_MODE}" ]]; then
  echo "Latest ${LIST_PIPELINES_COUNT} PipelineRuns for app '${APP}' in namespace '${NAMESPACE}':"
  LIVE_ROWS=$(oc get pipelineruns -n "${NAMESPACE}" --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
    | jq -r --arg app "${APP}" --argjson n "${LIST_PIPELINES_COUNT}" '
        [.items[] | {
          name: .metadata.name,
          app: (.metadata.labels["appstudio.openshift.io/application"] // "-"),
          state: (if .status.completionTime then "completed" else "running" end),
          created: .metadata.creationTimestamp,
          source: "live"
        } | select(.app == $app)]
        | sort_by(.created)
        | reverse
        | .[:$n]
        | .[]
        | [ .name, .app, .state, .created, .source ] | @tsv' \
    || true)

  KA_ROWS=""
  LIVE_COUNT=$(printf '%s\n' "${LIVE_ROWS}" | awk 'NF' | wc -l | tr -d ' ')
  NEED_ARCHIVED=$(( LIST_PIPELINES_COUNT - LIVE_COUNT ))
  if [[ "${NEED_ARCHIVED}" -gt 0 ]] && ka_check; then
    # Keep request bounded; ask KubeArchive only for what we may need (+N for dedupe headroom).
    KA_FETCH_COUNT=$(( NEED_ARCHIVED + LIST_PIPELINES_COUNT ))
    KA_ROWS=$(ka_get "/apis/tekton.dev/v1/namespaces/${NAMESPACE}/pipelineruns?labelSelector=appstudio.openshift.io/application=${APP}&limit=${KA_FETCH_COUNT}" \
      | jq -r --argjson n "${KA_FETCH_COUNT}" '
          [(.items // [])[] | {
            name: .metadata.name,
            app: (.metadata.labels["appstudio.openshift.io/application"] // "-"),
            state: (
              ((.status.conditions // []) | map(select(.type=="Succeeded")) | first) as $c
              | if $c == null then "unknown"
                elif $c.status == "True" then "completed"
                elif $c.status == "False" then "failed"
                else "running" end),
            created: .metadata.creationTimestamp,
            source: "archived"
          }]
          | sort_by(.created)
          | reverse
          | .[:$n]
          | .[]
          | [ .name, .app, .state, .created, .source ] | @tsv' \
      2>/dev/null || true)
  fi

  MERGED_ROWS=$(printf '%s\n%s' "${LIVE_ROWS}" "${KA_ROWS}" \
    | awk -F'\t' 'NF && !seen[$1]++ { print }' \
    | sort -t$'\t' -k4 -r \
    | head -n "${LIST_PIPELINES_COUNT}")

  if [[ -z "${MERGED_ROWS}" ]]; then
    echo "No PipelineRuns found for app '${APP}'."
    echo "Tip: use --app <name> to target another application."
  else
    {
      echo -e "NAME\tAPP\tSTATE\tCREATED\tSOURCE"
      echo "${MERGED_ROWS}"
    } | column -t -s $'\t'
  fi
  exit 0
fi

# ── Verify this is a Konflux cluster (has IntegrationTestScenario CRD) ─────────
KONFLUX_SERVER="https://api.stone-prod-p02.hjvn.p1.openshiftapps.com:6443"
if ! oc api-resources --api-group=appstudio.redhat.com 2>/dev/null | grep -q "IntegrationTestScenario"; then
  echo ""
  echo "⚠️  The current cluster ($(oc whoami --show-server)) does not have the"
  echo "   Konflux IntegrationTestScenario CRD (appstudio.redhat.com)."
  echo "   This script must run against the Konflux tenant cluster."
  if [[ -t 0 ]]; then
    read -r -p "   Log in to ${KONFLUX_SERVER} now? [Y/n] " _ans
    _ans="${_ans:-Y}"
  else
    _ans="Y"
  fi
  if [[ "${_ans}" =~ ^[Yy]$ ]]; then
    echo "   Running: oc login --server=${KONFLUX_SERVER} --web"
    oc login --server="${KONFLUX_SERVER}" --web
    if ! oc api-resources --api-group=appstudio.redhat.com 2>/dev/null | grep -q "IntegrationTestScenario"; then
      echo "❌ Still no IntegrationTestScenario CRD after login. Aborting."
      exit 1
    fi
    echo "✓ Re-logged in as $(oc whoami) on Konflux cluster"
  else
    echo "❌ Aborting — not connected to a Konflux cluster."
    exit 1
  fi
fi

WATCH_FROM_ARCHIVE=""
if [[ -n "${WATCH_MODE}" ]]; then
  if [[ -n "${WATCH_PIPELINERUN}" ]]; then
    echo "Watch mode: explicit PipelineRun '${WATCH_PIPELINERUN}'"
    if oc get pipelinerun "${WATCH_PIPELINERUN}" -n "${NAMESPACE}" &>/dev/null; then
      PR="${WATCH_PIPELINERUN}"
    elif ka_check; then
      KA_PR_JSON=$(ka_get "/apis/tekton.dev/v1/namespaces/${NAMESPACE}/pipelineruns/${WATCH_PIPELINERUN}")
      KA_PR_NAME=$(echo "${KA_PR_JSON}" | jq -r '.metadata.name // empty' 2>/dev/null)
      if [[ -n "${KA_PR_NAME}" ]]; then
        PR="${KA_PR_NAME}"
        WATCH_FROM_ARCHIVE="yes"
        WATCH_COMPLETED="yes"
        echo "↪ PipelineRun found in KubeArchive (pruned from live cluster)."
      else
        echo "❌ PipelineRun not found in namespace '${NAMESPACE}' or in KubeArchive: ${WATCH_PIPELINERUN}"
        exit 1
      fi
    else
      echo "❌ PipelineRun not found in namespace '${NAMESPACE}': ${WATCH_PIPELINERUN}"
      exit 1
    fi
  else
    echo "Watch mode: looking for your latest owned olminstall PipelineRun (app: ${APP}, owner: ${RUN_OWNER})..."
    WATCH_CANDIDATES=$(oc get pipelineruns -n "${NAMESPACE}" --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
      | jq -r --arg app "${APP}" '
          .items[]
          | select(.metadata.name | test("olminstall"))
          | select((.metadata.labels["appstudio.openshift.io/application"] // "") == $app)
          | [ .metadata.name,
              ((.spec.params // [] | map(select(.name == "SNAPSHOT") | .value) | first) // ""),
              (.metadata.annotations["olminstall.run-owner"] // "") ]
          | @tsv' \
      | tac || true)

    while IFS=$'\t' read -r _pr _snapshot _pr_owner; do
      [[ -z "${_pr}" ]] && continue
      if [[ "${_pr_owner}" == "${RUN_OWNER}" ]]; then
        PR="${_pr}"
        break
      fi
      [[ -z "${_snapshot}" ]] && continue
      _owner=$(oc get snapshot "${_snapshot}" -n "${NAMESPACE}" \
        -o jsonpath='{.metadata.annotations.olminstall\.run-owner}' 2>/dev/null || true)
      if [[ "${_owner}" == "${RUN_OWNER}" ]]; then
        PR="${_pr}"
        break
      fi
    done <<< "${WATCH_CANDIDATES}"

    # Fallback: search KubeArchive for archived owned runs
    if [[ -z "${PR}" ]] && ka_check; then
      echo "  No live PipelineRun found — searching KubeArchive..."
      KA_OWNED_PR=$(ka_get "/apis/tekton.dev/v1/namespaces/${NAMESPACE}/pipelineruns?labelSelector=appstudio.openshift.io/application=${APP}" \
        | jq -r --arg owner "${RUN_OWNER}" '
            [(.items // [])[]
             | select(.metadata.name | test("olminstall"))
             | select((.metadata.annotations["olminstall.run-owner"] // "") == $owner)
            ] | sort_by(.metadata.creationTimestamp) | last | .metadata.name // empty' \
        2>/dev/null || true)
      if [[ -n "${KA_OWNED_PR}" ]]; then
        PR="${KA_OWNED_PR}"
        WATCH_FROM_ARCHIVE="yes"
        WATCH_COMPLETED="yes"
        echo "  ↪ Found archived owned PipelineRun: ${PR}"
      fi
    fi

    if [[ -z "${PR}" ]]; then
      echo "❌ No owned olminstall PipelineRun found for app '${APP}' (live or archived)."
      echo "   Use '--watch <pipelinerun>' to target a specific run, or run with trigger flags (for example --product rhoai)."
      exit 1
    fi
    [[ -z "${WATCH_FROM_ARCHIVE}" ]] && echo "↪ Found latest owned PipelineRun: ${PR}"
  fi

  if [[ -n "${WATCH_FROM_ARCHIVE}" ]]; then
    KA_PR_DATA=$(ka_get "/apis/tekton.dev/v1/namespaces/${NAMESPACE}/pipelineruns/${PR}")
    WATCH_APP=$(echo "${KA_PR_DATA}" | jq -r '.metadata.labels["appstudio.openshift.io/application"] // empty' 2>/dev/null)
    WATCH_COMPLETION_TIME=$(echo "${KA_PR_DATA}" | jq -r '.status.completionTime // empty' 2>/dev/null)
    KA_SUCCEEDED=$(echo "${KA_PR_DATA}" | jq -r '
      ((.status.conditions // []) | map(select(.type=="Succeeded")) | first) as $c
      | if $c == null then "Unknown"
        elif $c.status == "True" then "Succeeded"
        elif $c.status == "False" then "Failed"
        else "Unknown" end' 2>/dev/null)
  else
    WATCH_APP=$(oc get pipelinerun "${PR}" -n "${NAMESPACE}" \
      -o jsonpath='{.metadata.labels.appstudio\.openshift\.io/application}' 2>/dev/null || true)
    WATCH_COMPLETION_TIME=$(oc get pipelinerun "${PR}" -n "${NAMESPACE}" \
      -o jsonpath='{.status.completionTime}' 2>/dev/null || true)
  fi

  if [[ -n "${WATCH_APP}" && "${WATCH_APP}" != "${APP}" ]]; then
    echo "⚠️  PipelineRun app label is '${WATCH_APP}', while --app is '${APP}'. Continuing with selected run."
  fi

  if [[ -n "${WATCH_COMPLETION_TIME}" && -z "${WATCH_FROM_ARCHIVE}" ]]; then
    WATCH_COMPLETED="yes"
    echo "ℹ️  PipelineRun ${PR} is already completed (completionTime=${WATCH_COMPLETION_TIME}). Showing recent logs/status."
  elif [[ -n "${WATCH_FROM_ARCHIVE}" ]]; then
    echo "ℹ️  PipelineRun ${PR} is archived (${KA_SUCCEEDED:-unknown}, completionTime=${WATCH_COMPLETION_TIME:-?}). Replaying logs from KubeArchive."
  fi
else
  # ── Check for an already-running olminstall PipelineRun for THIS app ──────────
  # Preference order:
  # 1) run explicitly marked with this user (PipelineRun or Snapshot annotation)
  # 2) otherwise newest running run for the same app (avoid duplicate triggers)
  echo "Checking for running olminstall PipelineRun (app: ${APP}, owner: ${RUN_OWNER})..."
  RUN_CANDIDATES=$(oc get pipelineruns -n "${NAMESPACE}" --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
    | jq -r --arg app "${APP}" '
        .items[]
        | select(.metadata.name | test("olminstall"))
        | select(.status.completionTime == null)
        | select((.metadata.labels["appstudio.openshift.io/application"] // "") == $app)
        | [ .metadata.name,
            ((.spec.params // [] | map(select(.name == "SNAPSHOT") | .value) | first) // ""),
            (.metadata.annotations["olminstall.run-owner"] // "") ]
        | @tsv' \
    | tac || true)

  FALLBACK_PR=""
  while IFS=$'\t' read -r _pr _snapshot _pr_owner; do
    [[ -z "${_pr}" ]] && continue
    [[ -z "${FALLBACK_PR}" ]] && FALLBACK_PR="${_pr}"
    if [[ "${_pr_owner}" == "${RUN_OWNER}" ]]; then
      PR="${_pr}"
      break
    fi
    [[ -z "${_snapshot}" ]] && continue
    _owner=$(oc get snapshot "${_snapshot}" -n "${NAMESPACE}" \
      -o jsonpath='{.metadata.annotations.olminstall\.run-owner}' 2>/dev/null || true)
    if [[ "${_owner}" == "${RUN_OWNER}" ]]; then
      PR="${_pr}"
      break
    fi
  done <<< "${RUN_CANDIDATES}"

  if [[ -n "${PR}" ]]; then
    echo "↪ Found running PipelineRun for app '${APP}' owned by '${RUN_OWNER}': ${PR} — attaching..."
  elif [[ -n "${FALLBACK_PR}" ]]; then
    PR="${FALLBACK_PR}"
    echo "↪ Found running PipelineRun for app '${APP}' but owner marker is unavailable — attaching to latest: ${PR}"
  else
  # ── Resolve FBCF image ───────────────────────────────────────────────────────
  if [[ -n "${IMAGE}" ]]; then
    echo "✓ Using provided image: ${IMAGE}"
  elif [[ "${PRODUCT}" == "rhoai" && -n "${VERSION}" ]]; then
    APP_PREFIX="rhoai-v${VERSION//./-}"
    echo "Resolving latest FBCF image for RHOAI ${VERSION} (apps matching ${APP_PREFIX}*)..."
    # Resolve exact app names first (fast), then query per-app with label selector (fast).
    # Scanning all snapshots without a label filter is too slow for large namespaces.
    MATCHING_APPS=$(oc get applications -n rhoai-tenant \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
      | tr ' ' '\n' \
      | grep -E "^${APP_PREFIX}(-|$)" \
      || true)
    if [[ -z "${MATCHING_APPS}" ]]; then
      echo "❌ No Konflux application found matching ${APP_PREFIX}* in rhoai-tenant"
      exit 1
    fi
    IMAGE=""; BEST_TS=""; RESOLVED_APP=""
    while IFS= read -r _app; do
      [[ -z "${_app}" ]] && continue
      _result=$(oc get snapshots -n rhoai-tenant \
        -l "appstudio.openshift.io/application=${_app}" -o json 2>/dev/null \
        | jq -r '
            [ .items[]
              | { ts: .metadata.creationTimestamp,
                  img: (.spec.components[]?
                        | select(.containerImage | test("rhoai-fbc-fragment@"))
                        | .containerImage) }
              | select(.img)
            ] | if length > 0 then sort_by(.ts) | last | [.ts, .img] | join("\t") else "" end' \
        || true)
      [[ -z "${_result}" ]] && continue
      _ts="${_result%%	*}"; _img="${_result##*	}"
      if [[ -z "${BEST_TS}" || "${_ts}" > "${BEST_TS}" ]]; then
        IMAGE="${_img}"; BEST_TS="${_ts}"; RESOLVED_APP="${_app}"
      fi
    done <<< "${MATCHING_APPS}"
    if [[ -z "${IMAGE}" ]]; then
      echo "❌ No FBCF snapshot found for RHOAI ${VERSION} (searched ${APP_PREFIX}*)"
      exit 1
    fi
    echo "✓ RHOAI ${VERSION} FBCF image: ${IMAGE} (from ${RESOLVED_APP})"
  elif [[ "${PRODUCT}" == "rhoai" ]]; then
    echo "Fetching latest FBCF image across all RHOAI apps (highest version)..."
    # Resolve app names first (fast), then query per-app with label selector (fast).
    ALL_RHOAI_APPS=$(oc get applications -n rhoai-tenant \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
      | tr ' ' '\n' \
      | grep -E "^rhoai-v" \
      || true)
    IMAGE=""; BEST_TS=""; RESOLVED_APP=""
    while IFS= read -r _app; do
      [[ -z "${_app}" ]] && continue
      _result=$(oc get snapshots -n rhoai-tenant \
        -l "appstudio.openshift.io/application=${_app}" -o json 2>/dev/null \
        | jq -r '
            [ .items[]
              | { ts: .metadata.creationTimestamp,
                  img: (.spec.components[]?
                        | select(.containerImage | test("rhoai-fbc-fragment@"))
                        | .containerImage) }
              | select(.img)
            ] | if length > 0 then sort_by(.ts) | last | [.ts, .img] | join("\t") else "" end' \
        || true)
      [[ -z "${_result}" ]] && continue
      _ts="${_result%%	*}"; _img="${_result##*	}"
      if [[ -z "${BEST_TS}" || "${_ts}" > "${BEST_TS}" ]]; then
        IMAGE="${_img}"; BEST_TS="${_ts}"; RESOLVED_APP="${_app}"
      fi
    done <<< "${ALL_RHOAI_APPS}"
    if [[ -n "${IMAGE}" ]]; then
      echo "✓ Latest FBCF image: ${IMAGE} (from ${RESOLVED_APP})"
    else
      IMAGE=""
      echo "⚠ Could not fetch latest image — falling back to pinned image in test-snapshot.yaml"
    fi
  elif [[ "${PRODUCT}" == "odh" ]]; then
    ODH_CATALOG_REPO="quay.io/opendatahub/opendatahub-operator-catalog"
    ODH_CATALOG_TAG="odh-stable"
    echo "Fetching latest ODH catalog snapshot from open-data-hub-tenant..."
    IMAGE=$(oc get snapshots -n open-data-hub-tenant -o json 2>/dev/null \
      | jq -r '
          [ .items[]
            | select(.metadata.labels["appstudio.openshift.io/application"] == "opendatahub-builds")
            | { ts: .metadata.creationTimestamp,
                img: (.spec.components[]?
                      | select(.containerImage | test("opendatahub-operator-catalog@|odh-operator-catalog@"))
                      | .containerImage) }
            | select(.img)
          ] | sort_by(.ts) | last | .img // empty' || true)
    if [[ -z "${IMAGE}" ]]; then
      echo "  No snapshots found (likely no access to open-data-hub-tenant)"
      echo "  Resolving from ${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG} via skopeo..."
      if command -v skopeo &>/dev/null; then
        ODH_DIGEST=$(skopeo inspect --no-tags "docker://${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG}" 2>/dev/null \
          | jq -r '.Digest // empty')
        if [[ -n "${ODH_DIGEST}" ]]; then
          IMAGE="${ODH_CATALOG_REPO}@${ODH_DIGEST}"
        fi
      fi
      if [[ -z "${IMAGE}" ]]; then
        echo "  skopeo unavailable or inspect failed — using tag reference"
        IMAGE="${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG}"
      fi
    fi
    echo "✓ Latest ODH catalog image: ${IMAGE}"
  fi

  # Auto-select channel unless --channel was provided explicitly.
  if [[ -z "${UPDATE_CHANNEL_OVERRIDE}" && "${PRODUCT}" == "odh" ]]; then
    UPDATE_CHANNEL_OVERRIDE="odh-stable"
    echo "✓ Auto-selected channel: ${UPDATE_CHANNEL_OVERRIDE} (product=${PRODUCT})"
  elif [[ -z "${UPDATE_CHANNEL_OVERRIDE}" && "${RESOLVED_APP:-}" == rhoai-v3-* ]]; then
    UPDATE_CHANNEL_OVERRIDE="stable-3.x"
    echo "✓ Auto-selected channel: ${UPDATE_CHANNEL_OVERRIDE} (from ${RESOLVED_APP})"
  fi

  # ── Ensure ITS CR is applied (idempotent) ───────────────────────────────────
  NEED_YQ=""
  [[ -n "${KONFLUX_REPO_OVERRIDE}" || -n "${KONFLUX_BRANCH_OVERRIDE}" || -n "${UPDATE_CHANNEL_OVERRIDE}" || -n "${ODH_PARAM_OVERRIDES}" ]] && NEED_YQ="yes"

  echo "Ensuring IntegrationTestScenario is applied..."
  if [[ -n "${NEED_YQ}" ]]; then
    if ! command -v yq &>/dev/null; then
      echo "❌ yq (https://github.com/mikefarah/yq) is required for --konflux-repo / --konflux-branch / --channel / --product odh."
      exit 1
    fi
    ITS_APPLY_TMP="$(mktemp)"

    # Build a dynamic list of params to delete — only remove what we will re-add.
    DEL_NAMES=()
    [[ -n "${KONFLUX_REPO_OVERRIDE}" ]]   && DEL_NAMES+=("SCRIPTS_REPO_URL")
    [[ -n "${KONFLUX_BRANCH_OVERRIDE}" ]] && DEL_NAMES+=("SCRIPTS_REPO_REVISION")
    [[ -n "${UPDATE_CHANNEL_OVERRIDE}" ]] && DEL_NAMES+=("UPDATE_CHANNEL")
    if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
      DEL_NAMES+=("OPERATOR_NAME" "OPERATOR_NAMESPACE" "FBCF_COMPONENT_NAME")
    fi

    if [[ ${#DEL_NAMES[@]} -gt 0 ]]; then
      DEL_EXPR=$(printf ' or .name == "%s"' "${DEL_NAMES[@]}")
      DEL_EXPR="${DEL_EXPR:4}"  # strip leading " or "
      yq e "del(.spec.params[] | select(${DEL_EXPR}))" "${ITS_FILE}" > "${ITS_APPLY_TMP}"
    else
      cp "${ITS_FILE}" "${ITS_APPLY_TMP}"
    fi

    if [[ -n "${KONFLUX_REPO_OVERRIDE}" ]]; then
      YQ_SCRIPTS_URL="${KONFLUX_REPO_OVERRIDE}" \
        yq e '.spec.params += [{"name":"SCRIPTS_REPO_URL","value":strenv(YQ_SCRIPTS_URL)}]' -i "${ITS_APPLY_TMP}"
      YQ_RESOLVER_URL="${KONFLUX_REPO_OVERRIDE}" \
        yq e '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_RESOLVER_URL)' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${KONFLUX_BRANCH_OVERRIDE}" ]]; then
      YQ_SCRIPTS_REV="${KONFLUX_BRANCH_OVERRIDE}" \
        yq e '.spec.params += [{"name":"SCRIPTS_REPO_REVISION","value":strenv(YQ_SCRIPTS_REV)}]' -i "${ITS_APPLY_TMP}"
      YQ_RESOLVER_REV="${KONFLUX_BRANCH_OVERRIDE}" \
        yq e '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_RESOLVER_REV)' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${UPDATE_CHANNEL_OVERRIDE}" ]]; then
      YQ_UPDATE_CHANNEL="${UPDATE_CHANNEL_OVERRIDE}" \
        yq e '.spec.params += [{"name":"UPDATE_CHANNEL","value":strenv(YQ_UPDATE_CHANNEL)}]' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
      yq e '.spec.params += [{"name":"OPERATOR_NAME","value":"opendatahub-operator"}]' -i "${ITS_APPLY_TMP}"
      yq e '.spec.params += [{"name":"OPERATOR_NAMESPACE","value":"opendatahub-operators"}]' -i "${ITS_APPLY_TMP}"
      yq e '.spec.params += [{"name":"FBCF_COMPONENT_NAME","value":"odh-operator-catalog"}]' -i "${ITS_APPLY_TMP}"
    fi
    echo "  ITS overrides: resolverRef=${KONFLUX_REPO_OVERRIDE:-<default>}@${KONFLUX_BRANCH_OVERRIDE:-<default>}" \
         " SCRIPTS_REPO=${KONFLUX_REPO_OVERRIDE:-<default>}@${KONFLUX_BRANCH_OVERRIDE:-<default>}" \
         " UPDATE_CHANNEL=${UPDATE_CHANNEL_OVERRIDE:-<pipeline default>}" \
         " PRODUCT=${PRODUCT}"
    # Pipe through grep to suppress harmless Warning lines; check oc exit code via PIPESTATUS.
    oc apply -n "${NAMESPACE}" -f "${ITS_APPLY_TMP}" 2>&1 | grep -v "^Warning" >&2
    [[ ${PIPESTATUS[0]} -ne 0 ]] && { echo "❌ ITS apply failed"; exit 1; }
  else
    oc apply -n "${NAMESPACE}" -f "${ITS_FILE}" 2>&1 | grep -v "^Warning" >&2
    [[ ${PIPESTATUS[0]} -ne 0 ]] && { echo "❌ ITS apply failed"; exit 1; }
  fi
  echo "✓ ITS ready"

  # ── Create Snapshot (patch app/image/component on the fly, file is never modified) ─
  SNAPSHOT_YAML=$(sed "s|application:.*|application: ${APP}|" "${SNAPSHOT_FILE}")
  [[ -n "${IMAGE}" ]] && SNAPSHOT_YAML=$(echo "${SNAPSHOT_YAML}" \
    | sed "s|containerImage:.*|containerImage: ${IMAGE}|")
  if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
    SNAPSHOT_YAML=$(echo "${SNAPSHOT_YAML}" \
      | sed 's|name: rhoai-fbc-fragment-ocp-421|name: odh-operator-catalog|')
  fi
  echo "Creating Snapshot to trigger pipeline (app: ${APP})..."
  SNAPSHOT_NAME=$(echo "${SNAPSHOT_YAML}" | oc create -n "${NAMESPACE}" -f - \
    -o jsonpath='{.metadata.name}')
  oc annotate snapshot "${SNAPSHOT_NAME}" -n "${NAMESPACE}" \
    "olminstall.run-owner=${RUN_OWNER}" --overwrite >/dev/null || true
  echo "✓ Snapshot: ${SNAPSHOT_NAME}"
  echo "  Snapshot owner marker: ${RUN_OWNER}"

  # ── Wait for the PipelineRun to appear ───────────────────────────────────────
  PR_WAIT_ATTEMPTS=$(( (PR_APPEAR_TIMEOUT_SECONDS + 4) / 5 ))
  [[ "${PR_WAIT_ATTEMPTS}" -lt 1 ]] && PR_WAIT_ATTEMPTS=1
  echo "Waiting for PipelineRun to start (snapshot: ${SNAPSHOT_NAME})..."
  for i in $(seq 1 "${PR_WAIT_ATTEMPTS}"); do
    PR=$(oc get pipelineruns -n "${NAMESPACE}" \
      --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
      | jq -r --arg app "${APP}" --arg snap "${SNAPSHOT_NAME}" '
          [ .items[]
            | select(.metadata.name | test("olminstall"))
            | select((.metadata.labels["appstudio.openshift.io/application"] // "") == $app)
            | select((.spec.params // [] | map(select(.name == "SNAPSHOT") | .value) | first // "") == $snap)
          ] | last | .metadata.name // empty' \
      || true)
    [[ -n "${PR}" ]] && break

    # If a run is already active for this app, attach to avoid duplicate triggers.
    FALLBACK_DURING_WAIT=$(oc get pipelineruns -n "${NAMESPACE}" \
      --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
      | jq -r --arg app "${APP}" '
          [ .items[]
            | select(.metadata.name | test("olminstall"))
            | select(.status.completionTime == null)
            | select((.metadata.labels["appstudio.openshift.io/application"] // "") == $app)
          ] | last | .metadata.name // empty' \
      || true)
    if [[ -n "${FALLBACK_DURING_WAIT}" ]]; then
      PR="${FALLBACK_DURING_WAIT}"
      echo "↪ No snapshot-linked run yet; attaching to active app run: ${PR}"
      break
    fi

    echo "  waiting... (${i}/${PR_WAIT_ATTEMPTS})"
    sleep 5
  done

  if [[ -z "${PR}" ]]; then
    CLEANUP_SNAPSHOT_ON_EXIT="no"
    echo "❌ PipelineRun did not appear after ${PR_APPEAR_TIMEOUT_SECONDS}s. Check Konflux:"
    echo "   ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/activity/pipelineruns"
    echo "   Tip: rerun the script in a minute; it will try to reattach first."
    exit 1
  fi
  oc annotate pipelinerun "${PR}" -n "${NAMESPACE}" \
    "olminstall.run-owner=${RUN_OWNER}" --overwrite >/dev/null || true
  fi
fi

# ── Print Konflux UI link ─────────────────────────────────────────────────────
echo ""
echo "PipelineRun : ${PR}"
if [[ -n "${WATCH_FROM_ARCHIVE}" ]]; then
  echo "Source      : KubeArchive (pruned from live cluster)"
else
  echo "Logs        : tkn pipelinerun logs ${PR} -n ${NAMESPACE} -f"
fi
echo "Konflux UI  : ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
echo ""

if [[ -n "${WATCH_FROM_ARCHIVE}" ]]; then
  # ── Replay archived logs via KubeArchive ──────────────────────────────────
  LOG_FILE="$(mktemp -t olminstall-run.XXXXXX)"
  chmod 600 "${LOG_FILE}"
  echo "Replaying archived logs from KubeArchive..."
  ka_replay_logs "${PR}" "${NAMESPACE}" | tee "${LOG_FILE}" || true

  OPERATOR_VERSION=$(sed -n 's/.*Operator version[[:space:]]*:[[:space:]]*\([^[:space:]]*\).*/\1/p' "${LOG_FILE}" 2>/dev/null | tail -1 || true)
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo " Summary (archived)"
  echo "═══════════════════════════════════════════════════════════"
  echo "  Pipeline  : ${PR}  [${KA_SUCCEEDED:-unknown}]"
  [[ -n "${OPERATOR_VERSION}" ]] && echo "  Operator  : ${OPERATOR_VERSION}"
  echo "  Konflux UI: ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
  echo "═══════════════════════════════════════════════════════════"
  [[ "${KA_SUCCEEDED}" == "Failed" ]] && PIPELINE_EXIT=1
  exit "${PIPELINE_EXIT}"
fi

# ── Wait for pipeline to leave Pending/Resolving before streaming ─────────────
if [[ -z "${WATCH_COMPLETED}" ]]; then
  WAIT_DEADLINE=$(($(date +%s) + 300))
  WAIT_START=$(date +%s)
  echo "Waiting for pipeline to start running..."
  while [ "$(date +%s)" -lt "$WAIT_DEADLINE" ]; do
    IFS=$'\t' read -r _CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
    case "${CREASON}" in
      ""|PipelineRunPending|ResolvingPipelineRef)
        ELAPSED=$(( $(date +%s) - WAIT_START ))
        echo "  $(date +%H:%M:%S)  ${CREASON:-pending} (${ELAPSED}s)"
        sleep 10 ;;
      *)
        echo "  $(date +%H:%M:%S)  ${CREASON:-starting} — ready to stream"
        break ;;
    esac
  done
  if [ "$(date +%s)" -ge "$WAIT_DEADLINE" ]; then
    echo "❌ Pipeline still pending after 5m. Check Konflux:"
    echo "   ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
    PIPELINE_EXIT=1
    exit 1
  fi
fi

# ── Stream logs (tkn) or poll status ─────────────────────────────────────────
LOG_FILE="$(mktemp -t olminstall-run.XXXXXX)"
chmod 600 "${LOG_FILE}"
ts_prefix() { while IFS= read -r line; do printf '[%s] %s\n' "$(date +%H:%M:%S)" "$line"; done; }
if command -v tkn &>/dev/null; then
  if [[ -n "${WATCH_COMPLETED}" ]]; then
    echo "Pipeline is already finished — showing last 200 log lines via tkn..."
    tkn pipelinerun logs "${PR}" -n "${NAMESPACE}" 2>&1 | tail -n 200 | ts_prefix | tee "${LOG_FILE}" || true
  else
    echo "Streaming logs via tkn (Ctrl-C to detach, pipeline keeps running)..."
    tkn pipelinerun logs "${PR}" -n "${NAMESPACE}" -f 2>&1 | ts_prefix | tee "${LOG_FILE}" || true
    # tkn can return before the PipelineRun CR flips to terminal; wait on type=Succeeded.
    POST_LOG_DEADLINE=$(($(date +%s) + 300))
    while [ "$(date +%s)" -lt "${POST_LOG_DEADLINE}" ]; do
      IFS=$'\t' read -r CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
      case "${CSTAT}" in
        True) break ;;
        False)
          echo "❌ Pipeline failed (${CREASON:-Failed})"
          oc get pipelinerun "${PR}" -n "${NAMESPACE}" -o json \
            | jq -r '(.status.conditions // []) | map(select(.type=="Succeeded")) | first | .message // empty' 2>/dev/null || true
          PIPELINE_EXIT=1
          break ;;
        *) sleep 3 ;;
      esac
    done
  fi
else
  echo "tkn not found — polling status (install tkn for live logs)"
  echo "  https://github.com/tektoncd/cli/releases"
  echo ""
  MAX_WAIT_SECONDS=5400
  POLL_DEADLINE=$(($(date +%s) + MAX_WAIT_SECONDS))
  while [ "$(date +%s)" -lt "${POLL_DEADLINE}" ]; do
    IFS=$'\t' read -r CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
    echo "  $(date +%H:%M:%S)  succeeded-condition: ${CSTAT}  reason: ${CREASON:-?}"
    case "${CSTAT}" in
      True)
        echo "✅ Pipeline succeeded"
        break ;;
      False)
        echo "❌ Pipeline failed (${CREASON:-Failed})"
        oc get pipelinerun "${PR}" -n "${NAMESPACE}" -o json \
          | jq -r '(.status.conditions // []) | map(select(.type=="Succeeded")) | first | .message // empty' 2>/dev/null || true
        PIPELINE_EXIT=1
        break ;;
    esac
    sleep 15
  done
  if [ "$(date +%s)" -ge "${POLL_DEADLINE}" ]; then
    echo "❌ Polling timed out before pipeline reached a terminal state (${MAX_WAIT_SECONDS}s)"
    PIPELINE_EXIT=1
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
IFS=$'\t' read -r FINAL_CSTAT FINAL_STATUS <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
OPERATOR_VERSION=$(sed -n 's/.*Operator version[[:space:]]*:[[:space:]]*\([^[:space:]]*\).*/\1/p' "${LOG_FILE}" 2>/dev/null | tail -1 || true)

echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Summary"
echo "═══════════════════════════════════════════════════════════"
echo "  Pipeline  : ${PR}  [${FINAL_STATUS:-unknown}]"
[[ -n "${OPERATOR_VERSION}" ]] && echo "  Operator  : ${OPERATOR_VERSION}"
echo "  Konflux UI: ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
echo "═══════════════════════════════════════════════════════════"

if [[ "${FINAL_CSTAT}" != "True" ]]; then
  PIPELINE_EXIT=1
fi
exit "${PIPELINE_EXIT}"
