#include "tidal_sidecar.h"
#include "tidal_json_utils.h"

#include <QNetworkReply>
#include <QTimer>
#include <QUrl>
#include <memory>

using TidalJson::arrayAt;
using TidalJson::firstNestedText;
using TidalJson::nonEmptyString;
using TidalJson::objectAt;
using TidalJson::unwrapDataObject;

namespace {

QJsonArray markFavoriteItems(const QJsonArray& items) {
    QJsonArray out;
    for (const QJsonValue& value : items) {
        if (!value.isObject()) {
            out.append(value);
            continue;
        }
        QJsonObject obj = value.toObject();
        obj.insert(QStringLiteral("favorite"), true);
        out.append(obj);
    }
    return out;
}

} // namespace

TidalSidecar::TidalSidecar(QObject* parent) : QObject(parent) {}

TidalSidecar::~TidalSidecar() {
    rejectAll(QStringLiteral("TIDAL API shutting down"));
}

void TidalSidecar::start() {
    if (m_started) return;
    m_started = true;
    QTimer::singleShot(0, this, [this]() { emit ready(); });
}

bool TidalSidecar::isRunning() const {
    return m_started;
}

int TidalSidecar::request(
    const QString& command,
    const QJsonObject& args,
    SuccessHandler onSuccess,
    ErrorHandler onError
) {
    if (!isRunning()) start();
    const int id = m_nextId++;
    m_pending.insert(id, Pending{std::move(onSuccess), std::move(onError)});
    QTimer::singleShot(0, this, [this, id, command, args]() {
        dispatch(id, command, args);
    });
    return id;
}

void TidalSidecar::dispatch(int id, const QString& command, const QJsonObject& args) {
    if (command == QStringLiteral("login")) {
        cmdLogin(id);
    } else if (command == QStringLiteral("search")) {
        cmdSearch(id, args);
    } else if (command == QStringLiteral("url")) {
        cmdUrl(id, args);
    } else if (command == QStringLiteral("collection")) {
        cmdCollection(id, args);
    } else if (command == QStringLiteral("home")) {
        cmdHome(id);
    } else if (command == QStringLiteral("lyrics")) {
        cmdLyrics(id, args);
    } else if (command == QStringLiteral("radio")) {
        cmdRadio(id, args);
    } else if (command == QStringLiteral("details")) {
        cmdDetails(id, args);
    } else if (command == QStringLiteral("favorite")) {
        cmdFavorite(id, args);
    } else if (command == QStringLiteral("stream")) {
        cmdStream(id, args);
    } else if (command == QStringLiteral("download")) {
        cmdDownload(id, args);
    } else {
        reject(id, QStringLiteral("unknown TIDAL command: %1").arg(command));
    }
}

void TidalSidecar::complete(int id, const QJsonObject& result) {
    if (!m_pending.contains(id)) return;
    Pending pending = m_pending.take(id);
    if (pending.onSuccess) pending.onSuccess(result);
}

void TidalSidecar::reject(int id, const QString& message) {
    if (!m_pending.contains(id)) return;
    Pending pending = m_pending.take(id);
    if (pending.onError) pending.onError(message);
    else emit statusMessage(message);
}

void TidalSidecar::rejectAll(const QString& message) {
    const auto pending = m_pending;
    m_pending.clear();
    for (const Pending& item : pending) {
        if (item.onError) item.onError(message);
    }
}

void TidalSidecar::cmdLogin(int id) {
    loadSavedLogin(
        [this, id](const QJsonObject&) {
            complete(id, QJsonObject{{QStringLiteral("logged_in"), true}, {QStringLiteral("reused"), true}});
        },
        [this, id](const QString&) {
            startDeviceLogin(
                [this, id](const QJsonObject&) {
                    complete(id, QJsonObject{{QStringLiteral("logged_in"), true}, {QStringLiteral("reused"), false}});
                },
                [this, id](const QString& error) { reject(id, error); }
            );
        }
    );
}

void TidalSidecar::cmdSearch(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString kind = mediaTypeKey(args.value(QStringLiteral("type")).toString(QStringLiteral("track")));
    const QString query = args.value(QStringLiteral("query")).toString();
    const int limit = qBound(1, args.value(QStringLiteral("limit")).toInt(10), 50);
    if (query.trimmed().isEmpty()) {
        complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), QJsonArray{}}});
        return;
    }

    auto doSearch = std::make_shared<std::function<void(const QStringList&, int)>>();
    *doSearch = [this, id, kind, limit, doSearch](const QStringList& queries, int index) {
        if (index >= queries.size()) {
            complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), QJsonArray{}}});
            return;
        }
        const QString q = queries.at(index);
        QJsonObject params{{QStringLiteral("query"), q}, {QStringLiteral("limit"), limit}, {QStringLiteral("offset"), 0}, {QStringLiteral("types"), searchTypesForKind(kind)}};
        apiRequest(QStringLiteral("GET"), QStringLiteral("search"), params, {}, ApiBase::V1,
            [this, id, kind, queries, index, doSearch](const QJsonValue& value) {
                const QJsonObject payload = value.toObject();
                QJsonArray items;
                if (kind == QStringLiteral("track")) items = parseTracksArray(payload.value(QStringLiteral("tracks")));
                else if (kind == QStringLiteral("album")) items = parseAlbumsArray(payload.value(QStringLiteral("albums")));
                else if (kind == QStringLiteral("playlist")) items = parsePlaylistsArray(payload.value(QStringLiteral("playlists")));
                else if (kind == QStringLiteral("artist")) items = parseArtistsArray(payload.value(QStringLiteral("artists")));
                if (items.isEmpty() && kind == QStringLiteral("track")) {
                    (*doSearch)(queries, index + 1);
                    return;
                }
                complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), items}});
            },
            [this, id](const QString& error) { reject(id, error); }
        );
    };

    QStringList queries{query};
    if (query.contains(QStringLiteral(" - "))) queries.append(QString(query).replace(QStringLiteral(" - "), QStringLiteral(" ")));
    if (query.contains(QLatin1Char('-')) && !query.contains(QStringLiteral(" - "))) queries.append(QString(query).replace(QLatin1Char('-'), QLatin1Char(' ')));
    (*doSearch)(queries, 0);
}

void TidalSidecar::cmdUrl(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString originalUrl = args.value(QStringLiteral("url")).toString().trimmed();
    if (originalUrl.isEmpty()) {
        reject(id, QStringLiteral("missing TIDAL URL"));
        return;
    }

    auto parseAndLoad = [this, id](const QString& resolved) {
        QUrl url(resolved);
        QStringList parts = url.path().split(QLatin1Char('/'), Qt::SkipEmptyParts);
        if (!parts.isEmpty() && parts.first() == QStringLiteral("browse")) parts.removeFirst();
        if (parts.size() < 2) {
            reject(id, QStringLiteral("unrecognized TIDAL URL format"));
            return;
        }
        const QString kind = mediaTypeKey(parts.at(0));
        QString itemId = parts.at(1);
        if (kind != QStringLiteral("playlist")) itemId = itemId.section(QLatin1Char('-'), 0, 0);
        if (itemId.isEmpty() || !(kind == QStringLiteral("track") || kind == QStringLiteral("album") || kind == QStringLiteral("playlist") || kind == QStringLiteral("artist"))) {
            reject(id, QStringLiteral("unsupported TIDAL URL"));
            return;
        }

        auto finish = [this, id, kind](const QJsonObject& item) {
            complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), QJsonArray{item}}});
        };
        if (kind == QStringLiteral("track")) loadTrack(itemId, finish, [this, id](const QString& e) { reject(id, e); });
        else if (kind == QStringLiteral("album")) loadAlbum(itemId, true, finish, [this, id](const QString& e) { reject(id, e); });
        else if (kind == QStringLiteral("playlist")) loadPlaylist(itemId, true, finish, [this, id](const QString& e) { reject(id, e); });
        else loadArtist(itemId, false, finish, [this, id](const QString& e) { reject(id, e); });
    };

    if (!originalUrl.contains(QStringLiteral("link.tidal.com"))) {
        parseAndLoad(originalUrl);
        return;
    }

    QNetworkRequest req{QUrl(originalUrl)};
    req.setHeader(QNetworkRequest::UserAgentHeader, QStringLiteral("tidal-bitperfect-qt6/0.1"));
    req.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    QNetworkReply* reply = m_network.head(req);
    connect(reply, &QNetworkReply::finished, this, [this, reply, parseAndLoad, originalUrl]() {
        const QUrl resolved = reply->url();
        const QString out = resolved.isValid() && !resolved.isEmpty() ? resolved.toString() : originalUrl;
        reply->deleteLater();
        parseAndLoad(out);
    });
}

void TidalSidecar::cmdCollection(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString kind = mediaTypeKey(args.value(QStringLiteral("type")).toString(QStringLiteral("track")));
    const QJsonObject params{{QStringLiteral("limit"), 100}, {QStringLiteral("offset"), 0}, {QStringLiteral("order"), QStringLiteral("DATE")}, {QStringLiteral("orderDirection"), QStringLiteral("DESC")}};

    if (kind == QStringLiteral("playlist")) {
        const QJsonObject v2Params{{QStringLiteral("folderId"), QStringLiteral("root")}, {QStringLiteral("offset"), 0}, {QStringLiteral("limit"), 50}, {QStringLiteral("includeOnly"), QStringLiteral("PLAYLIST")}, {QStringLiteral("order"), QStringLiteral("DATE")}, {QStringLiteral("orderDirection"), QStringLiteral("DESC")}};
        apiRequest(QStringLiteral("GET"), QStringLiteral("my-collection/playlists/folders"), v2Params, {}, ApiBase::V2,
            [this, id, kind](const QJsonValue& value) {
                complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), markFavoriteItems(parsePlaylistsArray(value))}});
            },
            [this, id](const QString& error) { reject(id, error); }
        );
        return;
    }

    apiRequest(QStringLiteral("GET"), QStringLiteral("users/%1/favorites/%2").arg(m_userId, kind + QStringLiteral("s")), params, {}, ApiBase::V1,
        [this, id, kind](const QJsonValue& value) {
            QJsonArray items;
            if (kind == QStringLiteral("track")) {
                items = parseTracksArray(value);
                if (items.isEmpty()) {
                    QStringList ids;
                    for (const QJsonValue& item : arrayFromPayload(value)) {
                        const QJsonObject obj = itemObject(item);
                        const QString tid = nonEmptyString(obj, {"id", "track_id", "trackId"});
                        if (!tid.isEmpty()) ids.append(tid);
                    }
                    fetchTracksByIds(ids, [this, id, kind](const QJsonArray& fetched) {
                        complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), markFavoriteItems(fetched)}});
                    }, [this, id](const QString& error) { reject(id, error); });
                    return;
                }
            }
            else if (kind == QStringLiteral("album")) items = parseAlbumsArray(value);
            else if (kind == QStringLiteral("artist")) items = parseArtistsArray(value);
            complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("items"), markFavoriteItems(items)}});
        },
        [this, id](const QString& error) { reject(id, error); }
    );
}

void TidalSidecar::cmdHome(int id) {
    if (!ensureLoggedIn(id)) return;
    const QJsonObject params{{QStringLiteral("deviceType"), QStringLiteral("BROWSER")}, {QStringLiteral("locale"), m_locale}, {QStringLiteral("platform"), QStringLiteral("WEB")}};
    apiRequest(QStringLiteral("GET"), QStringLiteral("home/feed/static"), params, {}, ApiBase::V2,
        [this, id](const QJsonValue& value) {
            QJsonArray sections;
            const QJsonObject root = value.toObject();
            const QJsonArray rawSections = arrayAt(root, {"items", "rows", "modules"});
            for (const QJsonValue& sectionValue : rawSections) {
                const QJsonObject sectionObj = sectionValue.toObject();
                const QString title = firstNestedText(sectionObj, QStringLiteral("title")).isEmpty()
                    ? nonEmptyString(sectionObj, {"title", "header", "name"})
                    : firstNestedText(sectionObj, QStringLiteral("title"));
                QJsonArray items = homeItemsFromValue(sectionObj, 12);
                if (items.isEmpty()) continue;
                sections.append(QJsonObject{{QStringLiteral("title"), title.isEmpty() ? QStringLiteral("Home") : title}, {QStringLiteral("items"), items}});
            }
            if (sections.isEmpty()) {
                const QJsonArray items = homeItemsFromValue(value, 24);
                if (!items.isEmpty()) sections.append(QJsonObject{{QStringLiteral("title"), QStringLiteral("Home")}, {QStringLiteral("items"), items}});
            }
            if (sections.isEmpty()) {
                reject(id, QStringLiteral("could not load home feed"));
                return;
            }
            complete(id, QJsonObject{{QStringLiteral("sections"), sections}});
        },
        [this, id](const QString& error) { reject(id, error); }
    );
}

void TidalSidecar::cmdLyrics(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString trackId = args.value(QStringLiteral("track_id")).toVariant().toString();
    QJsonObject empty{{QStringLiteral("track_id"), trackId}, {QStringLiteral("provider"), QJsonValue()}, {QStringLiteral("right_to_left"), false}, {QStringLiteral("text"), QString()}, {QStringLiteral("timed_lines"), QJsonArray{}}, {QStringLiteral("error"), QJsonValue()}};
    if (trackId.isEmpty()) {
        complete(id, empty);
        return;
    }
    apiRequest(QStringLiteral("GET"), QStringLiteral("tracks/%1/lyrics").arg(trackId), {}, {}, ApiBase::V1,
        [this, id, empty](const QJsonValue& value) mutable {
            const QJsonObject obj = value.toObject();
            const QString lyrics = nonEmptyString(obj, {"lyrics", "text"});
            const QString subtitles = nonEmptyString(obj, {"subtitles", "subtitle"});
            empty.insert(QStringLiteral("provider"), nonEmptyString(obj, {"lyricsProvider", "provider"}));
            empty.insert(QStringLiteral("right_to_left"), obj.value(QStringLiteral("rightToLeft")).toBool(false));
            empty.insert(QStringLiteral("text"), plainLyricsText(lyrics, subtitles));
            empty.insert(QStringLiteral("timed_lines"), parseTimedLyrics(subtitles));
            complete(id, empty);
        },
        [this, id, empty](const QString&) { complete(id, empty); }
    );
}

void TidalSidecar::cmdRadio(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString artistId = args.value(QStringLiteral("artist_id")).toVariant().toString();
    const QString trackId = args.value(QStringLiteral("track_id")).toVariant().toString();
    if (!artistId.isEmpty()) {
        apiRequest(QStringLiteral("GET"), QStringLiteral("artists/%1/radio").arg(artistId), {{QStringLiteral("limit"), 30}}, {}, ApiBase::V1,
            [this, id](const QJsonValue& value) {
                complete(id, QJsonObject{{QStringLiteral("items"), parseTracksArray(value)}});
            },
            [this, id](const QString& error) { reject(id, error); }
        );
        return;
    }
    if (trackId.isEmpty()) {
        reject(id, QStringLiteral("missing radio id"));
        return;
    }

    auto paths = std::make_shared<QStringList>(QStringList{
        QStringLiteral("tracks/%1/relationships/radio").arg(trackId),
        QStringLiteral("tracks/%1/radio").arg(trackId),
        QStringLiteral("tracks/%1/recommendations").arg(trackId),
        QStringLiteral("track/%1/radio").arg(trackId),
    });
    auto tryPath = std::make_shared<std::function<void(int, QString)>>();
    *tryPath = [this, id, paths, tryPath](int index, QString lastError) {
        if (index >= paths->size()) {
            reject(id, lastError.isEmpty() ? QStringLiteral("radio request failed") : lastError);
            return;
        }
        apiRequest(QStringLiteral("GET"), paths->at(index), {{QStringLiteral("limit"), 30}}, {}, ApiBase::V1,
            [this, id](const QJsonValue& value) {
                QJsonArray tracks = parseTracksArray(value);
                if (!tracks.isEmpty()) {
                    complete(id, QJsonObject{{QStringLiteral("items"), tracks}});
                    return;
                }
                QStringList ids;
                for (const QJsonValue& item : arrayFromPayload(value)) {
                    const QJsonObject obj = itemObject(item);
                    const QString tid = nonEmptyString(obj, {"id", "track_id", "trackId"});
                    if (!tid.isEmpty()) ids.append(tid);
                }
                fetchTracksByIds(ids, [this, id](const QJsonArray& fetched) {
                    complete(id, QJsonObject{{QStringLiteral("items"), fetched}});
                }, [this, id](const QString& error) { reject(id, error); });
            },
            [tryPath, index](const QString& error) { (*tryPath)(index + 1, error); }
        );
    };
    (*tryPath)(0, {});
}

void TidalSidecar::cmdDetails(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString kind = mediaTypeKey(args.value(QStringLiteral("type")).toString());
    const QString itemId = args.value(QStringLiteral("id")).toVariant().toString();
    if (itemId.isEmpty()) {
        reject(id, QStringLiteral("missing details id"));
        return;
    }
    auto finish = [this, id, kind](const QJsonObject& item) {
        complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("item"), item}});
    };
    auto fail = [this, id](const QString& error) { reject(id, error); };
    if (kind == QStringLiteral("album")) loadAlbum(itemId, true, finish, fail);
    else if (kind == QStringLiteral("playlist")) loadPlaylist(itemId, true, finish, fail);
    else if (kind == QStringLiteral("artist")) loadArtist(itemId, true, finish, fail);
    else if (kind == QStringLiteral("mix")) loadMix(itemId, finish, fail);
    else reject(id, QStringLiteral("unsupported details type: %1").arg(kind));
}

void TidalSidecar::cmdFavorite(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString kind = mediaTypeKey(args.value(QStringLiteral("type")).toString(QStringLiteral("track")));
    const QString itemId = args.value(QStringLiteral("id")).toVariant().toString();
    const bool favorite = args.value(QStringLiteral("favorite")).toBool(true);
    if (itemId.isEmpty()) {
        reject(id, QStringLiteral("missing favorite id"));
        return;
    }

    if (kind == QStringLiteral("playlist")) {
        if (favorite) {
            const QJsonObject params{{QStringLiteral("folderId"), QStringLiteral("root")}, {QStringLiteral("uuids"), itemId}};
            apiRequest(QStringLiteral("PUT"), QStringLiteral("my-collection/playlists/folders/add-favorites"), params, {}, ApiBase::V2,
                [this, id, kind, itemId, favorite](const QJsonValue&) { complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("id"), itemId}, {QStringLiteral("favorite"), favorite}}); },
                [this, id](const QString& error) { reject(id, error); }
            );
        } else {
            const QJsonObject params{{QStringLiteral("trns"), QStringLiteral("trn:playlist:%1").arg(itemId)}};
            apiRequest(QStringLiteral("PUT"), QStringLiteral("my-collection/playlists/folders/remove"), params, {}, ApiBase::V2,
                [this, id, kind, itemId, favorite](const QJsonValue&) { complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("id"), itemId}, {QStringLiteral("favorite"), favorite}}); },
                [this, id](const QString& error) { reject(id, error); }
            );
        }
        return;
    }

    const QString plural = kind + QStringLiteral("s");
    const QString path = QStringLiteral("users/%1/favorites/%2").arg(m_userId, plural);
    if (favorite) {
        apiRequest(QStringLiteral("POST"), path, {}, {{favoriteFormKeyForKind(kind), itemId}}, ApiBase::V1,
            [this, id, kind, itemId, favorite](const QJsonValue&) { complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("id"), itemId}, {QStringLiteral("favorite"), favorite}}); },
            [this, id](const QString& error) { reject(id, error); }
        );
    } else {
        apiRequest(QStringLiteral("DELETE"), path + QStringLiteral("/") + itemId, {}, {}, ApiBase::V1,
            [this, id, kind, itemId, favorite](const QJsonValue&) { complete(id, QJsonObject{{QStringLiteral("type"), kind}, {QStringLiteral("id"), itemId}, {QStringLiteral("favorite"), favorite}}); },
            [this, id](const QString& error) { reject(id, error); }
        );
    }
}

void TidalSidecar::cmdStream(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString trackId = args.value(QStringLiteral("track_id")).toVariant().toString();
    if (trackId.isEmpty()) {
        reject(id, QStringLiteral("missing track id"));
        return;
    }
    fetchStreamCandidates(trackId, QStringList{QStringLiteral("HI_RES_LOSSLESS"), QStringLiteral("LOSSLESS"), QStringLiteral("HIGH"), QStringLiteral("LOW")}, {},
        [this, id](const QJsonObject& result) { complete(id, result); },
        [this, id](const QString& error) { reject(id, error); }
    );
}

void TidalSidecar::cmdDownload(int id, const QJsonObject& args) {
    if (!ensureLoggedIn(id)) return;
    const QString trackId = args.value(QStringLiteral("track_id")).toVariant().toString();
    if (trackId.isEmpty()) {
        reject(id, QStringLiteral("missing track id"));
        return;
    }
    loadTrack(trackId,
        [this, id, trackId](const QJsonObject& meta) {
            fetchStreamCandidates(trackId, QStringList{QStringLiteral("HI_RES_LOSSLESS"), QStringLiteral("LOSSLESS"), QStringLiteral("HIGH"), QStringLiteral("LOW")}, {},
                [this, id, trackId, meta](const QJsonObject& stream) {
                    QJsonObject downloadMeta = meta;
                    const QJsonObject resolvedTrack = stream.value(QStringLiteral("track")).toObject();
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
                        const QJsonValue existing = downloadMeta.value(key);
                        const QJsonValue resolved = resolvedTrack.value(key);
                        if ((existing.isUndefined() || existing.isNull() || existing.toVariant().toString().isEmpty())
                            && !resolved.isUndefined()
                            && !resolved.isNull()) {
                            downloadMeta.insert(key, resolved);
                        }
                    }
                    for (const QString& key : {
                             QStringLiteral("audio_quality"),
                             QStringLiteral("track_max_quality"),
                             QStringLiteral("bit_depth"),
                             QStringLiteral("sample_rate"),
                         }) {
                        if (stream.contains(key)) downloadMeta.insert(key, stream.value(key));
                    }
                    const QString input = stream.value(QStringLiteral("input")).toString();
                    const QString url = stream.value(QStringLiteral("url")).toString();
                    const QString mpdPath = stream.value(QStringLiteral("mpd_path")).toString();
                    const bool isDash = stream.value(QStringLiteral("is_dash")).toBool(false);
                    if (!url.isEmpty() && QUrl(url).path().toLower().endsWith(QStringLiteral(".flac"))) {
                        downloadDirectFlac(url, trackId, downloadMeta,
                            [this, id](const QJsonObject& result) { complete(id, result); },
                            [this, id](const QString& error) { reject(id, error); }
                        );
                    } else {
                        transcodeToFlac(input, isDash, trackId, downloadMeta, mpdPath,
                            [this, id](const QJsonObject& result) { complete(id, result); },
                            [this, id](const QString& error) { reject(id, error); }
                        );
                    }
                },
                [this, id](const QString& error) { reject(id, error); }
            );
        },
        [this, id](const QString& error) { reject(id, error); }
    );
}
