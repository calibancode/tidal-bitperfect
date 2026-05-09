#include "cache_manager.h"

#include <QDateTime>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QSet>
#include <algorithm>

static QString defaultCacheDir() {
    return QDir::home().filePath(QStringLiteral(".cache/tidal-bitperfect"));
}

static QString downloadIdFromFileName(const QFileInfo& info) {
    const QString base = info.completeBaseName();
    if (base.endsWith(QLatin1Char(']'))) {
        const int open = base.lastIndexOf(QLatin1Char('['));
        if (open >= 0 && open + 1 < base.size() - 1) {
            return base.mid(open + 1, base.size() - open - 2);
        }
    }
    return base;
}

static QString downloadTitleFromFileName(const QFileInfo& info, const QString& id) {
    QString base = info.completeBaseName();
    const QString bracketedId = QStringLiteral(" [%1]").arg(id);
    if (base.endsWith(bracketedId)) {
        base.chop(bracketedId.size());
    }
    return base.isEmpty() ? QStringLiteral("Track %1").arg(id) : base;
}

CacheManagerQt::CacheManagerQt() : m_baseDir(defaultCacheDir()) {
    refresh();
}

QString CacheManagerQt::baseDir() const {
    return m_baseDir;
}

QString CacheManagerQt::audioDir() const {
    return QDir(m_baseDir).filePath(QStringLiteral("audio"));
}

QString CacheManagerQt::coversDir() const {
    return QDir(m_baseDir).filePath(QStringLiteral("covers"));
}

QString CacheManagerQt::downloadsDir() const {
    return QDir(m_baseDir).filePath(QStringLiteral("downloads"));
}

void CacheManagerQt::refresh() {
    QFile file(QDir(m_baseDir).filePath(QStringLiteral("index.json")));
    if (!file.open(QIODevice::ReadOnly)) {
        m_index = QJsonObject{{QStringLiteral("audio"), QJsonObject{}}, {QStringLiteral("covers"), QJsonObject{}}, {QStringLiteral("downloads"), QJsonObject{}}};
        return;
    }
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
    m_index = doc.isObject() ? doc.object() : QJsonObject{};
}

QString CacheManagerQt::cachedAudioPath(const QString& trackId) const {
    const QJsonObject audio = m_index.value(QStringLiteral("audio")).toObject();
    const QString path = audio.value(trackId).toObject().value(QStringLiteral("path")).toString();
    if (!path.isEmpty() && QFileInfo::exists(path)) {
        return path;
    }
    const QString legacy = QDir(audioDir()).filePath(QStringLiteral("%1.flac").arg(trackId));
    return QFileInfo::exists(legacy) ? legacy : QString();
}

QString CacheManagerQt::downloadPath(const QString& trackId) const {
    const QJsonObject downloads = m_index.value(QStringLiteral("downloads")).toObject();
    const QString path = downloads.value(trackId).toObject().value(QStringLiteral("path")).toString();
    if (!path.isEmpty() && QFileInfo::exists(path)) {
        return path;
    }
    QDir dir(downloadsDir());
    const QString suffix = QStringLiteral("[%1].flac").arg(trackId);
    for (const QFileInfo& info : dir.entryInfoList(QStringList{QStringLiteral("*.flac")}, QDir::Files)) {
        if (info.fileName().endsWith(suffix)) {
            return info.absoluteFilePath();
        }
    }
    return QString();
}

bool CacheManagerQt::hasCachedAudio(const QString& trackId) const {
    return !cachedAudioPath(trackId).isEmpty();
}

bool CacheManagerQt::hasDownload(const QString& trackId) const {
    return !downloadPath(trackId).isEmpty();
}

QVector<CacheManagerQt::Entry> CacheManagerQt::cachedTracks() const {
    return entriesForBucket(QStringLiteral("audio"));
}

QVector<CacheManagerQt::Entry> CacheManagerQt::downloads() const {
    return entriesForBucket(QStringLiteral("downloads"));
}

CacheManagerQt::Stats CacheManagerQt::audioStats() const {
    return statsForDir(audioDir(), QStringList{QStringLiteral("*.flac")});
}

CacheManagerQt::Stats CacheManagerQt::coverStats() const {
    return statsForDir(coversDir(), QStringList{QStringLiteral("*.img")});
}

CacheManagerQt::Stats CacheManagerQt::downloadStats() const {
    return statsForDir(downloadsDir(), QStringList{QStringLiteral("*.flac")});
}

void CacheManagerQt::clearAudio() {
    deleteFiles(audioDir(), QStringList{QStringLiteral("*.flac")});
    m_index.insert(QStringLiteral("audio"), QJsonObject{});
    saveIndex();
    refresh();
}

void CacheManagerQt::clearCovers() {
    deleteFiles(coversDir(), QStringList{QStringLiteral("*.img")});
    m_index.insert(QStringLiteral("covers"), QJsonObject{});
    saveIndex();
    refresh();
}

void CacheManagerQt::clearDownloads() {
    deleteFiles(downloadsDir(), QStringList{QStringLiteral("*.flac")});
    m_index.insert(QStringLiteral("downloads"), QJsonObject{});
    saveIndex();
    refresh();
}

bool CacheManagerQt::deleteDownload(const QString& trackId) {
    if (trackId.isEmpty()) return false;
    QJsonObject downloads = m_index.value(QStringLiteral("downloads")).toObject();
    QString path = downloads.value(trackId).toObject().value(QStringLiteral("path")).toString();
    downloads.remove(trackId);
    if (path.isEmpty()) path = downloadPath(trackId);
    bool removed = false;
    if (!path.isEmpty() && QFileInfo::exists(path)) {
        removed = QFile::remove(path);
    }
    m_index.insert(QStringLiteral("downloads"), downloads);
    saveIndex();
    refresh();
    return removed;
}

QVector<CacheManagerQt::Entry> CacheManagerQt::entriesForBucket(const QString& bucket) const {
    QVector<Entry> out;
    QSet<QString> seen;
    const QJsonObject obj = m_index.value(bucket).toObject();
    for (auto it = obj.begin(); it != obj.end(); ++it) {
        Entry entry = entryFromJson(it.key(), it.value().toObject());
        if (!entry.path.isEmpty() && QFileInfo::exists(entry.path)) {
            out.push_back(entry);
            seen.insert(entry.id);
        }
    }
    const bool audio = bucket == QStringLiteral("audio");
    const bool downloads = bucket == QStringLiteral("downloads");
    if (audio || downloads) {
        QDir dir(audio ? audioDir() : downloadsDir());
        for (const QFileInfo& info : dir.entryInfoList(QStringList{QStringLiteral("*.flac")}, QDir::Files)) {
            const QString id = audio ? info.completeBaseName() : downloadIdFromFileName(info);
            if (id.isEmpty() || seen.contains(id)) continue;
            Entry entry;
            entry.id = id;
            entry.title = audio ? QStringLiteral("Track %1").arg(id) : downloadTitleFromFileName(info, id);
            entry.artist = QStringLiteral("Unknown artist");
            entry.path = info.absoluteFilePath();
            entry.size = info.size();
            entry.mtime = info.lastModified().toSecsSinceEpoch();
            out.push_back(entry);
            seen.insert(id);
        }
    }
    std::sort(out.begin(), out.end(), [](const Entry& a, const Entry& b) { return a.mtime > b.mtime; });
    return out;
}

CacheManagerQt::Entry CacheManagerQt::entryFromJson(const QString& id, const QJsonObject& obj) const {
    Entry entry;
    entry.id = id;
    entry.title = obj.value(QStringLiteral("title")).toString(QStringLiteral("Track %1").arg(id));
    entry.artist = obj.value(QStringLiteral("artist_display")).toString(obj.value(QStringLiteral("artist")).toString(QStringLiteral("Unknown artist")));
    entry.album = obj.value(QStringLiteral("album")).toString();
    entry.albumId = obj.value(QStringLiteral("album_id")).toVariant().toString();
    entry.artistId = obj.value(QStringLiteral("artist_id")).toVariant().toString();
    entry.coverUrl = obj.value(QStringLiteral("cover_url")).toString();
    entry.coverThumbnailUrl = obj.value(QStringLiteral("cover_thumbnail_url")).toString();
    entry.audioQuality = obj.value(QStringLiteral("audio_quality")).toString();
    entry.trackMaxQuality = obj.value(QStringLiteral("track_max_quality")).toString();
    entry.bitDepth = obj.value(QStringLiteral("bit_depth")).toInt();
    entry.sampleRate = obj.value(QStringLiteral("sample_rate")).toInt();
    entry.path = obj.value(QStringLiteral("path")).toString();
    entry.size = static_cast<qint64>(obj.value(QStringLiteral("size")).toDouble());
    entry.mtime = obj.value(QStringLiteral("mtime")).toDouble();
    return entry;
}

CacheManagerQt::Stats CacheManagerQt::statsForDir(const QString& dir, const QStringList& patterns) const {
    Stats stats;
    QDirIterator it(dir, patterns, QDir::Files, QDirIterator::Subdirectories);
    while (it.hasNext()) {
        it.next();
        const QFileInfo info = it.fileInfo();
        ++stats.count;
        stats.bytes += info.size();
    }
    return stats;
}

void CacheManagerQt::deleteFiles(const QString& dir, const QStringList& patterns) {
    QDirIterator it(dir, patterns, QDir::Files, QDirIterator::Subdirectories);
    while (it.hasNext()) {
        QFile::remove(it.next());
    }
}

void CacheManagerQt::saveIndex() const {
    QDir().mkpath(m_baseDir);
    QFile file(QDir(m_baseDir).filePath(QStringLiteral("index.json")));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) return;
    file.write(QJsonDocument(m_index).toJson(QJsonDocument::Indented));
}
