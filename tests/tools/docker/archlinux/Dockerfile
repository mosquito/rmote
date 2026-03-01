FROM archlinux/archlinux

RUN sed -i "s/^#*DisableSandbox.*/DisableSandbox/" /etc/pacman.conf && \
    sed -i "s/^#*Color.*/Color/" /etc/pacman.conf && \
    sed -i "s/^#*ParallelDownloads.*/ParallelDownloads/" /etc/pacman.conf && \
    pacman -Syu python3 --noconfirm && \
    rm -rf /var/cache/pacman/pkg/*
