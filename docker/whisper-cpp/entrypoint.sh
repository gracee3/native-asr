#!/usr/bin/env bash
set -euo pipefail

whisper_bin=${WHISPER_CPP_BIN:-/opt/whisper/bin/whisper-cli}
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage:
  native-asr-whisper transcribe --model MODEL [OPTIONS] AUDIO
  native-asr-whisper version

Options: --threads N --format text|txt|json --language en --vad auto|on|none
EOF
}

version() {
  "$whisper_bin" --version
  printf 'native_asr_runtime=%s\n' "${NATIVE_ASR_RUNTIME:-whisper-cpp}"
  printf 'native_asr_runtime_version=%s\n' "${NATIVE_ASR_RUNTIME_VERSION:-unknown}"
  printf 'native_asr_runtime_revision=%s\n' "${NATIVE_ASR_RUNTIME_REVISION:-unknown}"
  ffmpeg -version | sed -n '1p'
}

transcribe() {
  local model='' audio='' format=text language=en vad=auto threads
  threads=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 1)
  while (($#)); do
    case $1 in
      --model) model=${2:-}; shift 2 ;;
      --threads) threads=${2:-}; shift 2 ;;
      --format) format=${2:-}; shift 2 ;;
      --language) language=${2:-}; shift 2 ;;
      --vad) vad=${2:-}; shift 2 ;;
      --help|-h) usage; return ;;
      --*) die "unknown option: $1" ;;
      *) [[ -z $audio ]] || die 'only one audio input is supported'; audio=$1; shift ;;
    esac
  done
  [[ -r $model ]] || die "model is not readable: $model"
  [[ $(basename -- "$model") == ggml-small.en.bin ]] || die 'unsupported whisper.cpp model'
  [[ -r $audio ]] || die "audio input is not readable: $audio"
  [[ $threads =~ ^[1-9][0-9]*$ ]] || die '--threads must be a positive integer'
  [[ $language == en ]] || die 'whisper.cpp small.en supports only --language en'
  [[ $format == text || $format == txt || $format == json ]] || die 'unsupported format'
  [[ $vad == auto || $vad == on || $vad == none ]] || die '--vad must be auto, on, or none'
  local work normalized output use_vad=false
  work=$(mktemp -d "${TMPDIR:-/tmp}/native-asr-whisper.XXXXXX")
  local work_quoted
  printf -v work_quoted '%q' "$work"
  trap "rm -rf -- $work_quoted" EXIT
  normalized=$work/input.wav
  output=$work/result
  ffmpeg -nostdin -hide_banner -loglevel error -y -i "$audio" -map_metadata -1 \
    -vn -sn -dn -ar 16000 -ac 1 -c:a pcm_s16le "$normalized"
  if [[ $vad == on ]]; then
    use_vad=true
  elif [[ $vad == auto ]]; then
    local seconds
    seconds=$(ffprobe -v error -show_entries format=duration \
      -of default=noprint_wrappers=1:nokey=1 "$normalized")
    awk -v seconds="$seconds" 'BEGIN { exit !(seconds > 30) }' && use_vad=true || true
  fi
  local -a args=(-m "$model" -f "$normalized" -l en -t "$threads" -oj -of "$output" -np)
  if [[ $use_vad == true ]]; then
    local vad_model
    vad_model=$(dirname -- "$(dirname -- "$model")")/_shared/ggml-silero-v6.2.0.bin
    [[ -r $vad_model ]] || die "VAD model is not readable: $vad_model"
    args+=(--vad -vm "$vad_model")
  fi
  "$whisper_bin" "${args[@]}" >/dev/null
  [[ -s $output.json ]] || die 'whisper.cpp emitted no JSON result'
  local text
  text=$(jq -r '[.transcription[]?.text // empty] | join(" ") | gsub("^[ ]+|[ ]+$"; "")' "$output.json")
  if [[ $format == json ]]; then
    jq -cn --arg text "$text" --arg model_path "$model" --arg audio_path "$audio" \
      --argjson vad "$use_vad" --slurpfile raw "$output.json" \
      '{runtime:"whisper-cpp",text:$text,model_path:$model_path,audio_path:$audio_path,
        segmentation:(if $vad then "silero-vad" else "native-windowing" end),raw:$raw[0]}'
  else
    printf '%s\n' "$text"
  fi
}

command=${1:---help}; (($# == 0)) || shift
case $command in
  transcribe) transcribe "$@" ;;
  version|versions|--version) version ;;
  help|--help|-h) usage ;;
  *) die "unknown command: $command" ;;
esac
