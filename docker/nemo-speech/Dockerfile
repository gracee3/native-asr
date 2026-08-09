# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

FROM ${BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG NEMO_SPEECH_VERSION=1.0.0
ARG NEMO_SPEECH_REVISION=5be7bfb104802131e61fe679b3f1401b27270216
ARG GGML_REVISION=c03b4e2bcece5134827881af90242086daf75be5

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libsentencepiece-dev \
        ninja-build \
        patchelf \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --filter=blob:none --no-checkout https://github.com/NVIDIA/NeMo-Speech.cpp.git \
        nemo-speech \
    && cd nemo-speech \
    && git checkout --detach "${NEMO_SPEECH_REVISION}" \
    && test "$(git rev-parse HEAD)" = "${NEMO_SPEECH_REVISION}" \
    && grep -Fqx "NEMO_SPEECH_VERSION: ${NEMO_SPEECH_VERSION}" VERSION \
    && test "$(git rev-parse HEAD:ggml)" = "${GGML_REVISION}" \
    && git submodule update --init --depth 1 ggml \
    && test "$(git -C ggml rev-parse HEAD)" = "${GGML_REVISION}"

WORKDIR /src/nemo-speech
RUN cmake -S . -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_AVX=ON \
        -DGGML_AVX2=ON \
        -DGGML_AVX512=OFF \
        -DGGML_AVX512_BF16=OFF \
        -DGGML_AVX512_VBMI=OFF \
        -DGGML_AVX512_VNNI=OFF \
        -DGGML_AVX_VNNI=OFF \
        -DGGML_BLAS=OFF \
        -DGGML_CPU_ALL_VARIANTS=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_OPENMP=ON \
        -DNEMO_SPEECH_BUILD_ASR=ON \
        -DNEMO_SPEECH_BUILD_CLI=ON \
        -DNEMO_SPEECH_BUILD_DIAR=OFF \
        -DNEMO_SPEECH_BUILD_EXAMPLES=OFF \
        -DNEMO_SPEECH_BUILD_GRPC=OFF \
        -DNEMO_SPEECH_BUILD_HTTP=OFF \
        -DNEMO_SPEECH_BUILD_NMT=OFF \
        -DNEMO_SPEECH_BUILD_TESTS=OFF \
        -DNEMO_SPEECH_BUILD_TOOLS=OFF \
        -DNEMO_SPEECH_BUILD_TTS=OFF \
        -DNEMO_SPEECH_GGML_PATCHED=OFF \
        -DNEMO_SPEECH_WITH_FLASHLIGHT=OFF \
        -DNEMO_SPEECH_WITH_GRPC=OFF \
        -DNEMO_SPEECH_WITH_NMT=OFF \
        -DNEMO_SPEECH_WITH_NORM=OFF \
    && cmake --build build --parallel --target nemo_speech_cli \
    && install -d /opt/native-asr/bin /opt/native-asr/lib \
        /opt/native-asr/share/licenses/nemo-speech/ggml \
    && install -m 0755 build/bin/nemo-speech /opt/native-asr/bin/ \
    && for library in build/bin/libggml*.so* build/bin/libnemo_speech*.so*; \
      do test -e "$library" && cp -a "$library" /opt/native-asr/lib/; done \
    && install -m 0644 LICENSE NOTICE THIRD_PARTY_NOTICES.md \
        /opt/native-asr/share/licenses/nemo-speech/ \
    && install -m 0644 ggml/LICENSE \
        /opt/native-asr/share/licenses/nemo-speech/ggml/ \
    && patchelf --set-rpath '$ORIGIN/../lib' /opt/native-asr/bin/nemo-speech \
    && find /opt/native-asr/lib -type f -name '*.so*' -exec \
        patchelf --set-rpath '$ORIGIN' {} \; \
    && test "$(patchelf --print-rpath /opt/native-asr/bin/nemo-speech)" = '$ORIGIN/../lib' \
    && ldd /opt/native-asr/bin/nemo-speech \
    && ! ldd /opt/native-asr/bin/nemo-speech | grep -F 'not found'

FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG NEMO_SPEECH_VERSION=1.0.0
ARG NEMO_SPEECH_REVISION=5be7bfb104802131e61fe679b3f1401b27270216
ARG GGML_REVISION=c03b4e2bcece5134827881af90242086daf75be5

LABEL org.opencontainers.image.title="native-asr NeMo-Speech.cpp runtime" \
      org.opencontainers.image.description="CPU-only NeMo-Speech.cpp ASR runtime; model weights are mounted at /models" \
      org.opencontainers.image.source="https://github.com/gracee3/native-asr" \
      org.opencontainers.image.version="${NEMO_SPEECH_VERSION}" \
      org.opencontainers.image.revision="${NEMO_SPEECH_REVISION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ffmpeg \
        jq \
        libgcc-s1 \
        libgomp1 \
        libsentencepiece0 \
        libstdc++6 \
        time \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 native-asr \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir /nonexistent --shell /usr/sbin/nologin native-asr

COPY --from=builder /opt/native-asr/ /opt/native-asr/
COPY --chmod=0755 docker/nemo-speech/entrypoint.sh /usr/local/bin/native-asr-nemo

ENV HOME=/tmp \
    NATIVE_ASR_RUNTIME=nemo-speech \
    NATIVE_ASR_RUNTIME_VERSION=${NEMO_SPEECH_VERSION} \
    NATIVE_ASR_RUNTIME_REVISION=${NEMO_SPEECH_REVISION} \
    NATIVE_ASR_GGML_REVISION=${GGML_REVISION} \
    NATIVE_ASR_NEMO_CPU_THREADS=4 \
    NEMO_SPEECH_BIN=/opt/native-asr/bin/nemo-speech

USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/native-asr-nemo"]
CMD ["--help"]
