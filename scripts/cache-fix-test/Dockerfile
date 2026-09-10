FROM ubuntu:24.04 AS builder
LABEL org.opencontainers.image.source="https://github.com/slaclab/epics-k8s-reconnection-test"

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    cmake \
    ninja-build \
    libreadline-dev \
    libevent-dev \
    perl \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# EPICS base 7.0.9
RUN git clone --branch R7.0.9 --depth 1 --recursive \
    https://github.com/epics-base/epics-base.git /opt/epics-src

WORKDIR /opt/epics-src
RUN make -j$(nproc) INSTALL_LOCATION=/opt/local \
    && make install INSTALL_LOCATION=/opt/local

ENV EPICS_BASE=/opt/local

RUN EPICS_ARCH=$(/opt/epics-src/startup/EpicsHostArch) \
    && echo "$EPICS_ARCH" > /etc/epics_host_arch \
    && echo "Detected EPICS_HOST_ARCH: $EPICS_ARCH"

# pvxs 1.5.2
RUN git clone --branch 1.5.2 --depth 1 \
    https://github.com/epics-base/pvxs.git /opt/pvxs-src

WORKDIR /opt/pvxs-src
RUN git submodule update --init

COPY patches/ /tmp/patches/
RUN git apply /tmp/patches/pvxs-udp-socket-recreate.patch

RUN arch=$(cat /etc/epics_host_arch) \
    && echo "EPICS_BASE=/opt/local"       > configure/RELEASE.local \
    && echo "EPICS_HOST_ARCH=$arch"      >> configure/RELEASE.local \
    && echo "INSTALL_LOCATION=/opt/local" >> configure/RELEASE.local \
    && EPICS_HOST_ARCH=$arch make -j$(nproc) \
    && EPICS_HOST_ARCH=$arch make install \
    && arch2=$(cat /etc/epics_host_arch) \
    && if ! ls /opt/local/lib/${arch2}/libpvxs* >/dev/null 2>&1; then \
           mkdir -p /opt/local/lib/${arch2} && cp -a /opt/pvxs-src/lib/${arch2}/. /opt/local/lib/${arch2}/; \
       fi \
    && if ! ls /opt/local/include/pvxs/version.h >/dev/null 2>&1; then \
           mkdir -p /opt/local/include && cp -a /opt/pvxs-src/include/. /opt/local/include/; \
       fi

# pvxs cli tools (pvxget, pvxmonitor, pvxput, pvxinfo) and softIocPVX
RUN arch=$(cat /etc/epics_host_arch) \
    && EPICS_HOST_ARCH=$arch make -j$(nproc) -C /opt/pvxs-src/tools \
    && EPICS_HOST_ARCH=$arch make install -C /opt/pvxs-src/tools \
    && EPICS_HOST_ARCH=$arch make -j$(nproc) -C /opt/pvxs-src/qsrv \
    && EPICS_HOST_ARCH=$arch make install -C /opt/pvxs-src/qsrv \
    && cp -a /opt/pvxs-src/bin/${arch}/. /opt/local/bin/${arch}/ \
    && mkdir -p /opt/local/dbd \
    && find /opt/pvxs-src -name '*.dbd' -exec cp {} /opt/local/dbd/ \;

# -----------------------------------------------------------------------
FROM ubuntu:24.04
LABEL org.opencontainers.image.source="https://github.com/slaclab/epics-k8s-reconnection-test"

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    libreadline8 \
    libevent-core-2.1-7t64 \
    libevent-pthreads-2.1-7t64 \
    perl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/local /opt/local
COPY --from=builder /etc/epics_host_arch /etc/epics_host_arch

ENV EPICS_BASE=/opt/local
ENV PVXS_BASE=/opt/local

RUN arch=$(cat /etc/epics_host_arch) \
    && echo "export EPICS_HOST_ARCH=$arch"                          > /etc/profile.d/epics.sh \
    && echo "export EPICS_BASE=/opt/local"                         >> /etc/profile.d/epics.sh \
    && echo "export PATH=/opt/local/bin/${arch}:\$PATH"            >> /etc/profile.d/epics.sh \
    && echo "export LD_LIBRARY_PATH=/opt/local/lib/${arch}"        >> /etc/profile.d/epics.sh

ENV PATH="/opt/local/bin/linux-aarch64:/opt/local/bin/linux-x86_64:${PATH}"
ENV LD_LIBRARY_PATH="/opt/local/lib/linux-aarch64:/opt/local/lib/linux-x86_64"

WORKDIR /app
