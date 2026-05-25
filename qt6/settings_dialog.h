#pragma once

#include <QDialog>
#include <QString>

#include <functional>

class CacheManagerQt;
class ScrobbleService;

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QSlider;
class QSpinBox;
class QTabWidget;

class SettingsDialog : public QDialog {
    Q_OBJECT

public:
    struct RuntimeState {
        QString currentDevice;
        int volumePercent = 100;
        bool gaplessEnabled = true;
        bool streamTransitionSmoothing = false;
        bool reduceAnimations = false;
        bool discordEnabled = false;
        QString discordClientId;
        bool mprisEnabled = true;
        bool mprisAvailable = false;
        bool mprisRunning = false;
        bool nativeAvailable = false;
        bool discordConnected = false;
        bool offlineMode = false;
        bool audioCacheEnabled = true;
        bool coverCacheEnabled = true;
        QString cacheMode = QStringLiteral("balanced");
        int audioCacheLimitMb = 0;
        int coverCacheLimitMb = 0;
    };

    struct Result {
        QString selectedDevice;
        int volumePercent = 100;
        bool gaplessEnabled = true;
        bool streamTransitionSmoothing = false;
        bool reduceAnimations = false;
        bool discordEnabled = false;
        QString discordClientId;
        bool mprisEnabled = true;
        bool mprisAvailable = false;
        bool audioCacheEnabled = true;
        bool coverCacheEnabled = true;
        QString cacheMode = QStringLiteral("balanced");
        int audioCacheLimitMb = 0;
        int coverCacheLimitMb = 0;
    };

    SettingsDialog(
        const RuntimeState& state,
        CacheManagerQt* cache,
        ScrobbleService* scrobble,
        std::function<void()> clearCache,
        std::function<void()> clearDownloads,
        QWidget* parent = nullptr
    );

    Result result() const;

private:
    void buildPlaybackTab();
    void buildStorageTab();
    void buildIntegrationsTab();
    void buildHealthTab();
    void updateCacheSummaries();
    void updateScrobbleStatus();
    void applyScrobbleConfig();
    void repopulateDevices(const QString& preferred);

    RuntimeState m_state;
    CacheManagerQt* m_cache = nullptr;
    ScrobbleService* m_scrobble = nullptr;
    std::function<void()> m_clearCache;
    std::function<void()> m_clearDownloads;

    QTabWidget* m_tabs = nullptr;
    QComboBox* m_deviceCombo = nullptr;
    QSlider* m_volumeSlider = nullptr;
    QCheckBox* m_gapless = nullptr;
    QCheckBox* m_streamTransitionSmoothing = nullptr;
    QCheckBox* m_reduceAnimations = nullptr;
    QCheckBox* m_discord = nullptr;
    QLineEdit* m_discordClientId = nullptr;
    QCheckBox* m_mpris = nullptr;
    QCheckBox* m_audioCache = nullptr;
    QCheckBox* m_coverCache = nullptr;
    QComboBox* m_cacheMode = nullptr;
    QSpinBox* m_audioCacheLimit = nullptr;
    QSpinBox* m_coverCacheLimit = nullptr;
    QLabel* m_cacheSummary = nullptr;
    QLabel* m_downloadsSummary = nullptr;
    QCheckBox* m_lastFm = nullptr;
    QLineEdit* m_lastFmApiKey = nullptr;
    QLineEdit* m_lastFmSecret = nullptr;
    QLineEdit* m_lastFmSession = nullptr;
    QCheckBox* m_listenBrainz = nullptr;
    QLineEdit* m_listenBrainzToken = nullptr;
    QLabel* m_scrobbleStatus = nullptr;
};
