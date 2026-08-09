# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG MOONSHINE_VERSION=0.1.1
ARG MOONSHINE_REVISION=b8fdbfec1211bc8393d08e54c0738811b8462c1f
ARG MOONSHINE_ARCHIVE_SHA256=f95b85ecea8c4c7467d20abe6cffb5c5c46ebfbb8de260c33a3900d3abca2535

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl --fail --location --show-error --retry 5 \
      --output moonshine.tar.gz \
      "https://github.com/moonshine-ai/moonshine/releases/download/v${MOONSHINE_VERSION}/moonshine-voice-linux-x86_64.tar.gz" \
    && echo "${MOONSHINE_ARCHIVE_SHA256}  moonshine.tar.gz" | sha256sum --check --strict \
    && mkdir /opt/moonshine \
    && tar -xzf moonshine.tar.gz -C /opt/moonshine --strip-components=1 \
    && test -f /opt/moonshine/include/moonshine-cpp.h \
    && test -f /opt/moonshine/lib/libmoonshine.so

COPY docker/moonshine/native-asr-moonshine.cpp /build/native-asr-moonshine.cpp
RUN install -d /opt/moonshine/bin \
    && g++ -std=c++20 -O3 -Wall -Wextra -Werror \
      -I/opt/moonshine/include native-asr-moonshine.cpp \
      -L/opt/moonshine/lib -lmoonshine \
      -Wl,-rpath,'$ORIGIN/../lib' -o /opt/moonshine/bin/native-asr-moonshine-core

FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG MOONSHINE_VERSION=0.1.1
ARG MOONSHINE_REVISION=b8fdbfec1211bc8393d08e54c0738811b8462c1f

LABEL org.opencontainers.image.title="native-asr Moonshine runtime" \
      org.opencontainers.image.description="CPU-only Moonshine C++ streaming ASR; models are mounted at /models" \
      org.opencontainers.image.source="https://github.com/gracee3/native-asr" \
      org.opencontainers.image.version="${MOONSHINE_VERSION}" \
      org.opencontainers.image.revision="${MOONSHINE_REVISION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg jq libgcc-s1 libstdc++6 time \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 native-asr \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin native-asr

COPY --from=builder /opt/moonshine/ /opt/moonshine/
COPY --chmod=0755 docker/moonshine/entrypoint.sh /usr/local/bin/native-asr-moonshine

ENV HOME=/tmp \
    NATIVE_ASR_RUNTIME=moonshine \
    NATIVE_ASR_RUNTIME_VERSION=${MOONSHINE_VERSION} \
    NATIVE_ASR_RUNTIME_REVISION=${MOONSHINE_REVISION}

USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/native-asr-moonshine"]
CMD ["--help"]
