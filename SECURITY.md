# Security policy

This integration handles end-to-end encryption keys, so security reports are very welcome.

## Reporting a vulnerability

Please **do not** open a public issue. Report it privately through [GitHub security advisories](https://github.com/ch-rit/ha-etesync/security/advisories/new). You will get an answer within a week.

## Scope

Relevant reports include, for example:

- flaws in the Etebase protocol implementation (`custom_components/etesync/etebase/`), such as missing integrity checks or key handling mistakes,
- credentials, session data or task content ending up in logs, diagnostics or other places they should not be.

Note that the stored Home Assistant session contains the account key by design (see "Security and privacy" in the README).

## Supported versions

Only the latest release receives fixes.
