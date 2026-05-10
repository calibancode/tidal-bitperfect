#pragma once

#include <QJsonArray>
#include <QObject>
#include <QPointer>
#include <QString>

class QLabel;
class QListWidget;
class QListWidgetItem;
class QEvent;
class QPropertyAnimation;
class TidalClient;

class LyricsController : public QObject {
    Q_OBJECT

public:
    explicit LyricsController(TidalClient* tidal, QObject* parent = nullptr);

    void setWidgets(QLabel* title, QLabel* meta, QListWidget* list);
    void setReduceAnimations(bool reduce);
    void loadLyrics(const QString& trackId, const QString& title, bool offline);
    void updatePosition(double positionSeconds);
    void scrollToCurrentLine(bool animated = true);
    void stopScrollAnimation();

signals:
    void seekRequested(double seconds);

protected:
    bool eventFilter(QObject* watched, QEvent* event) override;

private:
    void resetState();
    void clearList(const QString& message = QString());
    void holdAutoScroll();
    bool autoScrollHeld() const;
    void seekToLyricItem(QListWidgetItem* item);

    TidalClient* m_tidal = nullptr;
    QPointer<QLabel> m_title;
    QPointer<QLabel> m_meta;
    QPointer<QListWidget> m_list;
    QPropertyAnimation* m_scrollAnimation = nullptr;
    QMetaObject::Connection m_scrollActionConnection;
    QMetaObject::Connection m_itemClickedConnection;
    QJsonArray m_timedLyrics;
    QString m_currentTrackId;
    qint64 m_autoScrollHoldUntilMs = 0;
    int m_currentLyricIndex = -1;
    bool m_reduceAnimations = false;
};
