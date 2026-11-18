# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-18

### Added

- Checks per static proxy: reachability, exit IP against the expected IP, country and city from the echo and from each geolocation source, agreement between sources, `--expect-country`, target URLs.
- `--state`: changes since the previous run (exit IP, reachability, country per source, targets).
- Table, JSON and CSV output; `--fail-on-problems`; masked passwords.
- Geolocation sources: QuanticData `ip-info`, ipinfo.io, or any JSON service.
- QuanticData ISP plans through `generate`, with the expected IP of each Premium ISP line.

[Unreleased]: https://github.com/quanticdata-io/static-proxy-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/quanticdata-io/static-proxy-monitor/releases/tag/v0.1.0
