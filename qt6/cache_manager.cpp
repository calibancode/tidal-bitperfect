#include "cache_manager.h"

#include <QDateTime>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QCryptographicHash>
#include <QSaveFile>
#include <QSet>
#include <algorithm>
#include <cmath>

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

static QString cacheHashKey(const QString& key) {
    return QString::fromLatin1(QCryptographicHash::hash(key.toUtf8(), QCryptographicHash::Sha1).toHex());
}

static qint64 currentUnixTime() {
    return QDateTime::currentSecsSinceEpoch();
}

static int reasonWeight(const QString& reason) {
    const QString key = reason.toLower();
    if (key == QStringLiteral("gapless") || key == QStringLiteral("normalized_gapless")) return 120;
    if (key == QStringLiteral("played")) return 100;
    if (key == QStringLiteral("queue")) return 55;
    if (key == QStringLiteral("prefetch")) return 30;
    return 20;
}

static double audioEvictionScore(const CacheManagerQt::Entry& entry, qint64 nowSeconds, const QString& mode) {
    const double ageHours = entry.lastUsed > 0.0
        ? qMax(0.0, (static_cast<double>(nowSeconds) - entry.lastUsed) / 3600.0)
        : qMax(0.0, (static_cast<double>(nowSeconds) - entry.mtime) / 3600.0);
    const double modeMultiplier = mode == QStringLiteral("aggressive") ? 1.35 : (mode == QStringLiteral("conservative") ? 0.75 : 1.0);
    double score = static_cast<double>(reasonWeight(entry.cacheReason)) * modeMultiplier;
    score += static_cast<double>(qMin(entry.playCount, 20)) * 35.0;
    score += static_cast<double>(entry.priority) * 12.0;
    if (entry.lastPlayed > 0.0) score += 90.0;
    if (entry.cacheReason == QStringLiteral("prefetch") && entry.playCount <= 0) score -= 80.0;
    score -= ageHours * (mode == QStringLiteral("aggressive") ? 0.35 : 0.75);
    return score;
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

QByteArray CacheManagerQt::coverBytes(const QString& coverUrl) const {
    const QString path = coverPath(coverUrl);
    if (path.isEmpty() || !QFileInfo::exists(path)) return {};
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    const QByteArray data = file.readAll();
    file.close();
    file.setFileTime(QDateTime::currentDateTime(), QFileDevice::FileModificationTime);
    return data;
}

bool CacheManagerQt::storeCoverBytes(const QString& coverUrl, const QByteArray& data) {
    const QString path = coverPath(coverUrl);
    if (path.isEmpty() || data.isEmpty()) return false;
    QDir().mkpath(coversDir());
    if (QFileInfo::exists(path)) return true;
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) return false;
    if (file.write(data) != data.size()) return false;
    return file.commit();
}

bool CacheManagerQt::hasCachedAudio(const QString& trackId) const {
    return !cachedAudioPath(trackId).isEmpty();
}

bool CacheManagerQt::hasDownload(const QString& trackId) const {
    return !downloadPath(trackId).isEmpty();
}

CacheManagerQt::Entry CacheManagerQt::cachedAudioEntry(const QString& trackId) const {
    Entry entry;
    if (trackId.isEmpty()) return entry;
    const QJsonObject audio = m_index.value(QStringLiteral("audio")).toObject();
    const QJsonObject obj = audio.value(trackId).toObject();
    if (!obj.isEmpty()) entry = entryFromJson(trackId, obj);
    if (entry.path.isEmpty() || !QFileInfo::exists(entry.path)) {
        const QString path = cachedAudioPath(trackId);
        if (!path.isEmpty()) {
            const QFileInfo info(path);
            entry.id = trackId;
            entry.path = info.absoluteFilePath();
            entry.title = QStringLiteral("Track %1").arg(trackId);
            entry.artist = QStringLiteral("Unknown artist");
            entry.size = info.size();
            entry.mtime = info.lastModified().toSecsSinceEpoch();
        }
    }
    return entry;
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

bool CacheManagerQt::markAudioUsed(const QString& trackId, const QString& reason) {
    if (trackId.isEmpty()) return false;
    QJsonObject audio = m_index.value(QStringLiteral("audio")).toObject();
    QJsonObject entry = audio.value(trackId).toObject();
    QString path = entry.value(QStringLiteral("path")).toString();
    if (path.isEmpty()) path = cachedAudioPath(trackId);
    if (path.isEmpty() || !QFileInfo::exists(path)) return false;

    const qint64 now = currentUnixTime();
    entry.insert(QStringLiteral("path"), path);
    entry.insert(QStringLiteral("last_used"), static_cast<double>(now));
    if (entry.value(QStringLiteral("created_at")).toDouble() <= 0.0) {
        entry.insert(QStringLiteral("created_at"), static_cast<double>(now));
    }
    if (!reason.isEmpty()) entry.insert(QStringLiteral("cache_reason"), reason);
    if (reason == QStringLiteral("played")) {
        entry.insert(QStringLiteral("last_played"), static_cast<double>(now));
        entry.insert(QStringLiteral("play_count"), entry.value(QStringLiteral("play_count")).toInt(0) + 1);
    }
    QFile cachedFile(path);
    if (cachedFile.open(QIODevice::ReadWrite)) {
        cachedFile.setFileTime(QDateTime::fromSecsSinceEpoch(now), QFileDevice::FileModificationTime);
    }
    audio.insert(trackId, entry);
    m_index.insert(QStringLiteral("audio"), audio);
    saveIndex();
    refresh();
    return true;
}

bool CacheManagerQt::enforceAudioLimit(qint64 maxBytes, const QString& mode) {
    if (maxBytes <= 0) return false;
    QVector<Entry> entries = entriesForBucket(QStringLiteral("audio"));
    qint64 total = 0;
    for (const Entry& entry : entries) total += qMax<qint64>(0, entry.size);
    if (total <= maxBytes) return false;

    const QString policyMode = mode.isEmpty() ? QStringLiteral("balanced") : mode;
    const qint64 now = currentUnixTime();
    std::sort(entries.begin(), entries.end(), [now, policyMode](const Entry& a, const Entry& b) {
        const double aScore = audioEvictionScore(a, now, policyMode);
        const double bScore = audioEvictionScore(b, now, policyMode);
        if (std::abs(aScore - bScore) > 0.0001) return aScore < bScore;
        return a.mtime < b.mtime;
    });
    QJsonObject audio = m_index.value(QStringLiteral("audio")).toObject();
    bool changed = false;
    for (const Entry& entry : entries) {
        if (total <= maxBytes) break;
        if (!entry.path.isEmpty() && QFileInfo::exists(entry.path) && QFile::remove(entry.path)) {
            total -= qMax<qint64>(0, entry.size);
            changed = true;
        }
        audio.remove(entry.id);
    }
    if (changed) {
        m_index.insert(QStringLiteral("audio"), audio);
        saveIndex();
        refresh();
    }
    return changed;
}

bool CacheManagerQt::enforceCoverLimit(qint64 maxBytes) {
    if (maxBytes <= 0) return false;
    QVector<QFileInfo> files;
    qint64 total = 0;
    QDirIterator it(coversDir(), QStringList{QStringLiteral("*.img")}, QDir::Files, QDirIterator::Subdirectories);
    while (it.hasNext()) {
        it.next();
        const QFileInfo info = it.fileInfo();
        files.push_back(info);
        total += info.size();
    }
    if (total <= maxBytes) return false;

    std::sort(files.begin(), files.end(), [](const QFileInfo& a, const QFileInfo& b) { return a.lastModified() < b.lastModified(); });
    bool changed = false;
    for (const QFileInfo& info : files) {
        if (total <= maxBytes) break;
        if (QFile::remove(info.absoluteFilePath())) {
            total -= info.size();
            changed = true;
        }
    }
    return changed;
}

bool CacheManagerQt::enforceLimits(qint64 audioMaxBytes, qint64 coverMaxBytes, const QString& mode) {
    const bool audioChanged = enforceAudioLimit(audioMaxBytes, mode);
    const bool coverChanged = enforceCoverLimit(coverMaxBytes);
    return audioChanged || coverChanged;
}

bool CacheManagerQt::deleteCachedAudio(const QString& trackId) {
    if (trackId.isEmpty()) return false;
    QJsonObject audio = m_index.value(QStringLiteral("audio")).toObject();
    QString path = audio.value(trackId).toObject().value(QStringLiteral("path")).toString();
    audio.remove(trackId);
    if (path.isEmpty()) path = QDir(audioDir()).filePath(QStringLiteral("%1.flac").arg(trackId));
    bool removed = false;
    if (!path.isEmpty() && QFileInfo::exists(path)) {
        removed = QFile::remove(path);
    }
    m_index.insert(QStringLiteral("audio"), audio);
    saveIndex();
    refresh();
    return removed;
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
    entry.cacheReason = obj.value(QStringLiteral("cache_reason")).toString();
    entry.cacheMode = obj.value(QStringLiteral("cache_mode")).toString();
    entry.bitDepth = obj.value(QStringLiteral("bit_depth")).toInt();
    entry.sampleRate = obj.value(QStringLiteral("sample_rate")).toInt();
    entry.playCount = obj.value(QStringLiteral("play_count")).toInt();
    entry.priority = obj.value(QStringLiteral("cache_priority")).toInt();
    entry.createdAt = obj.value(QStringLiteral("created_at")).toDouble();
    entry.lastUsed = obj.value(QStringLiteral("last_used")).toDouble();
    entry.lastPlayed = obj.value(QStringLiteral("last_played")).toDouble();
    entry.path = obj.value(QStringLiteral("path")).toString();
    entry.size = static_cast<qint64>(obj.value(QStringLiteral("size")).toDouble());
    entry.mtime = obj.value(QStringLiteral("mtime")).toDouble();
    return entry;
}

QString CacheManagerQt::coverPath(const QString& coverUrl) const {
    if (coverUrl.isEmpty()) return {};
    return QDir(coversDir()).filePath(QStringLiteral("%1.img").arg(cacheHashKey(coverUrl)));
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
