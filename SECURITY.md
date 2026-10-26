# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: **Security → Report a vulnerability** on this repository (private advisory).

Please include:
- affected version or commit, and component (bot / engine / auth-web)
- steps to reproduce, using only accounts and infrastructure you own or are authorized to test
- the impact you observed

We aim to acknowledge reports within 7 days and to agree on a disclosure date with you.

## Scope

In scope: this repository's code, its Docker/Compose configuration, and its documented default deployment.

Out of scope: Telegram's own platform, social engineering, denial of service through volume, and findings that require a compromised host or leaked `.env` (those are documented as accepted risks in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)).

## Testing rules

- Test only against your own self-hosted instance and Telegram accounts you control.
- Never access, download, or forward other people's media or sessions.

## Audit status

All controls described in this repository were **reviewed by the developers only**. **No independent third-party audit has been performed.** If an audit is done, this section will link to the report.

## Supported versions

Only the latest commit on `main` receives security fixes.
