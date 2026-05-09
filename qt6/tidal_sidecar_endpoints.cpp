#include "tidal_sidecar.h"
#include "tidal_json_utils.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QProcess>
#include <QRegularExpression>
#include <QTemporaryFile>
#include <QUrl>
#include <algorithm>
#include <memory>

namespace {

QString normalizedQuality(QString quality) {
    quality = quality.trimmed().toUpper();
    quality.replace(QLatin1Char('-'), QLatin1Char('_'));
    quality.replace(QLatin1Char(' '), QLatin1Char('_'));
    if (quality == QStringLiteral("HIRES") || quality == QStringLiteral("HI_RES")) return QStringLiteral("HI_RES_LOSSLESS");
    if (quality == QStringLiteral("HIFI") || quality == QStringLiteral("HI_FI")) return QStringLiteral("LOSSLESS");
    return quality;
}

QString streamQuality(const QJsonObject& stream, const QString& fallback = {}) {
    const QString direct = normalizedQuality(TidalJson::nonEmptyString(stream, {"audioQuality", "audio_quality", "quality", "maxQuality", "max_quality"}));
    return direct.isEmpty() ? normalizedQuality(fallback) : direct;
}

QString playableUrlFromValue(const QJsonValue& value) {
    if (value.isString()) {
        const QString text = value.toString().trimmed();
        if (text.startsWith(QStringLiteral("http://"), Qt::CaseInsensitive)
            || text.startsWith(QStringLiteral("https://"), Qt::CaseInsensitive)) {
            return text;
        }
        return {};
    }
    if (value.isArray()) {
        for (const QJsonValue& child : value.toArray()) {
            const QString url = playableUrlFromValue(child);
            if (!url.isEmpty()) return url;
        }
        return {};
    }
    if (value.isObject()) {
        const QJsonObject obj = value.toObject();
        for (const QString& key : {
                 QStringLiteral("url"),
                 QStringLiteral("urls"),
                 QStringLiteral("audioUrl"),
                 QStringLiteral("streamUrl"),
                 QStringLiteral("manifestUrl"),
             }) {
            const QString url = playableUrlFromValue(obj.value(key));
            if (!url.isEmpty()) return url;
        }
        for (auto it = obj.begin(); it != obj.end(); ++it) {
            const QString url = playableUrlFromValue(it.value());
            if (!url.isEmpty()) return url;
        }
    }
    return {};
}

QByteArray decodedManifestBytes(const QString& manifest) {
    if (manifest.trimmed().isEmpty()) return {};
    return QByteArray::fromBase64(manifest.toUtf8());
}

QString playableUrlFromEncodedManifest(const QString& manifest) {
    const QByteArray bytes = decodedManifestBytes(manifest);
    if (bytes.isEmpty()) return {};
    QJsonParseError error;
    const QJsonDocument doc = QJsonDocument::fromJson(bytes, &error);
    if (error.error != QJsonParseError::NoError) return {};
    return doc.isObject() ? playableUrlFromValue(doc.object()) : playableUrlFromValue(doc.array());
}

QString playableUrlFromStream(const QJsonObject& stream) {
    const QString direct = playableUrlFromValue(stream);
    if (!direct.isEmpty()) return direct;
    return playableUrlFromEncodedManifest(stream.value(QStringLiteral("manifest")).toString());
}

bool hasDashManifest(const QJsonObject& stream) {
    const QString manifest = stream.value(QStringLiteral("manifest")).toString();
    if (manifest.isEmpty()) return false;
    const QString mime = stream.value(QStringLiteral("manifestMimeType")).toString();
    if (mime.contains(QStringLiteral("dash"), Qt::CaseInsensitive)) return true;
    const QString decoded = QString::fromUtf8(decodedManifestBytes(manifest)).trimmed();
    return decoded.startsWith(QStringLiteral("<MPD"), Qt::CaseInsensitive)
        || decoded.contains(QStringLiteral("<MPD"), Qt::CaseInsensitive);
}

bool streamHasPlayableInput(const QJsonObject& stream) {
    return hasDashManifest(stream) || !playableUrlFromStream(stream).isEmpty();
}

} // namespace

void TidalSidecar::loadTrack(const QString& trackId, ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("tracks/%1").arg(trackId), {}, {}, ApiBase::V1,
        [this, onSuccess](const QJsonValue& value) { onSuccess(parseTrack(value.toObject())); },
        onError
    );
}

void TidalSidecar::loadAlbum(const QString& albumId, bool includeTracks, ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("albums/%1").arg(albumId), {}, {}, ApiBase::V1,
        [this, albumId, includeTracks, onSuccess, onError](const QJsonValue& value) {
            QJsonObject album = parseAlbum(value.toObject());
            if (!includeTracks) {
                onSuccess(album);
                return;
            }
            apiRequest(QStringLiteral("GET"), QStringLiteral("albums/%1/tracks").arg(albumId), {{QStringLiteral("offset"), 0}}, {}, ApiBase::V1,
                [this, album, onSuccess](const QJsonValue& tracksValue) mutable {
                    album.insert(QStringLiteral("tracks"), parseTracksArray(tracksValue, album));
                    onSuccess(album);
                },
                [album, onSuccess](const QString&) mutable { onSuccess(album); }
            );
        },
        onError
    );
}

void TidalSidecar::loadPlaylist(const QString& playlistId, bool includeTracks, ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("playlists/%1").arg(playlistId), {}, {}, ApiBase::V1,
        [this, playlistId, includeTracks, onSuccess, onError](const QJsonValue& value) {
            QJsonObject playlist = parsePlaylist(value.toObject());
            if (!includeTracks) {
                onSuccess(playlist);
                return;
            }
            apiRequest(QStringLiteral("GET"), QStringLiteral("playlists/%1/tracks").arg(playlistId), {{QStringLiteral("offset"), 0}}, {}, ApiBase::V1,
                [this, playlist, onSuccess](const QJsonValue& tracksValue) mutable {
                    playlist.insert(QStringLiteral("tracks"), parseTracksArray(tracksValue));
                    onSuccess(playlist);
                },
                [playlist, onSuccess](const QString&) mutable { onSuccess(playlist); }
            );
        },
        onError
    );
}

void TidalSidecar::loadArtist(const QString& artistId, bool includeDetails, ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("artists/%1").arg(artistId), {}, {}, ApiBase::V1,
        [this, artistId, includeDetails, onSuccess](const QJsonValue& value) {
            QJsonObject artist = parseArtist(value.toObject());
            if (!includeDetails) {
                onSuccess(artist);
                return;
            }
            auto state = std::make_shared<QJsonObject>(artist);
            auto remaining = std::make_shared<int>(3);
            auto finish = [state, remaining, onSuccess]() {
                --(*remaining);
                if (*remaining == 0) onSuccess(*state);
            };
            apiRequest(QStringLiteral("GET"), QStringLiteral("artists/%1/toptracks").arg(artistId), {{QStringLiteral("limit"), 20}}, {}, ApiBase::V1,
                [this, state, finish](const QJsonValue& tracksValue) {
                    state->insert(QStringLiteral("tracks"), parseTracksArray(tracksValue));
                    finish();
                },
                [finish](const QString&) { finish(); }
            );
            apiRequest(QStringLiteral("GET"), QStringLiteral("artists/%1/albums").arg(artistId), {{QStringLiteral("limit"), 20}}, {}, ApiBase::V1,
                [this, state, finish](const QJsonValue& albumsValue) {
                    state->insert(QStringLiteral("albums"), parseAlbumsArray(albumsValue));
                    finish();
                },
                [finish](const QString&) { finish(); }
            );
            apiRequest(QStringLiteral("GET"), QStringLiteral("artists/%1/albums").arg(artistId), {{QStringLiteral("limit"), 20}, {QStringLiteral("filter"), QStringLiteral("EPSANDSINGLES")}}, {}, ApiBase::V1,
                [this, state, finish](const QJsonValue& albumsValue) {
                    state->insert(QStringLiteral("ep_singles"), parseAlbumsArray(albumsValue));
                    finish();
                },
                [finish](const QString&) { finish(); }
            );
        },
        onError
    );
}

void TidalSidecar::loadMix(const QString& mixId, ObjectHandler onSuccess, ErrorHandler onError) {
    apiRequest(QStringLiteral("GET"), QStringLiteral("pages/mix"), {{QStringLiteral("mixId"), mixId}, {QStringLiteral("deviceType"), QStringLiteral("BROWSER")}}, {}, ApiBase::V1,
        [this, mixId, onSuccess](const QJsonValue& value) {
            QJsonObject mix{{QStringLiteral("id"), mixId}, {QStringLiteral("title"), mixId}, {QStringLiteral("tracks"), QJsonArray{}}};
            const QJsonArray items = homeItemsFromValue(value, 100);
            QJsonArray tracks;
            for (const QJsonValue& wrapped : items) {
                const QJsonObject obj = wrapped.toObject();
                if (obj.value(QStringLiteral("type")).toString() == QStringLiteral("track")) tracks.append(obj.value(QStringLiteral("data")).toObject());
            }
            mix.insert(QStringLiteral("tracks"), tracks);
            onSuccess(mix);
        },
        onError
    );
}

void TidalSidecar::fetchTracksByIds(const QStringList& ids, ArrayHandler onSuccess, ErrorHandler onError) {
    QStringList unique;
    QSet<QString> seen;
    for (const QString& id : ids) {
        if (id.isEmpty() || seen.contains(id)) continue;
        seen.insert(id);
        unique.append(id);
    }
    if (unique.isEmpty()) {
        onSuccess({});
        return;
    }
    auto results = std::make_shared<QVector<QJsonObject>>(unique.size());
    auto remaining = std::make_shared<int>(unique.size());
    for (int i = 0; i < unique.size(); ++i) {
        loadTrack(unique.at(i),
            [results, remaining, i, onSuccess](const QJsonObject& track) {
                (*results)[i] = track;
                --(*remaining);
                if (*remaining == 0) {
                    QJsonArray arr;
                    for (const QJsonObject& item : *results) if (!item.isEmpty()) arr.append(item);
                    onSuccess(arr);
                }
            },
            [remaining, onSuccess, onError](const QString& error) {
                --(*remaining);
                if (*remaining == 0) onError(error);
            }
        );
    }
}

void TidalSidecar::fetchStreamCandidates(
    const QString& trackId,
    const QStringList& qualities,
    QVector<StreamCandidate> candidates,
    ObjectHandler onSuccess,
    ErrorHandler onError
) {
    if (qualities.isEmpty()) {
        if (candidates.isEmpty()) {
            onError(QStringLiteral("could not load stream candidates"));
            return;
        }
        const StreamCandidate chosen = *std::max_element(candidates.begin(), candidates.end(), [](const StreamCandidate& a, const StreamCandidate& b) {
            return std::tie(a.rank, a.bitDepth, a.sampleRate) < std::tie(b.rank, b.bitDepth, b.sampleRate);
        });
        const QJsonObject descriptor = streamDescriptorFromCandidate(chosen, onError);
        if (!descriptor.isEmpty()) onSuccess(descriptor);
        return;
    }
    const QString quality = qualities.first();
    QStringList rest = qualities;
    rest.removeFirst();
    loadStreamForQuality(trackId, quality,
        [this, trackId, rest, candidates, onSuccess, onError](const StreamCandidate& candidate) mutable {
            candidates.push_back(candidate);
            fetchStreamCandidates(trackId, rest, candidates, onSuccess, onError);
        },
        [this, trackId, rest, candidates, onSuccess, onError](const QString&) mutable {
            fetchStreamCandidates(trackId, rest, candidates, onSuccess, onError);
        }
    );
}

void TidalSidecar::loadStreamForQuality(
    const QString& trackId,
    const QString& quality,
    std::function<void(const StreamCandidate&)> onSuccess,
    ErrorHandler onError
) {
    loadTrack(trackId,
        [this, trackId, quality, onSuccess, onError](const QJsonObject& track) {
            const QJsonObject params{{QStringLiteral("playbackmode"), QStringLiteral("STREAM")}, {QStringLiteral("audioquality"), quality}, {QStringLiteral("assetpresentation"), QStringLiteral("FULL")}};
            apiRequest(QStringLiteral("GET"), QStringLiteral("tracks/%1/playbackinfopostpaywall").arg(trackId), params, {}, ApiBase::V1,
                [this, track, quality, onSuccess, onError, trackId](const QJsonValue& streamValue) {
                    QJsonObject stream = streamValue.toObject();
                    StreamCandidate candidate;
                    candidate.track = track;
                    candidate.stream = stream;
                    candidate.quality = quality;
                    candidate.rank = qualityRank(streamQuality(stream, quality));
                    candidate.bitDepth = stream.value(QStringLiteral("bitDepth")).toInt(16);
                    candidate.sampleRate = stream.value(QStringLiteral("sampleRate")).toInt(44100);
                    if (streamHasPlayableInput(stream)) {
                        onSuccess(candidate);
                        return;
                    }
                    const QJsonObject urlParams{{QStringLiteral("urlusagemode"), QStringLiteral("STREAM")}, {QStringLiteral("audioquality"), quality}, {QStringLiteral("assetpresentation"), QStringLiteral("FULL")}};
                    apiRequest(QStringLiteral("GET"), QStringLiteral("tracks/%1/urlpostpaywall").arg(trackId), urlParams, {}, ApiBase::V1,
                        [candidate, onSuccess](const QJsonValue& urlValue) mutable {
                            candidate.url = playableUrlFromValue(urlValue);
                            onSuccess(candidate);
                        },
                        onError
                    );
                },
                onError
            );
        },
        onError
    );
}

void TidalSidecar::downloadDirectFlac(
    const QString& url,
    const QString& trackId,
    const QJsonObject& meta,
    ObjectHandler onSuccess,
    ErrorHandler onError
) {
    httpGetBytes(QUrl(url), [this, trackId, meta, onSuccess, onError](const QByteArray& bytes) {
        const QString temp = writeTempFile(bytes, QStringLiteral("tidal_qt6_dl_"), QStringLiteral(".flac"));
        if (temp.isEmpty()) {
            onError(QStringLiteral("could not write temporary download"));
            return;
        }
        const QString saved = storeDownload(temp, trackId, meta);
        if (saved.isEmpty()) {
            QFile::remove(temp);
            onError(QStringLiteral("download save failed"));
            return;
        }
        onSuccess(QJsonObject{{QStringLiteral("path"), saved}, {QStringLiteral("track"), meta}});
    }, onError);
}

void TidalSidecar::transcodeToFlac(
    const QString& input,
    bool protocolWhitelist,
    const QString& trackId,
    const QJsonObject& meta,
    const QString& mpdPath,
    ObjectHandler onSuccess,
    ErrorHandler onError
) {
    if (input.isEmpty()) {
        onError(QStringLiteral("no playable input for download"));
        return;
    }
    QTemporaryFile out(QDir::tempPath() + QStringLiteral("/tidal_qt6_dl_XXXXXX.flac"));
    out.setAutoRemove(false);
    if (!out.open()) {
        onError(QStringLiteral("could not create temporary FLAC"));
        return;
    }
    const QString outPath = out.fileName();
    out.close();

    auto* process = new QProcess(this);
    QStringList cmd{QStringLiteral("-hide_banner"), QStringLiteral("-loglevel"), QStringLiteral("error"), QStringLiteral("-y")};
    if (protocolWhitelist) cmd << QStringLiteral("-protocol_whitelist") << QStringLiteral("file,https,tls,tcp,crypto");
    cmd << QStringLiteral("-i") << input << QStringLiteral("-c:a") << QStringLiteral("flac") << QStringLiteral("-f") << QStringLiteral("flac") << outPath;
    connect(process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, [this, process, outPath, trackId, meta, mpdPath, onSuccess, onError](int code, QProcess::ExitStatus status) {
        const QString err = QString::fromUtf8(process->readAllStandardError()).trimmed();
        process->deleteLater();
        if (!mpdPath.isEmpty()) QFile::remove(mpdPath);
        if (status != QProcess::NormalExit || code != 0 || QFileInfo(outPath).size() <= 0) {
            QFile::remove(outPath);
            onError(QStringLiteral("ffmpeg failed: %1").arg(err.isEmpty() ? QString::number(code) : err));
            return;
        }
        const QString saved = storeDownload(outPath, trackId, meta);
        if (saved.isEmpty()) {
            QFile::remove(outPath);
            onError(QStringLiteral("download save failed"));
            return;
        }
        onSuccess(QJsonObject{{QStringLiteral("path"), saved}, {QStringLiteral("track"), meta}});
    });
    process->start(QStringLiteral("ffmpeg"), cmd);
    if (!process->waitForStarted(3000)) {
        const QString err = process->errorString();
        process->deleteLater();
        QFile::remove(outPath);
        if (!mpdPath.isEmpty()) QFile::remove(mpdPath);
        onError(QStringLiteral("failed to start ffmpeg: %1").arg(err));
    }
}

QJsonObject TidalSidecar::streamDescriptorFromCandidate(const StreamCandidate& candidate, ErrorHandler onError) const {
    const QString manifest = candidate.stream.value(QStringLiteral("manifest")).toString();
    const QString manifestMime = candidate.stream.value(QStringLiteral("manifestMimeType")).toString();
    QString mpdPath;
    QString input = candidate.url.isEmpty() ? playableUrlFromStream(candidate.stream) : candidate.url;
    if (hasDashManifest(candidate.stream)) {
        const QByteArray bytes = decodedManifestBytes(manifest);
        mpdPath = writeTempFile(bytes, QStringLiteral("tidal_qt6_"), QStringLiteral(".mpd"));
        if (mpdPath.isEmpty()) {
            onError(QStringLiteral("could not write DASH manifest"));
            return {};
        }
        input = mpdPath;
    }
    if (input.isEmpty()) {
        onError(QStringLiteral("no playable URL or manifest was available for this track"));
        return {};
    }
    const int bitDepth = candidate.stream.value(QStringLiteral("bitDepth")).toInt(candidate.bitDepth ? candidate.bitDepth : 16);
    const int sampleRate = candidate.stream.value(QStringLiteral("sampleRate")).toInt(candidate.sampleRate ? candidate.sampleRate : 44100);
    const QString audioQuality = streamQuality(candidate.stream, candidate.quality);
    QString trackMaxQuality = parseTrackMaxQuality(candidate.track);
    if (trackMaxQuality.isEmpty()) trackMaxQuality = audioQuality;
    const double duration = jsonNumber(
        candidate.track,
        QStringLiteral("duration"),
        jsonNumber(candidate.track, QStringLiteral("duration_s"), jsonNumber(candidate.stream, QStringLiteral("duration")))
    );
    return QJsonObject{
        {QStringLiteral("input"), input},
        {QStringLiteral("url"), candidate.url},
        {QStringLiteral("mpd_path"), mpdPath},
        {QStringLiteral("is_dash"), !mpdPath.isEmpty()},
        {QStringLiteral("duration_s"), duration},
        {QStringLiteral("track"), candidate.track},
        {QStringLiteral("track_max_quality"), trackMaxQuality},
        {QStringLiteral("audio_quality"), audioQuality},
        {QStringLiteral("bit_depth"), bitDepth},
        {QStringLiteral("sample_rate"), sampleRate},
    };
}

QString TidalSidecar::downloadsDir() const {
    return QDir::home().filePath(QStringLiteral(".cache/tidal-bitperfect/downloads"));
}

QString TidalSidecar::safeFilenamePart(const QString& text, const QString& fallback) const {
    QString value = text.trimmed().isEmpty() ? fallback : text.trimmed();
    value.replace(QRegularExpression(QStringLiteral("[^0-9A-Za-z ._'-]+")), QStringLiteral("_"));
    value.replace(QRegularExpression(QStringLiteral("\\s+")), QStringLiteral(" "));
    value = value.trimmed();
    if (value.isEmpty()) value = fallback;
    return value.left(120);
}

QString TidalSidecar::downloadPath(const QString& trackId, const QJsonObject& meta) const {
    QString safeId = trackId;
    safeId.replace(QRegularExpression(QStringLiteral("[^0-9A-Za-z_-]+")), QStringLiteral("_"));
    if (safeId.isEmpty()) safeId = QString::number(qHash(trackId));
    const QString artist = safeFilenamePart(meta.value(QStringLiteral("artist_display")).toString(meta.value(QStringLiteral("artist")).toString()), QStringLiteral("Unknown Artist"));
    const QString title = safeFilenamePart(meta.value(QStringLiteral("title")).toString(), QStringLiteral("Track"));
    return QDir(downloadsDir()).filePath(QStringLiteral("%1 - %2 [%3].flac").arg(artist, title, safeId));
}

QString TidalSidecar::storeDownload(const QString& tempPath, const QString& trackId, const QJsonObject& meta) {
    QDir().mkpath(downloadsDir());
    QDir().mkpath(QDir::home().filePath(QStringLiteral(".cache/tidal-bitperfect")));
    const QString dest = downloadPath(trackId, meta);
    if (QFileInfo::exists(dest)) {
        QFile::remove(tempPath);
    } else if (!QFile::rename(tempPath, dest)) {
        if (!QFile::copy(tempPath, dest)) return {};
        QFile::remove(tempPath);
    }

    QFile indexFile(QDir::home().filePath(QStringLiteral(".cache/tidal-bitperfect/index.json")));
    QJsonObject index{{QStringLiteral("audio"), QJsonObject{}}, {QStringLiteral("covers"), QJsonObject{}}, {QStringLiteral("downloads"), QJsonObject{}}};
    if (indexFile.open(QIODevice::ReadOnly)) {
        const QJsonDocument doc = QJsonDocument::fromJson(indexFile.readAll());
        if (doc.isObject()) index = doc.object();
        indexFile.close();
    }
    QJsonObject downloads = index.value(QStringLiteral("downloads")).toObject();
    QJsonObject entry{{QStringLiteral("path"), dest}};
    const QFileInfo info(dest);
    entry.insert(QStringLiteral("mtime"), static_cast<double>(info.lastModified().toSecsSinceEpoch()));
    entry.insert(QStringLiteral("size"), static_cast<double>(info.size()));
    for (const QString& key : {QStringLiteral("title"), QStringLiteral("artist"), QStringLiteral("artist_id"), QStringLiteral("artists"), QStringLiteral("artist_display"), QStringLiteral("album"), QStringLiteral("album_id"), QStringLiteral("cover_url"), QStringLiteral("cover_thumbnail_url"), QStringLiteral("audio_quality"), QStringLiteral("track_max_quality"), QStringLiteral("bit_depth"), QStringLiteral("sample_rate")}) {
        if (meta.contains(key)) entry.insert(key, meta.value(key));
    }
    downloads.insert(trackId, entry);
    index.insert(QStringLiteral("downloads"), downloads);
    if (indexFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        indexFile.write(QJsonDocument(index).toJson(QJsonDocument::Indented));
    }
    return dest;
}

QString TidalSidecar::writeTempFile(const QByteArray& bytes, const QString& prefix, const QString& suffix) const {
    QTemporaryFile file(QDir::tempPath() + QStringLiteral("/") + prefix + QStringLiteral("XXXXXX") + suffix);
    file.setAutoRemove(false);
    if (!file.open()) return {};
    file.write(bytes);
    file.flush();
    return file.fileName();
}
