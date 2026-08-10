# Security policy

## Supported versions

Security fixes are applied to the current default branch. Historical snapshots
and unpinned forks are unsupported.

## Reporting a vulnerability

Do not disclose a vulnerability in a public issue. Use GitHub's private
security-advisory form for this repository. Include affected revision,
reproduction steps, impact, and any proposed mitigation.

Do not attach credentials, production data, databases, logs, model artifacts,
or customer/operator information. Revoke any exposed credential before filing.

## Deployment boundary

The bundled API is a loopback application service, not an Internet-facing
security boundary. Remote deployments must add TLS, authentication,
authorization, request limits, audit logging, and network isolation at a
reverse proxy or application gateway.
