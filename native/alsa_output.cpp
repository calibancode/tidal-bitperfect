#include "native_player_internal.h"

namespace tidal_native {

namespace {

int pcm_format_bits(snd_pcm_format_t format) {
    int bits = snd_pcm_format_physical_width(format);
    if (bits <= 0) bits = snd_pcm_format_width(format);
    return std::max(0, bits);
}

std::string format_number(double value) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(3) << value;
    return out.str();
}

} // namespace

SoundFile::SoundFile(const std::string& path) {
    std::memset(&info_, 0, sizeof(info_));
    file_ = sf_open(path.c_str(), SFM_READ, &info_);
    if (file_ == nullptr) {
        throw std::runtime_error(std::string("open FLAC failed: ") + sf_strerror(nullptr));
    }
}

SoundFile::~SoundFile() {
    if (file_ != nullptr) {
        sf_close(file_);
    }
}

SNDFILE* SoundFile::get() const {
    return file_;
}

const SF_INFO& SoundFile::info() const {
    return info_;
}

AlsaPcm::AlsaPcm(const std::string& device, const Format& fmt) {
    set_pipewire_alsa_metadata_if_needed(device);
    int err = snd_pcm_open(&pcm_, device.c_str(), SND_PCM_STREAM_PLAYBACK, 0);
    if (err < 0) {
        throw std::runtime_error(alsa_error("ALSA open failed", err));
    }

    snd_pcm_hw_params_t* params = nullptr;
    snd_pcm_hw_params_alloca(&params);

    if ((err = snd_pcm_hw_params_any(pcm_, params)) < 0) {
        throw std::runtime_error(alsa_error("ALSA params init failed", err));
    }
    if ((err = snd_pcm_hw_params_set_access(pcm_, params, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0) {
        throw std::runtime_error(alsa_error("ALSA access setup failed", err));
    }

    const snd_pcm_format_t pcm_format = fmt.bits == 16 ? SND_PCM_FORMAT_S16_LE : SND_PCM_FORMAT_S32_LE;
    if ((err = snd_pcm_hw_params_set_format(pcm_, params, pcm_format)) < 0) {
        throw std::runtime_error(alsa_error("ALSA format setup failed", err));
    }
    if ((err = snd_pcm_hw_params_set_channels(pcm_, params, static_cast<unsigned int>(fmt.channels))) < 0) {
        throw std::runtime_error(alsa_error("ALSA channel setup failed", err));
    }

    unsigned int rate = static_cast<unsigned int>(fmt.rate);
    int dir = 0;
    if ((err = snd_pcm_hw_params_set_rate_near(pcm_, params, &rate, &dir)) < 0) {
        throw std::runtime_error(alsa_error("ALSA rate setup failed", err));
    }
    if (rate != static_cast<unsigned int>(fmt.rate)) {
        std::ostringstream out;
        out << "ALSA rejected exact sample rate " << fmt.rate << "Hz (nearest " << rate << "Hz)";
        throw std::runtime_error(out.str());
    }

    unsigned int buffer_time_us = 250000;
    snd_pcm_hw_params_set_buffer_time_near(pcm_, params, &buffer_time_us, &dir);
    unsigned int period_time_us = 50000;
    snd_pcm_hw_params_set_period_time_near(pcm_, params, &period_time_us, &dir);

    if ((err = snd_pcm_hw_params(pcm_, params)) < 0) {
        throw std::runtime_error(alsa_error("ALSA params apply failed", err));
    }

    applied_format_ = fmt;
    unsigned int actual_channels = 0;
    if (snd_pcm_hw_params_get_channels(params, &actual_channels) == 0) {
        applied_format_.channels = static_cast<int>(actual_channels);
    }
    unsigned int actual_rate = 0;
    int actual_dir = 0;
    if (snd_pcm_hw_params_get_rate(params, &actual_rate, &actual_dir) == 0) {
        applied_format_.rate = static_cast<int>(actual_rate);
    }
    snd_pcm_format_t actual_format = SND_PCM_FORMAT_UNKNOWN;
    if (snd_pcm_hw_params_get_format(params, &actual_format) == 0) {
        const int actual_bits = pcm_format_bits(actual_format);
        if (actual_bits > 0) applied_format_.bits = actual_bits;
    }

    snd_pcm_sw_params_t* sw_params = nullptr;
    snd_pcm_sw_params_alloca(&sw_params);
    if ((err = snd_pcm_sw_params_current(pcm_, sw_params)) >= 0) {
        snd_pcm_uframes_t period_size = 0;
        snd_pcm_uframes_t buffer_size = 0;
        snd_pcm_hw_params_get_period_size(params, &period_size, nullptr);
        snd_pcm_hw_params_get_buffer_size(params, &buffer_size);
        if (period_size > 0 && buffer_size > 0) {
            snd_pcm_sw_params_set_avail_min(pcm_, sw_params, period_size);
            snd_pcm_sw_params_set_start_threshold(pcm_, sw_params, std::min(period_size, buffer_size));
        }
        snd_pcm_sw_params(pcm_, sw_params);
    }
    if ((err = snd_pcm_prepare(pcm_)) < 0) {
        throw std::runtime_error(alsa_error("ALSA prepare failed", err));
    }
}

AlsaPcm::~AlsaPcm() {
    if (pcm_ != nullptr) {
        snd_pcm_close(pcm_);
    }
}

snd_pcm_t* AlsaPcm::get() const {
    return pcm_;
}

const Format& AlsaPcm::applied_format() const {
    return applied_format_;
}

void AlsaPcm::drop_and_prepare() {
    if (pcm_ == nullptr) {
        return;
    }
    snd_pcm_drop(pcm_);
    const int err = snd_pcm_prepare(pcm_);
    if (err < 0) {
        throw std::runtime_error(alsa_error("ALSA seek prepare failed", err));
    }
}

void AlsaPcm::set_paused(bool paused) {
    if (pcm_ == nullptr) {
        return;
    }
    const int err = snd_pcm_pause(pcm_, paused ? 1 : 0);
    if (err < 0 && err != -ENOSYS) {
        emit_line("LOG", alsa_error("ALSA pause unsupported", err));
    }
}

Format detect_format(const SF_INFO& info) {
    if ((info.format & SF_FORMAT_TYPEMASK) != SF_FORMAT_FLAC) {
        throw std::runtime_error("not a FLAC file");
    }
    if (info.channels <= 0 || info.samplerate <= 0) {
        throw std::runtime_error("invalid FLAC stream format");
    }

    const int subtype = info.format & SF_FORMAT_SUBMASK;
    int bits = 0;
    if (subtype == SF_FORMAT_PCM_16) {
        bits = 16;
    } else if (subtype == SF_FORMAT_PCM_24 || subtype == SF_FORMAT_PCM_32) {
        bits = 32;
    } else {
        throw std::runtime_error("unsupported FLAC subtype");
    }

    Format fmt;
    fmt.channels = info.channels;
    fmt.rate = info.samplerate;
    fmt.bits = bits;
    if (info.frames > 0 && info.samplerate > 0) {
        fmt.duration_s = static_cast<double>(info.frames) / static_cast<double>(info.samplerate);
    }
    return fmt;
}


void write_frames(snd_pcm_t* pcm, const void* data, sf_count_t frames, int frame_bytes) {
    const auto* cursor = static_cast<const std::uint8_t*>(data);
    snd_pcm_sframes_t remaining = static_cast<snd_pcm_sframes_t>(frames);
    while (remaining > 0) {
        snd_pcm_sframes_t written = snd_pcm_writei(pcm, cursor, remaining);
        if (written == -EPIPE) {
            emit_line("LOG", "ALSA underrun recovered");
            snd_pcm_prepare(pcm);
            continue;
        }
        if (written == -ESTRPIPE) {
            while ((written = snd_pcm_resume(pcm)) == -EAGAIN) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (written < 0) {
                snd_pcm_prepare(pcm);
            }
            continue;
        }
        if (written < 0) {
            const int recovered = snd_pcm_recover(pcm, static_cast<int>(written), 1);
            if (recovered < 0) {
                throw std::runtime_error(alsa_error("ALSA write failed", static_cast<int>(written)));
            }
            emit_line("LOG", alsa_error("ALSA write recovered", static_cast<int>(written)));
            continue;
        }
        if (written == 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            continue;
        }
        cursor += written * frame_bytes;
        remaining -= written;
    }
}

void emit_format(const Format& output, const Format& source) {
    const Format source_fmt = source.rate > 0 ? source : output;
    emit_message("FORMAT", {
        {"channels", std::to_string(output.channels)},
        {"rate", std::to_string(output.rate)},
        {"bits", std::to_string(output.bits)},
        {"duration", format_number(output.duration_s)},
        {"source_channels", std::to_string(source_fmt.channels)},
        {"source_rate", std::to_string(source_fmt.rate)},
        {"source_bits", std::to_string(source_fmt.bits)}
    });
}

bool same_pcm_format(const Format& left, const Format& right) {
    return left.channels == right.channels && left.rate == right.rate && left.bits == right.bits;
}

std::string format_summary(const Format& fmt) {
    std::ostringstream out;
    out << fmt.channels << "ch @ " << fmt.rate << "Hz " << fmt.bits << "bit";
    return out.str();
}

sf_count_t played_frame_from_written(sf_count_t written_frames, snd_pcm_t* pcm) {
    if (pcm == nullptr) {
        return std::max<sf_count_t>(0, written_frames);
    }
    snd_pcm_sframes_t delay = 0;
    if (snd_pcm_delay(pcm, &delay) == 0 && delay > 0) {
        written_frames -= std::min<sf_count_t>(written_frames, static_cast<sf_count_t>(delay));
    }
    return std::max<sf_count_t>(0, written_frames);
}

sf_count_t alsa_delay_frames(snd_pcm_t* pcm) {
    if (pcm == nullptr) {
        return 0;
    }
    snd_pcm_sframes_t delay = 0;
    if (snd_pcm_delay(pcm, &delay) == 0 && delay > 0) {
        return static_cast<sf_count_t>(delay);
    }
    return 0;
}

double alsa_delay_seconds(const Format& fmt, snd_pcm_t* pcm) {
    if (fmt.rate <= 0) {
        return 0.0;
    }
    return static_cast<double>(alsa_delay_frames(pcm)) / static_cast<double>(fmt.rate);
}

double played_seconds_from_written(sf_count_t written_frames, const Format& fmt, snd_pcm_t* pcm) {
    if (fmt.rate <= 0) {
        return 0.0;
    }
    const sf_count_t played = played_frame_from_written(written_frames, pcm);
    return static_cast<double>(played) / static_cast<double>(fmt.rate);
}

void emit_position(SNDFILE* file, const Format& fmt, snd_pcm_t* pcm) {
    const sf_count_t frame = sf_seek(file, 0, SEEK_CUR);
    const double pos_s = frame >= 0 ? played_seconds_from_written(frame, fmt, pcm) : 0.0;
    emit_position_values(pos_s, fmt.duration_s);
}

double stream_played_position_s(
    std::uint64_t bytes_written,
    int block_align,
    double start_offset_s,
    const Format& fmt,
    snd_pcm_t* pcm
) {
    if (block_align <= 0 || fmt.rate <= 0) {
        return start_offset_s;
    }
    const auto written_frames = static_cast<sf_count_t>(bytes_written / static_cast<std::uint64_t>(block_align));
    return start_offset_s + played_seconds_from_written(written_frames, fmt, pcm);
}

void emit_position_values(double pos_s, double duration_s) {
    emit_message("POSITION", {{"seconds", format_number(pos_s)}, {"duration", format_number(duration_s)}});
}

} // namespace tidal_native
