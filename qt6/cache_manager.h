#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QVector>

class CacheManagerQt {
public:
    struct Entry {
        QString id;
        QString title;
        QString artist;
        QString album;
        QString albumId;
        QString artistId;
        QString coverUrl;
        QString coverThumbnailUrl;
        QString audioQuality;
        QString trackMaxQuality;
        int bitDepth = 0;
        int sampleRate = 0;
        QString path;
        qint64 size = 0;
        double mtime = 0.0;
    };

    struct Stats {
        int count = 0;
        qint64 bytes = 0;
    };

    CacheManagerQt();

    QString baseDir() const;
    QString audioDir() const;
    QString coversDir() const;
    QString downloadsDir() const;
    QString cachedAudioPath(const QString& trackId) const;
    QString downloadPath(const QString& trackId) const;
    bool hasCachedAudio(const QString& trackId) const;
    bool hasDownload(const QString& trackId) const;
    QVector<Entry> cachedTracks() const;
    QVector<Entry> downloads() const;
    Stats audioStats() const;
    Stats coverStats() const;
    Stats downloadStats() const;
    void clearAudio();
    void clearCovers();
    void clearDownloads();
    bool deleteDownload(const QString& trackId);
    void refresh();

private:
    QVector<Entry> entriesForBucket(const QString& bucket) const;
    Entry entryFromJson(const QString& id, const QJsonObject& obj) const;
    Stats statsForDir(const QString& dir, const QStringList& patterns) const;
    void deleteFiles(const QString& dir, const QStringList& patterns);
    void saveIndex() const;

    QString m_baseDir;
    QJsonObject m_index;
};
