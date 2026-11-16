# API Error Codes

**Product:** Acme API v2
**Category:** Developer / API
**Last updated:** 2024-12-18

## Overview

The Acme API returns standard HTTP status codes plus a JSON error body. Example error response:

```json
{
  "error": {
    "code": "invalid_api_key",
    "message": "The API key provided is invalid or has been revoked.",
    "request_id": "req_9f82c4ad3e"
  }
}
```

## Common error codes

| HTTP Status | Error Code | Description |
|---|---|---|
| 400 | `bad_request` | Malformed request body or query parameters. |
| 400 | `invalid_parameter` | A required parameter is missing or invalid. |
| 401 | `invalid_api_key` | API key is wrong, revoked, or expired. |
| 401 | `missing_auth` | No `Authorization` header was provided. |
| 403 | `insufficient_permissions` | The API key lacks the required scope. |
| 404 | `resource_not_found` | The requested object or endpoint does not exist. |
| 409 | `conflict` | The request conflicts with the current state (e.g., duplicate name). |
| 422 | `validation_error` | Request failed schema validation; see `details`. |
| 429 | `rate_limit_exceeded` | Too many requests — retry after `Retry-After` seconds. |
| 500 | `internal_error` | Unexpected server error. Retry with exponential backoff. |
| 502 | `gateway_error` | Upstream dependency failed. Retry is usually safe. |
| 503 | `service_unavailable` | API is temporarily down for maintenance or overload. |

## Rate limits

- **Free plan**: 60 requests/minute
- **Pro plan**: 300 requests/minute
- **Enterprise**: Custom limits

Rate limit headers are included on every response:

```
X-RateLimit-Limit: 300
X-RateLimit-Remaining: 297
X-RateLimit-Reset: 1734460000
```

When you exceed the limit, we return `429 rate_limit_exceeded` with a `Retry-After` header.

## Handling 5xx errors

Use **exponential backoff**: start with a 1-second wait, then 2, 4, 8 seconds, up to a maximum of 60 seconds. Add jitter to avoid thundering herd retries.

## Troubleshooting 401 errors

1. Verify the header format: `Authorization: Bearer sk_live_abc123...`
2. Check that you're using a **live key** (`sk_live_`) not a test key (`sk_test_`) for production requests.
3. Confirm the key hasn't been revoked in **Settings → API Keys**.
4. For 2-factor protected keys, include the `X-Acme-OTP` header.

## Getting help

Customers on the **Pro plan or higher** receive API support via email at **api-support@acme.io**. Include your `request_id` from the error response to speed up investigation.