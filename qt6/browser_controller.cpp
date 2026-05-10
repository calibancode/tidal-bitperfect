#include "browser_controller.h"

#include "main_window_support.h"
#include "tidal_client.h"

#include <QJsonValue>
#include <QTimer>
#include <QTreeWidget>
#include <QTreeWidgetItem>

#include <utility>

using namespace MainWindowSupport;

BrowserController::BrowserController(TidalClient* tidal, QObject* parent)
    : QObject(parent),
      m_tidal(tidal),
      m_loadingTimer(new QTimer(this)) {
    m_loadingTimer->setInterval(300);
    connect(m_loadingTimer, &QTimer::timeout, this, &BrowserController::tickLoadingLabels);
}

void BrowserController::setRequireOnlineCallback(RequireOnlineCallback callback) {
    m_requireOnline = std::move(callback);
}

bool BrowserController::requireOnline(const QString& action) {
    return !m_requireOnline || m_requireOnline(action);
}

void BrowserController::loadHome(QTreeWidget* tree) {
    if (!m_tidal || !requireOnline(QStringLiteral("Home"))) return;
    m_tidal->request(QStringLiteral("home"), {}, [this, tree](const QJsonObject& result) {
        populateTree(tree, result.value(QStringLiteral("sections")).toArray(), QString(), true);
    }, [this](const QString& error) {
        emit errorMessage(QStringLiteral("Home"), error);
    });
}

void BrowserController::search(QTreeWidget* tree, const QString& query, const QString& type, int limit) {
    if (!m_tidal || !requireOnline(QStringLiteral("Search"))) return;
    const QString kind = mediaTypeKey(type);
    QJsonObject args{{QStringLiteral("query"), query}, {QStringLiteral("type"), kind}, {QStringLiteral("limit"), limit}};
    m_tidal->request(QStringLiteral("search"), args, [this, tree](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        emit tracksDiscovered(items);
        populateTree(tree, items, result.value(QStringLiteral("type")).toString());
    }, [this](const QString& error) {
        emit errorMessage(QStringLiteral("Search"), error);
    });
}

void BrowserController::loadUrl(QTreeWidget* tree, const QString& url, bool queueAfterLoad) {
    if (!m_tidal || !requireOnline(queueAfterLoad ? QStringLiteral("URL queue") : QStringLiteral("URL loading"))) return;
    m_tidal->request(QStringLiteral("url"), {{QStringLiteral("url"), url}}, [this, tree, queueAfterLoad](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        emit tracksDiscovered(items);
        populateTree(tree, items, result.value(QStringLiteral("type")).toString(), !queueAfterLoad, !queueAfterLoad);
        if (queueAfterLoad) queueLoadedUrlItems(tree);
    }, [this](const QString& error) {
        emit errorMessage(QStringLiteral("URL"), error);
    });
}

void BrowserController::refreshCollection(QTreeWidget* tree, const QString& type) {
    if (!m_tidal || !requireOnline(QStringLiteral("Collection refresh"))) return;
    const QString kind = mediaTypeKey(type);
    m_tidal->request(QStringLiteral("collection"), {{QStringLiteral("type"), kind}}, [this, tree](const QJsonObject& result) {
        const QJsonArray items = result.value(QStringLiteral("items")).toArray();
        emit tracksDiscovered(items);
        emit favoriteItemsDiscovered(result.value(QStringLiteral("type")).toString(), items);
        populateTree(tree, items, result.value(QStringLiteral("type")).toString());
    }, [this](const QString& error) {
        emit errorMessage(QStringLiteral("Collection"), error);
    });
}

void BrowserController::refreshFavoriteState() {
    if (!m_tidal || !requireOnline(QStringLiteral("Favorite state"))) return;
    for (const QString& type : {QStringLiteral("track"), QStringLiteral("album"), QStringLiteral("playlist"), QStringLiteral("artist")}) {
        m_tidal->request(
            QStringLiteral("collection"),
            {{QStringLiteral("type"), type}},
            [this, type](const QJsonObject& result) {
                const QJsonArray items = result.value(QStringLiteral("items")).toArray();
                emit favoriteItemsDiscovered(result.value(QStringLiteral("type")).toString(type), items);
                emit tracksDiscovered(items);
            },
            [this, type](const QString& error) {
                emit statusMessage(QStringLiteral("Favorite state sync failed for %1: %2").arg(type, error));
            }
        );
    }
}

void BrowserController::populateTree(QTreeWidget* tree, const QJsonArray& items, const QString& typeHint, bool expandRoots, bool loadSingleLazyRoot) {
    if (!tree) return;
    clearLoadingItemsForTree(tree);
    tree->clear();
    for (const QJsonValue& value : items) {
        if (value.isObject()) tree->addTopLevelItem(makeItem(value.toObject(), typeHint));
    }
    if (expandRoots) {
        for (int i = 0; i < tree->topLevelItemCount(); ++i) tree->topLevelItem(i)->setExpanded(true);
    }
    if (loadSingleLazyRoot) requestDetailsForSingleLazyRoot(tree, false);
}

QTreeWidgetItem* BrowserController::makeItem(const QJsonObject& obj, const QString& typeHint) const {
    QString type = typeHint;
    QJsonObject data = obj;
    if (obj.contains(QStringLiteral("items"))) {
        type = QStringLiteral("section");
        data = obj;
    }
    if (obj.contains(QStringLiteral("data"))) {
        type = obj.value(QStringLiteral("type")).toString(type);
        data = obj.value(QStringLiteral("data")).toObject();
    }
    if (type.isEmpty()) {
        if (data.contains(QStringLiteral("items"))) type = QStringLiteral("section");
        else if (data.contains(QStringLiteral("name"))) type = QStringLiteral("artist");
        else if (data.contains(QStringLiteral("creator"))) type = QStringLiteral("playlist");
        else if (data.contains(QStringLiteral("tracks"))) type = QStringLiteral("album");
        else type = QStringLiteral("track");
    }
    QString text;
    if (type == QStringLiteral("section")) text = data.value(QStringLiteral("title")).toString(QStringLiteral("Section"));
    else if (type == QStringLiteral("artist")) text = QStringLiteral("Artist - %1").arg(data.value(QStringLiteral("name")).toString());
    else if (type == QStringLiteral("album")) text = QStringLiteral("Album - %1 - %2").arg(data.value(QStringLiteral("artist_display")).toString(data.value(QStringLiteral("artist")).toString()), data.value(QStringLiteral("title")).toString());
    else if (type == QStringLiteral("playlist")) text = QStringLiteral("Playlist - %1").arg(data.value(QStringLiteral("title")).toString());
    else if (type == QStringLiteral("mix")) text = QStringLiteral("Mix - %1").arg(data.value(QStringLiteral("title")).toString());
    else text = trackLineText(data);
    auto* item = new QTreeWidgetItem(QStringList{text});
    data.insert(QStringLiteral("_type"), type);
    item->setData(0, Qt::UserRole, data);
    addChildren(item, data);
    prepareLazyContainer(item, data);
    return item;
}

void BrowserController::addChildren(QTreeWidgetItem* item, const QJsonObject& data) const {
    for (const QJsonValue& child : data.value(QStringLiteral("items")).toArray()) {
        if (child.isObject()) item->addChild(makeItem(child.toObject()));
    }
    if (data.value(QStringLiteral("_type")).toString() == QStringLiteral("artist")) {
        const QJsonArray tracks = data.value(QStringLiteral("tracks")).toArray();
        if (!tracks.isEmpty()) {
            auto* group = new QTreeWidgetItem(QStringList{QStringLiteral("Top tracks")});
            group->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("top_tracks_group")}, {QStringLiteral("tracks"), tracks}});
            item->addChild(group);
            for (const QJsonValue& track : tracks) {
                if (track.isObject()) group->addChild(makeItem(track.toObject(), QStringLiteral("track")));
            }
        }
        const auto addAlbumGroup = [this, item](const QString& label, const QJsonArray& albums) {
            if (albums.isEmpty()) return;
            auto* group = new QTreeWidgetItem(QStringList{label});
            group->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("group")}, {QStringLiteral("title"), label}});
            item->addChild(group);
            for (const QJsonValue& album : albums) {
                if (album.isObject()) group->addChild(makeItem(album.toObject(), QStringLiteral("album")));
            }
        };
        addAlbumGroup(QStringLiteral("Albums"), data.value(QStringLiteral("albums")).toArray());
        addAlbumGroup(QStringLiteral("EP & Singles"), data.value(QStringLiteral("ep_singles")).toArray());
        return;
    }
    for (const QJsonValue& track : data.value(QStringLiteral("tracks")).toArray()) {
        if (track.isObject()) item->addChild(makeItem(track.toObject(), QStringLiteral("track")));
    }
    for (const QJsonValue& album : data.value(QStringLiteral("albums")).toArray()) {
        if (album.isObject()) item->addChild(makeItem(album.toObject(), QStringLiteral("album")));
    }
    for (const QJsonValue& album : data.value(QStringLiteral("ep_singles")).toArray()) {
        if (album.isObject()) item->addChild(makeItem(album.toObject(), QStringLiteral("album")));
    }
}

void BrowserController::prepareLazyContainer(QTreeWidgetItem* item, const QJsonObject& data) const {
    if (!item) return;
    const QString type = data.value(QStringLiteral("_type")).toString();
    if (!isContainerType(type)) return;
    const bool hasLoadedArrays = !data.value(QStringLiteral("tracks")).toArray().isEmpty()
        || !data.value(QStringLiteral("items")).toArray().isEmpty()
        || !data.value(QStringLiteral("albums")).toArray().isEmpty()
        || !data.value(QStringLiteral("ep_singles")).toArray().isEmpty();
    if (item->childCount() > 0 || hasLoadedArrays) {
        item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
        return;
    }
    item->setData(0, kDetailsStateRole, QStringLiteral("unloaded"));
    auto* placeholder = new QTreeWidgetItem(QStringList{QStringLiteral("Loading")});
    placeholder->setData(0, kLoadingPlaceholderRole, true);
    placeholder->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("loading")}});
    placeholder->setFlags(placeholder->flags() & ~Qt::ItemIsSelectable);
    item->addChild(placeholder);
}

bool BrowserController::itemNeedsDetails(QTreeWidgetItem* item) const {
    if (!item) return false;
    const QJsonObject obj = itemObject(item);
    if (!isContainerType(obj.value(QStringLiteral("_type")).toString())) return false;
    if (obj.value(QStringLiteral("id")).toVariant().toString().isEmpty()) return false;
    const QString state = item->data(0, kDetailsStateRole).toString();
    return state != QStringLiteral("loading") && state != QStringLiteral("loaded");
}

bool BrowserController::isLoadingPlaceholder(QTreeWidgetItem* item) const {
    return item && item->data(0, kLoadingPlaceholderRole).toBool();
}

void BrowserController::showLoadingPlaceholder(QTreeWidgetItem* item) {
    if (!item) return;
    if (item->childCount() == 1 && isLoadingPlaceholder(item->child(0))) {
        QTreeWidgetItem* placeholder = item->child(0);
        placeholder->setText(0, QStringLiteral("Loading"));
        registerLoadingItem(placeholder);
        item->setExpanded(true);
        return;
    }
    for (int i = 0; i < item->childCount(); ++i) unregisterLoadingItem(item->child(i));
    qDeleteAll(item->takeChildren());
    auto* placeholder = new QTreeWidgetItem(QStringList{QStringLiteral("Loading")});
    placeholder->setData(0, kLoadingPlaceholderRole, true);
    placeholder->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("loading")}});
    placeholder->setFlags(placeholder->flags() & ~Qt::ItemIsSelectable);
    item->addChild(placeholder);
    registerLoadingItem(placeholder);
    item->setExpanded(true);
}

void BrowserController::addEmptyContainerPlaceholder(QTreeWidgetItem* item, const QString& type) {
    if (!item || item->childCount() > 0) return;
    const QString label = type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found");
    auto* empty = new QTreeWidgetItem(QStringList{label});
    empty->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
    empty->setFlags(empty->flags() & ~Qt::ItemIsSelectable);
    item->addChild(empty);
}

void BrowserController::registerLoadingItem(QTreeWidgetItem* item) {
    if (!item || m_loadingItems.contains(item)) return;
    m_loadingItems.push_back(item);
    if (m_loadingTimer && !m_loadingTimer->isActive()) m_loadingTimer->start();
}

void BrowserController::unregisterLoadingItem(QTreeWidgetItem* item) {
    if (!item) return;
    m_loadingItems.removeAll(item);
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void BrowserController::clearLoadingItemsForTree(QTreeWidget* tree) {
    if (!tree) return;
    QVector<QTreeWidgetItem*> alive;
    for (QTreeWidgetItem* item : std::as_const(m_loadingItems)) {
        if (item && item->treeWidget() != tree) alive.push_back(item);
    }
    m_loadingItems = alive;
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void BrowserController::tickLoadingLabels() {
    if (m_loadingItems.isEmpty()) {
        if (m_loadingTimer) m_loadingTimer->stop();
        return;
    }
    const QStringList phases{QStringLiteral("Loading"), QStringLiteral("Loading."), QStringLiteral("Loading.."), QStringLiteral("Loading...")};
    m_loadingPhase = (m_loadingPhase + 1) % phases.size();
    QVector<QTreeWidgetItem*> alive;
    for (QTreeWidgetItem* item : std::as_const(m_loadingItems)) {
        if (!item || !item->treeWidget()) continue;
        item->setText(0, phases.at(m_loadingPhase));
        alive.push_back(item);
    }
    m_loadingItems = alive;
    if (m_loadingItems.isEmpty() && m_loadingTimer) m_loadingTimer->stop();
}

void BrowserController::onTreeItemExpanded(QTreeWidgetItem* item) {
    if (itemNeedsDetails(item)) loadContainerDetails(item);
}

QJsonObject BrowserController::itemObject(QTreeWidgetItem* item) const {
    return item ? item->data(0, Qt::UserRole).toJsonObject() : QJsonObject{};
}

void BrowserController::loadContainerDetails(QTreeWidgetItem* item, bool playAfterLoad, bool queueAfterLoad) {
    if (!item) return;
    if (!m_tidal || !requireOnline(QStringLiteral("Details loading"))) return;
    const QJsonObject obj = itemObject(item);
    const QString type = obj.value(QStringLiteral("_type")).toString();
    const QString id = obj.value(QStringLiteral("id")).toVariant().toString();
    if (!isContainerType(type) || id.isEmpty()) return;
    const QString state = item->data(0, kDetailsStateRole).toString();
    if (state == QStringLiteral("loaded")) {
        if (playAfterLoad || queueAfterLoad) emit objectActionRequested(obj, playAfterLoad);
        return;
    }
    if (state == QStringLiteral("loading")) return;
    QTreeWidget* tree = item->treeWidget();
    item->setData(0, kDetailsStateRole, QStringLiteral("loading"));
    showLoadingPlaceholder(item);
    emit statusMessage(QStringLiteral("Loading %1...").arg(type));
    m_tidal->request(QStringLiteral("details"), {{QStringLiteral("type"), type}, {QStringLiteral("id"), id}}, [this, tree, type, id, playAfterLoad, queueAfterLoad](const QJsonObject& result) {
        QTreeWidgetItem* item = findItemByIdentity(tree, type, id);
        if (!item) return;
        QJsonObject detail = result.value(QStringLiteral("item")).toObject();
        if (detail.isEmpty()) {
            item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
            bool reusedPlaceholder = false;
            for (int i = 0; i < item->childCount(); ++i) {
                QTreeWidgetItem* child = item->child(i);
                if (!isLoadingPlaceholder(child)) continue;
                unregisterLoadingItem(child);
                child->setText(0, type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found"));
                child->setData(0, kLoadingPlaceholderRole, false);
                child->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
                reusedPlaceholder = true;
                break;
            }
            if (!reusedPlaceholder) addEmptyContainerPlaceholder(item, type);
            return;
        }
        detail.insert(QStringLiteral("_type"), type);
        item->setData(0, Qt::UserRole, detail);
        addChildren(item, detail);
        item->setData(0, kDetailsStateRole, QStringLiteral("loaded"));
        bool hasRealChildren = false;
        for (int i = 0; i < item->childCount(); ++i) {
            if (!isLoadingPlaceholder(item->child(i))) {
                hasRealChildren = true;
                break;
            }
        }
        if (hasRealChildren) {
            for (int i = item->childCount() - 1; i >= 0; --i) {
                QTreeWidgetItem* child = item->child(i);
                if (!isLoadingPlaceholder(child)) continue;
                unregisterLoadingItem(child);
                delete item->takeChild(i);
            }
        } else if (item->childCount() > 0 && isLoadingPlaceholder(item->child(0))) {
            QTreeWidgetItem* child = item->child(0);
            unregisterLoadingItem(child);
            child->setText(0, type == QStringLiteral("artist") ? QStringLiteral("No items found") : QStringLiteral("No tracks found"));
            child->setData(0, kLoadingPlaceholderRole, false);
            child->setData(0, Qt::UserRole, QJsonObject{{QStringLiteral("_type"), QStringLiteral("empty")}});
        } else {
            addEmptyContainerPlaceholder(item, type);
        }
        item->setExpanded(true);
        emit tracksDiscovered(QJsonArray{detail});
        emit detailLoaded(detail, type, id);
        emit statusMessage(QStringLiteral("Loaded %1").arg(type));
        if (playAfterLoad || queueAfterLoad) emit objectActionRequested(detail, playAfterLoad);
    }, [this, tree, type, id](const QString& error) {
        QTreeWidgetItem* item = findItemByIdentity(tree, type, id);
        if (item) {
            for (int i = 0; i < item->childCount(); ++i) unregisterLoadingItem(item->child(i));
            qDeleteAll(item->takeChildren());
            item->setData(0, kDetailsStateRole, QStringLiteral("unloaded"));
            item->setChildIndicatorPolicy(QTreeWidgetItem::ShowIndicator);
        }
        emit errorMessage(QStringLiteral("Details"), error);
    });
}

void BrowserController::requestDetailsForSingleLazyRoot(QTreeWidget* tree, bool queueAfterLoad) {
    if (!tree || tree->topLevelItemCount() != 1) return;
    QTreeWidgetItem* item = tree->topLevelItem(0);
    if (itemNeedsDetails(item)) loadContainerDetails(item, false, queueAfterLoad);
}

void BrowserController::queueLoadedUrlItems(QTreeWidget* tree) {
    if (!tree) return;
    for (int i = 0; i < tree->topLevelItemCount(); ++i) {
        QTreeWidgetItem* item = tree->topLevelItem(i);
        const QJsonObject obj = itemObject(item);
        if (trackObjects(obj).isEmpty() && itemNeedsDetails(item)) {
            loadContainerDetails(item, false, true);
            continue;
        }
        item->setExpanded(true);
        emit objectActionRequested(obj, false);
    }
}
