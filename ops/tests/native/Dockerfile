# A userspace VM: never needs --privileged, host mounts, devices or Docker socket.
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils openssh-client \
    curl ca-certificates python3 \
    && useradd --uid 10000 --create-home runner \
    && install -d -o runner -g runner /work /payload /image
RUN curl --fail --location --retry 2 \
    https://cloud-images.ubuntu.com/noble/20260826/noble-server-cloudimg-arm64.img \
    --output /image/base.qcow2 \
    && echo 'afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f  /image/base.qcow2' | sha256sum --check
COPY boot.py /usr/local/bin/native-vm-boot.py
USER runner
WORKDIR /work
ENTRYPOINT ["python3", "/usr/local/bin/native-vm-boot.py"]
