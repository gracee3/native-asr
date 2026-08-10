# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG LLAMA_CPP_VERSION=b10333
ARG LLAMA_CPP_REVISION=08659901c43b51de735740f1cf61bb82fbe0c4e4

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates cmake git ninja-build patchelf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --filter=blob:none --no-checkout https://github.com/ggml-org/llama.cpp.git llama.cpp \
    && cd llama.cpp \
    && git checkout --detach "${LLAMA_CPP_REVISION}" \
    && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_REVISION}" \
    && test "$(git describe --tags --exact-match)" = "${LLAMA_CPP_VERSION}"

WORKDIR /src/llama.cpp
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON \
      -DGGML_BLAS=OFF \
      -DGGML_CUDA=OFF \
      -DGGML_NATIVE=OFF \
      -DLLAMA_BUILD_APP=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_SERVER=ON \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_TOOLS=ON \
      -DLLAMA_BUILD_UI=OFF \
      -DLLAMA_OPENSSL=OFF \
      -DLLAMA_USE_PREBUILT_UI=OFF \
    && cmake --build build --parallel --target llama-server \
    && install -d /opt/llama/bin /opt/llama/lib /opt/llama/share/licenses/llama.cpp \
    && install -m 0755 build/bin/llama-server /opt/llama/bin/ \
    && cp -a build/bin/lib*.so* /opt/llama/lib/ \
    && install -m 0644 LICENSE /opt/llama/share/licenses/llama.cpp/ \
    && patchelf --set-rpath '$ORIGIN/../lib' /opt/llama/bin/llama-server \
    && find /opt/llama/lib -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} \; \
    && ! ldd /opt/llama/bin/llama-server | grep -F 'not found'

FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG LLAMA_CPP_VERSION=b10333
ARG LLAMA_CPP_REVISION=08659901c43b51de735740f1cf61bb82fbe0c4e4

LABEL org.opencontainers.image.title="native-asr llama.cpp adjudicator runtime" \
      org.opencontainers.image.description="CPU-only bounded ASR candidate adjudication; models are mounted at /models" \
      org.opencontainers.image.source="https://github.com/gracee3/native-asr" \
      org.opencontainers.image.version="${LLAMA_CPP_VERSION}" \
      org.opencontainers.image.revision="${LLAMA_CPP_REVISION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl jq libgomp1 libstdc++6 time \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 native-asr \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin native-asr

COPY --from=builder /opt/llama/ /opt/llama/
COPY --chmod=0755 docker/llama-cpp/entrypoint.sh /usr/local/bin/native-asr-adjudicator

ENV HOME=/tmp \
    NATIVE_ASR_RUNTIME=llama-cpp \
    NATIVE_ASR_RUNTIME_VERSION=${LLAMA_CPP_VERSION} \
    NATIVE_ASR_RUNTIME_REVISION=${LLAMA_CPP_REVISION} \
    LLAMA_SERVER_BIN=/opt/llama/bin/llama-server

USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/native-asr-adjudicator"]
CMD ["--help"]
