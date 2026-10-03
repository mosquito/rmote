ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 systemd-sysv dbus ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Privileged containers share the host's binary-format handlers. Do not let
# systemd-binfmt flush them and break cross-architecture Docker execution.
RUN systemctl mask systemd-binfmt.service \
    proc-sys-fs-binfmt_misc.automount proc-sys-fs-binfmt_misc.mount
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
