// SPDX-License-Identifier: MIT
#include "cascade-protocol.h"

#include <nemo_speech/asr.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace protocol = native_asr::cascade_protocol;

namespace {

std::string trim(std::string value) {
    const auto not_space = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::string last_error(const char* operation) {
    const char* detail = nemo_speech_asr_last_error();
    return std::string(operation) + ": " + (detail ? detail : "unknown NeMo-Speech.cpp error");
}

struct Recognizer {
    nemo_speech_asr_recognizer* handle = nullptr;

    ~Recognizer() {
        if (handle)
            nemo_speech_asr_destroy(handle);
    }
};

bool create_recognizer(
    const char* model_path, bool streaming_model, int endpoint_ms, int right_context,
    Recognizer* recognizer, std::string* error) {
    nemo_speech_asr_backend_config backend = {};
    backend.size = sizeof(backend);
    backend.gpu = -1;

    nemo_speech_asr_model_config model = {};
    model.size = sizeof(model);
    model.path = model_path;

    nemo_speech_asr_streaming_config streaming = {};
    streaming.size = sizeof(streaming);
    streaming.chunk_size = 0.16F;
    streaming.ctc_left_padding = 1.92F;
    streaming.ctc_right_padding = 1.92F;
    streaming.rnnt_right_context = right_context;

    nemo_speech_asr_endpointing_config endpointing = {};
    endpointing.size = sizeof(endpointing);
    endpointing.enable = streaming_model;
    endpointing.vad_based = false;
    endpointing.stop_history_eou_ms = endpoint_ms;

    nemo_speech_asr_recognizer_config config = {};
    config.size = sizeof(config);
    config.backend = &backend;
    config.model = &model;
    config.streaming = &streaming;
    config.endpointing = streaming_model ? &endpointing : nullptr;

    if (nemo_speech_asr_create(&config, &recognizer->handle) != NEMO_SPEECH_ASR_OK) {
        *error = last_error("nemo_speech_asr_create");
        return false;
    }
    return true;
}

std::string result_text(const nemo_speech_asr_result* result) {
    if (!result || nemo_speech_asr_result_alternative_count(result) == 0)
        return {};
    const char* text = nemo_speech_asr_result_transcript(result, 0);
    return trim(text ? text : "");
}

bool emit_stream_results(nemo_speech_asr_stream* stream, std::string* error) {
    while (true) {
        nemo_speech_asr_result* result = nullptr;
        const auto status = nemo_speech_asr_stream_next(stream, &result);
        if (status != NEMO_SPEECH_ASR_OK) {
            *error = last_error("nemo_speech_asr_stream_next");
            return false;
        }
        if (!result)
            return true;
        const auto audio_ms = static_cast<std::uint64_t>(
            std::max(0.0F, nemo_speech_asr_result_audio_processed(result)) * 1000.0F + 0.5F);
        const auto kind = nemo_speech_asr_result_is_final(result) ? protocol::Message::final
                                                                 : protocol::Message::partial;
        const std::string text = result_text(result);
        const bool ok = protocol::write_packet(STDOUT_FILENO, kind, 0, audio_ms, text);
        nemo_speech_asr_result_destroy(result);
        if (!ok) {
            *error = "failed to write streaming result";
            return false;
        }
    }
}

int stream_worker(const char* model, int endpoint_ms, int right_context) {
    Recognizer recognizer;
    std::string error;
    if (!create_recognizer(model, true, endpoint_ms, right_context, &recognizer, &error)) {
        protocol::write_packet(STDOUT_FILENO, protocol::Message::error, 0, 0, error);
        return 2;
    }

    auto options = nemo_speech_asr_recognition_options_default();
    options.language_code = "en";
    options.interim_results = true;
    options.enable_word_time_offsets = true;
    options.stop_history_eou_ms = endpoint_ms;

    nemo_speech_asr_stream* stream = nullptr;
    if (nemo_speech_asr_streaming_recognize(recognizer.handle, &options, &stream) !=
        NEMO_SPEECH_ASR_OK) {
        error = last_error("nemo_speech_asr_streaming_recognize");
        protocol::write_packet(STDOUT_FILENO, protocol::Message::error, 0, 0, error);
        return 2;
    }

    std::fprintf(stderr, "cascade-worker: Nemotron model loaded (%s)\n", nemo_speech_asr_version());
    if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::ready, 0, 0, "nemotron")) {
        nemo_speech_asr_stream_close(stream);
        return 3;
    }

    std::vector<float> samples;
    protocol::Command command;
    std::uint32_t id = 0;
    while (protocol::read_command(STDIN_FILENO, &command, &id, &samples)) {
        if (command == protocol::Command::quit)
            break;
        if (command == protocol::Command::audio) {
            if (nemo_speech_asr_stream_push_f32(
                    stream, samples.data(), samples.size(), 16000) != NEMO_SPEECH_ASR_OK) {
                error = last_error("nemo_speech_asr_stream_push_f32");
                protocol::write_packet(STDOUT_FILENO, protocol::Message::error, id, 0, error);
                nemo_speech_asr_stream_close(stream);
                return 3;
            }
            if (!emit_stream_results(stream, &error)) {
                protocol::write_packet(STDOUT_FILENO, protocol::Message::error, id, 0, error);
                nemo_speech_asr_stream_close(stream);
                return 3;
            }
            if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::ack, id, 0)) {
                nemo_speech_asr_stream_close(stream);
                return 3;
            }
            continue;
        }
        if (command == protocol::Command::finish) {
            if (nemo_speech_asr_stream_finish(stream) != NEMO_SPEECH_ASR_OK) {
                error = last_error("nemo_speech_asr_stream_finish");
                protocol::write_packet(STDOUT_FILENO, protocol::Message::error, id, 0, error);
                nemo_speech_asr_stream_close(stream);
                return 3;
            }
            if (!emit_stream_results(stream, &error)) {
                protocol::write_packet(STDOUT_FILENO, protocol::Message::error, id, 0, error);
                nemo_speech_asr_stream_close(stream);
                return 3;
            }
            protocol::write_packet(STDOUT_FILENO, protocol::Message::ack, id, 0);
            nemo_speech_asr_stream_close(stream);
            return 0;
        }
        protocol::write_packet(
            STDOUT_FILENO, protocol::Message::error, id, 0,
            "stream worker received an invalid command");
        nemo_speech_asr_stream_close(stream);
        return 3;
    }
    nemo_speech_asr_stream_close(stream);
    return 0;
}

int offline_worker(const char* model) {
    Recognizer recognizer;
    std::string error;
    if (!create_recognizer(model, false, 0, -1, &recognizer, &error)) {
        protocol::write_packet(STDOUT_FILENO, protocol::Message::error, 0, 0, error);
        return 2;
    }
    std::fprintf(stderr, "cascade-worker: Parakeet model loaded (%s)\n", nemo_speech_asr_version());
    if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::ready, 0, 0, "parakeet"))
        return 3;

    auto options = nemo_speech_asr_recognition_options_default();
    options.language_code = "en";
    options.enable_word_time_offsets = true;

    std::vector<float> samples;
    protocol::Command command;
    std::uint32_t id = 0;
    while (protocol::read_command(STDIN_FILENO, &command, &id, &samples)) {
        if (command == protocol::Command::quit)
            return 0;
        if (command != protocol::Command::job) {
            protocol::write_packet(
                STDOUT_FILENO, protocol::Message::error, id, 0,
                "offline worker received an invalid command");
            return 3;
        }
        nemo_speech_asr_result* result = nullptr;
        if (nemo_speech_asr_recognize_f32(
                recognizer.handle, &options, samples.data(), samples.size(), 16000, &result) !=
            NEMO_SPEECH_ASR_OK) {
            error = last_error("nemo_speech_asr_recognize_f32");
            protocol::write_packet(STDOUT_FILENO, protocol::Message::error, id, 0, error);
            return 3;
        }
        const std::string text = result_text(result);
        nemo_speech_asr_result_destroy(result);
        if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::result, id, 0, text))
            return 3;
    }
    return 0;
}

void usage(const char* argv0) {
    std::fprintf(
        stderr,
        "Usage: %s stream MODEL --endpoint-ms N --right-context N\n"
        "       %s offline MODEL\n",
        argv0, argv0);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        usage(argv[0]);
        return 2;
    }
    const std::string mode = argv[1];
    const char* model = argv[2];
    if (mode == "offline")
        return offline_worker(model);
    if (mode != "stream") {
        usage(argv[0]);
        return 2;
    }
    int endpoint_ms = 800;
    int right_context = 1;
    for (int i = 3; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--endpoint-ms" && i + 1 < argc)
            endpoint_ms = std::atoi(argv[++i]);
        else if (arg == "--right-context" && i + 1 < argc)
            right_context = std::atoi(argv[++i]);
        else {
            usage(argv[0]);
            return 2;
        }
    }
    if (endpoint_ms <= 0 || right_context < 0) {
        usage(argv[0]);
        return 2;
    }
    return stream_worker(model, endpoint_ms, right_context);
}
