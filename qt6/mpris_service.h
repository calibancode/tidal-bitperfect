#pragma once

#include <QJsonObject>
#include <QObject>
#include <QVariantMap>

class MprisService : public QObject {
    Q_OBJECT

public:
    explicit MprisService(QObject* parent = nullptr);
    ~MprisService() override;

    static bool available();

    bool start();
    void stopService();
    bool running() const { return m_running; }

    QString playbackStatus() const { return m_playbackStatus; }
    QVariantMap metadata() const { return m_metadata; }
    qlonglong positionUs() const { return m_positionUs; }
    double volume() const { return m_volume; }
    bool canGoNext() const { return m_canGoNext; }
    bool canSeek() const { return m_canSeek; }

    void updateTrack(const QJsonObject& track, double durationSeconds = 0.0);
    void clearTrack();
    void setPlaybackStatus(const QString& status);
    void updatePosition(double positionSeconds, double durationSeconds);
    void notifySeeked(double positionSeconds);
    void setVolume(double fraction);
    void setCanGoNext(bool canGoNext);

    void requestPlay();
    void requestPause();
    void requestPlayPause();
    void requestStop();
    void requestNext();
    void requestSeek(qlonglong offsetUs);
    void requestSetPosition(qlonglong positionUs);
    void requestOpenUri(const QString& uri);
    void requestVolume(double fraction);
    void requestRaise();
    void requestQuit();

signals:
    void playRequested();
    void pauseRequested();
    void playPauseRequested();
    void stopRequested();
    void nextRequested();
    void seekRequested(double offsetSeconds);
    void setPositionRequested(double positionSeconds);
    void openUriRequested(const QString& uri);
    void volumeRequested(int percent);
    void raiseRequested();
    void quitRequested();
    void statusMessage(const QString& message);
    void errorMessage(const QString& message);

private:
    void emitPlayerPropertiesChanged(const QVariantMap& changed) const;
    void emitRootPropertiesChanged(const QVariantMap& changed) const;
    void emitSeeked() const;
    void updateDuration(double durationSeconds, bool emitChange);

    QString m_playbackStatus = QStringLiteral("Stopped");
    QVariantMap m_metadata;
    qlonglong m_positionUs = 0;
    qlonglong m_durationUs = 0;
    double m_volume = 1.0;
    bool m_canGoNext = false;
    bool m_canSeek = false;
    bool m_running = false;
};
