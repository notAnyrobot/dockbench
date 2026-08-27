# Dockbench

Dockbench is a browser-first environment for preparing and operating persistent development containers on local or remote container hosts.

## Language

**Dockbench**:
The product as a whole, including its browser workbench and companion command-line interface.
_Avoid_: Docker Workstation, Docker Workbench

**Dockbench server**:
The host-local process that provides the browser workbench for one container host.
_Avoid_: Workbench service, host manager

**Managed container**:
A development container created and recognized by Dockbench, with persistent workspace and state mounts.
_Avoid_: Workstation, desktop container

**Workspace**:
The host-owned directory containing a user's projects and mounted into managed containers.
_Avoid_: Code root

**Image recipe**:
A repository-owned, versioned build definition that selects a Dockerfile and its default image tag, target, and platform.
_Avoid_: Image template

**Desktop-capable image**:
An image that advertises Dockbench's supported desktop contract in addition to the generic shell contract.
_Avoid_: Desktop image
