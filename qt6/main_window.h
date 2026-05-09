#pragma once

#include "browser_controller.h"
#include "cache_manager.h"
#include "playback_controller.h"
#include "scrobble_service.h"
#include "tidal_sidecar.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QMap>
#include <QNetworkAccessManager>
#include <QSettings>
#include <QSet>
#include <QTreeWidget>

class QAction;
class QLabel;
class QLineEdit;
class QListWidget;
class QListWidgetItem;
class QMenu;
class DiscordRpcService;
class MprisService;
class QPushButton;
class QPropertyAnimation;
class QSlider;
class QSpinBox;
class QTabWidget;
class QComboBox;
class QWidget;
class QCloseEvent;
class QEvent;

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

protected:
    void closeEvent(QCloseEvent* event) override;
    bool eventFilter(QObject* watched, QEvent* event) override;

private slots:
    void login();
    void search();
    void loadUrl();
    void queueUrl();
    void refreshCollection();
    void refreshCacheTab();
    void refreshDevices();
    void queueCachedTracks();
    void queueDownloadedTracks();
    void clearCachedTracks();
    void clearDownloadedTracks();
    void playSelected();
    void stopPlayback();
    void togglePause();
    void playNextQueued();
    void queueSelected();
    void downloadSelected();
    void radioSelected();
    void toggleFavoriteSelected();
    void seekReleased();
    void volumeChanged(int value);
    void itemActivated(QTreeWidgetItem* item, int column);
    void sidecarLoginLink(const QString& url, const QString& code, int expiresSeconds);

private:
    void buildUi();
    void setupTreeActions(QTreeWidget* tree);
    void setupListActions(QListWidget* list);
    QJsonObject itemObject(QTreeWidgetItem* item) const;
    QJsonObject selectedTrack() const;
    QJsonObject selectedObject() const;
    QJsonObject selectedListObject() const;
    QJsonObject currentTrackObject() const;
    QTreeWidget* activeTree() const;
    QTreeWidgetItem* currentBrowseItem() const;
    void rememberTracks(const QJsonArray& items);
    void rememberFavoriteItems(const QString& type, const QJsonArray& items);
    void refreshFavoriteState();
    bool isFavoriteItem(const QString& type, const QString& id, const QJsonObject& obj = {}) const;
    void setFavoriteState(const QString& type, const QString& id, bool favorite);
    QAction* addFavoriteAction(QMenu* menu, const QString& type, const QString& id, const QJsonObject& obj = {});
    void toggleFavorite(const QString& type, const QString& id, bool favorite);
    bool itemNeedsDetails(QTreeWidgetItem* item) const;
    void loadContainerDetails(QTreeWidgetItem* item, bool playAfterLoad = false, bool queueAfterLoad = false);
    void playOrQueueObject(const QJsonObject& obj, bool playFirst);
    void requestRadioForObject(const QJsonObject& obj, bool playFirst);
    bool requireOnline(const QString& action);
    void updateNetworkMode();
    void enterOfflineMode(const QString& reason = QString());
    void setNetworkTabsEnabled(bool enabled);
    void syncPlaybackOutput();
    void setNowPlaying(const QJsonObject& track);
    void startPlayback(const QJsonObject& track);
    void setStatus(const QString& message);
    QString trackLine(const QJsonObject& track) const;
    QString tidalUrl(const QString& type, const QString& id) const;
    void copyTidalLink(const QString& type, const QString& id);
    void openTidalItem(const QString& type, const QString& id);
    bool trackIsLocal(const QString& id) const;
    bool trackIsDownloaded(const QString& id) const;
    void addTrackStorageAction(QMenu* menu, const QJsonObject& track);
    void downloadOrDeleteTrack(const QJsonObject& track);
    void appendQueue(const QJsonObject& track);
    void insertQueueNext(const QJsonObject& track);
    void refreshQueueView();
    void clearQueue();
    void updateQueueTabLabel();
    void removeQueueRow(int row);
    void playQueueRow(int row);
    void moveQueueRowToNext(int row);
    void loadLyrics(const QString& trackId);
    void updateLyrics(double positionSeconds);
    void seekToLyricItem(QListWidgetItem* item);
    void beginSeekPreview(double seconds);
    bool seekPreviewActive(double incomingPosition) const;
    void holdLyricsAutoScroll();
    bool lyricsAutoScrollHeld() const;
    void scrollLyricsToLine(int row, bool animated = true);
    void stopLyricsScrollAnimation();
    void loadCoverForSelected();
    void loadCover(const QJsonObject& track);
    void requestCover(const QString& coverUrl, const QString& requestId, const QString& albumId = QString());
    void setupPlaybackSignals();
    void initDiscord();
    void shutdownDiscord();
    void setDiscordEnabled(bool enabled, const QString& clientId = QString());
    void updateDiscordTrack(const QJsonObject& track);
    void updateDiscordContext();
    void updateDiscordPlaybackStatus();
    void updateDiscordPosition(double positionSeconds, double durationSeconds);
    void notifyDiscordSeeked(double positionSeconds, double durationSeconds);
    void initMpris();
    void shutdownMpris();
    void setMprisEnabled(bool enabled);
    void updateMprisTrack(const QJsonObject& track, double durationSeconds = 0.0);
    void updateMprisPlaybackStatus();
    void updateMprisPosition(double positionSeconds, double durationSeconds);
    void updateMprisVolume();
    void updateMprisQueueState();
    void mprisPlay();
    void mprisPause();
    void mprisNext();
    void mprisSeek(double offsetSeconds);
    void mprisSetPosition(double positionSeconds);
    void showSettingsDialog();
    void updatePauseButton();
    void updateCacheStatusLabels();
    void updateAudioStatusLabels();
    bool volumeControlAvailable() const;

    TidalSidecar m_sidecar;
    CacheManagerQt m_cache;
    QSettings m_settings;
    BrowserController m_browser;
    PlaybackController m_playback;
    ScrobbleService m_scrobble;
    DiscordRpcService* m_discord = nullptr;
    MprisService* m_mpris = nullptr;
    QNetworkAccessManager m_network;

    QComboBox* m_deviceCombo = nullptr;
    QTabWidget* m_tabs = nullptr;
    QTabWidget* m_detailsTabs = nullptr;
    QWidget* m_homeTab = nullptr;
    QWidget* m_searchTab = nullptr;
    QWidget* m_urlTab = nullptr;
    QWidget* m_collectionTab = nullptr;
    QWidget* m_cacheTab = nullptr;
    QTreeWidget* m_homeTree = nullptr;
    QTreeWidget* m_searchTree = nullptr;
    QTreeWidget* m_urlTree = nullptr;
    QTreeWidget* m_collectionTree = nullptr;
    QListWidget* m_cacheList = nullptr;
    QListWidget* m_downloadList = nullptr;
    QListWidget* m_queueList = nullptr;
    QListWidget* m_lastTrackList = nullptr;
    QLineEdit* m_searchEdit = nullptr;
    QComboBox* m_searchType = nullptr;
    QSpinBox* m_searchLimit = nullptr;
    QLineEdit* m_urlEdit = nullptr;
    QComboBox* m_collectionType = nullptr;
    QLabel* m_cover = nullptr;
    QLabel* m_title = nullptr;
    QLabel* m_meta = nullptr;
    QLabel* m_quality = nullptr;
    QLabel* m_bitrate = nullptr;
    QLabel* m_bitperfect = nullptr;
    QLabel* m_status = nullptr;
    QLabel* m_time = nullptr;
    QLabel* m_volumeLabel = nullptr;
    QLabel* m_cacheStatusLabel = nullptr;
    QLabel* m_downloadsStatusLabel = nullptr;
    QLabel* m_lyricsTitle = nullptr;
    QLabel* m_lyricsMeta = nullptr;
    QSlider* m_seek = nullptr;
    QSlider* m_volume = nullptr;
    QPushButton* m_pauseButton = nullptr;
    QListWidget* m_lyricsList = nullptr;
    QPropertyAnimation* m_lyricsScrollAnimation = nullptr;

    QMap<QString, QJsonObject> m_tracks;
    QMap<QString, QSet<QString>> m_favoriteIds;
    QSet<QString> m_favoriteTypesLoaded;
    QString m_coverRequestId;
    QString m_displayedCoverUrl;
    QString m_displayedCoverAlbumId;
    QJsonArray m_timedLyrics;
    double m_seekPreviewTarget = -1.0;
    qint64 m_seekPreviewUntilMs = 0;
    qint64 m_lyricsAutoScrollHoldUntilMs = 0;
    int m_outputRate = 0;
    int m_outputBits = 0;
    int m_outputChannels = 0;
    int m_queueTabIndex = -1;
    int m_currentLyricIndex = -1;
    bool m_reduceAnimations = false;
    bool m_discordEnabled = true;
    bool m_mprisEnabled = true;
    bool m_mprisAvailable = false;
    bool m_offlineMode = false;
    QString m_discordClientId;
};
