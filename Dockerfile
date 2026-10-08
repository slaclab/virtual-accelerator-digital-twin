ARG PYTHON_VERSION=3.12
ARG LCLS_LATTICE_REF=c6b8defbf2ba83bf8f5af70191c893de361657d1
ARG VIRTUAL_ACCELERATOR_REF=114170a35d0a558ea3f1681dea24ff377d574a32
ARG DOCKER_PLATFORM=linux/amd64
ARG EPICS_BASE_VERSION=R7.0.10
ARG PVXS_REPO=https://github.com/bisegni/pvxs.git
ARG PVXS_BRANCH=fix/pva-channel-cleanup
ARG P4P_VERSION=4.2.2
ARG LUME_BASE_VERSION=0.6.0
ARG BMAD_VERSION=20260904.1

# ── base: all deps, no app files ─────────────────────────────────────────────
FROM --platform=${DOCKER_PLATFORM} python:${PYTHON_VERSION}-slim AS base
ARG PYTHON_VERSION
ARG LCLS_LATTICE_REF
ARG VIRTUAL_ACCELERATOR_REF
ARG EPICS_BASE_VERSION
ARG PVXS_REPO
ARG PVXS_BRANCH
ARG P4P_VERSION
ARG LUME_BASE_VERSION
ARG BMAD_VERSION

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        bash \
        curl \
        git \
        ca-certificates \
        vim \
        tmux \
        supervisor \
        tzdata \
        procps \
        psmisc \
        iproute2 \
        iputils-ping \
        net-tools \
        netcat-traditional \
        dnsutils \
        traceroute \
        tcpdump \
        ethtool \
        socat \
        nmap \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Los_Angeles

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EPICS_ROOT=/opt/epics \
    EPICS_BASE=/opt/epics/base \
    PVXS_ROOT=/opt/epics/pvxs \
    P4P_ROOT=/opt/epics/p4p \
    EPICS_HOST_ARCH=linux-x86_64 \
    PATH=/opt/epics/base/bin/linux-x86_64:/opt/epics/pvxs/bin/linux-x86_64:/opt/conda/bin:$PATH \
    LD_LIBRARY_PATH=/opt/epics/base/lib/linux-x86_64:/opt/epics/pvxs/lib/linux-x86_64 \
    PYTHONPATH=/opt/epics/p4p-python \
    LCLS_LATTICE=/opt/lcls-lattice \
    KMP_DUPLICATE_LIB_OK=TRUE \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    TORCH_NUM_THREADS=2 \
    EPICS_PVA_AUTO_ADDR_LIST=YES \
    PYEPICS_LIBCA=/opt/epics/base/lib/linux-x86_64/libca.so

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash bzip2 curl git patchelf \
       build-essential libevent-dev perl libreadline-dev libncurses-dev \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && case "${arch}" in \
        amd64) conda_arch="x86_64" ;; \
        arm64) conda_arch="aarch64" ;; \
        *) echo "Unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${conda_arch}.sh" -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm -f /tmp/miniforge.sh \
    && conda config --system --add channels conda-forge \
    && conda config --system --set channel_priority strict \
    # bmad pinned: 20260904.1 contains bmad-ecosystem#2176 (rad_map leak fix).
    && conda install -y "python=${PYTHON_VERSION}" pip "bmad=${BMAD_VERSION}" pytao \
    && patchelf --clear-execstack /opt/conda/lib/libtao.so \
    && conda clean -afy

WORKDIR /app

RUN git clone https://github.com/slaclab/lcls-lattice.git /opt/lcls-lattice \
    && cd /opt/lcls-lattice \
    && git checkout ${LCLS_LATTICE_REF}

# ── Build EPICS Base, PVXS, and p4p from source ─────────────────────────────
# EPICS Base + PVXS + p4p come from source (not conda/PyPI). PVXS is
# bisegni/pvxs@fix/pva-channel-cleanup for the channel-cache fix. p4p 4.2.2 is
# built against these EPICS Base + PVXS trees using conda's Python 3.12 so
# its C extension is ABI-matched to the interpreter that runs bmad/pytao.
# TODO: pin to upstream tags once the fix lands.

RUN git clone --recurse-submodules --branch ${EPICS_BASE_VERSION} --depth 1 \
        https://github.com/epics-base/epics-base.git ${EPICS_BASE} \
    && make -C ${EPICS_BASE} -j"$(nproc)"

RUN git clone --recurse-submodules --branch ${PVXS_BRANCH} --depth 1 \
        ${PVXS_REPO} ${PVXS_ROOT} \
    && printf 'EPICS_BASE = %s\n' "${EPICS_BASE}" > ${PVXS_ROOT}/configure/RELEASE.local \
    && make -C ${PVXS_ROOT} -j"$(nproc)"

RUN python -m pip install --no-cache-dir setuptools_dso cython nose2 ply numpy \
    && git clone --recurse-submodules --branch ${P4P_VERSION} --depth 1 \
        https://github.com/epics-base/p4p.git ${P4P_ROOT} \
    && printf 'EPICS_BASE = %s\nPVXS = %s\n' "${EPICS_BASE}" "${PVXS_ROOT}" \
        > ${P4P_ROOT}/configure/RELEASE.local \
    && make -C ${P4P_ROOT} -j"$(nproc)" \
    && P4P_INIT="$(find ${P4P_ROOT} -type f -path '*/p4p/__init__.py' \
        ! -path '*/src/*' ! -path '*/documentation/*' | head -n 1)" \
    && test -n "${P4P_INIT}" \
    && ln -s "$(dirname "$(dirname "${P4P_INIT}")")" ${EPICS_ROOT}/p4p-python \
    && python -c "import p4p; assert p4p.__file__.startswith('/opt/epics/p4p-python/'), p4p.__file__; print('p4p OK:', getattr(p4p, '__version__', 'unknown'), p4p.__file__)"

RUN python -m pip install --upgrade setuptools wheel pyepics prometheus-client \
    && python -m pip install --upgrade --index-url https://download.pytorch.org/whl/cpu torch \
    && git clone https://github.com/slaclab/virtual-accelerator.git /opt/virtual-accelerator \
    && cd /opt/virtual-accelerator \
    && test -n "${VIRTUAL_ACCELERATOR_REF}" \
    && git checkout ${VIRTUAL_ACCELERATOR_REF} \
    && git rev-parse HEAD \
    && python -m pip install -e ".[bmad,pva,surrogate]" \
    && cd /app \
    && python -m pip install --force-reinstall --no-deps \
        "lume-base==${LUME_BASE_VERSION}" \
        "lume-bmad @ git+https://github.com/lume-science/lume-bmad.git" \
        "lume-pva @ git+https://github.com/lume-science/lume-pva.git" \
    && python -c "import lume, p4p; print('lume:', lume.__version__ if hasattr(lume, '__version__') else 'unknown'); assert p4p.__file__.startswith('/opt/epics/p4p-python/'), 'p4p was shadowed by a PyPI install: ' + p4p.__file__; print('p4p source-build still active:', p4p.__file__)"

ENV PVA_PORT=5075
EXPOSE 5075/tcp
EXPOSE 9090/tcp

# ── production: base + app files ─────────────────────────────────────────────
FROM base AS production
COPY run.py .
COPY entrypoint.sh .
COPY scripts/ ./scripts/

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["5075"]
