ARG PYTHON_VERSION=3.12
ARG LCLS_LATTICE_REF=52ad1a5ddd00aa57a89a4fc7f2fa1a2363216ae8
ARG VIRTUAL_ACCELERATOR_REF=fbd2f392809b59280bcb97da76ab11c0438dd915
ARG DOCKER_PLATFORM=linux/amd64

FROM --platform=${DOCKER_PLATFORM} python:${PYTHON_VERSION}-slim AS runtime
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
        \
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

# Set timezone to California (SLAC)
ENV TZ=America/Los_Angeles

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/conda/bin:$PATH \
    LCLS_LATTICE=/opt/lcls-lattice \
    KMP_DUPLICATE_LIB_OK=TRUE \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    TORCH_NUM_THREADS=2 \
    EPICS_PVA_AUTO_ADDR_LIST=YES

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash bzip2 curl git patchelf \
    && rm -rf /var/lib/apt/lists/*

# Install miniforge + conda packages (pytao requires bmad shared library)
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
    && conda install epics-base=7.0.9.0 pvxs=1.5.2 \
    && patchelf --clear-execstack /opt/conda/lib/libtao.so \
    && conda clean -afy

WORKDIR /app

# Clone lcls-lattice at pinned commit
RUN git clone https://github.com/slaclab/lcls-lattice.git /opt/lcls-lattice \
    && cd /opt/lcls-lattice \
    && git checkout ${LCLS_LATTICE_REF}

# Install Python packages
RUN python -m pip install --upgrade setuptools wheel pyepics p4p\
    && python -m pip install --upgrade --index-url https://download.pytorch.org/whl/cpu torch \
    && git clone https://github.com/slaclab/virtual-accelerator.git /opt/virtual-accelerator \
    && cd /opt/virtual-accelerator \
    && git checkout ${VIRTUAL_ACCELERATOR_REF} \
    && python -m pip install -e ".[bmad,pva,surrogate]" \
    && cd /app \
    && python -m pip install --force-reinstall --no-deps \
        "lume-bmad @ git+https://github.com/lume-science/lume-bmad.git" \
        "lume-pva @ git+https://github.com/lume-science/lume-pva.git"

COPY run.py .
COPY entrypoint.sh .
# scripts/ ships smoke_test.py and epics_env.sh so the image can self-verify
# (CI gate, apptainer, k8s test-pod) and clients can configure their environment.
COPY scripts/ ./scripts/

ENV PVA_PORT=5075

EXPOSE 5075/tcp

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["5075"]
