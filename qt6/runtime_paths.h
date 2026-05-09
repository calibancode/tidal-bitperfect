#pragma once

#include <QString>

namespace RuntimePaths {

QString assetPath(const QString& fileName);
QString desktopFileId();
bool desktopFileInstalled(const QString& desktopFileName);
QString nativePlayerPath();

} // namespace RuntimePaths
