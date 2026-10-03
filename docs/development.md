# Development and releases

[Home](../README.md) · [Architecture](architecture.md) · [Cloud monitor](../cloud-monitor/README.md)

## Local watchdog

Python 3.11+; standard library only.

```sh
python selftest.py --offline
python selftest.py
```

The offline suite covers decisions, HTTP health behavior, saved state, gateway
HTML parsing, notifier behavior and release policy. The second command also runs
live outbound network probes. Notification tests mock delivery and gateway reboots.

| File | Responsibility |
| --- | --- |
| `watchdog.py` | Configuration, probes, verdicts and reboot loop |
| `bgw320.py` | Gateway pages, authentication and restart requests |
| `health.py` | Health/status server and application version |
| `notifier.py` | Persistent local notification events and retries |
| `selftest.py`, `test_notifier.py` | Python verification |
| `testdata/` | Captured gateway HTML fixtures |
| `.github/image_tags.py` | Release tag validation and stable promotion |

Gateway fixtures preserve repeated labels and navigation collisions in the real
HTML. Keep those cases when changing the parser.

## Cloud monitor

Use Node.js 22+ from `cloud-monitor/`:

```sh
npm ci
npm test
npm run test:runtime
```

The runtime suite builds with Wrangler and tests a real local Workers runtime,
including Durable Object eviction. All outbound requests are mocked. Follow the
[cloud guide](../cloud-monitor/README.md) for a real deployment; CI does not deploy it.

## Image tags

Registry: `ghcr.io/mggarofalo/wan-watchdog`; platforms: `linux/amd64`, `linux/arm64`.

| Tag | Promotion policy |
| --- | --- |
| `X.Y.Z` | Matching final `vX.Y.Z` Git release tag |
| `X.Y`, `X` | Final releases in the minor/major line; major-zero alias is omitted |
| `X.Y.Z-name` | Matching prerelease tag; no minor/major alias promotion |
| `latest` | Every successful branch or version-tag push, including development/prereleases |
| `stable` | Final `vX.Y.0` releases only; patches/prereleases leave it unchanged |
| `sha-<commit>` | Published build traceability |

PR builds do not publish. Manual workflow dispatch publishes its branch/SHA/version
tags but does not promote `latest` or `stable`. Shared publication jobs are serialized;
`latest` represents the last successful push build to publish. GitHub can coalesce
pending jobs during bursts of pushes.

## Cut a release

1. Update `health.VERSION` and relevant documentation.
2. Run the offline suite and open a PR. Confirm the CI builds pass.
3. Merge, then tag that commit with the matching `vX.Y.Z` and push the tag.
4. Wait for the tag workflow to build both architectures and smoke-test the image.
5. Verify registry tags and publish release notes with upgrade instructions.

Tag validation requires a matching application version. Never move or reuse a
published version tag; use a new version for corrections. Full version tags are
treated as immutable by release convention, not enforced as immutable by the registry.
`v1.1.0` is the first tagged release; earlier code reported `1.0`.
