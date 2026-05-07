"""Shared defaults for Konflux olminstall tooling."""

DEFAULT_NAMESPACE = "rhoai-tenant"
DEFAULT_APP = "testops-playpen"
DEFAULT_PRODUCT = "rhoai"
PRODUCT_CHOICES = ("rhoai", "odh")
DEFAULT_LIST_COUNT = 10
DEFAULT_KONFLUX_UI = "https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com"
DEFAULT_KA_HOST = "https://kubearchive-api-server-product-kubearchive.apps.stone-prod-p02.hjvn.p1.openshiftapps.com"
KONFLUX_SERVER = "https://api.stone-prod-p02.hjvn.p1.openshiftapps.com:6443"
PENDING_REASONS = {"", "PipelineRunPending", "ResolvingPipelineRef"}
