// SPDX-License-Identifier: MIT
#ifndef NATIVE_ASR_CASCADE_PROTOCOL_H
#define NATIVE_ASR_CASCADE_PROTOCOL_H

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <string>
#include <unistd.h>
#include <vector>

namespace native_asr::cascade_protocol {

enum class Command : std::uint8_t {
    audio = 1,
    finish = 2,
    job = 3,
    quit = 4,
};

enum class Message : std::uint8_t {
    ready = 1,
    partial = 2,
    final = 3,
    result = 4,
    ack = 5,
    error = 6,
};

struct Packet {
    Message kind = Message::error;
    std::uint32_t id = 0;
    std::uint64_t audio_ms = 0;
    std::uint64_t speech_start_ms = 0;
    std::uint64_t speech_end_ms = 0;
    std::string text;
};

inline bool write_all(int fd, const void* data, std::size_t size) {
    const auto* bytes = static_cast<const std::uint8_t*>(data);
    while (size > 0) {
        const ssize_t written = ::write(fd, bytes, size);
        if (written > 0) {
            bytes += written;
            size -= static_cast<std::size_t>(written);
            continue;
        }
        if (written < 0 && errno == EINTR)
            continue;
        return false;
    }
    return true;
}

inline bool read_all(int fd, void* data, std::size_t size) {
    auto* bytes = static_cast<std::uint8_t*>(data);
    while (size > 0) {
        const ssize_t count = ::read(fd, bytes, size);
        if (count > 0) {
            bytes += count;
            size -= static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR)
            continue;
        return false;
    }
    return true;
}

template <typename T>
inline bool write_scalar(int fd, const T& value) {
    return write_all(fd, &value, sizeof(value));
}

template <typename T>
inline bool read_scalar(int fd, T* value) {
    return read_all(fd, value, sizeof(*value));
}

inline bool write_command(
    int fd, Command command, std::uint32_t id, const float* samples, std::size_t count) {
    const auto kind = static_cast<std::uint8_t>(command);
    const auto sample_count = static_cast<std::uint64_t>(count);
    return write_scalar(fd, kind) && write_scalar(fd, id) && write_scalar(fd, sample_count) &&
           (count == 0 || write_all(fd, samples, count * sizeof(float)));
}

inline bool read_command(
    int fd, Command* command, std::uint32_t* id, std::vector<float>* samples) {
    std::uint8_t kind = 0;
    std::uint64_t count = 0;
    if (!read_scalar(fd, &kind) || !read_scalar(fd, id) || !read_scalar(fd, &count))
        return false;
    if (kind < static_cast<std::uint8_t>(Command::audio) ||
        kind > static_cast<std::uint8_t>(Command::quit) || count > 16000ULL * 60ULL * 60ULL)
        return false;
    *command = static_cast<Command>(kind);
    samples->resize(static_cast<std::size_t>(count));
    return count == 0 || read_all(fd, samples->data(), samples->size() * sizeof(float));
}

inline bool write_packet(
    int fd, Message message, std::uint32_t id, std::uint64_t audio_ms,
    const std::string& text = {}, std::uint64_t speech_start_ms = 0,
    std::uint64_t speech_end_ms = 0) {
    const auto kind = static_cast<std::uint8_t>(message);
    const auto length = static_cast<std::uint32_t>(text.size());
    return write_scalar(fd, kind) && write_scalar(fd, id) && write_scalar(fd, audio_ms) &&
           write_scalar(fd, speech_start_ms) && write_scalar(fd, speech_end_ms) &&
           write_scalar(fd, length) && (text.empty() || write_all(fd, text.data(), text.size()));
}

inline bool parse_packet(std::vector<std::uint8_t>* buffer, Packet* packet) {
    constexpr std::size_t header = sizeof(std::uint8_t) + sizeof(std::uint32_t) +
                                   sizeof(std::uint64_t) * 3 + sizeof(std::uint32_t);
    if (buffer->size() < header)
        return false;
    std::size_t offset = 0;
    std::uint8_t kind = (*buffer)[offset];
    offset += sizeof(kind);
    std::memcpy(&packet->id, buffer->data() + offset, sizeof(packet->id));
    offset += sizeof(packet->id);
    std::memcpy(&packet->audio_ms, buffer->data() + offset, sizeof(packet->audio_ms));
    offset += sizeof(packet->audio_ms);
    std::memcpy(
        &packet->speech_start_ms, buffer->data() + offset, sizeof(packet->speech_start_ms));
    offset += sizeof(packet->speech_start_ms);
    std::memcpy(&packet->speech_end_ms, buffer->data() + offset, sizeof(packet->speech_end_ms));
    offset += sizeof(packet->speech_end_ms);
    std::uint32_t length = 0;
    std::memcpy(&length, buffer->data() + offset, sizeof(length));
    offset += sizeof(length);
    if (length > 16U * 1024U * 1024U)
        return false;
    if (buffer->size() < header + length)
        return false;
    packet->kind = static_cast<Message>(kind);
    packet->text.assign(
        reinterpret_cast<const char*>(buffer->data() + offset), static_cast<std::size_t>(length));
    buffer->erase(buffer->begin(), buffer->begin() + static_cast<std::ptrdiff_t>(header + length));
    return true;
}

}  // namespace native_asr::cascade_protocol

#endif
