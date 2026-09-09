# Server commands and access verification

Implementation of [specification #11](https://github.com/notAnyrobot/dockbench/issues/11)
and its implementation tickets #12–#16, with final acceptance tracked by #17.
Verified on September 9, 2026, against the refactor baseline `5db8982`.

## Implementation and review

Five implementers used Astra with low reasoning in isolated worktrees. Each was
instructed to use TDD at the interfaces agreed in the specification, and reported
failing behavioral tests followed by passing implementations. Root shells,
clipboard exchange, and configured deployment ran in parallel; managed restart
and web opening followed the configuration foundation.

The changes are integrated on `codex/server-cli-clipboard`. Separate Standards and
Spec reviews found no remaining actionable findings. Review caught and resolved
an empty workspace environment value selecting the caller's directory, and
malformed README command examples.

## Automated checks

The integrated implementation passed:

- 235 Python tests, including CLI, shared operations, HTTP/WebSocket, process
  ownership, configuration, and SSH connection behavior.
- 28 frontend tests, including explicit clipboard transfer, unavailable or denied
  host clipboard access, disconnect cleanup, and stale asynchronous results.
- Repository checks, including shell syntax, fake-Docker workflows, bootstrap,
  and layout checks.
- TypeScript checking and the Vite production build.
- Git whitespace checks. The existing frontend bundle-size warning and
  Starlette test-client deprecation warning remain.

Commands:

```bash
UV_CACHE_DIR=/tmp/dockbench-uv-cache uv run --frozen --group dev python -m pytest -q
UV_CACHE_DIR=/tmp/dockbench-uv-cache bash tests/check-context.sh
npm --prefix src/dockbench/web/frontend test
npm --prefix src/dockbench/web/frontend run build
git diff --check
```

## Runtime acceptance

- The new CLI shell entered the existing managed container as UID 0 in
  `/workspace`, then exited without restarting or provisioning the container.
- The production browser client exchanged text with an isolated TigerVNC desktop
  using the existing standard image. A 7,816-character outgoing sample preserved
  spaces, tabs, newlines, Chinese text, and emoji, verified independently through
  the desktop's X11 clipboard. A separate multiline Unicode desktop sample
  appeared in the incoming browser field. Copy to host reported success;
  disconnect cleared text and disabled transfer, and reconnect restored controls.
- Foreground YAML startup, canonical JSON runtime startup, and legacy JSON serve
  startup worked from another directory, served health and the built UI without
  Docker, and exited on Ctrl+C without registering a managed service.
- With temporary XDG directories and controlled build tools/systemd availability,
  an actual managed process deployed, stopped, and restarted on a changed YAML
  port. Restart ran no build or dependency sync. Repeated start preserved the PID
  and actual URL, local web opening succeeded, and status/stop worked after YAML
  became malformed. Stop preserved installation metadata for another start.

## Limits and handoff

The browser test used an isolated desktop and an HTTP session fixture forwarding
real RFB traffic, not the user's password-protected desktop session. An OS-host
paste after Copy was not verified: the automation's virtual clipboard is separate
from the browser clipboard. Denial/fallback behavior is covered automatically.
Real user-systemd deployment and a remote SSH host were not exercised; their
behavior was checked with controlled dependencies.

All temporary acceptance processes and the disposable desktop container were
cleaned up. Existing servers, the original managed container, desktop settings,
and the pre-existing uncommitted Dockerfile change were preserved. No image
rebuild, package version change, or migration of container persistence was needed.
