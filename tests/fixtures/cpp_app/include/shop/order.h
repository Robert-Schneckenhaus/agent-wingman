#pragma once

namespace shop {

/** A customer order. */
class Order {
public:
    explicit Order(int id);
    /// Cancels the order unless it was shipped.
    void cancel();
    double total() const;

private:
    int id_;
    bool cancelled_;
};

}  // namespace shop
