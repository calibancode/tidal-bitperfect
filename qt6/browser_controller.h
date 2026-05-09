#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QVector>

#include <functional>

class QTimer;
class QTreeWidget;
class QTreeWidgetItem;
class TidalSidecar;

class BrowserController : public QObject {
    Q_OBJECT

public:
    using RequireOnlineCallback = std::function<bool(const QString& action)>;

    explicit BrowserController(TidalSidecar* sidecar, QObject* parent = nullptr);

    void setRequireOnlineCallback(RequireOnlineCallback callback);

    void loadHome(QTreeWidget* tree);
    void search(QTreeWidget* tree, const QString& query, const QString& type, int limit);
    void loadUrl(QTreeWidget* tree, const QString& url, bool queueAfterLoad = false);
    void refreshCollection(QTreeWidget* tree, const QString& type);
    void refreshFavoriteState();

    QJsonObject itemObject(QTreeWidgetItem* item) const;
    bool itemNeedsDetails(QTreeWidgetItem* item) const;
    void loadContainerDetails(QTreeWidgetItem* item, bool playAfterLoad = false, bool queueAfterLoad = false);

public slots:
    void onTreeItemExpanded(QTreeWidgetItem* item);

signals:
    void statusMessage(const QString& message);
    void errorMessage(const QString& title, const QString& message);
    void tracksDiscovered(const QJsonArray& items);
    void favoriteItemsDiscovered(const QString& type, const QJsonArray& items);
    void detailLoaded(const QJsonObject& item, const QString& type, const QString& id);
    void objectActionRequested(const QJsonObject& object, bool playFirst);

private slots:
    void tickLoadingLabels();

private:
    bool requireOnline(const QString& action);
    void populateTree(QTreeWidget* tree, const QJsonArray& items, const QString& typeHint = QString(), bool expandRoots = false, bool loadSingleLazyRoot = false);
    QTreeWidgetItem* makeItem(const QJsonObject& obj, const QString& typeHint = QString()) const;
    void addChildren(QTreeWidgetItem* item, const QJsonObject& data) const;
    void prepareLazyContainer(QTreeWidgetItem* item, const QJsonObject& data) const;
    void showLoadingPlaceholder(QTreeWidgetItem* item);
    void addEmptyContainerPlaceholder(QTreeWidgetItem* item, const QString& type);
    bool isLoadingPlaceholder(QTreeWidgetItem* item) const;
    void registerLoadingItem(QTreeWidgetItem* item);
    void unregisterLoadingItem(QTreeWidgetItem* item);
    void clearLoadingItemsForTree(QTreeWidget* tree);
    void requestDetailsForSingleLazyRoot(QTreeWidget* tree, bool queueAfterLoad);
    void queueLoadedUrlItems(QTreeWidget* tree);

    TidalSidecar* m_sidecar = nullptr;
    RequireOnlineCallback m_requireOnline;
    QTimer* m_loadingTimer = nullptr;
    QVector<QTreeWidgetItem*> m_loadingItems;
    int m_loadingPhase = 0;
};
