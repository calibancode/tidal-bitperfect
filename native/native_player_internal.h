#pragma once

#include <alsa/asoundlib.h>
#include <sndfile.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <iomanip>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <memory>
#include <poll.h>
#include <signal.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <type_traits>
#include <unistd.h>
#include <utility>
#include <vector>

namespace tidal_native {

struct Args {
    std::string file;
    std::string ffmpeg_input;
    std::string device;
    std::string codec = "pcm_s16le";
    int volume_percent = 100;
    double duration_s = 0.0;
    bool protocol_whitelist = false;
    bool smooth_transition = false;
    bool daemon = false;

    bool use_ffmpeg() const {
        return !ffmpeg_input.empty();
    }
};

struct Format {
    int channels = 0;
    int rate = 0;
    int bits = 0;
    double duration_s = 0.0;
};

class SoundFile;

struct PlaybackState {
    bool stop = false;
    bool shutdown = false;
    bool paused = false;
    double gain = 1.0;
    std::string next_id;
    std::string next_path;
    std::unique_ptr<SoundFile> next_input;
    Format next_format;
    std::vector<std::uint8_t> next_prefill;
    sf_count_t next_prefill_frames = 0;
    bool next_ready = false;
    bool smooth_next_transition = false;
    std::vector<std::uint8_t> last_frame;
    Format last_frame_format;

    ~PlaybackState();
};

struct WavFormat {
    Format format;
    int block_align = 0;
};

struct StreamCommandResult {
    bool seek = false;
    double target_s = 0.0;
};

struct IpcMessage {
    std::string type;
    std::vector<std::pair<std::string, std::string>> fields;

    std::string value(const std::string& key, const std::string& fallback = {}) const;
    int int_value(const std::string& key, int fallback = 0) const;
    double double_value(const std::string& key, double fallback = 0.0) const;
    bool bool_value(const std::string& key, bool fallback = false) const;
};

bool starts_with(const std::string& text, const std::string& prefix);
std::string format_seconds_arg(double seconds);
void set_nonblocking(int fd);
void set_blocking(int fd);
void emit_line(const std::string& kind, const std::string& payload);
void emit_message(const std::string& type, std::initializer_list<std::pair<std::string, std::string>> fields = {});
bool read_ipc_message_blocking(IpcMessage& message);
std::string alsa_error(const std::string& context, int err);
double normalize_volume_percent(int percent);
void set_pipewire_alsa_metadata_if_needed(const std::string& device);
Args parse_args(int argc, char** argv);

class SoundFile {
public:
    explicit SoundFile(const std::string& path);
    ~SoundFile();

    SoundFile(const SoundFile&) = delete;
    SoundFile& operator=(const SoundFile&) = delete;

    SNDFILE* get() const;
    const SF_INFO& info() const;

private:
    SNDFILE* file_ = nullptr;
    SF_INFO info_{};
};

class AlsaPcm {
public:
    AlsaPcm(const std::string& device, const Format& fmt);
    ~AlsaPcm();

    AlsaPcm(const AlsaPcm&) = delete;
    AlsaPcm& operator=(const AlsaPcm&) = delete;

    snd_pcm_t* get() const;
    const Format& applied_format() const;
    void drop_and_prepare();
    void set_paused(bool paused);

private:
    snd_pcm_t* pcm_ = nullptr;
    Format applied_format_;
};

class ChildProcess {
public:
    explicit ChildProcess(const std::vector<std::string>& args);
    ~ChildProcess();

    ChildProcess(const ChildProcess&) = delete;
    ChildProcess& operator=(const ChildProcess&) = delete;

    int stdout_fd() const;
    int stderr_fd() const;
    void send_signal(int sig);
    bool is_running();
    void terminate();

private:
    static void close_fd(int& fd);

    pid_t pid_ = -1;
    int stdout_fd_ = -1;
    int stderr_fd_ = -1;
};

class CommandReader {
public:
    CommandReader();

    std::vector<IpcMessage> poll_messages();
    bool closed() const;

private:
    std::string buffer_;
    bool closed_ = false;
};

std::vector<std::string> build_ffmpeg_args(const Args& args, double start_s);
bool read_exact_fd(int fd, std::uint8_t* dest, std::size_t wanted);
WavFormat parse_wav_header_fd(int fd, double duration_s);
Format detect_format(const SF_INFO& info);
void apply_volume_bytes(std::uint8_t* data, std::size_t size, int bits, double gain);
void write_frames(snd_pcm_t* pcm, const void* data, sf_count_t frames, int frame_bytes);
void emit_format(const Format& output, const Format& source = {});
bool same_pcm_format(const Format& left, const Format& right);
std::string format_summary(const Format& fmt);
sf_count_t played_frame_from_written(sf_count_t written_frames, snd_pcm_t* pcm);
sf_count_t alsa_delay_frames(snd_pcm_t* pcm);
double alsa_delay_seconds(const Format& fmt, snd_pcm_t* pcm);
double played_seconds_from_written(sf_count_t written_frames, const Format& fmt, snd_pcm_t* pcm);
void emit_position(SNDFILE* file, const Format& fmt, snd_pcm_t* pcm = nullptr);
double stream_played_position_s(
    std::uint64_t bytes_written,
    int block_align,
    double start_offset_s,
    const Format& fmt,
    snd_pcm_t* pcm
);
void emit_position_values(double pos_s, double duration_s);
sf_count_t clamp_frame(double seconds, const SF_INFO& info);
void seek_to_frame(SNDFILE* file, AlsaPcm& pcm, const Format& fmt, sf_count_t target);
bool handle_next_command(const IpcMessage& message, PlaybackState& state);
void handle_command(
    const IpcMessage& message,
    PlaybackState& state,
    SNDFILE* file,
    const SF_INFO& info,
    const Format& fmt,
    AlsaPcm& pcm,
    bool apply_software_volume
);
bool play_queued_flac(
    AlsaPcm& pcm,
    Format& current_fmt,
    PlaybackState& state,
    bool apply_software_volume,
    CommandReader& commands
);
void drain_stderr(int fd, std::string& stderr_text);
StreamCommandResult handle_stream_command(
    const IpcMessage& message,
    PlaybackState& state,
    AlsaPcm& pcm,
    ChildProcess& child,
    const Format& fmt,
    double current_s,
    double duration_s,
    bool apply_software_volume
);
int run_file_mode(const Args& args, bool* shutdown_requested = nullptr);
int run_ffmpeg_mode(const Args& args, bool* shutdown_requested = nullptr);
int run(int argc, char** argv);

} // namespace tidal_native
