# Security Policy

## Supported versions

DVLib is currently alpha software. Security fixes are provided on the latest
revision of the default branch only.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature on the repository's Security tab. If
that feature is unavailable, contact the repository owner privately through
their GitHub profile.

Include the affected version or commit, reproduction steps, impact, and any
suggested mitigation. You should receive an acknowledgment within seven days.
Please allow a reasonable remediation period before public disclosure.

## Deployment expectations

The registry must be placed behind TLS and appropriate network access controls.
Operators should use strong publish tokens, protect registry backups, limit
request sizes at the reverse proxy, and keep Python dependencies patched. DVLib
does not claim that published HDL packages are safe or trustworthy.
