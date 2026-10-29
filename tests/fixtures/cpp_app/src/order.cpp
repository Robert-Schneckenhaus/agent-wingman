#include "shop/order.h"
#include "shop/util.h"

namespace shop {

Order::Order(int id) : id_(id), cancelled_(false) {}

void Order::cancel() {
    cancelled_ = true;
}

double Order::total() const {
    return util::round(id_ * 1.19);
}

}  // namespace shop
