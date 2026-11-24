# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-24

### Added

- Config flow for EteSync accounts on the hosted service or self-hosted Etebase servers; multiple accounts supported.
- To-do list entity for every task list, added and removed automatically as lists change.
- Create, rename, complete, re-open and delete tasks; due dates, due times and descriptions.
- Incremental sync every minute, automatic token renewal, retry on remote conflicts.
- Re-authentication flow, diagnostics, log-out when an account is removed.
- Pure-Python Etebase client (PyNaCl), verified against etebase-rs test vectors and the official client.

[Unreleased]: https://github.com/ch-rit/ha-etesync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ch-rit/ha-etesync/releases/tag/v0.1.0
