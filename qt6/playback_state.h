#pragma once

#include <QJsonObject>
#include <QString>

struct AudioFormat {
    int sampleRate = 0;
    int bitDepth = 0;
    int channels = 0;

    bool valid() const {
        return sampleRate > 0 && bitDepth > 0;
    }
};

struct PlaybackState {
    QJsonObject track;
    QString trackId;
    QString albumId;
    QString audioQuality;
    double positionSeconds = 0.0;
    double durationSeconds = 0.0;
    bool busy = false;
    bool paused = false;
    bool buffering = false;
    bool localFile = false;
    bool downloaded = false;
    bool bitPerfect = false;
    QString outputDevice;
    AudioFormat streamFormat;
    AudioFormat outputFormat;

    bool hasTrack() const {
        return !trackId.isEmpty();
    }

    bool playing() const {
        return busy && !paused && !buffering;
    }
};
