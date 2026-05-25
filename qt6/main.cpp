#include "main_window.h"

#include <QApplication>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QIcon>

#include "runtime_paths.h"

int main(int argc, char** argv) {
    QApplication app(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("tidal-bitperfect"));
    QCoreApplication::setOrganizationDomain(QStringLiteral("local"));
    QCoreApplication::setApplicationName(QStringLiteral("tidal-bitperfect-qt6"));
    app.setApplicationDisplayName(QStringLiteral("TIDAL Bitperfect Qt6"));
    const QString desktopId = RuntimePaths::desktopFileId();
    if (RuntimePaths::desktopFileInstalled(desktopId)) {
        QGuiApplication::setDesktopFileName(desktopId);
    }

    const QString iconPath = RuntimePaths::assetPath(QStringLiteral("tidal-bitperfect-qt6.svg"));
    if (!iconPath.isEmpty()) app.setWindowIcon(QIcon(iconPath));

    MainWindow window;
    window.resize(1100, 720);
    window.show();
    return app.exec();
}
