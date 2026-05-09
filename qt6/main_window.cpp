#include "main_window.h"

#include "discord_rpc_service.h"
#include "mpris_service.h"
#include "runtime_paths.h"

#include <QAbstractButton>
#include <QAbstractItemView>
#include <QAbstractSpinBox>
#include <QApplication>
#include <QBoxLayout>
#include <QBrush>
#include <QCheckBox>
#include <QCloseEvent>
#include <QClipboard>
#include <QColor>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QEasingCurve>
#include <QEvent>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QIcon>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLayout>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMessageBox>
#include <QNetworkReply>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPropertyAnimation>
#include <QPushButton>
#include <QResizeEvent>
#include <QScrollBar>
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
#include <QTcpSocket>
#include <QTimer>
#include <QTreeWidget>
#include <QTreeWidgetItemIterator>
#include <QTextEdit>
#include <QDateTime>
#include <QtAlgorithms>

#include <cmath>
#include <utility>

static constexpr int kDetailsStateRole = Qt::UserRole + 1;
static constexpr int kLoadingPlaceholderRole = Qt::UserRole + 2;
static constexpr qint64 kLyricsAutoScrollHoldMs = 8000;

class CoverLabel : public QLabel {
public:
    explicit CoverLabel(QWidget* parent = nullptr) : QLabel(parent) {
        setAlignment(Qt::AlignCenter);
        setMinimumSize(260, 260);
        setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    }

    void setCoverPixmap(const QPixmap& pixmap) {
        m_original = pixmap;
        updateScaledPixmap();
    }

    void setFallbackPixmap(const QPixmap& pixmap) {
        m_fallback = pixmap;
        updateScaledPixmap();
    }

protected:
    void resizeEvent(QResizeEvent* event) override {
        QLabel::resizeEvent(event);
        updateScaledPixmap();
    }

private:
    void updateScaledPixmap() {
        const QPixmap& source = m_original.isNull() ? m_fallback : m_original;
        if (source.isNull()) {
            clear();
            setText(QStringLiteral("No cover"));
            return;
        }
        setText(QString());
        QLabel::setPixmap(source.scaled(size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
    }

    QPixmap m_original;
    QPixmap m_fallback;
};

static QPixmap fallbackCoverPixmap() {
    const QString transparentIcon = RuntimePaths::assetPath(QStringLiteral("tidal-bitperfect-transparent.svg"));
    if (!transparentIcon.isEmpty()) {
        QPixmap pixmap(transparentIcon);
        if (!pixmap.isNull()) return pixmap;
    }
    QIcon icon = QIcon::fromTheme(QStringLiteral("tidal-bitperfect"));
    if (icon.isNull()) icon = QIcon::fromTheme(QStringLiteral("audio-x-generic"));
    if (icon.isNull()) return QPixmap();
    return icon.pixmap(512, 512);
}

static bool isContainerType(const QString& type) {
    return type == QStringLiteral("album") || type == QStringLiteral("playlist") || type == QStringLiteral("artist") || type == QStringLiteral("mix");
}

static bool isTrackObject(const QJsonObject& obj) {
    const QString type = obj.value(QStringLiteral("_type")).toString();
    return type == QStringLiteral("track") || (type.isEmpty() && obj.contains(QStringLiteral("id")) && obj.contains(QStringLiteral("title")));
}

static bool shouldRememberTrackObject(const QJsonObject& obj, const QString& typeHint = QString()) {
    const QString type = typeHint.isEmpty() ? obj.value(QStringLiteral("_type")).toString() : typeHint;
    if (type == QStringLiteral("track")) return true;
    if (!type.isEmpty()) return false;
    return obj.contains(QStringLiteral("duration"))
        || obj.contains(QStringLiteral("album"))
        || obj.contains(QStringLiteral("audio_quality"))
        || obj.contains(QStringLiteral("track_max_quality"));
}

static QVector<QJsonObject> trackObjects(const QJsonObject& obj) {
    QVector<QJsonObject> tracks;
    if (isTrackObject(obj)) {
        tracks.push_back(obj);
        return tracks;
    }
    for (const QJsonValue& value : obj.value(QStringLiteral("tracks")).toArray()) {
        if (value.isObject()) tracks.push_back(value.toObject());
    }
    return tracks;
}

static QString mediaTypeKey(const QString& label) {
    const QString lower = label.toLower();
    if (lower.startsWith(QStringLiteral("album"))) return QStringLiteral("album");
    if (lower.startsWith(QStringLiteral("playlist"))) return QStringLiteral("playlist");
    if (lower.startsWith(QStringLiteral("artist"))) return QStringLiteral("artist");
    return QStringLiteral("track");
}

static QString formatTime(double seconds) {
    const int total = qMax(0, static_cast<int>(std::llround(seconds)));
    const int minutes = total / 60;
    const int secs = total % 60;
    return QStringLiteral("%1:%2").arg(minutes).arg(secs, 2, 10, QLatin1Char('0'));
}

static QString formatBytes(qint64 bytes) {
    const char* units[] = {"B", "KB", "MB", "GB"};
    double value = static_cast<double>(qMax<qint64>(0, bytes));
    int unit = 0;
    while (value >= 1024.0 && unit < 3) {
        value /= 1024.0;
        ++unit;
    }
    return unit == 0
        ? QStringLiteral("%1 %2").arg(static_cast<qint64>(value)).arg(units[unit])
        : QStringLiteral("%1 %2").arg(value, 0, 'f', 1).arg(units[unit]);
}

static QString qualityLabelText(const QString& audioQuality, int bitDepth, int sampleRate) {
    QStringList parts;
    const QString quality = audioQuality.trimmed();
    if (!quality.isEmpty()) parts << quality;
    if (bitDepth > 0 && sampleRate > 0) parts << QStringLiteral("%1-bit/%2Hz").arg(bitDepth).arg(sampleRate);
    return parts.isEmpty() ? QStringLiteral("Quality: —") : QStringLiteral("Quality: %1").arg(parts.join(QLatin1Char(' ')));
}

static bool textInputFocused() {
    QWidget* focus = QApplication::focusWidget();
    return qobject_cast<QLineEdit*>(focus) != nullptr
        || qobject_cast<QTextEdit*>(focus) != nullptr
        || qobject_cast<QPlainTextEdit*>(focus) != nullptr
        || qobject_cast<QAbstractSpinBox*>(focus) != nullptr;
}

static bool networkOffline() {
    QTcpSocket socket;
    socket.connectToHost(QStringLiteral("1.1.1.1"), 443);
    const bool online = socket.waitForConnected(500);
    socket.abort();
    return !online;
}

static QStringList playbackDevices() {
    QSet<QString> devices{QStringLiteral("default"), QStringLiteral("null")};
    const QDir asound(QStringLiteral("/proc/asound"));
    for (const QString& entry : asound.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name)) {
        if (!entry.startsWith(QStringLiteral("card"))) continue;
        QFile idFile(asound.filePath(entry + QStringLiteral("/id")));
        if (!idFile.open(QIODevice::ReadOnly | QIODevice::Text)) continue;
        const QString cardId = QString::fromUtf8(idFile.readAll()).trimmed();
        if (cardId.isEmpty()) continue;
        devices.insert(QStringLiteral("hw:CARD=%1,DEV=0").arg(cardId));
        devices.insert(QStringLiteral("plughw:CARD=%1,DEV=0").arg(cardId));
        devices.insert(QStringLiteral("sysdefault:CARD=%1").arg(cardId));
    }
    QStringList out = devices.values();
    out.sort(Qt::CaseInsensitive);
    return out;
}

static QTreeWidgetItem* findItemByIdentity(QTreeWidget* tree, const QString& type, const QString& id) {
    if (!tree) return nullptr;
    QTreeWidgetItemIterator it(tree);
    while (*it) {
        const QJsonObject obj = (*it)->data(0, Qt::UserRole).toJsonObject();
        if (obj.value(QStringLiteral("_type")).toString() == type && obj.value(QStringLiteral("id")).toVariant().toString() == id) {
            return *it;
        }
        ++it;
    }
    return nullptr;
}

MainWindow::MainWindow(QWidget* parent) : QMainWindow(parent) {
    setWindowTitle(QStringLiteral("TIDAL Bitperfect Qt6"));
    resize(900, 650);
    buildUi();
    setupPlaybackSignals();
    connect(&m_sidecar, &TidalSidecar::statusMessage, this, &MainWindow::setStatus);
    connect(&m_sidecar, &TidalSidecar::fatalError, this, [this](const QString& msg) {
        if (networkOffline()) enterOfflineMode(msg);
        else QMessageBox::critical(this, QStringLiteral("Sidecar"), msg);
    });
    connect(&m_sidecar, &TidalSidecar::loginLink, this, &MainWindow::sidecarLoginLink);
    refreshDevices();
    const QString savedDevice = m_settings.value(QStringLiteral("qt6/alsa_device"), QStringLiteral("default")).toString();
    m_deviceCombo->setCurrentText(savedDevice);
    m_volume->setValue(qBound(0, m_settings.value(QStringLiteral("qt6/volume"), 100).toInt(), 100));
    m_searchLimit->setValue(qBound(1, m_settings.value(QStringLiteral("qt6/search_limit"), 10).toInt(), 50));
    m_gaplessEnabled = m_settings.value(QStringLiteral("qt6/gapless_enabled"), true).toBool();
    m_reduceAnimations = m_settings.value(QStringLiteral("qt6/reduce_animations"), false).toBool();
    m_discordEnabled = m_settings.value(QStringLiteral("qt6/discord_enabled"), true).toBool();
    m_discordClientId = m_settings.value(QStringLiteral("qt6/discord_client_id")).toString().trimmed();
    m_mprisEnabled = m_settings.value(QStringLiteral("qt6/mpris_enabled"), true).toBool();
    initDiscord();
    initMpris();
    login();
    refreshCacheTab();
}

void MainWindow::closeEvent(QCloseEvent* event) {
    stopLyricsScrollAnimation();
    shutdownDiscord();
    shutdownMpris();
    cleanupCurrentTempMpd();
    disconnect(&m_player, nullptr, this, nullptr);
    m_player.shutdown();
    QMainWindow::closeEvent(event);
}

bool MainWindow::eventFilter(QObject* watched, QEvent* event) {
    if (m_lyricsList
        && (watched == m_lyricsList
            || watched == m_lyricsList->viewport()
            || watched == m_lyricsList->verticalScrollBar())) {
        switch (event->type()) {
        case QEvent::Wheel:
        case QEvent::MouseButtonPress:
        case QEvent::MouseButtonDblClick:
        case QEvent::TouchBegin:
        case QEvent::TouchUpdate:
        case QEvent::KeyPress:
            holdLyricsAutoScroll();
            break;
        default:
            break;
        }
    }
    return QMainWindow::eventFilter(watched, event);
}

void MainWindow::buildUi() {
    auto* root = new QWidget(this);
    auto* main = new QVBoxLayout(root);
    m_loadingTimer = new QTimer(this);
    m_loadingTimer->setInterval(300);
    connect(m_loadingTimer, &QTimer::timeout, this, &MainWindow::tickLoadingLabels);

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
        m_sidecar.request(QStringLiteral("home"), {}, [this](const QJsonObject& result) {
            populateTree(m_homeTree, result.value(QStringLiteral("sections")).toArray(), QString(), true);
        });
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
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, albumId]() { openTidalItem(QStringLiteral("album"), albumId); });
            openAlbum->setEnabled(!m_offlineMode && !albumId.isEmpty());
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, artistId]() { openTidalItem(QStringLiteral("artist"), artistId); });
            openArtist->setEnabled(!m_offlineMode && !artistId.isEmpty());
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
    m_lyricsTitle = new QLabel(QStringLiteral("Lyrics"), lyricsPanel);
    QFont lyricsTitleFont = m_lyricsTitle->font();
    lyricsTitleFont.setPointSize(lyricsTitleFont.pointSize() + 3);
    lyricsTitleFont.setBold(true);
    m_lyricsTitle->setFont(lyricsTitleFont);
    m_lyricsTitle->setWordWrap(true);
    m_lyricsMeta = new QLabel(QString(), lyricsPanel);
    m_lyricsMeta->setWordWrap(true);
    m_lyricsMeta->setTextInteractionFlags(Qt::TextSelectableByMouse);
    auto* lyricsDivider = new QFrame(lyricsPanel);
    lyricsDivider->setFrameShape(QFrame::HLine);
    lyricsDivider->setFrameShadow(QFrame::Plain);
    lyricsDivider->setStyleSheet(QStringLiteral("color: rgba(255, 255, 255, 0.08);"));
    m_lyricsList = new QListWidget(lyricsPanel);
    m_lyricsList->setFrameShape(QFrame::NoFrame);
    m_lyricsList->setWordWrap(true);
    m_lyricsList->setSelectionMode(QAbstractItemView::NoSelection);
    m_lyricsList->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_lyricsList->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
    m_lyricsList->setFocusPolicy(Qt::NoFocus);
    m_lyricsList->setCursor(Qt::PointingHandCursor);
    m_lyricsList->setStyleSheet(QStringLiteral(
        "QListWidget { background: transparent; border: 0; }"
        "QListWidget::item { padding: 5px 2px; border-radius: 3px; }"
        "QListWidget::item:hover { background: rgba(255, 255, 255, 0.05); }"
        "QListWidget::item:selected { background: transparent; }"
        "QListWidget::item:focus { outline: none; }"
    ));
    QFont lyricsFont = m_lyricsList->font();
    lyricsFont.setPointSize(qMax(lyricsFont.pointSize() + 1, 13));
    m_lyricsList->setFont(lyricsFont);
    m_lyricsList->installEventFilter(this);
    m_lyricsList->viewport()->installEventFilter(this);
    m_lyricsList->verticalScrollBar()->installEventFilter(this);
    connect(m_lyricsList->verticalScrollBar(), &QScrollBar::actionTriggered, this, [this](int) {
        holdLyricsAutoScroll();
    });
    lyricsLayout->addWidget(m_lyricsTitle);
    lyricsLayout->addWidget(m_lyricsMeta);
    lyricsLayout->addWidget(lyricsDivider);
    lyricsLayout->addWidget(m_lyricsList, 1);
    m_lyricsList->addItem(QStringLiteral("Lyrics will appear for the currently playing track."));
    connect(m_lyricsList, &QListWidget::itemClicked, this, &MainWindow::seekToLyricItem);
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
            updateLyrics(m_positionSeconds);
            scrollLyricsToLine(m_currentLyricIndex, false);
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
            const QString id = item->data(Qt::UserRole).toString();
            const QJsonObject track = m_tracks.value(id);
            const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
            const QString artistId = track.value(QStringLiteral("artist_id")).toVariant().toString();
            menu.addAction(QStringLiteral("Play"), this, [this, item]() {
                playQueueRow(m_queueList->row(item));
            });
            menu.addAction(QStringLiteral("Play next"), this, [this, item]() {
                const int row = m_queueList->row(item);
                if (row <= 0 || row >= m_queue.size()) return;
                const QString id = m_queue.takeAt(row);
                m_queue.prepend(id);
                refreshQueueView();
                m_player.clearNextTrack();
                maybePrefetchNext();
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
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, albumId]() { openTidalItem(QStringLiteral("album"), albumId); });
            openAlbum->setEnabled(!m_offlineMode && !albumId.isEmpty());
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, artistId]() { openTidalItem(QStringLiteral("artist"), artistId); });
            openArtist->setEnabled(!m_offlineMode && !artistId.isEmpty());
            menu.addSeparator();
            addTrackStorageAction(&menu, track);
        }
        if (!item && !m_queue.isEmpty()) menu.addAction(QStringLiteral("Play next"), this, &MainWindow::playNextQueued);
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
    m_time->setMinimumWidth(92);
    auto* timeWrap = new QWidget(right);
    auto* timeLayout = new QHBoxLayout(timeWrap);
    timeLayout->setContentsMargins(0, 0, 0, 0);
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
        updateAudioStatusLabels();
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
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Left")), [this]() { m_player.seek(-10.0); });
    addShortcut(QKeySequence(QStringLiteral("Ctrl+Right")), [this]() { m_player.seek(10.0); });
    addShortcut(QKeySequence(QStringLiteral("J")), [this]() { if (!textInputFocused()) m_player.seek(-10.0); });
    addShortcut(QKeySequence(QStringLiteral("L")), [this]() { if (!textInputFocused()) m_player.seek(10.0); });

    split->addWidget(right);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    split->setSizes({680, 440});
    main->addWidget(split, 1);
    setCentralWidget(root);
}

void MainWindow::setupTreeActions(QTreeWidget* tree) {
    connect(tree, &QTreeWidget::itemActivated, this, &MainWindow::itemActivated);
    connect(tree, &QTreeWidget::itemExpanded, this, &MainWindow::onTreeItemExpanded);
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
            QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, albumId]() { openTidalItem(QStringLiteral("album"), albumId); });
            openAlbum->setEnabled(!m_offlineMode && !albumId.isEmpty());
            QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, artistId]() { openTidalItem(QStringLiteral("artist"), artistId); });
            openArtist->setEnabled(!m_offlineMode && !artistId.isEmpty());
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
        QAction* openAlbum = menu.addAction(QStringLiteral("Open album"), this, [this, albumId]() { openTidalItem(QStringLiteral("album"), albumId); });
        openAlbum->setEnabled(!m_offlineMode && !albumId.isEmpty());
        QAction* openArtist = menu.addAction(QStringLiteral("Open artist"), this, [this, artistId]() { openTidalItem(QStringLiteral("artist"), artistId); });
        openArtist->setEnabled(!m_offlineMode && !artistId.isEmpty());
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
    m_sidecar.request(QStringLiteral("login"), {}, [this](const QJsonObject&) {
        m_offlineMode = false;
        setNetworkTabsEnabled(true);
        setStatus(QStringLiteral("Ready"));
        m_sidecar.request(QStringLiteral("home"), {}, [this](const QJsonObject& result) {
            populateTree(m_homeTree, result.value(QStringLiteral("sections")).toArray(), QString(), true);
        });
        refreshFavoriteState();
        refreshCollection();
    }, [this](const QString& error) {
        if (networkOffline()) enterOfflineMode(error);
        else QMessageBox::critical(this, QStringLiteral("Login"), error);
    });
}

void MainWindow::sidecarLoginLink(const QString& url, const QString& code, int expiresSeconds) {
    setStatus(QStringLiteral("Authorize TIDAL login: %1").arg(code));
    QMessageBox::information(this, QStringLiteral("TIDAL Login"), QStringLiteral("Open this URL and authorize:\n%1\n\nCode: %2\nExpires in %3s").arg(url, code).arg(expiresSeconds));
    QDesktopServices::openUrl(QUrl(url));
}

void MainWindow::search() {
    if (!requireOnline(QStringLiteral("Search"))) return;
    QJsonObject args{{QStringLiteral("query"), m_searchEdit->text()}, {QStringLiteral("type"), mediaTypeKey(m_searchType->currentText())}, {QStringLiteral("limit"), m_searchLimit->value()}};
    m_sidecar.request(QStringLiteral("search"), args, [this](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        rememberTracks(items);
        populateTree(m_searchTree, items, result.value(QStringLiteral("type")).toString());
    });
}

void MainWindow::loadUrl() {
    if (!requireOnline(QStringLiteral("URL loading"))) return;
    m_sidecar.request(QStringLiteral("url"), {{QStringLiteral("url"), m_urlEdit->text()}}, [this](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        rememberTracks(items);
        populateTree(m_urlTree, items, result.value(QStringLiteral("type")).toString(), true, true);
    });
}

void MainWindow::queueUrl() {
    if (!requireOnline(QStringLiteral("URL queue"))) return;
    m_sidecar.request(QStringLiteral("url"), {{QStringLiteral("url"), m_urlEdit->text()}}, [this](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        rememberTracks(items);
        populateTree(m_urlTree, items, result.value(QStringLiteral("type")).toString(), true, true);
        for (const QJsonValue& value : items) {
            if (value.isObject()) playOrQueueObject(value.toObject(), false);
        }
    }, [this](const QString& error) { QMessageBox::critical(this, QStringLiteral("URL"), error); });
}

void MainWindow::refreshCollection() {
    if (!requireOnline(QStringLiteral("Collection refresh"))) return;
    m_sidecar.request(QStringLiteral("collection"), {{QStringLiteral("type"), mediaTypeKey(m_collectionType->currentText())}}, [this](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        rememberTracks(items);
        rememberFavoriteItems(result.value(QStringLiteral("type")).toString(), items);
        populateTree(m_collectionTree, items, result.value(QStringLiteral("type")).toString());
    });
}

void MainWindow::populateTree(QTreeWidget* tree, const QJsonArray& items, const QString& typeHint, bool expandRoots, bool loadSingleLazyRoot) {
    clearLoadingItemsForTree(tree);
    tree->clear();
    for (const QJsonValue& value : items) {
        if (value.isObject()) {
            tree->addTopLevelItem(makeItem(value.toObject(), typeHint));
        }
    }
    if (expandRoots) {
        for (int i = 0; i < tree->topLevelItemCount(); ++i) {
            tree->topLevelItem(i)->setExpanded(true);
        }
    }
    if (loadSingleLazyRoot && tree->topLevelItemCount() == 1) {
        QTreeWidgetItem* item = tree->topLevelItem(0);
        if (itemNeedsDetails(item)) loadContainerDetails(item);
    }
}

QTreeWidgetItem* MainWindow::makeItem(const QJsonObject& obj, const QString& typeHint) const {
    QString type = typeHint;
    QJsonObject data = obj;
    if (obj.contains(QStringLiteral("items"))) {
        type = QStringLiteral("section");
        data = obj;
    }
    if (obj.contains(QStringLiteral("data"))) {
        type = obj.value(QStringLiteral("type")).toString(type);
        data = obj.value(QStringLiteral("data")).toObject();
    }
    if (type.isEmpty()) {
        if (data.contains(QStringLiteral("items"))) type = QStringLiteral("section");
        else if (data.contains(QStringLiteral("name"))) type = QStringLiteral("artist");
        else if (data.contains(QStringLiteral("creator"))) type = QStringLiteral("playlist");
        else if (data.contains(QStringLiteral("tracks"))) type = QStringLiteral("album");
        else type = QStringLiteral("track");
    }
    QString text;
    if (type == QStringLiteral("section")) text = data.value(QStringLiteral("title")).toString(QStringLiteral("Section"));
    else if (type == QStringLiteral("artist")) text = QStringLiteral("Artist - %1").arg(data.value(QStringLiteral("name")).toString());
    else if (type == QStringLiteral("album")) text = QStringLiteral("Album - %1 - %2").arg(data.value(QStringLiteral("artist_display")).toString(data.value(QStringLiteral("artist")).toString()), data.value(QStringLiteral("title")).toString());
    else if (type == QStringLiteral("playlist")) text = QStringLiteral("Playlist - %1").arg(data.value(QStringLiteral("title")).toString());
    else if (type == QStringLiteral("mix")) text = QStringLiteral("Mix - %1").arg(data.value(QStringLiteral("title")).toString());
    else text = trackLine(data);
    auto* item = new QTreeWidgetItem(QStringList{text});
    data.insert(QStringLiteral("_type"), type);
    item->setData(0, Qt::UserRole, data);
    addChildren(item, data);
    prepareLazyContainer(item, data);
    return item;
}

void MainWindow::addChildren(QTreeWidgetItem* item, const QJsonObject& data) const {
    for (const QJsonValue& child : data.value(QStringLiteral("items")).toArray()) {
        if (child.isObject()) item->addChild(makeItem(child.toObject()));
    }
    if (data.value(QStringLiteral("_type")).toString() == QStringLiteral("artist")) {
        const QJsonArray tracks = data.value(QStringLiteral("tracks")).toArray();
        if (!tracks.isEmpty()) {
            auto* group = new QTreeWidgetItem(QStringList{QStringLiteral("Top tracks")});
            group->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("top_tracks_group")}, {QStringLiteral("tracks"), tracks}});
            item->addChild(group);
            for (const QJsonValue& track : tracks) {
                if (track.isObject()) group->addChild(makeItem(track.toObject(), QStringLiteral("track")));
            }
        }
        const auto addAlbumGroup = [this, item](const QString& label, const QJsonArray& albums) {
            if (albums.isEmpty()) return;
            auto* group = new QTreeWidgetItem(QStringList{label});
            group->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("group")}, {QStringLiteral("title"), label}});
            item->addChild(group);
            for (const QJsonValue& album : albums) {
                if (album.isObject()) group->addChild(makeItem(album.toObject(), QStringLiteral("album")));
            }
        };
        addAlbumGroup(QStringLiteral("Albums"), data.value(QStringLiteral("albums")).toArray());
        addAlbumGroup(QStringLiteral("EP & Singles"), data.value(QStringLiteral("ep_singles")).toArray());
        return;
    }
    for (const QJsonValue& track : data.value(QStringLiteral("tracks")).toArray()) {
        if (track.isObject()) item->addChild(makeItem(track.toObject(), QStringLiteral("track")));
    }
    for (const QJsonValue& album : data.value(QStringLiteral("albums")).toArray()) {
        if (album.isObject()) item->addChild(makeItem(album.toObject(), QStringLiteral("album")));
    }
    for (const QJsonValue& album : data.value(QStringLiteral("ep_singles")).toArray()) {
        if (album.isObject()) item->addChild(makeItem(album.toObject(), QStringLiteral("album")));
    }
}

void MainWindow::prepareLazyContainer(QTreeWidgetItem* item, const QJsonObject& data) const {
    if (!item) return;
    const QString type = data.value(QStringLiteral("_type")).toString();
    if (!isContainerType(type)) return;
    const bool hasLoadedArrays = !data.value(QStringLiteral("tracks")).toArray().isEmpty()
        || !data.value(QStringLiteral("items")).toArray().isEmpty()
        || !data.value(QStringLiteral("albums")).toArray().isEmpty()
        || !data.value(QStringLiteral("ep_singles")).toArray().isEmpty();
    if (item->childCount() > 0 || hasLoadedArrays) {
        item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
        return;
    }
    item->setData(0, kDetailsStateRole, QStringLiteral("unloaded"));
    auto* placeholder = new QTreeWidgetItem(QStringList{QStringLiteral("Loading")});
    placeholder->setData(0, kLoadingPlaceholderRole, true);
    placeholder->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("loading")}});
    placeholder->setFlags(placeholder->flags() & ~Qt::ItemIsSelectable);
    item->addChild(placeholder);
}

bool MainWindow::itemNeedsDetails(QTreeWidgetItem* item) const {
    if (!item) return false;
    const QJsonObject obj = itemObject(item);
    if (!isContainerType(obj.value(QStringLiteral("_type")).toString())) return false;
    if (obj.value(QStringLiteral("id")).toVariant().toString().isEmpty()) return false;
    const QString state = item->data(0, kDetailsStateRole).toString();
    return state != QStringLiteral("loading") && state != QStringLiteral("loaded");
}

bool MainWindow::isLoadingPlaceholder(QTreeWidgetItem* item) const {
    return item && item->data(0, kLoadingPlaceholderRole).toBool();
}

void MainWindow::showLoadingPlaceholder(QTreeWidgetItem* item) {
    if (!item) return;
    if (item->childCount() == 1 && isLoadingPlaceholder(item->child(0))) {
        QTreeWidgetItem* placeholder = item->child(0);
        placeholder->setText(0, QStringLiteral("Loading"));
        registerLoadingItem(placeholder);
        item->setExpanded(true);
        return;
    }
    for (int i = 0; i < item->childCount(); ++i) unregisterLoadingItem(item->child(i));
    qDeleteAll(item->takeChildren());
    auto* placeholder = new QTreeWidgetItem(QStringList{QStringLiteral("Loading")});
    placeholder->setData(0, kLoadingPlaceholderRole, true);
    placeholder->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("loading")}});
    placeholder->setFlags(placeholder->flags() & ~Qt::ItemIsSelectable);
    item->addChild(placeholder);
    registerLoadingItem(placeholder);
    item->setExpanded(true);
}

void MainWindow::addEmptyContainerPlaceholder(QTreeWidgetItem* item, const QString& type) {
    if (!item || item->childCount() > 0) return;
    const QString label = type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found");
    auto* empty = new QTreeWidgetItem(QStringList{label});
    empty->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
    empty->setFlags(empty->flags() & ~Qt::ItemIsSelectable);
    item->addChild(empty);
}

void MainWindow::registerLoadingItem(QTreeWidgetItem* item) {
    if (!item || m_loadingItems.contains(item)) return;
    m_loadingItems.push_back(item);
    if (m_loadingTimer && !m_loadingTimer->isActive()) m_loadingTimer->start();
}

void MainWindow::unregisterLoadingItem(QTreeWidgetItem* item) {
    if (!item) return;
    m_loadingItems.removeAll(item);
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void MainWindow::clearLoadingItemsForTree(QTreeWidget* tree) {
    if (!tree) return;
    QVector<QTreeWidgetItem*> alive;
    for (QTreeWidgetItem* item : std::as_const(m_loadingItems)) {
        if (item && item->treeWidget() != tree) alive.push_back(item);
    }
    m_loadingItems = alive;
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void MainWindow::tickLoadingLabels() {
    if (m_loadingItems.isEmpty()) {
        if (m_loadingTimer) m_loadingTimer->stop();
        return;
    }
    const QStringList phases{QStringLiteral("Loading"), QStringLiteral("Loading."), QStringLiteral("Loading.."), QStringLiteral("Loading...")};
    m_loadingPhase = (m_loadingPhase + 1) % phases.size();
    QVector<QTreeWidgetItem*> alive;
    for (QTreeWidgetItem* item : std::as_const(m_loadingItems)) {
        if (!item || !item->treeWidget()) continue;
        item->setText(0, phases.at(m_loadingPhase));
        alive.push_back(item);
    }
    m_loadingItems = alive;
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void MainWindow::onTreeItemExpanded(QTreeWidgetItem* item) {
    if (itemNeedsDetails(item)) loadContainerDetails(item);
}

QJsonObject MainWindow::itemObject(QTreeWidgetItem* item) const {
    return item ? item->data(0, Qt::UserRole).toJsonObject() : QJsonObject{};
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
        return m_tracks.value(m_queueList->currentItem()->data(Qt::UserRole).toString());
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
    return m_currentTrackId.isEmpty() ? QJsonObject{} : m_tracks.value(m_currentTrackId);
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
    if (!item) return;
    if (!requireOnline(QStringLiteral("Details loading"))) return;
    const QJsonObject obj = itemObject(item);
    const QString type = obj.value(QStringLiteral("_type")).toString();
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    if (!isContainerType(type) || id.isEmpty()) return;
    const QString state = item->data(0, kDetailsStateRole).toString();
    if (state == QStringLiteral("loaded")) {
        if (playAfterLoad || queueAfterLoad) playOrQueueObject(obj, playAfterLoad);
        return;
    }
    if (state == QStringLiteral("loading")) return;
    QTreeWidget* tree = item->treeWidget();
    item->setData(0, kDetailsStateRole, QStringLiteral("loading"));
    showLoadingPlaceholder(item);
    setStatus(QStringLiteral("Loading %1...").arg(type));
    m_sidecar.request(QStringLiteral("details"), {{QStringLiteral("type"), type}, {QStringLiteral("id"), id}}, [this, tree, type, id, playAfterLoad, queueAfterLoad](const QJsonObject& result) {
        QTreeWidgetItem* item = findItemByIdentity(tree, type, id);
        if (!item) return;
        QJsonObject detail = result.value(QStringLiteral("item")).toObject();
        if (detail.isEmpty()) {
            item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
            bool reusedPlaceholder = false;
            for (int i = 0; i < item->childCount(); ++i) {
                QTreeWidgetItem* child = item->child(i);
                if (!isLoadingPlaceholder(child)) continue;
                unregisterLoadingItem(child);
                child->setText(0, type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found"));
                child->setData(0, kLoadingPlaceholderRole, false);
                child->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
                reusedPlaceholder = true;
                break;
            }
            if (!reusedPlaceholder) addEmptyContainerPlaceholder(item, type);
            return;
        }
        detail.insert(QStringLiteral("_type"), type);
        item->setData(0, Qt::UserRole, detail);
        addChildren(item, detail);
        item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
        if (type == QStringLiteral("album") && !m_player.busy()) {
            const QString coverUrl = detail.value(QStringLiteral("cover_url")).toString();
            QString albumId = detail.value(QStringLiteral("id")).toVariant().toString();
            if (albumId.isEmpty()) albumId = id;
            requestCover(coverUrl, QStringLiteral("album:%1").arg(albumId.isEmpty() ? coverUrl : albumId), albumId);
        }
        bool hasRealChildren = false;
        for (int i = 0; i < item->childCount(); ++i) {
            if (!isLoadingPlaceholder(item->child(i))) {
                hasRealChildren = true;
                break;
            }
        }
        if (hasRealChildren) {
            for (int i = item->childCount() - 1; i >= 0; --i) {
                QTreeWidgetItem* child = item->child(i);
                if (!isLoadingPlaceholder(child)) continue;
                unregisterLoadingItem(child);
                delete item->takeChild(i);
            }
        } else if (item->childCount() > 0 && isLoadingPlaceholder(item->child(0))) {
            QTreeWidgetItem* child = item->child(0);
            unregisterLoadingItem(child);
            child->setText(0, type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found"));
            child->setData(0, kLoadingPlaceholderRole, false);
            child->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
        } else {
            addEmptyContainerPlaceholder(item, type);
        }
        item->setExpanded(true);
        rememberTracks(QJsonArray{detail});
        setStatus(QStringLiteral("Loaded %1").arg(type));
        if (playAfterLoad || queueAfterLoad) playOrQueueObject(detail, playAfterLoad);
    }, [this, tree, type, id](const QString& error) {
        QTreeWidgetItem* item = findItemByIdentity(tree, type, id);
        if (item) {
            for (int i = 0; i < item->childCount(); ++i) unregisterLoadingItem(item->child(i));
            qDeleteAll(item->takeChildren());
            item->setData(0, kDetailsStateRole, QStringLiteral("unloaded"));
            item->setChildIndicatorPolicy(QTreeWidgetItem::ShowIndicator);
        }
        QMessageBox::critical(this, QStringLiteral("Details"), error);
    });
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
        setNetworkTabsEnabled(true);
        updateDiscordContext();
        setStatus(QStringLiteral("Network restored; logging in..."));
        login();
    }
}

void MainWindow::enterOfflineMode(const QString& reason) {
    m_offlineMode = true;
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
    m_sidecar.request(QStringLiteral("radio"), args, [this, playFirst](const QJsonObject& result) {
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
            if (m_player.busy() || !m_currentTrackId.isEmpty()) {
                for (const QJsonObject& track : tracks) appendQueue(track);
            } else {
                startPlayback(tracks.first());
                for (qsizetype i = 1; i < tracks.size(); ++i) appendQueue(tracks.at(i));
            }
        } else {
            const bool shouldStart = m_currentTrackId.isEmpty() && !m_player.busy();
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
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) return;
    m_tracks[id] = track;
    m_queue.push_back(id);
    refreshQueueView();
    maybePrefetchNext();
}

void MainWindow::insertQueueNext(const QJsonObject& track) {
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    if (id.isEmpty()) return;
    m_tracks[id] = track;
    m_queue.prepend(id);
    refreshQueueView();
    m_player.clearNextTrack();
    maybePrefetchNext();
}

void MainWindow::refreshQueueView() {
    m_queueList->clear();
    for (qsizetype i = 0; i < m_queue.size(); ++i) {
        const QString id = m_queue.at(i);
        QString text = trackLine(m_tracks.value(id));
        if (i == 0) text = QStringLiteral("Next: %1").arg(text);
        auto* item = new QListWidgetItem(text);
        item->setData(Qt::UserRole, id);
        m_queueList->addItem(item);
    }
    updateQueueTabLabel();
    updateDiscordContext();
    updateMprisQueueState();
}

void MainWindow::clearQueue() {
    m_queue.clear();
    refreshQueueView();
    m_player.clearNextTrack();
}

void MainWindow::updateQueueTabLabel() {
    if (!m_detailsTabs || m_queueTabIndex < 0) return;
    const int count = m_queue.size();
    m_detailsTabs->setTabText(m_queueTabIndex, count ? QStringLiteral("Queue (%1)").arg(count) : QStringLiteral("Queue"));
}

void MainWindow::removeQueueRow(int row) {
    if (row < 0 || row >= m_queue.size()) return;
    const bool removedPrefetched = row == 0;
    m_queue.removeAt(row);
    refreshQueueView();
    if (removedPrefetched) {
        m_player.clearNextTrack();
        maybePrefetchNext();
    }
}

void MainWindow::playQueueRow(int row) {
    if (row < 0 || row >= m_queue.size()) return;
    const QString id = m_queue.at(row);
    m_queue.removeAt(row);
    refreshQueueView();
    m_player.clearNextTrack();
    m_userStopped = false;
    startPlayback(m_tracks.value(id));
}

void MainWindow::playNextQueued() {
    if (m_queue.isEmpty()) return;
    const QString id = m_queue.takeFirst();
    refreshQueueView();
    m_player.clearNextTrack();
    m_userStopped = false;
    startPlayback(m_tracks.value(id));
}

void MainWindow::startPlayback(const QJsonObject& track) {
    if (track.isEmpty()) return;
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    const QString cached = m_cache.downloadPath(id).isEmpty() ? m_cache.cachedAudioPath(id) : m_cache.downloadPath(id);
    if (cached.isEmpty() && m_offlineMode) {
        setStatus(QStringLiteral("Track is not available in cache/downloads"));
        return;
    }
    if (m_player.busy() && !m_currentTrackId.isEmpty() && id != m_currentTrackId) {
        m_replacingPlayback = true;
        m_player.clearNextTrack();
        m_player.stop();
        cleanupCurrentTempMpd();
    }
    m_userStopped = false;
    m_paused = false;
    m_positionSeconds = 0.0;
    m_duration = 0.0;
    updatePauseButton();
    m_settings.setValue(QStringLiteral("qt6/alsa_device"), m_deviceCombo->currentText());
    m_tracks[id] = track;
    m_currentTrackId = id;
    setNowPlaying(track);
    if (!cached.isEmpty()) {
        m_replacingPlayback = false;
        m_player.playFile(cached, m_deviceCombo->currentText(), m_volume->value());
        maybePrefetchNext();
        return;
    }
    requestStreamAndPlay(track);
}

void MainWindow::requestStreamAndPlay(const QJsonObject& track) {
    if (!requireOnline(QStringLiteral("Streaming"))) {
        setStatus(QStringLiteral("Track is not available in cache/downloads"));
        return;
    }
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    m_sidecar.request(QStringLiteral("stream"), {{QStringLiteral("track_id"), id}}, [this, track](const QJsonObject& result) {
        playStreamDescriptor(track, result);
    }, [this, id](const QString& error) {
        if (id == m_currentTrackId) QMessageBox::critical(this, QStringLiteral("Stream"), error);
    });
}

void MainWindow::playStreamDescriptor(const QJsonObject& track, const QJsonObject& stream) {
    const QString trackId = track.value(QStringLiteral("id")).toVariant().toString();
    if (!trackId.isEmpty() && trackId != m_currentTrackId) {
        const QString staleMpd = stream.value(QStringLiteral("mpd_path")).toString();
        if (!staleMpd.isEmpty()) QFile::remove(staleMpd);
        return;
    }
    QJsonObject activeTrack = track;
    const QJsonObject resolvedTrack = stream.value(QStringLiteral("track")).toObject();
    if (!resolvedTrack.isEmpty()) {
        for (const QString& key : {
                 QStringLiteral("artist_id"),
                 QStringLiteral("artists"),
                 QStringLiteral("artist_display"),
                 QStringLiteral("album"),
                 QStringLiteral("album_id"),
                 QStringLiteral("cover_url"),
                 QStringLiteral("cover_thumbnail_url"),
                 QStringLiteral("audio_quality"),
                 QStringLiteral("track_max_quality"),
             }) {
            const QJsonValue existing = activeTrack.value(key);
            const QJsonValue resolved = resolvedTrack.value(key);
            if ((existing.isUndefined() || existing.isNull() || existing.toVariant().toString().isEmpty())
                && !resolved.isUndefined()
                && !resolved.isNull()) {
                activeTrack.insert(key, resolved);
            }
        }
        if (!trackId.isEmpty() && activeTrack != track) {
            m_tracks[trackId] = activeTrack;
            updateDiscordTrack(activeTrack);
            loadCover(activeTrack);
        }
    }
    cleanupCurrentTempMpd();
    m_currentTempMpd = stream.value(QStringLiteral("mpd_path")).toString();
    const QString input = stream.value(QStringLiteral("input")).toString();
    const bool protocol = stream.value(QStringLiteral("is_dash")).toBool(false);
    const int bits = stream.value(QStringLiteral("bit_depth")).toInt(16);
    const QString codec = bits >= 24 ? QStringLiteral("pcm_s32le") : QStringLiteral("pcm_s16le");
    m_duration = stream.value(QStringLiteral("duration_s")).toDouble();
    m_streamBitDepth = bits;
    m_streamSampleRate = stream.value(QStringLiteral("sample_rate")).toInt();
    QString audioQuality = stream.value(QStringLiteral("audio_quality")).toString(stream.value(QStringLiteral("track_max_quality")).toString());
    if (audioQuality.isEmpty()) {
        audioQuality = activeTrack.value(QStringLiteral("audio_quality")).toString(activeTrack.value(QStringLiteral("track_max_quality")).toString());
    }
    m_quality->setText(qualityLabelText(audioQuality, bits, m_streamSampleRate));
    updateAudioStatusLabels();
    if (activeTrack != track) updateMprisTrack(activeTrack, m_duration);
    updateDiscordContext();
    updateMprisPosition(0.0, m_duration);
    m_replacingPlayback = false;
    m_player.playFfmpeg(input, m_deviceCombo->currentText(), m_volume->value(), codec, m_duration, protocol);
    maybePrefetchNext();
}

void MainWindow::setupPlaybackSignals() {
    connect(&m_player, &NativePlaybackClient::statusMessage, this, [this](const QString& message) {
        setStatus(message);
        if (message == QStringLiteral("Paused")) {
            m_paused = true;
            updatePauseButton();
            updateDiscordPlaybackStatus();
            updateMprisPlaybackStatus();
        } else if (message == QStringLiteral("Playing")) {
            m_paused = false;
            updatePauseButton();
            updateDiscordPlaybackStatus();
            updateMprisPlaybackStatus();
        }
    });
    connect(&m_player, &NativePlaybackClient::logMessage, this, &MainWindow::setStatus);
    connect(&m_player, &NativePlaybackClient::errorMessage, this, [this](const QString& msg) {
        cleanupCurrentTempMpd();
        m_replacingPlayback = false;
        m_paused = false;
        updatePauseButton();
        if (m_discord) m_discord->clearActivity();
        if (m_mpris) m_mpris->clearTrack();
        QMessageBox::critical(this, QStringLiteral("Playback"), msg);
    });
    connect(&m_player, &NativePlaybackClient::position, this, [this](double pos, double duration) {
        m_positionSeconds = pos;
        m_duration = duration;
        m_seek->setRange(0, qMax(0, static_cast<int>(duration * 1000)));
        m_seek->setEnabled(duration > 0.0);
        updateDiscordPosition(pos, duration);
        updateMprisPosition(pos, duration);
        if (seekPreviewActive(pos)) {
            if (!m_seek->isSliderDown()) m_seek->setValue(static_cast<int>(m_seekPreviewTarget * 1000));
            m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(m_seekPreviewTarget), formatTime(duration)));
            return;
        }
        m_seekPreviewTarget = -1.0;
        m_seekPreviewUntilMs = 0;
        if (!m_seek->isSliderDown()) m_seek->setValue(static_cast<int>(pos * 1000));
        m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(pos), formatTime(duration)));
        updateLyrics(pos);
    });
    connect(&m_player, &NativePlaybackClient::formatReady, this, [this](const NativeAudioFormat& fmt) {
        m_outputChannels = fmt.channels;
        m_outputRate = fmt.rate;
        m_outputBits = fmt.bits;
        if (m_streamSampleRate <= 0 && fmt.rate > 0) m_streamSampleRate = fmt.rate;
        if (m_streamBitDepth <= 0 && fmt.bits > 0) m_streamBitDepth = fmt.bits;
        if (fmt.duration > 0.0) {
            m_duration = fmt.duration;
            updateDiscordPosition(m_positionSeconds, fmt.duration);
            updateMprisPosition(m_positionSeconds, fmt.duration);
        }
        const QJsonObject track = currentTrackObject();
        const QString audioQuality = track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString());
        m_quality->setText(qualityLabelText(audioQuality, m_streamBitDepth, m_streamSampleRate));
        updateAudioStatusLabels();
        updateDiscordContext();
    });
    connect(&m_player, &NativePlaybackClient::advanced, this, &MainWindow::playbackAdvanced);
    connect(&m_player, &NativePlaybackClient::finishedOk, this, &MainWindow::playbackDone);
}

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
    m_discord->updateTrack(track, m_duration);
    updateDiscordContext();
}

void MainWindow::updateDiscordContext() {
    if (!m_discord || !m_discordEnabled) return;
    const bool local = trackIsLocal(m_currentTrackId);
    m_discord->updateContext(
        m_quality ? m_quality->text() : QString(),
        m_bitperfect ? m_bitperfect->text() : QString(),
        local,
        m_offlineMode,
        m_queue.size()
    );
}

void MainWindow::updateDiscordPlaybackStatus() {
    if (!m_discord || !m_discordEnabled) return;
    m_discord->setPlaying(m_player.busy() && !m_paused);
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
        if (!currentTrackObject().isEmpty()) updateMprisTrack(currentTrackObject(), m_duration);
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
    if (!m_player.busy()) m_mpris->setPlaybackStatus(QStringLiteral("Stopped"));
    else m_mpris->setPlaybackStatus(m_paused ? QStringLiteral("Paused") : QStringLiteral("Playing"));
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
    m_mpris->setCanGoNext(!m_queue.isEmpty());
}

void MainWindow::mprisPlay() {
    if (m_player.busy()) {
        if (m_paused) togglePause();
        return;
    }
    const QJsonObject current = currentTrackObject();
    if (!current.isEmpty()) startPlayback(current);
    else playSelected();
}

void MainWindow::mprisPause() {
    if (m_player.busy() && !m_paused) togglePause();
}

void MainWindow::mprisNext() {
    if (!m_queue.isEmpty()) playNextQueued();
    else if (m_player.busy()) stopPlayback();
}

void MainWindow::mprisSeek(double offsetSeconds) {
    if (!m_player.busy()) return;
    double target = qMax(0.0, m_positionSeconds + offsetSeconds);
    if (m_duration > 0.0) target = qMin(target, m_duration);
    beginSeekPreview(target);
    m_player.seekTo(target);
    notifyDiscordSeeked(target, m_duration);
    updateMprisPosition(target, m_duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}

void MainWindow::mprisSetPosition(double positionSeconds) {
    if (!m_player.busy()) return;
    double target = qMax(0.0, positionSeconds);
    if (m_duration > 0.0) target = qMin(target, m_duration);
    beginSeekPreview(target);
    m_player.seekTo(target);
    notifyDiscordSeeked(target, m_duration);
    updateMprisPosition(target, m_duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}

void MainWindow::maybePrefetchNext() {
    if (!m_gaplessEnabled || m_queue.isEmpty()) {
        m_player.clearNextTrack();
        return;
    }
    const QString id = m_queue.first();
    const QString path = m_cache.downloadPath(id).isEmpty() ? m_cache.cachedAudioPath(id) : m_cache.downloadPath(id);
    if (!path.isEmpty()) m_player.setNextTrack(id, path);
    else m_player.clearNextTrack();
}

void MainWindow::cleanupCurrentTempMpd() {
    if (m_currentTempMpd.isEmpty()) return;
    QFile::remove(m_currentTempMpd);
    m_currentTempMpd.clear();
}

void MainWindow::playbackAdvanced(const QString& trackId) {
    if (!m_queue.isEmpty() && m_queue.first() == trackId) m_queue.pop_front();
    m_currentTrackId = trackId;
    m_paused = false;
    m_positionSeconds = 0.0;
    updatePauseButton();
    refreshQueueView();
    setNowPlaying(m_tracks.value(trackId));
    maybePrefetchNext();
    updateDiscordContext();
}

void MainWindow::playbackDone() {
    m_paused = false;
    updatePauseButton();
    m_seek->setEnabled(false);
    if (m_replacingPlayback) {
        m_replacingPlayback = false;
        return;
    }
    cleanupCurrentTempMpd();
    if (m_userStopped) {
        m_userStopped = false;
        return;
    }
    if (!m_queue.isEmpty()) playNextQueued();
    else {
        if (m_discord) m_discord->clearActivity();
        if (m_mpris) m_mpris->clearTrack();
    }
}

void MainWindow::stopPlayback() {
    m_userStopped = true;
    m_paused = false;
    updatePauseButton();
    m_player.clearNextTrack();
    m_player.stop();
    setStatus(QStringLiteral("Stopped"));
    if (m_discord) m_discord->clearActivity();
    if (m_mpris) m_mpris->clearTrack();
}

void MainWindow::togglePause() {
    if (!m_player.busy()) {
        const QJsonObject current = currentTrackObject();
        if (!current.isEmpty()) startPlayback(current);
        else playSelected();
        return;
    }
    m_paused = !m_paused;
    updatePauseButton();
    updateDiscordPlaybackStatus();
    updateMprisPlaybackStatus();
    m_player.pauseToggle();
}
void MainWindow::seekReleased() {
    const double target = static_cast<double>(m_seek->value()) / 1000.0;
    beginSeekPreview(target);
    m_player.seekTo(target);
    updateMprisPosition(target, m_duration);
    notifyDiscordSeeked(target, m_duration);
    if (m_mpris) m_mpris->notifySeeked(target);
}
void MainWindow::volumeChanged(int value) {
    m_settings.setValue(QStringLiteral("qt6/volume"), value);
    if (m_volumeLabel) m_volumeLabel->setText(QStringLiteral("%1%").arg(value));
    m_player.setVolume(value);
    updateAudioStatusLabels();
    updateMprisVolume();
}

void MainWindow::updatePauseButton() {
    if (!m_pauseButton) return;
    if (!m_player.busy()) m_pauseButton->setText(QStringLiteral("Play"));
    else m_pauseButton->setText(m_paused ? QStringLiteral("Resume") : QStringLiteral("Pause"));
}

bool MainWindow::volumeControlAvailable() const {
    const QString device = m_deviceCombo ? m_deviceCombo->currentText().trimmed() : QString();
    return !device.isEmpty() && !device.startsWith(QStringLiteral("hw:"));
}

void MainWindow::updateAudioStatusLabels() {
    const bool volumeAvailable = volumeControlAvailable();
    if (m_volume) m_volume->setEnabled(volumeAvailable);
    if (m_volumeLabel) m_volumeLabel->setEnabled(volumeAvailable);

    if (m_outputChannels > 0 && m_outputRate > 0 && m_outputBits > 0) {
        const double outputKbps = (m_outputChannels * m_outputRate * m_outputBits) / 1000.0;
        if (m_streamSampleRate > 0 && m_streamBitDepth > 0) {
            const double streamKbps = (m_outputChannels * m_streamSampleRate * m_streamBitDepth) / 1000.0;
            m_bitrate->setText(QStringLiteral("Bitrate: stream ~%1 kbps | output PCM %2 kbps").arg(streamKbps, 0, 'f', 0).arg(outputKbps, 0, 'f', 0));
        } else {
            m_bitrate->setText(QStringLiteral("Bitrate: output PCM %1 kbps").arg(outputKbps, 0, 'f', 0));
        }
    } else {
        m_bitrate->setText(QStringLiteral("Bitrate: —"));
    }

    const QString device = m_deviceCombo ? m_deviceCombo->currentText().trimmed() : QString();
    if (device.isEmpty()) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: —"));
    } else if (!device.startsWith(QStringLiteral("hw:"))) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: unlikely (not hw:)"));
    } else if (m_streamSampleRate <= 0 || m_streamBitDepth <= 0 || m_outputRate <= 0 || m_outputBits <= 0) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: unknown (stream/format pending)"));
    } else if (m_outputRate != m_streamSampleRate) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: no (%1Hz != %2Hz)").arg(m_outputRate).arg(m_streamSampleRate));
    } else if (m_outputBits == m_streamBitDepth) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: yes"));
    } else if (m_streamBitDepth == 24 && m_outputBits == 32) {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: padded (24/32 PCM)"));
    } else {
        m_bitperfect->setText(QStringLiteral("Bit-perfect: no (%1-bit != %2-bit)").arg(m_outputBits).arg(m_streamBitDepth));
    }
}

void MainWindow::refreshCacheTab() {
    m_cache.refresh();
    auto trackObjectForEntry = [this](const CacheManagerQt::Entry& entry) {
        QJsonObject obj = m_tracks.value(entry.id);
        obj.insert(QStringLiteral("id"), entry.id);
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
    }
    m_downloadList->clear();
    for (const auto& entry : m_cache.downloads()) {
        const QJsonObject obj = trackObjectForEntry(entry);
        auto* item = new QListWidgetItem(trackLine(obj));
        item->setData(Qt::UserRole, obj);
        m_downloadList->addItem(item);
        m_tracks[entry.id] = obj;
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
    m_player.clearNextTrack();
    maybePrefetchNext();
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
    m_player.clearNextTrack();
    maybePrefetchNext();
}

void MainWindow::updateCacheStatusLabels() {
    const CacheManagerQt::Stats audio = m_cache.audioStats();
    const CacheManagerQt::Stats covers = m_cache.coverStats();
    const CacheManagerQt::Stats downloads = m_cache.downloadStats();
    if (m_cacheStatusLabel) {
        const QString prefix = m_offlineMode ? QStringLiteral("Offline | ") : QString();
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
    for (const QString& type : {QStringLiteral("track"), QStringLiteral("album"), QStringLiteral("playlist"), QStringLiteral("artist")}) {
        m_sidecar.request(
            QStringLiteral("collection"),
            {{QStringLiteral("type"), type}},
            [this, type](const QJsonObject& result) {
                const QJsonArray items = result.value(QStringLiteral("items")).toArray();
                rememberFavoriteItems(result.value(QStringLiteral("type")).toString(type), items);
                rememberTracks(items);
            },
            [this, type](const QString& error) {
                setStatus(QStringLiteral("Favorite state sync failed for %1: %2").arg(type, error));
            }
        );
    }
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
    m_sidecar.request(
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
        if (!id.isEmpty() && shouldRememberTrackObject(obj, type)) m_tracks[id] = obj;
        for (const QJsonValue& track : obj.value(QStringLiteral("tracks")).toArray()) {
            const QJsonObject t = track.toObject();
            const QString tid = t.value(QStringLiteral("id")).toVariant().toString();
            if (!tid.isEmpty()) m_tracks[tid] = t;
        }
    }
}

void MainWindow::setNowPlaying(const QJsonObject& track) {
    m_title->setText(track.value(QStringLiteral("title")).toString(QStringLiteral("Unknown title")));
    const QString album = track.value(QStringLiteral("album")).toString();
    const QString artist = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString());
    m_meta->setText(album.isEmpty() ? artist : QStringLiteral("%1 — %2").arg(artist, album));
    m_bitrate->setText(QStringLiteral("Bitrate: —"));
    m_bitperfect->setText(QStringLiteral("Bit-perfect: —"));
    m_streamSampleRate = track.value(QStringLiteral("sample_rate")).toInt();
    m_streamBitDepth = track.value(QStringLiteral("bit_depth")).toInt();
    m_outputRate = 0;
    m_outputBits = 0;
    m_outputChannels = 0;
    const QString audioQuality = track.value(QStringLiteral("audio_quality")).toString(track.value(QStringLiteral("track_max_quality")).toString());
    m_quality->setText(qualityLabelText(audioQuality, m_streamBitDepth, m_streamSampleRate));
    updateAudioStatusLabels();
    updateDiscordTrack(track);
    updateMprisTrack(track, 0.0);
    const QString coverUrl = track.value(QStringLiteral("cover_url")).toString();
    const QString albumId = track.value(QStringLiteral("album_id")).toVariant().toString();
    const bool currentCoverMatches = (!coverUrl.isEmpty() && coverUrl == m_displayedCoverUrl)
        || (!albumId.isEmpty() && albumId == m_displayedCoverAlbumId);
    if (!currentCoverMatches) {
        if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(QPixmap());
        else {
            m_cover->setText(QStringLiteral("No cover"));
            m_cover->setPixmap(QPixmap());
        }
    }
    loadCover(track);
    loadLyrics(track.value(QStringLiteral("id")).toVariant().toString());
}

void MainWindow::loadCoverForSelected() {
    if (m_player.busy()) return;
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
    const QString coverUrl = obj.value(QStringLiteral("cover_url")).toString();
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    QString albumId = obj.value(QStringLiteral("album_id")).toVariant().toString();
    if (type == QStringLiteral("album") && albumId.isEmpty()) albumId = id;
    const QString requestId = id.isEmpty()
        ? QStringLiteral("%1:%2").arg(type, coverUrl)
        : QStringLiteral("%1:%2").arg(type, id);
    requestCover(coverUrl, requestId, albumId);
}

void MainWindow::loadCover(const QJsonObject& track) {
    const QString coverUrl = track.value(QStringLiteral("cover_url")).toString();
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
        QPixmap pix;
        pix.loadFromData(reply->readAll());
        if (!pix.isNull()) {
            if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) cover->setCoverPixmap(pix);
            else m_cover->setPixmap(pix.scaled(m_cover->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
            m_displayedCoverUrl = coverUrl;
            m_displayedCoverAlbumId = albumId;
        } else if (auto* cover = dynamic_cast<CoverLabel*>(m_cover)) {
            cover->setCoverPixmap(QPixmap());
            m_displayedCoverUrl.clear();
            m_displayedCoverAlbumId.clear();
        }
        reply->deleteLater();
    });
}

void MainWindow::loadLyrics(const QString& trackId) {
    stopLyricsScrollAnimation();
    m_lyricsAutoScrollHoldUntilMs = 0;
    m_timedLyrics = {};
    m_currentLyricIndex = -1;
    if (m_lyricsTitle) m_lyricsTitle->setText(m_title->text());
    if (m_lyricsMeta) m_lyricsMeta->clear();
    m_lyricsList->clear();
    if (m_offlineMode) {
        m_lyricsList->addItem(QStringLiteral("Lyrics unavailable while offline."));
        return;
    }
    m_lyricsList->addItem(QStringLiteral("Loading lyrics..."));
    m_sidecar.request(QStringLiteral("lyrics"), {{QStringLiteral("track_id"), trackId}}, [this, trackId](const QJsonObject& result) {
        if (trackId != m_currentTrackId) return;
        const QString provider = result.value(QStringLiteral("provider")).toString();
        if (m_lyricsMeta) m_lyricsMeta->setText(provider.isEmpty() ? QString() : QStringLiteral("Source: %1").arg(provider));
        m_timedLyrics = result.value(QStringLiteral("timed_lines")).toArray();
        m_currentLyricIndex = -1;
        m_lyricsList->clear();
        if (!m_timedLyrics.isEmpty()) {
            for (const QJsonValue& value : m_timedLyrics) {
                const QJsonObject line = value.toObject();
                const QString text = line.value(QStringLiteral("text")).toString();
                auto* item = new QListWidgetItem(text.isEmpty() ? QStringLiteral(" ") : text);
                item->setData(Qt::UserRole, line.value(QStringLiteral("start_s")).toDouble(-1.0));
                item->setToolTip(QStringLiteral("Click to seek to %1").arg(formatTime(line.value(QStringLiteral("start_s")).toDouble())));
                item->setForeground(QBrush(QColor(136, 136, 136)));
                m_lyricsList->addItem(item);
            }
            updateLyrics(0.0);
            return;
        }
        const QString text = result.value(QStringLiteral("text")).toString();
        const QStringList lines = (text.isEmpty() ? QStringLiteral("No lyrics.") : text).split('\n');
        for (const QString& line : lines) m_lyricsList->addItem(line);
    }, [this, trackId](const QString&) {
        if (trackId == m_currentTrackId) {
            m_lyricsList->clear();
            m_lyricsList->addItem(QStringLiteral("Lyrics unavailable."));
        }
    });
}

void MainWindow::updateLyrics(double positionSeconds) {
    if (m_timedLyrics.isEmpty()) return;
    int active = -1;
    for (qsizetype i = 0; i < m_timedLyrics.size(); ++i) {
        const QJsonObject line = m_timedLyrics.at(i).toObject();
        const double start = line.value(QStringLiteral("start_s")).toDouble(-1.0);
        const QJsonValue endValue = line.value(QStringLiteral("end_s"));
        double end = endValue.isDouble() ? endValue.toDouble() : -1.0;
        if (end <= 0.0 && i + 1 < m_timedLyrics.size()) {
            end = m_timedLyrics.at(i + 1).toObject().value(QStringLiteral("start_s")).toDouble(-1.0);
        }
        if (start >= 0.0 && positionSeconds >= start && (end <= 0.0 || positionSeconds < end)) {
            active = static_cast<int>(i);
            break;
        }
        if (start >= 0.0 && positionSeconds >= start) active = static_cast<int>(i);
    }
    if (active == m_currentLyricIndex) return;
    if (m_currentLyricIndex >= 0 && m_currentLyricIndex < m_lyricsList->count()) {
        QListWidgetItem* previous = m_lyricsList->item(m_currentLyricIndex);
        QFont font = previous->font();
        font.setBold(false);
        font.setPointSize(m_lyricsList->font().pointSize());
        previous->setFont(font);
        previous->setForeground(QBrush(QColor(136, 136, 136)));
        previous->setBackground(QBrush());
    }
    m_currentLyricIndex = active;
    if (active >= 0 && active < m_lyricsList->count()) {
        QListWidgetItem* item = m_lyricsList->item(active);
        QFont font = item->font();
        font.setBold(true);
        font.setPointSize(qMax(m_lyricsList->font().pointSize() + 2, 15));
        item->setFont(font);
        item->setForeground(QBrush(QColor(240, 240, 240)));
        item->setBackground(QBrush(QColor(45, 55, 48)));
        if (!lyricsAutoScrollHeld()) {
            scrollLyricsToLine(active);
        }
    }
}

void MainWindow::scrollLyricsToLine(int row, bool animated) {
    if (!m_lyricsList || row < 0 || row >= m_lyricsList->count()) return;
    QScrollBar* bar = m_lyricsList->verticalScrollBar();
    if (!bar) return;
    const QRect rect = m_lyricsList->visualItemRect(m_lyricsList->item(row));
    if (!rect.isValid()) return;
    int target = bar->value() + rect.top() + (rect.height() / 2) - (m_lyricsList->viewport()->height() / 2);
    target = qBound(bar->minimum(), target, bar->maximum());
    stopLyricsScrollAnimation();
    if (!animated || m_reduceAnimations || std::abs(bar->value() - target) <= 2) {
        bar->setValue(target);
        return;
    }
    auto* animation = new QPropertyAnimation(bar, "value", this);
    animation->setDuration(140);
    animation->setEasingCurve(QEasingCurve::OutQuad);
    animation->setStartValue(bar->value());
    animation->setEndValue(target);
    connect(animation, &QPropertyAnimation::finished, this, [this, animation]() {
        if (m_lyricsScrollAnimation == animation) m_lyricsScrollAnimation = nullptr;
        animation->deleteLater();
    });
    m_lyricsScrollAnimation = animation;
    animation->start();
}

void MainWindow::stopLyricsScrollAnimation() {
    if (!m_lyricsScrollAnimation) return;
    QPropertyAnimation* animation = m_lyricsScrollAnimation;
    m_lyricsScrollAnimation = nullptr;
    animation->stop();
    animation->deleteLater();
}

void MainWindow::seekToLyricItem(QListWidgetItem* item) {
    if (!item) return;
    bool ok = false;
    const double start = item->data(Qt::UserRole).toDouble(&ok);
    if (!ok || start < 0.0) return;
    holdLyricsAutoScroll();
    m_lyricsList->setCurrentItem(nullptr);
    beginSeekPreview(start);
    m_player.seekTo(start);
}

void MainWindow::beginSeekPreview(double seconds) {
    m_seekPreviewTarget = qMax(0.0, seconds);
    m_seekPreviewUntilMs = QDateTime::currentMSecsSinceEpoch() + 3500;
    m_seek->setValue(static_cast<int>(m_seekPreviewTarget * 1000));
    m_time->setText(QStringLiteral("%1 / %2").arg(formatTime(m_seekPreviewTarget), formatTime(m_duration)));
    updateLyrics(m_seekPreviewTarget);
}

bool MainWindow::seekPreviewActive(double incomingPosition) const {
    if (m_seekPreviewTarget < 0.0) return false;
    if (QDateTime::currentMSecsSinceEpoch() > m_seekPreviewUntilMs) return false;
    return std::abs(incomingPosition - m_seekPreviewTarget) > 1.25;
}

void MainWindow::holdLyricsAutoScroll() {
    m_lyricsAutoScrollHoldUntilMs = QDateTime::currentMSecsSinceEpoch() + kLyricsAutoScrollHoldMs;
    stopLyricsScrollAnimation();
}

bool MainWindow::lyricsAutoScrollHeld() const {
    return QDateTime::currentMSecsSinceEpoch() < m_lyricsAutoScrollHoldUntilMs;
}

QString MainWindow::trackLine(const QJsonObject& track) const {
    const QString artist = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString(QStringLiteral("?")));
    const QString title = track.value(QStringLiteral("title")).toString(QStringLiteral("?"));
    const QString album = track.value(QStringLiteral("album")).toString();
    return album.isEmpty() ? QStringLiteral("%1 - %2").arg(artist, title) : QStringLiteral("%1 - %2 - %3").arg(artist, title, album);
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

bool MainWindow::trackIsLocal(const QString& id) const {
    return !id.isEmpty() && (m_cache.hasDownload(id) || m_cache.hasCachedAudio(id));
}

bool MainWindow::trackIsDownloaded(const QString& id) const {
    return !id.isEmpty() && m_cache.hasDownload(id);
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
        m_player.clearNextTrack();
        maybePrefetchNext();
        return;
    }
    if (!requireOnline(QStringLiteral("Download"))) return;
    setStatus(QStringLiteral("Downloading %1...").arg(track.value(QStringLiteral("title")).toString()));
    m_sidecar.request(QStringLiteral("download"), {{QStringLiteral("track_id"), id}}, [this](const QJsonObject& result) {
        const QJsonObject track = result.value(QStringLiteral("track")).toObject();
        if (!track.isEmpty()) m_tracks[track.value(QStringLiteral("id")).toVariant().toString()] = track;
        setStatus(QStringLiteral("Downloaded: %1").arg(result.value(QStringLiteral("path")).toString()));
        refreshCacheTab();
        maybePrefetchNext();
    }, [this](const QString& error) { QMessageBox::critical(this, QStringLiteral("Download"), error); });
}

void MainWindow::setStatus(const QString& message) {
    if (!m_status) return;
    m_status->setText(message.startsWith(QStringLiteral("Status:")) ? message : QStringLiteral("Status: %1").arg(message));
}

void MainWindow::showSettingsDialog() {
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("Settings"));
    dialog.resize(640, 420);
    auto* layout = new QVBoxLayout(&dialog);
    auto* tabs = new QTabWidget(&dialog);
    tabs->setDocumentMode(true);
    layout->addWidget(tabs, 1);

    auto* playbackTab = new QWidget(tabs);
    auto* playbackTabLayout = new QVBoxLayout(playbackTab);
    auto* outputGroup = new QGroupBox(QStringLiteral("Output"), playbackTab);
    auto* outputLayout = new QGridLayout(outputGroup);
    auto* deviceCombo = new QComboBox(outputGroup);
    deviceCombo->setEditable(true);
    const QString currentDevice = m_deviceCombo ? m_deviceCombo->currentText() : QStringLiteral("default");
    auto repopulateDevices = [deviceCombo](const QString& preferred) {
        QStringList devices = playbackDevices();
        const QString target = preferred.trimmed().isEmpty() ? QStringLiteral("default") : preferred.trimmed();
        if (!target.isEmpty() && !devices.contains(target)) devices.prepend(target);
        deviceCombo->clear();
        deviceCombo->addItems(devices);
        deviceCombo->setCurrentText(target);
    };
    repopulateDevices(currentDevice);
    auto* refreshDevicesButton = new QPushButton(QStringLiteral("Refresh"), outputGroup);
    auto* volumeSlider = new QSlider(Qt::Horizontal, outputGroup);
    volumeSlider->setRange(0, 100);
    volumeSlider->setValue(m_volume ? m_volume->value() : 100);
    auto* volumeValue = new QLabel(QStringLiteral("%1%").arg(volumeSlider->value()), outputGroup);
    volumeValue->setMinimumWidth(36);
    outputLayout->addWidget(new QLabel(QStringLiteral("ALSA device"), outputGroup), 0, 0);
    outputLayout->addWidget(deviceCombo, 0, 1);
    outputLayout->addWidget(refreshDevicesButton, 0, 2);
    outputLayout->addWidget(new QLabel(QStringLiteral("Volume"), outputGroup), 1, 0);
    outputLayout->addWidget(volumeSlider, 1, 1);
    outputLayout->addWidget(volumeValue, 1, 2);
    playbackTabLayout->addWidget(outputGroup);

    auto* behaviorGroup = new QGroupBox(QStringLiteral("Behavior"), playbackTab);
    auto* behaviorLayout = new QVBoxLayout(behaviorGroup);
    auto* gapless = new QCheckBox(QStringLiteral("Gapless playback"), behaviorGroup);
    gapless->setChecked(m_gaplessEnabled);
    gapless->setToolTip(QStringLiteral("Preloads the next cached/downloaded queued track for same-format handoff."));
    auto* reduceAnimations = new QCheckBox(QStringLiteral("Reduce animations"), behaviorGroup);
    reduceAnimations->setChecked(m_reduceAnimations);
    reduceAnimations->setToolTip(QStringLiteral("Disables animated lyric recentering."));
    behaviorLayout->addWidget(gapless);
    behaviorLayout->addWidget(reduceAnimations);
    playbackTabLayout->addWidget(behaviorGroup);
    playbackTabLayout->addStretch(1);
    tabs->addTab(playbackTab, QStringLiteral("Playback"));

    auto* storageTab = new QWidget(tabs);
    auto* storageLayout = new QVBoxLayout(storageTab);
    auto* localFilesGroup = new QGroupBox(QStringLiteral("Local Files"), storageTab);
    auto* localFilesLayout = new QGridLayout(localFilesGroup);
    auto* cachePath = new QLabel(m_cache.baseDir(), localFilesGroup);
    cachePath->setWordWrap(true);
    cachePath->setTextInteractionFlags(Qt::TextSelectableByMouse);
    auto* cacheSummary = new QLabel(localFilesGroup);
    auto* downloadsSummary = new QLabel(localFilesGroup);
    auto* openCache = new QPushButton(QStringLiteral("Open cache"), localFilesGroup);
    auto* openDownloads = new QPushButton(QStringLiteral("Open downloads"), localFilesGroup);
    auto* clearCache = new QPushButton(QStringLiteral("Clear cache"), localFilesGroup);
    auto* clearDownloads = new QPushButton(QStringLiteral("Clear downloads"), localFilesGroup);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Location"), localFilesGroup), 0, 0);
    localFilesLayout->addWidget(cachePath, 0, 1, 1, 3);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Cache"), localFilesGroup), 1, 0);
    localFilesLayout->addWidget(cacheSummary, 1, 1, 1, 3);
    localFilesLayout->addWidget(new QLabel(QStringLiteral("Downloads"), localFilesGroup), 2, 0);
    localFilesLayout->addWidget(downloadsSummary, 2, 1, 1, 3);
    localFilesLayout->addWidget(openCache, 3, 0, 1, 2);
    localFilesLayout->addWidget(openDownloads, 3, 2, 1, 2);
    localFilesLayout->addWidget(clearCache, 4, 0, 1, 2);
    localFilesLayout->addWidget(clearDownloads, 4, 2, 1, 2);
    storageLayout->addWidget(localFilesGroup);
    storageLayout->addStretch(1);
    tabs->addTab(storageTab, QStringLiteral("Storage"));

    auto updateCacheSummaries = [this, cacheSummary, downloadsSummary]() {
        const CacheManagerQt::Stats audio = m_cache.audioStats();
        const CacheManagerQt::Stats covers = m_cache.coverStats();
        const CacheManagerQt::Stats downloads = m_cache.downloadStats();
        cacheSummary->setText(QStringLiteral("Tracks: %1 | Covers: %2 | %3")
            .arg(audio.count)
            .arg(covers.count)
            .arg(formatBytes(audio.bytes + covers.bytes)));
        downloadsSummary->setText(QStringLiteral("Tracks: %1 | %2")
            .arg(downloads.count)
            .arg(formatBytes(downloads.bytes)));
    };
    updateCacheSummaries();

    auto* integrationsTab = new QWidget(tabs);
    auto* integrationsTabLayout = new QVBoxLayout(integrationsTab);
    auto* servicesGroup = new QGroupBox(QStringLiteral("Services"), integrationsTab);
    auto* integrationsLayout = new QFormLayout(servicesGroup);
    auto* discord = new QCheckBox(QStringLiteral("Discord Rich Presence"), servicesGroup);
    discord->setChecked(m_discordEnabled);
    auto* discordClientId = new QLineEdit(servicesGroup);
    discordClientId->setPlaceholderText(QStringLiteral("Built-in Discord app ID"));
    discordClientId->setText(m_discordClientId);
    const bool mprisAvailable = m_mprisAvailable || MprisService::available();
    auto* mpris = new QCheckBox(QStringLiteral("MPRIS media controls"), servicesGroup);
    mpris->setChecked(m_mprisEnabled && mprisAvailable);
    mpris->setEnabled(mprisAvailable);
    mpris->setToolTip(QStringLiteral("Exposes playback to media keys, playerctl, KDE Connect, and desktop shells."));
    integrationsLayout->addRow(QStringLiteral("Discord"), discord);
    integrationsLayout->addRow(QStringLiteral("Client ID"), discordClientId);
    integrationsLayout->addRow(QStringLiteral("Desktop media controls"), mpris);
    integrationsTabLayout->addWidget(servicesGroup);
    integrationsTabLayout->addStretch(1);
    tabs->addTab(integrationsTab, QStringLiteral("Integrations"));

    auto* healthTab = new QWidget(tabs);
    auto* healthLayout = new QVBoxLayout(healthTab);
    auto* healthGroup = new QGroupBox(QStringLiteral("Runtime"), healthTab);
    auto* runtimeLayout = new QFormLayout(healthGroup);
    runtimeLayout->addRow(QStringLiteral("Network"), new QLabel(m_offlineMode ? QStringLiteral("offline") : QStringLiteral("online"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("Native player"), new QLabel(m_player.available() ? QStringLiteral("available") : QStringLiteral("missing"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("Discord"), new QLabel(m_discord && m_discord->connected() ? QStringLiteral("connected") : QStringLiteral("idle"), healthGroup));
    runtimeLayout->addRow(QStringLiteral("MPRIS"), new QLabel(mprisAvailable ? (m_mpris && m_mpris->running() ? QStringLiteral("running") : QStringLiteral("available")) : QStringLiteral("unavailable"), healthGroup));
    healthLayout->addWidget(healthGroup);
    healthLayout->addStretch(1);
    tabs->addTab(healthTab, QStringLiteral("Health"));

    connect(refreshDevicesButton, &QPushButton::clicked, this, [deviceCombo, repopulateDevices]() {
        repopulateDevices(deviceCombo->currentText());
    });
    connect(volumeSlider, &QSlider::valueChanged, volumeValue, [volumeValue](int value) {
        volumeValue->setText(QStringLiteral("%1%").arg(value));
    });
    connect(openCache, &QPushButton::clicked, this, [this]() {
        QDir().mkpath(m_cache.baseDir());
        QDesktopServices::openUrl(QUrl::fromLocalFile(m_cache.baseDir()));
    });
    connect(openDownloads, &QPushButton::clicked, this, [this]() {
        QDir().mkpath(m_cache.downloadsDir());
        QDesktopServices::openUrl(QUrl::fromLocalFile(m_cache.downloadsDir()));
    });
    connect(clearCache, &QPushButton::clicked, this, [this, updateCacheSummaries]() {
        clearCachedTracks();
        updateCacheSummaries();
    });
    connect(clearDownloads, &QPushButton::clicked, this, [this, updateCacheSummaries]() {
        clearDownloadedTracks();
        updateCacheSummaries();
    });

    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) return;

    const QString selectedDevice = deviceCombo->currentText().trimmed();
    if (!selectedDevice.isEmpty() && m_deviceCombo) {
        if (m_deviceCombo->findText(selectedDevice) < 0) m_deviceCombo->insertItem(0, selectedDevice);
        m_deviceCombo->setCurrentText(selectedDevice);
        m_settings.setValue(QStringLiteral("qt6/alsa_device"), selectedDevice);
    }
    if (m_volume) m_volume->setValue(volumeSlider->value());
    m_gaplessEnabled = gapless->isChecked();
    m_reduceAnimations = reduceAnimations->isChecked();
    m_settings.setValue(QStringLiteral("qt6/gapless_enabled"), m_gaplessEnabled);
    m_settings.setValue(QStringLiteral("qt6/reduce_animations"), m_reduceAnimations);
    setDiscordEnabled(discord->isChecked(), discordClientId->text());
    if (mpris->isEnabled()) setMprisEnabled(mpris->isChecked());
    if (m_reduceAnimations) stopLyricsScrollAnimation();
    if (m_gaplessEnabled) maybePrefetchNext();
    else m_player.clearNextTrack();
    updateAudioStatusLabels();
}
