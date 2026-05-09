#include "mpris_service.h"

#include <QDBusAbstractAdaptor>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusObjectPath>
#include <QJsonArray>
#include <QStringList>

namespace {
constexpr const char* kBusName = "org.mpris.MediaPlayer2.tidal_bitperfect_qt6";
constexpr const char* kObjectPath = "/org/mpris/MediaPlayer2";
constexpr const char* kRootInterface = "org.mpris.MediaPlayer2";
constexpr const char* kPlayerInterface = "org.mpris.MediaPlayer2.Player";

QString cleanObjectPathSegment(const QString& value) {
    QString out;
    for (const QChar ch : value) {
        out.append(ch.isLetterOrNumber() ? ch : QLatin1Char('_'));
    }
    return out.isEmpty() ? QStringLiteral("0") : out;
}

QStringList artistNames(const QJsonObject& track) {
    QStringList names;
    for (const QJsonValue& value : track.value(QStringLiteral("artists")).toArray()) {
        if (value.isString()) names.push_back(value.toString());
        else if (value.isObject()) {
            const QString name = value.toObject().value(QStringLiteral("name")).toString();
            if (!name.isEmpty()) names.push_back(name);
        }
    }
    if (names.isEmpty()) {
        const QString display = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString());
        if (!display.isEmpty()) names.push_back(display);
    }
    return names;
}
}

class MediaPlayer2Adaptor : public QDBusAbstractAdaptor {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.mpris.MediaPlayer2")
    Q_PROPERTY(bool CanQuit READ canQuit)
    Q_PROPERTY(bool Fullscreen READ fullscreen WRITE setFullscreen)
    Q_PROPERTY(bool CanSetFullscreen READ canSetFullscreen)
    Q_PROPERTY(bool CanRaise READ canRaise)
    Q_PROPERTY(bool HasTrackList READ hasTrackList)
    Q_PROPERTY(QString Identity READ identity)
    Q_PROPERTY(QString DesktopEntry READ desktopEntry)
    Q_PROPERTY(QStringList SupportedUriSchemes READ supportedUriSchemes)
    Q_PROPERTY(QStringList SupportedMimeTypes READ supportedMimeTypes)

public:
    explicit MediaPlayer2Adaptor(MprisService* service) : QDBusAbstractAdaptor(service), m_service(service) {}

public slots:
    void Raise() { m_service->requestRaise(); }
    void Quit() { m_service->requestQuit(); }

public:
    bool canQuit() const { return true; }
    bool fullscreen() const { return false; }
    void setFullscreen(bool fullscreen) { Q_UNUSED(fullscreen); }
    bool canSetFullscreen() const { return false; }
    bool canRaise() const { return true; }
    bool hasTrackList() const { return false; }
    QString identity() const { return QStringLiteral("TIDAL Bitperfect Qt6"); }
    QString desktopEntry() const { return QStringLiteral("tidal-bitperfect-qt6"); }
    QStringList supportedUriSchemes() const { return {QStringLiteral("tidal"), QStringLiteral("https")}; }
    QStringList supportedMimeTypes() const { return {}; }

private:
    MprisService* m_service = nullptr;
};

class MediaPlayer2PlayerAdaptor : public QDBusAbstractAdaptor {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.mpris.MediaPlayer2.Player")
    Q_PROPERTY(QString PlaybackStatus READ playbackStatus)
    Q_PROPERTY(QString LoopStatus READ loopStatus WRITE setLoopStatus)
    Q_PROPERTY(double Rate READ rate WRITE setRate)
    Q_PROPERTY(bool Shuffle READ shuffle WRITE setShuffle)
    Q_PROPERTY(QVariantMap Metadata READ metadata)
    Q_PROPERTY(double Volume READ volume WRITE setVolume)
    Q_PROPERTY(qlonglong Position READ position)
    Q_PROPERTY(double MinimumRate READ minimumRate)
    Q_PROPERTY(double MaximumRate READ maximumRate)
    Q_PROPERTY(bool CanGoNext READ canGoNext)
    Q_PROPERTY(bool CanGoPrevious READ canGoPrevious)
    Q_PROPERTY(bool CanPlay READ canPlay)
    Q_PROPERTY(bool CanPause READ canPause)
    Q_PROPERTY(bool CanSeek READ canSeek)
    Q_PROPERTY(bool CanControl READ canControl)

public:
    explicit MediaPlayer2PlayerAdaptor(MprisService* service) : QDBusAbstractAdaptor(service), m_service(service) {}

public slots:
    void Next() { m_service->requestNext(); }
    void Previous() {}
    void Pause() { m_service->requestPause(); }
    void PlayPause() { m_service->requestPlayPause(); }
    void Stop() { m_service->requestStop(); }
    void Play() { m_service->requestPlay(); }
    void Seek(qlonglong offset) { m_service->requestSeek(offset); }
    void SetPosition(const QDBusObjectPath& trackId, qlonglong position) {
        Q_UNUSED(trackId);
        m_service->requestSetPosition(position);
    }
    void OpenUri(const QString& uri) { m_service->requestOpenUri(uri); }

signals:
    void Seeked(qlonglong position);

public:
    QString playbackStatus() const { return m_service->playbackStatus(); }
    QString loopStatus() const { return QStringLiteral("None"); }
    void setLoopStatus(const QString& loopStatus) { Q_UNUSED(loopStatus); }
    double rate() const { return 1.0; }
    void setRate(double rate) { Q_UNUSED(rate); }
    bool shuffle() const { return false; }
    void setShuffle(bool shuffle) { Q_UNUSED(shuffle); }
    QVariantMap metadata() const { return m_service->metadata(); }
    double volume() const { return m_service->volume(); }
    void setVolume(double volume) { m_service->requestVolume(volume); }
    qlonglong position() const { return m_service->positionUs(); }
    double minimumRate() const { return 1.0; }
    double maximumRate() const { return 1.0; }
    bool canGoNext() const { return m_service->canGoNext(); }
    bool canGoPrevious() const { return false; }
    bool canPlay() const { return true; }
    bool canPause() const { return true; }
    bool canSeek() const { return m_service->canSeek(); }
    bool canControl() const { return true; }

private:
    MprisService* m_service = nullptr;
};

MprisService::MprisService(QObject* parent) : QObject(parent) {
    new MediaPlayer2Adaptor(this);
    new MediaPlayer2PlayerAdaptor(this);
}

MprisService::~MprisService() {
    stopService();
}

bool MprisService::available() {
    return QDBusConnection::sessionBus().isConnected();
}

bool MprisService::start() {
    if (m_running) return true;
    QDBusConnection bus = QDBusConnection::sessionBus();
    if (!bus.isConnected()) {
        emit errorMessage(QStringLiteral("MPRIS D-Bus session bus is unavailable"));
        return false;
    }
    if (!bus.registerService(QString::fromLatin1(kBusName))) {
        emit errorMessage(QStringLiteral("MPRIS D-Bus name is already in use"));
        return false;
    }
    if (!bus.registerObject(QString::fromLatin1(kObjectPath), this, QDBusConnection::ExportAdaptors)) {
        bus.unregisterService(QString::fromLatin1(kBusName));
        emit errorMessage(QStringLiteral("Failed to register MPRIS D-Bus object"));
        return false;
    }
    m_running = true;
    emit statusMessage(QStringLiteral("MPRIS D-Bus service started"));
    return true;
}

void MprisService::stopService() {
    if (!m_running) return;
    QDBusConnection bus = QDBusConnection::sessionBus();
    bus.unregisterObject(QString::fromLatin1(kObjectPath));
    bus.unregisterService(QString::fromLatin1(kBusName));
    m_running = false;
    emit statusMessage(QStringLiteral("MPRIS D-Bus service stopped"));
}

void MprisService::updateTrack(const QJsonObject& track, double durationSeconds) {
    if (track.isEmpty()) {
        clearTrack();
        return;
    }
    const QString id = track.value(QStringLiteral("id")).toVariant().toString();
    QVariantMap metadata;
    metadata.insert(QStringLiteral("mpris:trackid"), QVariant::fromValue(QDBusObjectPath(QStringLiteral("/org/tidal/track/%1").arg(cleanObjectPathSegment(id)))));
    metadata.insert(QStringLiteral("xesam:title"), track.value(QStringLiteral("title")).toString());
    metadata.insert(QStringLiteral("xesam:artist"), artistNames(track));
    const QString album = track.value(QStringLiteral("album")).toString();
    if (!album.isEmpty()) metadata.insert(QStringLiteral("xesam:album"), album);
    const QString coverUrl = track.value(QStringLiteral("cover_url")).toString();
    if (!coverUrl.isEmpty()) metadata.insert(QStringLiteral("mpris:artUrl"), coverUrl);
    const QString link = id.isEmpty() ? QString() : QStringLiteral("https://tidal.com/track/%1").arg(id);
    if (!link.isEmpty()) metadata.insert(QStringLiteral("xesam:url"), link);
    m_metadata = metadata;
    m_positionUs = 0;
    m_durationUs = 0;
    updateDuration(durationSeconds, false);
    m_playbackStatus = QStringLiteral("Playing");
    emitPlayerPropertiesChanged({
        {QStringLiteral("Metadata"), m_metadata},
        {QStringLiteral("PlaybackStatus"), m_playbackStatus},
        {QStringLiteral("CanSeek"), m_canSeek}
    });
}

void MprisService::clearTrack() {
    m_metadata.clear();
    m_playbackStatus = QStringLiteral("Stopped");
    m_positionUs = 0;
    m_durationUs = 0;
    m_canSeek = false;
    emitPlayerPropertiesChanged({
        {QStringLiteral("Metadata"), m_metadata},
        {QStringLiteral("PlaybackStatus"), m_playbackStatus},
        {QStringLiteral("CanSeek"), m_canSeek}
    });
}

void MprisService::setPlaybackStatus(const QString& status) {
    if (status == m_playbackStatus) return;
    m_playbackStatus = status;
    emitPlayerPropertiesChanged({{QStringLiteral("PlaybackStatus"), m_playbackStatus}});
}

void MprisService::updatePosition(double positionSeconds, double durationSeconds) {
    m_positionUs = qMax<qlonglong>(0, static_cast<qlonglong>(positionSeconds * 1000000.0));
    updateDuration(durationSeconds, true);
}

void MprisService::notifySeeked(double positionSeconds) {
    m_positionUs = qMax<qlonglong>(0, static_cast<qlonglong>(positionSeconds * 1000000.0));
    emitSeeked();
}

void MprisService::setVolume(double fraction) {
    const double next = qBound(0.0, fraction, 1.0);
    if (qFuzzyCompare(m_volume + 1.0, next + 1.0)) return;
    m_volume = next;
    emitPlayerPropertiesChanged({{QStringLiteral("Volume"), m_volume}});
}

void MprisService::setCanGoNext(bool canGoNext) {
    if (m_canGoNext == canGoNext) return;
    m_canGoNext = canGoNext;
    emitPlayerPropertiesChanged({{QStringLiteral("CanGoNext"), m_canGoNext}});
}

void MprisService::requestPlay() { emit playRequested(); }
void MprisService::requestPause() { emit pauseRequested(); }
void MprisService::requestPlayPause() { emit playPauseRequested(); }
void MprisService::requestStop() { emit stopRequested(); }
void MprisService::requestNext() { emit nextRequested(); }
void MprisService::requestSeek(qlonglong offsetUs) { emit seekRequested(static_cast<double>(offsetUs) / 1000000.0); }
void MprisService::requestSetPosition(qlonglong positionUs) { emit setPositionRequested(static_cast<double>(positionUs) / 1000000.0); }
void MprisService::requestOpenUri(const QString& uri) { emit openUriRequested(uri); }
void MprisService::requestRaise() { emit raiseRequested(); }
void MprisService::requestQuit() { emit quitRequested(); }

void MprisService::requestVolume(double fraction) {
    setVolume(fraction);
    emit volumeRequested(qRound(m_volume * 100.0));
}

void MprisService::emitPlayerPropertiesChanged(const QVariantMap& changed) const {
    if (!m_running || changed.isEmpty()) return;
    QDBusMessage message = QDBusMessage::createSignal(
        QString::fromLatin1(kObjectPath),
        QStringLiteral("org.freedesktop.DBus.Properties"),
        QStringLiteral("PropertiesChanged")
    );
    message << QString::fromLatin1(kPlayerInterface) << changed << QStringList{};
    QDBusConnection::sessionBus().send(message);
}

void MprisService::emitRootPropertiesChanged(const QVariantMap& changed) const {
    if (!m_running || changed.isEmpty()) return;
    QDBusMessage message = QDBusMessage::createSignal(
        QString::fromLatin1(kObjectPath),
        QStringLiteral("org.freedesktop.DBus.Properties"),
        QStringLiteral("PropertiesChanged")
    );
    message << QString::fromLatin1(kRootInterface) << changed << QStringList{};
    QDBusConnection::sessionBus().send(message);
}

void MprisService::emitSeeked() const {
    if (!m_running) return;
    QDBusMessage message = QDBusMessage::createSignal(
        QString::fromLatin1(kObjectPath),
        QString::fromLatin1(kPlayerInterface),
        QStringLiteral("Seeked")
    );
    message << m_positionUs;
    QDBusConnection::sessionBus().send(message);
}

void MprisService::updateDuration(double durationSeconds, bool emitChange) {
    const qlonglong durationUs = qMax<qlonglong>(0, static_cast<qlonglong>(durationSeconds * 1000000.0));
    if (durationUs == m_durationUs) return;
    m_durationUs = durationUs;
    const bool nextCanSeek = durationUs > 0;
    bool changed = false;
    if (!m_metadata.isEmpty()) {
        if (durationUs > 0) m_metadata.insert(QStringLiteral("mpris:length"), durationUs);
        else m_metadata.remove(QStringLiteral("mpris:length"));
        changed = true;
    }
    if (m_canSeek != nextCanSeek) {
        m_canSeek = nextCanSeek;
        changed = true;
    }
    if (emitChange && changed) {
        emitPlayerPropertiesChanged({
            {QStringLiteral("Metadata"), m_metadata},
            {QStringLiteral("CanSeek"), m_canSeek}
        });
    }
}

#include "mpris_service.moc"
