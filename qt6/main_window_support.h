#pragma once

#include <QJsonObject>
#include <QLabel>
#include <QPixmap>
#include <QString>
#include <QStringList>
#include <QVector>
#include <Qt>

class QResizeEvent;
class QTreeWidget;
class QTreeWidgetItem;

namespace MainWindowSupport {

inline constexpr int kDetailsStateRole = Qt::UserRole + 1;
inline constexpr int kLoadingPlaceholderRole = Qt::UserRole + 2;
inline constexpr qint64 kLyricsAutoScrollHoldMs = 8000;

class CoverLabel : public QLabel {
public:
    explicit CoverLabel(QWidget* parent = nullptr);

    void setCoverPixmap(const QPixmap& pixmap);
    void setFallbackPixmap(const QPixmap& pixmap);

protected:
    void resizeEvent(QResizeEvent* event) override;

private:
    void updateScaledPixmap();

    QPixmap m_original;
    QPixmap m_fallback;
};

QPixmap fallbackCoverPixmap();
bool isContainerType(const QString& type);
bool isTrackObject(const QJsonObject& obj);
bool shouldRememberTrackObject(const QJsonObject& obj, const QString& typeHint = QString());
QVector<QJsonObject> trackObjects(const QJsonObject& obj);
QString mediaTypeKey(const QString& label);
QString formatTime(double seconds);
QString formatBytes(qint64 bytes);
QString qualityLabelText(const QString& audioQuality, int bitDepth, int sampleRate);
bool textInputFocused();
bool networkOffline();
QStringList playbackDevices();
QTreeWidgetItem* findItemByIdentity(QTreeWidget* tree, const QString& type, const QString& id);

} // namespace MainWindowSupport
