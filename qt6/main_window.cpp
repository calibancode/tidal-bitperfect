#include "main_window.h"

#include "discord_rpc_service.h"
#include "main_window_support.h"
#include "mpris_service.h"
#include "settings_dialog.h"

#include <QAbstractButton>
#include <QAbstractItemView>
#include <QApplication>
#include <QBoxLayout>
#include <QCloseEvent>
#include <QClipboard>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDir>
#include <QGroupBox>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLayout>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMessageBox>
#include <QNetworkReply>
#include <QPixmap>
#include <QPushButton>
#include <QSet>
#include <QShortcut>
#include <QSignalBlocker>
#include <QSizePolicy>
#include <QSlider>
#include <QSpinBox>
#include <QStringList>
#include <QSplitter>
#include <QTabWidget>
#include <QTabBar>
#include <QTimer>
#include <QTreeWidget>
#include <QDateTime>

#include <cmath>
#include <functional>

using namespace MainWindowSupport;

namespace {

QString artworkUrl(const QJsonObject& obj) {
    QString url = obj.value(QStringLiteral("cover_url")).toString();
    if (url.isEmpty()) url = obj.value(QStringLiteral("cover_thumbnail_url")).toString();
    return url;
}

bool jsonValueMissing(const QJsonValue& value) {
    if (value.isUndefined() || value.isNull()) return true;
    if (value.isString()) return value.toString().trimmed().isEmpty();
    if (value.isDouble()) return value.toDouble() <= 0.0;
    if (value.isArray()) return value.toArray().isEmpty();
    if (value.isObject()) return value.toObject().isEmpty();
    return false;
}

qint64 megabytesToBytes(int megabytes) {
    return megabytes <= 0 ? 0 : static_cast<qint64>(megabytes) * 1024 * 1024;
}

} // namespace

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent),
      m_browser(&m_tidal, this),
      m_lyrics(&m_tidal, this),
      m_playback(&m_tidal, &m_cache, this),
      m_scrobble(&m_playback, &m_settings, this) {
    setWindowTitle(QStringLiteral("TIDAL Bitperfect Qt6"));
    resize(900, 650);
    buildUi();
    setupPlaybackSignals();
    m_browser.setRequireOnlineCallback([this](const QString& action) { return requireOnline(action); });
    m_playback.setRequireOnlineCallback([this](const QString& action) { return requireOnline(action); });
    connect(&m_browser, &BrowserController::statusMessage, this, &MainWindow::setStatus);
    connect(&m_browser, &BrowserController::errorMessage, this, [this](const QString& title, const QString& message) {
        QMessageBox::critical(this, title, message);
    });
    connect(&m_browser, &BrowserController::tracksDiscovered, this, &MainWindow::rememberTracks);
    connect(&m_browser, &BrowserController::favoriteItemsDiscovered, this, &MainWindow::rememberFavoriteItems);
    connect(&m_browser, &BrowserController::objectActionRequested, this, &MainWindow::playOrQueueObject);
    connect(&m_browser, &BrowserController::detailLoaded, this, [this](const QJsonObject& detail, const QString& type, const QString& id) {
        if (type == QStringLiteral("album") && !m_playback.playbackState().busy) {
            const QString coverUrl = artworkUrl(detail);
            QString albumId = detail.value(QStringLiteral("id")).toVariant().toString();
            if (albumId.isEmpty()) albumId = id;
            requestCover(coverUrl, QStringLiteral("album:%1").arg(albumId.isEmpty() ? coverUrl : albumId), albumId);
        }
    });
    connect(&m_lyrics, &LyricsController::seekRequested, this, [this](double seconds) {
        beginSeekPreview(seconds);
        m_playback.seekTo(seconds);
    });
    connect(&m_scrobble, &ScrobbleService::statusMessage, this, &MainWindow::setStatus);
    connect(&m_scrobble, &ScrobbleService::errorMessage, this, &MainWindow::setStatus);
    connect(&m_scrobble, &ScrobbleService::lastFmAuthUrlReady, this, [this](const QUrl& url) {
        QDesktopServices::openUrl(url);
        setStatus(QStringLiteral("Authorize TIDAL Bitperfect on Last.fm, then finish authorization"));
    });
    connect(&m_tidal, &TidalClient::statusMessage, this, &MainWindow::setStatus);
    connect(&m_tidal, &TidalClient::fatalError, this, [this](const QString& msg) {
        if (networkOffline()) enterOfflineMode(msg);
        else QMessageBox::critical(this, QStringLiteral("TIDAL"), msg);
    });
    connect(&m_tidal, &TidalClient::loginLink, this, &MainWindow::tidalLoginLink);
    refreshDevices();
    const QString savedDevice = m_settings.value(QStringLiteral("qt6/alsa_device"), QStringLiteral("default")).toString();
    m_deviceCombo->setCurrentText(savedDevice);
    m_playback.setOutputDevice(savedDevice);
    m_volume->setValue(qBound(0, m_settings.value(QStringLiteral("qt6/volume"), 100).toInt(), 100));
    m_searchLimit->setValue(qBound(1, m_settings.value(QStringLiteral("qt6/search_limit"), 10).toInt(), 50));
    m_playback.setGaplessEnabled(m_settings.value(QStringLiteral("qt6/gapless_enabled"), true).toBool());
    m_playback.setStreamTransitionSmoothing(m_settings.value(QStringLiteral("qt6/stream_transition_smoothing"), false).toBool());
    m_audioCacheEnabled = m_settings.value(QStringLiteral("qt6/audio_cache_enabled"), true).toBool();
    m_coverCacheEnabled = m_settings.value(QStringLiteral("qt6/cover_cache_enabled"), true).toBool();
    m_cacheMode = m_settings.value(QStringLiteral("qt6/cache_mode"), QStringLiteral("balanced")).toString();
    m_audioCacheLimitMb = qMax(0, m_settings.value(QStringLiteral("qt6/audio_cache_limit_mb"), 0).toInt());
    m_coverCacheLimitMb = qMax(0, m_settings.value(QStringLiteral("qt6/cover_cache_limit_mb"), 0).toInt());
    m_playback.setCacheMode(m_cacheMode);
    m_playback.setAudioCacheEnabled(m_audioCacheEnabled);
    m_playback.setAudioCacheLimitBytes(megabytesToBytes(m_audioCacheLimitMb));
    if (m_cache.enforceLimits(megabytesToBytes(m_audioCacheLimitMb), megabytesToBytes(m_coverCacheLimitMb), m_cacheMode)) m_cache.refresh();
    m_reduceAnimations = m_settings.value(QStringLiteral("qt6/reduce_animations"), false).toBool();
    m_lyrics.setReduceAnimations(m_reduceAnimations);
    m_discordEnabled = m_settings.value(QStringLiteral("qt6/discord_enabled"), false).toBool();
    m_discordClientId = m_settings.value(QStringLiteral("qt6/discord_client_id")).toString().trimmed();
    m_mprisEnabled = m_settings.value(QStringLiteral("qt6/mpris_enabled"), true).toBool();
    initDiscord();
    initMpris();
    login();
    refreshCacheTab();
}

void MainWindow::closeEvent(QCloseEvent* event) {
    m_lyrics.stopScrollAnimation();
    shutdownDiscord();
    shutdownMpris();
    m_playback.shutdown();
    QMainWindow::closeEvent(event);
}

void MainWindow::buildUi() {
    auto* root = new QWidget(this);
    auto* main = new QVBoxLayout(root);

    auto* deviceRow = new QHBoxLayout();
    m_deviceCombo = new QComboBox(root);
    m_deviceCombo->setEditable(true);
    auto* refreshDevicesButton = new QPushButton(QStringLiteral("Refresh devices"), root);
    connect(refreshDevicesButton, &QPushButton::clicked, this, &MainWindow::refreshDevices);
    deviceRow->addWidget(new QLabel(QStringLiteral("ALSA device:"), root));
    deviceRow->addWidget(m_deviceCombo, 1);
    deviceRow->addWidget(refreshDevicesButton);
    main->addLayout(deviceRow);

    auto* split = new QSplitter(Qt::Horizontal, root);
    split->setHandleWidth(0);
    m_tabs = new QTabWidget();
    m_tabs->tabBar()->setExpanding(false);
    m_tabs->tabBar()->setUsesScrollButtons(false);
    m_tabs->setMinimumWidth(380);

    m_homeTab = new QWidget(m_tabs);
    auto* homeLayout = new QVBoxLayout(m_homeTab);
    auto* homeHeader = new QHBoxLayout();
    homeHeader->addWidget(new QLabel(QStringLiteral("Home"), m_homeTab));
    homeHeader->addStretch(1);
    auto* homeRefresh = new QPushButton(QStringLiteral("Refresh"), m_homeTab);
    homeHeader->addWidget(homeRefresh);
    homeLayout->addLayout(homeHeader);
    m_homeTree = new QTreeWidget(m_homeTab);
    m_homeTree->setHeaderHidden(true);
    homeLayout->addWidget(m_homeTree, 1);
    setupTreeActions(m_homeTree);
    connect(homeRefresh, &QPushButton::clicked, this, [this]() {
        m_browser.loadHome(m_homeTree);
    });
    m_tabs->addTab(m_homeTab, QStringLiteral("Home"));

    m_searchTab = new QWidget(m_tabs);
    auto* searchLayout = new QVBoxLayout(m_searchTab);
    auto* searchRow = new QHBoxLayout();
    m_searchEdit = new QLineEdit(m_searchTab);
    m_searchEdit->setPlaceholderText(QStringLiteral("Search, e.g. \"aphex twin flim\""));
    m_searchType = new QComboBox(m_searchTab);
    m_searchType->addItems({QStringLiteral("Tracks"), QStringLiteral("Albums"), QStringLiteral("Playlists"), QStringLiteral("Artists")});
    m_searchType->setMinimumWidth(110);
    m_searchType->setSizeAdjustPolicy(QComboBox::AdjustToContentsOnFirstShow);
    m_searchLimit = new QSpinBox(m_searchTab);
    m_searchLimit->setRange(1, 50);
    m_searchLimit->setValue(10);
    auto* searchButton = new QPushButton(QStringLiteral("Search"), m_searchTab);
    searchRow->addWidget(m_searchEdit, 1);
    searchRow->addWidget(new QLabel(QStringLiteral("Type:"), m_searchTab));
    searchRow->addWidget(m_searchType);
    searchRow->addWidget(new QLabel(QStringLiteral("Limit:"), m_searchTab));
    searchRow->addWidget(m_searchLimit);
    searchRow->addWidget(searchButton);
    searchLayout->addLayout(searchRow);
    m_searchTree = new QTreeWidget(m_searchTab);
    m_searchTree->setHeaderHidden(true);
    searchLayout->addWidget(m_searchTree, 1);
    connect(searchButton, &QPushButton::clicked, this, &MainWindow::search);
    connect(m_searchEdit, &QLineEdit::returnPressed, this, &MainWindow::search);
    connect(m_searchLimit, &QSpinBox::valueChanged, this, [this](int value) {
        m_settings.setValue(QStringLiteral("qt6/search_limit"), value);
    });
    setupTreeActions(m_searchTree);
    m_tabs->addTab(m_searchTab, QStringLiteral("Search"));

    m_urlTab = new QWidget(m_tabs);
    auto* urlLayout = new QVBoxLayout(m_urlTab);
    auto* urlRow = new QHBoxLayout();
    m_urlEdit = new QLineEdit(m_urlTab);
    m_urlEdit->setPlaceholderText(QStringLiteral("Paste a TIDAL track/album/playlist URL"));
    auto* urlLoad = new QPushButton(QStringLiteral("Load"), m_urlTab);
    auto* urlQueue = new QPushButton(QStringLiteral("Queue"), m_urlTab);
    urlRow->addWidget(m_urlEdit, 1);
    urlRow->addWidget(urlLoad);
    urlRow->addWidget(urlQueue);
    urlLayout->addLayout(urlRow);
    m_urlTree = new QTreeWidget(m_urlTab);
    m_urlTree->setHeaderHidden(true);
    urlLayout->addWidget(m_urlTree, 1);
    connect(urlLoad, &QPushButton::clicked, this, &MainWindow::loadUrl);
    connect(urlQueue, &QPushButton::clicked, this, &MainWindow::queueUrl);
    connect(m_urlEdit, &QLineEdit::returnPressed, this, &MainWindow::loadUrl);
    setupTreeActions(m_urlTree);
    m_tabs->addTab(m_urlTab, QStringLiteral("URL"));

    m_collectionTab = new QWidget(m_tabs);
    auto* collectionLayout = new QVBoxLayout(m_collectionTab);
    auto* collectionRow = new QHBoxLayout();
    m_collectionType = new QComboBox(m_collectionTab);
    m_collectionType->addItems({QStringLiteral("Tracks"), QStringLiteral("Albums"), QStringLiteral("Playlists"), QStringLiteral("Artists")});
    m_collectionType->setMinimumWidth(110);
    m_collectionType->setSizeAdjustPolicy(QComboBox::AdjustToContentsOnFirstShow);
    auto* collectionRefresh = new QPushButton(QStringLiteral("Refresh"), m_collectionTab);
    collectionRow->addWidget(new QLabel(QStringLiteral("Collection"), m_collectionTab));
    collectionRow->addWidget(m_collectionType);
    collectionRow->addStretch(1);
    collectionRow->addWidget(collectionRefresh);
    collectionLayout->addLayout(collectionRow);
    m_collectionTree = new QTreeWidget(m_collectionTab);
    m_collectionTree->setHeaderHidden(true);
    collectionLayout->addWidget(m_collectionTree, 1);
    connect(collectionRefresh, &QPushButton::clicked, this, &MainWindow::refreshCollection);
    connect(m_collectionType, &QComboBox::currentTextChanged, this, &MainWindow::refreshCollection);
    setupTreeActions(m_collectionTree);
    m_tabs->addTab(m_collectionTab, QStringLiteral("Collection"));

    m_cacheTab = new QWidget(m_tabs);
    auto* cacheLayout = new QVBoxLayout(m_cacheTab);

    auto* cacheGroup = new QGroupBox(QStringLiteral("Cache"), m_cacheTab);
    auto* cacheGroupLayout = new QVBoxLayout(cacheGroup);
    auto* cacheTop = new QHBoxLayout();
    m_cacheStatusLabel = new QLabel(QStringLiteral("Tracks: 0"), cacheGroup);
    m_cacheStatusLabel->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    auto* cacheQueue = new QPushButton(QStringLiteral("Queue"), cacheGroup);
    auto* cacheClear = new QPushButton(QStringLiteral("Clear"), cacheGroup);
    cacheTop->addWidget(m_cacheStatusLabel, 1);
    cacheTop->addWidget(cacheQueue);
    cacheTop->addWidget(cacheClear);
    m_cacheList = new QListWidget(m_cacheTab);
    cacheGroupLayout->addLayout(cacheTop);
    cacheGroupLayout->addWidget(m_cacheList, 1);

    auto* downloadsGroup = new QGroupBox(QStringLiteral("Downloads"), m_cacheTab);
    auto* downloadsGroupLayout = new QVBoxLayout(downloadsGroup);
    auto* downloadsTop = new QHBoxLayout();
    auto* openDownloads = new QPushButton(QStringLiteral("Open folder"), downloadsGroup);
    m_downloadsStatusLabel = new QLabel(QStringLiteral("Tracks: 0"), downloadsGroup);
    m_downloadsStatusLabel->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    auto* downloadsQueue = new QPushButton(QStringLiteral("Queue"), downloadsGroup);
    auto* downloadsClear = new QPushButton(QStringLiteral("Clear"), downloadsGroup);
    downloadsTop->addWidget(openDownloads);
    downloadsTop->addWidget(m_downloadsStatusLabel, 1);
    downloadsTop->addWidget(downloadsQueue);
    downloadsTop->addWidget(downloadsClear);
    m_downloadList = new QListWidget(m_cacheTab);
    downloadsGroupLayout->addLayout(downloadsTop);
    downloadsGroupLayout->addWidget(m_downloadList, 1);

    cacheLayout->addWidget(cacheGroup, 1);
    cacheLayout->addWidget(downloadsGroup, 1);
    connect(cacheQueue, &QPushButton::clicked, this, &MainWindow::queueCachedTracks);
    connect(cacheClear, &QPushButton::clicked, this, &MainWindow::clearCachedTracks);
    connect(openDownloads, &QPushButton::clicked, this, [this]() {
        QDir().mkpath(m_cache.downloadsDir());
        QDesktopServices::openUrl(QUrl::fromLocalFile(m_cache.downloadsDir()));
    });
    connect(downloadsQueue, &QPushButton::clicked, this, &MainWindow::queueDownloadedTracks);
    connect(downloadsClear, &QPushButton::clicked, this, &MainWindow::clearDownloadedTracks);
    connect(m_cacheList, &QListWidget::itemDoubleClicked, this, [this](QListWidgetItem* item) { startPlayback(item->data(Qt::UserRole).toJsonObject()); });
    connect(m_downloadList, &QListWidget::itemDoubleClicked, this, [this](QListWidgetItem* item) { startPlayback(item->data(Qt::UserRole).toJsonObject()); });
    setupListActions(m_cacheList);
    setupListActions(m_downloadList);
    m_tabs->addTab(m_cacheTab, QStringLiteral("Cache"));

    auto* leftPanel = new QWidget(split);
    auto* leftLayout = new QVBoxLayout(leftPanel);
    leftLayout->setContentsMargins(0, 0, 0, 0);
    leftLayout->addWidget(m_tabs, 1);
    split->addWidget(leftPanel);

    auto* right = new QWidget(split);
    right->setMinimumWidth(430);
    auto* rightLayout = new QVBoxLayout(right);
    rightLayout->setContentsMargins(4, 0, 0, 0);
    m_cover = new CoverLabel(right);
    if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setFallbackPixmap(fallbackCoverPixmap());
    m_cover->setAlignment(Qt::AlignCenter);
    m_cover->setFrameShape(QFrame::StyledPanel);
    m_title = new QLabel(QStringLiteral("Nothing playing"), right);
    m_title->setWordWrap(true);
    QFont titleFont = m_title->font();
    titleFont.setPointSize(titleFont.pointSize() + 2);
    titleFont.setBold(true);
    m_title->setFont(titleFont);
    m_meta = new QLabel(QStringLiteral("—"), right);
    m_quality = new QLabel(QStringLiteral("Quality: —"), right);
    m_bitrate = new QLabel(QStringLiteral("Bitrate: —"), right);
    m_bitperfect = new QLabel(QStringLiteral("Bit-perfect: —"), right);
    auto addNowPlayingMenu = [this](QWidget* widget) {
        widget->setContextMenuPolicy(Qt::CustomContextMenu);
        connect(widget, &QWidget::customContextMenuRequested, this, [this, widget](const QPoint& pos) {
            const QJsonObject track = currentTrackObject();
            const QString id = track.value(QStringLiteral("id")).toVariant().toString();
            if (id.isEmpty()) return;
            const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
            const QString artistId = track.value(QStringLiteral("artist_id")).toVariant().toString();
            QMenu menu(this);
            menu.addAction(QStringLiteral("Play"), this, [this, track]() { startPlayback(track); });
            menu.addAction(QStringLiteral("Play next"), this, [this, track]() { insertQueueNext(track); });
            menu.addAction(QStringLiteral("Append to queue"), this, [this, track]() { appendQueue(track); });
            menu.addSeparator();
            QAction* playRadio = menu.addAction(QStringLiteral("Play radio"), this, [this, track]() { requestRadioForObject(track, true); });
            playRadio->setEnabled(!m_offlineMode);
            QAction* queueRadio = menu.addAction(QStringLiteral("Queue radio"), this, [this, track]() { requestRadioForObject(track, false); });
            queueRadio->setEnabled(!m_offlineMode);
            menu.addSeparator();
            addFavoriteAction(&menu, QStringLiteral("track"), id, track);
            menu.addAction(QStringLiteral("Copy track link"), this, [this, id]() { copyTidalLink(QStringLiteral("track"), id); });
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, track]() { openTrackAlbum(track); });
            openAlbum->setEnabled(!m_offlineMode && (!albumId.isEmpty() || !id.isEmpty()));
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, track]() { openTrackArtist(track); });
            openArtist->setEnabled(!m_offlineMode && (!artistId.isEmpty() || !id.isEmpty()));
            menu.addSeparator();
            addTrackStorageAction(&menu, track);
            menu.exec(widget->mapToGlobal(pos));
        });
    };
    addNowPlayingMenu(m_cover);
    addNowPlayingMenu(m_title);
    addNowPlayingMenu(m_meta);
    addNowPlayingMenu(m_quality);
    addNowPlayingMenu(m_bitrate);
    addNowPlayingMenu(m_bitperfect);
    auto* lyricsPanel = new QWidget(right);
    auto* lyricsLayout = new QVBoxLayout(lyricsPanel);
    lyricsLayout->setContentsMargins(10, 10, 6, 0);
    lyricsLayout->setSpacing(8);
    auto* lyricsTitle = new QLabel(QStringLiteral("Lyrics"), lyricsPanel);
    QFont lyricsTitleFont = lyricsTitle->font();
    lyricsTitleFont.setPointSize(lyricsTitleFont.pointSize() + 3);
    lyricsTitleFont.setBold(true);
    lyricsTitle->setFont(lyricsTitleFont);
    lyricsTitle->setWordWrap(true);
    auto* lyricsMeta = new QLabel(QString(), lyricsPanel);
    lyricsMeta->setWordWrap(true);
    lyricsMeta->setTextInteractionFlags(Qt::TextSelectableByMouse);
    auto* lyricsDivider = new QFrame(lyricsPanel);
    lyricsDivider->setFrameShape(QFrame::HLine);
    lyricsDivider->setFrameShadow(QFrame::Plain);
    lyricsDivider->setStyleSheet(QStringLiteral("color: rgba(255, 255, 255, 0.08);"));
    auto* lyricsList = new QListWidget(lyricsPanel);
    lyricsList->setFrameShape(QFrame::NoFrame);
    lyricsList->setWordWrap(true);
    lyricsList->setSelectionMode(QAbstractItemView::NoSelection);
    lyricsList->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    lyricsList->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
    lyricsList->setFocusPolicy(Qt::NoFocus);
    lyricsList->setStyleSheet(QStringLiteral(
        "QListWidget { background: transparent; border: 0; }"
        "QListWidget::item { padding: 5px 2px; border-radius: 3px; }"
        "QListWidget::item:hover { background: rgba(255, 255, 255, 0.05); }"
        "QListWidget::item:selected { background: transparent; }"
        "QListWidget::item:focus { outline: none; }"
    ));
    QFont lyricsFont = lyricsList->font();
    lyricsFont.setPointSize(qMax(lyricsFont.pointSize() + 1, 13));
    lyricsList->setFont(lyricsFont);
    m_lyrics.setWidgets(lyricsTitle, lyricsMeta, lyricsList);
    lyricsLayout->addWidget(lyricsTitle);
    lyricsLayout->addWidget(lyricsMeta);
    lyricsLayout->addWidget(lyricsDivider);
    lyricsLayout->addWidget(lyricsList, 1);
    m_queueList = new QListWidget(right);
    m_detailsTabs = new QTabWidget(right);
    m_detailsTabs->setDocumentMode(true);
    auto* now = new QWidget(m_detailsTabs);
    auto* nowLayout = new QVBoxLayout(now);
    nowLayout->setContentsMargins(0, 0, 0, 0);
    auto* nowFrame = new QFrame(now);
    nowFrame->setObjectName(QStringLiteral("nowPlaying"));
    nowFrame->setFrameShape(QFrame::StyledPanel);
    nowFrame->setFrameShadow(QFrame::Raised);
    addNowPlayingMenu(nowFrame);
    auto* nowFrameLayout = new QVBoxLayout(nowFrame);
    nowFrameLayout->setSpacing(8);
    nowFrameLayout->addWidget(m_cover, 1);
    nowFrameLayout->addWidget(m_title);
    nowFrameLayout->addWidget(m_meta);
    auto* nowMetaRow = new QHBoxLayout();
    auto* nowMetaLeft = new QVBoxLayout();
    nowMetaLeft->addWidget(m_quality);
    nowMetaLeft->addWidget(m_bitrate);
    nowMetaLeft->addWidget(m_bitperfect);
    nowMetaRow->addLayout(nowMetaLeft);
    nowMetaRow->addStretch(1);
    nowFrameLayout->addLayout(nowMetaRow);
    nowLayout->addWidget(nowFrame, 1);
    m_detailsTabs->addTab(now, QStringLiteral("Now Playing"));
    const int lyricsTabIndex = m_detailsTabs->addTab(lyricsPanel, QStringLiteral("Lyrics"));
    connect(m_detailsTabs, &QTabWidget::currentChanged, this, [this, lyricsTabIndex](int index) {
        if (index != lyricsTabIndex) return;
        QTimer::singleShot(0, this, [this, lyricsTabIndex]() {
            if (!m_detailsTabs || m_detailsTabs->currentIndex() != lyricsTabIndex) return;
            m_lyrics.updatePosition(m_playback.playbackState().positionSeconds);
            m_lyrics.scrollToCurrentLine(false);
        });
    });
    m_queueTabIndex = m_detailsTabs->addTab(m_queueList, QStringLiteral("Queue"));
    connect(m_queueList, &QListWidget::itemDoubleClicked, this, [this](QListWidgetItem* item) {
        playQueueRow(item ? m_queueList->row(item) : -1);
    });
    m_queueList->setContextMenuPolicy(Qt::CustomContextMenu);
    connect(m_queueList, &QWidget::customContextMenuRequested, this, [this](const QPoint& pos) {
        QListWidgetItem* item = m_queueList->itemAt(pos);
        QMenu menu(this);
        if (item) {
            m_queueList->setCurrentItem(item);
            const QJsonObject track = item->data(Qt::UserRole).toJsonObject();
            const QString id = track.value(QStringLiteral("id")).toVariant().toString();
            const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
            const QString artistId = track.value(QStringLiteral("artist_id")).toVariant().toString();
            menu.addAction(QStringLiteral("Play"), this, [this, item]() {
                playQueueRow(m_queueList->row(item));
            });
            menu.addAction(QStringLiteral("Play next"), this, [this, item]() {
                moveQueueRowToNext(m_queueList->row(item));
            });
            menu.addAction(QStringLiteral("Remove from queue"), this, [this, item]() {
                removeQueueRow(m_queueList->row(item));
            });
            menu.addSeparator();
            QAction* playRadio = menu.addAction(QStringLiteral("Play radio"), this, [this, track]() { requestRadioForObject(track, true); });
            playRadio->setEnabled(!m_offlineMode);
            QAction* queueRadio = menu.addAction(QStringLiteral("Queue radio"), this, [this, track]() { requestRadioForObject(track, false); });
            queueRadio->setEnabled(!m_offlineMode);
            menu.addSeparator();
            addFavoriteAction(&menu, QStringLiteral("track"), id, track);
            menu.addAction(QStringLiteral("Copy track link"), this, [this, id]() { copyTidalLink(QStringLiteral("track"), id); });
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, track]() { openTrackAlbum(track); });
            openAlbum->setEnabled(!m_offlineMode && (!albumId.isEmpty() || !id.isEmpty()));
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, track]() { openTrackArtist(track); });
            openArtist->setEnabled(!m_offlineMode && (!artistId.isEmpty() || !id.isEmpty()));
            menu.addSeparator();
            addTrackStorageAction(&menu, track);
        }
        if (!item && !m_playback.queueEmpty()) menu.addAction(QStringLiteral("Play next"), this, &MainWindow::playNextQueued);
        menu.addAction(QStringLiteral("Clear queue"), this, &MainWindow::clearQueue);
        menu.exec(m_queueList->viewport()->mapToGlobal(pos));
    });
    rightLayout->addWidget(m_detailsTabs, 1);

    auto* controls = new QHBoxLayout();
    m_pauseButton = new QPushButton(QStringLiteral("Play"), right);
    auto* stop = new QPushButton(QStringLiteral("Stop"), right);
    auto* skip = new QPushButton(QStringLiteral("Skip"), right);
    for (QPushButton* button : {m_pauseButton, stop, skip}) {
        button->setMinimumWidth(72);
        button->setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Fixed);
    }
    controls->addWidget(m_pauseButton);
    controls->addWidget(stop);
    controls->addWidget(skip);
    m_time = new QLabel(QStringLiteral("0:00 / 0:00"), right);
    m_time->setAlignment(Qt::AlignHCenter | Qt::AlignVCenter);
    m_time->setMinimumWidth(128);
    m_time->setTextInteractionFlags(Qt::TextSelectableByMouse);
    QFont timeFont = m_time->font();
    timeFont.setPointSize(qMax(timeFont.pointSize() + 2, 13));
    timeFont.setBold(true);
    timeFont.setStyleHint(QFont::Monospace);
    m_time->setFont(timeFont);
    auto* timeWrap = new QWidget(right);
    timeWrap->setMinimumHeight(32);
    timeWrap->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    auto* timeLayout = new QHBoxLayout(timeWrap);
    timeLayout->setContentsMargins(8, 2, 8, 2);
    timeLayout->addStretch(1);
    timeLayout->addWidget(m_time);
    timeLayout->addStretch(1);
    controls->addWidget(timeWrap, 1);
    rightLayout->addLayout(controls);
    m_seek = new QSlider(Qt::Horizontal, right);
    m_seek->setEnabled(false);
    rightLayout->addWidget(m_seek);

    m_status = new QLabel(QStringLiteral("Status: starting…"), right);
    m_status->setTextInteractionFlags(Qt::TextSelectableByMouse);
    rightLayout->addWidget(m_status);

    auto* volumeRow = new QHBoxLayout();
    volumeRow->setSpacing(6);
    auto* settings = new QPushButton(QStringLiteral("Settings"), right);
    connect(settings, &QPushButton::clicked, this, &MainWindow::showSettingsDialog);
    volumeRow->addWidget(settings);
    m_volume = new QSlider(Qt::Horizontal, right);
    m_volume->setRange(0, 100);
    volumeRow->addWidget(m_volume, 1);
    m_volumeLabel = new QLabel(QStringLiteral("100%"), right);
    m_volumeLabel->setFixedWidth(35);
    m_volumeLabel->setAlignment(Qt::AlignCenter | Qt::AlignVCenter);
    volumeRow->addWidget(m_volumeLabel);
    rightLayout->addLayout(volumeRow);

    connect(m_pauseButton, &QPushButton::clicked, this, &MainWindow::togglePause);
    connect(stop, &QPushButton::clicked, this, &MainWindow::stopPlayback);
    connect(skip, &QPushButton::clicked, this, &MainWindow::playNextQueued);
    connect(m_seek, &QSlider::sliderReleased, this, &MainWindow::seekReleased);
    connect(m_volume, &QSlider::valueChanged, this, &MainWindow::volumeChanged);
    connect(m_deviceCombo, &QComboBox::currentTextChanged, this, [this]() {
        m_settings.setValue(QStringLiteral("qt6/alsa_device"), m_deviceCombo->currentText());
        m_playback.setOutputDevice(m_deviceCombo->currentText());
    });

    auto addShortcut = [this](const QKeySequence& sequence, const std::function<void()>& handler) {
        auto* shortcut = new QShortcut(sequence, this);
        connect(shortcut, &QShortcut::activated, this, handler);
    };
    addShortcut(QKeySequence(QStringLiteral("Ctrl+1")), [this]() { m_tabs->setCurrentWidget(m_homeTab); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+2")), [this]() { m_tabs->setCurrentWidget(m_searchTab); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+3")), [this]() { m_tabs->setCurrentWidget(m_urlTab); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+4")), [this]() { m_tabs->setCurrentWidget(m_collectionTab); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+5")), [this]() { m_tabs->setCurrentWidget(m_cacheTab); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+F")), [this]() { m_tabs->setCurrentWidget(m_searchTab); m_searchEdit->setFocus(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+L")), [this]() { m_tabs->setCurrentWidget(m_urlTab); m_urlEdit->setFocus(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Return")), [this]() { togglePause(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Enter")), [this]() { togglePause(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Space")), [this]() { togglePause(); });
    addShortcut(QKeySequence(QStringLiteral("K")), [this]() { if (!textInputFocused()) togglePause(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Shift+Return")), [this]() { playNextQueued(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Shift+Enter")), [this]() { playNextQueued(); });
    addShortcut(QKeySequence(QStringLiteral("F5")), [this]() { refreshDevices(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+R")), [this]() { refreshDevices(); });
    addShortcut(QKeySequence(QStringLiteral("Esc")), [this]() { if (!textInputFocused()) stopPlayback(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+.")), [this]() { stopPlayback(); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Left")), [this]() { m_playback.seek(-10.0); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Right")), [this]() { m_playback.seek(10.0); });
    addShortcut(QKeySequence(QStringLiteral("J")), [this]() { if (!textInputFocused()) m_playback.seek(-10.0); });
    addShortcut(QKeySequence(QStringLiteral("L")), [this]() { if (!textInputFocused()) m_playback.seek(10.0); });

    split->addWidget(right);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    split->setSizes({680, 440});
    main->addWidget(split, 1);
    setCentralWidget(root);
}

void MainWindow::setupTreeActions(QTreeWidget* tree) {
    connect(tree, &QTreeWidget::itemActivated, this, &MainWindow::itemActivated);
    connect(tree, &QTreeWidget::itemExpanded, &m_browser, &BrowserController::onTreeItemExpanded);
    connect(tree, &QTreeWidget::currentItemChanged, this, [this](QTreeWidgetItem*, QTreeWidgetItem*) {
        loadCoverForSelected();
    });
    tree->setContextMenuPolicy(Qt::CustomContextMenu);
    connect(tree, &QWidget::customContextMenuRequested, this, [this, tree](const QPoint& pos) {
        QTreeWidgetItem* item = tree->itemAt(pos);
        if (!item) return;
        tree->setCurrentItem(item);
        const QJsonObject obj = itemObject(item);
        const QString type = obj.value(QStringLiteral("_type")).toString();
        const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
        QMenu menu(this);
        if (type == QStringLiteral("track")) {
            const QString albumId = obj.value(QStringLiteral("album_id")).toVariant().toString();
            const QString artistId = obj.value(QStringLiteral("artist_id")).toVariant().toString();
            menu.addAction(QStringLiteral("Play"), this, [this, obj]() { startPlayback(obj); });
            menu.addAction(QStringLiteral("Play next"), this, [this, obj]() { insertQueueNext(obj); });
            menu.addAction(QStringLiteral("Append to queue"), this, [this, obj]() { appendQueue(obj); });
            menu.addSeparator();
            QAction* playRadio = menu.addAction(QStringLiteral("Play radio"), this, [this, obj]() { requestRadioForObject(obj, true); });
            playRadio->setEnabled(!m_offlineMode);
            QAction* queueRadio = menu.addAction(QStringLiteral("Queue radio"), this, [this, obj]() { requestRadioForObject(obj, false); });
            queueRadio->setEnabled(!m_offlineMode);
            menu.addSeparator();
            addFavoriteAction(&menu, QStringLiteral("track"), id, obj);
            menu.addAction(QStringLiteral("Copy track link"), this, [this, id]() { copyTidalLink(QStringLiteral("track"), id); });
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, obj]() { openTrackAlbum(obj); });
            openAlbum->setEnabled(!m_offlineMode && (!albumId.isEmpty() || !id.isEmpty()));
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, obj]() { openTrackArtist(obj); });
            openArtist->setEnabled(!m_offlineMode && (!artistId.isEmpty() || !id.isEmpty()));
            menu.addSeparator();
            addTrackStorageAction(&menu, obj);
            menu.exec(tree->viewport()->mapToGlobal(pos));
            return;
        }
        if (type == QStringLiteral("top_tracks_group")) {
            menu.addAction(QStringLiteral("Play top tracks"), this, [this, obj]() { playOrQueueObject(obj, true); });
            menu.addAction(QStringLiteral("Queue top tracks"), this, [this, obj]() { playOrQueueObject(obj, false); });
            menu.exec(tree->viewport()->mapToGlobal(pos));
            return;
        }
        if (isContainerType(type)) {
            menu.addAction(QStringLiteral("Play %1").arg(type), this, &MainWindow::playSelected);
            menu.addAction(QStringLiteral("Queue %1").arg(type), this, &MainWindow::queueSelected);
            if (itemNeedsDetails(item)) {
                menu.addAction(QStringLiteral("Load details"), this, [this, item]() { loadContainerDetails(item); });
            }
            if (type == QStringLiteral("artist")) {
                menu.addSeparator();
                QAction* playRadio = menu.addAction(QStringLiteral("Play radio"), this, [this, obj]() { requestRadioForObject(obj, true); });
                playRadio->setEnabled(!m_offlineMode);
                QAction* queueRadio = menu.addAction(QStringLiteral("Queue radio"), this, [this, obj]() { requestRadioForObject(obj, false); });
                queueRadio->setEnabled(!m_offlineMode);
            }
            menu.addSeparator();
            if (type != QStringLiteral("mix")) addFavoriteAction(&menu, type, id, obj);
            menu.addAction(QStringLiteral("Copy %1 link").arg(type), this, [this, type, id]() { copyTidalLink(type, id); });
            QAction* openItem = menu.addAction(QStringLiteral("Open %1").arg(type), this, [this, type, id]() { openTidalItem(type, id); });
            openItem->setEnabled(!m_offlineMode && !id.isEmpty());
            if (type == QStringLiteral("album")) {
                const QString artistId = obj.value(QStringLiteral("artist_id")).toVariant().toString();
                QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, artistId]() { openTidalItem(QStringLiteral("artist"), artistId); });
                openArtist->setEnabled(!m_offlineMode && !artistId.isEmpty());
            }
            menu.exec(tree->viewport()->mapToGlobal(pos));
            return;
        }
    });
}

void MainWindow::setupListActions(QListWidget* list) {
    connect(list, &QListWidget::itemSelectionChanged, this, [this, list]() {
        if (list->currentItem()) m_lastTrackList = list;
        loadCoverForSelected();
    });
    list->setContextMenuPolicy(Qt::CustomContextMenu);
    connect(list, &QWidget::customContextMenuRequested, this, [this, list](const QPoint& pos) {
        QListWidgetItem* item = list->itemAt(pos);
        if (!item) return;
        list->setCurrentItem(item);
        const QJsonObject track = item->data(Qt::UserRole).toJsonObject();
        const QString id = track.value(QStringLiteral("id")).toVariant().toString();
        if (track.isEmpty()) return;
        const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
        const QString artistId = track.value(QStringLiteral("artist_id")).toVariant().toString();
        QMenu menu(this);
        menu.addAction(QStringLiteral("Play"), this, [this, track]() { startPlayback(track); });
        menu.addAction(QStringLiteral("Play next"), this, [this, track]() { insertQueueNext(track); });
        menu.addAction(QStringLiteral("Append to queue"), this, [this, track]() { appendQueue(track); });
        menu.addSeparator();
        QAction* playRadio = menu.addAction(QStringLiteral("Play radio"), this, [this, track]() { requestRadioForObject(track, true); });
        playRadio->setEnabled(!m_offlineMode);
        QAction* queueRadio = menu.addAction(QStringLiteral("Queue radio"), this, [this, track]() { requestRadioForObject(track, false); });
        queueRadio->setEnabled(!m_offlineMode);
        menu.addSeparator();
        addFavoriteAction(&menu, QStringLiteral("track"), id, track);
        menu.addAction(QStringLiteral("Copy track link"), this, [this, id]() { copyTidalLink(QStringLiteral("track"), id); });
        QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, track]() { openTrackAlbum(track); });
        openAlbum->setEnabled(!m_offlineMode && (!albumId.isEmpty() || !id.isEmpty()));
        QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, track]() { openTrackArtist(track); });
        openArtist->setEnabled(!m_offlineMode && (!artistId.isEmpty() || !id.isEmpty()));
        menu.addSeparator();
        addTrackStorageAction(&menu, track);
        menu.exec(list->viewport()->mapToGlobal(pos));
    });
}

void MainWindow::refreshDevices() {
    const QString current = m_deviceCombo->currentText();
    QStringList devices = playbackDevices();
    if (!current.isEmpty() && !devices.contains(current)) {
        devices.prepend(current);
    }
    const QSignalBlocker blocker(m_deviceCombo);
    m_deviceCombo->clear();
    m_deviceCombo->addItems(devices);
    if (!current.isEmpty()) {
        m_deviceCombo->setCurrentText(current);
    }
    updateNetworkMode();
}

void MainWindow::login() {
    if (networkOffline()) {
        enterOfflineMode(QStringLiteral("Offline detected; cache playback only."));
        return;
    }
    m_offlineMode = false;
    setNetworkTabsEnabled(true);
    setStatus(QStringLiteral("Logging in..."));
    m_tidal.request(QStringLiteral("login"), {}, [this](const QJsonObject&) {
        m_offlineMode = false;
        setNetworkTabsEnabled(true);
        setStatus(QStringLiteral("Ready"));
        m_browser.loadHome(m_homeTree);
        refreshFavoriteState();
        refreshCollection();
    }, [this](const QString& error) {
        if (networkOffline()) enterOfflineMode(error);
        else QMessageBox::critical(this, QStringLiteral("Login"), error);
    });
}

void MainWindow::tidalLoginLink(const QString& url, const QString& code, int expiresSeconds) {
    setStatus(QStringLiteral("Authorize TIDAL login: %1").arg(code));
    QMessageBox::information(this, QStringLiteral("TIDAL Login"), QStringLiteral("Open this URL and authorize:\n%1\n\nCode: %2\nExpires in %3s").arg(url, code).arg(expiresSeconds));
    QDesktopServices::openUrl(QUrl(url));
}

void MainWindow::search() {
    m_browser.search(m_searchTree, m_searchEdit->text(), m_searchType->currentText(), m_searchLimit->value());
}

void MainWindow::loadUrl() {
    m_browser.loadUrl(m_urlTree, m_urlEdit->text());
}

void MainWindow::queueUrl() {
    m_browser.loadUrl(m_urlTree, m_urlEdit->text(), true);
}

void MainWindow::refreshCollection() {
    m_browser.refreshCollection(m_collectionTree, m_collectionType->currentText());
}

QJsonObject MainWindow::itemObject(QTreeWidgetItem* item) const {
    return m_browser.itemObject(item);
}

bool MainWindow::itemNeedsDetails(QTreeWidgetItem* item) const {
    return m_browser.itemNeedsDetails(item);
}

QTreeWidget* MainWindow::activeTree() const {
    QWidget* current = m_tabs->currentWidget();
    if (current == m_homeTab) return m_homeTree;
    if (current == m_searchTab) return m_searchTree;
    if (current == m_urlTab) return m_urlTree;
    if (current == m_collectionTab) return m_collectionTree;
    return nullptr;
}

QTreeWidgetItem* MainWindow::currentBrowseItem() const {
    QTreeWidget* tree = activeTree();
    return tree ? tree->currentItem() : nullptr;
}

QJsonObject MainWindow::selectedObject() const {
    const QJsonObject listObject = selectedListObject();
    if (!listObject.isEmpty()) return listObject;
    const QJsonObject treeObject = itemObject(currentBrowseItem());
    if (!treeObject.isEmpty()) return treeObject;
    return currentTrackObject();
}

QJsonObject MainWindow::selectedListObject() const {
    if (m_queueList && m_queueList->hasFocus() && m_queueList->currentItem()) {
        return m_queueList->currentItem()->data(Qt::UserRole).toJsonObject();
    }
    if (m_tabs->currentWidget() != m_cacheTab) return QJsonObject{};
    if (m_lastTrackList && m_lastTrackList->currentItem()) {
        return m_lastTrackList->currentItem()->data(Qt::UserRole).toJsonObject();
    }
    if (m_downloadList && m_downloadList->hasFocus() && m_downloadList->currentItem()) {
        return m_downloadList->currentItem()->data(Qt::UserRole).toJsonObject();
    }
    if (m_cacheList && m_cacheList->currentItem()) {
        return m_cacheList->currentItem()->data(Qt::UserRole).toJsonObject();
    }
    if (m_downloadList && m_downloadList->currentItem()) {
        return m_downloadList->currentItem()->data(Qt::UserRole).toJsonObject();
    }
    return QJsonObject{};
}

QJsonObject MainWindow::currentTrackObject() const {
    return m_playback.playbackState().track;
}

QJsonObject MainWindow::selectedTrack() const {
    QJsonObject obj = selectedObject();
    if (isTrackObject(obj)) return obj;
    return QJsonObject{};
}

void MainWindow::itemActivated(QTreeWidgetItem* item, int column) {
    Q_UNUSED(column);
    QJsonObject obj = itemObject(item);
    const QString type = obj.value(QStringLiteral("_type")).toString();
    if (type == QStringLiteral("track")) {
        startPlayback(obj);
    } else if (isContainerType(type) && itemNeedsDetails(item)) {
        loadContainerDetails(item);
    }
}

void MainWindow::playSelected() {
    QJsonObject obj = selectedObject();
    QTreeWidgetItem* item = currentBrowseItem();
    if (trackObjects(obj).isEmpty() && isContainerType(obj.value(QStringLiteral("_type")).toString())) {
        loadContainerDetails(item, true, false);
        return;
    }
    playOrQueueObject(obj, true);
}

void MainWindow::queueSelected() {
    QJsonObject obj = selectedObject();
    QTreeWidgetItem* item = currentBrowseItem();
    if (trackObjects(obj).isEmpty() && isContainerType(obj.value(QStringLiteral("_type")).toString())) {
        loadContainerDetails(item, false, true);
        return;
    }
    playOrQueueObject(obj, false);
}

void MainWindow::loadContainerDetails(QTreeWidgetItem* item, bool playAfterLoad, bool queueAfterLoad) {
    m_browser.loadContainerDetails(item, playAfterLoad, queueAfterLoad);
}

void MainWindow::playOrQueueObject(const QJsonObject& obj, bool playFirst) {
    const QVector<QJsonObject> tracks = trackObjects(obj);
    if (tracks.isEmpty()) return;
    if (playFirst) {
        startPlayback(tracks.first());
        for (qsizetype i = 1; i < tracks.size(); ++i) appendQueue(tracks.at(i));
    } else {
        for (const QJsonObject& track : tracks) appendQueue(track);
    }
}

void MainWindow::downloadSelected() {
    QJsonObject track = selectedTrack();
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) {
        setStatus(QStringLiteral("Select a track to download"));
        return;
    }
    downloadOrDeleteTrack(track);
}

void MainWindow::radioSelected() {
    QJsonObject obj = selectedObject();
    requestRadioForObject(obj, false);
}

bool MainWindow::requireOnline(const QString& action) {
    if (!m_offlineMode && !networkOffline()) return true;
    enterOfflineMode(QStringLiteral("%1 unavailable while offline.").arg(action));
    return false;
}

void MainWindow::updateNetworkMode() {
    if (networkOffline()) {
        enterOfflineMode(QStringLiteral("Offline detected; cache playback only."));
        return;
    }
    if (m_offlineMode) {
        m_offlineMode = false;
        m_playback.setOfflineMode(false);
        setNetworkTabsEnabled(true);
        updateDiscordContext();
        setStatus(QStringLiteral("Network restored; logging in..."));
        login();
    }
}

void MainWindow::enterOfflineMode(const QString& reason) {
    m_offlineMode = true;
    m_playback.setOfflineMode(true);
    setNetworkTabsEnabled(false);
    if (m_tabs && m_cacheTab) m_tabs->setCurrentWidget(m_cacheTab);
    refreshCacheTab();
    updateDiscordContext();
    setStatus(reason.isEmpty() ? QStringLiteral("Offline mode (cache only)") : reason);
}

void MainWindow::setNetworkTabsEnabled(bool enabled) {
    if (!m_tabs) return;
    for (QWidget* tab : {m_homeTab, m_searchTab, m_urlTab, m_collectionTab}) {
        const int index = m_tabs->indexOf(tab);
        if (index >= 0) m_tabs->setTabEnabled(index, enabled);
    }
    const int cacheIndex = m_tabs->indexOf(m_cacheTab);
    if (cacheIndex >= 0) m_tabs->setTabEnabled(cacheIndex, true);
}

void MainWindow::syncPlaybackOutput() {
    if (m_deviceCombo) m_playback.setOutputDevice(m_deviceCombo->currentText());
    if (m_volume) m_playback.setVolume(m_volume->value());
}

void MainWindow::requestRadioForObject(const QJsonObject& obj, bool playFirst) {
    if (!requireOnline(QStringLiteral("Radio"))) return;
    const QString type = obj.value(QStringLiteral("_type")).toString(QStringLiteral("track"));
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty() || !(type == QStringLiteral("track") || type == QStringLiteral("artist"))) {
        setStatus(QStringLiteral("Radio is available for tracks and artists"));
        return;
    }
    QJsonObject args;
    if (type == QStringLiteral("artist")) args.insert(QStringLiteral("artist_id"), id);
    else args.insert(QStringLiteral("track_id"), id);
    setStatus(QStringLiteral("Loading radio..."));
    m_tidal.request(QStringLiteral("radio"), args, [this, playFirst](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        rememberTracks(items);
        QVector<QJsonObject> tracks;
        for (const QJsonValue& value : items) if (value.isObject()) tracks.push_back(value.toObject());
        if (tracks.isEmpty()) {
            setStatus(QStringLiteral("Radio returned no tracks"));
            return;
        }
        if (playFirst) {
            clearQueue();
            const PlaybackState state = m_playback.playbackState();
            if (state.busy || state.hasTrack()) {
                for (const QJsonObject& track : tracks) appendQueue(track);
            } else {
                startPlayback(tracks.first());
                for (qsizetype i = 1; i < tracks.size(); ++i) appendQueue(tracks.at(i));
            }
        } else {
            const PlaybackState state = m_playback.playbackState();
            const bool shouldStart = !state.hasTrack() && !state.busy;
            for (const QJsonObject& track : tracks) appendQueue(track);
            if (shouldStart) playNextQueued();
        }
        setStatus(QStringLiteral("Radio loaded"));
    }, [this](const QString& error) { QMessageBox::critical(this, QStringLiteral("Radio"), error); });
}

void MainWindow::toggleFavoriteSelected() {
    QJsonObject obj = selectedObject();
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    const QString type = obj.value(QStringLiteral("_type")).toString(QStringLiteral("track"));
    if (id.isEmpty() || !(type == QStringLiteral("track") || type == QStringLiteral("album") || type == QStringLiteral("playlist") || type == QStringLiteral("artist"))) {
        setStatus(QStringLiteral("Select a track, album, playlist, or artist"));
        return;
    }
    toggleFavorite(type, id, !isFavoriteItem(type, id, obj));
}

void MainWindow::appendQueue(const QJsonObject& track) {
    m_playback.appendQueue(track);
}

void MainWindow::insertQueueNext(const QJsonObject& track) {
    m_playback.insertQueueNext(track);
}

void MainWindow::refreshQueueView() {
    m_queueList->clear();
    const QVector<QJsonObject> queue = m_playback.queuedTracks();
    for (qsizetype i = 0; i < queue.size(); ++i) {
        const QJsonObject track = queue.at(i);
        QString text = trackLine(track);
        if (i == 0) text = QStringLiteral("Next: %1").arg(text);
        auto* item = new QListWidgetItem(text);
        item->setData(Qt::UserRole, track);
        m_queueList->addItem(item);
    }
    updateQueueTabLabel();
    updateDiscordContext();
    updateMprisQueueState();
}

void MainWindow::clearQueue() {
    m_playback.clearQueue();
}

void MainWindow::updateQueueTabLabel() {
    if (!m_detailsTabs || m_queueTabIndex < 0) return;
    const int count = m_playback.queueSize();
    m_detailsTabs->setTabText(m_queueTabIndex, count ? QStringLiteral("Queue (%1)").arg(count) : QStringLiteral("Queue"));
}

void MainWindow::removeQueueRow(int row) {
    m_playback.removeQueueRow(row);
}

void MainWindow::playQueueRow(int row) {
    syncPlaybackOutput();
    m_playback.playQueueRow(row);
}

void MainWindow::moveQueueRowToNext(int row) {
    m_playback.moveQueueRowToNext(row);
}

void MainWindow::playNextQueued() {
    syncPlaybackOutput();
    m_playback.playNextQueued();
}

void MainWindow::startPlayback(const QJsonObject& track) {
    if (track.isEmpty()) return;
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (!id.isEmpty()) m_tracks[id] = track;
    syncPlaybackOutput();
    m_settings.setValue(QStringLiteral("qt6/alsa_device"), m_deviceCombo->currentText());
    m_playback.playTrack(track);
    if (trackNeedsDetailHydration(track)) {
        hydrateTrackDetails(track, [](const QJsonObject&) {});
    }
}

void MainWindow::handlePlaybackState(const PlaybackState& state) {
    const bool becameActivityVisible = state.busy && !m_playbackActivityVisible;
    if (state.hasTrack()) {
        const bool trackChanged = state.trackId != m_renderedPlaybackTrackId;
        const bool trackPayloadChanged = state.track != m_renderedPlaybackTrack;
        const bool durationChanged = std::abs(state.durationSeconds - m_renderedPlaybackDuration) > 0.1;
        if (!state.trackId.isEmpty() && !state.track.isEmpty()) m_tracks[state.trackId] = state.track;
        if (trackChanged || trackPayloadChanged) {
            setNowPlaying(state, trackChanged);
            m_renderedPlaybackTrackId = state.trackId;
            m_renderedPlaybackTrack = state.track;
        }
        if (state.busy && (trackChanged || trackPayloadChanged || durationChanged || becameActivityVisible)) {
            updateDiscordTrack(state);
            updateMprisTrack(state);
        }
        if (trackChanged || trackPayloadChanged || durationChanged) m_renderedPlaybackDuration = state.durationSeconds;
    } else {
        m_renderedPlaybackTrackId.clear();
        m_renderedPlaybackTrack = {};
        m_renderedPlaybackDuration = -1.0;
    }

    if (m_quality) m_quality->setText(qualityLabelText(state.audioQuality, state.streamFormat.bitDepth, state.streamFormat.sampleRate));
    updateAudioStatusLabels(state);

    if (m_seek) {
        m_seek->setRange(0, qMax(0, static_cast<int>(state.durationSeconds * 1000)));
        m_seek->setEnabled(state.busy && state.durationSeconds > 0.0);
    }
    updateDiscordPosition(state.positionSeconds, state.durationSeconds);
    updateMprisPosition(state.positionSeconds, state.durationSeconds);
    if (seekPreviewActive(state.positionSeconds)) {
        if (m_seek && !m_seek->isSliderDown()) m_seek->setValue(static_cast<int>(m_seekPreviewTarget * 1000));
        if (m_time) m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(m_seekPreviewTarget), formatTime(state.durationSeconds)));
    } else {
        m_seekPreviewTarget = -1.0;
        m_seekPreviewUntilMs = 0;
        if (m_seek && !m_seek->isSliderDown()) m_seek->setValue(static_cast<int>(state.positionSeconds * 1000));
        if (m_time) m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(state.positionSeconds), formatTime(state.durationSeconds)));
        m_lyrics.updatePosition(state.positionSeconds);
    }

    updatePauseButton(state);
    updateDiscordContext(state);
    updateDiscordPlaybackStatus(state);
    updateMprisPlaybackStatus(state);

    if (state.busy) {
        m_playbackActivityVisible = true;
    } else if (m_playbackActivityVisible) {
        if (m_discord) m_discord->clearActivity();
        if (m_mpris) m_mpris->clearTrack();
        m_playbackActivityVisible = false;
    }
}

void MainWindow::setupPlaybackSignals() {
    connect(&m_playback, &PlaybackController::statusMessage, this, &MainWindow::setStatus);
    connect(&m_playback, &PlaybackController::logMessage, this, &MainWindow::setStatus);
    connect(&m_playback, &PlaybackController::streamError, this, [this](const QString& msg) {
        QMessageBox::critical(this, QStringLiteral("Stream"), msg);
    });
    connect(&m_playback, &PlaybackController::playbackError, this, [this](const QString& msg) {
        QMessageBox::critical(this, QStringLiteral("Playback"), msg);
    });
    connect(&m_playback, &PlaybackController::queueChanged, this, [this](const QVector<QJsonObject>&) {
        refreshQueueView();
    });
    connect(&m_playback, &PlaybackController::playbackStateChanged, this, &MainWindow::handlePlaybackState);
}

void MainWindow::stopPlayback() {
    m_playback.stop();
}

void MainWindow::togglePause() {
    const PlaybackState state = m_playback.playbackState();
    if (!state.busy && !state.hasTrack()) {
        playSelected();
        return;
    }
    m_playback.togglePause();
}
void MainWindow::seekReleased() {
    const double target = static_cast<double>(m_seek->value()) / 1000.0;
    const PlaybackState state = m_playback.playbackState();
    beginSeekPreview(target);
    m_playback.seekTo(target);
    updateMprisPosition(target, state.durationSeconds);
    notifyDiscordSeeked(target, state.durationSeconds);
    if (m_mpris) m_mpris->notifySeeked(target);
}
void MainWindow::volumeChanged(int value) {
    m_settings.setValue(QStringLiteral("qt6/volume"), value);
    if (m_volumeLabel) m_volumeLabel->setText(QStringLiteral("%1%").arg(value));
    m_playback.setVolume(value);
    updateAudioStatusLabels();
    updateMprisVolume();
}

void MainWindow::updatePauseButton() {
    updatePauseButton(m_playback.playbackState());
}

void MainWindow::updatePauseButton(const PlaybackState& state) {
    if (!m_pauseButton) return;
    if (!state.busy) m_pauseButton->setText(QStringLiteral("Play"));
    else m_pauseButton->setText(state.paused ? QStringLiteral("Resume") : QStringLiteral("Pause"));
}

bool MainWindow::volumeControlAvailable() const {
    const QString device = m_deviceCombo ? m_deviceCombo->currentText().trimmed() : QString();
    return !device.isEmpty() && !device.startsWith(QStringLiteral("hw:"));
}

void MainWindow::updateAudioStatusLabels() {
    updateAudioStatusLabels(m_playback.playbackState());
}

void MainWindow::updateAudioStatusLabels(const PlaybackState& state) {
    const bool volumeAvailable = volumeControlAvailable();
    if (m_volume) m_volume->setEnabled(volumeAvailable);
    if (m_volumeLabel) m_volumeLabel->setEnabled(volumeAvailable);
    const int streamSampleRate = state.streamFormat.sampleRate;
    const int streamBitDepth = state.streamFormat.bitDepth;
    const int outputChannels = state.outputFormat.channels;
    const int outputRate = state.outputFormat.sampleRate;
    const int outputBits = state.outputFormat.bitDepth;

    if (outputChannels > 0 && outputRate > 0 && outputBits > 0) {
        const double outputKbps = (outputChannels * outputRate * outputBits) / 1000.0;
        if (streamSampleRate > 0 && streamBitDepth > 0) {
            const double streamKbps = (outputChannels * streamSampleRate * streamBitDepth) / 1000.0;
            m_bitrate->setText(QStringLiteral("Bitrate: stream ~%1 kbps | output PCM %2 kbps").arg(streamKbps, 0, 'f', 0).arg(outputKbps, 0, 'f', 0));
        } else {
            m_bitrate->setText(QStringLiteral("Bitrate: output PCM %1 kbps").arg(outputKbps, 0, 'f', 0));
        }
    } else {
        m_bitrate->setText(QStringLiteral("Bitrate: —"));
    }

    QString device = state.outputDevice.trimmed();
    if (device.isEmpty() && m_deviceCombo) device = m_deviceCombo->currentText().trimmed();
    if (device.isEmpty()) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: —"));
    } else if (!device.startsWith(QStringLiteral("hw:"))) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: unlikely (not hw:)"));
    } else if (streamSampleRate <= 0 || streamBitDepth <= 0 || outputRate <= 0 || outputBits <= 0) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: unknown (stream/format pending)"));
    } else if (outputRate != streamSampleRate) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: no (%1Hz != %2Hz)").arg(outputRate).arg(streamSampleRate));
    } else if (state.bitPerfect && outputBits == streamBitDepth) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: yes"));
    } else if (state.bitPerfect && streamBitDepth == 24 && outputBits == 32) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: padded (24/32 PCM)"));
    } else {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: no (%1-bit != %2-bit)").arg(outputBits).arg(streamBitDepth));
    }
}

void MainWindow::refreshCacheTab() {
    m_cache.refresh();
    auto trackObjectForEntry = [this](const CacheManagerQt::Entry& entry) {
        QJsonObject obj = m_tracks.value(entry.id);
        obj.insert(QStringLiteral("id"), entry.id);
        obj.insert(QStringLiteral("_type"), QStringLiteral("track"));
        if (!entry.title.isEmpty()) obj.insert(QStringLiteral("title"), entry.title);
        if (!entry.artist.isEmpty()) obj.insert(QStringLiteral("artist_display"), entry.artist);
        if (!entry.album.isEmpty()) obj.insert(QStringLiteral("album"), entry.album);
        if (!entry.albumId.isEmpty()) obj.insert(QStringLiteral("album_id"), entry.albumId);
        if (!entry.artistId.isEmpty()) obj.insert(QStringLiteral("artist_id"), entry.artistId);
        if (!entry.coverUrl.isEmpty()) obj.insert(QStringLiteral("cover_url"), entry.coverUrl);
        if (!entry.coverThumbnailUrl.isEmpty()) obj.insert(QStringLiteral("cover_thumbnail_url"), entry.coverThumbnailUrl);
        if (!entry.audioQuality.isEmpty()) obj.insert(QStringLiteral("audio_quality"), entry.audioQuality);
        if (!entry.trackMaxQuality.isEmpty()) obj.insert(QStringLiteral("track_max_quality"), entry.trackMaxQuality);
        if (entry.bitDepth > 0) obj.insert(QStringLiteral("bit_depth"), entry.bitDepth);
        if (entry.sampleRate > 0) obj.insert(QStringLiteral("sample_rate"), entry.sampleRate);
        return obj;
    };
    m_cacheList->clear();
    for (const auto& entry : m_cache.cachedTracks()) {
        const QJsonObject obj = trackObjectForEntry(entry);
        auto* item = new QListWidgetItem(trackLine(obj));
        item->setData(Qt::UserRole, obj);
        m_cacheList->addItem(item);
        m_tracks[entry.id] = obj;
        m_playback.updateTrackMetadata(obj);
    }
    m_downloadList->clear();
    for (const auto& entry : m_cache.downloads()) {
        const QJsonObject obj = trackObjectForEntry(entry);
        auto* item = new QListWidgetItem(trackLine(obj));
        item->setData(Qt::UserRole, obj);
        m_downloadList->addItem(item);
        m_tracks[entry.id] = obj;
        m_playback.updateTrackMetadata(obj);
    }
    updateCacheStatusLabels();
}

void MainWindow::queueCachedTracks() {
    for (int i = 0; i < m_cacheList->count(); ++i) {
        appendQueue(m_cacheList->item(i)->data(Qt::UserRole).toJsonObject());
    }
}

void MainWindow::queueDownloadedTracks() {
    for (int i = 0; i < m_downloadList->count(); ++i) {
        appendQueue(m_downloadList->item(i)->data(Qt::UserRole).toJsonObject());
    }
}

void MainWindow::clearCachedTracks() {
    const CacheManagerQt::Stats audio = m_cache.audioStats();
    const CacheManagerQt::Stats covers = m_cache.coverStats();
    if (audio.count == 0 && covers.count == 0) return;
    QMessageBox box(this);
    box.setWindowTitle(QStringLiteral("Clear Cache"));
    box.setText(QStringLiteral("Clear cached tracks, covers, or both?\n\nTracks: %1 (%2)\nCovers: %3 (%4)")
        .arg(audio.count)
        .arg(formatBytes(audio.bytes))
        .arg(covers.count)
        .arg(formatBytes(covers.bytes)));
    QPushButton* tracksButton = box.addButton(QStringLiteral("Tracks"), QMessageBox::AcceptRole);
    QPushButton* coversButton = box.addButton(QStringLiteral("Covers"), QMessageBox::AcceptRole);
    QPushButton* bothButton = box.addButton(QStringLiteral("Both"), QMessageBox::AcceptRole);
    box.addButton(QMessageBox::Cancel);
    box.exec();
    QAbstractButton* clicked = box.clickedButton();
    if (clicked == tracksButton) m_cache.clearAudio();
    else if (clicked == coversButton) m_cache.clearCovers();
    else if (clicked == bothButton) {
        m_cache.clearAudio();
        m_cache.clearCovers();
    } else return;
    refreshCacheTab();
    m_playback.refreshLocalPrefetch();
}

void MainWindow::clearDownloadedTracks() {
    const CacheManagerQt::Stats downloads = m_cache.downloadStats();
    if (downloads.count == 0) return;
    if (QMessageBox::question(
            this,
            QStringLiteral("Clear Downloads"),
            QStringLiteral("Delete all %1 downloaded tracks (%2)?").arg(downloads.count).arg(formatBytes(downloads.bytes))
        ) != QMessageBox::Yes) return;
    m_cache.clearDownloads();
    refreshCacheTab();
    m_playback.refreshLocalPrefetch();
}

void MainWindow::updateCacheStatusLabels() {
    const CacheManagerQt::Stats audio = m_cache.audioStats();
    const CacheManagerQt::Stats covers = m_cache.coverStats();
    const CacheManagerQt::Stats downloads = m_cache.downloadStats();
    if (m_cacheStatusLabel) {
        QStringList prefixParts;
        if (m_offlineMode) prefixParts << QStringLiteral("Offline");
        if (!m_audioCacheEnabled) prefixParts << QStringLiteral("Audio cache off");
        if (!m_coverCacheEnabled) prefixParts << QStringLiteral("Cover cache off");
        if (m_audioCacheEnabled) prefixParts << QStringLiteral("Cache: %1").arg(m_cacheMode);
        const QString prefix = prefixParts.isEmpty() ? QString() : prefixParts.join(QStringLiteral(" | ")) + QStringLiteral(" | ");
        m_cacheStatusLabel->setText(prefix + QStringLiteral("Tracks: %1 | Covers: %2 | %3")
            .arg(audio.count)
            .arg(covers.count)
            .arg(formatBytes(audio.bytes + covers.bytes)));
    }
    if (m_downloadsStatusLabel) {
        m_downloadsStatusLabel->setText(QStringLiteral("Tracks: %1 | %2").arg(downloads.count).arg(formatBytes(downloads.bytes)));
    }
}

void MainWindow::rememberFavoriteItems(const QString& type, const QJsonArray& items) {
    const QString kind = mediaTypeKey(type);
    QSet<QString> ids;
    for (const QJsonValue& value : items) {
        QJsonObject obj = value.toObject();
        if (obj.contains(QStringLiteral("data"))) obj = obj.value(QStringLiteral("data")).toObject();
        const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
        if (!id.isEmpty()) ids.insert(id);
    }
    m_favoriteIds[kind] = ids;
    m_favoriteTypesLoaded.insert(kind);
}

void MainWindow::refreshFavoriteState() {
    m_browser.refreshFavoriteState();
}

bool MainWindow::isFavoriteItem(const QString& type, const QString& id, const QJsonObject& obj) const {
    if (id.isEmpty()) return false;
    const QString kind = mediaTypeKey(type);
    if (m_favoriteTypesLoaded.contains(kind)) return m_favoriteIds.value(kind).contains(id);
    if (obj.contains(QStringLiteral("favorite"))) return obj.value(QStringLiteral("favorite")).toBool(false);
    if (obj.contains(QStringLiteral("favorited"))) return obj.value(QStringLiteral("favorited")).toBool(false);
    if (obj.contains(QStringLiteral("is_favorite"))) return obj.value(QStringLiteral("is_favorite")).toBool(false);
    if (obj.contains(QStringLiteral("isFavorite"))) return obj.value(QStringLiteral("isFavorite")).toBool(false);
    return false;
}

void MainWindow::setFavoriteState(const QString& type, const QString& id, bool favorite) {
    if (id.isEmpty()) return;
    const QString kind = mediaTypeKey(type);
    m_favoriteTypesLoaded.insert(kind);
    QSet<QString>& ids = m_favoriteIds[kind];
    if (favorite) ids.insert(id);
    else ids.remove(id);
    if (kind == QStringLiteral("track") && m_tracks.contains(id)) {
        QJsonObject track = m_tracks.value(id);
        track.insert(QStringLiteral("favorite"), favorite);
        m_tracks[id] = track;
        m_playback.updateTrackMetadata(track);
    }
}

QAction* MainWindow::addFavoriteAction(QMenu* menu, const QString& type, const QString& id, const QJsonObject& obj) {
    const QString kind = mediaTypeKey(type);
    const bool currentlyFavorite = isFavoriteItem(kind, id, obj);
    QAction* action = menu->addAction(currentlyFavorite ? QStringLiteral("Unfavorite") : QStringLiteral("Favorite"), this, [this, kind, id, currentlyFavorite]() {
        toggleFavorite(kind, id, !currentlyFavorite);
    });
    action->setEnabled(!m_offlineMode && !id.isEmpty());
    return action;
}

void MainWindow::toggleFavorite(const QString& type, const QString& id, bool favorite) {
    if (!requireOnline(QStringLiteral("Favorite"))) return;
    const QString kind = mediaTypeKey(type);
    if (id.isEmpty() || !(kind == QStringLiteral("track") || kind == QStringLiteral("album") || kind == QStringLiteral("playlist") || kind == QStringLiteral("artist"))) {
        setStatus(QStringLiteral("Select a track, album, playlist, or artist"));
        return;
    }
    m_tidal.request(
        QStringLiteral("favorite"),
        {{QStringLiteral("type"), kind}, {QStringLiteral("id"), id}, {QStringLiteral("favorite"), favorite}},
        [this, kind, id, favorite](const QJsonObject&) {
            setFavoriteState(kind, id, favorite);
            if (m_tabs && m_tabs->currentWidget() == m_collectionTab && mediaTypeKey(m_collectionType->currentText()) == kind) {
                refreshCollection();
            }
            setStatus(favorite ? QStringLiteral("Favorite added") : QStringLiteral("Favorite removed"));
        },
        [this](const QString& error) { QMessageBox::critical(this, QStringLiteral("Favorite"), error); }
    );
}

void MainWindow::rememberTracks(const QJsonArray& items) {
    for (const QJsonValue& value : items) {
        QJsonObject obj = value.toObject();
        QString type = obj.value(QStringLiteral("_type")).toString();
        if (obj.contains(QStringLiteral("data"))) {
            type = obj.value(QStringLiteral("type")).toString(type);
            obj = obj.value(QStringLiteral("data")).toObject();
        }
        const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
        if (!id.isEmpty() && shouldRememberTrackObject(obj, type)) {
            m_tracks[id] = obj;
            m_playback.updateTrackMetadata(obj);
        }
        for (const QJsonValue& track : obj.value(QStringLiteral("tracks")).toArray()) {
            const QJsonObject t = track.toObject();
            const QString tid = t.value(QStringLiteral("id")).toVariant().toString();
            if (!tid.isEmpty()) {
                m_tracks[tid] = t;
                m_playback.updateTrackMetadata(t);
            }
        }
    }
}

void MainWindow::setNowPlaying(const PlaybackState& state, bool trackChanged) {
    const QJsonObject& track = state.track;
    m_title->setText(track.value(QStringLiteral("title")).toString(QStringLiteral("Unknown title")));
    const QString album = track.value(QStringLiteral("album")).toString();
    const QString artist = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString());
    m_meta->setText(album.isEmpty() ? artist : QStringLiteral("%1 — %2").arg(artist, album));
    m_quality->setText(qualityLabelText(state.audioQuality, state.streamFormat.bitDepth, state.streamFormat.sampleRate));
    updateAudioStatusLabels(state);
    const QString coverUrl = artworkUrl(track);
    const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
    const bool currentCoverMatches = (!coverUrl.isEmpty() && coverUrl == m_displayedCoverUrl)
        || (!albumId.isEmpty() && albumId == m_displayedCoverAlbumId);
    if (trackChanged && !currentCoverMatches) {
        if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(QPixmap());
        else {
            m_cover->setText(QStringLiteral("No cover"));
            m_cover->setPixmap(QPixmap());
        }
    }
    loadCover(track);
    if (trackChanged) m_lyrics.loadLyrics(state.trackId, m_title->text(), m_offlineMode);
}

void MainWindow::setSelectedTrackPreview(const QJsonObject& track) {
    if (m_title) m_title->setText(track.value(QStringLiteral("title")).toString(QStringLiteral("Unknown title")));
    const QString album = track.value(QStringLiteral("album")).toString();
    const QString artist = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString());
    if (m_meta) m_meta->setText(album.isEmpty() ? artist : QStringLiteral("%1 — %2").arg(artist, album));
    if (m_quality) m_quality->setText(QStringLiteral("Quality: —"));
    if (m_bitrate) m_bitrate->setText(QStringLiteral("Bitrate: —"));
    if (m_bitperfect) m_bitperfect->setText(QStringLiteral("Bit-perfect: —"));
}

void MainWindow::loadCoverForSelected() {
    if (m_playback.playbackState().busy) return;
    QJsonObject obj;
    if (m_tabs && m_tabs->currentWidget() == m_cacheTab) {
        if (m_lastTrackList && m_lastTrackList->currentItem()) {
            obj = m_lastTrackList->currentItem()->data(Qt::UserRole).toJsonObject();
        }
    } else if (QTreeWidget* tree = activeTree()) {
        obj = itemObject(tree->currentItem());
    }
    const QString type = obj.value(QStringLiteral("_type")).toString();
    if (!(type == QStringLiteral("track") || type == QStringLiteral("album"))) return;
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    if (type == QStringLiteral("track")) setSelectedTrackPreview(obj);
    if (type == QStringLiteral("track") && trackNeedsDetailHydration(obj) && !m_offlineMode) {
        hydrateTrackDetails(obj, [this, id](const QJsonObject& hydrated) {
            if (selectedObject().value(QStringLiteral("id")).toVariant().toString() == id) {
                setSelectedTrackPreview(hydrated);
                loadCover(hydrated);
            }
        });
        return;
    }
    const QString coverUrl = artworkUrl(obj);
    QString albumId = obj.value(QStringLiteral("album_id")).toVariant().toString();
    if (type == QStringLiteral("album") && albumId.isEmpty()) albumId = id;
    const QString requestId = id.isEmpty()
        ? QStringLiteral("%1:%2").arg(type, coverUrl)
        : QStringLiteral("%1:%2").arg(type, id);
    requestCover(coverUrl, requestId, albumId);
}

void MainWindow::loadCover(const QJsonObject& track) {
    const QString coverUrl = artworkUrl(track);
    const QString trackId = track.value(QStringLiteral("id")).toVariant().toString();
    const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
    requestCover(coverUrl, trackId.isEmpty() ? QStringLiteral("cover:%1").arg(coverUrl) : QStringLiteral("track:%1").arg(trackId), albumId);
}

void MainWindow::requestCover(const QString& coverUrl, const QString& requestId, const QString& albumId) {
    m_coverRequestId = requestId;
    if ((!coverUrl.isEmpty() && coverUrl == m_displayedCoverUrl)
        || (!albumId.isEmpty() && albumId == m_displayedCoverAlbumId)) {
        if (!albumId.isEmpty()) m_displayedCoverAlbumId = albumId;
        return;
    }
    const QByteArray cachedCover = m_coverCacheEnabled ? m_cache.coverBytes(coverUrl) : QByteArray();
    if (!cachedCover.isEmpty()) {
        QPixmap pix;
        pix.loadFromData(cachedCover);
        if (!pix.isNull()) {
            if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(pix);
            else if (m_cover) m_cover->setPixmap(pix.scaled(m_cover->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
            m_displayedCoverUrl = coverUrl;
            m_displayedCoverAlbumId = albumId;
            return;
        }
    }
    const QUrl url(coverUrl);
    if (!url.isValid() || url.isEmpty()) {
        m_displayedCoverUrl.clear();
        m_displayedCoverAlbumId.clear();
        if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(QPixmap());
        else if (m_cover) m_cover->setPixmap(QPixmap());
        return;
    }
    QNetworkReply* reply = m_network.get(QNetworkRequest(url));
    connect(reply, &QNetworkReply::finished, this, [this, reply, requestId, coverUrl, albumId]() {
        if (requestId != m_coverRequestId) {
            reply->deleteLater();
            return;
        }
        const QByteArray data = reply->readAll();
        QPixmap pix;
        pix.loadFromData(data);
        if (!pix.isNull()) {
            if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(pix);
            else m_cover->setPixmap(pix.scaled(m_cover->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
            m_displayedCoverUrl = coverUrl;
            m_displayedCoverAlbumId = albumId;
            if (m_coverCacheEnabled && m_cache.storeCoverBytes(coverUrl, data)) {
                if (m_coverCacheLimitMb > 0) m_cache.enforceCoverLimit(megabytesToBytes(m_coverCacheLimitMb));
                updateCacheStatusLabels();
            }
        } else if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) {
            cover->setCoverPixmap(QPixmap());
            m_displayedCoverUrl.clear();
            m_displayedCoverAlbumId.clear();
        }
        reply->deleteLater();
    });
}

void MainWindow::beginSeekPreview(double seconds) {
    m_seekPreviewTarget = qMax(0.0, seconds);
    m_seekPreviewUntilMs = QDateTime::currentMSecsSinceEpoch() + 3500;
    m_seek->setValue(static_cast<int>(m_seekPreviewTarget * 1000));
    m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(m_seekPreviewTarget), formatTime(m_playback.playbackState().durationSeconds)));
    m_lyrics.updatePosition(m_seekPreviewTarget);
}

bool MainWindow::seekPreviewActive(double incomingPosition) const {
    if (m_seekPreviewTarget < 0.0) return false;
    if (QDateTime::currentMSecsSinceEpoch() > m_seekPreviewUntilMs) return false;
    return std::abs(incomingPosition - m_seekPreviewTarget) > 1.25;
}

QString MainWindow::trackLine(const QJsonObject& track) const {
    return trackLineText(track);
}

QString MainWindow::tidalUrl(const QString& type, const QString& id) const {
    const QString cleanId = id.trimmed();
    if (type.isEmpty() || cleanId.isEmpty()) return QString();
    return QStringLiteral("https://tidal.com/%1/%2").arg(type, cleanId);
}

void MainWindow::copyTidalLink(const QString& type, const QString& id) {
    const QString url = tidalUrl(type, id);
    if (url.isEmpty()) return;
    QApplication::clipboard()->setText(url);
    setStatus(QStringLiteral("Copied %1 link").arg(type));
}

void MainWindow::openTidalItem(const QString& type, const QString& id) {
    if (!requireOnline(QStringLiteral("Open item"))) return;
    const QString url = tidalUrl(type, id);
    if (url.isEmpty() || !m_urlEdit || !m_tabs) return;
    m_tabs->setCurrentWidget(m_urlTab);
    m_urlEdit->setText(url);
    loadUrl();
}

bool MainWindow::trackNeedsDetailHydration(const QJsonObject& track) const {
    if (!isTrackObject(track)) return false;
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) return false;
    return track.value(QStringLiteral("album_id")).toVariant().toString().isEmpty()
        || track.value(QStringLiteral("artist_id")).toVariant().toString().isEmpty()
        || artworkUrl(track).isEmpty()
        || (track.value(QStringLiteral("audio_quality")).toString().isEmpty()
            && track.value(QStringLiteral("track_max_quality")).toString().isEmpty())
        || jsonValueMissing(track.value(QStringLiteral("duration")));
}

QJsonObject MainWindow::mergeTrackDetails(const QJsonObject& track, const QJsonObject& details) const {
    QJsonObject merged = track;
    merged.insert(QStringLiteral("_type"), QStringLiteral("track"));
    for (const QString& key : {
             QStringLiteral("id"),
             QStringLiteral("title"),
             QStringLiteral("artist"),
             QStringLiteral("artist_id"),
             QStringLiteral("artists"),
             QStringLiteral("artist_display"),
             QStringLiteral("album"),
             QStringLiteral("album_id"),
             QStringLiteral("cover_url"),
             QStringLiteral("cover_thumbnail_url"),
             QStringLiteral("duration"),
             QStringLiteral("audio_quality"),
             QStringLiteral("track_max_quality"),
             QStringLiteral("bit_depth"),
             QStringLiteral("sample_rate"),
             QStringLiteral("favorite"),
         }) {
        if (jsonValueMissing(merged.value(key)) && !jsonValueMissing(details.value(key))) {
            merged.insert(key, details.value(key));
        }
    }
    return merged;
}

void MainWindow::hydrateTrackDetails(const QJsonObject& track, std::function<void(const QJsonObject&)> onReady) {
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty() || m_offlineMode) {
        if (onReady) onReady(track);
        return;
    }
    QJsonObject merged = track;
    if (m_tracks.contains(id)) merged = mergeTrackDetails(merged, m_tracks.value(id));
    if (!trackNeedsDetailHydration(merged)) {
        if (onReady) onReady(merged);
        return;
    }
    m_tidal.request(
        QStringLiteral("details"),
        {{QStringLiteral("type"), QStringLiteral("track")}, {QStringLiteral("id"), id}},
        [this, track, id, onReady](const QJsonObject& result) {
            const QJsonObject details = result.value(QStringLiteral("item")).toObject();
            const QJsonObject base = m_tracks.contains(id) ? mergeTrackDetails(track, m_tracks.value(id)) : track;
            const QJsonObject merged = mergeTrackDetails(base, details);
            m_tracks[id] = merged;
            m_playback.updateTrackMetadata(merged);
            updateCachedTrackRows(merged);
            if (onReady) onReady(merged);
        },
        [this, track, onReady](const QString& error) {
            setStatus(QStringLiteral("Track details unavailable: %1").arg(error));
            if (onReady) onReady(track);
        }
    );
}

void MainWindow::updateCachedTrackRows(const QJsonObject& track) {
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) return;
    for (QListWidget* list : {m_cacheList, m_downloadList}) {
        if (!list) continue;
        for (int i = 0; i < list->count(); ++i) {
            QListWidgetItem* item = list->item(i);
            QJsonObject row = item->data(Qt::UserRole).toJsonObject();
            if (row.value(QStringLiteral("id")).toVariant().toString() != id) continue;
            row = mergeTrackDetails(row, track);
            item->setData(Qt::UserRole, row);
            item->setText(trackLine(row));
            break;
        }
    }
}

void MainWindow::openTrackAlbum(const QJsonObject& track) {
    if (!requireOnline(QStringLiteral("Open album"))) return;
    const auto openAlbum = [this](const QJsonObject& readyTrack) {
        const QString albumId = readyTrack.value(QStringLiteral("album_id")).toVariant().toString();
        if (albumId.isEmpty()) {
            setStatus(QStringLiteral("Album unavailable for this track"));
            return;
        }
        openTidalItem(QStringLiteral("album"), albumId);
    };
    if (!track.value(QStringLiteral("album_id")).toVariant().toString().isEmpty()) {
        openAlbum(track);
        return;
    }
    hydrateTrackDetails(track, openAlbum);
}

void MainWindow::openTrackArtist(const QJsonObject& track) {
    if (!requireOnline(QStringLiteral("Open artist"))) return;
    const auto openArtist = [this](const QJsonObject& readyTrack) {
        const QString artistId = readyTrack.value(QStringLiteral("artist_id")).toVariant().toString();
        if (artistId.isEmpty()) {
            setStatus(QStringLiteral("Artist unavailable for this track"));
            return;
        }
        openTidalItem(QStringLiteral("artist"), artistId);
    };
    if (!track.value(QStringLiteral("artist_id")).toVariant().toString().isEmpty()) {
        openArtist(track);
        return;
    }
    hydrateTrackDetails(track, openArtist);
}

bool MainWindow::trackIsLocal(const QString& id) const {
    return m_playback.trackIsLocal(id);
}

bool MainWindow::trackIsDownloaded(const QString& id) const {
    return m_playback.trackIsDownloaded(id);
}

void MainWindow::addTrackStorageAction(QMenu* menu, const QJsonObject& track) {
    if (!menu) return;
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) return;
    const bool downloaded = trackIsDownloaded(id);
    QAction* action = menu->addAction(downloaded ? QStringLiteral("Delete download") : QStringLiteral("Download track"), this, [this, track]() {
        downloadOrDeleteTrack(track);
    });
    action->setEnabled(downloaded || !m_offlineMode);
}

void MainWindow::downloadOrDeleteTrack(const QJsonObject& track) {
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) {
        setStatus(QStringLiteral("Select a track to download"));
        return;
    }
    m_cache.refresh();
    if (m_cache.hasDownload(id)) {
        if (m_cache.deleteDownload(id)) setStatus(QStringLiteral("Deleted download"));
        else setStatus(QStringLiteral("Download not found"));
        refreshCacheTab();
        m_playback.refreshLocalPrefetch();
        return;
    }
    if (!requireOnline(QStringLiteral("Download"))) return;
    setStatus(QStringLiteral("Downloading %1...").arg(track.value(QStringLiteral("title")).toString()));
    m_tidal.request(QStringLiteral("download"), {{QStringLiteral("track_id"), id}}, [this](const QJsonObject& result) {
        const QJsonObject track = result.value(QStringLiteral("track")).toObject();
        if (!track.isEmpty()) {
            m_tracks[track.value(QStringLiteral("id")).toVariant().toString()] = track;
            m_playback.updateTrackMetadata(track);
        }
        QString message = QStringLiteral("Downloaded: %1").arg(result.value(QStringLiteral("path")).toString());
        const QString metadataWarning = result.value(QStringLiteral("metadata_warning")).toString();
        if (!metadataWarning.isEmpty()) message += QStringLiteral(" (metadata incomplete: %1)").arg(metadataWarning);
        setStatus(message);
        refreshCacheTab();
        m_playback.refreshLocalPrefetch();
    }, [this](const QString& error) { QMessageBox::critical(this, QStringLiteral("Download"), error); });
}

void MainWindow::setStatus(const QString& message) {
    if (!m_status) return;
    m_status->setText(message.startsWith(QStringLiteral("Status:")) ? message : QStringLiteral("Status: %1").arg(message));
}

void MainWindow::showSettingsDialog() {
    SettingsDialog::RuntimeState state;
    state.currentDevice = m_deviceCombo ? m_deviceCombo->currentText() : QStringLiteral("default");
    state.volumePercent = m_volume ? m_volume->value() : 100;
    state.gaplessEnabled = m_playback.gaplessEnabled();
    state.streamTransitionSmoothing = m_playback.streamTransitionSmoothing();
    state.reduceAnimations = m_reduceAnimations;
    state.discordEnabled = m_discordEnabled;
    state.discordClientId = m_discordClientId;
    state.mprisAvailable = m_mprisAvailable || MprisService::available();
    state.mprisEnabled = m_mprisEnabled;
    state.mprisRunning = m_mpris && m_mpris->running();
    state.nativeAvailable = m_playback.nativeAvailable();
    state.discordConnected = m_discord && m_discord->connected();
    state.offlineMode = m_offlineMode;
    state.audioCacheEnabled = m_audioCacheEnabled;
    state.coverCacheEnabled = m_coverCacheEnabled;
    state.cacheMode = m_cacheMode;
    state.audioCacheLimitMb = m_audioCacheLimitMb;
    state.coverCacheLimitMb = m_coverCacheLimitMb;

    SettingsDialog dialog(
        state,
        &m_cache,
        &m_scrobble,
        [this]() { clearCachedTracks(); },
        [this]() { clearDownloadedTracks(); },
        this
    );
    if (dialog.exec() != QDialog::Accepted) return;

    const SettingsDialog::Result settings = dialog.result();
    const QString selectedDevice = settings.selectedDevice.trimmed();
    if (!selectedDevice.isEmpty() && m_deviceCombo) {
        if (m_deviceCombo->findText(selectedDevice) < 0) m_deviceCombo->insertItem(0, selectedDevice);
        m_deviceCombo->setCurrentText(selectedDevice);
        m_settings.setValue(QStringLiteral("qt6/alsa_device"), selectedDevice);
    }
    if (m_volume) m_volume->setValue(settings.volumePercent);
    m_reduceAnimations = settings.reduceAnimations;
    m_playback.setGaplessEnabled(settings.gaplessEnabled);
    m_playback.setStreamTransitionSmoothing(settings.streamTransitionSmoothing);
    m_audioCacheEnabled = settings.audioCacheEnabled;
    m_coverCacheEnabled = settings.coverCacheEnabled;
    m_cacheMode = settings.cacheMode;
    m_audioCacheLimitMb = qMax(0, settings.audioCacheLimitMb);
    m_coverCacheLimitMb = qMax(0, settings.coverCacheLimitMb);
    m_playback.setCacheMode(m_cacheMode);
    m_playback.setAudioCacheEnabled(m_audioCacheEnabled);
    m_playback.setAudioCacheLimitBytes(megabytesToBytes(m_audioCacheLimitMb));
    if (m_cache.enforceLimits(megabytesToBytes(m_audioCacheLimitMb), megabytesToBytes(m_coverCacheLimitMb), m_cacheMode)) {
        refreshCacheTab();
    } else {
        updateCacheStatusLabels();
    }
    m_settings.setValue(QStringLiteral("qt6/gapless_enabled"), m_playback.gaplessEnabled());
    m_settings.setValue(QStringLiteral("qt6/stream_transition_smoothing"), m_playback.streamTransitionSmoothing());
    m_settings.setValue(QStringLiteral("qt6/audio_cache_enabled"), m_audioCacheEnabled);
    m_settings.setValue(QStringLiteral("qt6/cover_cache_enabled"), m_coverCacheEnabled);
    m_settings.setValue(QStringLiteral("qt6/cache_mode"), m_cacheMode);
    m_settings.setValue(QStringLiteral("qt6/audio_cache_limit_mb"), m_audioCacheLimitMb);
    m_settings.setValue(QStringLiteral("qt6/cover_cache_limit_mb"), m_coverCacheLimitMb);
    m_settings.setValue(QStringLiteral("qt6/reduce_animations"), m_reduceAnimations);
    m_lyrics.setReduceAnimations(m_reduceAnimations);
    setDiscordEnabled(settings.discordEnabled, settings.discordClientId);
    if (settings.mprisAvailable) setMprisEnabled(settings.mprisEnabled);
    updateAudioStatusLabels();
}
