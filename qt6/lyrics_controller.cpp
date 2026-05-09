#include "lyrics_controller.h"

#include "main_window_support.h"
#include "tidal_sidecar.h"

#include <QBrush>
#include <QColor>
#include <QDateTime>
#include <QEasingCurve>
#include <QEvent>
#include <QFont>
#include <QJsonObject>
#include <QJsonValue>
#include <QLabel>
#include <QListWidget>
#include <QPropertyAnimation>
#include <QRect>
#include <QScrollBar>
#include <QStringList>

#include <cmath>

using namespace MainWindowSupport;

LyricsController::LyricsController(TidalSidecar* sidecar, QObject* parent)
    : QObject(parent),
      m_sidecar(sidecar) {}

void LyricsController::setWidgets(QLabel* title, QLabel* meta, QListWidget* list) {
    if (m_list) {
        m_list->removeEventFilter(this);
        if (m_list->viewport()) m_list->viewport()->removeEventFilter(this);
        if (m_list->verticalScrollBar()) m_list->verticalScrollBar()->removeEventFilter(this);
    }
    QObject::disconnect(m_scrollActionConnection);
    QObject::disconnect(m_itemClickedConnection);

    m_title = title;
    m_meta = meta;
    m_list = list;

    if (m_list) {
        m_list->installEventFilter(this);
        m_list->viewport()->installEventFilter(this);
        m_list->verticalScrollBar()->installEventFilter(this);
        m_scrollActionConnection = connect(m_list->verticalScrollBar(), &QScrollBar::actionTriggered, this, [this](int) {
            holdAutoScroll();
        });
        m_itemClickedConnection = connect(m_list, &QListWidget::itemClicked, this, &LyricsController::seekToLyricItem);
    }
    clearList(QStringLiteral("Lyrics will appear for the currently playing track."));
}

void LyricsController::setReduceAnimations(bool reduce) {
    m_reduceAnimations = reduce;
    if (m_reduceAnimations) stopScrollAnimation();
}

void LyricsController::loadLyrics(const QString& trackId, const QString& title, bool offline) {
    stopScrollAnimation();
    resetState();
    m_currentTrackId = trackId;
    if (m_title) m_title->setText(title.isEmpty() ? QStringLiteral("Lyrics") : title);
    if (m_meta) m_meta->clear();
    if (offline) {
        clearList(QStringLiteral("Lyrics unavailable while offline."));
        return;
    }
    clearList(QStringLiteral("Loading lyrics..."));
    if (trackId.isEmpty() || !m_sidecar) {
        clearList(QStringLiteral("Lyrics unavailable."));
        return;
    }
    m_sidecar->request(QStringLiteral("lyrics"), {{QStringLiteral("track_id"), trackId}}, [this, trackId](const QJsonObject& result) {
        if (trackId != m_currentTrackId || !m_list) return;
        const QString provider = result.value(QStringLiteral("provider")).toString();
        if (m_meta) m_meta->setText(provider.isEmpty() ? QString() : QStringLiteral("Source: %1").arg(provider));
        m_timedLyrics = result.value(QStringLiteral("timed_lines")).toArray();
        m_currentLyricIndex = -1;
        m_list->clear();
        if (!m_timedLyrics.isEmpty()) {
            for (const QJsonValue& value : m_timedLyrics) {
                const QJsonObject line = value.toObject();
                const QString text = line.value(QStringLiteral("text")).toString();
                auto* item = new QListWidgetItem(text.isEmpty() ? QStringLiteral(" ") : text);
                item->setData(Qt::UserRole, line.value(QStringLiteral("start_s")).toDouble(-1.0));
                item->setToolTip(QStringLiteral("Click to seek to %1").arg(formatTime(line.value(QStringLiteral("start_s")).toDouble())));
                item->setForeground(QBrush(QColor(136, 136, 136)));
                m_list->addItem(item);
            }
            updatePosition(0.0);
            return;
        }
        const QString text = result.value(QStringLiteral("text")).toString();
        const QStringList lines = (text.isEmpty() ? QStringLiteral("No lyrics.") : text).split('\n');
        for (const QString& line : lines) m_list->addItem(line);
    }, [this, trackId](const QString&) {
        if (trackId != m_currentTrackId) return;
        clearList(QStringLiteral("Lyrics unavailable."));
    });
}

void LyricsController::updatePosition(double positionSeconds) {
    if (m_timedLyrics.isEmpty() || !m_list) return;
    int active = -1;
    for (qsizetype i = 0; i < m_timedLyrics.size(); ++i) {
        const QJsonObject line = m_timedLyrics.at(i).toObject();
        const double start = line.value(QStringLiteral("start_s")).toDouble(-1.0);
        const QJsonValue endValue = line.value(QStringLiteral("end_s"));
        double end = endValue.isDouble() ? endValue.toDouble() : -1.0;
        if (end <= 0.0 && i + 1 < m_timedLyrics.size()) {
            end = m_timedLyrics.at(i + 1).toObject().value(QStringLiteral("start_s")).toDouble(-1.0);
        }
        if (start >= 0.0 && positionSeconds >= start && (end <= 0.0 || positionSeconds < end)) {
            active = static_cast<int>(i);
            break;
        }
        if (start >= 0.0 && positionSeconds >= start) active = static_cast<int>(i);
    }
    if (active == m_currentLyricIndex) return;
    if (m_currentLyricIndex >= 0 && m_currentLyricIndex < m_list->count()) {
        QListWidgetItem* previous = m_list->item(m_currentLyricIndex);
        QFont font = previous->font();
        font.setBold(false);
        font.setPointSize(m_list->font().pointSize());
        previous->setFont(font);
        previous->setForeground(QBrush(QColor(136, 136, 136)));
        previous->setBackground(QBrush());
    }
    m_currentLyricIndex = active;
    if (active >= 0 && active < m_list->count()) {
        QListWidgetItem* item = m_list->item(active);
        QFont font = item->font();
        font.setBold(true);
        font.setPointSize(qMax(m_list->font().pointSize() + 2, 15));
        item->setFont(font);
        item->setForeground(QBrush(QColor(240, 240, 240)));
        item->setBackground(QBrush(QColor(45, 55, 48)));
        if (!autoScrollHeld()) scrollToCurrentLine(true);
    }
}

void LyricsController::scrollToCurrentLine(bool animated) {
    if (!m_list || m_currentLyricIndex < 0 || m_currentLyricIndex >= m_list->count()) return;
    QScrollBar* bar = m_list->verticalScrollBar();
    if (!bar) return;
    const QRect rect = m_list->visualItemRect(m_list->item(m_currentLyricIndex));
    if (!rect.isValid()) return;
    int target = bar->value() + rect.top() + (rect.height() / 2) - (m_list->viewport()->height() / 2);
    target = qBound(bar->minimum(), target, bar->maximum());
    stopScrollAnimation();
    if (!animated || m_reduceAnimations || std::abs(bar->value() - target) <= 2) {
        bar->setValue(target);
        return;
    }
    auto* animation = new QPropertyAnimation(bar, "value", this);
    animation->setDuration(140);
    animation->setEasingCurve(QEasingCurve::OutQuad);
    animation->setStartValue(bar->value());
    animation->setEndValue(target);
    connect(animation, &QPropertyAnimation::finished, this, [this, animation]() {
        if (m_scrollAnimation == animation) m_scrollAnimation = nullptr;
        animation->deleteLater();
    });
    m_scrollAnimation = animation;
    animation->start();
}

void LyricsController::stopScrollAnimation() {
    if (!m_scrollAnimation) return;
    QPropertyAnimation* animation = m_scrollAnimation;
    m_scrollAnimation = nullptr;
    animation->stop();
    animation->deleteLater();
}

bool LyricsController::eventFilter(QObject* watched, QEvent* event) {
    if (m_list
        && (watched == m_list
            || watched == m_list->viewport()
            || watched == m_list->verticalScrollBar())) {
        switch (event->type()) {
        case QEvent::Wheel:
        case QEvent::MouseButtonPress:
        case QEvent::MouseButtonDblClick:
        case QEvent::TouchBegin:
        case QEvent::TouchUpdate:
        case QEvent::KeyPress:
            holdAutoScroll();
            break;
        default:
            break;
        }
    }
    return QObject::eventFilter(watched, event);
}

void LyricsController::resetState() {
    m_timedLyrics = {};
    m_currentLyricIndex = -1;
    m_autoScrollHoldUntilMs = 0;
}

void LyricsController::clearList(const QString& message) {
    if (!m_list) return;
    m_list->clear();
    if (!message.isEmpty()) m_list->addItem(message);
}

void LyricsController::holdAutoScroll() {
    m_autoScrollHoldUntilMs = QDateTime::currentMSecsSinceEpoch() + kLyricsAutoScrollHoldMs;
    stopScrollAnimation();
}

bool LyricsController::autoScrollHeld() const {
    return QDateTime::currentMSecsSinceEpoch() < m_autoScrollHoldUntilMs;
}

void LyricsController::seekToLyricItem(QListWidgetItem* item) {
    if (!item) return;
    bool ok = false;
    const double start = item->data(Qt::UserRole).toDouble(&ok);
    if (!ok || start < 0.0) return;
    holdAutoScroll();
    if (m_list) m_list->setCurrentItem(nullptr);
    emit seekRequested(start);
}
