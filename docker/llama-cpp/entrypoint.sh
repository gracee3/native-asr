#!/usr/bin/env bash
set -euo pipefail

llama_server=${LLAMA_SERVER_BIN:-/opt/llama/bin/llama-server}
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage:
  native-asr-adjudicator serve --model MODEL [--threads 4] [--context 4096] [--slots 1]
  native-asr-adjudicator version

The serve command accepts JSONL requests on stdin and emits JSONL responses on
stdout. Its llama-server listens only on the container loopback interface.
EOF
}

version() {
  "$llama_server" --version
  printf 'native_asr_runtime=%s\n' "${NATIVE_ASR_RUNTIME:-llama-cpp}"
  printf 'native_asr_runtime_version=%s\n' "${NATIVE_ASR_RUNTIME_VERSION:-unknown}"
  printf 'native_asr_runtime_revision=%s\n' "${NATIVE_ASR_RUNTIME_REVISION:-unknown}"
}

serve() {
  local model='' threads=4 context=4096 slots=1
  while (($#)); do
    case $1 in
      --model) model=${2:-}; shift 2 ;;
      --threads) threads=${2:-}; shift 2 ;;
      --context) context=${2:-}; shift 2 ;;
      --slots) slots=${2:-}; shift 2 ;;
      --help|-h) usage; return ;;
      --*) die "unknown option: $1" ;;
      *) die "unexpected argument: $1" ;;
    esac
  done
  [[ -r $model && -f $model ]] || die "model is not a readable file: $model"
  [[ $threads == 4 ]] || die 'the adjudicator runtime is locked to four threads'
  [[ $context == 4096 ]] || die 'the adjudicator runtime is locked to a 4096-token context'
  [[ $slots == 1 ]] || die 'the adjudicator runtime is locked to one slot'

  local port=8080 server_pid started_ns ready_ns load_ms
  started_ns=$(date +%s%N)
  "$llama_server" \
    --model "$model" \
    --host 127.0.0.1 \
    --port "$port" \
    --threads "$threads" \
    --threads-batch "$threads" \
    --ctx-size "$context" \
    --parallel "$slots" \
    --seed 0 \
    --temp 0 \
    --top-k 1 \
    --jinja \
    --no-webui \
    --perf >&2 &
  server_pid=$!
  cleanup() {
    if kill -0 "$server_pid" 2>/dev/null; then
      kill -TERM "$server_pid" 2>/dev/null || true
    fi
    wait "$server_pid" 2>/dev/null || true
  }
  trap cleanup EXIT
  trap 'cleanup; trap - EXIT; exit 143' INT TERM

  local attempt ready=false
  for attempt in $(seq 1 1200); do
    if curl --silent --fail --max-time 1 "http://127.0.0.1:$port/health" >/dev/null; then
      ready_ns=$(date +%s%N)
      load_ms=$(((ready_ns - started_ns) / 1000000))
      jq -cn --argjson milliseconds "$load_ms" \
        '{event:"ready",protocol_version:1,load_seconds:($milliseconds / 1000)}'
      ready=true
      break
    fi
    kill -0 "$server_pid" 2>/dev/null || die 'llama-server exited during startup'
    sleep 0.1
  done
  [[ $ready == true ]] || die 'llama-server startup timed out'

  local line command request_id system input schema max_tokens payload raw
  while IFS= read -r line; do
    command=$(jq -er '.command' <<< "$line") || die 'invalid worker request'
    if [[ $command == shutdown ]]; then
      break
    fi
    [[ $command == adjudicate ]] || die "unknown worker command: $command"
    request_id=$(jq -er '.request_id | select(type == "string")' <<< "$line") || \
      die 'worker request has no request_id'
    system=$(jq -er '.prompt.system | select(type == "string")' <<< "$line") || \
      die 'worker request has no system prompt'
    input=$(jq -ec '.prompt.input | select(type == "object")' <<< "$line") || \
      die 'worker request has no structured input'
    schema=$(jq -ec '.prompt.response_schema | select(type == "object")' <<< "$line") || \
      die 'worker request has no response schema'
    max_tokens=$(jq -er '.max_tokens | select(type == "number")' <<< "$line") || \
      die 'worker request has no token bound'
    payload=$(jq -cn \
      --arg system "$system" \
      --argjson input "$input" \
      --argjson schema "$schema" \
      --argjson max_tokens "$max_tokens" '
        {
          model:"local-adjudicator",
          stream:false,
          messages:[
            {role:"system",content:$system},
            {role:"user",content:($input | tojson)}
          ],
          response_format:{
            type:"json_schema",
            json_schema:{name:"asr_adjudication",strict:true,schema:$schema}
          },
          temperature:0,
          top_k:1,
          top_p:1,
          seed:0,
          max_tokens:$max_tokens,
          cache_prompt:true,
          reasoning_effort:"none",
          chat_template_kwargs:{enable_thinking:false}
        }')
    if raw=$(curl --silent --show-error --fail \
        --header 'Content-Type: application/json' \
        --data-binary "$payload" \
        "http://127.0.0.1:$port/v1/chat/completions"); then
      if jq -e 'type == "object"' <<< "$raw" >/dev/null; then
        jq -cn --arg request_id "$request_id" --argjson response "$raw" \
          '{request_id:$request_id,response:$response}'
      else
        jq -cn --arg request_id "$request_id" \
          '{request_id:$request_id,error:"llama-server emitted malformed JSON"}'
      fi
    else
      jq -cn --arg request_id "$request_id" \
        '{request_id:$request_id,error:"llama-server request failed"}'
      kill -0 "$server_pid" 2>/dev/null || exit 1
    fi
  done
  cleanup
  trap - EXIT INT TERM
}

command=${1:---help}; (($# == 0)) || shift
case $command in
  serve) serve "$@" ;;
  version|versions|--version) version ;;
  help|--help|-h) usage ;;
  *) die "unknown command: $command" ;;
esac
