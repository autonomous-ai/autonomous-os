FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    btrfs-progs \
    dosfstools \
    parted \
    qemu-utils \
    qemu-user-static \
    wget \
    curl \
    ca-certificates \
    wpasupplicant \
    xz-utils \
    p7zip-full \
    e2fsprogs \
    cloud-guest-utils \
    rsync \
    util-linux \
    openssl \
    jq \
    python3 \
    python3-pip \
    && pip3 install --no-cache-dir --break-system-packages gdown \
    && rm -rf /var/lib/apt/lists/*

RUN which wpa_passphrase

WORKDIR /work
COPY build.sh /work/build.sh
COPY build-orangepi.sh /work/build-orangepi.sh
COPY lib /work/lib
RUN chmod +x /work/build.sh /work/build-orangepi.sh

# Default to OrangePi build; the Makefile overrides at run-time by appending
# /work/build.sh when TARGET=rpi.
CMD ["/work/build-orangepi.sh"]