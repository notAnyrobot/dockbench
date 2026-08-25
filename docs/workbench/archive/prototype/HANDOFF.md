# Historical Docker Workbench Prototype Handoff

> **Historical, non-authoritative design input.** This archived throwaway
> prototype is not a Workbench implementation or an approved production
> design. Any future Workbench must begin with a clean redesign.

## Purpose and current decision

This directory contains a **throwaway, browser-only UX prototype** for a remote Docker-host management interface. It makes no Docker, network, VNC, filesystem, or shell calls.

The selected design is **Variant A** (`?variant=A`): a dense three-pane operational workbench with a resizable lower dock. Variants B and C remain only as alternative explorations and should not be used as the implementation baseline.

## Run the prototype

From the repository root:

```bash
python3 -m http.server 4173 --directory docs/workbench/archive/prototype
```

Open `http://127.0.0.1:4173/?variant=A`.

## Prototype contents

- `index.html` — self-contained HTML/CSS/JS mock; no dependencies.
- `NOTES.md` — short run instructions.
- `HANDOFF.md` — this implementation handoff.

## Approved Variant A interaction model

### Main workbench

- **Left inventory**: images/containers selection and contextual (`⋮` / right-click) actions.
- **Center workspace**: selected-item workspace canvas.
- **Right inspector**: selected image/container details and contextual actions.
- **Lower dock**: Job Queue beside Terminal.

### Resizing

Variant A supports user-controlled, constrained layout resizing:

- vertical dividers resize inventory, center workspace, and inspector;
- horizontal divider changes lower dock height;
- lower-dock vertical divider resizes Job Queue against Terminal;
- minimum panel dimensions prevent unusable layouts.

An implementation should persist these layout settings per user/host (for example in browser storage first, then server-side preferences if accounts exist).

### Containers, images, and jobs

The intended real product supports:

1. browse/select Docker images and containers;
2. start a container after choosing an image;
3. stop/restart running containers, with confirmations for destructive/bulk operations;
4. package a selected image to a tarball;
5. load a tarball supplied from the browser or already copied to the remote host;
6. durable save/load/upload/checksum jobs with progress, cancellation, retry, and errors;
7. VNC Viewer only for a running container with an explicitly registered/approved VNC endpoint.

For large image archives, prefer importing from an **existing remote-host path** over routing multi-GB files through the browser. Browser upload should be optional and resumable.

### Terminal model

The selected UX is a **tabbed terminal drawer** in the lower dock:

- terminal is enabled only for a selected **running** container;
- **New terminal** creates another session for that container;
- users may have multiple terminal tabs;
- tabs can be selected and closed;
- prototype commands are mock-only.

In production, each terminal tab must map to an isolated, authenticated server-side `docker exec -it <container> bash` (with a configured shell fallback), carried over a WebSocket. Never implement this with browser-side Docker access or by passing arbitrary commands to a host shell.

## Recommended production architecture

```text
Browser UI -- HTTPS/WebSocket --> Host Manager API -- Unix socket --> Docker Engine
                                      |
                                      +--> durable job worker/store
                                      +--> VNC/noVNC gateway
```

- Run the Host Manager API on the remote workstation; it is the only component permitted to access the Docker Unix socket.
- Use an explicit Docker adapter/service layer and allowlisted image/archive storage roots.
- Model `docker save`, archive upload, `docker load`, and checksums as durable background jobs; surface updates over WebSocket or server-sent events.
- Detect VNC through launch metadata/approved endpoints, not arbitrary host-port scanning.
- Use TLS and authentication from the first usable deployment. Do not expose `/var/run/docker.sock` to the browser or LAN.
- Add RBAC/audit records before permitting multiple users or untrusted networks.

## Suggested API boundary

```text
GET    /api/host/status
GET    /api/images
GET    /api/containers
POST   /api/containers
POST   /api/containers/:id/stop
POST   /api/containers/:id/restart
DELETE /api/containers/:id

POST   /api/images/:id/export
POST   /api/images/import
GET    /api/jobs
POST   /api/jobs/:id/cancel

POST   /api/containers/:id/terminals
WS     /api/terminals/:sessionId
DELETE /api/terminals/:sessionId
GET    /api/containers/:id/vnc
WS     /api/events
```

Treat request validation, action authorization, terminal session cleanup, job cancellation, archive path allowlisting, and audit logging as server responsibilities.

## Suggested implementation order

1. Build the read-only host/image/container API and Variant-A-based UI shell.
2. Implement image-selected container launch and selected-container stop/restart flows.
3. Add durable image export/import jobs using a remote archive directory.
4. Implement real-time job/container updates, confirmations, and context menus.
5. Add terminal WebSocket sessions with authorization, resource limits, lifecycle cleanup, and audit logs.
6. Add VNC gateway integration for registered desktop containers.
7. Add roles, persistent layout preferences, browser uploads, and broader operational hardening.

## Validation so far

- `bash tests/check-context.sh` passed with the prototype present in its source
  repository.
- `git diff --check` passed during prototype work.
- Browser prototype was reviewed manually; Variant A is accepted.

## Suggested skills for the next session

- `frontend-design` / @designer — translate the selected prototype into a production UI without flattening its layout/interaction quality.
- `verification-planning` — define project-specific evidence before building the real host manager.
- `webapp-testing` — exercise browser workflows, resizing, terminal tabs, jobs, and visual regressions.
- `diagnose` — if Docker/WebSocket/VNC integration fails during implementation.
- `tdd` — for the server/API job and terminal-session behavior.
