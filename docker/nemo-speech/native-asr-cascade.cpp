// SPDX-License-Identifier: MIT
#include "cascade-protocol.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <poll.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <utility>
#include <vector>

#ifndef RENAME_NOREPLACE
#define RENAME_NOREPLACE (1 << 0)
#endif

namespace fs = std::filesystem;
namespace protocol = native_asr::cascade_protocol;
using Clock = std::chrono::steady_clock;

namespace {

constexpr int kSampleRate = 16000;
constexpr std::size_t kChunkSamples = 2560;  // 160 ms

std::atomic<bool> g_cancelled{false};

void on_signal(int) {
    g_cancelled.store(true);
}

class Cancelled : public std::runtime_error {
  public:
    Cancelled() : std::runtime_error("session cancelled") {}
};

std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char c : value) {
        switch (c) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(c) << std::dec;
                } else {
                    out << static_cast<char>(c);
                }
        }
    }
    return out.str();
}

std::string trim(std::string value) {
    const auto not_space = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::uint64_t duration_ms(Clock::duration duration) {
    return static_cast<std::uint64_t>(
        std::max<std::int64_t>(0, std::chrono::duration_cast<std::chrono::milliseconds>(duration).count()));
}

void write_private_file(const fs::path& path, const std::string& content) {
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0)
        throw std::runtime_error("cannot create audit file " + path.string() + ": " + std::strerror(errno));
    bool ok = protocol::write_all(fd, content.data(), content.size());
    if (ok)
        ok = ::fsync(fd) == 0;
    const int saved = errno;
    ::close(fd);
    if (!ok)
        throw std::runtime_error("cannot write audit file " + path.string() + ": " + std::strerror(saved));
}

class Audit {
  public:
    explicit Audit(const std::string& destination) {
        if (destination.empty())
            return;
        target_ = fs::absolute(destination).lexically_normal();
        const fs::path parent = target_.parent_path();
        if (!fs::is_directory(parent))
            throw std::runtime_error("audit parent is not a directory: " + parent.string());
        struct stat st {};
        if (::lstat(target_.c_str(), &st) == 0 || errno != ENOENT)
            throw std::runtime_error("audit destination already exists: " + target_.string());

        const auto stamp = std::chrono::duration_cast<std::chrono::nanoseconds>(
                               Clock::now().time_since_epoch())
                               .count();
        stage_ = parent /
                 ("." + target_.filename().string() + ".tmp." + std::to_string(::getpid()) + "." +
                  std::to_string(stamp));
        if (::mkdir(stage_.c_str(), 0700) != 0)
            throw std::runtime_error("cannot create audit staging directory: " + std::string(std::strerror(errno)));
        events_fd_ = ::open(
            (stage_ / "events.jsonl").c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (events_fd_ < 0) {
            fs::remove_all(stage_);
            throw std::runtime_error("cannot create audit event log: " + std::string(std::strerror(errno)));
        }
        enabled_ = true;
    }

    Audit(const Audit&) = delete;
    Audit& operator=(const Audit&) = delete;

    ~Audit() {
        if (events_fd_ >= 0)
            ::close(events_fd_);
        if (enabled_ && !published_) {
            std::error_code ignored;
            fs::remove_all(stage_, ignored);
        }
    }

    void event(const std::string& line) {
        if (!enabled_)
            return;
        const std::string record = line + "\n";
        if (!protocol::write_all(events_fd_, record.data(), record.size()))
            throw std::runtime_error("cannot write audit event log: " + std::string(std::strerror(errno)));
    }

    void publish(const std::string& result, const std::string& transcript) {
        if (!enabled_)
            return;
        if (::fsync(events_fd_) != 0)
            throw std::runtime_error("cannot fsync audit event log: " + std::string(std::strerror(errno)));
        if (::close(events_fd_) != 0) {
            events_fd_ = -1;
            throw std::runtime_error("cannot close audit event log: " + std::string(std::strerror(errno)));
        }
        events_fd_ = -1;
        write_private_file(stage_ / "result.json", result + "\n");
        write_private_file(stage_ / "transcript.txt", transcript);

        const int stage_fd = ::open(stage_.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (stage_fd < 0 || ::fsync(stage_fd) != 0) {
            const int saved = errno;
            if (stage_fd >= 0)
                ::close(stage_fd);
            throw std::runtime_error("cannot fsync audit directory: " + std::string(std::strerror(saved)));
        }
        ::close(stage_fd);

        const fs::path parent = target_.parent_path();
        if (::syscall(
                SYS_renameat2, AT_FDCWD, stage_.c_str(), AT_FDCWD, target_.c_str(),
                RENAME_NOREPLACE) != 0)
            throw std::runtime_error("cannot publish audit directory: " + std::string(std::strerror(errno)));
        published_ = true;

        const int parent_fd = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (parent_fd >= 0) {
            ::fsync(parent_fd);
            ::close(parent_fd);
        }
    }

  private:
    bool enabled_ = false;
    bool published_ = false;
    int events_fd_ = -1;
    fs::path target_;
    fs::path stage_;
};

class Worker {
  public:
    Worker() = default;
    Worker(const Worker&) = delete;
    Worker& operator=(const Worker&) = delete;
    ~Worker() { stop(true); }

    void configure(std::string binary, std::vector<std::string> arguments, std::string label) {
        binary_ = std::move(binary);
        arguments_ = std::move(arguments);
        label_ = std::move(label);
    }

    void start() {
        stop(true);
        int input_pipe[2] = {-1, -1};
        int output_pipe[2] = {-1, -1};
        if (::pipe2(input_pipe, O_CLOEXEC) != 0 || ::pipe2(output_pipe, O_CLOEXEC) != 0)
            throw std::runtime_error("cannot create " + label_ + " worker pipes");
        const pid_t child = ::fork();
        if (child < 0)
            throw std::runtime_error("cannot fork " + label_ + " worker");
        if (child == 0) {
            ::dup2(input_pipe[0], STDIN_FILENO);
            ::dup2(output_pipe[1], STDOUT_FILENO);
            ::close(input_pipe[0]);
            ::close(input_pipe[1]);
            ::close(output_pipe[0]);
            ::close(output_pipe[1]);
            std::vector<char*> argv;
            argv.push_back(const_cast<char*>(binary_.c_str()));
            for (auto& argument : arguments_)
                argv.push_back(const_cast<char*>(argument.c_str()));
            argv.push_back(nullptr);
            ::execv(binary_.c_str(), argv.data());
            std::fprintf(stderr, "cannot exec %s worker: %s\n", label_.c_str(), std::strerror(errno));
            _exit(127);
        }
        ::close(input_pipe[0]);
        ::close(output_pipe[1]);
        input_fd_ = input_pipe[1];
        output_fd_ = output_pipe[0];
        pid_ = child;
        buffer_.clear();
        const int flags = ::fcntl(output_fd_, F_GETFL, 0);
        ::fcntl(output_fd_, F_SETFL, flags | O_NONBLOCK);
        ++starts_;

        const auto deadline = Clock::now() + std::chrono::seconds(120);
        while (Clock::now() < deadline) {
            std::vector<protocol::Packet> packets;
            collect(&packets);
            for (const auto& packet : packets) {
                if (packet.kind == protocol::Message::ready)
                    return;
                if (packet.kind == protocol::Message::error)
                    throw std::runtime_error(label_ + " worker startup failed: " + packet.text);
            }
            if (!running())
                throw std::runtime_error(label_ + " worker exited during startup");
            struct pollfd fd { output_fd_, POLLIN, 0 };
            ::poll(&fd, 1, 20);
            if (g_cancelled.load())
                throw Cancelled();
        }
        throw std::runtime_error(label_ + " worker startup timed out");
    }

    void stop(bool force) {
        if (pid_ <= 0)
            return;
        if (!force && input_fd_ >= 0)
            protocol::write_command(input_fd_, protocol::Command::quit, 0, nullptr, 0);
        ::close(input_fd_);
        input_fd_ = -1;
        int status = 0;
        if (!force) {
            for (int i = 0; i < 20; ++i) {
                if (::waitpid(pid_, &status, WNOHANG) == pid_) {
                    close_output();
                    pid_ = -1;
                    return;
                }
                ::usleep(10000);
            }
        }
        ::kill(pid_, SIGKILL);
        while (::waitpid(pid_, &status, 0) < 0 && errno == EINTR) {}
        close_output();
        pid_ = -1;
    }

    bool send(protocol::Command command, std::uint32_t id, const std::vector<float>& samples) {
        return input_fd_ >= 0 &&
               protocol::write_command(input_fd_, command, id, samples.data(), samples.size());
    }

    bool send(protocol::Command command, std::uint32_t id, const float* samples, std::size_t count) {
        return input_fd_ >= 0 && protocol::write_command(input_fd_, command, id, samples, count);
    }

    void collect(std::vector<protocol::Packet>* packets) {
        if (output_fd_ < 0)
            return;
        std::array<std::uint8_t, 65536> chunk {};
        while (true) {
            const ssize_t count = ::read(output_fd_, chunk.data(), chunk.size());
            if (count > 0) {
                buffer_.insert(buffer_.end(), chunk.begin(), chunk.begin() + count);
                continue;
            }
            if (count < 0 && errno == EINTR)
                continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
                break;
            break;
        }
        protocol::Packet packet;
        while (protocol::parse_packet(&buffer_, &packet))
            packets->push_back(packet);
    }

    bool running() {
        if (pid_ <= 0)
            return false;
        int status = 0;
        const pid_t result = ::waitpid(pid_, &status, WNOHANG);
        if (result == 0)
            return true;
        if (result == pid_) {
            ::close(input_fd_);
            input_fd_ = -1;
            close_output();
            pid_ = -1;
        }
        return false;
    }

    int output_fd() const { return output_fd_; }
    int starts() const { return starts_; }
    pid_t pid() const { return pid_; }

  private:
    void close_output() {
        if (output_fd_ >= 0)
            ::close(output_fd_);
        output_fd_ = -1;
        buffer_.clear();
    }

    std::string binary_;
    std::vector<std::string> arguments_;
    std::string label_;
    pid_t pid_ = -1;
    int input_fd_ = -1;
    int output_fd_ = -1;
    int starts_ = 0;
    std::vector<std::uint8_t> buffer_;
};

struct Options {
    std::string worker = "/opt/native-asr/bin/native-asr-nemo-worker";
    std::string nemotron = "/models/nemotron-streaming-en/nemotron-speech-streaming-en-0.6b.q8_0.gguf";
    std::string parakeet = "/models/parakeet-tdt-v3/parakeet-tdt-0.6b-v3.q8_0.gguf";
    std::string audit;
    int endpoint_ms = 800;
    int deadline_ms = 2500;
    int right_context = 1;
    bool jsonl = false;
    bool pace = false;
};

struct Segment {
    std::uint32_t id = 0;
    std::uint64_t audio_start_ms = 0;
    std::uint64_t audio_end_ms = 0;
    std::uint32_t next_revision = 0;
    std::uint32_t selected_revision = 0;
    std::string nemotron_text;
    std::string selected_text;
    std::string selected_model = "nemo:nemotron-streaming-en";
    Clock::time_point finalized_at;
    Clock::time_point deadline;
    std::vector<float> audio;
    bool resolved = false;
    bool degraded = false;
    std::string degradation_reason;
};

class Cascade {
  public:
    explicit Cascade(Options options)
        : options_(std::move(options)), audit_(options_.audit) {
        nemotron_.configure(
            options_.worker,
            {"stream", options_.nemotron, "--endpoint-ms", std::to_string(options_.endpoint_ms),
             "--right-context", std::to_string(options_.right_context)},
            "Nemotron");
        parakeet_.configure(options_.worker, {"offline", options_.parakeet}, "Parakeet");
        const auto wall = std::chrono::system_clock::now().time_since_epoch();
        session_id_ = "cascade-" + std::to_string(
                                      std::chrono::duration_cast<std::chrono::microseconds>(wall).count()) +
                      "-" + std::to_string(::getpid());
    }

    int run() {
        nemotron_.start();
        parakeet_.start();
        session_start_ = Clock::now();
        sample_resources();

        const int input_flags = ::fcntl(STDIN_FILENO, F_GETFL, 0);
        if (input_flags >= 0)
            ::fcntl(STDIN_FILENO, F_SETFL, input_flags | O_NONBLOCK);

        std::vector<std::uint8_t> input;
        input.reserve(kChunkSamples * sizeof(float) * 2);
        bool eof = false;
        while (!eof) {
            if (g_cancelled.load())
                throw Cancelled();
            pump_parakeet();
            process_deadlines();
            sample_resources();

            struct pollfd fds[3] = {
                {STDIN_FILENO, POLLIN | POLLHUP | POLLERR, 0},
                {nemotron_.output_fd(), POLLIN | POLLHUP | POLLERR, 0},
                {parakeet_.output_fd(), POLLIN | POLLHUP | POLLERR, 0},
            };
            ::poll(fds, 3, 50);
            if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
                std::array<std::uint8_t, kChunkSamples * sizeof(float) * 4> bytes {};
                while (true) {
                    const ssize_t count = ::read(STDIN_FILENO, bytes.data(), bytes.size());
                    if (count > 0) {
                        input.insert(input.end(), bytes.begin(), bytes.begin() + count);
                        continue;
                    }
                    if (count == 0)
                        eof = true;
                    if (count < 0 && errno == EINTR)
                        continue;
                    break;
                }
            }
            const std::size_t chunk_bytes = kChunkSamples * sizeof(float);
            while (input.size() >= chunk_bytes) {
                std::array<float, kChunkSamples> samples {};
                std::memcpy(samples.data(), input.data(), chunk_bytes);
                input.erase(input.begin(), input.begin() + static_cast<std::ptrdiff_t>(chunk_bytes));
                feed(samples.data(), samples.size());
            }
        }

        if (input.size() % sizeof(float) != 0)
            throw std::runtime_error("stdin ended with a partial float32 sample");
        if (!input.empty()) {
            std::vector<float> tail(input.size() / sizeof(float));
            std::memcpy(tail.data(), input.data(), input.size());
            feed(tail.data(), tail.size());
        }
        finish_nemotron();

        while (!pending_.empty()) {
            if (g_cancelled.load())
                throw Cancelled();
            pump_parakeet();
            process_deadlines();
            sample_resources();
            struct pollfd fd { parakeet_.output_fd(), POLLIN | POLLHUP | POLLERR, 0 };
            ::poll(&fd, 1, 10);
        }

        nemotron_.stop(false);
        parakeet_.stop(false);
        sample_resources();
        if (!options_.jsonl && viewer_active_)
            std::cout << "\r\033[2K" << std::flush;

        const auto wall_ms = elapsed_ms();
        struct rusage self_usage {};
        struct rusage child_usage {};
        ::getrusage(RUSAGE_SELF, &self_usage);
        ::getrusage(RUSAGE_CHILDREN, &child_usage);
        const auto seconds = [](const timeval& value) {
            return static_cast<double>(value.tv_sec) + static_cast<double>(value.tv_usec) / 1e6;
        };
        const double user_seconds = seconds(self_usage.ru_utime) + seconds(child_usage.ru_utime);
        const double system_seconds = seconds(self_usage.ru_stime) + seconds(child_usage.ru_stime);
        const std::uint64_t audio_ms = total_samples_ * 1000ULL / kSampleRate;
        std::ostringstream result;
        result << "{\"session_id\":\"" << json_escape(session_id_)
               << "\",\"status\":\"complete\",\"segments\":" << committed_segments_
               << ",\"degraded_segments\":" << degraded_segments_
               << ",\"audio_ms\":" << audio_ms
               << ",\"wall_ms\":" << wall_ms
               << ",\"real_time_factor\":"
               << (audio_ms ? static_cast<double>(wall_ms) / static_cast<double>(audio_ms) : 0.0)
               << ",\"user_seconds\":" << user_seconds
               << ",\"system_seconds\":" << system_seconds
               << ",\"peak_rss_kb\":" << peak_rss_kb_
               << ",\"nemotron_loads\":" << nemotron_.starts()
               << ",\"parakeet_loads\":" << parakeet_.starts()
               << ",\"endpoint_ms\":" << options_.endpoint_ms
               << ",\"deadline_ms\":" << options_.deadline_ms
               << ",\"paced\":" << (options_.pace ? "true" : "false")
               << ",\"right_context_frames\":" << options_.right_context << "}";
        audit_.publish(result.str(), transcript_);
        return 0;
    }

  private:
    std::uint64_t elapsed_ms() const { return duration_ms(Clock::now() - session_start_); }

    static std::uint64_t process_rss_kb(pid_t pid) {
        if (pid <= 0)
            return 0;
        std::ifstream statm("/proc/" + std::to_string(pid) + "/statm");
        std::uint64_t pages = 0;
        std::uint64_t resident = 0;
        if (!(statm >> pages >> resident))
            return 0;
        return resident * static_cast<std::uint64_t>(::sysconf(_SC_PAGESIZE)) / 1024ULL;
    }

    void sample_resources() {
        const std::uint64_t total = process_rss_kb(::getpid()) + process_rss_kb(nemotron_.pid()) +
                                    process_rss_kb(parakeet_.pid());
        peak_rss_kb_ = std::max(peak_rss_kb_, total);
    }

    std::uint64_t latency_ms(std::uint64_t audio_end_ms) const {
        const std::uint64_t now = elapsed_ms();
        return now > audio_end_ms ? now - audio_end_ms : 0;
    }

    std::string event_json(
        const Segment& segment, std::uint32_t revision, const std::string& state,
        const std::string& model, const std::string& text, bool degraded,
        const std::string& reason) {
        std::ostringstream out;
        out << "{\"sequence\":" << sequence_++ << ",\"monotonic_ms\":" << elapsed_ms()
            << ",\"session_id\":\"" << json_escape(session_id_)
            << "\",\"track_id\":\"interactive\",\"segment_id\":" << segment.id
            << ",\"revision\":" << revision << ",\"state\":\"" << state
            << "\",\"audio_start_ms\":" << segment.audio_start_ms
            << ",\"audio_end_ms\":" << segment.audio_end_ms << ",\"model\":\""
            << model << "\",\"latency_ms\":" << latency_ms(segment.audio_end_ms)
            << ",\"text\":\"" << json_escape(text) << "\",\"degraded\":"
            << (degraded ? "true" : "false") << ",\"degradation_reason\":";
        if (reason.empty())
            out << "null";
        else
            out << "\"" << json_escape(reason) << "\"";
        out << "}";
        return out.str();
    }

    void emit(
        const Segment& segment, std::uint32_t revision, const std::string& state,
        const std::string& model, const std::string& text, bool degraded = false,
        const std::string& reason = {}) {
        const std::string line = event_json(segment, revision, state, model, text, degraded, reason);
        audit_.event(line);
        if (options_.jsonl) {
            std::cout << line << '\n' << std::flush;
            return;
        }
        if (state == "committed") {
            std::cout << "\r\033[2K" << text;
            if (degraded)
                std::cout << "  [Nemotron: " << reason << "]";
            std::cout << '\n' << std::flush;
            viewer_active_ = false;
        } else {
            std::cout << "\r\033[2K" << text << std::flush;
            viewer_active_ = true;
        }
    }

    void feed(const float* samples, std::size_t count) {
        if (options_.pace) {
            const std::uint64_t target_ms = (total_samples_ + count) * 1000ULL / kSampleRate;
            while (elapsed_ms() < target_ms) {
                if (g_cancelled.load())
                    throw Cancelled();
                pump_parakeet();
                process_deadlines();
                sample_resources();
                const std::uint64_t remaining = target_ms - elapsed_ms();
                const int wait_ms = static_cast<int>(std::min<std::uint64_t>(remaining, 20));
                struct pollfd fd { parakeet_.output_fd(), POLLIN | POLLHUP | POLLERR, 0 };
                ::poll(&fd, 1, wait_ms);
            }
        }
        segment_audio_.insert(segment_audio_.end(), samples, samples + count);
        total_samples_ += count;
        const std::uint32_t request = ++nemotron_request_;
        if (!nemotron_.send(protocol::Command::audio, request, samples, count))
            throw std::runtime_error("Nemotron worker input failed");
        wait_nemotron_ack(request);
    }

    void finish_nemotron() {
        const std::uint32_t request = ++nemotron_request_;
        if (!nemotron_.send(protocol::Command::finish, request, nullptr, 0))
            throw std::runtime_error("Nemotron worker finish failed");
        wait_nemotron_ack(request);
    }

    void wait_nemotron_ack(std::uint32_t request) {
        while (true) {
            if (g_cancelled.load())
                throw Cancelled();
            std::vector<protocol::Packet> packets;
            nemotron_.collect(&packets);
            for (const auto& packet : packets) {
                if (packet.kind == protocol::Message::ack && packet.id == request)
                    return;
                if (packet.kind == protocol::Message::partial || packet.kind == protocol::Message::final)
                    handle_nemotron(packet);
                else if (packet.kind == protocol::Message::error)
                    throw std::runtime_error("Nemotron worker failed: " + packet.text);
            }
            pump_parakeet();
            process_deadlines();
            sample_resources();
            if (!nemotron_.running())
                throw std::runtime_error("Nemotron worker exited unexpectedly");
            struct pollfd fds[2] = {
                {nemotron_.output_fd(), POLLIN | POLLHUP | POLLERR, 0},
                {parakeet_.output_fd(), POLLIN | POLLHUP | POLLERR, 0},
            };
            ::poll(fds, 2, 10);
        }
    }

    void handle_nemotron(const protocol::Packet& packet) {
        const std::string text = trim(packet.text);
        if (packet.kind == protocol::Message::partial) {
            if (text.empty() || text == last_partial_)
                return;
            Segment partial;
            partial.id = next_segment_id_;
            partial.audio_start_ms = segment_audio_base_sample_ * 1000ULL / kSampleRate;
            partial.audio_end_ms = std::max(partial.audio_start_ms, packet.audio_ms);
            emit(
                partial, current_revision_++, "provisional", "nemo:nemotron-streaming-en",
                text);
            last_partial_ = text;
            return;
        }
        if (text.empty())
            return;

        auto segment = std::make_shared<Segment>();
        segment->id = next_segment_id_++;
        segment->audio_start_ms = segment_audio_base_sample_ * 1000ULL / kSampleRate;
        segment->audio_end_ms = std::max(segment->audio_start_ms, packet.audio_ms);
        segment->nemotron_text = text;
        segment->selected_text = text;
        segment->selected_revision = current_revision_++;
        segment->next_revision = current_revision_;
        segment->finalized_at = Clock::now();
        segment->deadline = segment->finalized_at + std::chrono::milliseconds(options_.deadline_ms);

        const std::uint64_t requested_end_sample =
            std::min<std::uint64_t>(total_samples_, segment->audio_end_ms * kSampleRate / 1000ULL);
        const std::uint64_t available = requested_end_sample > segment_audio_base_sample_
                                            ? requested_end_sample - segment_audio_base_sample_
                                            : 0;
        const std::size_t take = std::min<std::size_t>(segment_audio_.size(), available);
        segment->audio.assign(segment_audio_.begin(), segment_audio_.begin() + static_cast<std::ptrdiff_t>(take));
        segment_audio_.erase(
            segment_audio_.begin(), segment_audio_.begin() + static_cast<std::ptrdiff_t>(take));
        segment_audio_base_sample_ += take;

        emit(
            *segment, segment->selected_revision, "model_final",
            "nemo:nemotron-streaming-en", segment->nemotron_text);
        pending_.push_back(segment);
        current_revision_ = 0;
        last_partial_.clear();
        queue_correction(segment);
        commit_ready();
    }

    void queue_correction(const std::shared_ptr<Segment>& segment) {
        if (!active_) {
            dispatch(segment);
            return;
        }
        if (!waiting_) {
            waiting_ = segment;
            return;
        }
        degrade(segment, "queue_overload");
    }

    void dispatch(const std::shared_ptr<Segment>& segment) {
        if (Clock::now() >= segment->deadline) {
            degrade(segment, "timeout");
            return;
        }
        if (!parakeet_.running()) {
            try {
                parakeet_.start();
            } catch (const std::exception&) {
                degrade(segment, "worker_failure");
                return;
            }
        }
        if (!parakeet_.send(protocol::Command::job, segment->id, segment->audio)) {
            degrade(segment, "worker_failure");
            restart_parakeet();
            return;
        }
        active_ = segment;
    }

    void pump_parakeet() {
        std::vector<protocol::Packet> packets;
        parakeet_.collect(&packets);
        for (const auto& packet : packets) {
            if (packet.kind == protocol::Message::result) {
                if (!active_ || packet.id != active_->id)
                    continue;  // late or cancelled result
                auto segment = active_;
                active_.reset();
                if (Clock::now() > segment->deadline) {
                    degrade(segment, "timeout");
                } else {
                    const std::string corrected = trim(packet.text);
                    if (corrected.empty()) {
                        degrade(segment, "empty_output");
                    } else {
                        segment->selected_text = corrected;
                        segment->selected_model = "nemo:parakeet-tdt-v3";
                        segment->selected_revision = segment->next_revision++;
                        emit(
                            *segment, segment->selected_revision, "model_final",
                            segment->selected_model, segment->selected_text);
                        segment->resolved = true;
                    }
                }
                dispatch_waiting();
            } else if (packet.kind == protocol::Message::error) {
                if (active_) {
                    auto segment = active_;
                    active_.reset();
                    degrade(segment, "worker_failure");
                }
                restart_parakeet();
                dispatch_waiting();
            }
        }

        if (!parakeet_.running() && active_) {
            auto segment = active_;
            active_.reset();
            degrade(segment, "worker_failure");
            restart_parakeet();
            dispatch_waiting();
        }
        commit_ready();
    }

    void process_deadlines() {
        const auto now = Clock::now();
        if (active_ && now >= active_->deadline) {
            auto segment = active_;
            active_.reset();
            degrade(segment, "timeout");
            restart_parakeet();
            dispatch_waiting();
        }
        if (waiting_ && now >= waiting_->deadline) {
            auto segment = waiting_;
            waiting_.reset();
            degrade(segment, "timeout");
        }
        commit_ready();
    }

    void restart_parakeet() {
        parakeet_.stop(true);
        try {
            parakeet_.start();
        } catch (const Cancelled&) {
            throw;
        } catch (const std::exception& error) {
            std::fprintf(stderr, "cascade: Parakeet restart failed: %s\n", error.what());
        }
    }

    void dispatch_waiting() {
        if (!waiting_ || active_)
            return;
        auto segment = waiting_;
        waiting_.reset();
        dispatch(segment);
    }

    void degrade(const std::shared_ptr<Segment>& segment, const std::string& reason) {
        segment->resolved = true;
        segment->degraded = true;
        segment->degradation_reason = reason;
        segment->selected_text = segment->nemotron_text;
        segment->selected_model = "nemo:nemotron-streaming-en";
    }

    void commit_ready() {
        while (!pending_.empty() && pending_.front()->resolved) {
            const auto segment = pending_.front();
            pending_.pop_front();
            emit(
                *segment, segment->selected_revision, "committed", segment->selected_model,
                segment->selected_text, segment->degraded, segment->degradation_reason);
            transcript_ += segment->selected_text;
            transcript_ += "\n";
            ++committed_segments_;
            if (segment->degraded)
                ++degraded_segments_;
            segment->audio.clear();
        }
    }

    Options options_;
    Audit audit_;
    Worker nemotron_;
    Worker parakeet_;
    Clock::time_point session_start_ = Clock::now();
    std::string session_id_;
    std::uint64_t sequence_ = 0;
    std::uint32_t next_segment_id_ = 0;
    std::uint32_t current_revision_ = 0;
    std::uint32_t nemotron_request_ = 0;
    std::uint64_t total_samples_ = 0;
    std::uint64_t segment_audio_base_sample_ = 0;
    std::vector<float> segment_audio_;
    std::string last_partial_;
    std::deque<std::shared_ptr<Segment>> pending_;
    std::shared_ptr<Segment> active_;
    std::shared_ptr<Segment> waiting_;
    std::string transcript_;
    std::uint64_t committed_segments_ = 0;
    std::uint64_t degraded_segments_ = 0;
    bool viewer_active_ = false;
    std::uint64_t peak_rss_kb_ = 0;
};

void usage(const char* argv0) {
    std::fprintf(
        stderr,
        "Usage: %s [OPTIONS] < 16k-mono-f32le.pcm\n"
        "Options:\n"
        "  --jsonl                 emit canonical JSONL instead of the terminal viewer\n"
        "  --pace                  pace stdin by its 16 kHz sample clock\n"
        "  --audit DIR             atomically publish a private audit bundle\n"
        "  --endpoint-ms N         token-silence endpoint threshold (default: 800)\n"
        "  --deadline-ms N         Parakeet correction deadline (default: 2500)\n"
        "  --right-context N       RNNT encoder frames (default: 1, about 160 ms)\n"
        "  --nemotron MODEL        streaming GGUF path\n"
        "  --parakeet MODEL        correction GGUF path\n"
        "  --worker PATH           native worker path (test/development override)\n",
        argv0);
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--jsonl")
            options.jsonl = true;
        else if (arg == "--pace")
            options.pace = true;
        else if (arg == "--audit" && i + 1 < argc)
            options.audit = argv[++i];
        else if (arg == "--endpoint-ms" && i + 1 < argc)
            options.endpoint_ms = std::atoi(argv[++i]);
        else if (arg == "--deadline-ms" && i + 1 < argc)
            options.deadline_ms = std::atoi(argv[++i]);
        else if (arg == "--right-context" && i + 1 < argc)
            options.right_context = std::atoi(argv[++i]);
        else if (arg == "--nemotron" && i + 1 < argc)
            options.nemotron = argv[++i];
        else if (arg == "--parakeet" && i + 1 < argc)
            options.parakeet = argv[++i];
        else if (arg == "--worker" && i + 1 < argc)
            options.worker = argv[++i];
        else if (arg == "--help" || arg == "-h") {
            usage(argv[0]);
            std::exit(0);
        } else {
            usage(argv[0]);
            throw std::runtime_error("unknown or incomplete option: " + arg);
        }
    }
    if (options.endpoint_ms <= 0 || options.deadline_ms <= 0 || options.right_context < 0)
        throw std::runtime_error("endpoint/deadline must be positive and right context nonnegative");
    if (options.audit == "." || options.audit == "..")
        throw std::runtime_error("invalid audit destination");
    return options;
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGPIPE, SIG_IGN);
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    try {
        Cascade cascade(parse_options(argc, argv));
        return cascade.run();
    } catch (const Cancelled&) {
        std::fprintf(stderr, "cascade: cancelled\n");
        return 130;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "cascade: %s\n", error.what());
        return 2;
    }
}
