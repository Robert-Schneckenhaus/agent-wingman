#pragma once
#include <map>
#include "shop/order.h"

namespace shop {

class OrderRepository {
public:
    virtual ~OrderRepository() = default;
    virtual Order* find(int id) = 0;
};

class MemoryRepository : public OrderRepository {
public:
    Order* find(int id) override;

private:
    std::map<int, Order*> orders_;
};

}  // namespace shop
