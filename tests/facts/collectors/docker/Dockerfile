# resolvectl status needs a systemd release with JSON status output.
ARG BASE=debian:forky-slim
FROM ${BASE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 systemd-sysv dbus iproute2 systemd-resolved systemd-timesyncd \
    && rm -rf /var/lib/apt/lists/*
# Container clocks are shared with the host. Exercise timesync availability and
# inactive state without allowing an NTP daemon to change the host's clock.
RUN systemctl mask systemd-timesyncd.service \
    && systemctl enable systemd-resolved.service \
    && mkdir -p /etc/systemd/resolved.conf.d \
    && printf '[Resolve]\nDNS=192.0.2.53\nFallbackDNS=\nDNSStubListener=no\n' \
        > /etc/systemd/resolved.conf.d/facts.conf

# Do not let a privileged container modify the host's binfmt handlers.
RUN systemctl mask systemd-binfmt.service \
    proc-sys-fs-binfmt_misc.automount proc-sys-fs-binfmt_misc.mount
# Networkd manages the same addresses and routes installed by ip in the fixture.
COPY 10-facts.network /etc/systemd/network/10-facts.network
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
