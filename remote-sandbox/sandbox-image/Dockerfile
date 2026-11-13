FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    HOME=/root \
    TERM=xterm-256color

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        wget \
        git \
        jq \
        python3 \
        python3-pip \
        openssh-client \
        rsync \
        unzip \
        tar \
        coreutils \
        procps \
        sudo \
        nano \
        less \
        iproute2 \
        iputils-ping \
        dnsutils \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'root ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/root-nopasswd \
    && chmod 440 /etc/sudoers.d/root-nopasswd \
    && mkdir -p /root/projects /root/skills /var/lib/kai /run/kai \
    && chmod 755 /var/lib/kai /run/kai

COPY sessiond.py /usr/local/bin/kai-sessiond
COPY session-cli.py /usr/local/bin/kai-session
COPY entrypoint.sh /usr/local/bin/kai-entrypoint
RUN chmod +x /usr/local/bin/kai-sessiond /usr/local/bin/kai-session /usr/local/bin/kai-entrypoint

WORKDIR /root
EXPOSE 0
ENTRYPOINT ["/usr/local/bin/kai-entrypoint"]
