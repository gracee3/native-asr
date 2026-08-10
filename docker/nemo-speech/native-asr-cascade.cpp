// SPDX-License-Identifier: MIT
// Repository-owned two-pass adapter for the pinned NeMo-Speech.cpp C ABI.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include "cascade-boundary.h"
#include "nemo_speech/asr.h"

namespace {

constexpr int kSampleRate = 16000;
constexpr size_t kChunkSamples = 320;  // 20 ms at 16 kHz.
constexpr int kEndpointMs = 1200;
constexpr const char* kNemotronAlias = "nemo:nemotron-streaming-en";
constexpr const char* kParakeetAlias = "nemo:parakeet-tdt-v3";

using Clock = std::chrono::steady_clock;

std::string json_string(const std::string& input) {
    std::string output = "\"";
    char buffer[7];
    for (unsigned char c : input) {
        switch (c) {
            case '\"': output += "\\\""; break;
            case '\\': output += "\\\\"; break;
            case '\b': output += "\\b"; break;
            case '\f': output += "\\f"; break;
            case '\n': output += "\\n"; break;
            case '\r': output += "\\r"; break;
            case '\t': output += "\\t"; break;
            default:
                if (c < 0x20) {
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
                    output += buffer;
                } else {
                    output += static_cast<char>(c);
                }
        }
    }
    output += "\"";
    return output;
}

std::string trim(const std::string& text) {
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

template <typename T>
T read_le(const uint8_t* bytes) {
    T value;
    std::memcpy(&value, bytes, sizeof(value));
    return value;
}

bool read_pcm16_wav(const std::string& path, std::vector<float>& samples, std::string& error) {
    FILE* file = std::fopen(path.c_str(), "rb");
    if (!file) {
        error = "cannot open normalized WAV";
        return false;
    }
    std::fseek(file, 0, SEEK_END);
    const long length = std::ftell(file);
    std::fseek(file, 0, SEEK_SET);
    if (length < 44) {
        std::fclose(file);
        error = "normalized WAV is too small";
        return false;
    }
    std::vector<uint8_t> bytes(static_cast<size_t>(length));
    const size_t count = std::fread(bytes.data(), 1, bytes.size(), file);
    std::fclose(file);
    if (count != bytes.size()) {
        error = "short read from normalized WAV";
        return false;
    }
    if (std::memcmp(bytes.data(), "RIFF", 4) != 0 ||
        std::memcmp(bytes.data() + 8, "WAVE", 4) != 0) {
        error = "normalized input is not RIFF/WAVE";
        return false;
    }

    uint16_t format = 0, channels = 0, bits = 0;
    uint32_t rate = 0;
    const uint8_t* data = nullptr;
    size_t data_length = 0;
    size_t position = 12;
    while (position + 8 <= bytes.size()) {
        const uint32_t chunk_length = read_le<uint32_t>(bytes.data() + position + 4);
        const size_t body = position + 8;
        if (body + chunk_length > bytes.size()) {
            error = "truncated normalized WAV chunk";
            return false;
        }
        if (std::memcmp(bytes.data() + position, "fmt ", 4) == 0 && chunk_length >= 16) {
            format = read_le<uint16_t>(bytes.data() + body);
            channels = read_le<uint16_t>(bytes.data() + body + 2);
            rate = read_le<uint32_t>(bytes.data() + body + 4);
            bits = read_le<uint16_t>(bytes.data() + body + 14);
        } else if (std::memcmp(bytes.data() + position, "data", 4) == 0) {
            data = bytes.data() + body;
            data_length = chunk_length;
        }
        position = body + chunk_length + (chunk_length & 1U);
    }
    if (!data || format != 1 || channels != 1 || rate != kSampleRate || bits != 16) {
        error = "expected normalized 16 kHz mono PCM16 WAV";
        return false;
    }
    samples.resize(data_length / 2);
    for (size_t i = 0; i < samples.size(); ++i) {
        samples[i] = static_cast<float>(read_le<int16_t>(data + i * 2)) / 32768.0f;
    }
    return true;
}

struct Recognizer {
    nemo_speech_asr_recognizer* value = nullptr;
    ~Recognizer() { nemo_speech_asr_destroy(value); }
};

struct Stream {
    nemo_speech_asr_stream* value = nullptr;
    ~Stream() { nemo_speech_asr_stream_close(value); }
};

struct Result {
    nemo_speech_asr_result* value = nullptr;
    ~Result() { nemo_speech_asr_result_destroy(value); }
};

class Emitter {
  public:
    Emitter(std::string session_id, Clock::time_point started)
        : session_id_(std::move(session_id)), started_(started) {}

    void emit(const std::string& event, double audio_position, const std::string& extra = {}) {
        const double elapsed = std::chrono::duration<double>(Clock::now() - started_).count();
        std::printf(
            "{\"schema_version\":1,\"session_id\":%s,\"sequence\":%zu,"
            "\"event\":%s,\"emitted_monotonic_seconds\":%.6f,"
            "\"audio_position_seconds\":%.6f%s%s}\n",
            json_string(session_id_).c_str(), sequence_++, json_string(event).c_str(), elapsed,
            std::max(0.0, audio_position), extra.empty() ? "" : ",", extra.c_str());
        std::fflush(stdout);
    }

    size_t sequence() const { return sequence_; }

  private:
    std::string session_id_;
    Clock::time_point started_;
    size_t sequence_ = 1;
};

std::string words_json(const nemo_speech_asr_result* result, double offset_seconds) {
    const size_t count = nemo_speech_asr_result_word_count(result, 0);
    std::string output = "[";
    for (size_t i = 0; i < count; ++i) {
        if (i) output += ",";
        const char* raw = nemo_speech_asr_result_word_text(result, 0, i);
        const double start = offset_seconds +
            static_cast<double>(nemo_speech_asr_result_word_start_time(result, 0, i)) / 1000.0;
        const double end = offset_seconds +
            static_cast<double>(nemo_speech_asr_result_word_end_time(result, 0, i)) / 1000.0;
        const float confidence = nemo_speech_asr_result_word_confidence(result, 0, i);
        char times[160];
        std::snprintf(
            times, sizeof(times), "\"start_seconds\":%.6f,\"end_seconds\":%.6f",
            std::max(0.0, start), std::max(start, end));
        output += "{\"word\":" + json_string(raw ? raw : "") + "," + times;
        if (std::isfinite(confidence)) {
            char score[64];
            std::snprintf(score, sizeof(score), ",\"confidence\":%.6f", confidence);
            output += score;
        }
        output += "}";
    }
    output += "]";
    return output;
}

std::string nullable_seconds(float value) {
    if (!std::isfinite(value)) return "null";
    char output[64];
    std::snprintf(output, sizeof(output), "%.6f", static_cast<double>(value));
    return output;
}

std::string endpoint_diagnostics_json(
    const nemo_speech_asr_result* result, double event_delivery_position) {
    const bool automatic = nemo_speech_asr_result_endpoint_triggered(result);
    const float decode_clock = nemo_speech_asr_result_endpoint_decode_clock(result);
    const float last_token = nemo_speech_asr_result_endpoint_last_token(result);
    const float logical_crossing = nemo_speech_asr_result_endpoint_threshold_crossing(result);
    const float raw_frontier = nemo_speech_asr_result_audio_processed(result);
    const double delivery_lag =
        automatic && std::isfinite(logical_crossing)
            ? event_delivery_position - static_cast<double>(logical_crossing)
            : NAN;
    return
        "\"endpoint_diagnostics\":{\"schema_version\":1,\"automatic_endpoint\":" +
        std::string(automatic ? "true" : "false") +
        ",\"decoder_clock_seconds\":" + nullable_seconds(decode_clock) +
        ",\"last_token_seconds\":" + nullable_seconds(last_token) +
        ",\"logical_threshold_crossing_seconds\":" + nullable_seconds(logical_crossing) +
        ",\"raw_delivery_frontier_seconds\":" + nullable_seconds(raw_frontier) +
        ",\"event_delivery_position_seconds\":" +
        nullable_seconds(static_cast<float>(event_delivery_position)) +
        ",\"delivery_lag_seconds\":" + nullable_seconds(static_cast<float>(delivery_lag)) + "}";
}

bool create_recognizer(
    const std::string& path, bool endpointing, Recognizer& recognizer, std::string& error) {
    nemo_speech_asr_backend_config backend = {};
    backend.size = sizeof(backend);
    backend.gpu = -1;
    nemo_speech_asr_model_config model = {};
    model.size = sizeof(model);
    model.path = path.c_str();
    nemo_speech_asr_endpointing_config endpoint = {};
    endpoint.size = sizeof(endpoint);
    endpoint.enable = endpointing;
    endpoint.vad_based = false;
    endpoint.stop_history_eou_ms = kEndpointMs;
    nemo_speech_asr_streaming_config streaming = {};
    streaming.size = sizeof(streaming);
    streaming.chunk_size = 0.16f;
    streaming.ctc_left_padding = 1.92f;
    streaming.ctc_right_padding = 1.92f;
    streaming.rnnt_right_context = -1;
    nemo_speech_asr_recognizer_config config = {};
    config.size = sizeof(config);
    config.backend = &backend;
    config.model = &model;
    config.streaming = &streaming;
    if (endpointing) config.endpointing = &endpoint;
    const auto status = nemo_speech_asr_create(&config, &recognizer.value);
    if (status != NEMO_SPEECH_ASR_OK) {
        const char* detail = nemo_speech_asr_last_error();
        error = detail ? detail : "unknown model-load failure";
        return false;
    }
    return true;
}

struct Cascade {
    Emitter& emitter;
    Recognizer& parakeet;
    const std::vector<float>& audio;
    size_t segment_start = 0;
    size_t segment_number = 1;
    int nemotron_revision = 0;
    std::string last_partial;
    std::vector<std::string> authoritative;
    size_t partials = 0;
    size_t parakeet_segments = 0;
    size_t fallbacks = 0;
    size_t silence_segments = 0;
    size_t warnings = 0;
    double pass_two_seconds = 0.0;

    std::string segment_id() const {
        char value[32];
        std::snprintf(value, sizeof(value), "segment-%06zu", segment_number);
        return value;
    }

    void emit_update(
        const std::string& state, const std::string& track, int revision,
        const std::string& text, double start, double end, double delivery_position,
        const std::string& model_alias, const std::string& extra = {},
        const std::string& words = {}) {
        std::string fields =
            "\"segment_id\":" + json_string(segment_id()) +
            ",\"track_id\":" + json_string(track) +
            ",\"revision\":" + std::to_string(revision) +
            ",\"state\":" + json_string(state) +
            ",\"text\":" + json_string(text) +
            ",\"source_time\":{\"start_seconds\":" + std::to_string(start) +
            ",\"end_seconds\":" + std::to_string(end) + "}" +
            ",\"model_alias\":" + (model_alias.empty() ? "null" : json_string(model_alias));
        if (!words.empty()) fields += ",\"words\":" + words;
        if (!extra.empty()) fields += "," + extra;
        emitter.emit("transcript_update", delivery_position, fields);
    }

    void provisional(const std::string& text, double audio_position) {
        const std::string cleaned = trim(text);
        if (cleaned.empty() || cleaned == last_partial) return;
        last_partial = cleaned;
        ++nemotron_revision;
        ++partials;
        emit_update(
            "provisional", "nemotron", nemotron_revision, cleaned,
            static_cast<double>(segment_start) / kSampleRate, audio_position, audio_position,
            kNemotronAlias);
    }

    void warning(const std::string& code, const std::string& message, double position) {
        ++warnings;
        emitter.emit(
            "session_warning", position,
            "\"code\":" + json_string(code) + ",\"message\":" + json_string(message) +
                ",\"segment_id\":" + json_string(segment_id()) +
                ",\"model_alias\":" + json_string(kParakeetAlias));
    }

    bool finalize(
        nemo_speech_asr_result* nemotron_result, size_t delivered_samples, std::string& error) {
        const bool automatic = nemo_speech_asr_result_endpoint_triggered(nemotron_result);
        const double logical_crossing = static_cast<double>(
            nemo_speech_asr_result_endpoint_threshold_crossing(nemotron_result));
        const auto resolved = native_asr::resolve_cascade_boundary(
            automatic, logical_crossing, segment_start, delivered_samples, audio.size(),
            kSampleRate);
        if (resolved.error != native_asr::BoundaryError::none) {
            error = native_asr::boundary_error_message(resolved.error);
            return false;
        }
        const size_t boundary = resolved.sample;
        if (boundary == segment_start && !automatic && boundary == audio.size()) return true;
        if (boundary <= segment_start) {
            error = "EOF boundary does not complete the remaining source audio";
            return false;
        }
        const double start = static_cast<double>(segment_start) / kSampleRate;
        const double end = static_cast<double>(boundary) / kSampleRate;
        const double delivery_position = static_cast<double>(delivered_samples) / kSampleRate;
        const char* raw_nemotron = nemo_speech_asr_result_transcript(nemotron_result, 0);
        const std::string nemotron = trim(raw_nemotron ? raw_nemotron : "");
        const std::string endpoint_diagnostics =
            endpoint_diagnostics_json(nemotron_result, delivery_position);
        ++nemotron_revision;
        emit_update(
            "segment_final", "nemotron", nemotron_revision, nemotron, start, end,
            delivery_position, kNemotronAlias, endpoint_diagnostics,
            words_json(nemotron_result, 0.0));

        nemo_speech_asr_recognition_options options = nemo_speech_asr_recognition_options_default();
        options.language_code = "en";
        options.enable_word_time_offsets = true;
        Result pass_two;
        const auto pass_two_started = Clock::now();
        const auto status = nemo_speech_asr_recognize_f32(
            parakeet.value, &options, audio.data() + segment_start, boundary - segment_start,
            kSampleRate, &pass_two.value);
        pass_two_seconds += std::chrono::duration<double>(Clock::now() - pass_two_started).count();

        std::string selected;
        std::string selection;
        std::string model_alias;
        std::string selected_words;
        if (status == NEMO_SPEECH_ASR_OK && pass_two.value) {
            const char* raw = nemo_speech_asr_result_transcript(pass_two.value, 0);
            selected = trim(raw ? raw : "");
            if (!selected.empty()) {
                selection = "parakeet";
                model_alias = kParakeetAlias;
                selected_words = words_json(pass_two.value, start);
                ++parakeet_segments;
            } else {
                warning(
                    "pass_two_empty",
                    "Parakeet returned an empty segment; selecting Nemotron when nonempty",
                    delivery_position);
            }
        } else {
            const char* detail = nemo_speech_asr_last_error();
            warning(
                "pass_two_error",
                std::string("Parakeet segment inference failed; selecting Nemotron when nonempty: ") +
                    (detail ? detail : "unknown error"),
                delivery_position);
        }
        if (selection.empty() && !nemotron.empty()) {
            selected = nemotron;
            selection = "nemotron_fallback";
            model_alias = kNemotronAlias;
            selected_words = words_json(nemotron_result, 0.0);
            ++fallbacks;
        } else if (selection.empty()) {
            selected.clear();
            selection = "silence";
            model_alias.clear();
            ++silence_segments;
        }

        std::string extra =
            "\"selection\":" + json_string(selection) +
            ",\"supersedes\":{\"track_id\":\"nemotron\",\"revision\":" +
            std::to_string(nemotron_revision) + "}," + endpoint_diagnostics;
        emit_update(
            "cascade_final", "authoritative", 1, selected, start, end, delivery_position,
            model_alias, extra, selected_words);
        if (!selected.empty()) authoritative.push_back(selected);

        segment_start = boundary;
        ++segment_number;
        nemotron_revision = 0;
        last_partial.clear();
        return true;
    }

    std::string transcript() const {
        std::string text;
        for (const auto& segment : authoritative) {
            if (!text.empty()) text += " ";
            text += segment;
        }
        return text;
    }
};

void usage(const char* program) {
    std::fprintf(
        stderr,
        "Usage: %s --session-id ID --stream-model MODEL --final-model MODEL [--pace] AUDIO\n",
        program);
}

}  // namespace

int main(int argc, char** argv) {
    std::string session_id, stream_model, final_model, audio_path;
    bool pace = false;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        if (argument == "--session-id" && i + 1 < argc) session_id = argv[++i];
        else if (argument == "--stream-model" && i + 1 < argc) stream_model = argv[++i];
        else if (argument == "--final-model" && i + 1 < argc) final_model = argv[++i];
        else if (argument == "--pace") pace = true;
        else if (!argument.empty() && argument[0] == '-') {
            usage(argv[0]);
            return 2;
        } else if (audio_path.empty()) audio_path = argument;
        else {
            usage(argv[0]);
            return 2;
        }
    }
    if (session_id.empty() || stream_model.empty() || final_model.empty() || audio_path.empty()) {
        usage(argv[0]);
        return 2;
    }

    const auto started = Clock::now();
    Emitter emitter(session_id, started);
    emitter.emit(
        "session_started", 0.0,
        std::string("\"models\":[") + json_string(kNemotronAlias) + "," +
            json_string(kParakeetAlias) +
            "],\"chunk_milliseconds\":20,\"endpointing\":{\"kind\":"
            "\"token_silence\",\"silence_milliseconds\":1200},\"paced\":" +
            (pace ? "true" : "false"));

    std::vector<float> audio;
    std::string error;
    if (!read_pcm16_wav(audio_path, audio, error) || audio.empty()) {
        emitter.emit(
            "session_error", 0.0,
            "\"stage\":\"audio\",\"message\":" + json_string(error.empty() ? "empty audio" : error));
        return 1;
    }

    Recognizer nemotron, parakeet;
    const auto nemotron_load_started = Clock::now();
    if (!create_recognizer(stream_model, true, nemotron, error)) {
        emitter.emit(
            "session_error", 0.0,
            "\"stage\":\"pass_one_load\",\"message\":" + json_string(error));
        return 1;
    }
    const double nemotron_load_seconds =
        std::chrono::duration<double>(Clock::now() - nemotron_load_started).count();
    const auto parakeet_load_started = Clock::now();
    if (!create_recognizer(final_model, false, parakeet, error)) {
        emitter.emit(
            "session_error", 0.0,
            "\"stage\":\"pass_two_load\",\"message\":" + json_string(error));
        return 1;
    }
    const double parakeet_load_seconds =
        std::chrono::duration<double>(Clock::now() - parakeet_load_started).count();

    nemo_speech_asr_recognition_options options = nemo_speech_asr_recognition_options_default();
    options.language_code = "en";
    options.interim_results = true;
    options.enable_word_time_offsets = true;
    options.stop_history_eou_ms = kEndpointMs;
    Stream stream;
    if (nemo_speech_asr_streaming_recognize(nemotron.value, &options, &stream.value) !=
        NEMO_SPEECH_ASR_OK) {
        const char* detail = nemo_speech_asr_last_error();
        emitter.emit(
            "session_error", 0.0,
            "\"stage\":\"pass_one_stream\",\"message\":" +
                json_string(detail ? detail : "could not start stream"));
        return 1;
    }

    Cascade cascade{emitter, parakeet, audio};
    size_t pushed = 0;
    const auto inference_started = Clock::now();
    while (pushed < audio.size()) {
        const size_t count = std::min(kChunkSamples, audio.size() - pushed);
        if (nemo_speech_asr_stream_push_f32(
                stream.value, audio.data() + pushed, count, kSampleRate) != NEMO_SPEECH_ASR_OK) {
            const char* detail = nemo_speech_asr_last_error();
            emitter.emit(
                "session_error", static_cast<double>(pushed) / kSampleRate,
                "\"stage\":\"pass_one\",\"message\":" +
                    json_string(detail ? detail : "stream push failed"));
            return 1;
        }
        pushed += count;
        while (true) {
            Result result;
            const auto status = nemo_speech_asr_stream_next(stream.value, &result.value);
            if (status != NEMO_SPEECH_ASR_OK) {
                const char* detail = nemo_speech_asr_last_error();
                emitter.emit(
                    "session_error", static_cast<double>(pushed) / kSampleRate,
                    "\"stage\":\"pass_one\",\"message\":" +
                        json_string(detail ? detail : "stream decode failed"));
                return 1;
            }
            if (!result.value) break;
            const char* raw = nemo_speech_asr_result_transcript(result.value, 0);
            if (nemo_speech_asr_result_is_final(result.value)) {
                if (!cascade.finalize(result.value, pushed, error)) {
                    emitter.emit(
                        "session_error", static_cast<double>(pushed) / kSampleRate,
                        "\"stage\":\"boundary_attribution\",\"message\":" +
                            json_string(error));
                    return 1;
                }
            } else {
                cascade.provisional(
                    raw ? raw : "", nemo_speech_asr_result_audio_processed(result.value));
            }
        }
        if (pace && pushed < audio.size()) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    if (nemo_speech_asr_stream_finish(stream.value) != NEMO_SPEECH_ASR_OK) {
        const char* detail = nemo_speech_asr_last_error();
        emitter.emit(
            "session_error", static_cast<double>(pushed) / kSampleRate,
            "\"stage\":\"pass_one_finish\",\"message\":" +
                json_string(detail ? detail : "stream finish failed"));
        return 1;
    }
    while (true) {
        Result result;
        const auto status = nemo_speech_asr_stream_next(stream.value, &result.value);
        if (status != NEMO_SPEECH_ASR_OK) {
            const char* detail = nemo_speech_asr_last_error();
            emitter.emit(
                "session_error", static_cast<double>(pushed) / kSampleRate,
                "\"stage\":\"pass_one_finish\",\"message\":" +
                    json_string(detail ? detail : "final result failed"));
            return 1;
        }
        if (!result.value) break;
        if (nemo_speech_asr_result_is_final(result.value) &&
            !cascade.finalize(result.value, pushed, error)) {
            emitter.emit(
                "session_error", static_cast<double>(pushed) / kSampleRate,
                "\"stage\":\"boundary_attribution\",\"message\":" + json_string(error));
            return 1;
        }
    }

    const double inference_seconds =
        std::chrono::duration<double>(Clock::now() - inference_started).count();
    const double duration = static_cast<double>(audio.size()) / kSampleRate;
    const size_t segment_count = cascade.segment_number - 1;
    emitter.emit(
        "session_metrics", duration,
        "\"model_load_counts\":{" + json_string(kNemotronAlias) + ":1," +
            json_string(kParakeetAlias) + ":1},\"timing\":{\"nemotron_load_seconds\":" +
            std::to_string(nemotron_load_seconds) +
            ",\"parakeet_load_seconds\":" + std::to_string(parakeet_load_seconds) +
            ",\"inference_seconds\":" + std::to_string(inference_seconds) +
            ",\"pass_two_seconds\":" + std::to_string(cascade.pass_two_seconds) +
            "},\"counts\":{\"segments\":" + std::to_string(segment_count) +
            ",\"provisional_updates\":" + std::to_string(cascade.partials) +
            ",\"parakeet_segments\":" + std::to_string(cascade.parakeet_segments) +
            ",\"nemotron_fallbacks\":" + std::to_string(cascade.fallbacks) +
            ",\"silence_segments\":" + std::to_string(cascade.silence_segments) +
            ",\"warnings\":" + std::to_string(cascade.warnings) + "}");
    emitter.emit(
        "session_completed", duration,
        "\"text\":" + json_string(cascade.transcript()) +
            ",\"segment_count\":" + std::to_string(segment_count));
    return 0;
}
