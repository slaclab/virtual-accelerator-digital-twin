ARG PYTHON_VERSION=3.12
ARG LCLS_LATTICE_REF=c6b8defbf2ba83bf8f5af70191c893de361657d1
ARG VIRTUAL_ACCELERATOR_REF=aadb438756323733da319829b3ca34695f958925
ARG DOCKER_PLATFORM=linux/amd64
ARG EPICS_BASE_VERSION=R7.0.10
ARG PVXS_REPO=https://github.com/bisegni/pvxs.git
ARG PVXS_BRANCH=fix/pva-channel-cleanup
ARG P4P_VERSION=4.2.2
# lume-base pinned: newer versions changed how PV type names are handled, which
# breaks with our source-built p4p 4.2.2 ("names must be a list of strings").
# 0.5.0 is the last known-good with our patches; upgrade only after verifying
# lume-pva's Type() construction still matches p4p's runtime expectations.
ARG LUME_BASE_VERSION=0.5.0

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
        libtcmalloc-minimal4 \
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
    PYEPICS_LIBCA=/opt/epics/base/lib/linux-x86_64/libca.so \
    MALLOC_ARENA_MAX=1
# tcmalloc was preloaded here to reduce fragmentation, but it silently defeated every other
# memory mitigation in this repo: tcmalloc does not export malloc_trim, so the three
# _libc.malloc_trim(0) calls (run.py, lume_bmad_model, lume_staged_model) were trimming
# glibc's unused heap, and MALLOC_ARENA_MAX / MALLOC_MMAP_THRESHOLD_ are glibc-only tunables
# it ignores. Measured over matched 20 h windows, removing it reclaimed ~126 MB of anonymous
# memory immediately and halved parent growth (2.68 -> 1.39 MB/h) at no cost in cycle time
# (0.733 s vs 0.726 s). Keep glibc so those mitigations actually take effect.

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
    # bmad pinned: 20260904.1 is the first build containing bmad-ecosystem#2176, which fixes
    # the rad_map leak (#2177/#2175) that grew this service 269 MB -> 4.6 GB in 17 h. Leaving
    # it unpinned meant Docker reused the cached conda layer and silently kept 20260828.0.
    && conda install -y "python=${PYTHON_VERSION}" pip "bmad=20260904.1" pytao \
    && patchelf --clear-execstack /opt/conda/lib/libtao.so \
    && conda clean -afy

WORKDIR /app

RUN git clone https://github.com/slaclab/lcls-lattice.git /opt/lcls-lattice \
    && cd /opt/lcls-lattice \
    && git checkout ${LCLS_LATTICE_REF}

# ── Build EPICS Base, PVXS, and p4p from source ─────────────────────────────
# Manager mandate: EPICS Base + PVXS + p4p come from source (not conda/PyPI).
# PVXS is bisegni/pvxs@fix/pva-channel-cleanup which fixes the channel-cache
# leak that grew this service 269 MB -> 4.6 GB in 17 h. p4p 4.2.2 is built
# against these exact EPICS Base + PVXS trees using conda's Python 3.12 so
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

RUN python -m pip install --upgrade setuptools wheel pyepics prometheus-client memray \
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
COPY tao_recycle.py .
COPY entrypoint.sh .
COPY scripts/ ./scripts/

# Patch 1/2 — lume_bmad Fortran heap leak (lume_bmad/model.py):
#   BUG A: initial_particles.setter called tao "set beam comb_ds_save" every cycle
#          → Tao reallocated ~3M-double comb arrays per cycle → Fortran heap grew ~180 MB/hr
#   BUG B: initial_particles.setter called update_state() redundantly
#          → LUMEBmadModel._set() always calls update_state() right after, doubling Tao reads
#   BUG C: LUMEBmadModel._set() called _refresh_dynamic_action_variables() redundantly
#          → setter already calls it; _set() duplicated the tao_global + bunch_comb reads
# TODO: remove once fixes land upstream in lume-science/lume-bmad
COPY todo/patches/lume_bmad_model.patch.py /opt/conda/lib/python3.12/site-packages/lume_bmad/model.py

# Patch 2/2 — lume StagedModel leak (lume/staged_model.py):
#   Prod runs MODEL=cu_hxr_staged → StagedModel._set() runs every cycle but had no
#   malloc_trim / h5py.h5.garbage_collect(). glibc heap fragmentation and HDF5 internal
#   free lists accumulated across all stages, causing RSS growth identical to the bmad leak.
# TODO: remove once fixes land upstream in lume-science/lume
COPY todo/patches/lume_staged_model.patch.py /opt/conda/lib/python3.12/site-packages/lume/staged_model.py

# Patch 3/3 — lume-pva SharedPV.post() C++ heap leak (lume_pva/runner.py + variables.py):
#   Each simulation cycle posted ~180 output PVs. Every post() call constructed a fresh
#   Value(type_, {...}) via pack_value() — a new C++ PVStructure allocation invisible to
#   Python tracemalloc/memray. RSS grew monotonically at ~X MB/hr even with Python heap flat.
#   Fix: cache one Value per PV and mutate it in-place via update_value(); pvxs assign()
#   deep-copies into internal storage on post() so the cached object is never aliased.
# TODO: remove once fixes land upstream in lume-science/lume-pva
COPY todo/patches/lume_pva_runner.patch.py /opt/conda/lib/python3.12/site-packages/lume_pva/runner.py
COPY todo/patches/lume_pva_variables.patch.py /opt/conda/lib/python3.12/site-packages/lume_pva/variables.py

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["5075"]
