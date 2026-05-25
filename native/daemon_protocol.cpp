#include "native_player_internal.h"

#include <cctype>

namespace tidal_native {

namespace {

int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + c - 'a';
    if (c >= 'A' && c <= 'F') return 10 + c - 'A';
    return -1;
}

std::string encode_field_value(const std::string& value) {
    constexpr char digits[] = "0123456789ABCDEF";
    std::string out;
    for (const unsigned char c : value) {
        if (c == '%' || c == '=' || c == '\n' || c == '\r' || c < 0x20 || c > 0x7e) {
            out.push_back('%');
            out.push_back(digits[(c >> 4) & 0x0f]);
            out.push_back(digits[c & 0x0f]);
        } else {
            out.push_back(static_cast<char>(c));
        }
    }
    return out;
}

std::string decode_field_value(const std::string& value) {
    std::string out;
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '%' && i + 2 < value.size()) {
            const int hi = hex_value(value[i + 1]);
            const int lo = hex_value(value[i + 2]);
            if (hi >= 0 && lo >= 0) {
                out.push_back(static_cast<char>((hi << 4) | lo));
                i += 2;
                continue;
            }
        }
        out.push_back(value[i]);
    }
    return out;
}

std::string encode_payload(const IpcMessage& message) {
    std::string payload = message.type;
    payload.push_back('\n');
    for (const auto& field : message.fields) {
        payload += field.first;
        payload.push_back('=');
        payload += encode_field_value(field.second);
        payload.push_back('\n');
    }
    return payload;
}

IpcMessage parse_payload(const std::string& payload) {
    IpcMessage message;
    const std::size_t first_newline = payload.find('\n');
    if (first_newline == std::string::npos) {
        message.type = payload;
        return message;
    }
    message.type = payload.substr(0, first_newline);
    std::size_t start = first_newline + 1;
    while (start < payload.size()) {
        const std::size_t end = payload.find('\n', start);
        const std::string line = payload.substr(start, end == std::string::npos ? std::string::npos : end - start);
        if (!line.empty()) {
            const std::size_t sep = line.find('=');
            if (sep != std::string::npos) {
                message.fields.push_back({line.substr(0, sep), decode_field_value(line.substr(sep + 1))});
            }
        }
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return message;
}

bool try_take_message(std::string& buffer, IpcMessage& message) {
    const std::size_t header_end = buffer.find('\n');
    if (header_end == std::string::npos) return false;
    const std::string header = buffer.substr(0, header_end);
    if (header.empty() || !std::all_of(header.begin(), header.end(), [](unsigned char c) { return std::isdigit(c); })) {
        buffer.erase(0, header_end + 1);
        message = {};
        return true;
    }
    const auto payload_size = static_cast<std::size_t>(std::stoull(header));
    const std::size_t frame_start = header_end + 1;
    if (buffer.size() < frame_start + payload_size) return false;
    message = parse_payload(buffer.substr(frame_start, payload_size));
    buffer.erase(0, frame_start + payload_size);
    return true;
}

} // namespace

std::string IpcMessage::value(const std::string& key, const std::string& fallback) const {
    for (const auto& field : fields) {
        if (field.first == key) return field.second;
    }
    return fallback;
}

int IpcMessage::int_value(const std::string& key, int fallback) const {
    const std::string raw = value(key);
    if (raw.empty()) return fallback;
    return std::stoi(raw);
}

double IpcMessage::double_value(const std::string& key, double fallback) const {
    const std::string raw = value(key);
    if (raw.empty()) return fallback;
    return std::stod(raw);
}

bool IpcMessage::bool_value(const std::string& key, bool fallback) const {
    const std::string raw = value(key);
    if (raw.empty()) return fallback;
    return raw == "1" || raw == "true";
}

CommandReader::CommandReader() {
    const int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);
    }
}

std::vector<IpcMessage> CommandReader::poll_messages() {
    std::vector<IpcMessage> messages;
    if (closed_) {
        return messages;
    }
    pollfd fd{};
    fd.fd = STDIN_FILENO;
    fd.events = POLLIN | POLLHUP;
    const int ready = poll(&fd, 1, 0);
    if (ready <= 0) {
        return messages;
    }
    if (fd.revents & (POLLERR | POLLNVAL)) {
        closed_ = true;
        return messages;
    }
    if ((fd.revents & POLLIN) == 0) {
        if (fd.revents & POLLHUP) closed_ = true;
        return messages;
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

    IpcMessage message;
    while (try_take_message(buffer_, message)) {
        messages.push_back(message);
    }
    return messages;
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
    if (payload.empty()) {
        emit_message(kind);
    } else {
        emit_message(kind, {{"message", payload}});
    }
}

void emit_message(const std::string& type, std::initializer_list<std::pair<std::string, std::string>> fields) {
    const std::string payload = encode_payload(IpcMessage{type, std::vector<std::pair<std::string, std::string>>(fields)});
    std::cout << payload.size() << '\n' << payload << std::flush;
}

bool read_ipc_message_blocking(IpcMessage& message) {
    std::string header;
    char ch = '\0';
    while (true) {
        const ssize_t n = read(STDIN_FILENO, &ch, 1);
        if (n > 0) {
            if (ch == '\n') break;
            header.push_back(ch);
            continue;
        }
        if (n == 0) return false;
        if (errno == EINTR) continue;
        return false;
    }
    if (header.empty() || !std::all_of(header.begin(), header.end(), [](unsigned char c) { return std::isdigit(c); })) {
        message = {};
        return true;
    }
    const auto payload_size = static_cast<std::size_t>(std::stoull(header));
    std::string payload(payload_size, '\0');
    if (payload_size > 0 && !read_exact_fd(STDIN_FILENO, reinterpret_cast<std::uint8_t*>(payload.data()), payload_size)) {
        return false;
    }
    message = parse_payload(payload);
    return true;
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
        } else if (key == "--smooth-transition") {
            args.smooth_transition = true;
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
                << "  --protocol-whitelist              ffmpeg DASH/MPD mode only\n"
                << "  --smooth-transition               ffmpeg mode queued handoff de-click\n";
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

int run(int argc, char** argv) {
    const Args args = parse_args(argc, argv);
    if (args.daemon) {
        emit_line("READY", "");
        while (true) {
            set_blocking(STDIN_FILENO);
            IpcMessage message;
            if (!read_ipc_message_blocking(message)) {
                break;
            }
            if (message.type == "shutdown" || message.type == "quit") {
                emit_line("BYE", "");
                return 0;
            }
            if (message.type.empty()) {
                continue;
            }
            try {
                if (message.type == "clear_next" || message.type == "next" || message.type == "set_volume" ||
                    message.type == "pause_toggle" || message.type == "stop" || message.type == "seek" || message.type == "seek_to") {
                    emit_line("LOG", "ignored idle native control command");
                    continue;
                }
                if (message.type == "play_file") {
                    Args play_args;
                    play_args.file = message.value("path");
                    play_args.device = message.value("device");
                    play_args.volume_percent = message.int_value("volume", 100);
                    if (play_args.file.empty() || play_args.device.empty()) {
                        throw std::runtime_error("play_file requires path and device");
                    }
                    bool shutdown_requested = false;
                    run_file_mode(play_args, &shutdown_requested);
                    if (shutdown_requested) {
                        emit_line("BYE", "");
                        return 0;
                    }
                    continue;
                }
                if (message.type == "play_ffmpeg") {
                    Args play_args;
                    play_args.ffmpeg_input = message.value("input");
                    play_args.device = message.value("device");
                    play_args.volume_percent = message.int_value("volume", 100);
                    play_args.codec = message.value("codec", "pcm_s16le");
                    play_args.duration_s = message.double_value("duration", 0.0);
                    play_args.protocol_whitelist = message.bool_value("protocol", false);
                    play_args.smooth_transition = message.bool_value("smooth_transition", false);
                    if (play_args.ffmpeg_input.empty() || play_args.device.empty()) {
                        throw std::runtime_error("play_ffmpeg requires input and device");
                    }
                    bool shutdown_requested = false;
                    run_ffmpeg_mode(play_args, &shutdown_requested);
                    if (shutdown_requested) {
                        emit_line("BYE", "");
                        return 0;
                    }
                    continue;
                }
                throw std::runtime_error("unknown daemon command: " + message.type);
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
