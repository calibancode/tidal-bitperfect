#include "native_player_internal.h"

namespace tidal_native {

ChildProcess::ChildProcess(const std::vector<std::string>& args) {
    int out_pipe[2] = {-1, -1};
    int err_pipe[2] = {-1, -1};
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
        throw std::runtime_error("pipe failed");
    }

    pid_ = fork();
    if (pid_ < 0) {
        close(out_pipe[0]);
        close(out_pipe[1]);
        close(err_pipe[0]);
        close(err_pipe[1]);
        throw std::runtime_error("fork failed");
    }

    if (pid_ == 0) {
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(err_pipe[1], STDERR_FILENO);
        close(out_pipe[0]);
        close(out_pipe[1]);
        close(err_pipe[0]);
        close(err_pipe[1]);

        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (const auto& arg : args) {
            argv.push_back(const_cast<char*>(arg.c_str()));
        }
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    close(out_pipe[1]);
    close(err_pipe[1]);
    stdout_fd_ = out_pipe[0];
    stderr_fd_ = err_pipe[0];
    set_nonblocking(stderr_fd_);
}

ChildProcess::~ChildProcess() {
    terminate();
    close_fd(stdout_fd_);
    close_fd(stderr_fd_);
}

int ChildProcess::stdout_fd() const {
    return stdout_fd_;
}

int ChildProcess::stderr_fd() const {
    return stderr_fd_;
}

void ChildProcess::send_signal(int sig) {
    if (pid_ > 0) {
        kill(pid_, sig);
    }
}

bool ChildProcess::is_running() {
    if (pid_ <= 0) {
        return false;
    }
    int status = 0;
    const pid_t got = waitpid(pid_, &status, WNOHANG);
    if (got == 0) {
        return true;
    }
    if (got == pid_) {
        pid_ = -1;
        return false;
    }
    return errno == EINTR;
}

void ChildProcess::terminate() {
    if (pid_ <= 0) {
        return;
    }
    kill(pid_, SIGTERM);
    for (int i = 0; i < 20; ++i) {
        if (!is_running()) {
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    kill(pid_, SIGKILL);
    int status = 0;
    while (waitpid(pid_, &status, 0) < 0 && errno == EINTR) {
    }
    pid_ = -1;
}

void ChildProcess::close_fd(int& fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

std::vector<std::string> build_ffmpeg_args(const Args& args, double start_s) {
    std::vector<std::string> cmd = {"ffmpeg", "-hide_banner", "-loglevel", "error"};
    if (start_s > 0.0) {
        cmd.push_back("-ss");
        cmd.push_back(format_seconds_arg(start_s));
    }
    if (args.protocol_whitelist) {
        cmd.push_back("-protocol_whitelist");
        cmd.push_back("file,https,tls,tcp,crypto");
    }
    cmd.push_back("-i");
    cmd.push_back(args.ffmpeg_input);
    cmd.push_back("-c:a");
    cmd.push_back(args.codec.empty() ? "pcm_s16le" : args.codec);
    cmd.push_back("-f");
    cmd.push_back("wav");
    cmd.push_back("pipe:1");
    return cmd;
}

bool read_exact_fd(int fd, std::uint8_t* dest, std::size_t wanted) {
    std::size_t total = 0;
    while (total < wanted) {
        const ssize_t n = read(fd, dest + total, wanted - total);
        if (n > 0) {
            total += static_cast<std::size_t>(n);
            continue;
        }
        if (n == 0) {
            return false;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
        }
        return false;
    }
    return true;
}

std::uint16_t le16(const std::uint8_t* p) {
    return static_cast<std::uint16_t>(p[0] | (p[1] << 8));
}

std::uint32_t le32(const std::uint8_t* p) {
    return static_cast<std::uint32_t>(p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24));
}

void skip_exact_fd(int fd, std::uint32_t wanted) {
    std::vector<std::uint8_t> buffer(4096);
    std::uint32_t remaining = wanted;
    while (remaining > 0) {
        const std::size_t chunk = std::min<std::size_t>(buffer.size(), remaining);
        if (!read_exact_fd(fd, buffer.data(), chunk)) {
            throw std::runtime_error("short read while skipping WAV chunk");
        }
        remaining -= static_cast<std::uint32_t>(chunk);
    }
    if (wanted % 2 == 1) {
        std::uint8_t pad = 0;
        read_exact_fd(fd, &pad, 1);
    }
}

WavFormat parse_wav_header_fd(int fd, double duration_s) {
    std::uint8_t hdr[12];
    if (!read_exact_fd(fd, hdr, sizeof(hdr))) {
        throw std::runtime_error("short read on WAV header");
    }
    if (std::memcmp(hdr, "RIFF", 4) != 0 || std::memcmp(hdr + 8, "WAVE", 4) != 0) {
        throw std::runtime_error("not a RIFF/WAVE stream");
    }

    int channels = 0;
    int rate = 0;
    int bits = 0;
    int block_align = 0;

    while (true) {
        std::uint8_t chunk[8];
        if (!read_exact_fd(fd, chunk, sizeof(chunk))) {
            throw std::runtime_error("could not find fmt/data chunks in WAV stream");
        }
        const std::uint32_t chunk_size = le32(chunk + 4);
        if (std::memcmp(chunk, "fmt ", 4) == 0) {
            std::vector<std::uint8_t> data(chunk_size);
            if (!read_exact_fd(fd, data.data(), data.size())) {
                throw std::runtime_error("short read in WAV fmt chunk");
            }
            if (chunk_size % 2 == 1) {
                std::uint8_t pad = 0;
                read_exact_fd(fd, &pad, 1);
            }
            if (chunk_size < 16) {
                throw std::runtime_error("invalid WAV fmt chunk");
            }
            std::uint16_t audio_fmt = le16(data.data());
            channels = static_cast<int>(le16(data.data() + 2));
            rate = static_cast<int>(le32(data.data() + 4));
            block_align = static_cast<int>(le16(data.data() + 12));
            bits = static_cast<int>(le16(data.data() + 14));
            if (audio_fmt == 65534 && chunk_size >= 40) {
                const auto subformat = le32(data.data() + 24);
                if (subformat == 1) {
                    audio_fmt = 1;
                } else if (subformat == 3) {
                    throw std::runtime_error("unsupported WAV subformat IEEE_FLOAT");
                }
            } else if (audio_fmt == 65534) {
                audio_fmt = 1;
            }
            if (audio_fmt != 1) {
                throw std::runtime_error("unsupported WAV audio format");
            }
            if (bits != 16 && bits != 24 && bits != 32) {
                if (bits <= 16) {
                    bits = 16;
                } else if (bits <= 24) {
                    bits = 24;
                } else if (bits <= 32) {
                    bits = 32;
                } else {
                    throw std::runtime_error("unsupported WAV bit depth");
                }
            }
        } else if (std::memcmp(chunk, "data", 4) == 0) {
            if (channels <= 0 || rate <= 0 || bits <= 0 || block_align <= 0) {
                throw std::runtime_error("WAV data chunk arrived before valid fmt chunk");
            }
            WavFormat out;
            out.format.channels = channels;
            out.format.rate = rate;
            out.format.bits = bits;
            out.format.duration_s = duration_s;
            out.block_align = block_align;
            return out;
        } else {
            skip_exact_fd(fd, chunk_size);
        }
    }
}

void drain_stderr(int fd, std::string& stderr_text) {
    if (fd < 0) {
        return;
    }
    std::uint8_t buffer[4096];
    while (true) {
        const ssize_t n = read(fd, buffer, sizeof(buffer));
        if (n > 0) {
            stderr_text.append(reinterpret_cast<const char*>(buffer), static_cast<std::size_t>(n));
            continue;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;
        }
        return;
    }
}

} // namespace tidal_native
