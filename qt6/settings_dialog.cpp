#include "settings_dialog.h"

#include "cache_manager.h"
#include "main_window_support.h"
#include "scrobble_service.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialogButtonBox>
#include <QDir>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSlider>
#include <QSpinBox>
#include <QStringList>
#include <QTabWidget>
#include <QUrl>
#include <QVBoxLayout>
#include <QWidget>

#include <utility>

using namespace MainWindowSupport;

SettingsDialog::SettingsDialog(
    const RuntimeState& state,
    CacheManagerQt* cache,
    ScrobbleService* scrobble,
    std::function<void()> clearCache,
    std::function<void()> clearDownloads,
    QWidget* parent
)
    : QDialog(parent),
      m_state(state),
      m_cache(cache),
      m_scrobble(scrobble),
      m_clearCache(std::move(clearCache)),
      m_clearDownloads(std::move(clearDownloads)) {
    setWindowTitle(QStringLiteral("Settings"));
    resize(640, 420);
    auto* layout = new QVBoxLayout(this);
    m_tabs = new QTabWidget(this);
    m_tabs->setDocumentMode(true);
    layout->addWidget(m_tabs, 1);

    buildPlaybackTab();
    buildStorageTab();
    buildIntegrationsTab();
    buildHealthTab();

    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
    connect(buttons, &QDialogButtonBox::accepted, this, [this]() {
        applyScrobbleConfig();
        accept();
    });
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
}

SettingsDialog::Result SettingsDialog::result() const {
    Result out;
    out.selectedDevice = m_deviceCombo ? m_deviceCombo->currentText().trimmed() : m_state.currentDevice;
    out.volumePercent = m_volumeSlider ? m_volumeSlider->value() : m_state.volumePercent;
    out.gaplessEnabled = m_gapless ? m_gapless->isChecked() : m_state.gaplessEnabled;
    out.streamTransitionSmoothing = m_streamTransitionSmoothing ? m_streamTransitionSmoothing->isChecked() : m_state.streamTransitionSmoothing;
    out.reduceAnimations = m_reduceAnimations ? m_reduceAnimations->isChecked() : m_state.reduceAnimations;
    out.discordEnabled = m_discord ? m_discord->isChecked() : m_state.discordEnabled;
    out.discordClientId = m_discordClientId ? m_discordClientId->text() : m_state.discordClientId;
    out.mprisEnabled = m_mpris ? m_mpris->isChecked() : m_state.mprisEnabled;
    out.mprisAvailable = m_mpris ? m_mpris->isEnabled() : m_state.mprisAvailable;
    out.audioCacheEnabled = m_audioCache ? m_audioCache->isChecked() : m_state.audioCacheEnabled;
    out.coverCacheEnabled = m_coverCache ? m_coverCache->isChecked() : m_state.coverCacheEnabled;
    out.cacheMode = m_cacheMode ? m_cacheMode->currentData().toString() : m_state.cacheMode;
    out.audioCacheLimitMb = m_audioCacheLimit ? m_audioCacheLimit->value() : m_state.audioCacheLimitMb;
    out.coverCacheLimitMb = m_coverCacheLimit ? m_coverCacheLimit->value() : m_state.coverCacheLimitMb;
    return out;
}

void SettingsDialog::buildPlaybackTab() {
    auto* playbackTab = new QWidget(m_tabs);
    auto* playbackTabLayout = new QVBoxLayout(playbackTab);
    auto* outputGroup = new QGroupBox(QStringLiteral("Output"), playbackTab);
    auto* outputLayout = new QGridLayout(outputGroup);
    m_deviceCombo = new QComboBox(outputGroup);
    m_deviceCombo->setEditable(true);
    repopulateDevices(m_state.currentDevice);
    auto* refreshDevicesButton = new QPushButton(QStringLiteral("Refresh"), outputGroup);
    m_volumeSlider = new QSlider(Qt::Horizontal, outputGroup);
    m_volumeSlider->setRange(0, 100);
    m_volumeSlider->setValue(m_state.volumePercent);
    auto* volumeValue = new QLabel(QStringLiteral("%1%").arg(m_volumeSlider->value()), outputGroup);
    volumeValue->setMinimumWidth(36);
    outputLayout->addWidget(new QLabel(QStringLiteral("ALSA device"), outputGroup), 0, 0);
    outputLayout->addWidget(m_deviceCombo, 0, 1);
    outputLayout->addWidget(refreshDevicesButton, 0, 2);
    outputLayout->addWidget(new QLabel(QStringLiteral("Volume"), outputGroup), 1, 0);
    outputLayout->addWidget(m_volumeSlider, 1, 1);
    outputLayout->addWidget(volumeValue, 1, 2);
    playbackTabLayout->addWidget(outputGroup);

    auto* behaviorGroup = new QGroupBox(QStringLiteral("Behavior"), playbackTab);
    auto* behaviorLayout = new QVBoxLayout(behaviorGroup);
    m_gapless = new QCheckBox(QStringLiteral("Gapless playback"), behaviorGroup);
    m_gapless->setChecked(m_state.gaplessEnabled);
    m_gapless->setToolTip(QStringLiteral("Prefetches the next queued stream into the audio cache for same-format handoff."));
    m_streamTransitionSmoothing = new QCheckBox(QStringLiteral("Soften streamed transitions"), behaviorGroup);
    m_streamTransitionSmoothing->setChecked(m_state.streamTransitionSmoothing);
    m_streamTransitionSmoothing->setToolTip(QStringLiteral("Applies a tiny de-click ramp only when a streamed track hands off to a queued track."));
    m_reduceAnimations = new QCheckBox(QStringLiteral("Reduce animations"), behaviorGroup);
    m_reduceAnimations->setChecked(m_state.reduceAnimations);
    m_reduceAnimations->setToolTip(QStringLiteral("Disables animated lyric recentering."));
    behaviorLayout->addWidget(m_gapless);
    behaviorLayout->addWidget(m_streamTransitionSmoothing);
    behaviorLayout->addWidget(m_reduceAnimations);
    playbackTabLayout->addWidget(behaviorGroup);
    playbackTabLayout->addStretch(1);
    m_tabs->addTab(playbackTab, QStringLiteral("Playback"));

    connect(refreshDevicesButton, &QPushButton::clicked, this, [this]() {
        repopulateDevices(m_deviceCombo ? m_deviceCombo->currentText() : QString());
    });
    connect(m_volumeSlider, &QSlider::valueChanged, volumeValue, [volumeValue](int value) {
        volumeValue->setText(QStringLiteral("%1%").arg(value));
    });
}

void SettingsDialog::buildStorageTab() {
    auto* storageTab = new QWidget(m_tabs);
    auto* storageLayout = new QVBoxLayout(storageTab);
    auto* policyGroup = new QGroupBox(QStringLiteral("Cache Policy"), storageTab);
    auto* policyLayout = new QGridLayout(policyGroup);
    m_audioCache = new QCheckBox(QStringLiteral("Audio cache"), policyGroup);
    m_audioCache->setChecked(m_state.audioCacheEnabled);
    m_audioCache->setToolTip(QStringLiteral("Keeps prefetched queued streams on disk for gapless handoff."));
    m_coverCache = new QCheckBox(QStringLiteral("Cover cache"), policyGroup);
    m_coverCache->setChecked(m_state.coverCacheEnabled);
    m_coverCache->setToolTip(QStringLiteral("Keeps downloaded artwork on disk."));
    m_cacheMode = new QComboBox(policyGroup);
    m_cacheMode->addItem(QStringLiteral("Conservative"), QStringLiteral("conservative"));
    m_cacheMode->addItem(QStringLiteral("Balanced"), QStringLiteral("balanced"));
    m_cacheMode->addItem(QStringLiteral("Aggressive"), QStringLiteral("aggressive"));
    const int modeIndex = qMax(0, m_cacheMode->findData(m_state.cacheMode));
    m_cacheMode->setCurrentIndex(modeIndex);
    m_cacheMode->setToolTip(QStringLiteral("Controls how eagerly queued streams are prefetched and how strongly useful cache entries are protected."));
    m_audioCacheLimit = new QSpinBox(policyGroup);
    m_audioCacheLimit->setRange(0, 102400);
    m_audioCacheLimit->setSuffix(QStringLiteral(" MB"));
    m_audioCacheLimit->setSpecialValueText(QStringLiteral("Unlimited"));
    m_audioCacheLimit->setValue(qBound(0, m_state.audioCacheLimitMb, 102400));
    m_coverCacheLimit = new QSpinBox(policyGroup);
    m_coverCacheLimit->setRange(0, 102400);
    m_coverCacheLimit->setSuffix(QStringLiteral(" MB"));
    m_coverCacheLimit->setSpecialValueText(QStringLiteral("Unlimited"));
    m_coverCacheLimit->setValue(qBound(0, m_state.coverCacheLimitMb, 102400));
    m_audioCacheLimit->setEnabled(m_audioCache->isChecked());
    m_coverCacheLimit->setEnabled(m_coverCache->isChecked());
    policyLayout->addWidget(new QLabel(QStringLiteral("Mode"), policyGroup), 0, 0);
    policyLayout->addWidget(m_cacheMode, 0, 1, 1, 2);
    policyLayout->addWidget(m_audioCache, 1, 0);
    policyLayout->addWidget(new QLabel(QStringLiteral("Audio limit"), policyGroup), 1, 1);
    policyLayout->addWidget(m_audioCacheLimit, 1, 2);
    policyLayout->addWidget(m_coverCache, 2, 0);
    policyLayout->addWidget(new QLabel(QStringLiteral("Cover limit"), policyGroup), 2, 1);
    policyLayout->addWidget(m_coverCacheLimit, 2, 2);
    policyLayout->setColumnStretch(2, 1);
    storageLayout->addWidget(policyGroup);

    auto* localFilesGroup = new QGroupBox(QStringLiteral("Local Files"), storageTab);
    auto* localFilesLayout = new QGridLayout(localFilesGroup);
    auto* cachePath = new QLabel(m_cache ? m_cache->baseDir() : QString(), localFilesGroup);
    cachePath->setWordWrap(true);
    cachePath->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_cacheSummary = new QLabel(localFilesGroup);
    m_downloadsSummary = new QLabel(localFilesGroup);
    auto* openCache = new QPushButton(QStringLiteral("Open cache"), localFilesGroup);
    auto* openDownloads = new QPushButton(QStringLiteral("Open downloads"), localFilesGroup);
    auto* clearCache = new QPushButton(QStringLiteral("Clear cache"), localFilesGroup);
    auto* clearDownloads = new QPushButton(QStringLiteral("Clear downloads"), localFilesGroup);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Location"), localFilesGroup), 0, 0);
    localFilesLayout->addWidget(cachePath, 0, 1, 1, 3);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Cache"), localFilesGroup), 1, 0);
    localFilesLayout->addWidget(m_cacheSummary, 1, 1, 1, 3);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Downloads"), localFilesGroup), 2, 0);
    localFilesLayout->addWidget(m_downloadsSummary, 2, 1, 1, 3);
    localFilesLayout->addWidget(openCache, 3, 0, 1, 2);
    localFilesLayout->addWidget(openDownloads, 3, 2, 1, 2);
    localFilesLayout->addWidget(clearCache, 4, 0, 1, 2);
    localFilesLayout->addWidget(clearDownloads, 4, 2, 1, 2);
    storageLayout->addWidget(localFilesGroup);
    storageLayout->addStretch(1);
    m_tabs->addTab(storageTab, QStringLiteral("Storage"));
    updateCacheSummaries();

    connect(openCache, &QPushButton::clicked, this, [this]() {
        if (!m_cache) return;
        QDir().mkpath(m_cache->baseDir());
        QDesktopServices::openUrl(QUrl::fromLocalFile(m_cache->baseDir()));
    });
    connect(openDownloads, &QPushButton::clicked, this, [this]() {
        if (!m_cache) return;
        QDir().mkpath(m_cache->downloadsDir());
        QDesktopServices::openUrl(QUrl::fromLocalFile(m_cache->downloadsDir()));
    });
    connect(clearCache, &QPushButton::clicked, this, [this]() {
        if (m_clearCache) m_clearCache();
        updateCacheSummaries();
    });
    connect(clearDownloads, &QPushButton::clicked, this, [this]() {
        if (m_clearDownloads) m_clearDownloads();
        updateCacheSummaries();
    });
    connect(m_audioCache, &QCheckBox::toggled, m_audioCacheLimit, &QWidget::setEnabled);
    connect(m_coverCache, &QCheckBox::toggled, m_coverCacheLimit, &QWidget::setEnabled);
}

void SettingsDialog::buildIntegrationsTab() {
    auto* integrationsTab = new QWidget(m_tabs);
    auto* integrationsTabLayout = new QVBoxLayout(integrationsTab);
    auto* servicesGroup = new QGroupBox(QStringLiteral("Services"), integrationsTab);
    auto* integrationsLayout = new QFormLayout(servicesGroup);
    m_discord = new QCheckBox(QStringLiteral("Discord Rich Presence"), servicesGroup);
    m_discord->setChecked(m_state.discordEnabled);
    m_discordClientId = new QLineEdit(servicesGroup);
    m_discordClientId->setPlaceholderText(QStringLiteral("Built-in Discord app ID"));
    m_discordClientId->setText(m_state.discordClientId);
    m_mpris = new QCheckBox(QStringLiteral("MPRIS media controls"), servicesGroup);
    m_mpris->setChecked(m_state.mprisEnabled && m_state.mprisAvailable);
    m_mpris->setEnabled(m_state.mprisAvailable);
    m_mpris->setToolTip(QStringLiteral("Exposes playback to media keys, playerctl, KDE Connect, and desktop shells."));
    integrationsLayout->addRow(QStringLiteral("Desktop media controls"), m_mpris);
    integrationsLayout->addRow(QStringLiteral("Discord"), m_discord);
    integrationsLayout->addRow(QStringLiteral("Client ID"), m_discordClientId);
    integrationsTabLayout->addWidget(servicesGroup);

    auto* scrobbleGroup = new QGroupBox(QStringLiteral("Scrobbling"), integrationsTab);
    auto* scrobbleLayout = new QGridLayout(scrobbleGroup);
    const ScrobbleService::LastFmConfig lastFmConfig = m_scrobble ? m_scrobble->lastFmConfig() : ScrobbleService::LastFmConfig{};
    const ScrobbleService::ListenBrainzConfig listenBrainzConfig = m_scrobble ? m_scrobble->listenBrainzConfig() : ScrobbleService::ListenBrainzConfig{};
    m_lastFm = new QCheckBox(QStringLiteral("Last.fm"), scrobbleGroup);
    m_lastFm->setChecked(lastFmConfig.enabled);
    m_lastFmApiKey = new QLineEdit(scrobbleGroup);
    m_lastFmApiKey->setText(lastFmConfig.apiKey);
    m_lastFmSecret = new QLineEdit(scrobbleGroup);
    m_lastFmSecret->setEchoMode(QLineEdit::Password);
    m_lastFmSecret->setText(lastFmConfig.sharedSecret);
    m_lastFmSession = new QLineEdit(scrobbleGroup);
    m_lastFmSession->setEchoMode(QLineEdit::Password);
    m_lastFmSession->setText(lastFmConfig.sessionKey);
    auto* lastFmAuth = new QPushButton(QStringLiteral("Authorize"), scrobbleGroup);
    auto* lastFmFinish = new QPushButton(QStringLiteral("Finish"), scrobbleGroup);
    auto* lastFmButtons = new QWidget(scrobbleGroup);
    auto* lastFmButtonLayout = new QHBoxLayout(lastFmButtons);
    lastFmButtonLayout->setContentsMargins(0, 0, 0, 0);
    lastFmButtonLayout->addWidget(lastFmAuth);
    lastFmButtonLayout->addWidget(lastFmFinish);
    auto* lastFmSessionRow = new QWidget(scrobbleGroup);
    auto* lastFmSessionLayout = new QHBoxLayout(lastFmSessionRow);
    lastFmSessionLayout->setContentsMargins(0, 0, 0, 0);
    lastFmSessionLayout->addWidget(m_lastFmSession, 1);
    lastFmSessionLayout->addWidget(lastFmButtons);
    m_listenBrainz = new QCheckBox(QStringLiteral("ListenBrainz"), scrobbleGroup);
    m_listenBrainz->setChecked(listenBrainzConfig.enabled);
    m_listenBrainzToken = new QLineEdit(scrobbleGroup);
    m_listenBrainzToken->setEchoMode(QLineEdit::Password);
    m_listenBrainzToken->setText(listenBrainzConfig.token);
    m_scrobbleStatus = new QLabel(scrobbleGroup);
    updateScrobbleStatus();
    scrobbleLayout->addWidget(m_lastFm, 0, 0);
    scrobbleLayout->addWidget(new QLabel(QStringLiteral("API key"), scrobbleGroup), 0, 1);
    scrobbleLayout->addWidget(m_lastFmApiKey, 0, 2);
    scrobbleLayout->addWidget(new QLabel(QStringLiteral("Secret"), scrobbleGroup), 1, 1);
    scrobbleLayout->addWidget(m_lastFmSecret, 1, 2);
    scrobbleLayout->addWidget(new QLabel(QStringLiteral("Session"), scrobbleGroup), 2, 1);
    scrobbleLayout->addWidget(lastFmSessionRow, 2, 2);
    scrobbleLayout->addWidget(m_listenBrainz, 3, 0);
    scrobbleLayout->addWidget(new QLabel(QStringLiteral("Token"), scrobbleGroup), 3, 1);
    scrobbleLayout->addWidget(m_listenBrainzToken, 3, 2);
    scrobbleLayout->addWidget(m_scrobbleStatus, 4, 0, 1, 3);
    scrobbleLayout->setColumnStretch(2, 1);
    integrationsTabLayout->addWidget(scrobbleGroup);
    integrationsTabLayout->addStretch(1);
    m_tabs->addTab(integrationsTab, QStringLiteral("Integrations"));

    connect(lastFmAuth, &QPushButton::clicked, this, [this]() {
        if (!m_scrobble) return;
        applyScrobbleConfig();
        m_scrobble->beginLastFmAuthorization();
    });
    connect(lastFmFinish, &QPushButton::clicked, this, [this]() {
        if (!m_scrobble) return;
        applyScrobbleConfig();
        m_scrobble->completeLastFmAuthorization();
    });
    if (m_scrobble) {
        connect(m_scrobble, &ScrobbleService::lastFmSessionKeyReady, this, [this](const QString& sessionKey, const QString&) {
            if (m_lastFmSession) m_lastFmSession->setText(sessionKey);
        });
        connect(m_scrobble, &ScrobbleService::configurationChanged, this, &SettingsDialog::updateScrobbleStatus);
    }
}

void SettingsDialog::buildHealthTab() {
    auto* healthTab = new QWidget(m_tabs);
    auto* healthLayout = new QVBoxLayout(healthTab);
    auto* healthGroup = new QGroupBox(QStringLiteral("Runtime"), healthTab);
    auto* runtimeLayout = new QFormLayout(healthGroup);
    runtimeLayout->addRow(QStringLiteral("Network"), new QLabel(m_state.offlineMode ? QStringLiteral("offline") : QStringLiteral("online"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("Native player"), new QLabel(m_state.nativeAvailable ? QStringLiteral("available") : QStringLiteral("missing"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("Discord"), new QLabel(m_state.discordConnected ? QStringLiteral("connected") : QStringLiteral("idle"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("MPRIS"), new QLabel(m_state.mprisAvailable ? (m_state.mprisRunning ? QStringLiteral("running") : QStringLiteral("available")) : QStringLiteral("unavailable"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("Scrobbling"), new QLabel((m_scrobble && (m_scrobble->lastFmReady() || m_scrobble->listenBrainzReady())) ? QStringLiteral("ready") : QStringLiteral("disabled"), healthGroup));
    healthLayout->addWidget(healthGroup);
    healthLayout->addStretch(1);
    m_tabs->addTab(healthTab, QStringLiteral("Health"));
}

void SettingsDialog::updateCacheSummaries() {
    if (!m_cache) return;
    const CacheManagerQt::Stats audio = m_cache->audioStats();
    const CacheManagerQt::Stats covers = m_cache->coverStats();
    const CacheManagerQt::Stats downloads = m_cache->downloadStats();
    if (m_cacheSummary) {
        m_cacheSummary->setText(QStringLiteral("Tracks: %1 | Covers: %2 | %3")
            .arg(audio.count)
            .arg(covers.count)
            .arg(formatBytes(audio.bytes + covers.bytes)));
    }
    if (m_downloadsSummary) {
        m_downloadsSummary->setText(QStringLiteral("Tracks: %1 | %2")
            .arg(downloads.count)
            .arg(formatBytes(downloads.bytes)));
    }
}

void SettingsDialog::updateScrobbleStatus() {
    if (!m_scrobble || !m_scrobbleStatus) return;
    QStringList parts;
    parts << QStringLiteral("Last.fm: %1").arg(m_scrobble->lastFmReady() ? QStringLiteral("ready") : QStringLiteral("not configured"));
    parts << QStringLiteral("ListenBrainz: %1").arg(m_scrobble->listenBrainzReady() ? QStringLiteral("ready") : QStringLiteral("not configured"));
    if (m_scrobble->pendingCount() > 0) parts << QStringLiteral("Pending: %1").arg(m_scrobble->pendingCount());
    m_scrobbleStatus->setText(parts.join(QStringLiteral(" | ")));
}

void SettingsDialog::applyScrobbleConfig() {
    if (!m_scrobble) return;
    m_scrobble->setLastFmConfig(
        m_lastFm && m_lastFm->isChecked(),
        m_lastFmApiKey ? m_lastFmApiKey->text() : QString(),
        m_lastFmSecret ? m_lastFmSecret->text() : QString(),
        m_lastFmSession ? m_lastFmSession->text() : QString()
    );
    m_scrobble->setListenBrainzConfig(
        m_listenBrainz && m_listenBrainz->isChecked(),
        m_listenBrainzToken ? m_listenBrainzToken->text() : QString()
    );
}

void SettingsDialog::repopulateDevices(const QString& preferred) {
    if (!m_deviceCombo) return;
    QStringList devices = playbackDevices();
    const QString target = preferred.trimmed().isEmpty() ? QStringLiteral("default") : preferred.trimmed();
    if (!target.isEmpty() && !devices.contains(target)) devices.prepend(target);
    m_deviceCombo->clear();
    m_deviceCombo->addItems(devices);
    m_deviceCombo->setCurrentText(target);
}
