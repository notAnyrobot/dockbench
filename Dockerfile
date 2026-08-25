# syntax=docker/dockerfile:1.7

ARG UV_VERSION=0.12.3
ARG CUDA_IMAGE=nvidia/cuda:12.8.1-devel-ubuntu22.04

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

FROM ${CUDA_IMAGE} AS core

ARG MINIFORGE_VERSION=26.3.2-2
ARG MINIFORGE_SHA256=42260ffe3830fb953d5eee1bbb32229ff06aa7c3833c1ed7a9a0420a95685d94
ARG HF_HUB_VERSION=1.24.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash-completion \
    build-essential \
    ca-certificates \
    ccache \
    cmake \
    curl \
    ffmpeg \
    git \
    git-lfs \
    htop \
    jq \
    less \
    libdbus-1-3 \
    libegl-dev \
    libegl1 \
    libfontconfig1 \
    libgl1 \
    libgles2 \
    libglib2.0-0 \
    libglvnd0 \
    libice6 \
    libopengl0 \
    libsm6 \
    libvulkan1 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcursor1 \
    libxext6 \
    libxi6 \
    libxinerama1 \
    libxkbcommon-x11-0 \
    libxrandr2 \
    libxrender1 \
    nano \
    ninja-build \
    openssh-client \
    pkg-config \
    ripgrep \
    rsync \
    tini \
    tk \
    tmux \
    tree \
    unzip \
    vim \
    vulkan-tools \
    wget \
    zip \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-bin /uv /uvx /usr/local/bin/

RUN curl -fsSL \
      "https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}/Miniforge3-${MINIFORGE_VERSION}-Linux-x86_64.sh" \
      -o /tmp/miniforge.sh \
    && echo "${MINIFORGE_SHA256}  /tmp/miniforge.sh" | sha256sum -c - \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && printf 'channels:\n  - conda-forge\nchannel_priority: strict\nauto_activate_base: false\n' \
      > /opt/conda/.condarc \
    && /opt/conda/bin/conda clean --all --yes

ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_TOOL_DIR=/opt/uv/tools \
    UV_TOOL_BIN_DIR=/opt/uv/bin

RUN uv tool install "huggingface-hub==${HF_HUB_VERSION}" \
    && uv cache clean

ENV HOME=/state/home \
    UV_CACHE_DIR=/state/cache/uv \
    UV_PYTHON_INSTALL_DIR=/state/uv/python \
    UV_TOOL_DIR=/state/uv/tools \
    UV_TOOL_BIN_DIR=/state/uv/bin \
    HF_HOME=/state/cache/huggingface \
    TORCH_HOME=/state/cache/torch \
    CCACHE_DIR=/state/cache/ccache \
    CONDA_PKGS_DIRS=/state/conda/pkgs \
    MUJOCO_GL=egl \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,display,video \
    PATH=/state/uv/bin:/opt/uv/bin:/opt/conda/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN mkdir -p \
    /Code \
    /state/home \
    /state/cache/uv \
    /state/cache/huggingface \
    /state/cache/torch \
    /state/cache/ccache \
    /state/uv/python \
    /state/uv/tools \
    /state/uv/bin \
    /state/conda/pkgs

COPY bin/verify-image /usr/local/bin/verify-image

WORKDIR /Code
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

FROM core AS desktop

RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus-x11 \
    fonts-dejavu-core \
    tigervnc-common \
    tigervnc-standalone-server \
    tigervnc-tools \
    xauth \
    xfce4 \
    xfce4-terminal \
    && rm -rf /var/lib/apt/lists/*

COPY bin/start-vnc /usr/local/bin/start-vnc

EXPOSE 5901
