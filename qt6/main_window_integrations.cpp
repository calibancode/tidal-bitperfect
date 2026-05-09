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
    const PlaybackState state = m_playback.playbackState();
    if (state.hasTrack() && state.busy) updateDiscordTrack(state);
    updateDiscordContext(state);
    updateDiscordPlaybackStatus(state);
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

void MainWindow::updateDiscordTrack(const PlaybackState& state) {
    if (!m_discord || !m_discordEnabled) return;
    if (!state.hasTrack()) return;
    m_discord->updateTrack(state.track, state.durationSeconds);
    updateDiscordContext(state);
}

void MainWindow::updateDiscordContext() {
    updateDiscordContext(m_playback.playbackState());
}

void MainWindow::updateDiscordContext(const PlaybackState& state) {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->updateContext(
        m_quality ? m_quality->text() : QString(),
        m_bitperfect ? m_bitperfect->text() : QString(),
        state.localFile,
        m_offlineMode,
        m_playback.queueSize()
    );
}

void MainWindow::updateDiscordPlaybackStatus() {
    updateDiscordPlaybackStatus(m_playback.playbackState());
}

void MainWindow::updateDiscordPlaybackStatus(const PlaybackState& state) {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->setPlaying(state.playing());
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
        const PlaybackState state = m_playback.playbackState();
        if (state.hasTrack() && state.busy) updateMprisTrack(state);
        updateMprisPlaybackStatus(state);
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

void MainWindow::updateMprisTrack(const PlaybackState& state) {
    if (!m_mpris || !m_mpris->running()) return;
    if (!state.hasTrack()) return;
    m_mpris->updateTrack(state.track, state.durationSeconds);
    updateMprisQueueState();
}

void MainWindow::updateMprisPlaybackStatus() {
    updateMprisPlaybackStatus(m_playback.playbackState());
}

void MainWindow::updateMprisPlaybackStatus(const PlaybackState& state) {
    if (!m_mpris || !m_mpris->running()) return;
    if (!state.busy) m_mpris->setPlaybackStatus(QStringLiteral("Stopped"));
    else m_mpris->setPlaybackStatus(state.paused ? QStringLiteral("Paused") : QStringLiteral("Playing"));
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
    const PlaybackState state = m_playback.playbackState();
    if (state.busy) {
        if (state.paused) togglePause();
        return;
    }
    const QJsonObject current = currentTrackObject();
    if (!current.isEmpty()) startPlayback(current);
    else playSelected();
}

void MainWindow::mprisPause() {
    const PlaybackState state = m_playback.playbackState();
    if (state.busy && !state.paused) togglePause();
}

void MainWindow::mprisNext() {
    if (!m_playback.queueEmpty()) playNextQueued();
    else if (m_playback.playbackState().busy) stopPlayback();
}

void MainWindow::mprisSeek(double offsetSeconds) {
    const PlaybackState state = m_playback.playbackState();
    if (!state.busy) return;
    const double duration = state.durationSeconds;
    double target = qMax(0.0, state.positionSeconds + offsetSeconds);
    if (duration > 0.0) target = qMin(target, duration);
    beginSeekPreview(target);
    m_playback.seekTo(target);
    notifyDiscordSeeked(target, duration);
    updateMprisPosition(target, duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}

void MainWindow::mprisSetPosition(double positionSeconds) {
    const PlaybackState state = m_playback.playbackState();
    if (!state.busy) return;
    const double duration = state.durationSeconds;
    double target = qMax(0.0, positionSeconds);
    if (duration > 0.0) target = qMin(target, duration);
    beginSeekPreview(target);
    m_playback.seekTo(target);
    notifyDiscordSeeked(target, duration);
    updateMprisPosition(target, duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}
