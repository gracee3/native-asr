# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG SHERPA_ONNX_VERSION=1.13.2
ARG SHERPA_ONNX_REVISION=13d0ae6c539d2809d32f5eaa3ef1db0c459d0b24
ARG ONNXRUNTIME_VERSION=1.24.4

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        patchelf \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --filter=blob:none https://github.com/k2-fsa/sherpa-onnx.git \
    && cd sherpa-onnx \
    && git checkout --detach "${SHERPA_ONNX_REVISION}" \
    && test "$(git rev-parse HEAD)" = "${SHERPA_ONNX_REVISION}" \
    && test "$(git describe --tags --exact-match)" = "v${SHERPA_ONNX_VERSION}" \
    && grep -Fq "v${ONNXRUNTIME_VERSION}" cmake/onnxruntime-linux-x86_64.cmake

WORKDIR /src/sherpa-onnx
RUN cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON \
        -DSHERPA_ONNX_ENABLE_BINARY=ON \
        -DSHERPA_ONNX_ENABLE_C_API=OFF \
        -DSHERPA_ONNX_BUILD_C_API_EXAMPLES=OFF \
        -DSHERPA_ONNX_ENABLE_CHECK=OFF \
        -DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF \
        -DSHERPA_ONNX_ENABLE_PYTHON=OFF \
        -DSHERPA_ONNX_ENABLE_SANITIZER=OFF \
        -DSHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION=OFF \
        -DSHERPA_ONNX_ENABLE_TESTS=OFF \
        -DSHERPA_ONNX_ENABLE_TTS=OFF \
        -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF \
        -DSHERPA_ONNX_LINK_LIBSTDCPP_STATICALLY=OFF \
        -DSHERPA_ONNX_USE_PRE_INSTALLED_ONNXRUNTIME_IF_AVAILABLE=OFF \
    && cmake --build build --parallel --target \
        sherpa-onnx \
        sherpa-onnx-offline \
        sherpa-onnx-vad-with-offline-asr \
        sherpa-onnx-version \
    && install -d /opt/native-asr/bin \
    && install -m 0755 \
        build/bin/sherpa-onnx \
        build/bin/sherpa-onnx-offline \
        build/bin/sherpa-onnx-vad-with-offline-asr \
        build/bin/sherpa-onnx-version \
        /opt/native-asr/bin/ \
    && install -d /opt/native-asr/lib \
    && install -m 0755 build/_deps/onnxruntime-src/lib/libonnxruntime.so* /opt/native-asr/lib/ \
    && for binary in \
        /opt/native-asr/bin/sherpa-onnx \
        /opt/native-asr/bin/sherpa-onnx-offline \
        /opt/native-asr/bin/sherpa-onnx-vad-with-offline-asr; \
      do patchelf --set-rpath '$ORIGIN/../lib' "$binary"; done \
    && test "$(patchelf --print-rpath /opt/native-asr/bin/sherpa-onnx-offline)" = '$ORIGIN/../lib' \
    && ldd /opt/native-asr/bin/sherpa-onnx-offline \
    && ! ldd /opt/native-asr/bin/sherpa-onnx-offline | grep -F 'not found'

FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG SHERPA_ONNX_VERSION=1.13.2
ARG SHERPA_ONNX_REVISION=13d0ae6c539d2809d32f5eaa3ef1db0c459d0b24
ARG ONNXRUNTIME_VERSION=1.24.4

LABEL org.opencontainers.image.title="native-asr sherpa-onnx runtime" \
      org.opencontainers.image.description="CPU-only sherpa-onnx ASR runtime; model weights are mounted at /models" \
      org.opencontainers.image.source="https://github.com/gracee3/native-asr" \
      org.opencontainers.image.version="${SHERPA_ONNX_VERSION}" \
      org.opencontainers.image.revision="${SHERPA_ONNX_REVISION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ffmpeg \
        jq \
        libgcc-s1 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 native-asr \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin native-asr

COPY --from=builder /opt/native-asr/ /opt/native-asr/
COPY --chmod=0755 docker/sherpa-onnx/entrypoint.sh /usr/local/bin/native-asr-sherpa

ENV HOME=/tmp \
    NATIVE_ASR_RUNTIME=sherpa-onnx \
    NATIVE_ASR_RUNTIME_VERSION=${SHERPA_ONNX_VERSION} \
    NATIVE_ASR_RUNTIME_REVISION=${SHERPA_ONNX_REVISION} \
    NATIVE_ASR_ONNXRUNTIME_VERSION=${ONNXRUNTIME_VERSION} \
    SHERPA_BIN_DIR=/opt/native-asr/bin

USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/native-asr-sherpa"]
CMD ["--help"]
