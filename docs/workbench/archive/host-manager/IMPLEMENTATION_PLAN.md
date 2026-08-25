# Historical Host Manager: First-Slice Implementation Plan

> **Historical, non-authoritative design input.** This plan was archived with
> a throwaway prototype and must not be adopted as a production Workbench
> implementation. A future Workbench requires a clean redesign.

## Basis and boundary

Build the first production slice from [the accepted Variant A handoff](../prototype/HANDOFF.md), specifically its [workbench](../prototype/HANDOFF.md#main-workbench), [architecture](../prototype/HANDOFF.md#recommended-production-architecture), and [suggested implementation order](../prototype/HANDOFF.md#suggested-implementation-order). This plan does not make the prototype a runtime dependency.

The slice is strictly read-only:

```text
Variant A browser UI -> authenticated same-origin API -> typed Docker adapter -> Docker Unix socket
```

The browser never receives Docker-socket access, a Docker API endpoint, or Docker credentials.

## Required decision gates (before implementation)

Record and approve all of the following before creating application code:

1. **Stack:** select supported UI/API languages, framework, package/build tooling, test runner, and Docker client library; confirm the client can use a Unix socket without shelling out.
2. **Authentication:** select the first-deployment identity/session mechanism and authorization policy. Every API request must be authenticated; unauthenticated requests must not reveal inventory or host state.
3. **TLS/deployment:** select TLS termination, certificate lifecycle, and trusted proxy rules. The first usable deployment uses HTTPS; bind locally by default and require an explicit deployment decision before any non-local listener.
4. **Host targeting:** define the initial single-host identity and stable `hostId` used for preferences and audit-ready request context. Multi-host routing is deferred.
5. **Managed-inventory rule:** approve the exact container labels/namespaces that identify robotics-managed containers and any migration/empty-state behavior.

## API and data contracts

Expose only same-origin JSON endpoints:

| Endpoint | Result | Scope |
| --- | --- | --- |
| `GET /api/host/status` | allowlisted host/Docker availability and summary | current approved host |
| `GET /api/images` | allowlisted image projections | images tagged `robotics-ws:*` by default |
| `GET /api/containers` | allowlisted container projections | robotics-managed containers by default |

Define versioned TypeScript (or selected-stack equivalent) contracts shared by route and adapter tests:

- `HostStatus`: `hostId`, `dockerReachable`, `dockerApiVersion`, `serverTime`, and scoped inventory counts; omit engine environment, raw errors, socket paths, and host filesystem details.
- `ImageSummary`: immutable ID, repository/tag references, created time, size, and approved robotics metadata only.
- `ContainerSummary`: immutable ID, name, image reference/ID, normalized state/status, created time, and approved robotics metadata only.
- `ApiError`: `code`, stable safe `message`, optional request/correlation ID. Map invalid requests to 400, unauthenticated to 401, unauthorized to 403, unavailable Docker to 503, and unexpected failures to 500 without leaking internals.

The typed `DockerAdapter` owns all Docker calls and filtering. Its read-only interface supplies `getHostStatus()`, `listImages(scope)`, and `listContainers(scope)`, returning domain models rather than Docker SDK response objects. Route handlers validate query input, enforce authentication/authorization, request a fixed inventory scope, project only contract fields, and translate adapter failures.

## UI behavior and persistence

Implement the Variant A shell with left inventory, center selection workspace, right inspector, and lower Job Queue/Terminal dock. Populate it only through the three APIs. The initial selection, loading, empty, unavailable, and safe error states must be explicit.

Show all mutation, job, terminal, and VNC controls as disabled placeholders with explanatory text; do not create their routes, sockets, job workers, Docker write calls, or browser-side fallbacks. Preserve the handoff's constrained resizable panes. Persist only layout dimensions keyed by authenticated `userId` and `hostId`; use browser storage initially. Do not persist inventory, credentials, API responses, terminal data, or raw Docker metadata. Server-side preference storage is a later decision.

## Security and operational rules

- API and UI are same-origin; do not enable permissive cross-origin access.
- The host manager alone may open the Unix Docker socket. Never mount, proxy, publish, or expose it to the browser or LAN.
- Bind loopback/local interfaces by default. TLS and authentication are mandatory for the first usable deployment, subject to the approved gates above.
- Set security headers appropriate to the selected stack (at minimum CSP, frame-ancestors/anti-clickjacking, `nosniff`, referrer policy, and permissions policy) and `Cache-Control: no-store` for authenticated HTML and API responses.
- Log correlation IDs and safe operational failures; redact authorization material, socket paths, Docker payloads, and sensitive labels.

## Phases

1. **Contracts and adapter:** implement approved stack skeleton, auth/TLS deployment boundary, typed models, scoped adapter, safe error mapping, and three GET routes.
2. **Read-only Variant A:** implement authenticated UI shell, inventory/selection/inspector data loading, layout persistence, and disabled future-feature placeholders.
3. **Hardening:** complete the verification below and document deployment defaults/decision outcomes before declaring the slice usable.
4. **Later (out of this slice):** container lifecycle actions; image export/import and archive paths/uploads; durable jobs and events; terminals/`exec`; VNC gateway; RBAC/audit expansion; multi-host support; server-side preferences.

## Verification plan

- **Contract:** adapter and route tests cover allowlisted projections, default image/container scope, response schemas, auth failures, and Docker error-to-HTTP mapping.
- **Browser mock:** mock all three endpoints to verify Variant A loading, selection, empty/unavailable/error states, disabled controls, and layout restoration for distinct user/host keys.
- **Isolated rootless Docker smoke:** run against a disposable rootless Docker daemon/socket; verify only scoped fixtures appear and no write operation is attempted.
- **Security-negative:** prove unauthenticated/unauthorized requests are rejected, cross-origin access is not permitted, no-store/security headers are present, and neither UI nor network responses disclose the Docker socket or sensitive adapter errors.

## Acceptance criteria

- All three authenticated same-origin GET endpoints return documented, allowlisted projections only.
- Default inventory contains only robotics-managed containers and `robotics-ws:*` images.
- Docker access is confined to the typed server-side adapter over a Unix socket; no write API exists.
- Variant A read-only UI handles normal and failure states, preserves layout per user/host, and visibly disables deferred features.
- Local binding, TLS/auth, no-store, and security headers meet the approved deployment decisions.
- Contract, browser-mock, rootless-smoke, and security-negative verification pass.
