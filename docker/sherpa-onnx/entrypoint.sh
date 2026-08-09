#!/usr/bin/env bash
set -euo pipefail

bin_dir=${SHERPA_BIN_DIR:-/usr/local/libexec/native-asr}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Usage:
  native-asr-sherpa transcribe --model MODEL_DIR [OPTIONS] AUDIO
  native-asr-sherpa version

Options:
  --threads N              inference threads (default: available CPUs)
  --format text|txt|json   output format (default: text)
  --language CODE          Canary source/target language (default: en)
  --vad auto|on|none       long-form VAD mode (default: auto)

MODEL_DIR must be below the read-only /models mount in normal container use.
Input audio is normalized to a temporary 16 kHz mono PCM16 WAV.
EOF
}

version() {
  "$bin_dir/sherpa-onnx-version"
  printf 'native_asr_runtime=%s\n' "${NATIVE_ASR_RUNTIME:-sherpa-onnx}"
  printf 'native_asr_runtime_version=%s\n' "${NATIVE_ASR_RUNTIME_VERSION:-unknown}"
  printf 'native_asr_runtime_revision=%s\n' "${NATIVE_ASR_RUNTIME_REVISION:-unknown}"
  printf 'onnxruntime_version=%s\n' "${NATIVE_ASR_ONNXRUNTIME_VERSION:-unknown}"
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

require_model_file() {
  local path=$1
  [[ -r $path ]] || die "required model file is not readable: $path"
}

transcribe() {
  local model='' audio='' format=text language=en vad_mode=auto
  local threads
  threads=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')

  while (($#)); do
    case $1 in
      --model)
        (($# >= 2)) || die '--model requires a directory'
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
        (($# >= 2)) || die '--language requires a code'
        language=$2
        shift 2
        ;;
      --vad)
        (($# >= 2)) || die '--vad requires auto, on, or none'
        vad_mode=$2
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
  [[ -d $model ]] || die "model directory does not exist: $model"
  [[ -n $audio ]] || die 'an audio input is required'
  [[ -r $audio ]] || die "audio input is not readable: $audio"
  [[ $threads =~ ^[1-9][0-9]*$ ]] || die '--threads must be a positive integer'
  [[ $vad_mode == auto || $vad_mode == on || $vad_mode == none ]] || \
    die '--vad must be auto, on, or none'

  local work
  work=$(mktemp -d "${TMPDIR:-/tmp}/native-asr-sherpa.XXXXXX")
  local work_quoted
  printf -v work_quoted '%q' "$work"
  trap "rm -rf -- $work_quoted" EXIT
  local normalized=$work/input.wav result_file=$work/result.json
  ffmpeg -nostdin -hide_banner -loglevel error -y \
    -i "$audio" -map_metadata -1 -vn -sn -dn \
    -ar 16000 -ac 1 -c:a pcm_s16le "$normalized"

  local model_kind
  case $(basename -- "$model") in
    parakeet-unified-en-0.6b-int8) model_kind=parakeet-unified ;;
    canary-180m-flash-int8) model_kind=canary ;;
    nemotron-streaming-en-560ms-int8) model_kind=nemotron-streaming ;;
    *) die "unsupported sherpa model directory: $(basename -- "$model")" ;;
  esac

  local segmentation=none
  local shared_dir
  shared_dir=$(dirname -- "$model")/_shared
  local vad_model=$shared_dir/silero_vad.onnx
  if [[ $model_kind != nemotron-streaming && $vad_mode != none ]]; then
    if [[ -r $vad_model ]]; then
      segmentation=silero-vad
    elif [[ $vad_mode == on ]]; then
      die "--vad on requires: $vad_model"
    fi
  fi

  local -a model_args=(--num-threads="$threads")
  case $model_kind in
    parakeet-unified)
      [[ $language == en ]] || die 'Parakeet Unified EN supports only --language en'
      require_model_file "$model/encoder.int8.onnx"
      require_model_file "$model/decoder.int8.onnx"
      require_model_file "$model/joiner.int8.onnx"
      require_model_file "$model/tokens.txt"
      model_args+=(
        --encoder="$model/encoder.int8.onnx"
        --decoder="$model/decoder.int8.onnx"
        --joiner="$model/joiner.int8.onnx"
        --tokens="$model/tokens.txt"
        --model-type=nemo_transducer
      )
      ;;
    canary)
      [[ $language == en || $language == es || $language == de || $language == fr ]] || \
        die 'Canary language must be en, es, de, or fr'
      require_model_file "$model/encoder.int8.onnx"
      require_model_file "$model/decoder.int8.onnx"
      require_model_file "$model/tokens.txt"
      model_args+=(
        --canary-encoder="$model/encoder.int8.onnx"
        --canary-decoder="$model/decoder.int8.onnx"
        --tokens="$model/tokens.txt"
        --canary-src-lang="$language"
        --canary-tgt-lang="$language"
      )
      ;;
    nemotron-streaming)
      [[ $language == en ]] || die 'Nemotron Streaming EN supports only --language en'
      require_model_file "$model/encoder.int8.onnx"
      require_model_file "$model/decoder.int8.onnx"
      require_model_file "$model/joiner.int8.onnx"
      require_model_file "$model/tokens.txt"
      model_args+=(
        --encoder="$model/encoder.int8.onnx"
        --decoder="$model/decoder.int8.onnx"
        --joiner="$model/joiner.int8.onnx"
        --tokens="$model/tokens.txt"
      )
      ;;
  esac

  if [[ $segmentation == silero-vad ]]; then
    local segments_file=$work/segments.txt
    "$bin_dir/sherpa-onnx-vad-with-offline-asr" \
      --silero-vad-model="$vad_model" \
      --silero-vad-threshold=0.2 \
      --silero-vad-min-speech-duration=0.2 \
      "${model_args[@]}" "$normalized" > "$segments_file"
    jq -Rn \
      --arg runtime sherpa-onnx \
      --arg model_path "$model" \
      --arg audio_path "$audio" \
      --arg language "$language" \
      '[inputs
        | capture("^(?<start>[0-9.]+) -- (?<end>[0-9.]+): (?<text>.*)$")?
        | select(. != null)
        | {start: (.start | tonumber), end: (.end | tonumber), text: .text}
      ] as $segments
      | {
          runtime: $runtime,
          model_path: $model_path,
          audio_path: $audio_path,
          language: $language,
          segmentation: "silero-vad",
          text: ($segments | map(.text) | join(" ")),
          segments: $segments
        }' < "$segments_file" > "$result_file"
  elif [[ $model_kind == nemotron-streaming ]]; then
    local combined=$work/runtime.log raw_json
    if ! "$bin_dir/sherpa-onnx" "${model_args[@]}" "$normalized" > "$combined" 2>&1; then
      cat "$combined" >&2
      return 1
    fi
    raw_json=$(awk '/^\{.*\}$/ { line=$0 } END { print line }' "$combined")
    [[ -n $raw_json ]] || { cat "$combined" >&2; die 'sherpa streaming output contained no JSON result'; }
    jq -cn \
      --argjson raw "$raw_json" \
      --arg model_path "$model" \
      --arg audio_path "$audio" \
      --arg language "$language" \
      '$raw + {
        runtime: "sherpa-onnx",
        model_path: $model_path,
        audio_path: $audio_path,
        language: $language,
        segmentation: "native-streaming-file"
      }' > "$result_file"
  else
    local raw_json
    raw_json=$("$bin_dir/sherpa-onnx-offline" "${model_args[@]}" "$normalized")
    jq -cn \
      --argjson raw "$raw_json" \
      --arg model_path "$model" \
      --arg audio_path "$audio" \
      --arg language "$language" \
      '$raw + {
        runtime: "sherpa-onnx",
        model_path: $model_path,
        audio_path: $audio_path,
        language: $language,
        segmentation: "none"
      }' > "$result_file"
  fi

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
  version|versions|--version) version ;;
  help|--help|-h) usage ;;
  *) die "unknown command: $command" ;;
esac
