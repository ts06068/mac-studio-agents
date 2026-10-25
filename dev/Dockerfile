FROM docker:29.6.2-cli AS dockercli
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-server sudo git curl ca-certificates openssl \
      python3 python3-pip python3-venv build-essential pkg-config \
      nodejs npm jq rsync unzip zip less vim-tiny tmux procps iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
    && mkdir -p /run/sshd /etc/ssh/host-keys
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
COPY dev/entrypoint.sh /usr/local/sbin/dev-entrypoint
RUN chmod 755 /usr/local/sbin/dev-entrypoint
EXPOSE 22
ENTRYPOINT ["/usr/local/sbin/dev-entrypoint"]

