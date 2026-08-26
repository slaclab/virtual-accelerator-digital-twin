ARG PYTHON_VERSION=3.12
ARG LCLS_LATTICE_REF=c6b8defbf2ba83bf8f5af70191c893de361657d1
ARG VIRTUAL_ACCELERATOR_REF=fbd2f392809b59280bcb97da76ab11c0438dd915
ARG DOCKER_PLATFORM=linux/amd64

# ── base: all deps, no app files ─────────────────────────────────────────────
FROM --platform=${DOCKER_PLATFORM} python:${PYTHON_VERSION}-slim AS base
ARG PYTHON_VERSION
ARG LCLS_LATTICE_REF

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
    PATH=/opt/conda/epics/bin/linux-x86_64:/opt/conda/bin:$PATH \
    LCLS_LATTICE=/opt/lcls-lattice \
    KMP_DUPLICATE_LIB_OK=TRUE \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    TORCH_NUM_THREADS=2 \
    EPICS_PVA_AUTO_ADDR_LIST=YES \
    PYEPICS_LIBCA=/opt/conda/epics/lib/linux-x86_64/libca.so \
    MALLOC_ARENA_MAX=1 \
    LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash bzip2 curl git patchelf \
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
    && conda install -y "python=${PYTHON_VERSION}" pip bmad pytao \
    && conda install epics-base pvxs=1.5.2 \
    && patchelf --clear-execstack /opt/conda/lib/libtao.so \
    && conda clean -afy

WORKDIR /app

RUN git clone https://github.com/slaclab/lcls-lattice.git /opt/lcls-lattice \
    && cd /opt/lcls-lattice \
    && git checkout ${LCLS_LATTICE_REF}

RUN python -m pip install --upgrade setuptools wheel pyepics p4p prometheus-client memray \
    && python -m pip install --upgrade --index-url https://download.pytorch.org/whl/cpu torch \
    && git clone https://github.com/slaclab/virtual-accelerator.git /opt/virtual-accelerator \
    && cd /opt/virtual-accelerator \
    && git checkout ${VIRTUAL_ACCELERATOR_REF} \
    && python -m pip install -e ".[bmad,pva,surrogate]" \
    && cd /app \
    && python -m pip install --force-reinstall --no-deps \
        "lume-bmad @ git+https://github.com/lume-science/lume-bmad.git" \
        "lume-pva @ git+https://github.com/lume-science/lume-pva.git"

ENV PVA_PORT=5075
EXPOSE 5075/tcp
EXPOSE 9090/tcp

# ── production: base + app files ─────────────────────────────────────────────
FROM base AS production
COPY run.py .
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
