#include "lyrics_controller.h"

#include "main_window_support.h"
#include "tidal_client.h"

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

namespace {
constexpr int kLyricStartRole = Qt::UserRole;
constexpr int kTimedLyricRole = Qt::UserRole + 1;

const QColor kLyricCurrentColor(242, 242, 242);
const QColor kLyricPastColor(176, 176, 176);
const QColor kLyricFutureColor(124, 124, 124);
const QColor kLyricMessageColor(150, 150, 150);
} // namespace

LyricsController::LyricsController(TidalClient* tidal, QObject* parent)
    : QObject(parent),
      m_tidal(tidal) {}

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
    if (trackId.isEmpty() || !m_tidal) {
        clearList(QStringLiteral("Lyrics unavailable."));
        return;
    }
    m_tidal->request(QStringLiteral("lyrics"), {{QStringLiteral("track_id"), trackId}}, [this, trackId](const QJsonObject& result) {
        if (trackId != m_currentTrackId || !m_list) return;
        const QString provider = result.value(QStringLiteral("provider")).toString();
        m_timedLyrics = result.value(QStringLiteral("timed_lines")).toArray();
        m_currentLyricIndex = -1;
        m_list->clear();
        if (!m_timedLyrics.isEmpty()) {
            for (const QJsonValue& value : m_timedLyrics) {
                addTimedLine(value.toObject());
            }
            updateMetaLabel(provider, true, true);
            updatePosition(0.0);
            return;
        }
        const QString text = result.value(QStringLiteral("text")).toString();
        const bool hasText = !text.trimmed().isEmpty();
        const QStringList lines = (hasText ? text : QStringLiteral("No lyrics.")).split('\n');
        updateMetaLabel(provider, false, hasText);
        for (const QString& line : lines) addPlainLine(line);
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
    const int previousIndex = m_currentLyricIndex;
    m_currentLyricIndex = active;
    styleLyricItem(previousIndex);
    if (active >= 0 && active < m_list->count()) {
        styleLyricItem(active);
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
    if (message.isEmpty()) return;
    auto* item = new QListWidgetItem(message);
    item->setFlags(Qt::ItemIsEnabled);
    item->setForeground(QBrush(kLyricMessageColor));
    m_list->addItem(item);
}

void LyricsController::addTimedLine(const QJsonObject& line) {
    if (!m_list) return;
    const QString text = line.value(QStringLiteral("text")).toString();
    const double start = line.value(QStringLiteral("start_s")).toDouble(-1.0);
    auto* item = new QListWidgetItem(text.isEmpty() ? QStringLiteral(" ") : text);
    if (start >= 0.0) {
        item->setData(kLyricStartRole, start);
        item->setData(kTimedLyricRole, true);
        item->setToolTip(QStringLiteral("Click to seek to %1").arg(formatTime(start)));
    }
    item->setForeground(QBrush(kLyricFutureColor));
    m_list->addItem(item);
}

void LyricsController::addPlainLine(const QString& text) {
    if (!m_list) return;
    auto* item = new QListWidgetItem(text.isEmpty() ? QStringLiteral(" ") : text);
    item->setFlags(Qt::ItemIsEnabled);
    item->setForeground(QBrush(kLyricPastColor));
    m_list->addItem(item);
}

void LyricsController::updateMetaLabel(const QString& provider, bool synced, bool hasLyrics) {
    if (!m_meta) return;
    QStringList parts;
    if (hasLyrics) parts << (synced ? QStringLiteral("Synced lyrics") : QStringLiteral("Plain lyrics"));
    if (!provider.isEmpty()) parts << QStringLiteral("Source: %1").arg(provider);
    m_meta->setText(parts.join(QStringLiteral(" - ")));
}

void LyricsController::styleLyricItem(int index) {
    if (!m_list || index < 0 || index >= m_list->count()) return;
    QListWidgetItem* item = m_list->item(index);
    if (!item || !itemIsTimed(item)) return;
    QFont font = item->font();
    font.setBold(index == m_currentLyricIndex);
    font.setPointSize(m_list->font().pointSize());
    item->setFont(font);
    item->setBackground(QBrush());
    if (index == m_currentLyricIndex) {
        item->setForeground(QBrush(kLyricCurrentColor));
    } else if (index < m_currentLyricIndex) {
        item->setForeground(QBrush(kLyricPastColor));
    } else {
        item->setForeground(QBrush(kLyricFutureColor));
    }
}

bool LyricsController::itemIsTimed(const QListWidgetItem* item) const {
    return item && item->data(kTimedLyricRole).toBool();
}

void LyricsController::holdAutoScroll() {
    m_autoScrollHoldUntilMs = QDateTime::currentMSecsSinceEpoch() + kLyricsAutoScrollHoldMs;
    stopScrollAnimation();
}

bool LyricsController::autoScrollHeld() const {
    return QDateTime::currentMSecsSinceEpoch() < m_autoScrollHoldUntilMs;
}

void LyricsController::seekToLyricItem(QListWidgetItem* item) {
    if (!item || !itemIsTimed(item)) return;
    bool ok = false;
    const double start = item->data(kLyricStartRole).toDouble(&ok);
    if (!ok || start < 0.0) return;
    holdAutoScroll();
    if (m_list) m_list->setCurrentItem(nullptr);
    emit seekRequested(start);
}
