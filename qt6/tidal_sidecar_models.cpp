#include "tidal_sidecar.h"
#include "tidal_json_utils.h"

#include <QRegularExpression>
#include <algorithm>
#include <cmath>

using TidalJson::arrayAt;
using TidalJson::firstNestedText;
using TidalJson::nonEmptyString;
using TidalJson::objectAt;
using TidalJson::unwrapDataObject;

namespace {

QJsonObject unwrapArtistObject(const QJsonObject& raw) {
    if (raw.value(QStringLiteral("artist")).isObject()) return raw.value(QStringLiteral("artist")).toObject();
    if (raw.value(QStringLiteral("item")).isObject()) return raw.value(QStringLiteral("item")).toObject();
    if (raw.value(QStringLiteral("resource")).isObject()) return raw.value(QStringLiteral("resource")).toObject();
    if (raw.value(QStringLiteral("data")).isObject()) return raw.value(QStringLiteral("data")).toObject();
    return raw;
}

QString firstArtistId(const QJsonObject& obj, const QJsonObject& artistObj = {}) {
    const QString direct = nonEmptyString(obj, {"artistId", "artist_id"});
    if (!direct.isEmpty()) return direct;

    const QString nested = nonEmptyString(artistObj, {"id", "artistId"});
    if (!nested.isEmpty()) return nested;

    for (const QJsonValue& value : arrayAt(obj, {"artists"})) {
        if (!value.isObject()) continue;
        const QJsonObject artist = unwrapArtistObject(value.toObject());
        const QString id = nonEmptyString(artist, {"id", "artistId"});
        if (!id.isEmpty()) return id;
    }
    return {};
}

QString normalizedQuality(QString quality) {
    quality = quality.trimmed().toUpper();
    quality.replace(QLatin1Char('-'), QLatin1Char('_'));
    quality.replace(QLatin1Char(' '), QLatin1Char('_'));
    if (quality == QStringLiteral("HIRES") || quality == QStringLiteral("HI_RES")) return QStringLiteral("HI_RES_LOSSLESS");
    if (quality == QStringLiteral("HIFI") || quality == QStringLiteral("HI_FI")) return QStringLiteral("LOSSLESS");
    return quality;
}

QString qualityString(const QJsonObject& obj) {
    return normalizedQuality(nonEmptyString(obj, {"audioQuality", "audio_quality", "quality", "maxQuality", "max_quality"}));
}

bool isHiResTag(QString tag) {
    tag = tag.trimmed().toUpper();
    return tag.contains(QStringLiteral("HIRES_LOSSLESS"))
        || tag.contains(QStringLiteral("HI_RES_LOSSLESS"))
        || tag.contains(QStringLiteral("HIRES"))
        || tag.contains(QStringLiteral("HI_RES"));
}

QString qualityFromMediaTags(const QJsonObject& obj) {
    const QJsonObject mediaMetadata = obj.value(QStringLiteral("mediaMetadata")).toObject();
    const QJsonValue tags = mediaMetadata.value(QStringLiteral("tags"));
    if (tags.isArray()) {
        for (const QJsonValue& tag : tags.toArray()) {
            QString text;
            if (tag.isObject()) text = nonEmptyString(tag.toObject(), {"name", "type", "tag", "id"});
            else text = tag.toString();
            if (isHiResTag(text)) return QStringLiteral("HI_RES_LOSSLESS");
        }
    } else if (tags.isObject()) {
        const QJsonObject tagObj = tags.toObject();
        for (auto it = tagObj.begin(); it != tagObj.end(); ++it) {
            if (isHiResTag(it.key()) && it.value().toBool(true)) return QStringLiteral("HI_RES_LOSSLESS");
            if (isHiResTag(it.value().toVariant().toString())) return QStringLiteral("HI_RES_LOSSLESS");
        }
    }
    return {};
}

QString imageIdString(const QJsonObject& obj, std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        const QJsonValue value = obj.value(QString::fromLatin1(key));
        if (value.isString()) {
            const QString text = value.toString().trimmed();
            if (!text.isEmpty()) return text;
        }
        if (value.isObject()) {
            const QJsonObject nested = value.toObject();
            const QString text = nonEmptyString(nested, {"url", "id", "uuid", "imageId", "cover", "squareImage"});
            if (!text.isEmpty()) return text;
        }
    }
    return {};
}

QString trackCoverId(const QJsonObject& track, const QJsonObject& album) {
    const QString albumCover = imageIdString(album, {"cover", "image", "imageId", "squareImage", "coverArt", "coverImage"});
    if (!albumCover.isEmpty()) return albumCover;
    return imageIdString(track, {"cover", "image", "imageId", "squareImage", "coverArt", "coverImage", "albumCover", "thumbnail"});
}

} // namespace

QJsonObject TidalSidecar::parseTrack(const QJsonObject& raw, const QJsonObject& albumOverride) const {
    const QJsonObject obj = unwrapDataObject(raw);
    const QJsonObject artistObj = objectAt(obj, {"artist"});
    const QJsonArray artistsRaw = arrayAt(obj, {"artists"});
    QStringList artists;
    for (const QJsonValue& value : artistsRaw) {
        const QString name = value.toObject().value(QStringLiteral("name")).toString();
        if (!name.isEmpty() && !artists.contains(name)) artists.append(name);
    }
    if (artists.isEmpty() && !artistObj.value(QStringLiteral("name")).toString().isEmpty()) artists.append(artistObj.value(QStringLiteral("name")).toString());
    const QJsonObject albumObj = albumOverride.isEmpty() ? objectAt(obj, {"album"}) : albumOverride;
    const QString albumTitle = nonEmptyString(albumObj, {"title", "name"});
    const QString coverId = trackCoverId(obj, albumObj);
    QString trackQuality = qualityString(obj);
    if (trackQuality.isEmpty()) trackQuality = qualityFromMediaTags(obj);
    return QJsonObject{
        {QStringLiteral("id"), obj.value(QStringLiteral("id")).toVariant().toString()},
        {QStringLiteral("artist"), artistObj.value(QStringLiteral("name")).toString(artists.isEmpty() ? QStringLiteral("?") : artists.first())},
        {QStringLiteral("artist_id"), firstArtistId(obj, artistObj)},
        {QStringLiteral("artists"), QJsonArray::fromStringList(artists)},
        {QStringLiteral("artist_display"), artistDisplay(obj)},
        {QStringLiteral("title"), nonEmptyString(obj, {"title", "name"}).isEmpty() ? QStringLiteral("?") : nonEmptyString(obj, {"title", "name"})},
        {QStringLiteral("album"), albumTitle},
        {QStringLiteral("album_id"), albumObj.value(QStringLiteral("id")).toVariant().toString()},
        {QStringLiteral("duration"), jsonNumber(obj, QStringLiteral("duration"))},
        {QStringLiteral("audio_quality"), trackQuality},
        {QStringLiteral("track_max_quality"), trackQuality},
        {QStringLiteral("cover_url"), imageUrl(coverId, {}, QStringLiteral("640"))},
        {QStringLiteral("cover_thumbnail_url"), imageUrl(coverId, {}, QStringLiteral("320"))},
    };
}

QJsonObject TidalSidecar::parseAlbum(const QJsonObject& raw, bool includeEmptyTracks) const {
    const QJsonObject obj = unwrapDataObject(raw);
    const QJsonObject artistObj = objectAt(obj, {"artist"});
    const QString coverId = imageIdString(obj, {"cover", "image", "imageId", "squareImage", "coverArt", "coverImage"});
    QJsonObject out{
        {QStringLiteral("id"), obj.value(QStringLiteral("id")).toVariant().toString()},
        {QStringLiteral("album_id"), obj.value(QStringLiteral("id")).toVariant().toString()},
        {QStringLiteral("title"), nonEmptyString(obj, {"title", "name"}).isEmpty() ? QStringLiteral("?") : nonEmptyString(obj, {"title", "name"})},
        {QStringLiteral("artist"), artistObj.value(QStringLiteral("name")).toString(QStringLiteral("?"))},
        {QStringLiteral("artist_id"), firstArtistId(obj, artistObj)},
        {QStringLiteral("artists"), QJsonArray::fromStringList(artistNames(obj))},
        {QStringLiteral("artist_display"), artistDisplay(obj)},
        {QStringLiteral("cover_url"), imageUrl(coverId, {}, QStringLiteral("640"))},
        {QStringLiteral("cover_thumbnail_url"), imageUrl(coverId, {}, QStringLiteral("320"))},
        {QStringLiteral("year"), obj.value(QStringLiteral("releaseDate")).toString().left(4).toInt()},
        {QStringLiteral("release_type"), nonEmptyString(obj, {"type", "releaseType"})},
        {QStringLiteral("version"), obj.value(QStringLiteral("version"))},
        {QStringLiteral("explicit"), obj.value(QStringLiteral("explicit")).toBool(false)},
        {QStringLiteral("audio_modes"), obj.value(QStringLiteral("audioModes")).toArray()},
        {QStringLiteral("num_tracks"), obj.value(QStringLiteral("numberOfTracks")).toInt(obj.value(QStringLiteral("numTracks")).toInt())},
    };
    if (includeEmptyTracks) out.insert(QStringLiteral("tracks"), QJsonArray{});
    return out;
}

QJsonObject TidalSidecar::parsePlaylist(const QJsonObject& raw, bool includeEmptyTracks) const {
    const QJsonObject obj = unwrapDataObject(raw);
    const QJsonObject creator = objectAt(obj, {"creator", "owner"});
    const QString coverId = nonEmptyString(obj, {"squareImage", "image", "cover", "imageId"});
    QJsonObject out{
        {QStringLiteral("id"), nonEmptyString(obj, {"uuid", "id"})},
        {QStringLiteral("title"), nonEmptyString(obj, {"title", "name"}).isEmpty() ? QStringLiteral("?") : nonEmptyString(obj, {"title", "name"})},
        {QStringLiteral("creator"), creator.value(QStringLiteral("name")).toString()},
        {QStringLiteral("cover_url"), imageUrl(coverId, {}, coverId == obj.value(QStringLiteral("image")).toString() ? QStringLiteral("wide") : QStringLiteral("480"))},
    };
    if (includeEmptyTracks) out.insert(QStringLiteral("tracks"), QJsonArray{});
    return out;
}

QJsonObject TidalSidecar::parseArtist(const QJsonObject& raw, bool includeEmptyDetails) const {
    const QJsonObject obj = unwrapDataObject(raw);
    QJsonObject out{
        {QStringLiteral("id"), obj.value(QStringLiteral("id")).toVariant().toString()},
        {QStringLiteral("name"), obj.value(QStringLiteral("name")).toString(QStringLiteral("?"))},
        {QStringLiteral("cover_url"), imageUrl(obj.value(QStringLiteral("picture")).toString(), {}, QStringLiteral("320"))},
    };
    if (includeEmptyDetails) {
        out.insert(QStringLiteral("tracks"), QJsonArray{});
        out.insert(QStringLiteral("albums"), QJsonArray{});
        out.insert(QStringLiteral("ep_singles"), QJsonArray{});
    }
    return out;
}

QJsonObject TidalSidecar::parseMix(const QJsonObject& raw) const {
    const QJsonObject obj = unwrapDataObject(raw);
    return QJsonObject{
        {QStringLiteral("id"), nonEmptyString(obj, {"id", "mixId"})},
        {QStringLiteral("title"), nonEmptyString(obj, {"title", "name"}).isEmpty() ? QStringLiteral("?") : nonEmptyString(obj, {"title", "name"})},
        {QStringLiteral("sub_title"), nonEmptyString(obj, {"subTitle", "sub_title", "subtitle"})},
        {QStringLiteral("mix_type"), nonEmptyString(obj, {"mixType", "mix_type", "type"})},
        {QStringLiteral("cover_url"), imageUrl(nonEmptyString(obj, {"image", "cover", "imageId", "squareImage"}), {}, QStringLiteral("320"))},
        {QStringLiteral("tracks"), QJsonArray{}},
    };
}

QJsonArray TidalSidecar::parseTracksArray(const QJsonValue& value, const QJsonObject& albumOverride) const {
    QJsonArray out;
    for (const QJsonValue& item : arrayFromPayload(value)) {
        const QJsonObject obj = itemObject(item);
        if (obj.isEmpty()) continue;
        if (nonEmptyString(obj, {"title", "name"}).isEmpty()
            && !obj.contains(QStringLiteral("album"))
            && !obj.contains(QStringLiteral("artists"))) {
            continue;
        }
        out.append(parseTrack(obj, albumOverride));
    }
    return out;
}

QJsonArray TidalSidecar::parseAlbumsArray(const QJsonValue& value) const {
    QJsonArray out;
    for (const QJsonValue& item : arrayFromPayload(value)) {
        const QJsonObject obj = itemObject(item);
        if (!obj.isEmpty()) out.append(parseAlbum(obj, false));
    }
    return out;
}

QJsonArray TidalSidecar::parsePlaylistsArray(const QJsonValue& value) const {
    QJsonArray out;
    for (const QJsonValue& item : arrayFromPayload(value)) {
        const QJsonObject obj = itemObject(item);
        if (!obj.isEmpty()) out.append(parsePlaylist(obj, false));
    }
    return out;
}

QJsonArray TidalSidecar::parseArtistsArray(const QJsonValue& value) const {
    QJsonArray out;
    for (const QJsonValue& item : arrayFromPayload(value)) {
        const QJsonObject obj = itemObject(item);
        if (!obj.isEmpty()) out.append(parseArtist(obj, false));
    }
    return out;
}

QJsonArray TidalSidecar::arrayFromPayload(const QJsonValue& value) const {
    if (value.isArray()) return value.toArray();
    if (!value.isObject()) return {};
    const QJsonObject obj = value.toObject();
    for (const QString& key : {QStringLiteral("items"), QStringLiteral("data"), QStringLiteral("tracks"), QStringLiteral("albums"), QStringLiteral("artists"), QStringLiteral("playlists")}) {
        const QJsonValue nested = obj.value(key);
        if (nested.isArray()) return nested.toArray();
        if (nested.isObject()) {
            const QJsonArray arr = arrayFromPayload(nested);
            if (!arr.isEmpty()) return arr;
        }
    }
    return {};
}

QJsonObject TidalSidecar::itemObject(const QJsonValue& value) const {
    if (!value.isObject()) return {};
    QJsonObject obj = value.toObject();
    if (obj.value(QStringLiteral("item")).isObject()) obj = obj.value(QStringLiteral("item")).toObject();
    if (obj.value(QStringLiteral("resource")).isObject()) obj = obj.value(QStringLiteral("resource")).toObject();
    if (obj.value(QStringLiteral("data")).isObject() && obj.size() <= 3) obj = obj.value(QStringLiteral("data")).toObject();
    return obj;
}

QJsonArray TidalSidecar::homeItemsFromValue(const QJsonValue& value, int limit) const {
    QJsonArray out;
    if (limit <= 0) return out;
    if (value.isArray()) {
        for (const QJsonValue& child : value.toArray()) {
            const QJsonArray nested = homeItemsFromValue(child, limit - out.size());
            for (const QJsonValue& item : nested) {
                out.append(item);
                if (out.size() >= limit) return out;
            }
        }
        return out;
    }
    if (!value.isObject()) return out;
    const QJsonObject obj = value.toObject();
    const QJsonObject direct = homeItemFromObject(obj);
    if (!direct.isEmpty()) {
        out.append(direct);
        return out;
    }
    for (const QString& key : {QStringLiteral("items"), QStringLiteral("data"), QStringLiteral("modules"), QStringLiteral("rows"), QStringLiteral("pagedList"), QStringLiteral("list")}) {
        const QJsonValue child = obj.value(key);
        if (child.isUndefined()) continue;
        const QJsonArray nested = homeItemsFromValue(child, limit - out.size());
        for (const QJsonValue& item : nested) {
            out.append(item);
            if (out.size() >= limit) return out;
        }
    }
    return out;
}

QJsonObject TidalSidecar::homeItemFromObject(const QJsonObject& obj) const {
    QJsonObject item = itemObject(obj);
    if (item.isEmpty()) item = obj;
    QString type = mediaTypeKey(nonEmptyString(obj, {"type", "contentType", "itemType"}));
    if (type.isEmpty() || type == QStringLiteral("unknown")) type = mediaTypeKey(nonEmptyString(item, {"type", "contentType", "itemType"}));
    if (type == QStringLiteral("track") || (item.contains(QStringLiteral("artists")) && item.contains(QStringLiteral("album")))) {
        return QJsonObject{{QStringLiteral("type"), QStringLiteral("track")}, {QStringLiteral("data"), parseTrack(item)}};
    }
    if (type == QStringLiteral("album") || (item.contains(QStringLiteral("cover")) && item.contains(QStringLiteral("numberOfTracks")))) {
        return QJsonObject{{QStringLiteral("type"), QStringLiteral("album")}, {QStringLiteral("data"), parseAlbum(item, false)}};
    }
    if (type == QStringLiteral("playlist") || item.contains(QStringLiteral("uuid"))) {
        return QJsonObject{{QStringLiteral("type"), QStringLiteral("playlist")}, {QStringLiteral("data"), parsePlaylist(item, false)}};
    }
    if (type == QStringLiteral("mix") || item.contains(QStringLiteral("mixId"))) {
        return QJsonObject{{QStringLiteral("type"), QStringLiteral("mix")}, {QStringLiteral("data"), parseMix(item)}};
    }
    return {};
}

QJsonArray TidalSidecar::parseTimedLyrics(const QString& subtitles) const {
    QJsonArray out;
    static const QRegularExpression lineRe(QStringLiteral("((?:\\[(?:(\\d{1,2}):)?(\\d{1,2})(?:\\.(\\d{1,3}))?\\])+)(.*)"));
    static const QRegularExpression stampRe(QStringLiteral("\\[(?:(\\d{1,2}):)?(\\d{1,2})(?:\\.(\\d{1,3}))?\\]"));
    struct Line { double start = 0.0; QString text; int order = 0; };
    QVector<Line> lines;
    int order = 0;
    for (const QString& raw : subtitles.split(QLatin1Char('\n'))) {
        const QRegularExpressionMatch lineMatch = lineRe.match(raw);
        if (!lineMatch.hasMatch()) {
            ++order;
            continue;
        }
        const QString text = lineMatch.captured(5).trimmed();
        if (text.isEmpty()) {
            ++order;
            continue;
        }
        QRegularExpressionMatchIterator it = stampRe.globalMatch(lineMatch.captured(1));
        while (it.hasNext()) {
            const QRegularExpressionMatch match = it.next();
            const int minutes = match.captured(1).isEmpty() ? 0 : match.captured(1).toInt();
            const int seconds = match.captured(2).toInt();
            const QString fracText = match.captured(3);
            const double frac = fracText.isEmpty() ? 0.0 : fracText.toDouble() / std::pow(10.0, fracText.size());
            lines.push_back(Line{minutes * 60.0 + seconds + frac, text, order});
        }
        ++order;
    }
    std::sort(lines.begin(), lines.end(), [](const Line& a, const Line& b) {
        if (a.start == b.start) return a.order < b.order;
        return a.start < b.start;
    });
    for (int i = 0; i < lines.size(); ++i) {
        QJsonObject obj{{QStringLiteral("start_s"), lines.at(i).start}, {QStringLiteral("text"), lines.at(i).text}};
        if (i + 1 < lines.size() && lines.at(i + 1).start > lines.at(i).start) obj.insert(QStringLiteral("end_s"), lines.at(i + 1).start);
        else obj.insert(QStringLiteral("end_s"), QJsonValue());
        out.append(obj);
    }
    return out;
}

QString TidalSidecar::imageUrl(const QString& imageId, const QString& fallback, const QString& size) const {
    QString id = imageId.isEmpty() ? fallback : imageId;
    if (id.isEmpty()) return {};
    if (id.startsWith(QStringLiteral("http://"), Qt::CaseInsensitive)
        || id.startsWith(QStringLiteral("https://"), Qt::CaseInsensitive)) {
        return id;
    }
    id.replace(QLatin1Char('-'), QLatin1Char('/'));
    if (size == QStringLiteral("origin")) return QStringLiteral("https://resources.tidal.com/images/%1/origin.jpg").arg(id);
    if (size == QStringLiteral("wide")) return QStringLiteral("https://resources.tidal.com/images/%1/1080x720.jpg").arg(id);
    const QString dimension = size.isEmpty() ? QStringLiteral("320") : size;
    return QStringLiteral("https://resources.tidal.com/images/%1/%2x%2.jpg").arg(id, dimension);
}

QString TidalSidecar::artistDisplay(const QJsonObject& obj) const {
    const QStringList names = artistNames(obj);
    if (names.isEmpty()) return QStringLiteral("?");
    if (names.size() == 1) return names.first();
    if (names.size() == 2) return names.at(0) + QStringLiteral(" & ") + names.at(1);
    QStringList head = names;
    const QString last = head.takeLast();
    return head.join(QStringLiteral(", ")) + QStringLiteral(" & ") + last;
}

QStringList TidalSidecar::artistNames(const QJsonObject& obj) const {
    QStringList names;
    for (const QJsonValue& value : arrayAt(obj, {"artists"})) {
        const QString name = value.toObject().value(QStringLiteral("name")).toString();
        if (!name.isEmpty() && !names.contains(name)) names.append(name);
    }
    if (names.isEmpty()) {
        const QString name = objectAt(obj, {"artist"}).value(QStringLiteral("name")).toString();
        if (!name.isEmpty()) names.append(name);
    }
    return names;
}

QString TidalSidecar::mediaTypeKey(const QString& kind) const {
    QString k = kind.trimmed().toLower();
    if (k.endsWith(QLatin1Char('s'))) k.chop(1);
    if (k == QStringLiteral("tracks") || k == QStringLiteral("track") || k == QStringLiteral("song")) return QStringLiteral("track");
    if (k == QStringLiteral("album")) return QStringLiteral("album");
    if (k == QStringLiteral("playlist")) return QStringLiteral("playlist");
    if (k == QStringLiteral("artist")) return QStringLiteral("artist");
    if (k == QStringLiteral("mix") || k == QStringLiteral("mixe")) return QStringLiteral("mix");
    return k.isEmpty() ? QStringLiteral("track") : k;
}

QString TidalSidecar::searchTypesForKind(const QString& kind) const {
    if (kind == QStringLiteral("track")) return QStringLiteral("tracks");
    if (kind == QStringLiteral("album")) return QStringLiteral("albums");
    if (kind == QStringLiteral("playlist")) return QStringLiteral("playlists");
    if (kind == QStringLiteral("artist")) return QStringLiteral("artists");
    return kind + QStringLiteral("s");
}

QString TidalSidecar::favoritePathForKind(const QString& kind) const {
    return QStringLiteral("users/%1/favorites/%2").arg(m_userId, kind + QStringLiteral("s"));
}

QString TidalSidecar::favoriteFormKeyForKind(const QString& kind) const {
    if (kind == QStringLiteral("album")) return QStringLiteral("albumId");
    if (kind == QStringLiteral("artist")) return QStringLiteral("artistId");
    if (kind == QStringLiteral("playlist")) return QStringLiteral("playlistId");
    return QStringLiteral("trackId");
}

QString TidalSidecar::parseTrackMaxQuality(const QJsonObject& track) const {
    const QString fromTags = qualityFromMediaTags(track);
    return fromTags.isEmpty() ? qualityString(track) : fromTags;
}

QString TidalSidecar::plainLyricsText(const QString& lyrics, const QString& subtitles) const {
    if (!lyrics.trimmed().isEmpty()) return lyrics.trimmed();
    QStringList lines;
    static const QRegularExpression stampRe(QStringLiteral("(?:\\[(?:\\d{1,2}:)?\\d{1,2}(?:\\.\\d{1,3})?\\])+"));
    for (QString line : subtitles.split(QLatin1Char('\n'))) {
        line.remove(stampRe);
        line = line.trimmed();
        if (!line.isEmpty()) lines.append(line);
    }
    return lines.join(QLatin1Char('\n')).trimmed();
}

int TidalSidecar::qualityRank(const QString& quality) const {
    const QString q = quality.toUpper();
    if (q == QStringLiteral("HI_RES_LOSSLESS")) return 3;
    if (q == QStringLiteral("LOSSLESS")) return 2;
    if (q == QStringLiteral("HIGH")) return 1;
    if (q == QStringLiteral("LOW")) return 0;
    return 0;
}

int TidalSidecar::streamScore(const QJsonObject& stream) const {
    return qualityRank(qualityString(stream)) * 100000000
        + stream.value(QStringLiteral("bitDepth")).toInt(0) * 100000
        + stream.value(QStringLiteral("sampleRate")).toInt(0);
}

double TidalSidecar::jsonNumber(const QJsonObject& obj, const QString& key, double fallback) const {
    const QJsonValue value = obj.value(key);
    if (value.isDouble()) return value.toDouble();
    if (value.isString()) {
        bool ok = false;
        const double parsed = value.toString().toDouble(&ok);
        if (ok) return parsed;
    }
    return fallback;
}
