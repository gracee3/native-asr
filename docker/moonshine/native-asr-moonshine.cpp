#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "moonshine-cpp.h"

namespace {
struct Wav {
  int sample_rate = 0;
  std::vector<float> samples;
};

uint16_t u16(std::istream &in) {
  uint8_t b[2];
  in.read(reinterpret_cast<char *>(b), 2);
  return static_cast<uint16_t>(b[0]) | (static_cast<uint16_t>(b[1]) << 8);
}

uint32_t u32(std::istream &in) {
  uint8_t b[4];
  in.read(reinterpret_cast<char *>(b), 4);
  return static_cast<uint32_t>(b[0]) | (static_cast<uint32_t>(b[1]) << 8) |
         (static_cast<uint32_t>(b[2]) << 16) | (static_cast<uint32_t>(b[3]) << 24);
}

Wav read_wav(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot open WAV: " + path);
  char id[4];
  in.read(id, 4);
  if (std::strncmp(id, "RIFF", 4) != 0) throw std::runtime_error("not RIFF PCM");
  (void)u32(in);
  in.read(id, 4);
  if (std::strncmp(id, "WAVE", 4) != 0) throw std::runtime_error("not WAVE PCM");
  uint16_t format = 0, channels = 0, bits = 0;
  uint32_t rate = 0;
  std::vector<int16_t> pcm;
  while (in.read(id, 4)) {
    const uint32_t size = u32(in);
    if (std::strncmp(id, "fmt ", 4) == 0) {
      format = u16(in);
      channels = u16(in);
      rate = u32(in);
      (void)u32(in);
      (void)u16(in);
      bits = u16(in);
      if (size > 16) in.seekg(size - 16, std::ios::cur);
    } else if (std::strncmp(id, "data", 4) == 0) {
      pcm.resize(size / sizeof(int16_t));
      in.read(reinterpret_cast<char *>(pcm.data()), size);
    } else {
      in.seekg(size, std::ios::cur);
    }
    if (size & 1U) in.seekg(1, std::ios::cur);
  }
  if (format != 1 || channels != 1 || bits != 16 || rate == 0 || pcm.empty())
    throw std::runtime_error("WAV must be mono PCM16");
  Wav wav;
  wav.sample_rate = static_cast<int>(rate);
  wav.samples.reserve(pcm.size());
  for (int16_t sample : pcm) wav.samples.push_back(static_cast<float>(sample) / 32768.0F);
  return wav;
}

std::string quote(const std::string &value) {
  std::string out = "\"";
  for (unsigned char c : value) {
    switch (c) {
      case '\\': out += "\\\\"; break;
      case '"': out += "\\\""; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buffer[7];
          std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
          out += buffer;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out + "\"";
}

struct Listener : moonshine::TranscriptEventListener {
  bool events;
  uint64_t *sequence;
  double *audio_position;
  std::chrono::steady_clock::time_point origin;
  std::map<uint64_t, std::string> finals;
  std::map<uint64_t, std::string> partial_text;
  uint64_t partial_events = 0;
  uint64_t revisions = 0;
  uint64_t errors = 0;

  Listener(bool emit_events, uint64_t *event_sequence,
           double *current_audio_position,
           std::chrono::steady_clock::time_point started)
      : events(emit_events),
        sequence(event_sequence),
        audio_position(current_audio_position),
        origin(started) {}

  void emit(const char *type, const moonshine::TranscriptLine &line, bool final) {
    if (!events) return;
    const double emitted = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - origin).count();
    std::cout << "{\"event\":" << quote(type)
              << ",\"sequence\":" << (*sequence)++
              << ",\"audio_position_seconds\":" << *audio_position
              << ",\"monotonic_emission_seconds\":" << emitted
              << ",\"text\":" << quote(line.text)
              << ",\"final\":" << (final ? "true" : "false")
              << ",\"latency_ms\":" << line.lastTranscriptionLatencyMs << "}\n";
    std::cout.flush();
  }
  void onLineStarted(const moonshine::LineStarted &event) override {
    ++partial_events;
    partial_text[event.line.lineId] = event.line.text;
    emit("stt_partial", event.line, false);
  }
  void onLineTextChanged(const moonshine::LineTextChanged &event) override {
    ++partial_events;
    const auto previous = partial_text.find(event.line.lineId);
    if (previous != partial_text.end() && previous->second != event.line.text) ++revisions;
    partial_text[event.line.lineId] = event.line.text;
    emit("stt_partial", event.line, false);
  }
  void onLineCompleted(const moonshine::LineCompleted &event) override {
    finals[event.line.lineId] = event.line.text;
    emit("stt_final", event.line, true);
  }
  void onError(const moonshine::Error &event) override {
    ++errors;
    if (!events) return;
    moonshine::TranscriptLine line;
    line.text = event.errorMessage;
    emit("stt_error", line, true);
  }
  std::string text() const {
    std::string result;
    for (const auto &[id, line] : finals) {
      (void)id;
      if (!result.empty() && !line.empty()) result += ' ';
      result += line;
    }
    return result;
  }
};
}  // namespace

int main(int argc, char **argv) {
  try {
    std::string model;
    double interval = 0.5;
    bool events = false, pace = false;
    std::vector<std::string> files;
    for (int i = 1; i < argc; ++i) {
      const std::string arg = argv[i];
      if (arg == "--model" && i + 1 < argc) model = argv[++i];
      else if (arg == "--update-interval" && i + 1 < argc) interval = std::stod(argv[++i]);
      else if (arg == "--events") events = true;
      else if (arg == "--pace") pace = true;
      else if (!arg.empty() && arg[0] == '-') throw std::runtime_error("unknown option: " + arg);
      else files.push_back(arg);
    }
    if (model.empty() || files.empty()) throw std::runtime_error("--model and WAV input are required");
    const auto load_started = std::chrono::steady_clock::now();
    moonshine::Transcriber transcriber(model, moonshine::ModelArch::SMALL_STREAMING, interval);
    const double load_seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - load_started).count();
    uint64_t sequence = 0;
    for (const auto &file : files) {
      Wav wav = read_wav(file);
      double audio_position = 0.0;
      const auto stream_started = std::chrono::steady_clock::now();
      Listener listener(events, &sequence, &audio_position, stream_started);
      auto stream = transcriber.createStream(interval);
      stream.addListener(&listener);
      stream.start();
      const size_t chunk = static_cast<size_t>(wav.sample_rate / 50);
      for (size_t offset = 0; offset < wav.samples.size(); offset += chunk) {
        const size_t end = std::min(offset + chunk, wav.samples.size());
        std::vector<float> part(wav.samples.begin() + offset, wav.samples.begin() + end);
        audio_position = static_cast<double>(end) / wav.sample_rate;
        stream.addAudio(part, wav.sample_rate);
        if (pace) {
          std::this_thread::sleep_until(stream_started + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
              std::chrono::duration<double>(audio_position)));
        }
      }
      stream.stop();
      const double wall = std::chrono::duration<double>(
          std::chrono::steady_clock::now() - stream_started).count();
      if (events) {
        std::cout << "{\"event\":\"stt_metrics\",\"sequence\":" << sequence++
                  << ",\"audio_position_seconds\":" << audio_position
                  << ",\"monotonic_emission_seconds\":" << wall
                  << ",\"text\":" << quote(listener.text())
                  << ",\"final\":true,\"model_load_seconds\":" << load_seconds
                  << ",\"wall_seconds\":" << wall
                  << ",\"latency_ms\":"
                  << (pace ? std::to_string(std::max(0.0, (wall - audio_position) * 1000.0)) : "null")
                  << ",\"real_time_factor\":" << (audio_position > 0 ? wall / audio_position : 0)
                  << ",\"partial_events\":" << listener.partial_events
                  << ",\"revisions\":" << listener.revisions
                  << ",\"failures\":" << listener.errors << "}\n";
      } else {
        std::cout << "{\"runtime\":\"moonshine\",\"text\":" << quote(listener.text())
                  << ",\"model_path\":" << quote(model)
                  << ",\"audio_path\":" << quote(file)
                  << ",\"segmentation\":\"native-streaming\",\"model_load_seconds\":"
                  << load_seconds << "}\n";
      }
    }
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "error: " << error.what() << '\n';
    return 2;
  }
}
