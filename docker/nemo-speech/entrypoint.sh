#!/usr/bin/env bash
set -euo pipefail

nemo_bin=${NEMO_SPEECH_BIN:-/opt/native-asr/bin/nemo-speech}
cascade_bin=${NATIVE_ASR_CASCADE_BIN:-/opt/native-asr/bin/native-asr-cascade}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Usage:
  native-asr-nemo transcribe --model MODEL_GGUF [OPTIONS] AUDIO
  native-asr-nemo cascade [OPTIONS] < 16k-mono-f32le.pcm
  native-asr-nemo version

Options:
  --threads N              must be 4 for this pinned runtime
  --format text|txt|json   output format (default: text)
  --language CODE          optional language code or prompt
  --stream auto|on|none    stateful streaming-file mode (default: auto)

MODEL_GGUF must be below the read-only /models mount in normal container use.
Input audio is normalized to a temporary 16 kHz mono PCM16 WAV.
The cascade consumes raw 16 kHz mono float32 PCM on stdin and never saves it.
EOF
}

version() {
  "$nemo_bin" --version
  printf 'native_asr_runtime=%s\n' "${NATIVE_ASR_RUNTIME:-nemo-speech}"
  printf 'native_asr_runtime_version=%s\n' "${NATIVE_ASR_RUNTIME_VERSION:-unknown}"
  printf 'native_asr_runtime_revision=%s\n' "${NATIVE_ASR_RUNTIME_REVISION:-unknown}"
  printf 'ggml_revision=%s\n' "${NATIVE_ASR_GGML_REVISION:-unknown}"
  printf 'cpu_threads=%s\n' "${NATIVE_ASR_NEMO_CPU_THREADS:-4}"
  ffmpeg -version | sed -n '1p'
}

emit_result() {
  local result_file=$1 format=$2
  case $format in
    text|txt) jq -er '.text' "$result_file" ;;
    json) jq -c . "$result_file" ;;
    *) die "unsupported format: $format" ;;
  esac
}

transcribe() {
  local model='' audio='' format=text language='' stream_mode=auto
  local threads=${NATIVE_ASR_NEMO_CPU_THREADS:-4}

  while (($#)); do
    case $1 in
      --model)
        (($# >= 2)) || die '--model requires a GGUF file'
        model=$2
        shift 2
        ;;
      --threads)
        (($# >= 2)) || die '--threads requires a positive integer'
        threads=$2
        shift 2
        ;;
      --format)
        (($# >= 2)) || die '--format requires text, txt, or json'
        format=$2
        shift 2
        ;;
      --language)
        (($# >= 2)) || die '--language requires a code or prompt'
        language=$2
        shift 2
        ;;
      --stream)
        (($# >= 2)) || die '--stream requires auto, on, or none'
        stream_mode=$2
        shift 2
        ;;
      --help|-h)
        usage
        return 0
        ;;
      --*) die "unknown option: $1" ;;
      *)
        [[ -z $audio ]] || die 'only one audio input is supported'
        audio=$1
        shift
        ;;
    esac
  done

  [[ -n $model ]] || die '--model is required'
  [[ -r $model ]] || die "model GGUF is not readable: $model"
  [[ -n $audio ]] || die 'an audio input is required'
  [[ -r $audio ]] || die "audio input is not readable: $audio"
  [[ $threads =~ ^[1-9][0-9]*$ ]] || die '--threads must be a positive integer'
  [[ $threads == ${NATIVE_ASR_NEMO_CPU_THREADS:-4} ]] || \
    die "this pinned NeMo-Speech.cpp runtime uses a fixed ${NATIVE_ASR_NEMO_CPU_THREADS:-4} CPU threads"
  [[ $stream_mode == auto || $stream_mode == on || $stream_mode == none ]] || \
    die '--stream must be auto, on, or none'

  local model_kind cache_aware=false
  case $(basename -- "$model") in
    parakeet-tdt-0.6b-v3.q8_0.gguf) model_kind=parakeet-tdt ;;
    nemotron-speech-streaming-en-0.6b.q8_0.gguf)
      model_kind=nemotron-streaming-en
      cache_aware=true
      [[ -z $language ]] && language=en
      [[ $language == en ]] || die 'Nemotron Streaming EN supports only --language en'
      ;;
    nemotron-3.5-asr-streaming-0.6b.q8_0.gguf)
      model_kind=nemotron-3.5-streaming
      cache_aware=true
      ;;
    parakeet-ctc-1.1b.q8_0.gguf)
      model_kind=parakeet-ctc
      [[ -z $language ]] && language=en
      [[ $language == en ]] || die 'Parakeet CTC 1.1B supports only --language en'
      ;;
    *) die "unsupported NeMo-Speech.cpp model file: $(basename -- "$model")" ;;
  esac

  local use_stream=false
  if [[ $stream_mode == on ]]; then
    [[ $cache_aware == true ]] || die "$model_kind is not cache-aware and rejects streaming"
    use_stream=true
  elif [[ $stream_mode == auto && $cache_aware == true ]]; then
    use_stream=true
  fi

  local work
  work=$(mktemp -d "${TMPDIR:-/tmp}/native-asr-nemo.XXXXXX")
  local work_quoted
  printf -v work_quoted '%q' "$work"
  trap "rm -rf -- $work_quoted" EXIT
  local normalized=$work/input.wav result_file=$work/result.json
  ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$audio" -map_metadata -1 -vn -sn -dn \
    -ar 16000 -ac 1 -c:a pcm_s16le "$normalized"

  local -a args=(
    transcribe "$normalized"
    --model "$model"
    --device cpu
    --format json
    --word-times
  )
  [[ -z $language ]] || args+=(--language "$language")
  [[ $use_stream == false ]] || args+=(--stream)

  local raw_json
  raw_json=$("$nemo_bin" "${args[@]}")
  jq -cn \
    --argjson raw "$raw_json" \
    --arg model_path "$model" \
    --arg audio_path "$audio" \
    --arg language_request "$language" \
    --arg model_kind "$model_kind" \
    --argjson threads "$threads" \
    --argjson streaming "$use_stream" \
    '$raw + {
      runtime: "nemo-speech",
      model_path: $model_path,
      audio_path: $audio_path,
      language_request: (if $language_request == "" then null else $language_request end),
      model_kind: $model_kind,
      threads: $threads,
      segmentation: (if $streaming then "native-streaming-file" else "native-full-utterance" end)
    }' > "$result_file"

  emit_result "$result_file" "$format"
}

if (($#)); then
  command=$1
  shift
else
  command=--help
fi
case $command in
  transcribe) transcribe "$@" ;;
  cascade) exec "$cascade_bin" "$@" ;;
  version|versions|--version) version ;;
  help|--help|-h) usage ;;
  *) die "unknown command: $command" ;;
esac
