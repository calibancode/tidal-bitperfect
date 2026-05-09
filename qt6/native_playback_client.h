#pragma once

#include <QProcess>
#include <QObject>

struct NativeAudioFormat {
    int channels = 0;
    int rate = 0;
    int bits = 0;
    double duration = 0.0;
};

class NativePlaybackClient : public QObject {
    Q_OBJECT

public:
    explicit NativePlaybackClient(QObject* parent = nullptr);
    ~NativePlaybackClient() override;

    bool available() const;
    bool busy() const { return m_busy; }
    void shutdown();
    void playFile(const QString& path, const QString& device, int volumePercent);
    void playFfmpeg(
        const QString& input,
        const QString& device,
        int volumePercent,
        const QString& codec,
        double duration,
        bool protocolWhitelist
    );
    void setNextTrack(const QString& trackId, const QString& path);
    void clearNextTrack();
    void stop();
    void pauseToggle();
    void seek(double deltaSeconds);
    void seekTo(double seconds);
    void setVolume(int percent);

signals:
    void formatReady(const NativeAudioFormat& format);
    void position(double seconds, double duration);
    void statusMessage(const QString& message);
    void logMessage(const QString& message);
    void advanced(const QString& trackId);
    void errorMessage(const QString& message);
    void finishedOk();

private slots:
    void onReadyReadStdout();
    void onReadyReadStderr();
    void onFinished(int exitCode, QProcess::ExitStatus status);

private:
    QString helperPath() const;
    bool startDaemon();
    void restartDaemon();
    void send(const QString& line);
    void handleLine(const QByteArray& rawLine);

    QProcess m_process;
    QByteArray m_buffer;
    bool m_seenDone = false;
    bool m_seenError = false;
    bool m_busy = false;
    bool m_suppressFinished = false;
    bool m_shuttingDown = false;
};
