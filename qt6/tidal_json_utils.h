#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QJsonValue>
#include <QString>
#include <initializer_list>

namespace TidalJson {

inline QString nonEmptyString(const QJsonObject& obj, std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        const QString value = obj.value(QString::fromLatin1(key)).toVariant().toString();
        if (!value.isEmpty()) return value;
    }
    return {};
}

inline QJsonObject objectAt(const QJsonObject& obj, std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        const QJsonValue value = obj.value(QString::fromLatin1(key));
        if (value.isObject()) return value.toObject();
    }
    return {};
}

inline QJsonArray arrayAt(const QJsonObject& obj, std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        const QJsonValue value = obj.value(QString::fromLatin1(key));
        if (value.isArray()) return value.toArray();
        if (value.isObject()) {
            const QJsonObject nested = value.toObject();
            if (nested.value(QStringLiteral("items")).isArray()) return nested.value(QStringLiteral("items")).toArray();
            if (nested.value(QStringLiteral("data")).isArray()) return nested.value(QStringLiteral("data")).toArray();
        }
    }
    return {};
}

inline QJsonObject unwrapDataObject(const QJsonObject& obj) {
    if (obj.value(QStringLiteral("data")).isObject()) return obj.value(QStringLiteral("data")).toObject();
    if (obj.value(QStringLiteral("item")).isObject()) return obj.value(QStringLiteral("item")).toObject();
    if (obj.value(QStringLiteral("resource")).isObject()) return obj.value(QStringLiteral("resource")).toObject();
    return obj;
}

inline QString firstNestedText(const QJsonObject& obj, const QString& key) {
    const QJsonValue value = obj.value(key);
    if (value.isString()) return value.toString();
    if (value.isObject()) {
        const QJsonObject nested = value.toObject();
        const QString title = nonEmptyString(nested, {"title", "text", "name"});
        if (!title.isEmpty()) return title;
    }
    return {};
}

} // namespace TidalJson
