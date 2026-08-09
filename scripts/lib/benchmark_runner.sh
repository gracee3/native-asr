#!/usr/bin/env bash

# Shared orchestration helpers for long, sequential benchmark runs.

benchmark_runner_die() {
  printf 'error: %s\n' "$*" >&2
  return 1
}

benchmark_runner_inhibit_sleep() {
  local script_path=$1
  if [[ ${NATIVE_ASR_INHIBIT_SLEEP:-1} != 1 ||
        ${NATIVE_ASR_SLEEP_INHIBITED:-0} == 1 ]]; then
    return
  fi
  if ! command -v systemd-inhibit >/dev/null 2>&1; then
    printf '%s\n' \
      'warning: systemd-inhibit is unavailable; sleep is not automatically blocked' >&2
    return
  fi
  export NATIVE_ASR_SLEEP_INHIBITED=1
  exec systemd-inhibit \
    --what=sleep:idle \
    --who=native-asr \
    --why='native-asr benchmark in progress' \
    --mode=block \
    "$script_path"
}

benchmark_runner_lock() {
  BENCHMARK_RUN_ROOT=$(dirname -- "$NATIVE_ASR_BENCHMARKS")
  export BENCHMARK_RUN_ROOT
  mkdir -p "$BENCHMARK_RUN_ROOT"
  exec 9>"$BENCHMARK_RUN_ROOT/runner.lock"
  flock -n 9 || benchmark_runner_die \
    "another native-asr benchmark runner holds $BENCHMARK_RUN_ROOT/runner.lock"
}

benchmark_runner_phase() {
  printf '%s\t%s\n' "$(date --iso-8601=seconds)" "$1" |
    tee "$BENCHMARK_RUN_ROOT/phase.tsv"
}

benchmark_runner_check_power() {
  if [[ ${NATIVE_ASR_SKIP_POWER_CHECK:-0} == 1 ]]; then
    printf '%s\n' 'warning: benchmark power checks were explicitly disabled' >&2
    return
  fi

  if command -v powerprofilesctl >/dev/null 2>&1; then
    local profile
    profile=$(powerprofilesctl get)
    [[ $profile == performance ]] || benchmark_runner_die \
      "power profile is $profile; select performance or set NATIVE_ASR_SKIP_POWER_CHECK=1"
  else
    printf '%s\n' \
      'warning: powerprofilesctl is unavailable; power profile was not verified' >&2
  fi

  local detected=0 online=0 supply kind state
  shopt -s nullglob
  for supply in /sys/class/power_supply/*; do
    [[ -r $supply/type && -r $supply/online ]] || continue
    read -r kind < "$supply/type"
    case $kind in
      Mains|USB|USB_C|USB_PD)
        detected=1
        read -r state < "$supply/online"
        [[ $state == 1 ]] && online=1
        ;;
    esac
  done
  shopt -u nullglob

  if ((detected && !online)); then
    benchmark_runner_die \
      'no online AC/USB power source detected; connect power before benchmarking'
  elif ((!detected)); then
    printf '%s\n' \
      'warning: no readable AC/USB power source was found; power was not verified' >&2
  fi
}

benchmark_runner_preflight() {
  local expected_revision=$1 required
  for required in git jq flock just; do
    command -v "$required" >/dev/null 2>&1 || benchmark_runner_die \
      "required command is unavailable: $required"
  done
  [[ -z $(git -C "$NATIVE_ASR_REPO_ROOT" status --porcelain) ]] ||
    benchmark_runner_die 'the repository must be clean before benchmarking'
  [[ $(git -C "$NATIVE_ASR_REPO_ROOT" rev-parse HEAD) == "$expected_revision" ]] ||
    benchmark_runner_die "HEAD does not match expected revision $expected_revision"
  benchmark_runner_check_power
}
