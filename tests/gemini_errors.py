"""Gemini 429 messages as `str(google.genai.errors.ClientError)` renders them.

Per-minute throttling and a spent daily allowance both say RESOURCE_EXHAUSTED
and "You exceeded your current quota"; only the `quotaId` tells them apart.
"""

_TEMPLATE = (
    "429 RESOURCE_EXHAUSTED. {{'error': {{'code': 429, 'message': 'You exceeded your "
    "current quota, please check your plan and billing details. For more information "
    "on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.\\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: {limit}, model: gemini-2.5-flash\\n"
    "Please retry in {delay}s.', 'status': 'RESOURCE_EXHAUSTED', 'details': "
    "[{{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': "
    "[{{'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests', "
    "'quotaId': '{quota_id}', 'quotaDimensions': {{'location': 'global', "
    "'model': 'gemini-2.5-flash'}}, 'quotaValue': '{limit}'}}]}}, "
    "{{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '{delay}s'}}]}}}}"
)

PER_MINUTE_429 = _TEMPLATE.format(
    limit=10, delay=37, quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
)
PER_DAY_429 = _TEMPLATE.format(
    limit=250, delay=52, quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
)
