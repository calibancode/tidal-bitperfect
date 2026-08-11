#include "native_player_internal.h"

namespace tidal_native {

namespace {

constexpr sf_count_t kPlaybackChunkFrames = 4096;
constexpr double kStreamStagingReserveSeconds = 2.0;
constexpr sf_count_t kStreamWriteChunkFrames = 2048;

std::size_t stream_staging_reserve_bytes(const WavFormat& wav) {
    if (wav.format.rate <= 0 || wav.block_align <= 0) {
        return 0;
    }
    return static_cast<std::size_t>(std::llround(
        kStreamStagingReserveSeconds * static_cast<double>(wav.format.rate) * static_cast<double>(wav.block_align)
    ));
}

sf_count_t stream_remaining_frames(double duration_s, double start_offset_s, const Format& fmt) {
    if (duration_s <= 0.0 || fmt.rate <= 0) {
        return 0;
    }
    const double remaining_s = std::max(0.0, duration_s - start_offset_s);
    return static_cast<sf_count_t>(std::llround(remaining_s * static_cast<double>(fmt.rate)));
}

sf_count_t stream_written_frames(std::uint64_t bytes_written, int block_align) {
    if (block_align <= 0) {
        return 0;
    }
    return static_cast<sf_count_t>(bytes_written / static_cast<std::uint64_t>(block_align));
}

bool queued_stream_handoff_ready(const PlaybackState& state, const Format& current_fmt) {
    return state.next_ready && state.next_input && same_pcm_format(current_fmt, state.next_format);
}

template <typename Sample>
Sample read_sample(const std::uint8_t* data) {
    Sample value = 0;
    std::memcpy(&value, data, sizeof(Sample));
    return value;
}

template <typename Sample>
void write_sample(std::uint8_t* data, Sample value) {
    std::memcpy(data, &value, sizeof(Sample));
}

void remember_last_frame(PlaybackState& state, const std::uint8_t* data, std::size_t bytes, const Format& fmt) {
    const int frame_bytes = fmt.channels * (fmt.bits / 8);
    if (data == nullptr || frame_bytes <= 0 || bytes < static_cast<std::size_t>(frame_bytes)) {
        return;
    }
    state.last_frame.assign(data + bytes - static_cast<std::size_t>(frame_bytes), data + bytes);
    state.last_frame_format = fmt;
}

template <typename Sample>
void smooth_transition_samples(std::vector<std::uint8_t>& data, sf_count_t frames, const Format& fmt, const std::vector<std::uint8_t>& last_frame) {
    const int sample_bytes = static_cast<int>(sizeof(Sample));
    const int frame_bytes = fmt.channels * sample_bytes;
    if (frames < 2 || fmt.channels <= 0 || frame_bytes <= 0 || last_frame.size() < static_cast<std::size_t>(frame_bytes)) {
        return;
    }
    const sf_count_t ramp_frames = std::min<sf_count_t>(frames, std::max<sf_count_t>(2, fmt.rate > 0 ? fmt.rate / 200 : 220));
    std::vector<long double> first_samples(static_cast<std::size_t>(fmt.channels), 0.0L);
    std::vector<long double> last_samples(static_cast<std::size_t>(fmt.channels), 0.0L);
    for (int channel = 0; channel < fmt.channels; ++channel) {
        const auto offset = static_cast<std::size_t>(channel * sample_bytes);
        first_samples[static_cast<std::size_t>(channel)] = static_cast<long double>(read_sample<Sample>(data.data() + offset));
        last_samples[static_cast<std::size_t>(channel)] = static_cast<long double>(read_sample<Sample>(last_frame.data() + offset));
    }
    const auto min_value = static_cast<long double>(std::numeric_limits<Sample>::min());
    const auto max_value = static_cast<long double>(std::numeric_limits<Sample>::max());
    for (sf_count_t frame = 0; frame < ramp_frames; ++frame) {
        const long double factor = static_cast<long double>(ramp_frames - 1 - frame) / static_cast<long double>(ramp_frames - 1);
        for (int channel = 0; channel < fmt.channels; ++channel) {
            const auto offset = (static_cast<std::size_t>(frame) * static_cast<std::size_t>(fmt.channels) + static_cast<std::size_t>(channel))
                * static_cast<std::size_t>(sample_bytes);
            const long double original = static_cast<long double>(read_sample<Sample>(data.data() + offset));
            const long double correction = (last_samples[static_cast<std::size_t>(channel)] - first_samples[static_cast<std::size_t>(channel)]) * factor;
            const long double smoothed = std::clamp(original + correction, min_value, max_value);
            write_sample<Sample>(data.data() + offset, static_cast<Sample>(std::llround(smoothed)));
        }
    }
}

void smooth_transition_start(PlaybackState& state, std::vector<std::uint8_t>& data, sf_count_t frames, const Format& fmt) {
    if (!state.smooth_next_transition || data.empty()) {
        return;
    }
    if (!same_pcm_format(state.last_frame_format, fmt)) {
        return;
    }
    if (fmt.bits == 16) {
        smooth_transition_samples<std::int16_t>(data, frames, fmt, state.last_frame);
    } else if (fmt.bits == 32) {
        smooth_transition_samples<std::int32_t>(data, frames, fmt, state.last_frame);
    }
}

} // namespace

template <typename Sample>
void apply_volume(std::vector<Sample>& samples, sf_count_t frames, int channels, double gain) {
    if (gain >= 0.9999) {
        return;
    }
    const auto count = static_cast<std::size_t>(frames) * static_cast<std::size_t>(channels);
    const auto min_value = static_cast<long double>(std::numeric_limits<Sample>::min());
    const auto max_value = static_cast<long double>(std::numeric_limits<Sample>::max());
    for (std::size_t i = 0; i < count; ++i) {
        long double scaled = static_cast<long double>(samples[i]) * gain;
        scaled = std::clamp(scaled, min_value, max_value);
        samples[i] = static_cast<Sample>(std::llround(scaled));
    }
}

void apply_volume_bytes(std::uint8_t* data, std::size_t size, int bits, double gain) {
    if (gain >= 0.9999 || data == nullptr || size == 0) {
        return;
    }
    if (bits == 16) {
        for (std::size_t i = 0; i + sizeof(std::int16_t) <= size; i += sizeof(std::int16_t)) {
            std::int16_t sample = 0;
            std::memcpy(&sample, data + i, sizeof(sample));
            long double scaled = static_cast<long double>(sample) * gain;
            scaled = std::clamp(
                scaled,
                static_cast<long double>(std::numeric_limits<std::int16_t>::min()),
                static_cast<long double>(std::numeric_limits<std::int16_t>::max())
            );
            sample = static_cast<std::int16_t>(std::llround(scaled));
            std::memcpy(data + i, &sample, sizeof(sample));
        }
        return;
    }
    if (bits == 32) {
        for (std::size_t i = 0; i + sizeof(std::int32_t) <= size; i += sizeof(std::int32_t)) {
            std::int32_t sample = 0;
            std::memcpy(&sample, data + i, sizeof(sample));
            long double scaled = static_cast<long double>(sample) * gain;
            scaled = std::clamp(
                scaled,
                static_cast<long double>(std::numeric_limits<std::int32_t>::min()),
                static_cast<long double>(std::numeric_limits<std::int32_t>::max())
            );
            sample = static_cast<std::int32_t>(std::llround(scaled));
            std::memcpy(data + i, &sample, sizeof(sample));
        }
    }
}

sf_count_t clamp_frame(double seconds, const SF_INFO& info) {
    const double frame_value = seconds * static_cast<double>(info.samplerate);
    sf_count_t target = static_cast<sf_count_t>(std::llround(frame_value));
    target = std::max<sf_count_t>(0, target);
    if (info.frames > 0) {
        target = std::min<sf_count_t>(target, info.frames);
    }
    return target;
}

void seek_to_frame(SNDFILE* file, AlsaPcm& pcm, const Format& fmt, sf_count_t target) {
    if (sf_seek(file, target, SEEK_SET) < 0) {
        throw std::runtime_error(std::string("FLAC seek failed: ") + sf_strerror(file));
    }
    pcm.drop_and_prepare();
    emit_position(file, fmt, pcm.get());
}

template <typename Sample>
sf_count_t read_prefill(SoundFile& input, const Format& fmt, std::vector<std::uint8_t>& out) {
    std::vector<Sample> samples(static_cast<std::size_t>(kPlaybackChunkFrames) * static_cast<std::size_t>(fmt.channels));
    const sf_count_t frames = std::is_same<Sample, short>::value
        ? sf_readf_short(input.get(), reinterpret_cast<short*>(samples.data()), kPlaybackChunkFrames)
        : sf_readf_int(input.get(), reinterpret_cast<int*>(samples.data()), kPlaybackChunkFrames);
    if (frames < 0) {
        throw std::runtime_error(std::string("FLAC prefill failed: ") + sf_strerror(input.get()));
    }
    const auto bytes = static_cast<std::size_t>(frames) * static_cast<std::size_t>(fmt.channels) * sizeof(Sample);
    out.resize(bytes);
    if (bytes > 0) {
        std::memcpy(out.data(), samples.data(), bytes);
    }
    return frames;
}

PlaybackState::~PlaybackState() = default;

void clear_next_track(PlaybackState& state) {
    state.next_id.clear();
    state.next_path.clear();
    state.next_input.reset();
    state.next_format = {};
    state.next_prefill.clear();
    state.next_prefill_frames = 0;
    state.next_ready = false;
}

bool prepare_next_track(PlaybackState& state) {
    if (state.next_path.empty()) {
        clear_next_track(state);
        return false;
    }
    try {
        auto input = std::make_unique<SoundFile>(state.next_path);
        state.next_format = detect_format(input->info());
        state.next_prefill_frames = state.next_format.bits == 16
            ? read_prefill<short>(*input, state.next_format, state.next_prefill)
            : read_prefill<int>(*input, state.next_format, state.next_prefill);
        state.next_input = std::move(input);
        state.next_ready = true;
        return true;
    } catch (const std::exception& exc) {
        emit_line("LOG", std::string("native next prepare failed: ") + exc.what());
        clear_next_track(state);
        return false;
    }
}

bool handle_next_command(const IpcMessage& message, PlaybackState& state) {
    if (message.type == "clear_next") {
        clear_next_track(state);
        return true;
    }
    if (message.type != "next") {
        return false;
    }
    state.next_id = message.value("track_id");
    state.next_path = message.value("path");
    state.next_input.reset();
    state.next_format = {};
    state.next_ready = false;
    prepare_next_track(state);
    return true;
}

void handle_command(
    const IpcMessage& message,
    PlaybackState& state,
    SNDFILE* file,
    const SF_INFO& info,
    const Format& fmt,
    AlsaPcm& pcm,
    bool /*apply_software_volume*/
) {
    if (handle_next_command(message, state)) {
        return;
    }

    const std::string& command = message.type;
    if (command == "stop") {
        state.stop = true;
        return;
    }
    if (command == "shutdown" || command == "quit") {
        state.stop = true;
        state.shutdown = true;
        return;
    }
    if (command == "pause_toggle") {
        state.paused = !state.paused;
        pcm.set_paused(state.paused);
        emit_line("STATUS", state.paused ? "Paused" : "Playing");
        return;
    }
    if (command == "set_volume") {
        const double percent = message.double_value("percent", 100.0);
        state.gain = normalize_volume_percent(static_cast<int>(std::llround(percent)));
        return;
    }
    if (command == "seek" || command == "seek_to") {
        const double value = message.double_value("seconds", 0.0);
        const sf_count_t current = sf_seek(file, 0, SEEK_CUR);
        const double current_s = current >= 0 ? static_cast<double>(current) / static_cast<double>(info.samplerate) : 0.0;
        const double target_s = command == "seek" ? current_s + value : value;
        emit_line("STATUS", "Seeking...");
        seek_to_frame(file, pcm, fmt, clamp_frame(target_s, info));
        emit_line("STATUS", state.paused ? "Paused" : "Playing");
    }
}

template <typename Sample>
void playback_loop(
    SoundFile& input,
    AlsaPcm& pcm,
    const Format& fmt,
    PlaybackState& state,
    bool apply_software_volume,
    CommandReader& commands,
    const std::function<void()>& after_first_write = {}
) {
    std::vector<Sample> buffer(static_cast<std::size_t>(kPlaybackChunkFrames) * static_cast<std::size_t>(fmt.channels));
    auto last_position = std::chrono::steady_clock::now() - std::chrono::milliseconds(250);
    bool first_write = true;

    while (!state.stop) {
        for (const IpcMessage& message : commands.poll_messages()) {
            handle_command(message, state, input.get(), input.info(), fmt, pcm, apply_software_volume);
            if (state.stop) {
                break;
            }
        }
        if (commands.closed()) {
            state.stop = true;
            state.shutdown = true;
        }
        if (state.stop) {
            break;
        }
        if (state.paused) {
            const auto now = std::chrono::steady_clock::now();
            if (now - last_position >= std::chrono::milliseconds(250)) {
                emit_position(input.get(), fmt, pcm.get());
                last_position = now;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }

        const sf_count_t frames = std::is_same<Sample, short>::value
            ? sf_readf_short(input.get(), reinterpret_cast<short*>(buffer.data()), kPlaybackChunkFrames)
            : sf_readf_int(input.get(), reinterpret_cast<int*>(buffer.data()), kPlaybackChunkFrames);
        if (frames < 0) {
            throw std::runtime_error(std::string("FLAC read failed: ") + sf_strerror(input.get()));
        }
        if (frames == 0) {
            break;
        }

        if (apply_software_volume) {
            apply_volume(buffer, frames, fmt.channels, state.gain);
        }
        remember_last_frame(state, reinterpret_cast<const std::uint8_t*>(buffer.data()), static_cast<std::size_t>(frames) * static_cast<std::size_t>(fmt.channels) * sizeof(Sample), fmt);
        write_frames(pcm.get(), buffer.data(), frames, fmt.channels * (fmt.bits / 8));
        if (first_write) {
            first_write = false;
            if (after_first_write) {
                after_first_write();
            }
        }

        const auto now = std::chrono::steady_clock::now();
        if (now - last_position >= std::chrono::milliseconds(250)) {
            emit_position(input.get(), fmt, pcm.get());
            last_position = now;
        }
    }
}

bool play_queued_flac(
    AlsaPcm& pcm,
    Format& current_fmt,
    PlaybackState& state,
    bool apply_software_volume,
    CommandReader& commands
) {
    if (state.stop || state.next_path.empty()) {
        return false;
    }

    const std::string next_id = state.next_id;
    const std::string next_path = state.next_path;
    std::unique_ptr<SoundFile> next_input;
    Format next_fmt;
    std::vector<std::uint8_t> prefill;
    sf_count_t prefill_frames = 0;
    if (state.next_ready && state.next_input) {
        next_input = std::move(state.next_input);
        next_fmt = state.next_format;
        prefill = std::move(state.next_prefill);
        prefill_frames = state.next_prefill_frames;
    } else {
        try {
            next_input = std::make_unique<SoundFile>(next_path);
            next_fmt = detect_format(next_input->info());
        } catch (const std::exception& exc) {
            emit_line("LOG", std::string("native next open failed: ") + exc.what());
            clear_next_track(state);
            return false;
        }
    }
    clear_next_track(state);
    if (!same_pcm_format(current_fmt, next_fmt)) {
        emit_line(
            "LOG",
            "native next format mismatch: " + format_summary(current_fmt) + " -> " + format_summary(next_fmt)
        );
        return false;
    }

    current_fmt = next_fmt;
    Format output_fmt = pcm.applied_format();
    output_fmt.duration_s = current_fmt.duration_s;
    bool emitted_advanced = false;
    auto emit_advanced = [&]() {
        if (emitted_advanced) {
            return;
        }
        emitted_advanced = true;
        emit_message("ADVANCED", {{"track_id", next_id}});
        emit_format(output_fmt, current_fmt);
        emit_line("STATUS", state.paused ? "Paused" : "Playing");
    };
    if (state.paused) {
        if (prefill_frames > 0 && sf_seek(next_input->get(), 0, SEEK_SET) < 0) {
            throw std::runtime_error(std::string("FLAC prefill rewind failed: ") + sf_strerror(next_input->get()));
        }
        emit_advanced();
    } else if (prefill_frames > 0) {
        if (apply_software_volume) {
            apply_volume_bytes(prefill.data(), prefill.size(), current_fmt.bits, state.gain);
        }
        smooth_transition_start(state, prefill, prefill_frames, current_fmt);
        remember_last_frame(state, prefill.data(), prefill.size(), current_fmt);
        write_frames(pcm.get(), prefill.data(), prefill_frames, current_fmt.channels * (current_fmt.bits / 8));
        emit_advanced();
    }

    if (current_fmt.bits == 16) {
        playback_loop<short>(*next_input, pcm, current_fmt, state, apply_software_volume, commands, emit_advanced);
    } else {
        playback_loop<int>(*next_input, pcm, current_fmt, state, apply_software_volume, commands, emit_advanced);
    }
    return true;
}


int run_file_mode(const Args& args, bool* shutdown_requested) {
    auto input = std::make_unique<SoundFile>(args.file);
    Format fmt = detect_format(input->info());

    PlaybackState state;
    state.gain = normalize_volume_percent(args.volume_percent);
    const bool apply_software_volume = !starts_with(args.device, "hw:");
    CommandReader commands;

    emit_line("STATUS", "Opening ALSA device...");
    AlsaPcm pcm(args.device, fmt);
    emit_format(pcm.applied_format(), fmt);
    emit_line("STATUS", "Playing");

    if (fmt.bits == 16) {
        playback_loop<short>(*input, pcm, fmt, state, apply_software_volume, commands);
    } else {
        playback_loop<int>(*input, pcm, fmt, state, apply_software_volume, commands);
    }
    while (!state.stop && play_queued_flac(pcm, fmt, state, apply_software_volume, commands)) {
    }

    if (state.stop) {
        snd_pcm_drop(pcm.get());
    } else {
        snd_pcm_drain(pcm.get());
        emit_position_values(fmt.duration_s, fmt.duration_s);
    }
    emit_line("DONE", "");
    if (shutdown_requested != nullptr) {
        *shutdown_requested = state.shutdown;
    }
    return 0;
}

StreamCommandResult handle_stream_command(
    const IpcMessage& message,
    PlaybackState& state,
    AlsaPcm& pcm,
    ChildProcess& child,
    const Format& /*fmt*/,
    double current_s,
    double duration_s,
    bool /*apply_software_volume*/
) {
    StreamCommandResult result;
    if (handle_next_command(message, state)) {
        return result;
    }

    const std::string& command = message.type;
    if (command == "stop") {
        state.stop = true;
        child.terminate();
        return result;
    }
    if (command == "shutdown" || command == "quit") {
        state.stop = true;
        state.shutdown = true;
        child.terminate();
        return result;
    }
    if (command == "pause_toggle") {
        state.paused = !state.paused;
        pcm.set_paused(state.paused);
        child.send_signal(state.paused ? SIGSTOP : SIGCONT);
        emit_line("STATUS", state.paused ? "Paused" : "Playing");
        return result;
    }
    if (command == "set_volume") {
        const double percent = message.double_value("percent", 100.0);
        state.gain = normalize_volume_percent(static_cast<int>(std::llround(percent)));
        return result;
    }
    if (command == "seek" || command == "seek_to") {
        const double value = message.double_value("seconds", 0.0);
        double target = command == "seek" ? current_s + value : value;
        target = std::max(0.0, target);
        if (duration_s > 0.0) {
            target = std::min(duration_s, target);
        }
        result.seek = true;
        result.target_s = target;
        emit_line("STATUS", "Seeking...");
        return result;
    }
    return result;
}

int run_ffmpeg_mode(const Args& args, bool* shutdown_requested) {
    PlaybackState state;
    state.gain = normalize_volume_percent(args.volume_percent);
    state.smooth_next_transition = args.smooth_transition;
    const bool apply_software_volume = !starts_with(args.device, "hw:");
    CommandReader commands;
    std::unique_ptr<AlsaPcm> pcm;
    Format current_fmt;
    bool have_fmt = false;
    double start_offset_s = 0.0;

    while (!state.stop) {
        std::string stderr_text;
        auto child = std::make_unique<ChildProcess>(build_ffmpeg_args(args, start_offset_s));
        WavFormat wav = parse_wav_header_fd(child->stdout_fd(), args.duration_s);
        set_nonblocking(child->stdout_fd());
        current_fmt = wav.format;
        have_fmt = true;

        if (pcm == nullptr) {
            emit_line("STATUS", "Opening ALSA device...");
            pcm = std::make_unique<AlsaPcm>(args.device, current_fmt);
        } else {
            pcm->drop_and_prepare();
        }
        Format output_fmt = pcm->applied_format();
        output_fmt.duration_s = current_fmt.duration_s;
        emit_format(output_fmt, current_fmt);
        emit_position_values(start_offset_s, args.duration_s);
        if (state.paused) {
            pcm->set_paused(true);
            child->send_signal(SIGSTOP);
        }
        emit_line("STATUS", state.paused ? "Paused" : "Playing");

        std::vector<std::uint8_t> pending;
        std::uint64_t bytes_written = 0;
        const std::size_t staging_reserve = stream_staging_reserve_bytes(wav);
        const std::size_t write_chunk = static_cast<std::size_t>(kStreamWriteChunkFrames) * static_cast<std::size_t>(wav.block_align);
        bool started = false;
        const sf_count_t expected_frames = stream_remaining_frames(args.duration_s, start_offset_s, current_fmt);
        auto last_position = std::chrono::steady_clock::now() - std::chrono::milliseconds(250);
        bool restart = false;
        bool eof = false;

        while (!state.stop && !restart) {
            const double current_s = stream_played_position_s(
                bytes_written,
                wav.block_align,
                start_offset_s,
                current_fmt,
                pcm ? pcm->get() : nullptr
            );
            for (const IpcMessage& message : commands.poll_messages()) {
                StreamCommandResult command = handle_stream_command(
                    message,
                    state,
                    *pcm,
                    *child,
                    current_fmt,
                    current_s,
                    args.duration_s,
                    apply_software_volume
                );
                if (command.seek) {
                    start_offset_s = command.target_s;
                    child->terminate();
                    restart = true;
                    break;
                }
                if (state.stop) {
                    break;
                }
            }
            if (commands.closed()) {
                state.stop = true;
                state.shutdown = true;
            }
            if (state.stop || restart) {
                break;
            }

            pollfd fds[2] = {};
            fds[0].fd = child->stdout_fd();
            fds[0].events = POLLIN | POLLHUP;
            fds[1].fd = child->stderr_fd();
            fds[1].events = POLLIN | POLLHUP;
            const int ready = poll(fds, 2, 10);
            if (ready < 0 && errno == EINTR) {
                continue;
            }
            if (ready < 0) {
                throw std::runtime_error("poll failed while reading ffmpeg");
            }

            if (fds[1].revents & (POLLIN | POLLHUP)) {
                drain_stderr(child->stderr_fd(), stderr_text);
            }

            if (!state.paused && pending.size() < staging_reserve && (fds[0].revents & (POLLIN | POLLHUP))) {
                std::uint8_t chunk[16384];
                while (pending.size() < staging_reserve) {
                    const std::size_t wanted = std::min(sizeof(chunk), staging_reserve - pending.size());
                    const ssize_t n = read(child->stdout_fd(), chunk, wanted);
                    if (n > 0) {
                        pending.insert(pending.end(), chunk, chunk + n);
                        continue;
                    }
                    if (n == 0) {
                        eof = true;
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    throw std::runtime_error("read failed while reading ffmpeg output");
                }
            }

            const std::size_t whole = wav.block_align > 0
                ? (pending.size() / static_cast<std::size_t>(wav.block_align)) * static_cast<std::size_t>(wav.block_align)
                : 0;
            if ((!started && whole >= staging_reserve) || eof) started = true;
            if (!state.paused && started && whole > 0) {
                const std::size_t write_size = std::min(whole, write_chunk);
                if (apply_software_volume) {
                    apply_volume_bytes(pending.data(), write_size, current_fmt.bits, state.gain);
                }
                remember_last_frame(state, pending.data(), write_size, current_fmt);
                write_frames(
                    pcm->get(),
                    pending.data(),
                    static_cast<sf_count_t>(write_size / static_cast<std::size_t>(wav.block_align)),
                    wav.block_align
                );
                bytes_written += write_size;
                pending.erase(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(write_size));
            }

            if (expected_frames > 0 && queued_stream_handoff_ready(state, current_fmt)) {
                const sf_count_t written_frames = stream_written_frames(bytes_written, wav.block_align);
                if (written_frames >= expected_frames) {
                    child->send_signal(SIGTERM);
                    eof = true;
                    break;
                }
            }

            const auto now = std::chrono::steady_clock::now();
            if (now - last_position >= std::chrono::milliseconds(250)) {
                const double pos_s = stream_played_position_s(
                    bytes_written,
                    wav.block_align,
                    start_offset_s,
                    current_fmt,
                    pcm ? pcm->get() : nullptr
                );
                emit_position_values(pos_s, args.duration_s);
                last_position = now;
            }

            if (eof && pending.empty()) {
                break;
            }
        }

        drain_stderr(child->stderr_fd(), stderr_text);
        if (restart) {
            continue;
        }
        if (state.stop) {
            break;
        }
        if (!eof && !stderr_text.empty()) {
            emit_line("LOG", stderr_text);
        }
        if (pcm != nullptr && have_fmt) {
            while (!state.stop && play_queued_flac(*pcm, current_fmt, state, apply_software_volume, commands)) {
            }
        }
        break;
    }

    if (pcm != nullptr) {
        if (state.stop) {
            snd_pcm_drop(pcm->get());
        } else {
            snd_pcm_drain(pcm->get());
            emit_position_values(current_fmt.duration_s, current_fmt.duration_s);
        }
    }
    emit_line("DONE", "");
    if (shutdown_requested != nullptr) {
        *shutdown_requested = state.shutdown;
    }
    return 0;
}

} // namespace tidal_native
