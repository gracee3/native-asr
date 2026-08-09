#!/usr/bin/env bash
set -euo pipefail

core=${MOONSHINE_BIN:-/opt/moonshine/bin/native-asr-moonshine-core}
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage:
  native-asr-moonshine transcribe --model MODEL_DIR [OPTIONS] AUDIO
  native-asr-moonshine stream --model MODEL_DIR [OPTIONS] AUDIO
  native-asr-moonshine version

Options: --format text|txt|json --language en
         --stream auto|on|none --update-interval-ms N --pace
EOF
}

version() {
  printf 'native_asr_runtime=%s\n' "${NATIVE_ASR_RUNTIME:-moonshine}"
  printf 'native_asr_runtime_version=%s\n' "${NATIVE_ASR_RUNTIME_VERSION:-unknown}"
  printf 'native_asr_runtime_revision=%s\n' "${NATIVE_ASR_RUNTIME_REVISION:-unknown}"
  ffmpeg -version | sed -n '1p'
}

run_audio() {
  local events=$1; shift
  local model='' audio='' format=text language=en stream=auto interval=500 pace=false
  while (($#)); do
    case $1 in
      --model) model=${2:-}; shift 2 ;;
      --threads) die 'Moonshine manages its ONNX Runtime thread pool; --threads is not configurable' ;;
      --format) format=${2:-}; shift 2 ;;
      --language) language=${2:-}; shift 2 ;;
      --stream) stream=${2:-}; shift 2 ;;
      --update-interval-ms) interval=${2:-}; shift 2 ;;
      --pace) pace=true; shift ;;
      --help|-h) usage; return ;;
      --*) die "unknown option: $1" ;;
      *) [[ -z $audio ]] || die 'only one audio input is supported'; audio=$1; shift ;;
    esac
  done
  [[ -d $model ]] || die "model directory does not exist: $model"
  [[ -r $audio ]] || die "audio input is not readable: $audio"
  [[ $language == en ]] || die 'Moonshine Small Streaming supports only --language en'
  [[ $stream == auto || $stream == on ]] || die 'Moonshine Small Streaming requires streaming mode'
  [[ $interval =~ ^[1-9][0-9]*$ ]] || die '--update-interval-ms must be a positive integer'
  [[ $format == text || $format == txt || $format == json ]] || die 'unsupported format'
  local work normalized
  work=$(mktemp -d "${TMPDIR:-/tmp}/native-asr-moonshine.XXXXXX")
  local work_quoted
  printf -v work_quoted '%q' "$work"
  trap "rm -rf -- $work_quoted" EXIT
  normalized=$work/input.wav
  ffmpeg -nostdin -hide_banner -loglevel error -y -i "$audio" -map_metadata -1 \
    -vn -sn -dn -ar 16000 -ac 1 -c:a pcm_s16le "$normalized"
  local -a args=(--model "$model" --update-interval "$(awk -v ms="$interval" 'BEGIN { print ms / 1000 }')")
  [[ $events == true ]] && args+=(--events)
  [[ $pace == true ]] && args+=(--pace)
  args+=("$normalized")
  if [[ $events == true || $format == json ]]; then
    "$core" "${args[@]}"
  else
    "$core" "${args[@]}" | jq -er '.text'
  fi
}

command=${1:---help}; (($# == 0)) || shift
case $command in
  transcribe) run_audio false "$@" ;;
  stream) run_audio true "$@" ;;
  version|versions|--version) version ;;
  help|--help|-h) usage ;;
  *) die "unknown command: $command" ;;
esac
