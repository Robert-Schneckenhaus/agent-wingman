#include "shop/repository.h"

namespace shop {

/** Use cases around orders. */
class OrderService {
public:
    explicit OrderService(OrderRepository* repository) : repository_(repository) {}

    void cancelOrder(int id) {
        Order* order = repository_->find(id);
        order->cancel();
    }

private:
    OrderRepository* repository_;
};

Order* MemoryRepository::find(int id) {
    return orders_[id];
}

}  // namespace shop
