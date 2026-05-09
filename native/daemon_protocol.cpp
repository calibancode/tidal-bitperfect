#include "native_player_internal.h"

namespace tidal_native {

CommandReader::CommandReader() {
    const int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);
    }
}

std::vector<std::string> CommandReader::poll_lines() {
    std::vector<std::string> lines;
    if (closed_) {
        return lines;
    }
    pollfd fd{};
    fd.fd = STDIN_FILENO;
    fd.events = POLLIN | POLLHUP;
    const int ready = poll(&fd, 1, 0);
    if (ready <= 0) {
        return lines;
    }
    if (fd.revents & (POLLERR | POLLNVAL)) {
        closed_ = true;
        return lines;
    }
    if ((fd.revents & POLLIN) == 0) {
        if (fd.revents & POLLHUP) closed_ = true;
        return lines;
    }

    char chunk[512];
    while (true) {
        const ssize_t n = read(STDIN_FILENO, chunk, sizeof(chunk));
        if (n > 0) {
            buffer_.append(chunk, static_cast<std::size_t>(n));
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            break;
        }
        if (n == 0) {
            closed_ = true;
            break;
        }
        closed_ = true;
        break;
    }

    std::size_t pos = 0;
    while (true) {
        const std::size_t newline = buffer_.find('\n', pos);
        if (newline == std::string::npos) {
            buffer_.erase(0, pos);
            break;
        }
        lines.push_back(buffer_.substr(pos, newline - pos));
        pos = newline + 1;
    }
    return lines;
}

bool CommandReader::closed() const {
    return closed_;
}

bool starts_with(const std::string& text, const std::string& prefix) {
    return text.rfind(prefix, 0) == 0;
}

std::string format_seconds_arg(double seconds) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(3) << seconds;
    return out.str();
}

void set_nonblocking(int fd) {
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
}

void set_blocking(int fd) {
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
    }
}

void emit_line(const std::string& kind, const std::string& payload) {
    std::cout << kind;
    if (!payload.empty()) {
        std::cout << ' ' << payload;
    }
    std::cout << '\n' << std::flush;
}

std::string alsa_error(const std::string& context, int err) {
    std::ostringstream out;
    out << context << ": " << snd_strerror(err);
    return out.str();
}

double normalize_volume_percent(int percent) {
    const double normalized = std::clamp(percent, 0, 100) / 100.0;
    return normalized * normalized;
}

void set_pipewire_alsa_metadata_if_needed(const std::string& device) {
    if (starts_with(device, "hw:")) {
        return;
    }
    const char* value =
        "{ application.name = \"TIDAL Bitperfect\" "
        "node.name = \"tidal-bitperfect\" "
        "node.nick = \"TIDAL\" "
        "node.description = \"TIDAL Bitperfect Playback\" "
        "media.name = \"TIDAL Bitperfect\" "
        "media.software = \"TIDAL Bitperfect\" "
        "media.role = \"Music\" }";
    setenv("PIPEWIRE_ALSA", value, 1);
}

Args parse_args(int argc, char** argv) {
    Args args;
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        auto require_value = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                throw std::runtime_error(std::string("missing value for ") + name);
            }
            return argv[++i];
        };

        if (key == "--file") {
            args.file = require_value("--file");
        } else if (key == "--daemon") {
            args.daemon = true;
        } else if (key == "--ffmpeg-input") {
            args.ffmpeg_input = require_value("--ffmpeg-input");
        } else if (key == "--device") {
            args.device = require_value("--device");
        } else if (key == "--codec") {
            args.codec = require_value("--codec");
        } else if (key == "--duration") {
            args.duration_s = std::stod(require_value("--duration"));
        } else if (key == "--protocol-whitelist") {
            args.protocol_whitelist = true;
        } else if (key == "--volume") {
            args.volume_percent = std::stoi(require_value("--volume"));
        } else if (key == "--help" || key == "-h") {
            std::cout
                << "usage: tidal-native-player (--file PATH | --ffmpeg-input INPUT) --device ALSA_PCM [options]\n"
                << "       tidal-native-player --daemon\n"
                << "options:\n"
                << "  --volume 0-100\n"
                << "  --codec pcm_s16le|pcm_s32le       ffmpeg mode only\n"
                << "  --duration SECONDS                ffmpeg mode only\n"
                << "  --protocol-whitelist              ffmpeg DASH/MPD mode only\n";
            std::exit(0);
        } else {
            throw std::runtime_error("unknown argument: " + key);
        }
    }

    if (args.daemon) {
        return args;
    }

    if (args.file.empty() == args.ffmpeg_input.empty()) {
        throw std::runtime_error("exactly one of --file or --ffmpeg-input is required");
    }
    if (args.device.empty()) {
        throw std::runtime_error("--device is required");
    }
    return args;
}

std::vector<std::string> split_tabs(const std::string& line) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (start <= line.size()) {
        const std::size_t pos = line.find('\t', start);
        if (pos == std::string::npos) {
            out.push_back(line.substr(start));
            break;
        }
        out.push_back(line.substr(start, pos - start));
        start = pos + 1;
    }
    return out;
}

int run(int argc, char** argv) {
    const Args args = parse_args(argc, argv);
    if (args.daemon) {
        emit_line("READY", "");
        std::string line;
        while (true) {
            set_blocking(STDIN_FILENO);
            if (!std::getline(std::cin, line)) {
                break;
            }
            if (line == "shutdown" || line == "quit") {
                emit_line("BYE", "");
                return 0;
            }
            if (line.empty()) {
                continue;
            }
            std::vector<std::string> parts = split_tabs(line);
            try {
                std::string command = parts.empty() ? std::string() : parts[0];
                const std::size_t command_space = command.find(' ');
                if (command_space != std::string::npos) {
                    command = command.substr(0, command_space);
                }
                if (
                    command == "clear_next" || command == "next" || command == "set_volume" ||
                    command == "pause_toggle" || command == "stop" || command == "seek" || command == "seek_to"
                ) {
                    emit_line("LOG", "ignored idle native control command");
                    continue;
                }
                if (!parts.empty() && parts[0] == "play_file") {
                    if (parts.size() < 4) {
                        throw std::runtime_error("play_file requires path, device, volume");
                    }
                    Args play_args;
                    play_args.file = parts[1];
                    play_args.device = parts[2];
                    play_args.volume_percent = std::stoi(parts[3]);
                    bool shutdown_requested = false;
                    run_file_mode(play_args, &shutdown_requested);
                    if (shutdown_requested) {
                        emit_line("BYE", "");
                        return 0;
                    }
                    continue;
                }
                if (!parts.empty() && parts[0] == "play_ffmpeg") {
                    if (parts.size() < 7) {
                        throw std::runtime_error(
                            "play_ffmpeg requires input, device, volume, codec, duration, protocol flag"
                        );
                    }
                    Args play_args;
                    play_args.ffmpeg_input = parts[1];
                    play_args.device = parts[2];
                    play_args.volume_percent = std::stoi(parts[3]);
                    play_args.codec = parts[4];
                    play_args.duration_s = std::stod(parts[5]);
                    play_args.protocol_whitelist = parts[6] == "1" || parts[6] == "true";
                    bool shutdown_requested = false;
                    run_ffmpeg_mode(play_args, &shutdown_requested);
                    if (shutdown_requested) {
                        emit_line("BYE", "");
                        return 0;
                    }
                    continue;
                }
                throw std::runtime_error("unknown daemon command: " + parts[0]);
            } catch (const std::exception& exc) {
                emit_line("ERROR", exc.what());
                emit_line("DONE", "");
            }
        }
        return 0;
    }
    if (args.use_ffmpeg()) {
        return run_ffmpeg_mode(args);
    }
    return run_file_mode(args);
}

} // namespace tidal_native
