# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [0.1.1] - 2026-07-10

### Fixed
- Hardened outbound endpoint URL handling in the HTTP delivery path.
- Added targeted scanner suppressions for known false positives on validated `urllib` usage.

## [0.1.0] - 2026-07-10

### Added
- Initial Postfix status tracker implementation.
- Cron-friendly stateful parser with log rotation support.
- Batched HTTPS delivery to multiple endpoints.
- Configurable endpoint authentication (`headers`, `basic`, `bearer`).
- Secure config mode enforcement (`0400`/`0600`).
- Lock file and PID metadata handling.
- Documentation and sample payload/config files.
