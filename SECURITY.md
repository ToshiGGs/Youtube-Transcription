# Security policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. If that option is
unavailable, open a non-sensitive issue asking the maintainer to establish a private
contact channel. Do not place exploit details, credentials, tokens, signed media URLs,
private Discord identifiers, or personal data in a public issue.

Please include the affected version, impact, reproduction conditions, and the smallest
safe proof needed to validate the report. The maintainer will acknowledge a complete
report before discussing a disclosure timeline.

## Security boundaries

- This bot is intended for explicitly allowlisted Discord channels only.
- It has no inbound HTTP listener and needs no published container ports.
- Secrets belong only in `.env` or the deployment platform's secret store.
- YouTube cookie files are optional runtime state and must never enter Git.
- Podcast URLs are limited to public HTTP(S) addresses on ports 80 and 443. DNS
  responses, redirects, media types, response sizes, and media duration are bounded.
- Proxy credentials are passed to child processes through their environment rather
  than command-line arguments. Direct-IP fallback is disabled by default.
- Provider error bodies and signed URLs are intentionally excluded from default logs.

These controls reduce risk but do not make untrusted media or third-party services
risk-free. Keep dependencies updated and run the container with the included
read-only, non-root Compose policy.
