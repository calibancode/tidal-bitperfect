#include "main_window.h"

#include "discord_rpc_service.h"
#include "mpris_service.h"

#include <QLabel>
#include <QLineEdit>
#include <QSettings>
#include <QSlider>
#include <QTabWidget>
#include <QWidget>

void MainWindow::initDiscord() {
    if (!m_discordEnabled) return;
    if (!m_discord) {
        m_discord = new DiscordRpcService(this);
        connect(m_discord, &DiscordRpcService::statusMessage, this, &MainWindow::setStatus);
        connect(m_discord, &DiscordRpcService::errorMessage, this, &MainWindow::setStatus);
    }
    m_discord->setClientId(m_discordClientId);
    m_discord->start();
    if (!currentTrackObject().isEmpty()) updateDiscordTrack(currentTrackObject());
    updateDiscordContext();
    updateDiscordPlaybackStatus();
}

void MainWindow::shutdownDiscord() {
    if (!m_discord) return;
    m_discord->stopService();
    delete m_discord;
    m_discord = nullptr;
}

void MainWindow::setDiscordEnabled(bool enabled, const QString& clientId) {
    m_discordEnabled = enabled;
    m_discordClientId = clientId.trimmed();
    m_settings.setValue(QStringLiteral("qt6/discord_enabled"), enabled);
    m_settings.setValue(QStringLiteral("qt6/discord_client_id"), m_discordClientId);
    if (enabled) initDiscord();
    else shutdownDiscord();
}

void MainWindow::updateDiscordTrack(const QJsonObject& track) {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->updateTrack(track, m_playback.duration());
    updateDiscordContext();
}

void MainWindow::updateDiscordContext() {
    if (!m_discord || !m_discordEnabled) return;
    const bool local = trackIsLocal(m_playback.currentTrackId());
    m_discord->updateContext(
        m_quality ? m_quality->text() : QString(),
        m_bitperfect ? m_bitperfect->text() : QString(),
        local,
        m_offlineMode,
        m_playback.queueSize()
    );
}

void MainWindow::updateDiscordPlaybackStatus() {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->setPlaying(m_playback.busy() && !m_playback.paused());
}

void MainWindow::updateDiscordPosition(double positionSeconds, double durationSeconds) {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->updatePosition(positionSeconds, durationSeconds);
}

void MainWindow::notifyDiscordSeeked(double positionSeconds, double durationSeconds) {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->notifySeeked(positionSeconds, durationSeconds);
}

void MainWindow::initMpris() {
    m_mprisAvailable = MprisService::available();
    if (!m_mprisEnabled || !m_mprisAvailable) return;
    if (!m_mpris) {
        m_mpris = new MprisService(this);
        connect(m_mpris, &MprisService::statusMessage, this, &MainWindow::setStatus);
        connect(m_mpris, &MprisService::errorMessage, this, &MainWindow::setStatus);
        connect(m_mpris, &MprisService::playRequested, this, &MainWindow::mprisPlay);
        connect(m_mpris, &MprisService::pauseRequested, this, &MainWindow::mprisPause);
        connect(m_mpris, &MprisService::playPauseRequested, this, &MainWindow::togglePause);
        connect(m_mpris, &MprisService::stopRequested, this, &MainWindow::stopPlayback);
        connect(m_mpris, &MprisService::nextRequested, this, &MainWindow::mprisNext);
        connect(m_mpris, &MprisService::seekRequested, this, &MainWindow::mprisSeek);
        connect(m_mpris, &MprisService::setPositionRequested, this, &MainWindow::mprisSetPosition);
        connect(m_mpris, &MprisService::volumeRequested, this, [this](int percent) {
            if (m_volume) m_volume->setValue(qBound(0, percent, 100));
        });
        connect(m_mpris, &MprisService::openUriRequested, this, [this](const QString& uri) {
            if (uri.isEmpty() || !m_tabs || !m_urlEdit) return;
            m_tabs->setCurrentWidget(m_urlTab);
            m_urlEdit->setText(uri);
            loadUrl();
        });
        connect(m_mpris, &MprisService::raiseRequested, this, [this]() {
            showNormal();
            activateWindow();
            raise();
        });
        connect(m_mpris, &MprisService::quitRequested, this, &QWidget::close);
    }
    if (m_mpris->start()) {
        updateMprisVolume();
        updateMprisQueueState();
        if (!currentTrackObject().isEmpty()) updateMprisTrack(currentTrackObject(), m_playback.duration());
        updateMprisPlaybackStatus();
    }
}

void MainWindow::shutdownMpris() {
    if (!m_mpris) return;
    m_mpris->stopService();
    delete m_mpris;
    m_mpris = nullptr;
}

void MainWindow::setMprisEnabled(bool enabled) {
    m_mprisEnabled = enabled;
    m_settings.setValue(QStringLiteral("qt6/mpris_enabled"), enabled);
    if (enabled) initMpris();
    else shutdownMpris();
}

void MainWindow::updateMprisTrack(const QJsonObject& track, double durationSeconds) {
    if (!m_mpris || !m_mpris->running()) return;
    m_mpris->updateTrack(track, durationSeconds);
    updateMprisQueueState();
}

void MainWindow::updateMprisPlaybackStatus() {
    if (!m_mpris || !m_mpris->running()) return;
    if (!m_playback.busy()) m_mpris->setPlaybackStatus(QStringLiteral("Stopped"));
    else m_mpris->setPlaybackStatus(m_playback.paused() ? QStringLiteral("Paused") : QStringLiteral("Playing"));
}

void MainWindow::updateMprisPosition(double positionSeconds, double durationSeconds) {
    if (!m_mpris || !m_mpris->running()) return;
    m_mpris->updatePosition(positionSeconds, durationSeconds);
}

void MainWindow::updateMprisVolume() {
    if (!m_mpris || !m_mpris->running() || !m_volume) return;
    m_mpris->setVolume(static_cast<double>(m_volume->value()) / 100.0);
}

void MainWindow::updateMprisQueueState() {
    if (!m_mpris || !m_mpris->running()) return;
    m_mpris->setCanGoNext(!m_playback.queueEmpty());
}

void MainWindow::mprisPlay() {
    if (m_playback.busy()) {
        if (m_playback.paused()) togglePause();
        return;
    }
    const QJsonObject current = currentTrackObject();
    if (!current.isEmpty()) startPlayback(current);
    else playSelected();
}

void MainWindow::mprisPause() {
    if (m_playback.busy() && !m_playback.paused()) togglePause();
}

void MainWindow::mprisNext() {
    if (!m_playback.queueEmpty()) playNextQueued();
    else if (m_playback.busy()) stopPlayback();
}

void MainWindow::mprisSeek(double offsetSeconds) {
    if (!m_playback.busy()) return;
    const double duration = m_playback.duration();
    double target = qMax(0.0, m_playback.positionSeconds() + offsetSeconds);
    if (duration > 0.0) target = qMin(target, duration);
    beginSeekPreview(target);
    m_playback.seekTo(target);
    notifyDiscordSeeked(target, duration);
    updateMprisPosition(target, duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}

void MainWindow::mprisSetPosition(double positionSeconds) {
    if (!m_playback.busy()) return;
    const double duration = m_playback.duration();
    double target = qMax(0.0, positionSeconds);
    if (duration > 0.0) target = qMin(target, duration);
    beginSeekPreview(target);
    m_playback.seekTo(target);
    notifyDiscordSeeked(target, duration);
    updateMprisPosition(target, duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}
