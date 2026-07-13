# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [0.2.1] - 2026-07-13

### Added
- Added opt-in stderr debug logging via `--debug-stderr` and `debug_stderr` config.
- Added detailed debug messages for config/load, state offsets, batch delivery, and failure traces.

### Fixed
- Continued processing remaining endpoints/batches when one endpoint delivery fails.
- Logged per-endpoint delivery failures and returned non-zero when any batch failed.

## [0.2.0] - 2026-07-13

### Added
- Added per-endpoint TLS verification control via `verify_tls` (default `true`).
- Extended sample config with a local endpoint example using `https://api.local` and disabled TLS verification.

### Changed
- Updated documentation for endpoint configuration to describe `verify_tls` across all auth modes.

### Chore
- Ignored local `config/config.json` in git.

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
