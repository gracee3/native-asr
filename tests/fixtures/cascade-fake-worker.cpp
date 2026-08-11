// SPDX-License-Identifier: MIT
#include "cascade-protocol.h"

#include <chrono>
#include <cstdlib>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

namespace protocol = native_asr::cascade_protocol;

namespace {

int environment_int(const char* name, int fallback) {
    const char* value = std::getenv(name);
    return value && *value ? std::atoi(value) : fallback;
}

std::string scenario() {
    const char* value = std::getenv("CASCADE_FAKE_SCENARIO");
    return value ? value : "success";
}

int stream_worker() {
    if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::ready, 0, 0, "nemotron"))
        return 3;
    const int final_every = environment_int("CASCADE_FAKE_FINAL_EVERY", 2);
    std::uint64_t audio_ms = 0;
    int chunks = 0;
    int segment = 0;
    std::vector<float> samples;
    protocol::Command command;
    std::uint32_t id = 0;
    while (protocol::read_command(STDIN_FILENO, &command, &id, &samples)) {
        if (command == protocol::Command::quit)
            return 0;
        if (command == protocol::Command::audio) {
            audio_ms += samples.size() * 1000ULL / 16000ULL;
            ++chunks;
            const std::string prefix = "nemotron segment " + std::to_string(segment + 1);
            protocol::write_packet(
                STDOUT_FILENO, protocol::Message::partial, 0, audio_ms,
                prefix + " partial " + std::to_string(chunks));
            if (chunks >= final_every) {
                protocol::write_packet(STDOUT_FILENO, protocol::Message::final, 0, audio_ms, prefix);
                chunks = 0;
                ++segment;
            }
            protocol::write_packet(STDOUT_FILENO, protocol::Message::ack, id, 0);
            continue;
        }
        if (command == protocol::Command::finish) {
            if (chunks > 0) {
                protocol::write_packet(
                    STDOUT_FILENO, protocol::Message::final, 0, audio_ms,
                    "nemotron segment " + std::to_string(segment + 1));
            }
            protocol::write_packet(STDOUT_FILENO, protocol::Message::ack, id, 0);
            return 0;
        }
        return 3;
    }
    return 0;
}

int offline_worker() {
    if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::ready, 0, 0, "parakeet"))
        return 3;
    std::vector<float> samples;
    protocol::Command command;
    std::uint32_t id = 0;
    while (protocol::read_command(STDIN_FILENO, &command, &id, &samples)) {
        if (command == protocol::Command::quit)
            return 0;
        if (command != protocol::Command::job)
            return 3;
        const int delay = environment_int("CASCADE_FAKE_DELAY_MS", 0);
        if (delay > 0)
            std::this_thread::sleep_for(std::chrono::milliseconds(delay));
        const std::string mode = scenario();
        if (mode == "failure") {
            protocol::write_packet(
                STDOUT_FILENO, protocol::Message::error, id, 0, "injected Parakeet failure");
            return 3;
        }
        const std::string text = mode == "empty" ? "" : "parakeet segment " + std::to_string(id + 1);
        if (!protocol::write_packet(STDOUT_FILENO, protocol::Message::result, id, 0, text))
            return 3;
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2)
        return 2;
    const std::string mode = argv[1];
    if (mode == "stream")
        return stream_worker();
    if (mode == "offline")
        return offline_worker();
    return 2;
}
