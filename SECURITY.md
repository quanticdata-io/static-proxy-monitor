# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Email **hello@quanticdata.io** with the subject "Security: static-proxy-monitor" and include a description, steps to reproduce and the version you use. We acknowledge reports within 3 business days and keep you informed until a fix is released.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## Credentials

Never commit proxy credentials or an API key. static-proxy-monitor masks proxy passwords in every output. `QUANTICDATA_API_KEY` is read from the environment and sent only in the `Authorization` header to the API base URL (`QUANTICDATA_API_BASE`, default `https://api.quanticdata.io/v1`); `IPINFO_TOKEN` is sent only to ipinfo.io. Geolocation lookups are called directly, never through the proxies under test. The `--state` file holds exit IPs and masked proxy strings, never passwords. Report any path where a credential is printed, logged or sent elsewhere.
