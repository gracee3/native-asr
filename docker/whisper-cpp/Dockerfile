# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG WHISPER_CPP_VERSION=1.9.2
ARG WHISPER_CPP_REVISION=306c88f4d1286aec1bf96e544632897886af5501

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates cmake git ninja-build patchelf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --filter=blob:none --no-checkout https://github.com/ggml-org/whisper.cpp.git whisper.cpp \
    && cd whisper.cpp \
    && git checkout --detach "${WHISPER_CPP_REVISION}" \
    && test "$(git rev-parse HEAD)" = "${WHISPER_CPP_REVISION}" \
    && test "$(git describe --tags --exact-match)" = "v${WHISPER_CPP_VERSION}"

WORKDIR /src/whisper.cpp
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON \
      -DGGML_BLAS=OFF \
      -DGGML_CUDA=OFF \
      -DGGML_NATIVE=OFF \
      -DWHISPER_BUILD_EXAMPLES=ON \
      -DWHISPER_BUILD_SERVER=OFF \
      -DWHISPER_BUILD_TESTS=OFF \
    && cmake --build build --parallel --target whisper-cli \
    && install -d /opt/whisper/bin /opt/whisper/lib /opt/whisper/share/licenses/whisper.cpp \
    && install -m 0755 build/bin/whisper-cli /opt/whisper/bin/ \
    && cp -a build/bin/lib*.so* /opt/whisper/lib/ \
    && install -m 0644 LICENSE /opt/whisper/share/licenses/whisper.cpp/ \
    && patchelf --set-rpath '$ORIGIN/../lib' /opt/whisper/bin/whisper-cli \
    && find /opt/whisper/lib -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} \; \
    && ! ldd /opt/whisper/bin/whisper-cli | grep -F 'not found'

FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG WHISPER_CPP_VERSION=1.9.2
ARG WHISPER_CPP_REVISION=306c88f4d1286aec1bf96e544632897886af5501

LABEL org.opencontainers.image.title="native-asr whisper.cpp runtime" \
      org.opencontainers.image.description="CPU-only whisper.cpp ASR; models are mounted at /models" \
      org.opencontainers.image.source="https://github.com/gracee3/native-asr" \
      org.opencontainers.image.version="${WHISPER_CPP_VERSION}" \
      org.opencontainers.image.revision="${WHISPER_CPP_REVISION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg jq libgomp1 libstdc++6 time \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 native-asr \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin native-asr

COPY --from=builder /opt/whisper/ /opt/whisper/
COPY --chmod=0755 docker/whisper-cpp/entrypoint.sh /usr/local/bin/native-asr-whisper

ENV HOME=/tmp \
    NATIVE_ASR_RUNTIME=whisper-cpp \
    NATIVE_ASR_RUNTIME_VERSION=${WHISPER_CPP_VERSION} \
    NATIVE_ASR_RUNTIME_REVISION=${WHISPER_CPP_REVISION} \
    WHISPER_CPP_BIN=/opt/whisper/bin/whisper-cli

USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/native-asr-whisper"]
CMD ["--help"]
