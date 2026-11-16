# Resolved Ticket #1042 — SSO Login Failure After Domain Migration

**Product:** Acme
**Category:** Resolved Ticket
**Status:** Resolved
**Date:** 2024-12-05

## Original Question

> We migrated our company domain from oldco.com to newco.com yesterday. Since then, our employees can't log in via Google SSO. They see "Your domain is not authorized for this workspace." How do we fix this?

## Final Answer

Hi Sarah,

Thanks for reaching out, and apologies for the login trouble after your domain migration.

The error "Your domain is not authorized for this workspace" occurs because SSO authorization on the Acme side is still bound to your old domain. When you migrate domains, our workspace's **SSO domain mapping** must be updated manually on our end.

Here's what we did to resolve your ticket:

1. We verified ownership of the new domain, **newco.com**.
2. We updated your workspace's SSO domain mapping from **oldco.com** to **newco.com**.
3. We confirmed that users with `@newco.com` emails can now authenticate successfully via Google SSO.

**Important for your team:**

- Employees should log in with their **new email addresses** (`@newco.com`).
- Users who logged in with the old domain will need to **log out and log back in** once to refresh their session.
- No data was affected by this change.

Please have a few team members test the login flow. If anyone still sees the error, let us know and we'll investigate right away.

**Resolution summary:** SSO domain mapping updated on the workspace. Login restored for the new domain.

Best,
Marcus, Acme Support