#include "native_player_internal.h"

namespace tidal_native {

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

bool handle_next_command_line(const std::string& line, PlaybackState& state) {
    if (line == "clear_next") {
        state.next_id.clear();
        state.next_path.clear();
        emit_line("LOG", "cleared native next track");
        return true;
    }
    if (line.rfind("next\t", 0) != 0) {
        return false;
    }
    const std::size_t id_start = 5;
    const std::size_t path_start = line.find('\t', id_start);
    if (path_start != std::string::npos && path_start + 1 < line.size()) {
        state.next_id = line.substr(id_start, path_start - id_start);
        state.next_path = line.substr(path_start + 1);
        emit_line("LOG", "queued native next track " + state.next_id);
    }
    return true;
}

void handle_command(
    const std::string& line,
    PlaybackState& state,
    SNDFILE* file,
    const SF_INFO& info,
    const Format& fmt,
    AlsaPcm& pcm,
    bool apply_software_volume
) {
    if (handle_next_command_line(line, state)) {
        return;
    }

    std::istringstream in(line);
    std::string command;
    in >> command;
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
        double percent = 100.0;
        in >> percent;
        const double new_gain = normalize_volume_percent(static_cast<int>(std::llround(percent)));
        const bool changed = std::abs(new_gain - state.gain) > 0.0001;
        state.gain = new_gain;
        if (changed && apply_software_volume && alsa_delay_seconds(fmt, pcm.get()) > kVolumeFlushDelaySeconds) {
            const sf_count_t current = sf_seek(file, 0, SEEK_CUR);
            const sf_count_t target = std::max<sf_count_t>(0, current - alsa_delay_frames(pcm.get()));
            seek_to_frame(file, pcm, fmt, target);
        }
        return;
    }
    if (command == "seek" || command == "seek_to") {
        double value = 0.0;
        in >> value;
        const sf_count_t current = sf_seek(file, 0, SEEK_CUR);
        const double current_s = current >= 0 ? static_cast<double>(current) / static_cast<double>(info.samplerate) : 0.0;
        const double target_s = command == "seek" ? current_s + value : value;
        emit_line("STATUS", "Seeking...");
        seek_to_frame(file, pcm, fmt, clamp_frame(target_s, info));
        emit_line("STATUS", state.paused ? "Paused" : "Playing");
    }
}

template <typename Sample>
void playback_loop(SoundFile& input, AlsaPcm& pcm, const Format& fmt, PlaybackState& state, bool apply_software_volume) {
    constexpr sf_count_t chunk_frames = 4096;
    std::vector<Sample> buffer(static_cast<std::size_t>(chunk_frames) * static_cast<std::size_t>(fmt.channels));
    CommandReader commands;
    auto last_position = std::chrono::steady_clock::now() - std::chrono::milliseconds(250);

    while (!state.stop) {
        for (const std::string& line : commands.poll_lines()) {
            handle_command(line, state, input.get(), input.info(), fmt, pcm, apply_software_volume);
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
            ? sf_readf_short(input.get(), reinterpret_cast<short*>(buffer.data()), chunk_frames)
            : sf_readf_int(input.get(), reinterpret_cast<int*>(buffer.data()), chunk_frames);
        if (frames < 0) {
            throw std::runtime_error(std::string("FLAC read failed: ") + sf_strerror(input.get()));
        }
        if (frames == 0) {
            break;
        }

        if (apply_software_volume) {
            apply_volume(buffer, frames, fmt.channels, state.gain);
        }
        write_frames(pcm.get(), buffer.data(), frames, fmt.channels * (fmt.bits / 8));

        const auto now = std::chrono::steady_clock::now();
        if (now - last_position >= std::chrono::milliseconds(250)) {
                emit_position(input.get(), fmt, pcm.get());
            last_position = now;
        }
    }
}

bool play_queued_flac(AlsaPcm& pcm, Format& current_fmt, PlaybackState& state, bool apply_software_volume) {
    if (state.stop || state.next_path.empty()) {
        return false;
    }

    const std::string next_id = state.next_id;
    const std::string next_path = state.next_path;
    state.next_id.clear();
    state.next_path.clear();

    auto next_input = std::make_unique<SoundFile>(next_path);
    const Format next_fmt = detect_format(next_input->info());
    if (!same_pcm_format(current_fmt, next_fmt)) {
        emit_line(
            "LOG",
            "native next format mismatch: " + format_summary(current_fmt) + " -> " + format_summary(next_fmt)
        );
        return false;
    }

    current_fmt = next_fmt;
    emit_line("ADVANCED", next_id);
    emit_format(current_fmt);
    emit_line("STATUS", state.paused ? "Paused" : "Playing");

    if (current_fmt.bits == 16) {
        playback_loop<short>(*next_input, pcm, current_fmt, state, apply_software_volume);
    } else {
        playback_loop<int>(*next_input, pcm, current_fmt, state, apply_software_volume);
    }
    return true;
}


int run_file_mode(const Args& args, bool* shutdown_requested) {
    auto input = std::make_unique<SoundFile>(args.file);
    Format fmt = detect_format(input->info());
    emit_format(fmt);

    PlaybackState state;
    state.gain = normalize_volume_percent(args.volume_percent);
    const bool apply_software_volume = !starts_with(args.device, "hw:");

    emit_line("STATUS", "Opening ALSA device...");
    AlsaPcm pcm(args.device, fmt);
    emit_line("STATUS", "Playing");

    if (fmt.bits == 16) {
        playback_loop<short>(*input, pcm, fmt, state, apply_software_volume);
    } else {
        playback_loop<int>(*input, pcm, fmt, state, apply_software_volume);
    }
    while (!state.stop && play_queued_flac(pcm, fmt, state, apply_software_volume)) {
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
    const std::string& line,
    PlaybackState& state,
    AlsaPcm& pcm,
    ChildProcess& child,
    const Format& fmt,
    double current_s,
    double duration_s,
    bool apply_software_volume
) {
    StreamCommandResult result;
    if (handle_next_command_line(line, state)) {
        return result;
    }

    std::istringstream in(line);
    std::string command;
    in >> command;
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
        double percent = 100.0;
        in >> percent;
        const double new_gain = normalize_volume_percent(static_cast<int>(std::llround(percent)));
        const bool changed = std::abs(new_gain - state.gain) > 0.0001;
        state.gain = new_gain;
        if (changed && apply_software_volume && alsa_delay_seconds(fmt, pcm.get()) > kVolumeFlushDelaySeconds) {
            result.seek = true;
            result.target_s = current_s;
        }
        return result;
    }
    if (command == "seek" || command == "seek_to") {
        double value = 0.0;
        in >> value;
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
        current_fmt = wav.format;
        have_fmt = true;
        emit_format(current_fmt);
        emit_position_values(start_offset_s, args.duration_s);

        if (pcm == nullptr) {
            emit_line("STATUS", "Opening ALSA device...");
            pcm = std::make_unique<AlsaPcm>(args.device, current_fmt);
        } else {
            pcm->drop_and_prepare();
        }
        if (state.paused) {
            pcm->set_paused(true);
            child->send_signal(SIGSTOP);
        }
        emit_line("STATUS", state.paused ? "Paused" : "Playing");

        std::vector<std::uint8_t> pending;
        std::uint64_t bytes_written = 0;
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
            for (const std::string& line : commands.poll_lines()) {
                StreamCommandResult command = handle_stream_command(
                    line,
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
            const int ready = poll(fds, 2, 50);
            if (ready < 0 && errno == EINTR) {
                continue;
            }
            if (ready < 0) {
                throw std::runtime_error("poll failed while reading ffmpeg");
            }

            if (fds[1].revents & (POLLIN | POLLHUP)) {
                drain_stderr(child->stderr_fd(), stderr_text);
            }

            if (!state.paused && (fds[0].revents & (POLLIN | POLLHUP))) {
                std::uint8_t chunk[16384];
                const ssize_t n = read(child->stdout_fd(), chunk, sizeof(chunk));
                if (n > 0) {
                    pending.insert(pending.end(), chunk, chunk + n);
                } else if (n == 0) {
                    eof = true;
                } else if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) {
                    throw std::runtime_error("read failed while reading ffmpeg output");
                }
            }

            const std::size_t whole = wav.block_align > 0
                ? (pending.size() / static_cast<std::size_t>(wav.block_align)) * static_cast<std::size_t>(wav.block_align)
                : 0;
            if (whole > 0) {
                if (apply_software_volume) {
                    apply_volume_bytes(pending.data(), whole, current_fmt.bits, state.gain);
                }
                write_frames(
                    pcm->get(),
                    pending.data(),
                    static_cast<sf_count_t>(whole / static_cast<std::size_t>(wav.block_align)),
                    wav.block_align
                );
                bytes_written += whole;
                pending.erase(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(whole));
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

            if (eof) {
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
            while (!state.stop && play_queued_flac(*pcm, current_fmt, state, apply_software_volume)) {
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
