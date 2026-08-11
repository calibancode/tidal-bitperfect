#include "main_window_support.h"

#include "runtime_paths.h"

#include <QAbstractSpinBox>
#include <QApplication>
#include <QDir>
#include <QFile>
#include <QIcon>
#include <QJsonArray>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QResizeEvent>
#include <QSet>
#include <QTcpSocket>
#include <QTextEdit>
#include <QTreeWidget>
#include <QTreeWidgetItemIterator>

#include <cmath>

namespace MainWindowSupport {

CoverLabel::CoverLabel(QWidget* parent) : QLabel(parent) {
    setAlignment(Qt::AlignCenter);
    setMinimumSize(260, 260);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
}

void CoverLabel::setCoverPixmap(const QPixmap& pixmap) {
    m_original = pixmap;
    updateScaledPixmap();
}

void CoverLabel::setFallbackPixmap(const QPixmap& pixmap) {
    m_fallback = pixmap;
    updateScaledPixmap();
}

void CoverLabel::resizeEvent(QResizeEvent* event) {
    QLabel::resizeEvent(event);
    updateScaledPixmap();
}

void CoverLabel::updateScaledPixmap() {
    const QPixmap& source = m_original.isNull() ? m_fallback : m_original;
    if (source.isNull()) {
        clear();
        setText(QStringLiteral("No cover"));
        return;
    }
    setText(QString());
    QLabel::setPixmap(source.scaled(size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
}

QPixmap fallbackCoverPixmap() {
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

bool isContainerType(const QString& type) {
    return type == QStringLiteral("album") || type == QStringLiteral("playlist") || type == QStringLiteral("artist") || type == QStringLiteral("mix");
}

bool isTrackObject(const QJsonObject& obj) {
    const QString type = obj.value(QStringLiteral("_type")).toString();
    return type == QStringLiteral("track") || (type.isEmpty() && obj.contains(QStringLiteral("id")) && obj.contains(QStringLiteral("title")));
}

bool shouldRememberTrackObject(const QJsonObject& obj, const QString& typeHint) {
    const QString type = typeHint.isEmpty() ? obj.value(QStringLiteral("_type")).toString() : typeHint;
    if (type == QStringLiteral("track")) return true;
    if (!type.isEmpty()) return false;
    return obj.contains(QStringLiteral("duration"))
        || obj.contains(QStringLiteral("album"))
        || obj.contains(QStringLiteral("audio_quality"))
        || obj.contains(QStringLiteral("track_max_quality"));
}

QVector<QJsonObject> trackObjects(const QJsonObject& obj) {
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

QString trackLineText(const QJsonObject& track, bool includeAlbum) {
    const QString artist = track.value(QStringLiteral("artist_display")).toString(track.value(QStringLiteral("artist")).toString(QStringLiteral("?")));
    const QString title = track.value(QStringLiteral("title")).toString(QStringLiteral("?"));
    const QString album = track.value(QStringLiteral("album")).toString();
    const QString titleAndArtist = QStringLiteral("%1 - %2").arg(title, artist);
    return !includeAlbum || album.isEmpty() ? titleAndArtist : QStringLiteral("%1 - %2").arg(titleAndArtist, album);
}

QString mediaTypeKey(const QString& label) {
    const QString lower = label.toLower();
    if (lower.startsWith(QStringLiteral("album"))) return QStringLiteral("album");
    if (lower.startsWith(QStringLiteral("playlist"))) return QStringLiteral("playlist");
    if (lower.startsWith(QStringLiteral("artist"))) return QStringLiteral("artist");
    return QStringLiteral("track");
}

QString formatTime(double seconds) {
    const int total = qMax(0, static_cast<int>(std::llround(seconds)));
    const int minutes = total / 60;
    const int secs = total % 60;
    return QStringLiteral("%1:%2").arg(minutes).arg(secs, 2, 10, QLatin1Char('0'));
}

QString formatBytes(qint64 bytes) {
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

QString qualityLabelText(const QString& audioQuality, int bitDepth, int sampleRate) {
    QStringList parts;
    const QString quality = audioQuality.trimmed();
    if (!quality.isEmpty()) parts << quality;
    if (bitDepth > 0 && sampleRate > 0) parts << QStringLiteral("%1-bit/%2Hz").arg(bitDepth).arg(sampleRate);
    return parts.isEmpty() ? QStringLiteral("Quality: —") : QStringLiteral("Quality: %1").arg(parts.join(QLatin1Char(' ')));
}

bool textInputFocused() {
    QWidget* focus = QApplication::focusWidget();
    return qobject_cast<QLineEdit*>(focus) != nullptr
        || qobject_cast<QTextEdit*>(focus) != nullptr
        || qobject_cast<QPlainTextEdit*>(focus) != nullptr
        || qobject_cast<QAbstractSpinBox*>(focus) != nullptr;
}

bool networkOffline() {
    QTcpSocket socket;
    socket.connectToHost(QStringLiteral("1.1.1.1"), 443);
    const bool online = socket.waitForConnected(500);
    socket.abort();
    return !online;
}

QStringList playbackDevices() {
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

QTreeWidgetItem* findItemByIdentity(QTreeWidget* tree, const QString& type, const QString& id) {
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

} // namespace MainWindowSupport
