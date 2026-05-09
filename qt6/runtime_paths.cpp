#include "runtime_paths.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>
#include <QStringList>

namespace {

QString sourceDir() {
    return QStringLiteral(TIDAL_SOURCE_DIR);
}

QString appDirPath() {
    return QCoreApplication::applicationDirPath();
}

QString firstExistingFile(const QStringList& candidates) {
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return candidate;
    }
    return {};
}

QString firstExecutable(const QStringList& candidates) {
    for (const QString& candidate : candidates) {
        if (QFileInfo(candidate).isExecutable()) return candidate;
    }
    return {};
}

} // namespace

namespace RuntimePaths {

QString assetPath(const QString& fileName) {
    const QDir appDir(appDirPath());
    const QDir source(sourceDir());
    return firstExistingFile({
        appDir.filePath(QStringLiteral("../share/tidal-bitperfect/%1").arg(fileName)),
        appDir.filePath(QStringLiteral("../share/icons/hicolor/scalable/apps/%1").arg(fileName)),
        appDir.filePath(fileName),
        source.filePath(QStringLiteral("packaging/linux/%1").arg(fileName)),
    });
}

QString desktopFileId() {
    return QStringLiteral("tidal-bitperfect-qt6");
}

bool desktopFileInstalled(const QString& desktopFileName) {
    const QString fileName = desktopFileName.endsWith(QStringLiteral(".desktop"))
        ? desktopFileName
        : QStringLiteral("%1.desktop").arg(desktopFileName);
    return !QStandardPaths::locate(QStandardPaths::ApplicationsLocation, fileName).isEmpty();
}

QString nativePlayerPath() {
    const QString env = QString::fromLocal8Bit(qgetenv("TIDAL_NATIVE_PLAYER"));
    if (!env.isEmpty() && QFileInfo(env).isExecutable()) {
        return env;
    }

    const QDir appDir(appDirPath());
    const QDir source(sourceDir());
    const QString local = firstExecutable({
        appDir.filePath(QStringLiteral("tidal-native-player")),
        source.filePath(QStringLiteral("build/tidal-native-player")),
        source.filePath(QStringLiteral("tidal-native-player")),
    });
    if (!local.isEmpty()) {
        return local;
    }

    return QStandardPaths::findExecutable(QStringLiteral("tidal-native-player"));
}

} // namespace RuntimePaths
