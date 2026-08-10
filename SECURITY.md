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

The `fxalpha serve-api` command and the documented launch scripts bind to
`127.0.0.1` by default. Treat any explicit non-loopback `--host` value as an
operator-managed remote deployment and apply the controls above before use.

## MLflow boundary

The supported FXAlpha workflow uses `MlflowClient` with a local `file://`
tracking URI. This repository does not launch an MLflow HTTP tracking server,
AI Gateway, model-serving endpoint, or artifact-serving endpoint. Upstream
MLflow advisories that require those server surfaces are therefore outside the
supported deployment path, but they become relevant if an operator enables
such a service independently. Do not expose an MLflow service based on this
repository without separate authentication, authorization, network isolation,
and an advisory review of the exact installed version.

## Dependency and fork updates

Python and GitHub Actions version updates are proposed by Dependabot, but are
never merged automatically. Major-version changes require a separate
compatibility review. Third-party Git submodule pins are intentionally updated
by the reviewed fork procedure in `docs/THIRD_PARTY_FORKS.md`; a fork's default
branch is not an approved replacement for an FXAlpha integration pin.

## Request, file, and direct-network boundaries

- The isolated daily preflight accepts only `auto`, `YYYYMMDD`, or a valid
  `YYYY-MM-DD` calendar date before it constructs a shell-free argument list.
- GUI files are resolved canonically and must remain descendants of the
  repository's `gui/` directory; sibling-prefix paths and escaping symlinks are
  rejected.
- CORS responses echo only a canonical `null` or loopback HTTP(S) origin, and
  GUI response content types come from a fixed extension map.
- Tushare direct-network probes may bind only to an explicit unicast IPv4
  source address. Wildcard, loopback, link-local, multicast, reserved,
  broadcast, IPv6, and malformed source values are rejected before socket
  creation reaches the bind operation.
- Secret-audit reports emit only a fixed finding category plus the affected
  repository path and, for history checks, the Git object ID. Matched secret
  text and the internal detector label are never included in console output.
