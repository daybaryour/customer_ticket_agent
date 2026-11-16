# Outage & Status Page Guide

**Product:** Acme
**Category:** Service Status & Incidents
**Last updated:** 2024-12-20

## Where to check Acme's status

- **Status page**: https://status.acme.io
- **Twitter/X**: @AcmeStatus
- **In-app banner**: services with scheduled maintenance show a banner 24 hours in advance

## How to subscribe to status updates

1. Visit **https://status.acme.io**.
2. Click **Subscribe to updates**.
3. Choose email, SMS, or webhook notifications.
4. Select the components you want alerts for (e.g., **API**, **Dashboard**, **Integrations**).

Webhook payloads are delivered as JSON to your endpoint and include the incident ID, status, and affected components.

## Understanding the status page

| Status | Meaning |
|---|---|
| **Operational** | Service is functioning normally. |
| **Degraded performance** | Service is functional but slower or partially impaired. |
| **Partial outage** | A subset of users or features are unavailable. |
| **Major outage** | Service is down for most or all users. |
| **Under maintenance** | Planned maintenance is in progress. |

## How Acme reports incidents

During an incident, we update the status page **at least every 30 minutes**. Each incident includes:

- A summary of the issue
- Affected components
- Timeline of updates
- A post-incident report (PIR) after resolution

## What to do during an outage

1. **Check the status page first** — if we've posted an incident, there's no need to open a ticket; we're already working on it.
2. Retry failed actions with **exponential backoff** for API integrations.
3. Avoid bulk-importing data until service fully recovers.
4. If no incident is posted but you're experiencing issues, contact support with your account ID, timestamps, and error messages.

## Service credits for outages

If Acme experiences a **major outage lasting more than 24 consecutive hours**, affected paid customers may request a **service credit** proportional to the downtime. Email **billing@acme.io** with the subject "Service Credit Request" after the incident status page is updated to Resolved.

## Planned maintenance windows

Maintenance is scheduled during **Sundays 02:00–06:00 UTC** whenever possible. We typically provide **7 days' notice** on the status page and via email to workspace admins.

## Historical uptime

Monthly uptime reports and the last 90 days of incident history are publicly available at **https://status.acme.io/history**.